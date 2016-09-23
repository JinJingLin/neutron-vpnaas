#    (c) Copyright 2016 IBM Corporation
#    All Rights Reserved.
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

from neutron import manager

from oslo_config import cfg
from oslo_utils import importutils

from neutron.common import constants as n_constants

from neutron_vpnaas.db.vpn.vpn_db import VPNPluginDb

from neutron_vpnaas.db.vpn import vpn_agentschedulers_db as agent_db
from neutron_vpnaas.db.vpn import vpn_ext_gw_db
from neutron_vpnaas.db.vpn import vpn_hamode_db
from neutron_vpnaas.db.vpn import vpn_hascheduler_db as vpn_hascheduler_db
from neutron_vpnaas.db.vpn import vpn_models

from neutron_vpnaas.services.vpn.plugin import VPNDriverPlugin

class VPNOVNPlugin(VPNPluginDb,
                   vpn_ext_gw_db.VPNExtGWPlugin_db,
                   vpn_hascheduler_db.VPN_HA_scheduler_db_mixin,
                   vpn_hamode_db.VPN_HA_Router_db_mixin):
    """Implementation of the VPN Service Plugin.

    This class manages the workflow of VPNaaS request/response.
    Most DB related works are implemented in class
    vpn_db.VPNPluginDb.
    """
    def __init__(self):
        self.vpn_scheduler = importutils.import_object(
            cfg.CONF.vpn_scheduler_driver)
        super(VPNOVNPlugin, self).__init__()

    def check_router_in_use(self, context, router_id):
        pass

    supported_extension_aliases = ["vpnaas",
                                   "vpn-endpoint-groups",
                                   "service-type",
                                   "vpn-ext-gw",
                                   "vpn-agent-scheduler"]
    path_prefix = "/vpn"


class VPNOVNDriverPlugin(VPNOVNPlugin, VPNDriverPlugin):
    def _get_agent_hosting_vpn_services(self, context, host):
        plugin = manager.NeutronManager.get_plugin()
        agent = plugin._get_agent_by_type_and_host(
            context, n_constants.AGENT_TYPE_L3, host)

        if not agent.admin_state_up:
            return []
        query = context.session.query(vpn_models.VPNService)
        query = query.join(vpn_models.IPsecSiteConnection)
        query = query.join(agent_db.RouterVPNAgentBinding,
                           agent_db.RouterVPNAgentBinding.router_id ==
                           vpn_models.VPNService.router_id)
        query = query.filter(
            agent_db.RouterVPNAgentBinding.vpn_agent_id == agent.id)
        return query
