"""postgresql case insensitive indexes

Revision ID: 6f2b8d0f5f4e
Revises: 2b231de97dc3
Create Date: 2026-05-17 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = '6f2b8d0f5f4e'
down_revision = '2b231de97dc3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().engine.name != 'postgresql':
        return

    op.create_index(
        'ix_users_username_lower_unique',
        'users',
        [sa.text('lower(username)')],
        unique=True,
        postgresql_where=sa.text('username IS NOT NULL'),
    )
    op.create_index(
        'ix_nodes_name_lower_unique',
        'nodes',
        [sa.text('lower(name)')],
        unique=True,
        postgresql_where=sa.text('name IS NOT NULL'),
    )


def downgrade() -> None:
    if op.get_bind().engine.name != 'postgresql':
        return

    op.drop_index('ix_nodes_name_lower_unique', table_name='nodes')
    op.drop_index('ix_users_username_lower_unique', table_name='users')
