"""inventory: locations, stock_pools, sales_channels, stock_balances, ledger_entries,
idempotency_records

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 app/models/inventory.py 相同的列约定（契约 2.2）。
ID = sa.String(32, collation="utf8mb4_bin")
STATUS = sa.String(16, collation="utf8mb4_bin")
REF = sa.String(128, collation="utf8mb4_bin")
CODE32 = sa.String(32, collation="utf8mb4_bin")
TIMESTAMP = mysql.DATETIME(fsp=6)
NOW = sa.text("CURRENT_TIMESTAMP(6)")
ZERO = sa.text("0")
TABLE_OPTIONS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_ai_ci"}
QUANTITIES = ("stock_balance", "reserved", "unavailable", "paid_pending_dispatch")
ENTRY_TYPES = (
    "OPENING_BALANCE",
    "RECEIPT",
    "COUNT_ADJUSTMENT",
    "MARK_UNAVAILABLE",
    "RESTORE_AVAILABLE",
    "WRITE_OFF",
    "RESERVE",
    "RELEASE",
    "SALE_COMMIT",
    "LATE_SALE_COMMIT",
    "DISPATCH",
    "REFUND_RESTOCK",
    "RETURN_SELLABLE",
    "RETURN_UNSELLABLE",
    "MERGE_TRANSFER_OUT",
    "MERGE_TRANSFER_IN",
    "DEMO_RESET",
    "CORRECTION",
)


def _one_of(table: str, column: str, values: Sequence[str]) -> sa.CheckConstraint:
    allowed = ", ".join(f"'{value}'" for value in values)
    return sa.CheckConstraint(f"{column} IN ({allowed})", name=op.f(f"ck_{table}_{column}"))


def _quantity_invariants(table: str, suffix: str = "") -> list[sa.CheckConstraint]:
    checks = [
        sa.CheckConstraint(
            f"{column}{suffix} >= 0", name=op.f(f"ck_{table}_{column}{suffix}_nonnegative")
        )
        for column in QUANTITIES
    ]
    checks.append(
        sa.CheckConstraint(
            f"reserved{suffix} + unavailable{suffix} <= stock_balance{suffix}",
            name=op.f(f"ck_{table}_reserved_unavailable{suffix}_within_balance"),
        )
    )
    return checks


def _tenant_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id"], ["tenants.tenant_id"], name=op.f(f"fk_{table}_tenant_id_tenants")
    )


def upgrade() -> None:
    op.create_table(
        "locations",
        sa.Column("location_id", ID, nullable=False),
        sa.Column("tenant_id", ID, nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("status", STATUS, nullable=False),
        sa.Column("created_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.Column("updated_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("location_id", name=op.f("pk_locations")),
        _tenant_fk("locations"),
        sa.UniqueConstraint(
            "tenant_id", "location_id", name=op.f("uq_locations_tenant_id_location_id")
        ),
        _one_of("locations", "status", ("active", "inactive")),
        **TABLE_OPTIONS,
    )
    op.create_table(
        "stock_pools",
        sa.Column("pool_id", ID, nullable=False),
        sa.Column("tenant_id", ID, nullable=False),
        sa.Column("location_id", ID, nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("status", STATUS, nullable=False),
        sa.Column("created_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.Column("updated_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("pool_id", name=op.f("pk_stock_pools")),
        _tenant_fk("stock_pools"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "location_id"],
            ["locations.tenant_id", "locations.location_id"],
            name=op.f("fk_stock_pools_tenant_id_locations"),
        ),
        sa.UniqueConstraint("tenant_id", "pool_id", name=op.f("uq_stock_pools_tenant_id_pool_id")),
        _one_of("stock_pools", "status", ("initializing", "live")),
        **TABLE_OPTIONS,
    )
    op.create_table(
        "sales_channels",
        sa.Column("channel_id", ID, nullable=False),
        sa.Column("tenant_id", ID, nullable=False),
        sa.Column("pool_id", ID, nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("status", STATUS, nullable=False),
        sa.Column("created_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.Column("updated_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("channel_id", name=op.f("pk_sales_channels")),
        _tenant_fk("sales_channels"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "pool_id"],
            ["stock_pools.tenant_id", "stock_pools.pool_id"],
            name=op.f("fk_sales_channels_tenant_id_stock_pools"),
        ),
        sa.UniqueConstraint(
            "tenant_id", "channel_id", name=op.f("uq_sales_channels_tenant_id_channel_id")
        ),
        # 一个池只允许一个销售入口（契约第 3 节，C-07）。
        sa.UniqueConstraint(
            "tenant_id", "pool_id", name=op.f("uq_sales_channels_tenant_id_pool_id")
        ),
        _one_of("sales_channels", "status", ("active", "inactive")),
        **TABLE_OPTIONS,
    )
    op.create_table(
        "stock_balances",
        sa.Column("balance_id", ID, nullable=False),
        sa.Column("tenant_id", ID, nullable=False),
        sa.Column("pool_id", ID, nullable=False),
        sa.Column("sku_id", ID, nullable=False),
        sa.Column("stock_balance", sa.BigInteger(), server_default=ZERO, nullable=False),
        sa.Column("reserved", sa.BigInteger(), server_default=ZERO, nullable=False),
        sa.Column("unavailable", sa.BigInteger(), server_default=ZERO, nullable=False),
        sa.Column("paid_pending_dispatch", sa.BigInteger(), server_default=ZERO, nullable=False),
        sa.Column("balance_version", sa.BigInteger(), server_default=ZERO, nullable=False),
        sa.Column("created_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.Column("updated_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("balance_id", name=op.f("pk_stock_balances")),
        _tenant_fk("stock_balances"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "pool_id"],
            ["stock_pools.tenant_id", "stock_pools.pool_id"],
            name=op.f("fk_stock_balances_tenant_id_stock_pools"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "sku_id"],
            ["skus.tenant_id", "skus.sku_id"],
            name=op.f("fk_stock_balances_tenant_id_skus"),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "pool_id",
            "sku_id",
            name=op.f("uq_stock_balances_tenant_id_pool_id_sku_id"),
        ),
        *_quantity_invariants("stock_balances"),
        sa.CheckConstraint(
            "balance_version >= 0", name=op.f("ck_stock_balances_balance_version_nonnegative")
        ),
        **TABLE_OPTIONS,
    )
    op.create_table(
        "ledger_entries",
        sa.Column("entry_id", ID, nullable=False),
        sa.Column("tenant_id", ID, nullable=False),
        sa.Column("pool_id", ID, nullable=False),
        sa.Column("sku_id", ID, nullable=False),
        sa.Column("entry_type", CODE32, nullable=False),
        sa.Column("stock_balance_delta", sa.BigInteger(), nullable=False),
        sa.Column("reserved_delta", sa.BigInteger(), nullable=False),
        sa.Column("unavailable_delta", sa.BigInteger(), nullable=False),
        sa.Column("paid_pending_dispatch_delta", sa.BigInteger(), nullable=False),
        sa.Column("stock_balance_after", sa.BigInteger(), nullable=False),
        sa.Column("reserved_after", sa.BigInteger(), nullable=False),
        sa.Column("unavailable_after", sa.BigInteger(), nullable=False),
        sa.Column("paid_pending_dispatch_after", sa.BigInteger(), nullable=False),
        sa.Column("balance_version_after", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", ID, nullable=True),
        sa.Column("source_type", CODE32, nullable=True),
        sa.Column("source_ref", REF, nullable=True),
        sa.Column("line_ref", REF, nullable=True),
        sa.Column("command_id", ID, nullable=False),
        sa.Column("idempotency_key", REF, nullable=True),
        sa.Column("credential_id", ID, nullable=True),
        sa.Column("actor_ref", REF, nullable=True),
        sa.Column("actor_role", CODE32, nullable=True),
        sa.Column("reason_code", CODE32, nullable=True),
        sa.Column("note", sa.String(500), nullable=True),
        sa.Column("request_id", REF, nullable=True),
        sa.Column("corrects_entry_id", ID, nullable=True),
        sa.Column("occurred_at", TIMESTAMP, nullable=False),
        sa.Column("recorded_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("entry_id", name=op.f("pk_ledger_entries")),
        _tenant_fk("ledger_entries"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "pool_id", "sku_id"],
            ["stock_balances.tenant_id", "stock_balances.pool_id", "stock_balances.sku_id"],
            name=op.f("fk_ledger_entries_tenant_id_stock_balances"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "channel_id"],
            ["sales_channels.tenant_id", "sales_channels.channel_id"],
            name=op.f("fk_ledger_entries_tenant_id_sales_channels"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "corrects_entry_id"],
            ["ledger_entries.tenant_id", "ledger_entries.entry_id"],
            name=op.f("fk_ledger_entries_tenant_id_ledger_entries"),
        ),
        sa.UniqueConstraint(
            "tenant_id", "entry_id", name=op.f("uq_ledger_entries_tenant_id_entry_id")
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "pool_id",
            "sku_id",
            "balance_version_after",
            name=op.f("uq_ledger_entries_tenant_id_pool_id_sku_id_balance_version_after"),
        ),
        _one_of("ledger_entries", "entry_type", ENTRY_TYPES),
        *_quantity_invariants("ledger_entries", "_after"),
        sa.CheckConstraint(
            "balance_version_after >= 1",
            name=op.f("ck_ledger_entries_balance_version_after_positive"),
        ),
        sa.CheckConstraint(
            "(source_type IS NULL) = (source_ref IS NULL)",
            name=op.f("ck_ledger_entries_source_pair"),
        ),
        sa.CheckConstraint(
            "(entry_type = 'CORRECTION') = (corrects_entry_id IS NOT NULL)",
            name=op.f("ck_ledger_entries_correction_target"),
        ),
        sa.CheckConstraint(
            "corrects_entry_id <> entry_id", name=op.f("ck_ledger_entries_correction_not_self")
        ),
        **TABLE_OPTIONS,
    )
    op.create_index(
        op.f("ix_ledger_entries_command_id"), "ledger_entries", ["command_id"], unique=False
    )
    op.create_table(
        "idempotency_records",
        sa.Column("idempotency_record_id", ID, nullable=False),
        sa.Column("tenant_id", ID, nullable=False),
        sa.Column("credential_id", ID, nullable=False),
        sa.Column("operation", sa.String(64, collation="utf8mb4_bin"), nullable=False),
        sa.Column("idempotency_key", REF, nullable=False),
        sa.Column("request_hash", sa.CHAR(64, collation="utf8mb4_bin"), nullable=False),
        sa.Column("response_status", sa.SmallInteger(), nullable=False),
        sa.Column("response_body", sa.JSON(), nullable=False),
        sa.Column("request_id", REF, nullable=True),
        sa.Column("created_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("idempotency_record_id", name=op.f("pk_idempotency_records")),
        _tenant_fk("idempotency_records"),
        # 名字超过 64 字符，建表时按命名约定截断加摘要，与模型一致。
        sa.UniqueConstraint(
            "tenant_id",
            "credential_id",
            "operation",
            "idempotency_key",
            name=op.f("uq_idempotency_records_tenant_id_credential_id_operation_idempotency_key"),
        ),
        sa.CheckConstraint(
            "response_status BETWEEN 200 AND 499 AND response_status <> 429",
            name=op.f("ck_idempotency_records_response_status"),
        ),
        **TABLE_OPTIONS,
    )


def downgrade() -> None:
    op.drop_table("idempotency_records")
    op.drop_index(op.f("ix_ledger_entries_command_id"), table_name="ledger_entries")
    op.drop_table("ledger_entries")
    op.drop_table("stock_balances")
    op.drop_table("sales_channels")
    op.drop_table("stock_pools")
    op.drop_table("locations")
