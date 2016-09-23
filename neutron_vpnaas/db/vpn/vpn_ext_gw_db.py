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

from neutron_lib import constants as l3_constants

from oslo_log import log as logging
from oslo_utils import uuidutils

from neutron_lib import exceptions as n_exc

from neutron import manager

from neutron.callbacks import events
from neutron.callbacks import registry
from neutron.callbacks import resources

from neutron.db import common_db_mixin as base_db
from neutron.db import l3_db
from neutron.db import model_base
from neutron.db import models_v2

from neutron.plugins.common import constants
from neutron.plugins.common import utils as p_utils

from neutron_vpnaas.extensions import vpn_ext_gw

from sqlalchemy import orm
from sqlalchemy.orm import exc

from neutron_vpnaas._i18n import _

import sqlalchemy as sa


LOG = logging.getLogger(__name__)

DEVICE_OWNER_VPN_ROUTER_GW = l3_constants.DEVICE_OWNER_NETWORK_PREFIX + \
                             "vpn_router_gateway"


class VPNExtGW(model_base.BASEV2, models_v2.HasId, model_base.HasProject):
    __tablename__ = 'vpn_ext_gws'
    router_id = sa.Column(sa.String(36), sa.ForeignKey('routers.id'),
                          nullable=False)
    port_id = sa.Column(sa.String(36), sa.ForeignKey('ports.id'),
                        nullable=False)
    port = orm.relationship(models_v2.Port, lazy='joined')
    router = orm.relationship(l3_db.Router, lazy='joined')


class VPNExtGWPlugin_db(vpn_ext_gw.VPNExtGWPluginBase, base_db.CommonDbMixin):
    """DB class to support vpn external ports configuration."""

    def __new__(cls):
        VPNExtGWPlugin_db._subscribe_callbacks()
        return super(VPNExtGWPlugin_db, cls).__new__(cls)

    @staticmethod
    def _subscribe_callbacks():
        registry.subscribe(_prevent_vpn_port_delete_callback, resources.PORT,
                           events.BEFORE_DELETE)

    @property
    def _core_plugin(self):
        return manager.NeutronManager.get_plugin()

    @property
    def _vpn_plugin(self):

        return manager.NeutronManager.get_service_plugins().get(constants.VPN)

    def prevent_vpn_gw_port_deletion(self, context, port_id):
        """Checks to make sure a port is allowed to be deleted.

        Raises an exception if this is not the case.  This should be called by
        any plugin when the API requests the deletion of a port, since some
        ports for L3 are not intended to be deleted directly via a DELETE
        to /ports, but rather via other API calls that perform the proper
        deletion checks.
        """
        try:
            port = self._core_plugin.get_port(context, port_id)
        except n_exc.PortNotFound:
            # non-existent ports don't need to be protected from deletion
            return
        if port['device_owner'] not in [DEVICE_OWNER_VPN_ROUTER_GW]:
            return
        # Raise port in use only if the port has IP addresses
        # Otherwise it's a stale port that can be removed
        fixed_ips = port['fixed_ips']
        if not fixed_ips:
            LOG.debug("Port %(port_id)s has owner %(port_owner)s, but "
                      "no IP address, so it can be deleted",
                      {'port_id': port['id'],
                       'port_owner': port['device_owner']})
            return

        reason = _('has device owner %s') % port['device_owner']
        raise n_exc.ServicePortInUse(port_id=port['id'], reason=reason)

    def _make_vpn_ext_gw_dict(self, gateway_db, fields=None):
        if gateway_db and gateway_db.port:
            nw_id = gateway_db.port['network_id']
            res = {'id': gateway_db['id'],
                   'tenant_id': gateway_db['tenant_id'], 'network_id': nw_id,
                   'router_id': gateway_db['router_id'],
                   'router_name': gateway_db.router['name'],
                   'external_fixed_ips': [{'subnet_id': ip["subnet_id"],
                                           'ip_address': ip["ip_address"]} for
                                          ip in gateway_db.port['fixed_ips']]}
            return self._fields(res, fields)

        return {}

    def _check_for_external_ip_change(self, context, gw_port, ext_ips):
        # determine if new external IPs differ from the existing fixed_ips
        if not ext_ips:
            # no external_fixed_ips were included
            return False
        if not gw_port:
            return True

        subnet_ids = set(ip['subnet_id'] for ip in gw_port['fixed_ips'])
        new_subnet_ids = set(
            f['subnet_id'] for f in ext_ips if f.get('subnet_id'))
        subnet_change = not new_subnet_ids == subnet_ids
        if subnet_change:
            return True
        ip_addresses = set(ip['ip_address'] for ip in gw_port['fixed_ips'])
        new_ip_addresses = set(
            f['ip_address'] for f in ext_ips if f.get('ip_address'))
        ip_address_change = not ip_addresses == new_ip_addresses
        return ip_address_change

    def _validate_gw_info(self, context, network_id, gw_port, ext_ips):
        if network_id:
            network_db = self._core_plugin._get_network(context, network_id)
            if not network_db.external:
                msg = _("Network %s is not an external network") % network_id
                raise n_exc.BadRequest(resource='router', msg=msg)
            if ext_ips:
                subnets = self._core_plugin.get_subnets_by_network(context,
                                                                   network_id)
                for s in subnets:
                    if not s['gateway_ip']:
                        continue
                    for ext_ip in ext_ips:
                        if ext_ip.get('ip_address') == s['gateway_ip']:
                            msg = _("External IP %s is the same as the "
                                    "gateway IP") % ext_ip.get('ip_address')
                            raise n_exc.BadRequest(resource='router', msg=msg)
        return network_id

    def _update_current_vpn_gw_port(self, context, gateway_db, ext_ips):
        self._core_plugin.update_port(context, gateway_db.port['id'],
                                      {'port': {'fixed_ips': ext_ips}})
        gw_port = self._core_plugin._get_port(context.elevated(),
                                              gateway_db.port['id'])
        return gw_port

    def _delete_current_vpn_gw_port(self, context, gateway_db, new_network_id):
        """Delete gw port if attached to an old network."""
        port_requires_deletion = (
            gateway_db and gateway_db.port and gateway_db.port[
                'network_id'] != new_network_id)
        if not port_requires_deletion:
            return
        admin_ctx = context.elevated()
        self._core_plugin.delete_port(admin_ctx, gateway_db.port['id'],
                                      l3_port_check=False)

    def _create_vpn_router_gw_port(self, context, router_id, net_id, ext_ips):
        # Port has no 'tenant-id', as it is hidden from user
        port_data = {'tenant_id': '',  # intentionally not set
                     'network_id': net_id,
                     'fixed_ips': ext_ips or l3_constants.ATTR_NOT_SPECIFIED,
                     'device_id': router_id,
                     'device_owner': DEVICE_OWNER_VPN_ROUTER_GW,
                     'admin_state_up': True, 'name': ''}
        gw_port = p_utils.create_port(self._core_plugin, context.elevated(),
                                      {'port': port_data})

        if not gw_port['fixed_ips']:
            LOG.debug('No IPs available for external network %s', net_id)
        gw_port = self._core_plugin._get_port(context.elevated(),
                                              gw_port['id'])
        return gw_port

    def _update_vpn_gw_info(self, context, gateway_db, info):
        vpn_gw_port = None
        if gateway_db:
            vpn_gw_port = gateway_db.port
            router_id = gateway_db['router_id']
            net_id = vpn_gw_port['network_id']
        else:
            router_id = info['router_id']
            net_id = info['network_id']
        ext_ips = info.get('external_fixed_ips') if info else []
        ext_ip_change = self._check_for_external_ip_change(context,
                                                           vpn_gw_port,
                                                           ext_ips)
        net_id = self._validate_gw_info(context, net_id, vpn_gw_port, ext_ips)
        if vpn_gw_port and ext_ip_change and vpn_gw_port[
            'network_id'] == net_id:
            return self._update_current_vpn_gw_port(context, gateway_db,
                                                    ext_ips)
        else:
            self._delete_current_vpn_gw_port(context, gateway_db, net_id)
            return self._create_vpn_router_gw_port(context, router_id, net_id,
                                                   ext_ips)

    def get_vpn_gw_by_router_id(self, context, router_id):
        try:
            gateway_db = self._model_query(context, VPNExtGW).filter(
                VPNExtGW.router_id == router_id).one()
        except exc.NoResultFound:
            raise vpn_ext_gw.VPNGWNotFound(router_id=router_id)
        return gateway_db

    def get_vpn_gw_dict_by_router_id(self, context, router_id):
        gateway_db = self.get_vpn_gw_by_router_id(context, router_id)
        return self._make_vpn_ext_gw_dict(gateway_db)

    def gateway_is_used(self, context, gateway_db):
        filters = {'router_id': [gateway_db['router_id']]}
        vpnservices = self._vpn_plugin.get_vpnservices(context,
                                                       filters=filters)
        if vpnservices:
            services = ",".join([v['id'] for v in vpnservices])
            raise vpn_ext_gw.VPNGWInUsed(gateway_id=gateway_db['id'],
                                         services=services)

    def create_gateway(self, context, gateway):
        info = gateway['gateway']
        router_id = info['router_id']
        gateway_db = None
        try:
            gateway_db = self.get_vpn_gw_by_router_id(context, router_id)
        except vpn_ext_gw.VPNGWNotFound:
            LOG.debug("Could not find vpn gateway.")

        if gateway_db:
            raise vpn_ext_gw.RouterHasVPNExternal(router_id=router_id)

        # validator = self._get_validator()
        gw_port = self._update_vpn_gw_info(context, None, info)
        with context.session.begin(subtransactions=True):
            # TODO(xianms) validator.(context, gateway)
            gateway_db = VPNExtGW(id=uuidutils.generate_uuid(),
                                  tenant_id=info['tenant_id'],
                                  port_id=gw_port['id'],
                                  router_id=info['router_id'])
            context.session.add(gateway_db)

        return self._make_vpn_ext_gw_dict(gateway_db)

    def update_gateway(self, context, gateway_id, gateway):
        gateway_changes = gateway['gateway']
        # validator = self._get_validator()

        gateway_db = self._get_resource(context, VPNExtGW, gateway_id)
        self.gateway_is_used(context, gateway_db)
        gw_port = self._update_vpn_gw_info(context, gateway_db,
                                           gateway_changes)
        with context.session.begin(subtransactions=True):
            gateway_db.update({"port_id": gw_port["id"]})
        return self._make_vpn_ext_gw_dict(gateway_db)

    def delete_gateway(self, context, gateway_id):
        gateway_db = self._get_resource(context, VPNExtGW, gateway_id)
        self.gateway_is_used(context, gateway_db)
        with context.session.begin(subtransactions=True):
            # TODO(xianms) self.check_gateway_not_in_use(context, gateway_id)
            context.session.delete(gateway_db)

        self._delete_current_vpn_gw_port(context, gateway_db, None)

    def get_gateway(self, context, gateway_id, fields=None):
        gateway_db = self._get_resource(context, VPNExtGW, gateway_id)
        return self._make_vpn_ext_gw_dict(gateway_db, fields)

    def get_gateways(self, context, filters=None, fields=None):
        return self._get_collection(context, VPNExtGW,
                                    self._make_vpn_ext_gw_dict,
                                    filters=filters, fields=fields)


def _prevent_vpn_port_delete_callback(resource, event, trigger, **kwargs):
    context = kwargs['context']
    port_id = kwargs['port_id']
    port_check = kwargs['port_check']
    vpn_plugin = manager.NeutronManager.get_service_plugins().get(
        constants.VPN)
    if vpn_plugin and port_check:
        vpn_plugin.prevent_vpn_gw_port_deletion(context, port_id)
