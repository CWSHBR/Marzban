"""add hysteria proxy type

Revision ID: 4dbb93282645
Revises: 6f2b8d0f5f4e
Create Date: 2026-06-06 00:00:00.000000

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = '4dbb93282645'
down_revision = '6f2b8d0f5f4e'
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TYPE proxytypes ADD VALUE IF NOT EXISTS 'Hysteria'")


def downgrade() -> None:
    pass
