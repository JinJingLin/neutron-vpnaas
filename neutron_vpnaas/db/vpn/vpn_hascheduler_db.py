# Copyright (C) 2014 eNovance SAS <licensing@enovance.com>
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from neutron_lib import constants
from sqlalchemy import func
from sqlalchemy import sql

from neutron.callbacks import events
from neutron.callbacks import registry
from neutron.callbacks import resources
from neutron.db import agents_db
from neutron.db import l3_agentschedulers_db as vpn_sch_db
from neutron.db import l3_attrs_db
from neutron.db import l3_db
from neutron.extensions import portbindings
from neutron import manager
from neutron.plugins.common import constants as service_constants

from neutron_vpnaas.db.vpn import vpn_agentschedulers_db as vpn_sch_db

class VPN_HA_scheduler_db_mixin(vpn_sch_db.AZVPNAgentSchedulerDbMixin):

    def get_ha_routers_vpn_agents_count(self, context):
        """Return a map between HA routers and how many agents every
        router is scheduled to.
        """

        # Postgres requires every column in the select to be present in
        # the group by statement when using an aggregate function.
        # One solution is to generate a subquery and join it with the desired
        # columns.
        binding_model = vpn_sch_db.RouterL3AgentBinding
        sub_query = (context.session.query(
            binding_model.router_id,
            func.count(binding_model.router_id).label('count')).
            join(vpn_attrs_db.RouterExtraAttributes,
                 binding_model.router_id ==
                 vpn_attrs_db.RouterExtraAttributes.router_id).
            join(vpn_db.Router).
            group_by(binding_model.router_id).subquery())

        query = (context.session.query(vpn_db.Router, sub_query.c.count).
                 join(sub_query))

        return [(self._make_router_dict(router), agent_count)
                for router, agent_count in query]

    def get_vpn_agents_ordered_by_num_routers(self, context, agent_ids):
        if not agent_ids:
            return []
        query = (context.session.query(agents_db.Agent, func.count(
            vpn_sch_db.RouterVPNAgentBinding.router_id).label('count')).
            outerjoin(vpn_sch_db.RouterVPNAgentBinding).
            group_by(agents_db.Agent.id).
            filter(agents_db.Agent.id.in_(agent_ids)).
            order_by('count'))

        return [record[0] for record in query]

    def _get_agents_dict_for_router(self, agents_and_states):
        agents = []
        for agent, ha_state in agents_and_states:
            vpn_agent_dict = self._make_agent_dict(agent)
            vpn_agent_dict['ha_state'] = ha_state
            agents.append(vpn_agent_dict)
        return {'agents': agents}

    def list_vpn_agents_hosting_router(self, context, router_id):
        with context.session.begin(subtransactions=True):
            if self.vpn_router_is_ha(context, router_id):
                bindings = self.get_vpn_bindings_hosting_router_with_ha_states(
                    context, router_id)
            else:
                bindings = self._get_vpn_bindings_hosting_routers(
                    context, [router_id])
                bindings = [(binding.vpn_agent, None) for binding in bindings]

        return self._get_agents_dict_for_router(bindings)


def _notify_vpn_agent_ha_port_update(resource, event, trigger, **kwargs):
    new_port = kwargs.get('port')
    original_port = kwargs.get('original_port')
    context = kwargs.get('context')
    host = new_port[portbindings.HOST_ID]

    if new_port and original_port and host:
        new_device_owner = new_port.get('device_owner', '')
        if (new_device_owner == constants.DEVICE_OWNER_ROUTER_HA_INTF and
            new_port['status'] == constants.PORT_STATUS_ACTIVE and
            original_port['status'] != new_port['status']):
            l3_plugin = manager.NeutronManager.get_service_plugins().get(
                service_constants.L3_ROUTER_NAT)
            l3_plugin.vpn_rpc_notifier.routers_updated_on_host(
                context, [new_port['device_id']], host)


def subscribe():
    registry.subscribe(
        _notify_vpn_agent_ha_port_update, resources.PORT, events.AFTER_UPDATE)
