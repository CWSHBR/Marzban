"""add high availability tables

Revision ID: c1f4a7b90e21
Revises: 4dbb93282645
Create Date: 2026-08-03 00:00:00.000000

"""
from datetime import datetime

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'c1f4a7b90e21'
down_revision = '4dbb93282645'
branch_labels = None
depends_on = None


def upgrade() -> None:
    ha_leases = op.create_table(
        'ha_leases',
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('holder', sa.String(length=128), nullable=True),
        sa.Column('epoch', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
        sa.Column('advertise_url', sa.String(length=256), nullable=True),
        sa.Column('acquired_at', sa.DateTime(), nullable=True),
        sa.Column('renewed_at', sa.DateTime(), nullable=True),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.Column('stepdown_requested_epoch', sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint('name'),
    )
    # The single row every panel contends for. Seeded expired so the first
    # panel to start can acquire immediately.
    op.bulk_insert(ha_leases, [{
        'name': 'panel',
        'holder': None,
        'epoch': 0,
        'advertise_url': None,
        'acquired_at': None,
        'renewed_at': None,
        'expires_at': datetime(1970, 1, 1),
        'stepdown_requested_epoch': None,
    }])

    op.create_table(
        'ha_members',
        sa.Column('panel_id', sa.String(length=128), nullable=False),
        sa.Column('role', sa.String(length=16), nullable=False),
        sa.Column('version', sa.String(length=32), nullable=True),
        sa.Column('advertise_url', sa.String(length=256), nullable=True),
        # sa.true() renders per dialect. A literal text('1') is accepted by
        # SQLite but Postgres rejects an integer default on a boolean column.
        sa.Column('local_core_enabled', sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('last_seen', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('panel_id'),
    )

    op.create_table(
        'node_sync_outbox',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'),
                  autoincrement=True, nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('op', sa.String(length=32), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('payload', sa.JSON(), nullable=True),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('done_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_node_sync_outbox_created_at', 'node_sync_outbox', ['created_at'])
    op.create_index('ix_node_sync_outbox_done_at', 'node_sync_outbox', ['done_at'])

    ha_cache_versions = op.create_table(
        'ha_cache_versions',
        sa.Column('key', sa.String(length=64), nullable=False),
        sa.Column('version', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('key'),
    )
    op.bulk_insert(ha_cache_versions, [
        {'key': 'hosts', 'version': 0, 'updated_at': None},
        {'key': 'core_config', 'version': 0, 'updated_at': None},
    ])

    op.create_table(
        'notification_outbox',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'),
                  autoincrement=True, nullable=False),
        sa.Column('action', sa.String(length=64), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('tries', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('send_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('sent_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_notification_outbox_send_at', 'notification_outbox', ['send_at'])
    op.create_index('ix_notification_outbox_sent_at', 'notification_outbox', ['sent_at'])

    # Deliberately left empty: the migration cannot resolve XRAY_JSON, whose
    # path differs per host. The first panel to boot seeds it from its file.
    op.create_table(
        'core_configs',
        sa.Column('id', sa.Integer(), autoincrement=False, nullable=False),
        sa.Column('config', sa.JSON(), nullable=False),
        sa.Column('version', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('updated_by', sa.String(length=128), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('core_configs')
    op.drop_index('ix_notification_outbox_sent_at', table_name='notification_outbox')
    op.drop_index('ix_notification_outbox_send_at', table_name='notification_outbox')
    op.drop_table('notification_outbox')
    op.drop_table('ha_cache_versions')
    op.drop_index('ix_node_sync_outbox_done_at', table_name='node_sync_outbox')
    op.drop_index('ix_node_sync_outbox_created_at', table_name='node_sync_outbox')
    op.drop_table('node_sync_outbox')
    op.drop_table('ha_members')
    op.drop_table('ha_leases')
