"""sales_channel_kind: channels become api, platform or backoffice; a pool may have several

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-23

契约第 3 节“入口的种类与组合”（0.22，Q-81）。
sales_channels 加列 kind，默认 api，已有的行都是 api；加列 platform 与 platform_shop_id，
只有 platform 入口有；(tenant_id, platform, platform_shop_id) 唯一。
去掉唯一约束 (tenant_id, pool_id)：它同时是到 stock_pools 的复合外键要用的索引，
MySQL 不允许删掉外键唯一可用的索引，所以先建同列的普通索引、再删唯一约束。
一个池的入口组合由开通入口的命令行在锁住池行之后检查，数据库层不兜底。
已有的行都是 api、两列为空，新约束对它们照样成立，所以不按 core_instance 区分实例。

降级只用于 CI 与开发库：一个池有多个入口时恢复不了原唯一约束，在改动任何东西之前失败。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.domain.channels import API, CHANNEL_KINDS, PLATFORM, PLATFORMS

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 app/models/inventory.py 相同的列约定（契约 2.2）。
STATUS = sa.String(16, collation="utf8mb4_bin")
CODE32 = sa.String(32, collation="utf8mb4_bin")
SHOP_ID = sa.String(64, collation="utf8mb4_bin")

POOL_INDEX = "ix_sales_channels_tenant_id_pool_id"
POOL_UNIQUE = "uq_sales_channels_tenant_id_pool_id"
SHOP_UNIQUE = "uq_sales_channels_tenant_id_platform_platform_shop_id"


def _in(column: str, values: Sequence[str]) -> str:
    allowed = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({allowed})"


# 名字互不为前缀：测试按约束名的子串认定是哪一条拒绝的。
# kind 为未知值、两列为空时只由 kind 那条拒绝；两列为空时后两条不拦。
CHECKS = {
    "ck_sales_channels_kind": _in("kind", CHANNEL_KINDS),
    "ck_sales_channels_platform_pair": (
        f"(kind = '{PLATFORM}') = (platform IS NOT NULL)"
        f" AND (kind = '{PLATFORM}') = (platform_shop_id IS NOT NULL)"
    ),
    "ck_sales_channels_platform_value": _in("platform", PLATFORMS),
    # 以排除字符集外的字符来写：ICU 的 $ 还会匹配末尾换行之前的位置。
    "ck_sales_channels_shop_id": (
        "CHAR_LENGTH(platform_shop_id) BETWEEN 1 AND 64"
        " AND NOT REGEXP_LIKE(platform_shop_id, '[^A-Za-z0-9._:-]', 'c')"
    ),
}


def upgrade() -> None:
    op.add_column("sales_channels", sa.Column("kind", STATUS, server_default=API, nullable=False))
    op.add_column("sales_channels", sa.Column("platform", CODE32, nullable=True))
    op.add_column("sales_channels", sa.Column("platform_shop_id", SHOP_ID, nullable=True))
    for name, condition in CHECKS.items():
        op.create_check_constraint(op.f(name), "sales_channels", condition)
    # 先建普通索引给复合外键用，再删唯一约束。
    op.create_index(op.f(POOL_INDEX), "sales_channels", ["tenant_id", "pool_id"], unique=False)
    op.drop_constraint(op.f(POOL_UNIQUE), "sales_channels", type_="unique")
    op.create_unique_constraint(
        op.f(SHOP_UNIQUE), "sales_channels", ["tenant_id", "platform", "platform_shop_id"]
    )


def downgrade() -> None:
    # MySQL 的 DDL 不在事务里：先查，免得改到一半才在恢复原唯一约束时失败、库停在半路。
    shared = op.get_bind().execute(
        sa.text(
            "SELECT COUNT(*) FROM ("
            "SELECT 1 FROM sales_channels GROUP BY tenant_id, pool_id HAVING COUNT(*) > 1"
            ") AS shared_pools"
        )
    )
    if shared.scalar_one():
        raise RuntimeError("a pool has more than one sales channel; cannot downgrade below 0013")
    op.drop_constraint(op.f(SHOP_UNIQUE), "sales_channels", type_="unique")
    # 先恢复唯一约束（外键改用它），再删普通索引。
    op.create_unique_constraint(op.f(POOL_UNIQUE), "sales_channels", ["tenant_id", "pool_id"])
    op.drop_index(op.f(POOL_INDEX), table_name="sales_channels")
    for name in reversed(CHECKS):
        op.drop_constraint(op.f(name), "sales_channels", type_="check")
    op.drop_column("sales_channels", "platform_shop_id")
    op.drop_column("sales_channels", "platform")
    op.drop_column("sales_channels", "kind")
