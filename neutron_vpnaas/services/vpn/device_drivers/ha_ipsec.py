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
import shutil

from oslo_log import log as logging
from neutron_vpnaas._i18n import _, _LI
from neutron.agent.linux import keepalived
from neutron.agent.linux import external_process

from neutron_vpnaas.services.vpn.device_drivers import ha

HA_DEFAULT_VR_ID = 20
IP_MONITOR_PROCESS_SERVICE = 'ip_monitor'

LOG = logging.getLogger(__name__)

KEEPALIVED_STATE_CHANGE_SERVER_BACKLOG = 4096

class HAOvnSwanProcess(object):

    def __init__(self, agent, conf, host):
        LOG.info(_LI("TODO: do the HA initialized here"))
        self.agent = agent
        self.conf = conf
        self.host = host
        self.process_monitor = external_process.ProcessMonitor(
            config=self.conf,
            resource_type='vpnservice')

    def initialize(self, vpnservice, namespace):
        self.namespace = namespace
        self.vpnservice_id = vpnservice['id']
        self._init_keepalived_manager(self.process_monitor)
        agent_rpc = self.agent.agent_rpc
        ha_agent = ha.VPNAgentMixin(agent_rpc, self.conf, self.host)
        state_change_callback=ha_agent.ha_enqueue_state_change
        self.update_initial_state(state_change_callback)
        self.spawn_state_change_monitor(self.process_monitor)
        self.enable_keepalived()

    def _init_keepalived_manager(self, process_monitor):
        self.keepalived_manager = keepalived.KeepalivedManager(
            self.vpnservice_id,
            keepalived.KeepalivedConf(),
            process_monitor,
            conf_path=self.conf.vpn_ha.ha_confs_path,
            namespace=self.namespace
        )

        config = self.keepalived_manager.config
        interface_name = 'eth0'
        subnet = '169.254.128.0/24'  # the HA subnet ip
        ha_port_cidrs = []
        ha_port_cidrs.append(subnet)
        vpn_ha_vr_id = 254
        instance = keepalived.KeepalivedInstance(
            'BACKUP',
            interface_name,
            vpn_ha_vr_id,
            ha_port_cidrs,
            nopreempt=True,
            advert_int=self.conf.vpn_ha.ha_vrrp_advert_int,
            priority=keepalived.HA_DEFAULT_PRIORITY
        )
        
        instance.track_interfaces.append(interface_name)
        
        if self.conf.vpn_ha.ha_vrrp_auth_password:
            instance.set_authentication(self.conf.vpn_ha.ha_vrrp_auth_type,
                                        self.conf.vpn_ha.ha_vrrp_auth_password)
        
        config.add_instance(instance)
        LOG.info(_LI('#####_init_keepalived_manager finished###'))

    def update_initial_state(self, callback):
        #ha_device = ip_lib.IPDevice(
        #    self.get_ha_device_name(),
        #    self.ha_namespace)
        #addresses = ha_device.addr.list()
        #cidrs = (address['cidr'] for address in addresses)
        #ha_cidr = self._get_primary_vip()
        #state = 'master' if ha_cidr in cidrs else 'backup'
        # TODO, since the HA port interface is not ready, set it hard code here
        state = 'master'
        self.ha_state = state
        callback(self.vpnservice_id, state)

    def enable_keepalived(self):
        self.keepalived_manager.spawn() 

    def disable_keepalived(self):
        self.keepalived_manager.disable()
        conf_dir = self.keepalived_manager.get_conf_dir()
        shutil.rmtree(conf_dir)      

    def _get_state_change_monitor_callback(self):
        ha_device = 'lo'
        ha_cidr = ['169.254.128.0/24']

        def callback(pid_file):
            cmd = [
                'neutron-vpn-keepalived-state-change',
                '--vpnservice_id=%s' % self.vpnservice_id,
                '--namespace=%s' % self.namespace,
                '--conf_dir=%s' % self.keepalived_manager.get_conf_dir(),
                '--monitor_interface=%s' % ha_device,
                '--monitor_cidr=%s' % ha_cidr,
                '--pid_file=%s' % pid_file,
                '--state_path=%s' % self.conf.state_path,
                '--user=%s' % os.geteuid(),
                '--group=%s' % os.getegid()
            ]

            return cmd

        return callback

    def _get_state_change_monitor_process_manager(self):
        return external_process.ProcessManager(
            self.conf,
            '%s.monitor' % self.vpnservice_id,
            self.namespace,
            default_cmd_callback=self._get_state_change_monitor_callback()
        )

    def spawn_state_change_monitor(self, process_monitor):
        pm = self._get_state_change_monitor_process_manager()
        pm.enable()
        process_monitor.register(
            self.vpnservice_id, IP_MONITOR_PROCESS_SERVICE, pm)

    def destroy_state_change_monitor(self, process_monitor, vpnservice):
        pm = self._get_state_change_monitor_process_manager()
        process_monitor.unregister(
            vpnservice['id'], IP_MONITOR_PROCESS_SERVICE)
        pm.disable()

    def delete(self, vpnservice):
        self.destroy_state_change_monitor(self.process_monitor, vpnservice)
        self.disable_keepalived()
