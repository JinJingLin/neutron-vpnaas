# Copyright 2016 MingShuang Xian/IBM
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
#

"""vpn ha mode

Revision ID: 6e6b317ff8f2
Revises: ffe1ba400bbd
Create Date: 2016-09-18 20:16:43.298978

"""

# revision identifiers, used by Alembic.
revision = '6e6b317ff8f2'
down_revision = 'ffe1ba400bbd'

from alembic import op
import sqlalchemy as sa


vpn_ha_states = sa.Enum('active', 'standby', name='l3_ha_states')

def upgrade():
    op.create_table(
        'VPN_router_ha_status',
        sa.Column('router_id', sa.String(length=36), nullable=False,
                  primary_key=True),
        sa.Column('ha', sa.Boolean(), nullable=False,
                  server_default=sa.sql.false()),
        sa.ForeignKeyConstraint(['router_id'], ['routers.id'],
                                ondelete='CASCADE'),
    )

    op.create_table('ha_vpn_router_agent_port_bindings',
                    sa.Column('port_id', sa.String(length=36),
                              nullable=False),
                    sa.Column('router_id', sa.String(length=36),
                              nullable=False),
                    sa.Column('vpn_agent_id', sa.String(length=36),
                              nullable=True),
                    sa.Column('state', vpn_ha_states,
                              server_default='standby'),
                    sa.PrimaryKeyConstraint('port_id', 'router_id'),
                    sa.ForeignKeyConstraint(['port_id'], ['ports.id'],
                                            ondelete='CASCADE'),
                    sa.ForeignKeyConstraint(['router_id'], ['routers.id'],
                                            ondelete='CASCADE'),
                    sa.ForeignKeyConstraint(['vpn_agent_id'], ['agents.id'],
                                            ondelete='CASCADE'))

    op.create_table('ha_vpn_router_networks',
                    sa.Column('project_id', sa.String(length=255),
                              nullable=False, primary_key=True),
                    sa.Column('network_id', sa.String(length=36),
                              nullable=False,
                              primary_key=True),
                    sa.ForeignKeyConstraint(['network_id'], ['networks.id'],
                                            ondelete='CASCADE'))

    op.create_table('ha_vpn_vpn_router_vrid_allocations',
                    sa.Column('network_id', sa.String(length=36),
                              nullable=False,
                              primary_key=True),
                    sa.Column('vr_id', sa.Integer(),
                              nullable=False,
                              primary_key=True),
                    sa.ForeignKeyConstraint(['network_id'], ['networks.id'],
                                            ondelete='CASCADE'))


