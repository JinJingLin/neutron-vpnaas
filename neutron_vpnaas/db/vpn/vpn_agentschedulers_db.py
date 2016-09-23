# Copyright (c) 2013 OpenStack Foundation.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import debtcollector
from neutron_lib import constants
from oslo_config import cfg
from oslo_db import exception as db_exc
from oslo_log import log as logging
import oslo_messaging
import six
import sqlalchemy as sa
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy import orm
from sqlalchemy.orm import joinedload
from sqlalchemy import sql

from neutron._i18n import _
from neutron._i18n import _LI
from neutron.common import utils as n_utils
from neutron.db import agents_db
from neutron.db import agentschedulers_db
from neutron.db import l3_attrs_db
from neutron.db import model_base

from neutron.extensions import router_availability_zone as router_az
from neutron import manager
from neutron.plugins.common import constants as service_constants

from neutron_vpnaas.extensions import vpn_agentschedulers


LOG = logging.getLogger(__name__)

VPN_AGENTS_SCHEDULER_OPTS = [
    cfg.StrOpt('vpn_scheduler_driver',
               default='neutron_vpnaas.scheduler.vpn_agent_scheduler'
                       '.LeastRoutersScheduler',
               help=_('Driver to use for scheduling '
                      'router to a default vpn agent')),
    cfg.BoolOpt('vpn_auto_schedule', default=True,
                help=_('Allow auto scheduling of routers to vpn agent.')),
    cfg.BoolOpt('allow_automatic_vpnagent_failover', default=False,
                help=_('Automatically reschedule routers from offline vpn '
                       'agents to online vpn agents.')),
]

cfg.CONF.register_opts(VPN_AGENTS_SCHEDULER_OPTS)


class RouterVPNAgentBinding(model_base.BASEV2):
    """Represents binding between neutron routers and L3 agents."""

    router_id = sa.Column(sa.String(36),
                          sa.ForeignKey("routers.id", ondelete='CASCADE'),
                          primary_key=True)
    vpn_agent = orm.relation(agents_db.Agent)
    vpn_agent_id = sa.Column(sa.String(36),
                             sa.ForeignKey("agents.id", ondelete='CASCADE'),
                             primary_key=True)


class VPNAgentSchedulerDbMixin(vpn_agentschedulers.VPNAgentSchedulerPluginBase,
                               agentschedulers_db.AgentSchedulerDbMixin):
    """Mixin class to add vpn agent scheduler extension to plugins
    using the vpn agent for routing.
    """

    vpn_scheduler = None

    @property
    def l3_plugin(self):
        return manager.NeutronManager.get_service_plugins().get(
            service_constants.L3_ROUTER_NAT)

    @debtcollector.removals.remove(
        message="This will be removed in the O cycle. "
                "Please use 'add_periodic_vpn_agent_status_check' instead."
    )
    def start_periodic_vpn_agent_status_check(self):
        if not cfg.CONF.allow_automatic_vpnagent_failover:
            LOG.info(_LI("Skipping period vpn agent status check because "
                         "automatic router rescheduling is disabled."))
            return

        self.add_agent_status_check(
            self.reschedule_routers_from_down_agents)

    def add_periodic_vpn_agent_status_check(self):
        if not cfg.CONF.allow_automatic_vpnagent_failover:
            LOG.info(_LI("Skipping period vpn agent status check because "
                         "automatic router rescheduling is disabled."))
            return

        self.add_agent_status_check_worker(
            self.reschedule_routers_from_down_agents)

    def reschedule_routers_from_down_agents(self):
        """Reschedule routers from down vpn agents if admin state is up."""
        self.reschedule_resources_from_down_agents(
            agent_type='L3',
            get_down_bindings=self.get_down_router_bindings,
            agent_id_attr='vpn_agent_id',
            resource_id_attr='router_id',
            resource_name='router',
            reschedule_resource=self.reschedule_router,
            rescheduling_failed=vpn_agentschedulers.RouterReschedulingFailed)

    def get_down_router_bindings(self, context, agent_dead_limit):
        cutoff = self.get_cutoff_time(agent_dead_limit)
        return (context.session.query(RouterVPNAgentBinding).
            join(agents_db.Agent).
            filter(agents_db.Agent.heartbeat_timestamp < cutoff,
                   agents_db.Agent.admin_state_up).
            outerjoin(l3_attrs_db.RouterExtraAttributes,
                      l3_attrs_db.RouterExtraAttributes.router_id ==
                      RouterVPNAgentBinding.router_id).filter(
            sa.or_(
                l3_attrs_db.RouterExtraAttributes.ha == sql.false(),
                l3_attrs_db.RouterExtraAttributes.ha == sql.null())))

    def _get_agent_mode(self, agent_db):
        agent_conf = self.get_configuration_dict(agent_db)
        return agent_conf.get(constants.L3_AGENT_MODE,
                              constants.L3_AGENT_MODE_LEGACY)

    def validate_agent_router_combination(self, context, agent, router):
        """Validate if the router can be correctly assigned to the agent.

        :raises: InvalidL3Agent if attempting to assign router to an
          unsuitable agent (disabled, type != L3, incompatible configuration)
        """
        if agent['agent_type'] != constants.AGENT_TYPE_L3:
            raise vpn_agentschedulers.InvalidL3Agent(id=agent['id'])

        is_suitable_agent = (
            agentschedulers_db.services_available(agent['admin_state_up']) and
            self.get_vpn_agent_candidates(context, router,
                                          [agent],
                                          ignore_admin_state=True))
        if not is_suitable_agent:
            raise vpn_agentschedulers.InvalidL3Agent(id=agent['id'])

    def check_agent_router_scheduling_needed(self, context, agent, router):
        """Check if the router scheduling is needed.

        :raises: RouterHostedByL3Agent if router is already assigned
          to a different agent.
        :returns: True if scheduling is needed, otherwise False
        """
        router_id = router['id']
        agent_id = agent['id']
        query = context.session.query(RouterVPNAgentBinding)
        bindings = query.filter_by(router_id=router_id).all()
        if not bindings:
            return True
        for binding in bindings:
            if binding.vpn_agent_id == agent_id:
                # router already bound to the agent we need
                return False
        if self.vpn_router_is_ha(context, router_id):
            return True
        # legacy router case: router is already bound to some agent
        raise vpn_agentschedulers.RouterHostedByL3Agent(
            router_id=router_id,
            agent_id=bindings[0].vpn_agent_id)

    def create_router_to_agent_binding(self, context, agent, router):
        """Create router to agent binding."""
        router_id = router['id']
        agent_id = agent['id']
        if self.vpn_scheduler:
            try:
                if self.vpn_router_is_ha(context, router_id):
                    plugin = manager.NeutronManager.get_service_plugins().get(
                        service_constants.VPN)
                    self.vpn_scheduler.create_ha_port_and_bind(
                        plugin, context, router['id'],
                        router['tenant_id'], agent)
                else:
                    self.vpn_scheduler.bind_router(
                        context, router_id, agent)
            except db_exc.DBError:
                raise vpn_agentschedulers.RouterSchedulingFailed(
                    router_id=router_id, agent_id=agent_id)

    def add_router_to_vpn_agent(self, context, agent_id, router_id):
        """Add a vpn agent to host a router."""
        with context.session.begin(subtransactions=True):
            router = self.l3_plugin.get_router(context, router_id)
            agent = self._get_agent(context, agent_id)
            self.validate_agent_router_combination(context, agent, router)
            if not self.check_agent_router_scheduling_needed(
                    context, agent, router):
                return
        self.create_router_to_agent_binding(context, agent, router)

        vpn_notifier = self.agent_notifiers.get(constants.AGENT_TYPE_L3)
        if vpn_notifier:
            vpn_notifier.router_added_to_agent(
                context, [router_id], agent.host)

    def remove_router_from_vpn_agent(self, context, agent_id, router_id):
        """Remove the router from vpn agent.

        After removal, the router will be non-hosted until there is update
        which leads to re-schedule or be added to another agent manually.
        """
        agent = self._get_agent(context, agent_id)

        self._unbind_router(context, router_id, agent_id)

        #plugin = manager.NeutronManager.get_service_plugins().get(
        #    service_constants.VPN)
        if self.vpn_router_is_ha(context, router_id):
            self.delete_ha_interfaces_on_host(context, router_id, agent.host)
        retain_router = False
        vpn_notifier = self.agent_notifiers.get(constants.AGENT_TYPE_L3)
        if retain_router and vpn_notifier:
            vpn_notifier.routers_updated_on_host(
                context, [router_id], agent.host)
        elif vpn_notifier:
            vpn_notifier.router_removed_from_agent(
                context, router_id, agent.host)

    def _unbind_router(self, context, router_id, agent_id):
        with context.session.begin(subtransactions=True):
            query = context.session.query(RouterVPNAgentBinding)
            query = query.filter(
                RouterVPNAgentBinding.router_id == router_id,
                RouterVPNAgentBinding.vpn_agent_id == agent_id)
            query.delete()

    def _unschedule_router(self, context, router_id, agents_ids):
        with context.session.begin(subtransactions=True):
            for agent_id in agents_ids:
                self._unbind_router(context, router_id, agent_id)

    def reschedule_router(self, context, router_id, candidates=None):
        """Reschedule router to (a) new vpn agent(s)

        Remove the router from the agent(s) currently hosting it and
        schedule it again
        """
        cur_agents = self.list_vpn_agents_hosting_router(
            context, router_id)['agents']
        with context.session.begin(subtransactions=True):
            cur_agents_ids = [agent['id'] for agent in cur_agents]
            self._unschedule_router(context, router_id, cur_agents_ids)

            self.schedule_router(context, router_id, candidates=candidates)
            new_agents = self.list_vpn_agents_hosting_router(
                context, router_id)['agents']
            if not new_agents:
                raise vpn_agentschedulers.RouterReschedulingFailed(
                    router_id=router_id)

        self._notify_agents_router_rescheduled(context, router_id,
                                               cur_agents, new_agents)

    def _notify_agents_router_rescheduled(self, context, router_id,
                                          old_agents, new_agents):
        vpn_notifier = self.agent_notifiers.get(constants.AGENT_TYPE_L3)
        if not vpn_notifier:
            return

        old_hosts = [agent['host'] for agent in old_agents]
        new_hosts = [agent['host'] for agent in new_agents]
        for host in set(old_hosts) - set(new_hosts):
            vpn_notifier.router_removed_from_agent(
                context, router_id, host)

        for agent in new_agents:
            try:
                vpn_notifier.router_added_to_agent(
                    context, [router_id], agent['host'])
            except oslo_messaging.MessagingException:
                self._unbind_router(context, router_id, agent['id'])
                raise vpn_agentschedulers.RouterReschedulingFailed(
                    router_id=router_id)

    def list_routers_on_vpn_agent(self, context, agent_id):
        query = context.session.query(RouterVPNAgentBinding.router_id)
        query = query.filter(RouterVPNAgentBinding.vpn_agent_id == agent_id)

        router_ids = [item[0] for item in query]
        if router_ids:
            return {'routers':
                    self.l3_plugin.get_routers(context,
                    filters={'id': router_ids})}
        else:
            # Exception will be thrown if the requested agent does not exist.
            self._get_agent(context, agent_id)
            return {'routers': []}

    def _get_active_vpn_agent_routers_sync_data(self, context, host, agent,
                                                router_ids):
        if n_utils.is_extension_supported(self,
                                          constants.L3_HA_MODE_EXT_ALIAS):
            routers = self.get_ha_sync_data_for_host(context, host, agent,
                                                     router_ids=router_ids,
                                                     active=True)
        else:
            routers = self.get_sync_data(context, router_ids=router_ids,
                                         active=True)
        return self.filter_allocating_and_missing_routers(context, routers)

    def list_router_ids_on_host(self, context, host, router_ids=None):
        agent = self._get_agent_by_type_and_host(
            context, constants.AGENT_TYPE_L3, host)
        if not agentschedulers_db.services_available(agent.admin_state_up):
            return []
        return self._get_router_ids_for_agent(context, agent, router_ids)

    def _get_router_ids_for_agent(self, context, agent, router_ids):
        """Get IDs of routers that the agent should host
        """
        query = context.session.query(RouterVPNAgentBinding.router_id)
        query = query.filter(
            RouterVPNAgentBinding.vpn_agent_id == agent.id)

        if router_ids:
            query = query.filter(
                RouterVPNAgentBinding.router_id.in_(router_ids))

        return [item[0] for item in query]

    def list_active_sync_routers_on_active_vpn_agent(
            self, context, host, router_ids):
        agent = self._get_agent_by_type_and_host(
            context, constants.AGENT_TYPE_L3, host)
        if not agentschedulers_db.services_available(agent.admin_state_up):
            LOG.info(_LI("Agent has its services disabled. Returning "
                         "no active routers. Agent: %s"), agent)
            return []
        scheduled_router_ids = self._get_router_ids_for_agent(
            context, agent, router_ids)
        diff = set(router_ids or []) - set(scheduled_router_ids or [])
        if diff:
            LOG.debug("Agent requested router IDs not scheduled to it. "
                      "Scheduled: %(sched)s. Unscheduled: %(diff)s. "
                      "Agent: %(agent)s.",
                      {'sched': scheduled_router_ids, 'diff': diff,
                       'agent': agent})
        if scheduled_router_ids:
            return self._get_active_vpn_agent_routers_sync_data(
                context, host, agent, scheduled_router_ids)
        return []

    def get_vpn_agents_hosting_routers(self, context, router_ids,
                                       admin_state_up=None,
                                       active=None):
        if not router_ids:
            return []
        query = context.session.query(RouterVPNAgentBinding)
        query = query.options(orm.contains_eager(
            RouterVPNAgentBinding.vpn_agent))
        query = query.join(RouterVPNAgentBinding.vpn_agent)
        query = query.filter(RouterVPNAgentBinding.router_id.in_(router_ids))
        if admin_state_up is not None:
            query = (query.filter(agents_db.Agent.admin_state_up ==
                                  admin_state_up))
        vpn_agents = [binding.vpn_agent for binding in query]
        if active is not None:
            vpn_agents = [vpn_agent for vpn_agent in
                          vpn_agents if not
                          agents_db.AgentDbMixin.is_agent_down(
                              vpn_agent['heartbeat_timestamp'])]
        return vpn_agents

    def _get_vpn_bindings_hosting_routers(self, context, router_ids):
        if not router_ids:
            return []
        query = context.session.query(RouterVPNAgentBinding)
        query = query.options(joinedload('vpn_agent')).filter(
            RouterVPNAgentBinding.router_id.in_(router_ids))
        return query.all()

    def list_vpn_agents_hosting_router(self, context, router_id):
        with context.session.begin(subtransactions=True):
            bindings = self._get_vpn_bindings_hosting_routers(
                context, [router_id])

        return {'agents': [self._make_agent_dict(binding.vpn_agent) for
                           binding in bindings]}

    def get_vpn_agents(self, context, active=None, filters=None):
        query = context.session.query(agents_db.Agent)
        query = query.filter(
            agents_db.Agent.agent_type == constants.AGENT_TYPE_L3)
        if active is not None:
            query = (query.filter(agents_db.Agent.admin_state_up == active))
        if filters:
            for key, value in six.iteritems(filters):
                column = getattr(agents_db.Agent, key, None)
                if column:
                    if not value:
                        return []
                    query = query.filter(column.in_(value))

            agent_modes = filters.get('agent_modes', [])
            if agent_modes:
                agent_mode_key = '\"agent_mode\": \"'
                configuration_filter = (
                    [agents_db.Agent.configurations.contains('%s%s\"' %
                                                             (agent_mode_key,
                                                              agent_mode))
                     for agent_mode in agent_modes])
                query = query.filter(or_(*configuration_filter))

        return [vpn_agent
                for vpn_agent in query
                if agentschedulers_db.AgentSchedulerDbMixin.is_eligible_agent(
                    active, vpn_agent)]

    def get_vpn_agent_candidates(self, context, sync_router, vpn_agents,
                                 ignore_admin_state=False):
        """Get the valid vpn agents for the router from a list of vpn_agents.
        """
        candidates = []
        for vpn_agent in vpn_agents:
            if not ignore_admin_state and not vpn_agent.admin_state_up:
                # ignore_admin_state True comes from manual scheduling
                # where admin_state_up judgement is already done.
                continue

            agent_conf = self.get_configuration_dict(vpn_agent)

            router_id = agent_conf.get('router_id', None)
            if router_id and router_id != sync_router['id']:
                continue

            handle_internal_only_routers = agent_conf.get(
                'handle_internal_only_routers', True)
            gateway_external_network_id = agent_conf.get(
                'gateway_external_network_id', None)

            ex_net_id = (sync_router['external_gateway_info'] or {}).get(
                'network_id')
            if ((not ex_net_id and not handle_internal_only_routers) or
                    (ex_net_id and gateway_external_network_id and
                     ex_net_id != gateway_external_network_id)):
                continue

            candidates.append(vpn_agent)
        return candidates

    def auto_schedule_routers(self, context, host, router_ids):
        if self.vpn_scheduler:
            return self.vpn_scheduler.auto_schedule_routers(
                self, context, host, router_ids)

    def schedule_router(self, context, router, candidates=None):
        if self.vpn_scheduler:
            return self.vpn_scheduler.schedule(
                self, context, router, candidates=candidates)

    def schedule_routers(self, context, routers):
        """Schedule the routers to vpn agents."""
        for router in routers:
            self.schedule_router(context, router, candidates=None)

    def get_vpn_agent_with_min_routers(self, context, agent_ids):
        """Return vpn agent with the least number of routers."""
        if not agent_ids:
            return None
        query = context.session.query(
            agents_db.Agent,
            func.count(
                RouterVPNAgentBinding.router_id
            ).label('count')).outerjoin(RouterVPNAgentBinding).group_by(
            agents_db.Agent.id,
            RouterVPNAgentBinding.vpn_agent_id).order_by('count')
        res = query.filter(agents_db.Agent.id.in_(agent_ids)).first()
        return res[0]

    def get_hosts_to_notify(self, context, router_id):
        """Returns all hosts to send notification about router update"""
        state = agentschedulers_db.get_admin_state_up_filter()
        agents = self.get_vpn_agents_hosting_routers(
            context, [router_id], admin_state_up=state, active=True)
        return [a.host for a in agents]


class AZVPNAgentSchedulerDbMixin(VPNAgentSchedulerDbMixin,
                                 router_az.RouterAvailabilityZonePluginBase):
    """Mixin class to add availability_zone supported vpn agent scheduler."""

    def get_router_availability_zones(self, router):
        return list({agent.availability_zone for agent in router.vpn_agents})
