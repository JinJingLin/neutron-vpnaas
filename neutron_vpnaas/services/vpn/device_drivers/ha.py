# Copyright 2016 IBM Corporation, All Rights Reserved.
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

import os

import eventlet
from oslo_config import cfg
from oslo_log import log as logging
import webob

from neutron._i18n import _LI
from neutron.agent.linux import utils as agent_utils
from neutron.common import utils as common_utils
from neutron.notifiers import batch_notifier
from neutron.agent.linux import keepalived

LOG = logging.getLogger(__name__)

KEEPALIVED_STATE_CHANGE_SERVER_BACKLOG = 4096

vpn_ha_opts = [
    cfg.StrOpt('ha_confs_path',
               default='$state_path/ha_confs',
               help=_('Location to store keepalived/conntrackd '
                      'config files')),
    cfg.StrOpt('ha_vrrp_auth_type',
               default='PASS',
               choices=keepalived.VALID_AUTH_TYPES,
               help=_('VRRP authentication type')),
    cfg.StrOpt('ha_vrrp_auth_password',
               help=_('VRRP authentication password'),
               secret=True),
    cfg.IntOpt('ha_vrrp_advert_int',
               default=2,
               help=_('The advertisement interval in seconds')),
]

cfg.CONF.register_opts(vpn_ha_opts, 'vpn_ha')

class VPNKeepalivedStateChangeHandler(object):
    def __init__(self, agent):
        self.agent = agent

    @webob.dec.wsgify(RequestClass=webob.Request)
    def __call__(self, req):
        vpnservice_id = req.headers['X-Neutron-VPNaaS-Id']
        state = req.headers['X-Neutron-VPNaaS-State']
        self.enqueue(vpnservice_id, state)

    def enqueue(self, vpnservice_id, state):
        LOG.debug('Handling notification for vpn service '
                  '%(vpnservice_id)s, state %(state)s', {'vpnservice_id': vpnservice_id,
                                                     'state': state})
        self.agent.ha_enqueue_state_change(vpnservice_id, state)


class VPNAgentKeepalivedStateChangeServer(object):
    def __init__(self, agent, conf):
        self.agent = agent
        self.conf = conf

        agent_utils.ensure_directory_exists_without_file(
            self.get_vpn_keepalived_state_change_socket_path(self.conf))

    @classmethod
    def get_vpn_keepalived_state_change_socket_path(cls, conf):
        return os.path.join(conf.state_path, 'vpn-keepalived-state-change')

    def run(self):
        server = agent_utils.UnixDomainWSGIServer(
            'neutron-vpn-keepalived-state-change')
        server.start(VPNKeepalivedStateChangeHandler(self.agent),
                     self.get_vpn_keepalived_state_change_socket_path(self.conf),
                     workers=0,
                     backlog=KEEPALIVED_STATE_CHANGE_SERVER_BACKLOG)
        server.wait()

class VPNAgentMixin(object):
    def __init__(self, agent_rpc, conf, host):
        self.conf = conf
        self.host = host
        self._init_vpn_ha_conf_path()
        self.agent_rpc = agent_rpc
        self.vpn_state_change_notifier = batch_notifier.BatchNotifier(
            self._calculate_batch_duration(), self.notify_vpn_server)
        eventlet.spawn(self._start_vpn_keepalived_notifications_server)

    def _start_vpn_keepalived_notifications_server(self):
        state_change_server = (
            VPNAgentKeepalivedStateChangeServer(self, self.conf))
        state_change_server.run()
        # here this API start the server side which will received the keepalived status change.

    def _calculate_batch_duration(self):
        # Slave becomes the master after not hearing from it 3 times
        detection_time = self.conf.vpn_ha.ha_vrrp_advert_int * 3

        # Keepalived takes a couple of seconds to configure the VIPs
        configuration_time = 2

        # Give it enough slack to batch all events due to the same failure
        return (detection_time + configuration_time) * 2

    def ha_enqueue_state_change(self, vpnservice_id, state):
        LOG.info(_LI('VPN service %(vpnservice_id)s transitioned to %(state)s'),
                 {'vpnservice_id': vpnservice_id,
                  'state': state})

        # Just notify the ha state change here
        self.vpn_state_change_notifier.queue_event((vpnservice_id, state))


    def notify_vpn_server(self, batched_events):
        translation_map = {'master': 'active',
                           'backup': 'standby',
                           'fault': 'standby'}
        translated_states = dict((vpnservice_id, translation_map[state]) for
                                 vpnservice_id, state in batched_events)
        LOG.debug('Updating server with HA VPN services states %s',
                  translated_states)
        # don't need the context here as params.
        self.agent_rpc.update_ha_vpn_states(self.host, translated_states)

    def _init_vpn_ha_conf_path(self):
        ha_full_path = os.path.dirname("/%s/" % self.conf.vpn_ha.ha_confs_path)
        common_utils.ensure_dir(ha_full_path)
