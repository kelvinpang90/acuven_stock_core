"""sku_merges: SKU merge orders and their count snapshots

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-19

契约 2.8 的存放。只建两张新表（契约 2.2），不动已有的表与约束，所以不按 core_instance 区分实例。
“合并单互不重叠”不在库里约束，由新建命令锁两个 SKU 行后检查保证（契约 2.8）。
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
ACTOR_PREFIXES = ("", "counted_", "executed_", "completed_", "cancelled_")
CREDENTIAL_COLUMNS = (
    "credential_id",
    "counted_credential_id",
    "executed_credential_id",
    "completed_credential_id",
    "cancelled_credential_id",
)
ENTRY_COLUMNS = ("transfer_out_entry_id", "transfer_in_entry_id", "adjustment_entry_id")

INDEXES = {
    "ix_sku_merges_tenant_id_source_sku_id": ["tenant_id", "source_sku_id"],
    "ix_sku_merges_tenant_id_canonical_sku_id": ["tenant_id", "canonical_sku_id"],
}


def _in(column: str, values: Sequence[str]) -> str:
    allowed = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({allowed})"


def _set_exactly_when(condition: str, columns: Sequence[str]) -> str:
    return " AND ".join(f"({condition}) = ({column} IS NOT NULL)" for column in columns)


def _actor_columns(prefix: str) -> list[sa.Column]:
    return [
        sa.Column(f"{prefix}actor_ref", REF, nullable=True),
        sa.Column(f"{prefix}actor_role", CODE32, nullable=True),
    ]


def _merge_checks() -> list[sa.CheckConstraint]:
    count_columns = (
        " AND ".join(f"(pool_id IS NULL) = ({column} IS NULL)" for column in COUNT_COLUMNS[1:])
        + f" AND (NOT ({_in('status', COUNTED_STATUSES)}) OR pool_id IS NOT NULL)"
        + f" AND (status <> '{SELLING_PAUSED}' OR pool_id IS NULL)"
    )
    execution_columns = (
        _set_exactly_when(_in("status", EXECUTED_STATUSES), EXECUTION_COLUMNS)
        + f" AND ({_in('status', EXECUTED_STATUSES)} OR adjustment_entry_id IS NULL)"
    )
    checks = {
        "source_not_canonical": "source_sku_id <> canonical_sku_id",
        "status": _in("status", STATUSES),
        "count_columns": count_columns,
        "execution_columns": execution_columns,
        "completion_columns": _set_exactly_when(f"status = '{COMPLETED}'", COMPLETION_COLUMNS),
        "cancellation_columns": _set_exactly_when(f"status = '{CANCELLED}'", CANCELLATION_COLUMNS),
    }
    for prefix in ACTOR_PREFIXES:
        checks[f"{prefix}actor"] = f"({prefix}actor_ref IS NULL) = ({prefix}actor_role IS NULL)"
    return [
        sa.CheckConstraint(sql, name=op.f(f"ck_sku_merges_{name}")) for name, sql in checks.items()
    ]


def _merge_foreign_keys() -> list[sa.ForeignKeyConstraint]:
    # 复合外键都以 tenant_id 开头，在模型里显式命名，名字里带上第二列。
    targets = [
        ("source_sku_id", "skus", "sku_id"),
        ("canonical_sku_id", "skus", "sku_id"),
        ("pool_id", "stock_pools", "pool_id"),
        *[(column, "credentials", "credential_id") for column in CREDENTIAL_COLUMNS],
        *[(column, "ledger_entries", "entry_id") for column in ENTRY_COLUMNS],
    ]
    return [
        sa.ForeignKeyConstraint(
            ["tenant_id", column],
            [f"{table}.tenant_id", f"{table}.{target}"],
            name=op.f(f"fk_sku_merges_{column}_{table}"),
        )
        for column, table, target in targets
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
        # 新建。
        sa.Column("credential_id", ID, nullable=False),
        *_actor_columns(""),
        sa.Column("request_id", REF, nullable=False),
        sa.Column("created_at", TIMESTAMP, server_default=NOW, nullable=False),
        # 录入实数。
        sa.Column("pool_id", ID, nullable=True),
        sa.Column("shelf_count", sa.BigInteger(), nullable=True),
        sa.Column("unsellable_count", sa.BigInteger(), nullable=True),
        sa.Column("counted_credential_id", ID, nullable=True),
        *_actor_columns("counted_"),
        sa.Column("counted_request_id", REF, nullable=True),
        sa.Column("counted_at", TIMESTAMP, nullable=True),
        # 执行。
        sa.Column("executed_credential_id", ID, nullable=True),
        *_actor_columns("executed_"),
        sa.Column("executed_request_id", REF, nullable=True),
        sa.Column("executed_at", TIMESTAMP, nullable=True),
        sa.Column("transfer_out_entry_id", ID, nullable=True),
        sa.Column("transfer_in_entry_id", ID, nullable=True),
        sa.Column("adjustment_entry_id", ID, nullable=True),
        sa.Column("repointed_line_count", sa.Integer(), nullable=True),
        # 完成。
        sa.Column("completed_credential_id", ID, nullable=True),
        *_actor_columns("completed_"),
        sa.Column("completed_request_id", REF, nullable=True),
        sa.Column("completed_at", TIMESTAMP, nullable=True),
        # 取消。
        sa.Column("cancelled_credential_id", ID, nullable=True),
        *_actor_columns("cancelled_"),
        sa.Column("cancelled_request_id", REF, nullable=True),
        sa.Column("cancelled_at", TIMESTAMP, nullable=True),
        sa.PrimaryKeyConstraint("merge_id", name=op.f("pk_sku_merges")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.tenant_id"], name=op.f("fk_sku_merges_tenant_id_tenants")
        ),
        *_merge_foreign_keys(),
        sa.UniqueConstraint("tenant_id", "merge_id", name=op.f("uq_sku_merges_tenant_id_merge_id")),
        *_merge_checks(),
        **TABLE_OPTIONS,
    )
    for name, columns in INDEXES.items():
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
            for column in (*QUANTITIES, "balance_version")
        ],
        **TABLE_OPTIONS,
    )


def downgrade() -> None:
    # 快照有外键指向合并单，先删；两条索引支撑指向 skus 的外键，删表时一并删掉。
    op.drop_table("sku_merge_snapshots")
    op.drop_table("sku_merges")
