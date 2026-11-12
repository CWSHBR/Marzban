"""per-node xray config

Revision ID: d2a5b8c31f47
Revises: c1f4a7b90e21
Create Date: 2026-11-12 00:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'd2a5b8c31f47'
down_revision = 'c1f4a7b90e21'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # All nullable and without defaults: NULL keeps every existing node on the
    # shared template, so the upgrade changes nothing until a config is saved.
    with op.batch_alter_table('nodes') as batch_op:
        batch_op.add_column(sa.Column('xray_config', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('xray_config_updated_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('xray_config_updated_by', sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column('applied_config_hash', sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('nodes') as batch_op:
        batch_op.drop_column('applied_config_hash')
        batch_op.drop_column('xray_config_updated_by')
        batch_op.drop_column('xray_config_updated_at')
        batch_op.drop_column('xray_config')
