#    (c) Copyright 2016 IBM Corporation, All Rights Reserved.
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

import abc

import six

from neutron_lib.api import converters
from neutron_lib import exceptions as nexception

from neutron.api import extensions
from neutron.api.v2 import attributes as attr
from neutron.api.v2 import resource_helper
from neutron.plugins.common import constants as nconstants

from neutron_vpnaas._i18n import _


class RouterIsNotVPNExternal(nexception.BadRequest):
    message = _("Router %(router_id)s has no VPN external network gateway set")


class RouterHasVPNExternal(nexception.BadRequest):
    message = _(
        "Router %(router_id)s already has VPN external network gateway")


class VPNGWInUsed(nexception.BadRequest):
    message = _(
        "Gateway %(gateway_id)s is used by VPN services %(services)s")


class VPNGWNotFound(nexception.NotFound):
    message = _("VPN gateway for router %(router_id)s could not be found")


VPN_GW = 'gateway_info'
RESOURCE_ATTRIBUTE_MAP = {
    "gateways": {
        'id': {'allow_post': False, 'allow_put': False,
               'validate': {'type:uuid': None},
               'is_visible': True,
               'primary_key': True},
        'tenant_id': {'allow_post': True, 'allow_put': False,
                      'validate': {'type:string': attr.TENANT_ID_MAX_LEN},
                      'required_by_policy': True,
                      'is_visible': True},
        'router_id': {'allow_post': True, 'allow_put': False,
                      'validate': {'type:uuid': None},
                      'is_visible': True},
        'router_name': {'allow_post': False, 'allow_put': False,
                        'validate': {'type:string': attr.NAME_MAX_LEN},
                        'is_visible': True},
        'network_id': {'allow_post': True, 'allow_put': True,
                       'validate': {'type:uuid': None},
                       'is_visible': True},
        'external_fixed_ips': {'allow_post': True, 'allow_put': True,
                               'is_visible': True, 'default': None,
                               'enforce_policy': True,
                               'convert_list_to':
                                   converters.convert_kvp_list_to_dict,

                               'subnet_id': {'type:uuid': None,
                                             'required': True},
                               'fixed_ips': None
                               }
    }
}


@six.add_metaclass(abc.ABCMeta)
class Vpn_ext_gw(extensions.ExtensionDescriptor):
    @classmethod
    def get_name(cls):
        return "VPN External Gateway"

    @classmethod
    def get_alias(cls):
        return "vpn-ext-gw"

    @classmethod
    def get_description(cls):
        return "VPN external ports support"

    @classmethod
    def get_updated(cls):
        return "2016-07-08T10:00:00-00:00"

    @classmethod
    def get_resources(cls):
        plural_mappings = resource_helper.build_plural_mappings(
            {}, RESOURCE_ATTRIBUTE_MAP)
        attr.PLURALS.update(plural_mappings)
        return resource_helper.build_resource_info(plural_mappings,
                                                   RESOURCE_ATTRIBUTE_MAP,
                                                   nconstants.VPN,
                                                   register_quota=True,
                                                   translate_name=True)

    def get_required_extensions(self):
        return ["vpnaas"]

    def update_attributes_map(self, attributes):
        super(Vpn_ext_gw, self).update_attributes_map(
            attributes, extension_attrs_map=RESOURCE_ATTRIBUTE_MAP)

    def get_extended_resources(self, version):
        if version == "2.0":
            return RESOURCE_ATTRIBUTE_MAP
        else:
            return {}


@six.add_metaclass(abc.ABCMeta)
class VPNExtGWPluginBase(object):
    @abc.abstractmethod
    def create_gateway(self, context, gateway):
        pass

    @abc.abstractmethod
    def update_gateway(self, context, gateway_id, gateway):
        pass

    @abc.abstractmethod
    def delete_gateway(self, context, gateway_id):
        pass

    @abc.abstractmethod
    def get_gateway(self, context, gateway_id, fields=None):
        pass

    @abc.abstractmethod
    def get_gateways(self, context, filters=None, fields=None):
        pass
