# Copyright 2016, Yi Jing Zhu, IBM.
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
import collections
import netaddr

from neutron.common import rpc as n_rpc
from neutron import context as nctx
from neutron import manager
from neutron.plugins.common import constants as service_constants
from neutron_vpnaas.extensions.vpn_ext_gw import RouterIsNotVPNExternal
from neutron_vpnaas.services.vpn.common import topics
from neutron_vpnaas.services.vpn.service_drivers import base_ipsec
from neutron_vpnaas.services.vpn.service_drivers import \
    ovn_validator as ipsec_validator
from oslo_log import log as logging


LOG = logging.getLogger(__name__)

IPSEC = 'ipsec'
BASE_IPSEC_VERSION = '1.0'

'''
Below define are copied from networking_ovn to avoid pep8 checking failure.
They should be removed when the networking_ovn transit network API is ready
'''

OVN_NETWORK_NAME_EXT_ID_KEY = 'neutron:network_name'
OVN_PORT_NAME_EXT_ID_KEY = 'neutron:port_name'
OVN_VPN_ADMIN_NET_PORT_DEVICE_ID = 'OVN_VPN_ADMIN_NETWORK_PORT'
OVN_VPN_ADMIN_NET_PORT_DEVICE_OWNER = 'OVN_VPN_ADMIN_NETWORK'
OVN_TRANSIT_LS_NAME_PREFIX = 'otls'

OVN_MAX_HA_AGENTS = 8

'''
Below methods are copied from networking_ovn to avoid pep8 checking failure.
They should be removed when the networking_ovn transit network API is
ready
'''


def ovn_name(id):
    # The name of the OVN entry will be neutron-<UUID>
    # This is due to the fact that the OVN application checks if the name
    # is a UUID. If so then there will be no matches.
    # We prefix the UUID to enable us to use the Neutron UUID when
    # updating, deleting etc.
    return 'neutron-%s' % id


def ovn_lrouter_port_name(id):
    # The name of the OVN lrouter port entry will be lrp-<UUID>
    # This is to distinguish with the name of the connected lswitch patch port,
    # which is named with neutron port uuid, so that OVS patch ports are
    # generated properly. The pairing patch port names will be:
    #   - patch-lrp-<UUID>-to-<UUID>
    #   - patch-<UUID>-to-lrp-<UUID>
    # lrp stands for Logical Router Port
    return 'lrp-%s' % id


def ovn_name(id):
    # The name of the OVN entry will be neutron-<UUID>
    # This is due to the fact that the OVN application checks if the name
    # is a UUID. If so then there will be no matches.
    # We prefix the UUID to enable us to use the Neutron UUID when
    # updating, deleting etc.
    return 'neutron-%s' % id


def ovn_transit_ls_name(id):
    "Ovn Transit Logical Switch"
    return '%s-%s' % (OVN_TRANSIT_LS_NAME_PREFIX, id)


def ovn_vdtsp_name(id):
    "Distributed Transit switch port"
    return 'vdtsp-%s' % id


def ovn_vtsp_name(vtsp, id):
    "Distributed Transit switch port"
    return '%s-%s' % (vtsp, id)


class IPsecHelper(object):
    def __init__(self):
        # TODO (xianms) Checking if the l3 plugin type is ovn
        self.l3_admin_net_cidr = self.l3_plugin.l3_admin_net_cidr

    @property
    def l3_plugin(self):
        return manager.NeutronManager.get_service_plugins().get(
            service_constants.L3_ROUTER_NAT)

    @property
    def service_plugin(self):
        return manager.NeutronManager.get_service_plugins().get(
            service_constants.VPN)

    @property
    def _ovn(self):
        return self.l3_plugin._ovn

    @property
    def _admin_net(self):
        return self.l3_plugin._admin_net

    def get_vpn_transit_port_names(self, agent_number=1):
        name = ['vdtsp']
        for agent in range(agent_number):
            name.append('vtsp' + str(agent))

        # HA is enabled if there are more than one agents for the router
        if agent_number > 1:
            name.append('vip')
        return name

    def _get_transit_network_ports(self, router_id, agent_number=1,
                                   create=False):
        name = self.get_vpn_transit_port_names(agent_number)
        transit_net_ports = {}
        ports = self._admin_net.get_l3_admin_net_ports(
            name,
            OVN_VPN_ADMIN_NET_PORT_DEVICE_ID,
            OVN_VPN_ADMIN_NET_PORT_DEVICE_OWNER, create)
        for port in ports:
            ip = port.get('fixed_ips')[0].get('ip_address')
            port.update({'ip': ip,
                         'addresses': port.get('mac_address') + ' ' + ip})
            key = port.get('name').lower()

            # convert neutron port name to ovn port name
            if port.get('name') == 'vdtsp':
                port['name'] = ovn_vdtsp_name(router_id)
            elif port.get('name') == 'vip':
                pass
            else:
                port['name'] = ovn_vtsp_name(port['name'], router_id)

            transit_net_ports[key] = port

        return transit_net_ports

    def _join_lrouter_and_namespace(self, router_id, transit_net_ports):
        lswitch_name = ovn_transit_ls_name(router_id)

        lrouter_name = ovn_name(router_id)
        cidr = netaddr.IPNetwork(self.l3_admin_net_cidr)

        with self._ovn.transaction(check_error=True) as txn:
            # 1. Create a transit logical switch
            txn.add(self._ovn.create_lswitch(lswitch_name=lswitch_name))

            # 3. Add vtsp ports
            for key in transit_net_ports.keys():
                if 'vtsp' in key:
                    vtsp_name = ovn_vtsp_name(key, router_id)
                    vtsp_address = transit_net_ports[key]['addresses']
                    txn.add(
                        self._ovn.create_lswitch_port(
                            lport_name=vtsp_name,
                            lswitch_name=lswitch_name,
                            addresses=vtsp_address,
                            enabled='True'))
                if 'vdtsp' in key:
                    # 2. Add vdtsp port
                    vdtsp_name = ovn_vdtsp_name(router_id)
                    vdtsp_addresses = transit_net_ports['vdtsp']['addresses']
                    vdtrp_name = ovn_lrouter_port_name(
                        ovn_vdtsp_name(router_id))
                    vdtrp_mac = transit_net_ports['vdtsp']['mac_address']
                    vdtrp_ip = transit_net_ports['vdtsp']['ip']
                    vdtrp_network = "%s/%s" % (vdtrp_ip, str(cidr.prefixlen))

                    txn.add(
                        self._ovn.create_lswitch_port(lport_name=vdtsp_name,
                                                      lswitch_name=lswitch_name,
                                                      addresses=vdtsp_addresses,
                                                      enabled='True'))

                    # 4. Add vdtrp port in logical router
                    txn.add(self._ovn.add_lrouter_port(name=vdtrp_name,
                                                       lrouter=lrouter_name,
                                                       mac=vdtrp_mac,
                                                       networks=vdtrp_network))
                    try:
                        txn.add(self._ovn.set_lrouter_port_in_lswitch_port(
                            ovn_vdtsp_name(router_id), vdtrp_name))
                    except:
                        pass


    def _disjoin_lrouter_and_namespace(self, router_id, transit_net_ports):
        lrouter_name = ovn_name(router_id)
        lswitch_name = ovn_transit_ls_name(router_id)
        vdtsp_name = ovn_vdtsp_name(router_id)
        vdtrp_name = ovn_lrouter_port_name(ovn_vdtsp_name(router_id))

        with self._ovn.transaction(check_error=True) as txn:
            # 1. Delete vstp ports
            for key in transit_net_ports.keys():
                if 'vtsp' in key:
                    vtsp_name = ovn_vtsp_name(key, router_id)
                    txn.add(self._ovn.delete_lswitch_port(vtsp_name,
                                                          lswitch_name))
                if 'vdtsp' in key:
                    # 2. Delete vdtrp port
                    txn.add(self._ovn.delete_lrouter_port(vdtrp_name,
                                                          lrouter_name))
                    # 3. Delete vdtsp port
                    txn.add(self._ovn.delete_lswitch_port(vdtsp_name,
                                                          lswitch_name))
                    # 6. Delete transit logical switch
                    # TODO fix deleting the ovn switch
                    # txn.add(self._ovn.delete_lswitch(lswitch_name))

    def _join_transit_and_lrouter(self, router_id):
        ts_ports = self._get_transit_network_ports(router_id,
                                                   OVN_MAX_HA_AGENTS,
                                                   create=True)
        vdtsp = {'vdtsp': ts_ports['vdtsp']}
        self._join_lrouter_and_namespace(router_id, vdtsp)

    # find a free vtsp port for the agent and add it to transit network
    def _join_transit_and_agent(self, context, router_id):
        agents = self.service_plugin.get_ha_router_port_bindings(context,
                                                                 [router_id])
        ts_ports = self._get_transit_network_ports(router_id,
                                                   OVN_MAX_HA_AGENTS,
                                                   create=True)
        ts_ports.pop('vip')
        ts_ports.pop('vdtsp')
        ids = []
        for agent in agents:
            ids.append(agent.port_id)
        for key in ts_ports.keys():
            if ts_ports[key]['id'] in ids:
                ts_ports.pop(key)

        if len(ts_ports) > 0:
            key = sorted(ts_ports)[0]
            agent_port = {key: ts_ports[key]}
            self._join_lrouter_and_namespace(router_id, agent_port)
            return ts_ports[key]

    def _disjoin_transit_and_agent(self, context, router_id, port_id):
        port = self._admin_net._plugin.get_port(context, port_id)
        agent_port = {port['name']: port}
        self._disjoin_lrouter_and_namespace(router_id, agent_port)

    def _disjoin_transit_and_agents(self, context, router_id):
        ts_ports = self._get_transit_network_ports(router_id,
                                                   OVN_MAX_HA_AGENTS)
        ts_ports.pop('vip')
        ts_ports.pop('vdtsp')
        self._disjoin_lrouter_and_namespace(router_id, ts_ports)

    # get both namespace ip and vdtsp port ip
    def get_vpn_nexthop_ip(self, context, router_id):
        is_ha = self.service_plugin.vpn_router_is_ha(context, router_id)
        ts_ports = self._get_transit_network_ports(router_id,
                                                   OVN_MAX_HA_AGENTS,
                                                   create=False)
        if is_ha:
            namespace_ip = ts_ports['vtsp1']['ip']
        else:
            namespace_ip = ts_ports['vip']['ip']
        lrouter_ip = ts_ports['vdtsp']['ip']
        return namespace_ip, lrouter_ip

    def set_static_route(self, context, vpnservice):
        cidrs = self._get_peer_cidrs(vpnservice)
        router_id = vpnservice['router_id']

        nexthop, _ = self.get_vpn_nexthop_ip(context, router_id)

        router_name = ovn_name(router_id)
        with self._ovn.transaction(check_error=True) as txn:
            for cidr in cidrs:
                txn.add(self._ovn.add_static_route(router_name,
                                                   ip_prefix=cidr,
                                                   nexthop=nexthop))

    def del_static_route(self, context, cidrs, vpnservice):
        router_id = vpnservice['router_id']
        nexthop, _ = self.get_vpn_nexthop_ip(context, router_id)

        router_name = ovn_name(router_id)

        with self._ovn.transaction(check_error=True) as txn:
            for cidr in cidrs:
                txn.add(self._ovn.delete_static_route(router_name,
                                                      ip_prefix=cidr,
                                                      nexthop=nexthop))

    def _get_agent_number(self, context, router_id):
        admin_context = context if context.is_admin else context.elevated()
        self.service_plugin.schedule_routers(admin_context, [router_id])

        vpn_agents = self.service_plugin.get_vpn_agents_hosting_routers(
            admin_context, [router_id],
            admin_state_up=True,
            active=True)

        return len(vpn_agents)

    def _get_vpn_internal_port(self, context, router_id, host):
        # TODO (xianms) handle HA case, different hosts has different ports
        agent_number = OVN_MAX_HA_AGENTS
        ts_ports = self._get_transit_network_ports(router_id, agent_number)
        ts_ports['vtsp0']['fixed_ips'] = [ts_ports['vtsp0']['ip']]
        return ts_ports['vtsp0']

    def _get_vpn_external_port(self, host, router_id):
        filters = {'device_id': [router_id],
                   'device_owner': ['network:vpn_router_gateway']}

        plugin = manager.NeutronManager.get_plugin()
        context = nctx.get_admin_context()
        port_list = plugin.get_ports(context, filters=filters)
        if port_list:
            return port_list[0]
        return None

    def get_subnet_by_id(self, subnet_id):
        plugin = manager.NeutronManager.get_plugin()
        context = nctx.get_admin_context()

        filters = {'id': [subnet_id]}
        subnets = plugin.get_subnets(context, filters=filters)
        if subnets:
            return subnets[0]
        return None

    def _get_peer_cidrs(self, vpnservice):
        cidrs = []
        for ipsec_site_connection in vpnservice.ipsec_site_connections:
            for peer_cidr in ipsec_site_connection.peer_cidrs:
                cidrs.append(peer_cidr.cidr)
        return cidrs


class IPsecVpnOvnDriverCallBack(base_ipsec.IPsecVpnDriverCallBack):
    def __init__(self, driver):
        super(IPsecVpnOvnDriverCallBack, self).__init__(driver)
        self.admin_ctx = nctx.get_admin_context()
        self._OVNHelper = None

    @property
    def _IPsecHelper(self):
        if self._OVNHelper is None:
            self._OVNHelper = IPsecHelper()
        return self._OVNHelper

    def get_provider_network4vpn(self, context, router_id):
        vpn_plugin = manager.NeutronManager.get_service_plugins().get(
            service_constants.VPN)
        vpn_gw = vpn_plugin.get_vpn_gw_dict_by_router_id(context, router_id)
        network_id = vpn_gw['network_id']
        plugin = manager.NeutronManager.get_plugin()
        net = plugin.get_network(context, network_id)
        return net

    '''
    def get_transit_network4vpn(self, context, router_id=None):
        return self._IPsecHelper.get_transit_network(router_id)
    '''

    def get_subnet_info(self, context, subnet_id=None):
        return self._IPsecHelper.get_subnet_by_id(subnet_id)

    def get_vpn_transit_lip(self, context, router_id=None):
        _, lrouter_ip = self._IPsecHelper.get_vpn_nexthop_ip(context,
                                                             router_id)
        return lrouter_ip

    def find_vpn_port(self, context, ptype=None, router_id=None,
                      host=None):
        if ptype == 'internal':
            return self._IPsecHelper._get_vpn_internal_port(context,
                                                            router_id, host)
        elif ptype == 'external':
            return self._IPsecHelper._get_vpn_external_port(host, router_id)
        return None


class BaseOvnIPsecVPNDriver(base_ipsec.BaseIPsecVPNDriver):
    def __init__(self, service_plugin):
        self._OVNHelper = None
        super(BaseOvnIPsecVPNDriver, self).__init__(
            service_plugin,
            ipsec_validator.OVNVpnValidator(service_plugin))

    @property
    def _IPsecHelper(self):
        if self._OVNHelper is None:
            self._OVNHelper = IPsecHelper()
        return self._OVNHelper

    def _get_gateway_ips(self, router):
        """Obtain the IPv4 and/or IPv6 GW IP for the router.

        If there are multiples, (arbitrarily) use the first one.
        """
        gateway = self.service_plugin.get_vpn_gw_dict_by_router_id(
            nctx.get_admin_context(),
            router['id'])
        if gateway is None or gateway['external_fixed_ips'] is None:
            raise RouterIsNotVPNExternal(router_id=router['id'])

        v4_ip = v6_ip = None
        for fixed_ip in gateway['external_fixed_ips']:
            addr = fixed_ip['ip_address']
            vers = netaddr.IPAddress(addr).version
            if vers == 4:
                if v4_ip is None:
                    v4_ip = addr
            elif v6_ip is None:
                v6_ip = addr
        return v4_ip, v6_ip

    def _setup(self, context, vpnservice_id):
        vpnservice = self.service_plugin._get_vpnservice(
            context, vpnservice_id)
        router_id = vpnservice['router_id']
        self.service_plugin.create_vpn_ha_router(context, router_id)
        self._IPsecHelper._join_transit_and_lrouter(router_id)

    def _cleanup(self, context, vpnservice):
        router_id = vpnservice['router_id']
        self.service_plugin.delete_vpn_ha_router(context, router_id)
        agent_number = OVN_MAX_HA_AGENTS
        ts_ports = self._IPsecHelper._get_transit_network_ports(router_id,
                                                                agent_number)
        self._IPsecHelper._disjoin_lrouter_and_namespace(router_id, ts_ports)

    def _set_requirements(self, context, ipsec_site_connection):
        vpnservice = self.service_plugin._get_vpnservice(
            context, ipsec_site_connection['vpnservice_id'])
        self._IPsecHelper.set_static_route(context, vpnservice)

    def _del_requirements(self, context, ipsec_site_connection):
        vpnservice = self.service_plugin._get_vpnservice(
            context, ipsec_site_connection['vpnservice_id'])
        peer_cidrs = ipsec_site_connection['peer_cidrs']
        self._IPsecHelper.del_static_route(context, peer_cidrs, vpnservice)

    def create_vpnservice(self, context, vpnservice_dict):
        super(BaseOvnIPsecVPNDriver, self).create_vpnservice(context,
                                                             vpnservice_dict)
        self._setup(context, vpnservice_dict['id'])
        vpnservice = self.service_plugin._get_vpnservice(
            context, vpnservice_dict['id'])
        self.agent_rpc.prepare_namespace(context, vpnservice['router_id'])

    def delete_vpnservice(self, context, vpnservice):
        router_id = vpnservice['router_id']
        super(BaseOvnIPsecVPNDriver, self).delete_vpnservice(context,
                                                             vpnservice)
        services = self.service_plugin.get_vpnservices(context)
        router_ids = [s['router_id'] for s in services]
        if router_id not in router_ids:
            self.agent_rpc.cleanup_namespace(context, vpnservice['router_id'])
            self._cleanup(context, vpnservice)

    def create_ipsec_site_connection(self, context, ipsec_site_connection):
        self._set_requirements(context, ipsec_site_connection)
        super(BaseOvnIPsecVPNDriver, self).create_ipsec_site_connection(
            context, ipsec_site_connection)

    def delete_ipsec_site_connection(self, context, ipsec_site_connection):
        self._del_requirements(context, ipsec_site_connection)
        super(BaseOvnIPsecVPNDriver, self).delete_ipsec_site_connection(
            context, ipsec_site_connection)

    def update_ipsec_site_connection(
            self, context, old_ipsec_site_connection, ipsec_site_connection):
        self._del_requirements(context, old_ipsec_site_connection)
        self._set_requirements(context, ipsec_site_connection)
        super(BaseOvnIPsecVPNDriver, self).update_ipsec_site_connection(
            context, old_ipsec_site_connection, ipsec_site_connection)


class IPsecOvnVpnAgentApi(base_ipsec.IPsecVpnAgentApi):
    def _agent_notification(self, context, method, router_id,
                            version=None, **kwargs):
        """Notify update for the agent.

        This method will find where is the router, and
        dispatch notification for the agent.
        """
        admin_context = context if context.is_admin else context.elevated()
        if not version:
            version = self.target.version

        self.driver.service_plugin.schedule_routers(admin_context, [router_id])

        vpn_agents = self.driver.service_plugin.get_vpn_agents_hosting_routers(
            admin_context, [router_id],
            admin_state_up=True,
            active=True)

        for vpn_agent in vpn_agents:
            LOG.debug('Notify agent at %(topic)s.%(host)s the message '
                      '%(method)s %(args)s',
                      {'topic': self.topic,
                       'host': vpn_agent.host,
                       'method': method,
                       'args': kwargs})
            cctxt = self.client.prepare(server=vpn_agent.host, version=version)
            cctxt.cast(context, method, **kwargs)

    def prepare_namespace(self, context, router_id, **kwargs):
        kwargs['router'] = {'id': router_id}
        self._agent_notification(context, 'prepare_namespace', router_id,
                                 **kwargs)

    def cleanup_namespace(self, context, router_id, **kwargs):
        kwargs['router'] = {'id': router_id}
        self._agent_notification(context, 'cleanup_namespace', router_id,
                                 **kwargs)

    def vpn_router_deleted(self, context, router_id):
        self._agent_notification(context, 'vpn_router_deleted', router_id)

    def vpn_routers_updated(self, context, router_ids, **kwargs):
        if router_ids:
            self._agent_notification(context, 'vpn_routers_updated',
                                     router_ids, **kwargs)


class IPsecOvnVPNDriver(BaseOvnIPsecVPNDriver):
    """VPN Service Driver class for IPsec."""

    def __init__(self, service_plugin):
        super(IPsecOvnVPNDriver, self).__init__(service_plugin)

    def create_rpc_conn(self):
        self.endpoints = [IPsecVpnOvnDriverCallBack(self)]
        self.conn = n_rpc.create_connection()
        self.conn.create_consumer(
            topics.IPSEC_DRIVER_TOPIC, self.endpoints, fanout=False)
        self.conn.consume_in_threads()
        self.agent_rpc = IPsecOvnVpnAgentApi(
            topics.IPSEC_AGENT_TOPIC, BASE_IPSEC_VERSION, self)
