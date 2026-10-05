"""tracked categories and category alerts

Revision ID: 002
Revises: 001
Create Date: 2026-10-02
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Extend ruletype enum with category alert kinds (PostgreSQL)
    op.execute("ALTER TYPE ruletype ADD VALUE IF NOT EXISTS 'category_min_drop'")
    op.execute("ALTER TYPE ruletype ADD VALUE IF NOT EXISTS 'category_price_below'")
    op.execute("ALTER TYPE ruletype ADD VALUE IF NOT EXISTS 'category_new_sku'")

    marketplace_enum = postgresql.ENUM(
        "wildberries",
        "ozon",
        "yandex_market",
        name="marketplace",
        create_type=False,
    )

    op.create_table(
        "tracked_categories",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("marketplace", marketplace_enum, nullable=False),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("title", sa.String(500), nullable=False, server_default=""),
        sa.Column("external_key", sa.String(64), nullable=False),
        sa.Column("min_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("previous_min_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("item_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("parse_errors_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_parsed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name="pk_tracked_categories"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_tracked_categories_user_id_users",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "user_id",
            "marketplace",
            "external_key",
            name="uq_user_marketplace_category",
        ),
    )
    op.create_index(
        "ix_tracked_categories_user_id", "tracked_categories", ["user_id"]
    )

    op.create_table(
        "category_snapshots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("category_id", sa.BigInteger(), nullable=False),
        sa.Column("sku_ids", sa.JSON(), nullable=False),
        sa.Column("min_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("median_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("item_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_category_snapshots"),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["tracked_categories.id"],
            name="fk_category_snapshots_category_id_tracked_categories",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_category_snapshots_category_id", "category_snapshots", ["category_id"]
    )
    op.create_index(
        "ix_category_snapshots_recorded_at", "category_snapshots", ["recorded_at"]
    )

    op.add_column(
        "alert_rules",
        sa.Column("category_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_alert_rules_category_id_tracked_categories",
        "alert_rules",
        "tracked_categories",
        ["category_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.alter_column("alert_logs", "product_id", existing_type=sa.BigInteger(), nullable=True)
    op.add_column(
        "alert_logs",
        sa.Column("category_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_alert_logs_category_id_tracked_categories",
        "alert_logs",
        "tracked_categories",
        ["category_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_alert_logs_category_id_tracked_categories", "alert_logs", type_="foreignkey"
    )
    op.drop_column("alert_logs", "category_id")
    op.alter_column("alert_logs", "product_id", existing_type=sa.BigInteger(), nullable=False)

    op.drop_constraint(
        "fk_alert_rules_category_id_tracked_categories", "alert_rules", type_="foreignkey"
    )
    op.drop_column("alert_rules", "category_id")

    op.drop_index("ix_category_snapshots_recorded_at", table_name="category_snapshots")
    op.drop_index("ix_category_snapshots_category_id", table_name="category_snapshots")
    op.drop_table("category_snapshots")

    op.drop_index("ix_tracked_categories_user_id", table_name="tracked_categories")
    op.drop_table("tracked_categories")
    # PostgreSQL cannot easily remove enum values; leave them in place.
