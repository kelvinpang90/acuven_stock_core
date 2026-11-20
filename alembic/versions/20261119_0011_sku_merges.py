"""sku_merges: SKU merges and their count snapshots

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-19

契约 2.8 的存放。只建两张新表（契约 2.2），不动已有的表与约束，所以不按 core_instance 区分实例。
“合并单互不重叠”不建约束，由新建命令保证（契约 2.8）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op
from app.domain.sku_merges import (
    CANCELLED,
    COMPLETED,
    COUNTED,
    RECONCILE_PENDING,
    SELLING_PAUSED,
    STATUSES,
)

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 app/models/merges.py 相同的列约定（契约 2.2）。
ID = sa.String(32, collation="utf8mb4_bin")
REF = sa.String(128, collation="utf8mb4_bin")
CODE32 = sa.String(32, collation="utf8mb4_bin")
TIMESTAMP = mysql.DATETIME(fsp=6)
NOW = sa.text("CURRENT_TIMESTAMP(6)")
TABLE_OPTIONS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_ai_ci"}
QUANTITIES = ("stock_balance", "reserved", "unavailable", "paid_pending_dispatch")
COUNT_COLUMNS = (
    "pool_id",
    "shelf_count",
    "unsellable_count",
    "counted_credential_id",
    "counted_request_id",
    "counted_at",
)
COUNTED_STATUSES = (COUNTED, RECONCILE_PENDING, COMPLETED)
EXECUTION_COLUMNS = (
    "executed_credential_id",
    "executed_request_id",
    "executed_at",
    "transfer_out_entry_id",
    "transfer_in_entry_id",
    "repointed_line_count",
)
EXECUTED_STATUSES = (RECONCILE_PENDING, COMPLETED)
COMPLETION_COLUMNS = ("completed_credential_id", "completed_request_id", "completed_at")
CANCELLATION_COLUMNS = ("cancelled_credential_id", "cancelled_request_id", "cancelled_at")
ACTOR_GROUPS = ("counted", "executed", "completed", "cancelled")
CREDENTIAL_COLUMNS = (
    "credential_id",
    "counted_credential_id",
    "executed_credential_id",
    "completed_credential_id",
    "cancelled_credential_id",
)
ENTRY_COLUMNS = ("transfer_out_entry_id", "transfer_in_entry_id", "adjustment_entry_id")
SKU_INDEXES = {
    "ix_sku_merges_tenant_id_source_sku_id": ["tenant_id", "source_sku_id"],
    "ix_sku_merges_tenant_id_canonical_sku_id": ["tenant_id", "canonical_sku_id"],
}


def _in(column: str, values: Sequence[str]) -> str:
    allowed = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({allowed})"


def _same_nullness(columns: Sequence[str]) -> str:
    first, *rest = columns
    return " AND ".join(f"({first} IS NULL) = ({column} IS NULL)" for column in rest)


def _present_exactly_in(statuses: Sequence[str], columns: Sequence[str]) -> str:
    in_statuses = _in("status", statuses)
    return " AND ".join(f"({in_statuses}) = ({column} IS NOT NULL)" for column in columns)


def _actor_columns(group: str) -> list[sa.Column]:
    return [
        sa.Column(f"{group}_actor_ref", REF, nullable=True),
        sa.Column(f"{group}_actor_role", CODE32, nullable=True),
    ]


def upgrade() -> None:
    op.create_table(
        "sku_merges",
        sa.Column("merge_id", ID, nullable=False),
        sa.Column("tenant_id", ID, nullable=False),
        sa.Column("source_sku_id", ID, nullable=False),
        sa.Column("canonical_sku_id", ID, nullable=False),
        sa.Column("status", CODE32, nullable=False),
        sa.Column("reason", sa.String(500), nullable=True),
        sa.Column("credential_id", ID, nullable=False),
        sa.Column("actor_ref", REF, nullable=False),
        sa.Column("actor_role", CODE32, nullable=False),
        sa.Column("request_id", REF, nullable=False),
        sa.Column("created_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.Column("pool_id", ID, nullable=True),
        sa.Column("shelf_count", sa.BigInteger(), nullable=True),
        sa.Column("unsellable_count", sa.BigInteger(), nullable=True),
        sa.Column("counted_credential_id", ID, nullable=True),
        *_actor_columns("counted"),
        sa.Column("counted_request_id", REF, nullable=True),
        sa.Column("counted_at", TIMESTAMP, nullable=True),
        sa.Column("executed_credential_id", ID, nullable=True),
        *_actor_columns("executed"),
        sa.Column("executed_request_id", REF, nullable=True),
        sa.Column("executed_at", TIMESTAMP, nullable=True),
        sa.Column("transfer_out_entry_id", ID, nullable=True),
        sa.Column("transfer_in_entry_id", ID, nullable=True),
        sa.Column("adjustment_entry_id", ID, nullable=True),
        sa.Column("repointed_line_count", sa.BigInteger(), nullable=True),
        sa.Column("completed_credential_id", ID, nullable=True),
        *_actor_columns("completed"),
        sa.Column("completed_request_id", REF, nullable=True),
        sa.Column("completed_at", TIMESTAMP, nullable=True),
        sa.Column("cancelled_credential_id", ID, nullable=True),
        *_actor_columns("cancelled"),
        sa.Column("cancelled_request_id", REF, nullable=True),
        sa.Column("cancelled_at", TIMESTAMP, nullable=True),
        sa.PrimaryKeyConstraint("merge_id", name=op.f("pk_sku_merges")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.tenant_id"], name=op.f("fk_sku_merges_tenant_id_tenants")
        ),
        # 以 tenant_id 开头的复合外键在模型里显式命名，名字里带上第二列。
        sa.ForeignKeyConstraint(
            ["tenant_id", "source_sku_id"],
            ["skus.tenant_id", "skus.sku_id"],
            name=op.f("fk_sku_merges_source_sku_id_skus"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "canonical_sku_id"],
            ["skus.tenant_id", "skus.sku_id"],
            name=op.f("fk_sku_merges_canonical_sku_id_skus"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "pool_id"],
            ["stock_pools.tenant_id", "stock_pools.pool_id"],
            name=op.f("fk_sku_merges_pool_id_stock_pools"),
        ),
        *[
            sa.ForeignKeyConstraint(
                ["tenant_id", column],
                ["credentials.tenant_id", "credentials.credential_id"],
                name=op.f(f"fk_sku_merges_{column}_credentials"),
            )
            for column in CREDENTIAL_COLUMNS
        ],
        *[
            sa.ForeignKeyConstraint(
                ["tenant_id", column],
                ["ledger_entries.tenant_id", "ledger_entries.entry_id"],
                name=op.f(f"fk_sku_merges_{column}_ledger_entries"),
            )
            for column in ENTRY_COLUMNS
        ],
        sa.UniqueConstraint("tenant_id", "merge_id", name=op.f("uq_sku_merges_tenant_id_merge_id")),
        sa.CheckConstraint(
            "source_sku_id <> canonical_sku_id", name=op.f("ck_sku_merges_source_not_canonical")
        ),
        sa.CheckConstraint(_in("status", STATUSES), name=op.f("ck_sku_merges_status")),
        sa.CheckConstraint(
            _same_nullness(COUNT_COLUMNS)
            + f" AND (NOT ({_in('status', COUNTED_STATUSES)}) OR pool_id IS NOT NULL)"
            + f" AND (status <> '{SELLING_PAUSED}' OR pool_id IS NULL)",
            name=op.f("ck_sku_merges_count_columns"),
        ),
        sa.CheckConstraint(
            _present_exactly_in(EXECUTED_STATUSES, EXECUTION_COLUMNS)
            + f" AND ({_in('status', EXECUTED_STATUSES)} OR adjustment_entry_id IS NULL)",
            name=op.f("ck_sku_merges_execution_columns"),
        ),
        sa.CheckConstraint(
            _present_exactly_in((COMPLETED,), COMPLETION_COLUMNS),
            name=op.f("ck_sku_merges_completion_columns"),
        ),
        sa.CheckConstraint(
            _present_exactly_in((CANCELLED,), CANCELLATION_COLUMNS),
            name=op.f("ck_sku_merges_cancellation_columns"),
        ),
        *[
            sa.CheckConstraint(
                f"({group}_actor_ref IS NULL) = ({group}_actor_role IS NULL)",
                name=op.f(f"ck_sku_merges_{group}_actor"),
            )
            for group in ACTOR_GROUPS
        ],
        **TABLE_OPTIONS,
    )
    # 两条索引支撑指向两个 SKU 的外键，MySQL 随之删掉建表时按外键名隐式建的索引。
    for name, columns in SKU_INDEXES.items():
        op.create_index(op.f(name), "sku_merges", columns, unique=False)

    op.create_table(
        "sku_merge_snapshots",
        sa.Column("merge_id", ID, nullable=False),
        sa.Column("sku_id", ID, nullable=False),
        sa.Column("tenant_id", ID, nullable=False),
        sa.Column("pool_id", ID, nullable=False),
        *[sa.Column(column, sa.BigInteger(), nullable=False) for column in QUANTITIES],
        sa.Column("balance_version", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("merge_id", "sku_id", name=op.f("pk_sku_merge_snapshots")),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.tenant_id"],
            name=op.f("fk_sku_merge_snapshots_tenant_id_tenants"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "merge_id"],
            ["sku_merges.tenant_id", "sku_merges.merge_id"],
            name=op.f("fk_sku_merge_snapshots_tenant_id_sku_merges"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "pool_id", "sku_id"],
            ["stock_balances.tenant_id", "stock_balances.pool_id", "stock_balances.sku_id"],
            name=op.f("fk_sku_merge_snapshots_tenant_id_stock_balances"),
        ),
        *[
            sa.CheckConstraint(
                f"{column} >= 0", name=op.f(f"ck_sku_merge_snapshots_{column}_nonnegative")
            )
            for column in QUANTITIES
        ],
        sa.CheckConstraint(
            "reserved + unavailable <= stock_balance",
            name=op.f("ck_sku_merge_snapshots_reserved_unavailable_within_balance"),
        ),
        sa.CheckConstraint(
            "balance_version >= 0", name=op.f("ck_sku_merge_snapshots_balance_version_nonnegative")
        ),
        **TABLE_OPTIONS,
    )


def downgrade() -> None:
    # 两条索引支撑指向 SKU 的外键，不能先单独删；删表时一并删掉。
    op.drop_table("sku_merge_snapshots")
    op.drop_table("sku_merges")
