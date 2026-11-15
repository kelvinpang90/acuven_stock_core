"""stock management: stock_imports, stock_documents, stock_counts, stock_count_lines;
decision columns on stock_balances, go-live columns on stock_pools, one correction per entry

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 app/models/inventory.py 相同的列约定（契约 2.2）。
ID = sa.String(32, collation="utf8mb4_bin")
STATUS = sa.String(16, collation="utf8mb4_bin")
REF = sa.String(128, collation="utf8mb4_bin")
CODE32 = sa.String(32, collation="utf8mb4_bin")
HASH = sa.CHAR(64, collation="utf8mb4_bin")
TIMESTAMP = mysql.DATETIME(fsp=6)
NOW = sa.text("CURRENT_TIMESTAMP(6)")
ZERO = sa.text("0")
TABLE_OPTIONS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_ai_ci"}
QUANTITIES = ("stock_balance", "reserved", "unavailable", "paid_pending_dispatch")
WENT_LIVE_COLUMNS = (
    "went_live_at",
    "went_live_credential_id",
    "went_live_actor_ref",
    "went_live_request_id",
)
IMPORT_CONFIRMATION_COLUMNS = (
    "result",
    "confirmed_credential_id",
    "confirmed_actor_ref",
    "confirmed_request_id",
    "confirmed_at",
)
COUNT_CONFIRMATION_COLUMNS = (
    "confirmed_credential_id",
    "confirmed_actor_ref",
    "confirmed_request_id",
    "confirmed_at",
    "confirmed_reason_code",
)
COUNT_CANCELLATION_COLUMNS = (
    "cancelled_credential_id",
    "cancelled_actor_ref",
    "cancelled_request_id",
    "cancelled_at",
)
COUNT_RANGE = "BETWEEN 0 AND 1000000"


def _all_or_none(status: str, columns: Sequence[str]) -> str:
    return " AND ".join(f"(status = '{status}') = ({column} IS NOT NULL)" for column in columns)


def _tenant_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id"], ["tenants.tenant_id"], name=op.f(f"fk_{table}_tenant_id_tenants")
    )


def _pool_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", "pool_id"],
        ["stock_pools.tenant_id", "stock_pools.pool_id"],
        name=op.f(f"fk_{table}_tenant_id_stock_pools"),
    )


def _channel_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", "channel_id"],
        ["sales_channels.tenant_id", "sales_channels.channel_id"],
        name=op.f(f"fk_{table}_tenant_id_sales_channels"),
    )


def _credential_fk(table: str, column: str = "credential_id") -> sa.ForeignKeyConstraint:
    # 开单人、预览人等照命名约定；同表另一列指向凭据的与它同名，按列名显式命名。
    name = (
        f"fk_{table}_tenant_id_credentials"
        if column == "credential_id"
        else f"fk_{table}_{column}_credentials"
    )
    return sa.ForeignKeyConstraint(
        ["tenant_id", column],
        ["credentials.tenant_id", "credentials.credential_id"],
        name=op.f(name),
    )


def upgrade() -> None:
    # 余额行的判定列（契约 4.1）。还没有写流水的命令上线，现有行取默认值 0 与空即正确。
    op.add_column(
        "stock_balances",
        sa.Column("last_book_only_version", sa.BigInteger(), server_default=ZERO, nullable=False),
    )
    op.add_column(
        "stock_balances",
        sa.Column("last_reconciled_version", sa.BigInteger(), server_default=ZERO, nullable=False),
    )
    op.add_column("stock_balances", sa.Column("opening_entry_id", ID, nullable=True))
    op.create_check_constraint(
        op.f("ck_stock_balances_last_book_only_version_range"),
        "stock_balances",
        "last_book_only_version >= 0 AND last_book_only_version <= balance_version",
    )
    op.create_check_constraint(
        op.f("ck_stock_balances_last_reconciled_version_range"),
        "stock_balances",
        "last_reconciled_version >= 0 AND last_reconciled_version <= last_book_only_version",
    )

    # 池切 live 的记录（契约 6.3）。池切 live 还没有实现，库里没有 live 的池，
    # 两条检查约束对现有行都成立。
    op.add_column("stock_pools", sa.Column("went_live_at", TIMESTAMP, nullable=True))
    op.add_column("stock_pools", sa.Column("went_live_credential_id", ID, nullable=True))
    op.add_column("stock_pools", sa.Column("went_live_actor_ref", REF, nullable=True))
    op.add_column("stock_pools", sa.Column("went_live_request_id", REF, nullable=True))
    op.create_foreign_key(
        op.f("fk_stock_pools_tenant_id_credentials"),
        "stock_pools",
        "credentials",
        ["tenant_id", "went_live_credential_id"],
        ["tenant_id", "credential_id"],
    )
    op.create_check_constraint(
        op.f("ck_stock_pools_live_status"),
        "stock_pools",
        "(status = 'live') = (went_live_at IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_stock_pools_went_live_columns"),
        "stock_pools",
        " AND ".join(
            f"(went_live_at IS NULL) = ({column} IS NULL)" for column in WENT_LIVE_COLUMNS[1:]
        ),
    )

    # 一条流水最多被更正一次（契约 6.7）。InnoDB 改用这个唯一索引支撑 0002 的
    # (tenant_id, corrects_entry_id) 外键，删掉按外键名隐式建的索引。
    op.create_unique_constraint(
        op.f("uq_ledger_entries_tenant_id_corrects_entry_id"),
        "ledger_entries",
        ["tenant_id", "corrects_entry_id"],
    )

    op.create_table(
        "stock_imports",
        sa.Column("import_id", ID, nullable=False),
        sa.Column("tenant_id", ID, nullable=False),
        sa.Column("pool_id", ID, nullable=False),
        sa.Column("status", STATUS, nullable=False),
        sa.Column("expires_at", TIMESTAMP, nullable=False),
        sa.Column("rows", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("credential_id", ID, nullable=False),
        sa.Column("actor_ref", REF, nullable=False),
        sa.Column("request_id", REF, nullable=False),
        sa.Column("created_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.Column("confirmed_credential_id", ID, nullable=True),
        sa.Column("confirmed_actor_ref", REF, nullable=True),
        sa.Column("confirmed_request_id", REF, nullable=True),
        sa.Column("confirmed_at", TIMESTAMP, nullable=True),
        sa.PrimaryKeyConstraint("import_id", name=op.f("pk_stock_imports")),
        _tenant_fk("stock_imports"),
        _pool_fk("stock_imports"),
        _credential_fk("stock_imports"),
        _credential_fk("stock_imports", "confirmed_credential_id"),
        sa.UniqueConstraint(
            "tenant_id", "import_id", name=op.f("uq_stock_imports_tenant_id_import_id")
        ),
        sa.CheckConstraint(
            "status IN ('previewed', 'confirmed')", name=op.f("ck_stock_imports_status")
        ),
        # rows 是 MySQL 8 的保留字。
        sa.CheckConstraint("JSON_TYPE(`rows`) = 'ARRAY'", name=op.f("ck_stock_imports_rows")),
        sa.CheckConstraint(
            _all_or_none("confirmed", IMPORT_CONFIRMATION_COLUMNS),
            name=op.f("ck_stock_imports_confirmation"),
        ),
        **TABLE_OPTIONS,
    )
    op.create_table(
        "stock_documents",
        sa.Column("document_id", ID, nullable=False),
        sa.Column("tenant_id", ID, nullable=False),
        sa.Column("channel_id", ID, nullable=False),
        sa.Column("pool_id", ID, nullable=False),
        sa.Column("document_type", CODE32, nullable=False),
        sa.Column("document_ref", REF, nullable=False),
        sa.Column("request_hash", HASH, nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("credential_id", ID, nullable=False),
        sa.Column("actor_ref", REF, nullable=False),
        sa.Column("actor_role", CODE32, nullable=False),
        sa.Column("request_id", REF, nullable=False),
        sa.Column("created_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("document_id", name=op.f("pk_stock_documents")),
        _tenant_fk("stock_documents"),
        _channel_fk("stock_documents"),
        _pool_fk("stock_documents"),
        _credential_fk("stock_documents"),
        # 名字超过 64 字符，建表时按命名约定截断加摘要，与模型一致。
        sa.UniqueConstraint(
            "tenant_id",
            "channel_id",
            "document_type",
            "document_ref",
            name=op.f("uq_stock_documents_tenant_id_channel_id_document_type_document_ref"),
        ),
        sa.CheckConstraint(
            "document_type IN ('receipt', 'unavailable_adjustment', 'correction')",
            name=op.f("ck_stock_documents_document_type"),
        ),
        sa.CheckConstraint("JSON_TYPE(result) = 'OBJECT'", name=op.f("ck_stock_documents_result")),
        **TABLE_OPTIONS,
    )
    op.create_table(
        "stock_counts",
        sa.Column("count_id", ID, nullable=False),
        sa.Column("tenant_id", ID, nullable=False),
        sa.Column("channel_id", ID, nullable=False),
        sa.Column("pool_id", ID, nullable=False),
        sa.Column("count_ref", REF, nullable=False),
        sa.Column("status", STATUS, nullable=False),
        sa.Column("note", sa.String(500), nullable=True),
        sa.Column("request_hash", HASH, nullable=False),
        sa.Column("credential_id", ID, nullable=False),
        sa.Column("actor_ref", REF, nullable=False),
        sa.Column("request_id", REF, nullable=False),
        sa.Column("created_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.Column("confirmed_credential_id", ID, nullable=True),
        sa.Column("confirmed_actor_ref", REF, nullable=True),
        sa.Column("confirmed_request_id", REF, nullable=True),
        sa.Column("confirmed_at", TIMESTAMP, nullable=True),
        sa.Column("confirmed_reason_code", CODE32, nullable=True),
        sa.Column("confirm_note", sa.String(500), nullable=True),
        sa.Column("cancelled_credential_id", ID, nullable=True),
        sa.Column("cancelled_actor_ref", REF, nullable=True),
        sa.Column("cancelled_request_id", REF, nullable=True),
        sa.Column("cancelled_at", TIMESTAMP, nullable=True),
        sa.PrimaryKeyConstraint("count_id", name=op.f("pk_stock_counts")),
        _tenant_fk("stock_counts"),
        _channel_fk("stock_counts"),
        _pool_fk("stock_counts"),
        _credential_fk("stock_counts"),
        # 与上一条按命名约定会同名，模型里显式命名。
        _credential_fk("stock_counts", "confirmed_credential_id"),
        _credential_fk("stock_counts", "cancelled_credential_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "channel_id",
            "count_ref",
            name=op.f("uq_stock_counts_tenant_id_channel_id_count_ref"),
        ),
        sa.UniqueConstraint(
            "tenant_id", "count_id", name=op.f("uq_stock_counts_tenant_id_count_id")
        ),
        sa.CheckConstraint(
            "status IN ('open', 'confirmed', 'cancelled')", name=op.f("ck_stock_counts_status")
        ),
        sa.CheckConstraint(
            _all_or_none("confirmed", COUNT_CONFIRMATION_COLUMNS)
            + " AND (status = 'confirmed' OR confirm_note IS NULL)",
            name=op.f("ck_stock_counts_confirmation"),
        ),
        sa.CheckConstraint(
            _all_or_none("cancelled", COUNT_CANCELLATION_COLUMNS),
            name=op.f("ck_stock_counts_cancellation"),
        ),
        **TABLE_OPTIONS,
    )
    op.create_table(
        "stock_count_lines",
        sa.Column("count_id", ID, nullable=False),
        sa.Column("sku_id", ID, nullable=False),
        sa.Column("tenant_id", ID, nullable=False),
        sa.Column("pool_id", ID, nullable=False),
        sa.Column("shelf_count", sa.BigInteger(), nullable=False),
        sa.Column("unsellable_count", sa.BigInteger(), nullable=False),
        sa.Column("snapshot_stock_balance", sa.BigInteger(), nullable=False),
        sa.Column("snapshot_reserved", sa.BigInteger(), nullable=False),
        sa.Column("snapshot_unavailable", sa.BigInteger(), nullable=False),
        sa.Column("snapshot_paid_pending_dispatch", sa.BigInteger(), nullable=False),
        sa.Column("snapshot_balance_version", sa.BigInteger(), nullable=False),
        sa.Column("d_shelf", sa.BigInteger(), nullable=False),
        sa.Column("d_u", sa.BigInteger(), nullable=False),
        sa.Column("actor_ref", REF, nullable=False),
        sa.Column("request_id", REF, nullable=False),
        sa.Column("submitted_at", TIMESTAMP, nullable=False),
        sa.Column("entry_id", ID, nullable=True),
        sa.PrimaryKeyConstraint("count_id", "sku_id", name=op.f("pk_stock_count_lines")),
        _tenant_fk("stock_count_lines"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "count_id"],
            ["stock_counts.tenant_id", "stock_counts.count_id"],
            name=op.f("fk_stock_count_lines_tenant_id_stock_counts"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "pool_id", "sku_id"],
            ["stock_balances.tenant_id", "stock_balances.pool_id", "stock_balances.sku_id"],
            name=op.f("fk_stock_count_lines_tenant_id_stock_balances"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "entry_id"],
            ["ledger_entries.tenant_id", "ledger_entries.entry_id"],
            name=op.f("fk_stock_count_lines_tenant_id_ledger_entries"),
        ),
        sa.CheckConstraint(
            f"shelf_count {COUNT_RANGE}", name=op.f("ck_stock_count_lines_shelf_count")
        ),
        sa.CheckConstraint(
            f"unsellable_count {COUNT_RANGE}", name=op.f("ck_stock_count_lines_unsellable_count")
        ),
        *[
            sa.CheckConstraint(
                f"snapshot_{column} >= 0",
                name=op.f(f"ck_stock_count_lines_snapshot_{column}_nonnegative"),
            )
            for column in QUANTITIES
        ],
        # 名字超过 64 字符，建表时按命名约定截断加摘要，与模型一致。
        sa.CheckConstraint(
            "snapshot_reserved + snapshot_unavailable <= snapshot_stock_balance",
            name=op.f("ck_stock_count_lines_snapshot_reserved_unavailable_within_balance"),
        ),
        sa.CheckConstraint(
            "snapshot_balance_version >= 0",
            name=op.f("ck_stock_count_lines_snapshot_balance_version_nonnegative"),
        ),
        sa.CheckConstraint(
            "d_shelf = shelf_count - (snapshot_stock_balance - snapshot_unavailable"
            " + snapshot_paid_pending_dispatch)",
            name=op.f("ck_stock_count_lines_d_shelf"),
        ),
        sa.CheckConstraint(
            "d_u = unsellable_count - snapshot_unavailable",
            name=op.f("ck_stock_count_lines_d_u"),
        ),
        **TABLE_OPTIONS,
    )


def downgrade() -> None:
    op.drop_table("stock_count_lines")
    op.drop_table("stock_counts")
    op.drop_table("stock_documents")
    op.drop_table("stock_imports")

    # 唯一约束这时支撑着 0002 的 (tenant_id, corrects_entry_id) 外键，直接删会报 1553。
    # 先删外键、再删唯一约束，然后重建外键：MySQL 按外键名隐式建回索引，回到 0004 的结构。
    op.drop_constraint(
        op.f("fk_ledger_entries_tenant_id_ledger_entries"), "ledger_entries", type_="foreignkey"
    )
    op.drop_constraint(
        op.f("uq_ledger_entries_tenant_id_corrects_entry_id"), "ledger_entries", type_="unique"
    )
    op.create_foreign_key(
        op.f("fk_ledger_entries_tenant_id_ledger_entries"),
        "ledger_entries",
        "ledger_entries",
        ["tenant_id", "corrects_entry_id"],
        ["tenant_id", "entry_id"],
    )

    # 检查约束引用的列删不掉，先删约束。
    op.drop_constraint(op.f("ck_stock_pools_went_live_columns"), "stock_pools", type_="check")
    op.drop_constraint(op.f("ck_stock_pools_live_status"), "stock_pools", type_="check")
    op.drop_constraint(
        op.f("fk_stock_pools_tenant_id_credentials"), "stock_pools", type_="foreignkey"
    )
    # 加外键时 MySQL 按外键名隐式建了索引；删外键不会删它，这里删掉（同 0003 的降级）。
    op.drop_index(op.f("fk_stock_pools_tenant_id_credentials"), table_name="stock_pools")
    for column in reversed(WENT_LIVE_COLUMNS):
        op.drop_column("stock_pools", column)

    op.drop_constraint(
        op.f("ck_stock_balances_last_reconciled_version_range"), "stock_balances", type_="check"
    )
    op.drop_constraint(
        op.f("ck_stock_balances_last_book_only_version_range"), "stock_balances", type_="check"
    )
    op.drop_column("stock_balances", "opening_entry_id")
    op.drop_column("stock_balances", "last_reconciled_version")
    op.drop_column("stock_balances", "last_book_only_version")
