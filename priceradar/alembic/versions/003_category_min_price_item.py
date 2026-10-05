"""add min-price product link on tracked categories

Revision ID: 003
Revises: 002
Create Date: 2026-10-02
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tracked_categories",
        sa.Column("min_price_url", sa.String(2048), nullable=True),
    )
    op.add_column(
        "tracked_categories",
        sa.Column("min_price_title", sa.String(500), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tracked_categories", "min_price_title")
    op.drop_column("tracked_categories", "min_price_url")
