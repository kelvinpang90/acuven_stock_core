"""平台渠道的配额表：池与入口两级配额，以及只追加的配额变更（契约 3.1 的存放，
3.2 中落在 channel_allocations 上的读回待核对与人工生效两组列；租户隔离见第 10 节第 1 条）。

只有表结构与数据库约束。F ≥ 0 不是数据库约束（契约 3.1）；channel_allocations 的 pool_id
与入口绑定的池相同、入口是 platform 种类，都由服务层保证。行由服务在业务事务之前预建，
取默认值，所以行还不存在时 expected_version 为 0 与预建出的行一致。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.catalog import ID, NOW, TABLE_OPTIONS, TIMESTAMP
from app.models.inventory import CODE32, REF, ZERO

# 安全库存、配额、已售未扣与人工确认的显示量的范围（契约 3.1）。
ALLOCATION_RANGE = "BETWEEN 0 AND 1000000"
DEFAULT_SAFETY_STOCK = text("2")

# review_hold 为真时才有、且必有的列（契约 3.2 的设上与解除）。
REVIEW_COLUMNS = ("review_reason", "review_since")


class PoolAllocation(Base):
    """池 × SKU 一行：安全库存 S 与管理员改配额用的版本（契约 3.1）。

    version 只在管理员改配额或 S 时加一，平台事件改 Q 不加。
    """

    __tablename__ = "pool_allocations"
    __table_args__ = (
        # 先有余额行。
        ForeignKeyConstraint(
            ["tenant_id", "pool_id", "sku_id"],
            ["stock_balances.tenant_id", "stock_balances.pool_id", "stock_balances.sku_id"],
        ),
        CheckConstraint(f"safety_stock {ALLOCATION_RANGE}", name="safety_stock"),
        CheckConstraint("version >= 0", name="version"),
        TABLE_OPTIONS,
    )

    tenant_id: Mapped[str] = mapped_column(ID, ForeignKey("tenants.tenant_id"), primary_key=True)
    pool_id: Mapped[str] = mapped_column(ID, primary_key=True)
    sku_id: Mapped[str] = mapped_column(ID, primary_key=True)
    safety_stock: Mapped[int] = mapped_column(Integer, server_default=DEFAULT_SAFETY_STOCK)
    version: Mapped[int] = mapped_column(Integer, server_default=ZERO)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)


class ChannelAllocation(Base):
    """平台入口 × SKU 一行：剩余配额 Q 与已售未扣 pending（契约 3.1），
    以及读回待核对与人工生效两组列（契约 3.2）。
    """

    __tablename__ = "channel_allocations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "channel_id"], ["sales_channels.tenant_id", "sales_channels.channel_id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "pool_id", "sku_id"],
            ["stock_balances.tenant_id", "stock_balances.pool_id", "stock_balances.sku_id"],
        ),
        # 按池与 SKU 取各入口的行；也是上一条复合外键要用的索引。
        Index("ix_channel_allocations_tenant_id_pool_id_sku_id", "tenant_id", "pool_id", "sku_id"),
        # 约束名互不为前缀：测试按约束名的子串认定是哪一条拒绝的。
        CheckConstraint(f"quota {ALLOCATION_RANGE}", name="quota_range"),
        CheckConstraint(f"pending {ALLOCATION_RANGE}", name="pending_range"),
        CheckConstraint(f"manual_confirmed_quota {ALLOCATION_RANGE}", name="manual_quota_range"),
        CheckConstraint(
            " AND ".join(
                f"(review_hold <> 0) = ({column} IS NOT NULL)" for column in REVIEW_COLUMNS
            ),
            name="review_columns",
        ),
        CheckConstraint(
            "(manual_confirmed_quota IS NULL) = (manual_confirmed_at IS NULL)",
            name="manual_columns",
        ),
        TABLE_OPTIONS,
    )

    tenant_id: Mapped[str] = mapped_column(ID, ForeignKey("tenants.tenant_id"), primary_key=True)
    channel_id: Mapped[str] = mapped_column(ID, primary_key=True)
    pool_id: Mapped[str] = mapped_column(ID)
    sku_id: Mapped[str] = mapped_column(ID, primary_key=True)
    quota: Mapped[int] = mapped_column(Integer, server_default=ZERO)
    # 累积超过上限时那个新订单事件整笔回滚、留在收件箱（契约 3.1）。
    pending: Mapped[int] = mapped_column(Integer, server_default=ZERO)
    review_hold: Mapped[bool] = mapped_column(Boolean, server_default=ZERO)
    review_reason: Mapped[str | None] = mapped_column(String(255))
    review_since: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    manual_confirmed_quota: Mapped[int | None] = mapped_column(Integer)
    manual_confirmed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)


class AllocationChange(Base):
    """管理员改配额或 S 一次一行，只追加（契约 3.1）。

    随订单事件发生的 Q 与 pending 变化不记在这里。
    """

    __tablename__ = "allocation_changes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "pool_id", "sku_id"],
            ["pool_allocations.tenant_id", "pool_allocations.pool_id", "pool_allocations.sku_id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "credential_id"], ["credentials.tenant_id", "credentials.credential_id"]
        ),
        # 按池与 SKU 查改动历史；也是指向 pool_allocations 的外键要用的索引。
        Index(
            "ix_allocation_changes_tenant_id_pool_id_sku_id_id",
            "tenant_id",
            "pool_id",
            "sku_id",
            "id",
        ),
        CheckConstraint("JSON_TYPE(changes) = 'OBJECT'", name="changes"),
        CheckConstraint("CHAR_LENGTH(reason) > 0", name="reason"),
        CheckConstraint("(actor_ref IS NULL) = (actor_role IS NULL)", name="actor"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(ID, ForeignKey("tenants.tenant_id"))
    pool_id: Mapped[str] = mapped_column(ID)
    sku_id: Mapped[str] = mapped_column(ID)
    # S 与各入口 Q 的前后值。
    changes: Mapped[dict[str, Any]] = mapped_column(JSON)
    reason: Mapped[str] = mapped_column(String(500))
    credential_id: Mapped[str] = mapped_column(ID)
    actor_ref: Mapped[str | None] = mapped_column(REF)
    actor_role: Mapped[str | None] = mapped_column(CODE32)
    request_id: Mapped[str] = mapped_column(REF)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)
