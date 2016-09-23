..
 This work is licensed under a Creative Commons Attribution 3.0 Unported
 License.

 http://creativecommons.org/licenses/by/3.0/legalcode

=========================
VPNaaS for OVN Networking
=========================

Launchpad blueprint:

https://blueprints.launchpad.net/neutron/+spec/vpn-ovn

This blueprint covers the support for VPNaaS with OVN networking by adding a
new centralized VPNaaS agent on a group of HA nodes. The new VPN agent will
create a namespace on the node and a transit network. It also adds two ports
in the transit network, one port connects the transit with the OVN distributed
logical router, another port connects the namespace with transit network. The
IPsec VPN traffic flow reach to namepsace via the transit network and send
back. The agent also run the Swan process in the namespace to encrypt or
decrypt the IPsec traffic flow.

Problem Description
===================

Currently VPNaaS service plugin only has support for the reference Neutron
software routers, such as neutron L3 router. It can't work together with OVN
distributed router.

Proposed Change
===============

::

 +-------------+-------------------------------+----------+ provider
               |                               |               or
               |                               |             public
               |                               |             network
       +-------+---------+             +-------+--------+
       |                 |    Transit  |                |
       |                 |    Network  |   VPN Agent    |
       |   OVN           |-------------|   Namespace    |
       |   Distributed   |             |                |
       |   Router        |             |                |
       +-----------------+             +----------------+
               |
               |
               |
               |
               |
               |
               |
               |
               |
               |        private network
 +-------+------------------------+-------------------------+------+
         |                        |                         |
         |                        |                         |
    +----+---+                +---+---+                  +--+---+
    | VM1    |                | VM2   |                  | VM3  |
    |        |                |       |                  |      |
    +--------+                +-------+                  +------+
                            Figure 1

Add a new VPN agent to support VPN+OVN. Together with the new agent, changes
for VPNaaS plugin service driver are also needed. This will have no impact on
existing VPN solution. The existing VPN agent can still work with neutron l3
router.

Changes on neutron server
-------------------------

Block diagram:

::

        +------------------------------------------------------------------+
        |                                                                  |
        |                        Neutron Server                            |
        |                                                                  |
        +--------------+----------------------------------+----------------+
        |              |                                  |                |
        |              |           VPN plugin             |                |
        |              |                                  |                |
        +-----------+  |  +-----------------------------+ |  +-------------+
        ||          |  |  |                             | |  |   VPN      ||
        ||  OVN API <-----+    VPN service driver       +----> scheduler  ||
        ||          |  |  |                             | |  |            ||
        +-----------+  |  +----+--------------------+---+ |  +-------------+
        |              |       |                    |     |                |
        +--------------+----------------------------------+----------------+
                               |                    |
                               +                    +
                            RPC message          RPC message
                               +                    +
                               |                    |
                               |                    |
                       +-------v------+       +-----v-------+
                       |              |       |             |
                       |   VPN agent  +-+HA+--+   VPN agent |
                       |              |       |             |
                       +--------------+       +-------------+
                                    Figure 2


Above block diagram is same as current VPNaaS framework. The neutron server
side includes below components:
1. VPN plugin
2. VPN service driver
3. VPN agent scheduler

VPN plugin
++++++++++

Both VPN plugin and plugin service driver are configurable in neutron.conf and
neutron_vpnaas.conf. The main function of VPN plugin is to store the VPN's
configuration in VPN database, then it invokes the VPN service driver, this
part of code is common for all VPNaaS solutions.

VPN service driver and Agent scheduler
++++++++++++++++++++++++++++++++++++++

The VPN service driver has different implementation for different VPNaaS
solutions. For VPN+OVN, the main function of VPN service driver is to picks up
a list of “candidate” VPN agents and send the RPC message to the list of
specified VPN agents. The OVN L3 plugin has already includes a scheduler to
pick up the list of “candidate” chassis to host L3 gateway for the router. The
VPN service driver would use same candidates to host VPN agents and sends RPC
message to all chassis which host the ONV L3 gateway.
But this solution would limits the flexibility, it requires the VPN agent must
run with OVN L3 gateway together.

Another solution for this issue is to add a new scheduler for VPNaaS. The new
scheduler will check if an agent has been assigned for the VPN service when
the VPN plugin driver sends a RPC message. It will select agents for the VPN
service if there is no agent for it. The selection algorithm can be same as
neutron L3 router scheduler, so some part of the code of neutron l3 router
scheduler can be re-used. As with the router scheduler, a scheduler plugin is
provided to support different scheduling algorithms.

Transit network
+++++++++++++++

The transit network is used to connect the OVN logical router and namespace.
And the subnet of this network should be configurable and default is
169.254.64.0/18. The transit network is per router and created when the first
VPN service of the router is created, and two kinds of ports in the transit
network are created also to connect the namespace and OVN logical router.
1. Router port. It is a distributed router port and used to connect the
transit network and the OVN router.
2. Namespace port. It is in namespace and used to connect the transit network
and the namespace.

The VPN service driver will check if the transit network and ports are
created. And it will invoke networking_ovn.ovsdb.impl_idl_ovn APIs to create
the transit network and ports if they are not existing.

Static Routes management
++++++++++++++++++++++++

There are static route entries to make sure the traffic flow from tenant
private network can reach the VPN namespace.

The static route entries are:
1. In namespace:
prefix: tenant private network subnet, nexthop: the IP of the transit port
on OVN logical router
This route entry to make sure the traffic from VPN peer can reach to the OVN
router. It will be added by VPN agent in the namespace.

2. In the OVN logical router:
prefix: VPN connection peer subnet, nexthop: the IP of the transit port on
the namespace
This route entry to make sure the traffic from the local private can reach to
the namespace. VPN service driver will invoke
networking_ovn.ovsdb.impl_idl_ovn APIs to add it when a new IPsec connection
is created or updated.

For other uses of VPN service driver, it will be same as existing plugin
service driver. The new VPNaaS plugin service driver only sends out below RPC
messages,
1. vpnservice_updated (existed) - create and delete messages are included in
updated message.


Changes on VPN agent
--------------------

Block diagram of vpn agent:

::

                               +--------------+
                      +--------+Public Network+------+
                      |        +--------------+      |
                      |                              |
                      |                              |
                      |                              |
                +-----+-----------+        +---------+-------+
                | Agent1 namespace|        | Agent2 namespace|
                | +-------------+ |        | +-------------+ |
                | |IPsec Process| +--VRRP--+ |IPsec Process| |
                | +-------------+ |        | +-------------+ |
                +-----------------+        +-----------------+
                      |                              |
                      |                              |
                      |                              |
                      |        +----------------+    |
                      +--------+Transit Network +----+
                               +-------+--------+
                                       |
                                       |
                                       |
                               +-------+--------+
                       +-------+   OVN Router   +----+
                       |       +----------------+    |
                       |                             |
                       |                             |
                 +-----+-----------+        +--------+--------+
                 | Private Network1|        |Private Network2 |
                 +-----+-----------+        +--------+--------+
                       |                             |
                       |                             |
                       |                             |
                 +-----+-----------+        +--------+--------+
                 |       VM1       |        |       VM2       |
                 +-----------------+        +-----------------+
                                    Figure 3

Namespace management
++++++++++++++++++++

There will be one namespace per VPN agent router. The new VPN agent needs to
check if the namespace exists when it receives
the vpnservice_updated RPC message and creates a new one if it does not exist.
And removes the namespace when all VPN services are deleted.

Transit network and port
++++++++++++++++++++++++

As mentioned in previous, there is a port in the namespace to connect transit
network, it is create by the VPN service driver. When the agent create the new
namespace, the agent will invoke the OVS interface driver to plug this port
also.

Static Routes management
++++++++++++++++++++++++

As mentioned in previous, there are some routes in the namespace to redirect
the traffic from peer to the OVN router. The agent will add/update these
routes when a new IPsec connection is created or updated.

VPN External IP address management
++++++++++++++++++++++++++++++++++

Within the neutron L3 router, VPNaaS uses the router gateway public IP address
as local public IP address. But for VPN+OVN, the router gateway public IP
address can't be used any more since it is for OVN L3 gateway. A new public IP
address is needed for the VPNaaS namespace. A new RESTful API is needed to
configure the VPN gateway public IP address also.
Below API is defined to configure the VPN gateway public IP address:
URL: /v2.0/vpn/gateways
Request Body Example:

.. code-block:: javascript

    {"gateway":
        {
            "router_id": "999c39b2-178f-4340-a69c-a1068dbae016",
            "network_id": "afab184a-43a3-4d77-bb27-e779874c123a"
        }
    }

Response Example:

.. code-block:: javascript

    {
        "gateway": {
            "router_id": "999c39b2-178f-4340-a69c-a1068dbae016",
            "network_id": "afab184a-43a3-4d77-bb27-e779874c123a",
            "tenant_id": "1eaaa81700b348029c9cbf9f3835bc58",
            "router_name": "router1",
            "id": "841aa615-a11d-4457-85f4-8429ace79cdc",
            "external_fixed_ips": [
                {
                    "subnet_id": "17a597fb-a308-4b6c-b328-d4e5f4c1547f",
                    "ip_address": "172.24.4.8"
                },
                {
                    "subnet_id": "36da89c5-72a4-4054-87c3-b4dbf5cb9384",
                    "ip_address": "2001:db8::1"
                }
            ]
        }
    }

When user uses above API to VPN public IP address. A neutron port will be
created also in the external network. And the agent will plug the external
port also when the namespace is created and unplug it when the namespace is
deleted.

Responsibility of DB operations
-------------------------------

The VPN service plugin will remain responsible for the CRUD operations on the
VPN DB objects. This will ensure consistency across common fields between
different VPN plugin.

Data Model Impact
+++++++++++++++++

An new VPN external gateway table will be added in VPN database to store the
VPN public IP address.
This table will be defined as below:

.. code-block:: python

    class VPNExtGW(model_base.BASEV2, models_v2.HasId, model_base.HasProject):
        __tablename__ = 'vpn_ext_gws'
        router_id = sa.Column(sa.String(36), sa.ForeignKey('routers.id'),
                              nullable=False)
        port_id = sa.Column(
            sa.String(36),
            sa.ForeignKey('ports.id', ondelete="CASCADE"),
            primary_key=True)
        port = orm.relationship(models_v2.Port)
        router = orm.relationship(l3_db.Router)

No other modifications to the existing tables are required.

REST API and CLI Impact
+++++++++++++++++++++++

All existing VPN APIs will be kept same as current implementation. But as
mentioned as above, a new API will be added to configure VPN gateway public IP
address. And the according CLI will be added also.

New configuration option
------------------------

A new configuration parameters are added in neutron-vpnaas.conf

* vpn_transit_net_cidr = 169.254.64.0/18

HA support
----------

The HA function is same as it is done currently L3 HA. But the VRRP protocol
is run on the VPN transit network. No extra HA network is needed.

Work Items
----------

* Add new VPN scheduler in the VPNaaS plugin RPC notification
* Add new namespace management in VPN agent side
* Add transit network management in VPN agent side
* Add transit routes management in VPN agent side
* Add VPN GW IP address support in both VPN plugin side and VPN agent side
* Add external VPN gateway DB model
* Add HA support on the agent side
* Add VPN metering function in VPN agent side

References
==========

.. _rfe: https://bugs.launchpad.net/neutron/+bug/1586253
