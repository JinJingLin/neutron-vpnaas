#    (c) Copyright 2016 IBM Corporation, All Rights Reserved.
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
import netaddr

from neutron import context as nctx
from neutron import manager
from neutron.plugins.common import constants as nconstants

from neutron_vpnaas.db.vpn import vpn_ext_gw_db

from neutron_vpnaas.extensions import vpnaas
from neutron_vpnaas.services.vpn.service_drivers import ipsec_validator


class OVNVpnValidator(ipsec_validator.IpsecVpnValidator):

    # overwrite validator function to use vpn gw external ip

    def __init__(self, service_plugin):
        super(OVNVpnValidator, self).__init__(service_plugin)

    @property
    def vpn_plugin(self):
        try:
            return self._vpn_plugin
        except AttributeError:
            self._vpn_plugin = manager.NeutronManager.get_service_plugins().\
	        get(nconstants.VPN)
            return self._vpn_plugin

    def _check_router(self, context, router_id):
        gateway = self.vpn_plugin.get_vpn_gw_dict_by_router_id(
            context, router_id)
        if gateway is None or gateway['external_fixed_ips'] is None:
            raise vpn_ext_gw_db.RouterIsNotVPNExternal(router_id=router_id)

    def _validate_peer_address(self, ip_version, router):
        context = nctx.get_admin_context()
        gateway = self.vpn_plugin.get_vpn_gw_dict_by_router_id(
            context, router['id'])
        if gateway is None or gateway['external_fixed_ips'] is None:
            raise vpn_ext_gw_db.RouterIsNotVPNExternal(router_id=router.id)

        for fixed_ip in gateway['external_fixed_ips']:
            addr = fixed_ip['ip_address']
            if ip_version == netaddr.IPAddress(addr).version:
                return

        raise vpnaas.ExternalNetworkHasNoSubnet(
            router_id=router.id,
            ip_version='IPv6' if ip_version == 6 else "IPv4")
