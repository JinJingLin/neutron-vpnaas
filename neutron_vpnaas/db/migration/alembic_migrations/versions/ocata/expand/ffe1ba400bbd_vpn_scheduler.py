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

"""vpn scheduler

Revision ID: ffe1ba400bbd
Revises: 22e0145ac80b
Create Date: 2016-09-18 17:55:58.623575

"""

# revision identifiers, used by Alembic.
revision = 'ffe1ba400bbd'
down_revision = '22e0145ac80b'

from alembic import op
import sqlalchemy as sa


def upgrade():
    op.create_table(
        'routervpnagentbindings',
        sa.Column('router_id', sa.String(length=36), nullable=True),
        sa.Column('vpn_agent_id', sa.String(length=36), nullable=True),
        sa.ForeignKeyConstraint(['vpn_agent_id'], ['agents.id'],
                                ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['router_id'], ['routers.id'],
                                ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('router_id', 'vpn_agent_id'),
    )
