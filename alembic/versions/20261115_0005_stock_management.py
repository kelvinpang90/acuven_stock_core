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


def _one_of(table: str, column: str, values: Sequence[str]) -> sa.CheckConstraint:
    allowed = ", ".join(f"'{value}'" for value in values)
    return sa.CheckConstraint(f"{column} IN ({allowed})", name=op.f(f"ck_{table}_{column}"))


def _snapshot_invariants(table: str) -> list[sa.CheckConstraint]:
    # 名字超过 64 字符的，建表时按命名约定截断加摘要，与模型一致。
    checks = [
        sa.CheckConstraint(
            f"snapshot_{column} >= 0", name=op.f(f"ck_{table}_snapshot_{column}_nonnegative")
        )
        for column in QUANTITIES
    ]
    checks.append(
        sa.CheckConstraint(
            "snapshot_reserved + snapshot_unavailable <= snapshot_stock_balance",
            name=op.f(f"ck_{table}_snapshot_reserved_unavailable_within_balance"),
        )
    )
    return checks


def _tenant_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id"], ["tenants.tenant_id"], name=op.f(f"fk_{table}_tenant_id_tenants")
    )


def _composite_fk(
    table: str, columns: list[str], referred: str, referred_columns: list[str], name: str = ""
) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", *columns],
        [f"{referred}.tenant_id", *(f"{referred}.{column}" for column in referred_columns)],
        name=op.f(name or f"fk_{table}_tenant_id_{referred}"),
    )


def _credential_fk(table: str, column: str = "credential_id") -> sa.ForeignKeyConstraint:
    # 一张表有多个指向 credentials 的外键时，按命名约定会同名，第二个起按列名显式命名。
    name = "" if column == "credential_id" else f"fk_{table}_{column}_credentials"
    return _composite_fk(table, [column], "credentials", ["credential_id"], name)


def upgrade() -> None:
    # 判定列（契约 4.1，0.9、0.10）。没有写流水的代码，已有的行取默认 0、0 与 NULL 即满足检查。
    op.add_column(
        "stock_balances",
        sa.Column("last_book_only_version", sa.BigInteger(), server_default=ZERO, nullable=False),
    )
    op.add_column(
        "stock_balances",
        sa.Column("last_reconciled_version", sa.BigInteger(), server_default=ZERO, nullable=False),
    )
    # 不加外键：流水表已有外键指向余额行，反向引用会成环。
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

    # 池切 live 的记录（契约 6.3）。还没有切 live 的代码，已有的池都是 initializing、四列为空。
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
        op.f("ck_stock_pools_went_live_columns"),
        "stock_pools",
        " AND ".join(
            f"(went_live_at IS NULL) = ({column} IS NULL)" for column in WENT_LIVE_COLUMNS[1:]
        ),
    )
    op.create_check_constraint(
        op.f("ck_stock_pools_went_live_status"),
        "stock_pools",
        "(status = 'live') = (went_live_at IS NOT NULL)",
    )

    # 一条流水最多被更正一次（契约 6.7）。InnoDB 之后改用这个唯一索引支撑
    # 外键 (tenant_id, corrects_entry_id)，删掉 0002 时按外键名隐式建的索引。
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
        _composite_fk("stock_imports", ["pool_id"], "stock_pools", ["pool_id"]),
        _credential_fk("stock_imports"),
        _credential_fk("stock_imports", "confirmed_credential_id"),
        sa.UniqueConstraint(
            "tenant_id", "import_id", name=op.f("uq_stock_imports_tenant_id_import_id")
        ),
        _one_of("stock_imports", "status", ("previewed", "confirmed")),
        # rows 是 MySQL 8 的保留字。
        sa.CheckConstraint("JSON_TYPE(`rows`) = 'ARRAY'", name=op.f("ck_stock_imports_rows")),
        sa.CheckConstraint(
            " AND ".join(
                f"(status = 'confirmed') = ({column} IS NOT NULL)"
                for column in IMPORT_CONFIRMATION_COLUMNS
            ),
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
        _composite_fk("stock_documents", ["channel_id"], "sales_channels", ["channel_id"]),
        _composite_fk("stock_documents", ["pool_id"], "stock_pools", ["pool_id"]),
        _credential_fk("stock_documents"),
        # 名字超过 64 字符，建表时按命名约定截断加摘要，与模型一致。
        sa.UniqueConstraint(
            "tenant_id",
            "channel_id",
            "document_type",
            "document_ref",
            name=op.f("uq_stock_documents_tenant_id_channel_id_document_type_document_ref"),
        ),
        _one_of(
            "stock_documents",
            "document_type",
            ("receipt", "unavailable_adjustment", "correction"),
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
        _composite_fk("stock_counts", ["channel_id"], "sales_channels", ["channel_id"]),
        _composite_fk("stock_counts", ["pool_id"], "stock_pools", ["pool_id"]),
        _credential_fk("stock_counts"),
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
        _one_of("stock_counts", "status", ("open", "confirmed", "cancelled")),
        sa.CheckConstraint(
            " AND ".join(
                f"(status = 'confirmed') = ({column} IS NOT NULL)"
                for column in COUNT_CONFIRMATION_COLUMNS
            )
            + " AND (status = 'confirmed' OR confirm_note IS NULL)",
            name=op.f("ck_stock_counts_confirmation"),
        ),
        sa.CheckConstraint(
            " AND ".join(
                f"(status = 'cancelled') = ({column} IS NOT NULL)"
                for column in COUNT_CANCELLATION_COLUMNS
            ),
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
        _composite_fk("stock_count_lines", ["count_id"], "stock_counts", ["count_id"]),
        _composite_fk(
            "stock_count_lines", ["pool_id", "sku_id"], "stock_balances", ["pool_id", "sku_id"]
        ),
        _composite_fk("stock_count_lines", ["entry_id"], "ledger_entries", ["entry_id"]),
        sa.CheckConstraint(
            "shelf_count BETWEEN 0 AND 1000000",
            name=op.f("ck_stock_count_lines_shelf_count_range"),
        ),
        sa.CheckConstraint(
            "unsellable_count BETWEEN 0 AND 1000000",
            name=op.f("ck_stock_count_lines_unsellable_count_range"),
        ),
        *_snapshot_invariants("stock_count_lines"),
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

    # 加唯一约束后 InnoDB 用它支撑外键 (tenant_id, corrects_entry_id)，直接删会报 1553。
    # 先删外键、再删唯一约束，然后重建外键：MySQL 按外键名隐式建索引，回到 0004 的结构
    # （0003 的降级注释讲过同一现象）。
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

    # 检查约束引用多列，MySQL 不许删其中的列，先删约束。
    op.drop_constraint(op.f("ck_stock_pools_went_live_status"), "stock_pools", type_="check")
    op.drop_constraint(op.f("ck_stock_pools_went_live_columns"), "stock_pools", type_="check")
    op.drop_constraint(
        op.f("fk_stock_pools_tenant_id_credentials"), "stock_pools", type_="foreignkey"
    )
    # stock_pools 没有以 (tenant_id, went_live_credential_id) 开头的索引，加外键时
    # MySQL 按外键名隐式建了一个；删外键不会删它，这里删掉。
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
