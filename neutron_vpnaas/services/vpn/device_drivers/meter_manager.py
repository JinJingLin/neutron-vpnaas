# Copyright (c) 2016 Jin Jing Lin, IBM.
# All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
import netaddr
import traceback

from neutron.agent.linux import iptables_manager
from neutron.common import rpc as n_rpc
from oslo_config import cfg
from oslo_log import log as logging
from oslo_service import loopingcall
from oslo_utils import timeutils

LOG = logging.getLogger(__name__)

meter_opts = [
    cfg.BoolOpt('vpn_meter_enable',
                default=False,
                help=_('Enable flag for VPN metering function')),
    cfg.IntOpt('vpn_measure_interval',
               default=30,
               help=_('The interval between two metering measures')),
    cfg.IntOpt('vpn_report_interval',
               default=300,
               help=_('The interval between two metering reports'))
]

cfg.CONF.register_opts(meter_opts, 'meter')

WRAP_NAME = 'neutron-vpnmeter'
TOP_CHAIN = WRAP_NAME + '-FORWARD'
RULE_OUT = '-o-'
RULE_IN = '-i-'
COUNT_CHAIN_OUT = '-co-'
COUNT_CHAIN_IN = '-ci-'

MAX_CHAIN_LEN_WRAP = 11
MAX_CHAIN_LEN_NOWRAP = 28


def get_chain_name(chain_name, wrap=True):
    if wrap:
        return chain_name[:MAX_CHAIN_LEN_WRAP]
    else:
        return chain_name[:MAX_CHAIN_LEN_NOWRAP]


class MeterManager(object):
    def __init__(self, conf, host, context):
        self.conf = conf
        self.host = host
        self.context = context
        self.namespaces = {}
        self.meter_ims = {}
        measure_interval = self.conf.meter.vpn_measure_interval
        self.metering_loop = loopingcall.FixedIntervalLoopingCall(
            self._metering_loop
        )
        self.last_report = 0
        self.metering_loop.start(interval=measure_interval)
        self.tenant_conn_ids = {}
        self.conn_tenant_id = {}
        self.metering_out_infos = {}
        self.metering_in_infos = {}
        self.metering_infos = [self.metering_out_infos, self.metering_in_infos]

    def _get_meter_iptabels_manager(self, namespace):
        meter_iptables_manager = iptables_manager.IptablesManager(
            state_less=True,
            namespace=namespace,
            binary_name='neutron-vpnmeter',
            use_ipv6=False)

        return meter_iptables_manager

    def _clean_tenant_conn_mapping(self, vpnservice):
        # delete the tenant-conn mapping entry when the process is destroy
        if not vpnservice:
            return
        tenant_id = vpnservice['project_id']
        conns_del = []
        for ipsec_site_connection in vpnservice['ipsec_site_connections']:
            conn_id = ipsec_site_connection['id']
            conns_del.append(conn_id)
            del self.conn_tenant_id[conn_id]
        del self.tenant_conn_ids[tenant_id]

        return conns_del

    def _sync_tenant_conn_mapping(self, vpnservice):
        # update the global tenant - conn mapping
        if not vpnservice:
            return
        tenant_id = vpnservice['project_id']
        # each time flush the tenant_conn_ids DB and fill in with new conn ids
        self.tenant_conn_ids[tenant_id] = []
        for ipsec_site_connection in vpnservice['ipsec_site_connections']:
            ipsec_conn_id = ipsec_site_connection['id']
            self.conn_tenant_id[ipsec_conn_id] = tenant_id
            if self.tenant_conn_ids.get(tenant_id):
                if ipsec_conn_id not in self.tenant_conn_ids.get(tenant_id):
                    self.tenant_conn_ids[tenant_id].append(ipsec_conn_id)
            else:
                self.tenant_conn_ids[tenant_id].append(ipsec_conn_id)

        for conn_id in self.conn_tenant_id.keys():
            if self.conn_tenant_id[conn_id] == tenant_id:
                if conn_id not in self.tenant_conn_ids[tenant_id]:
                    del self.conn_tenant_id[conn_id]

    def update_metering(self, process, namespace, remove_flag=False):
        vpnservice = process.vpnservice
        tenant_id = vpnservice['project_id']

        # Read the counters of all rules before do any update action
        self._add_metering_infos()

        if remove_flag:
            conn_ids = self._clean_tenant_conn_mapping(vpnservice)
            self._remove_metering_rule(namespace, conn_ids)
            del self.namespaces[tenant_id]
        else:
            self._add_metering_rule(vpnservice, namespace)
            self._sync_tenant_conn_mapping(vpnservice)
            self.namespaces[tenant_id] = namespace

    def _remove_chains(self, conn_ids, namespace):
        meter_im = self._get_meter_iptabels_manager(namespace)
        chains_del = []
        if not conn_ids:
            return
        for conn_del in conn_ids:
            chain_del_out = get_chain_name(WRAP_NAME + COUNT_CHAIN_OUT +
                                           conn_del, wrap=False)
            chain_del_in = get_chain_name(WRAP_NAME + COUNT_CHAIN_IN +
                                          conn_del, wrap=False)
            rule_del_out = get_chain_name(WRAP_NAME + RULE_OUT +
                                          conn_del, wrap=False)
            rule_del_in = get_chain_name(WRAP_NAME + RULE_IN +
                                         conn_del, wrap=False)
            chains_del.append(chain_del_out)
            chains_del.append(chain_del_in)
            chains_del.append(rule_del_out)
            chains_del.append(rule_del_in)

        for chain_del in chains_del:
            meter_im.ipv4['filter'].remove_chain(chain_del, wrap=False)

    def _add_metering_rule(self, vpnservice, namespace):
        meter_im = self._get_meter_iptabels_manager(namespace)
        self.meter_ims[namespace] = meter_im
        tenant_id = vpnservice['project_id']

        # Remove all chains &rules in namespace before write the new ones.
        conns_del = self.tenant_conn_ids.get(tenant_id)
        self._remove_chains(conns_del, namespace)

        # ADD the chains & rules for new connction
        for ipsec_site_conn in vpnservice['ipsec_site_connections']:
            connection_id = ipsec_site_conn['id']
            peer_cidrs = ipsec_site_conn['peer_cidrs']

            rule_name_out = get_chain_name(WRAP_NAME + RULE_OUT +
                                           connection_id, wrap=False)
            rule_name_in = get_chain_name(WRAP_NAME + RULE_IN +
                                          connection_id, wrap=False)
            chain_name_out = get_chain_name(WRAP_NAME + COUNT_CHAIN_OUT +
                                            connection_id, wrap=False)
            chain_name_in = get_chain_name(WRAP_NAME + COUNT_CHAIN_IN +
                                           connection_id, wrap=False)
            meter_im.ipv4['filter'].add_chain(rule_name_out, wrap=False)
            meter_im.ipv4['filter'].add_chain(rule_name_in, wrap=False)
            meter_im.ipv4['filter'].add_rule(TOP_CHAIN, '-j ' +
                                             rule_name_out, wrap=False)
            meter_im.ipv4['filter'].add_rule(TOP_CHAIN, '-j ' +
                                             rule_name_in, wrap=False)
            meter_im.ipv4['filter'].add_chain(chain_name_out, wrap=False)
            meter_im.ipv4['filter'].add_chain(chain_name_in, wrap=False)
            meter_im.ipv4['filter'].add_rule(chain_name_out, '', wrap=False)
            meter_im.ipv4['filter'].add_rule(chain_name_in, '', wrap=False)

            # For multi-subnet, all the rules are finally jump to count chain
            for peer_cidr in peer_cidrs:
                if netaddr.IPNetwork(peer_cidr).version == 6:
                    continue
                ipt_rule_in = '-s %s -j %s' % (peer_cidr, chain_name_in)
                ipt_rule_out = '-d %s -j %s' % (peer_cidr, chain_name_out)
                meter_im.ipv4['filter'].add_rule(rule_name_in, ipt_rule_in,
                                                 wrap=False, top=True)
                meter_im.ipv4['filter'].add_rule(rule_name_out, ipt_rule_out,
                                                 wrap=False, top=True)

        meter_im.apply()

    def _remove_metering_rule(self, namespace, conn_ids):
        del self.meter_ims[namespace]
        meter_im = self._get_meter_iptabels_manager(namespace)

        #Remove all rules & chains of the existing connection in the namespace
        self._remove_chains(conn_ids, namespace)

        meter_im.apply()

    def _metering_notification(self):
        for i in range(0, 2):
            for connection_id, info in self.metering_infos[i].items():
                data = {'connection_id': connection_id,
                        'tenant_id': self.conn_tenant_id.get(connection_id),
                        'pkts': info['pkts'],
                        'bytes': info['bytes'],
                        'time': info['time'],
                        'first_update': info['first_update'],
                        'last_update': info['last_update'],
                        'host': self.host}

                LOG.debug("Send VPN metering report: %s" % data)
                notifier = n_rpc.get_notifier('metering')
                if i == 0:
                    notifier.info(self.context, 'vpn.meter.tx', data)
                else:
                    notifier.info(self.context, 'vpn.meter.rx', data)

                info['pkts'] = 0
                info['bytes'] = 0
                info['time'] = 0

    def _purge_metering_info(self):
        vpn_report_interval = self.conf.meter.vpn_report_interval
        deadline_timestamp = timeutils.utcnow_ts() - vpn_report_interval
        for i in range(0, 2):
            conn_ids = []
            for connection_id, info in self.metering_infos[i].items():
                if info['last_update'] < deadline_timestamp:
                    conn_ids.append(connection_id)

            for connection_id in conn_ids:
                del self.metering_infos[i][connection_id]

    def _get_traffic_counters(self, namespaces):
        accs_out = {}
        accs_in = {}
        accs = [accs_out, accs_in]
        for tenant_id, ns in namespaces.items():
            if not self.meter_ims.get(ns):
                meter_im = self._get_meter_iptabels_manager(ns)
            else:
                meter_im = self.meter_ims[ns]
            conn_ids = self.tenant_conn_ids[tenant_id]
            for conn_id in conn_ids:
                chain_out = get_chain_name(WRAP_NAME + COUNT_CHAIN_OUT +
                                           conn_id, wrap=False)
                chain_in = get_chain_name(WRAP_NAME + COUNT_CHAIN_IN +
                                          conn_id, wrap=False)
                chain = [chain_out, chain_in]
                chain_accs = [{}, {}]
                for i in range(0, 2):
                    try:
                        chain_accs[i] = meter_im.get_traffic_counters(
                            chain[i], wrap=False, zero=True)
                    except Exception:
                        LOG.info(traceback.format_exc())
                        continue
                    if not chain_accs[i]:
                        continue

                    acc = accs[i].get(conn_id, {'pkts': 0, 'bytes': 0})
                    acc['pkts'] += chain_accs[i]['pkts']
                    acc['bytes'] += chain_accs[i]['bytes']

                    accs[i][conn_id] = acc
        return accs

    def _add_metering_info(self, conn_id, index, pkts, bytes):
        ts = timeutils.utcnow_ts()
        info = self.metering_infos[index].get(conn_id, {'bytes': 0,
                                                        'pkts': 0,
                                                        'time': 0,
                                                        'first_update': ts,
                                                        'last_update': ts})

        info['bytes'] += bytes
        info['pkts'] += pkts
        info['time'] += ts - info['last_update']
        info['last_update'] = ts

        self.metering_infos[index][conn_id] = info
        return info

    def _add_metering_infos(self):
        accs = self._get_traffic_counters(self.namespaces)
        if not accs:
            return

        for i in range(0, 2):
            for conn_id, acc in accs[i].items():
                self._add_metering_info(conn_id, i, acc['pkts'], acc['bytes'])

    def _metering_loop(self):
        self._add_metering_infos()

        ts = timeutils.utcnow_ts()
        delta = ts - self.last_report

        report_interval = self.conf.meter.vpn_report_interval
        if delta >= report_interval:
            self._metering_notification()
            self._purge_metering_info()
            self.last_report = ts
