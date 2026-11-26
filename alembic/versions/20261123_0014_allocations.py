"""allocations: pool_allocations, channel_allocations, allocation_changes

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-23

契约 3.1 的存放，以及 3.2 中落在 channel_allocations 上的读回待核对与人工生效两组列（0.22）。
只建新表（契约 2.2），不动已有的表与约束，所以不按 core_instance 区分实例。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 app/models/platform.py 相同的列约定（契约 2.2）。
ID = sa.String(32, collation="utf8mb4_bin")
REF = sa.String(128, collation="utf8mb4_bin")
CODE32 = sa.String(32, collation="utf8mb4_bin")
TIMESTAMP = mysql.DATETIME(fsp=6)
NOW = sa.text("CURRENT_TIMESTAMP(6)")
ZERO = sa.text("0")
TABLE_OPTIONS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_ai_ci"}
ALLOCATION_RANGE = "BETWEEN 0 AND 1000000"
REVIEW_COLUMNS = ("review_reason", "review_since")


def _tenant_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id"], ["tenants.tenant_id"], name=op.f(f"fk_{table}_tenant_id_tenants")
    )


def _balance_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", "pool_id", "sku_id"],
        ["stock_balances.tenant_id", "stock_balances.pool_id", "stock_balances.sku_id"],
        name=op.f(f"fk_{table}_tenant_id_stock_balances"),
    )


def upgrade() -> None:
    op.create_table(
        "pool_allocations",
        sa.Column("tenant_id", ID, nullable=False),
        sa.Column("pool_id", ID, nullable=False),
        sa.Column("sku_id", ID, nullable=False),
        sa.Column("safety_stock", sa.Integer(), server_default=sa.text("2"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=ZERO, nullable=False),
        sa.Column("updated_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "pool_id", "sku_id", name=op.f("pk_pool_allocations")),
        _tenant_fk("pool_allocations"),
        _balance_fk("pool_allocations"),
        sa.CheckConstraint(
            f"safety_stock {ALLOCATION_RANGE}", name=op.f("ck_pool_allocations_safety_stock")
        ),
        sa.CheckConstraint("version >= 0", name=op.f("ck_pool_allocations_version")),
        **TABLE_OPTIONS,
    )
    op.create_table(
        "channel_allocations",
        sa.Column("tenant_id", ID, nullable=False),
        sa.Column("channel_id", ID, nullable=False),
        sa.Column("pool_id", ID, nullable=False),
        sa.Column("sku_id", ID, nullable=False),
        sa.Column("quota", sa.Integer(), server_default=ZERO, nullable=False),
        sa.Column("pending", sa.Integer(), server_default=ZERO, nullable=False),
        sa.Column("review_hold", sa.Boolean(), server_default=ZERO, nullable=False),
        sa.Column("review_reason", sa.String(255), nullable=True),
        sa.Column("review_since", TIMESTAMP, nullable=True),
        sa.Column("manual_confirmed_quota", sa.Integer(), nullable=True),
        sa.Column("manual_confirmed_at", TIMESTAMP, nullable=True),
        sa.Column("updated_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id", "channel_id", "sku_id", name=op.f("pk_channel_allocations")
        ),
        _tenant_fk("channel_allocations"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "channel_id"],
            ["sales_channels.tenant_id", "sales_channels.channel_id"],
            name=op.f("fk_channel_allocations_tenant_id_sales_channels"),
        ),
        _balance_fk("channel_allocations"),
        sa.CheckConstraint(
            f"quota {ALLOCATION_RANGE}", name=op.f("ck_channel_allocations_quota_range")
        ),
        sa.CheckConstraint(
            f"pending {ALLOCATION_RANGE}", name=op.f("ck_channel_allocations_pending_range")
        ),
        sa.CheckConstraint(
            f"manual_confirmed_quota {ALLOCATION_RANGE}",
            name=op.f("ck_channel_allocations_manual_quota_range"),
        ),
        sa.CheckConstraint(
            " AND ".join(
                f"(review_hold <> 0) = ({column} IS NOT NULL)" for column in REVIEW_COLUMNS
            ),
            name=op.f("ck_channel_allocations_review_columns"),
        ),
        sa.CheckConstraint(
            "(manual_confirmed_quota IS NULL) = (manual_confirmed_at IS NULL)",
            name=op.f("ck_channel_allocations_manual_columns"),
        ),
        **TABLE_OPTIONS,
    )
    op.create_index(
        op.f("ix_channel_allocations_tenant_id_pool_id_sku_id"),
        "channel_allocations",
        ["tenant_id", "pool_id", "sku_id"],
        unique=False,
    )
    op.create_table(
        "allocation_changes",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", ID, nullable=False),
        sa.Column("pool_id", ID, nullable=False),
        sa.Column("sku_id", ID, nullable=False),
        sa.Column("changes", sa.JSON(), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("credential_id", ID, nullable=False),
        sa.Column("actor_ref", REF, nullable=True),
        sa.Column("actor_role", CODE32, nullable=True),
        sa.Column("request_id", REF, nullable=False),
        sa.Column("created_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_allocation_changes")),
        _tenant_fk("allocation_changes"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "pool_id", "sku_id"],
            ["pool_allocations.tenant_id", "pool_allocations.pool_id", "pool_allocations.sku_id"],
            name=op.f("fk_allocation_changes_tenant_id_pool_allocations"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "credential_id"],
            ["credentials.tenant_id", "credentials.credential_id"],
            name=op.f("fk_allocation_changes_tenant_id_credentials"),
        ),
        sa.CheckConstraint(
            "JSON_TYPE(changes) = 'OBJECT'", name=op.f("ck_allocation_changes_changes")
        ),
        sa.CheckConstraint("CHAR_LENGTH(reason) > 0", name=op.f("ck_allocation_changes_reason")),
        sa.CheckConstraint(
            "(actor_ref IS NULL) = (actor_role IS NULL)", name=op.f("ck_allocation_changes_actor")
        ),
        **TABLE_OPTIONS,
    )
    op.create_index(
        op.f("ix_allocation_changes_tenant_id_pool_id_sku_id_id"),
        "allocation_changes",
        ["tenant_id", "pool_id", "sku_id", "id"],
        unique=False,
    )


def downgrade() -> None:
    # 两条索引各支撑一条复合外键，不能先单独删；删表时一并删掉。先删引用别表的。
    op.drop_table("allocation_changes")
    op.drop_table("channel_allocations")
    op.drop_table("pool_allocations")
