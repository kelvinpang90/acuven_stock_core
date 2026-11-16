"""demo resets: demo_resets, the core_instance marker, and no DEMO_RESET entries on production

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-16

标记迁移（契约 6.1）：按执行迁移的进程的 CORE_ENV 写入 core_instance。
从这一条起迁移要求 CORE_ENV。降级要读库里的标记，只能在线执行，不支持 --sql。
"""

from __future__ import annotations

import os
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op
from app.domain.environment import PRODUCTION, parse_core_env

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 app/models/inventory.py、app/models/instance.py 相同的列约定（契约 2.2）。
ID = sa.String(32, collation="utf8mb4_bin")
STATUS = sa.String(16, collation="utf8mb4_bin")
REF = sa.String(128, collation="utf8mb4_bin")
CODE32 = sa.String(32, collation="utf8mb4_bin")
HASH = sa.CHAR(64, collation="utf8mb4_bin")
TIMESTAMP = mysql.DATETIME(fsp=6)
NOW = sa.text("CURRENT_TIMESTAMP(6)")
TABLE_OPTIONS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_ai_ci"}

# 0002 建的流水类型约束的列表，抄在这里而不 import 0002：迁移写成之后不随别的文件变。
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
PRODUCTION_ENTRY_TYPES = tuple(value for value in ENTRY_TYPES if value != "DEMO_RESET")
ENTRY_TYPE_CHECK = "ck_ledger_entries_entry_type"


def _replace_entry_type_check(values: Sequence[str]) -> None:
    allowed = ", ".join(f"'{value}'" for value in values)
    op.drop_constraint(op.f(ENTRY_TYPE_CHECK), "ledger_entries", type_="check")
    op.create_check_constraint(
        op.f(ENTRY_TYPE_CHECK), "ledger_entries", f"entry_type IN ({allowed})"
    )


def upgrade() -> None:
    # 契约 6.1 的顺序。MySQL 的 DDL 会隐式提交，所以先校验、不改库；
    # 标记最后写，“有这一行”就说明流水类型约束已经按环境处理过。
    core_env = parse_core_env(os.environ.get("CORE_ENV", ""))

    if core_env == PRODUCTION:
        # 生产库在数据库约束层面拒绝 DEMO_RESET（契约 6.1 生产实例的防护）。
        _replace_entry_type_check(PRODUCTION_ENTRY_TYPES)

    op.create_table(
        "demo_resets",
        sa.Column("reset_id", ID, nullable=False),
        sa.Column("tenant_id", ID, nullable=False),
        sa.Column("channel_id", ID, nullable=False),
        sa.Column("pool_id", ID, nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("request_hash", HASH, nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("credential_id", ID, nullable=False),
        sa.Column("actor_ref", REF, nullable=True),
        sa.Column("actor_role", CODE32, nullable=True),
        sa.Column("request_id", REF, nullable=False),
        sa.Column("created_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("reset_id", name=op.f("pk_demo_resets")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.tenant_id"], name=op.f("fk_demo_resets_tenant_id_tenants")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "channel_id"],
            ["sales_channels.tenant_id", "sales_channels.channel_id"],
            name=op.f("fk_demo_resets_tenant_id_sales_channels"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "pool_id"],
            ["stock_pools.tenant_id", "stock_pools.pool_id"],
            name=op.f("fk_demo_resets_tenant_id_stock_pools"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "credential_id"],
            ["credentials.tenant_id", "credentials.credential_id"],
            name=op.f("fk_demo_resets_tenant_id_credentials"),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "pool_id",
            "business_date",
            name=op.f("uq_demo_resets_tenant_id_pool_id_business_date"),
        ),
        sa.CheckConstraint("JSON_TYPE(result) = 'OBJECT'", name=op.f("ck_demo_resets_result")),
        sa.CheckConstraint(
            "(actor_ref IS NULL) = (actor_role IS NULL)", name=op.f("ck_demo_resets_actor")
        ),
        **TABLE_OPTIONS,
    )

    core_instance = op.create_table(
        "core_instance",
        # MySQL 8 不允许检查约束引用自增列，关掉整数主键默认的 AUTO_INCREMENT。
        sa.Column("singleton", mysql.TINYINT(), autoincrement=False, nullable=False),
        sa.Column("core_env", STATUS, nullable=False),
        sa.Column("created_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("singleton", name=op.f("pk_core_instance")),
        sa.CheckConstraint("singleton = 1", name=op.f("ck_core_instance_singleton")),
        sa.CheckConstraint(
            "core_env IN ('demo', 'production')", name=op.f("ck_core_instance_core_env")
        ),
        **TABLE_OPTIONS,
    )
    op.bulk_insert(core_instance, [{"singleton": 1, "core_env": core_env}])


def downgrade() -> None:
    # 只用于 CI 与开发库（契约 6.1）。按库里的标记而不是 CORE_ENV 决定是否恢复约束；
    # 两者一致已由迁移前核对保证。
    marker = op.get_bind().execute(sa.text("SELECT core_env FROM core_instance")).scalar_one()

    op.drop_table("core_instance")
    op.drop_table("demo_resets")

    if marker == PRODUCTION:
        _replace_entry_type_check(ENTRY_TYPES)
