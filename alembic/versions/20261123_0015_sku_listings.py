"""sku_listings: platform listings hang under SKUs; catalog changes may be about a listing

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-23

契约 2.9 的存放与 2.7 的 object_type（0.22，Q-90）。
建表 sku_listings；catalog_changes 的 object_type 检查约束加 listing。
MySQL 的检查约束不能同名并存，先删原约束，新约束沿用 ck_catalog_changes_object_type。
“同入口同 SKU 最多一条 active”与“channel_id 必须是 platform 入口”
不建数据库约束，由命令保证（契约 2.9）。
已有的变更记录都是 product 或 sku，新约束对它们照样成立，
所以不按 core_instance 区分实例。

降级只用于 CI 与开发库：库里有 listing 的变更记录时恢复不了原约束，
在改动任何东西之前失败。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 app/models/platform.py 相同的列约定（契约 2.2）。
ID = sa.String(32, collation="utf8mb4_bin")
STATUS = sa.String(16, collation="utf8mb4_bin")
REF = sa.String(128, collation="utf8mb4_bin")
CODE32 = sa.String(32, collation="utf8mb4_bin")
PLATFORM_ID = sa.String(64, collation="utf8mb4_bin")
TIMESTAMP = mysql.DATETIME(fsp=6)
NOW = sa.text("CURRENT_TIMESTAMP(6)")
TABLE_OPTIONS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_ai_ci"}

OBJECT_TYPE_CHECK = "ck_catalog_changes_object_type"
OLD_OBJECT_TYPES = "object_type IN ('product', 'sku')"
OBJECT_TYPES = "object_type IN ('product', 'sku', 'listing')"


def _platform_id(column: str) -> str:
    # 以排除字符集外的字符来写：ICU 的 $ 还会匹配末尾换行之前的位置。
    return (
        f"CHAR_LENGTH({column}) BETWEEN 1 AND 64"
        f" AND NOT REGEXP_LIKE({column}, '[^A-Za-z0-9._:-]', 'c')"
    )


def upgrade() -> None:
    op.create_table(
        "sku_listings",
        sa.Column("listing_id", ID, nullable=False),
        sa.Column("tenant_id", ID, nullable=False),
        sa.Column("channel_id", ID, nullable=False),
        sa.Column("platform_item_id", PLATFORM_ID, nullable=False),
        sa.Column("platform_model_id", PLATFORM_ID, nullable=False),
        sa.Column("sku_id", ID, nullable=False),
        sa.Column("status", STATUS, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("credential_id", ID, nullable=False),
        sa.Column("actor_ref", REF, nullable=True),
        sa.Column("actor_role", CODE32, nullable=True),
        sa.Column("request_id", REF, nullable=False),
        sa.Column("created_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.Column("updated_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("listing_id", name=op.f("pk_sku_listings")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "channel_id"],
            ["sales_channels.tenant_id", "sales_channels.channel_id"],
            name=op.f("fk_sku_listings_tenant_id_sales_channels"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "sku_id"],
            ["skus.tenant_id", "skus.sku_id"],
            name=op.f("fk_sku_listings_tenant_id_skus"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "credential_id"],
            ["credentials.tenant_id", "credentials.credential_id"],
            name=op.f("fk_sku_listings_tenant_id_credentials"),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "channel_id",
            "platform_item_id",
            "platform_model_id",
            name=op.f("uq_sku_listings_platform_ids"),
        ),
        sa.CheckConstraint(_platform_id("platform_item_id"), name=op.f("ck_sku_listings_item_id")),
        sa.CheckConstraint(
            _platform_id("platform_model_id"), name=op.f("ck_sku_listings_model_id")
        ),
        sa.CheckConstraint("status IN ('active', 'inactive')", name=op.f("ck_sku_listings_status")),
        sa.CheckConstraint("version >= 1", name=op.f("ck_sku_listings_version")),
        sa.CheckConstraint(
            "(actor_ref IS NULL) = (actor_role IS NULL)", name=op.f("ck_sku_listings_actor")
        ),
        **TABLE_OPTIONS,
    )
    op.create_index(
        op.f("ix_sku_listings_tenant_id_channel_id_sku_id_status"),
        "sku_listings",
        ["tenant_id", "channel_id", "sku_id", "status"],
        unique=False,
    )
    op.drop_constraint(op.f(OBJECT_TYPE_CHECK), "catalog_changes", type_="check")
    op.create_check_constraint(op.f(OBJECT_TYPE_CHECK), "catalog_changes", OBJECT_TYPES)


def downgrade() -> None:
    # MySQL 的 DDL 不在事务里：先查，免得删了表之后才在恢复原约束时失败。
    listings = op.get_bind().execute(
        sa.text("SELECT COUNT(*) FROM catalog_changes WHERE object_type = 'listing'")
    )
    if listings.scalar_one():
        raise RuntimeError("catalog_changes has listing records; cannot downgrade below 0015")
    # 普通索引可能支撑到 sales_channels 的复合外键，不单独删；删表时一并删掉。
    op.drop_table("sku_listings")
    op.drop_constraint(op.f(OBJECT_TYPE_CHECK), "catalog_changes", type_="check")
    op.create_check_constraint(op.f(OBJECT_TYPE_CHECK), "catalog_changes", OLD_OBJECT_TYPES)
