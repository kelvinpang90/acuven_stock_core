"""平台渠道的表：池与入口两级配额，只追加的配额变更（契约 3.1 的存放，
3.2 中落在 channel_allocations 上的读回待核对与人工生效两组列），
平台商品（契约 2.9 的存放），以及平台订单、平台订单行与收件箱（契约 5.10 的存放，
3.2 中落在 platform_order_lines 上的已知差异）；租户隔离见第 10 节第 1 条。

只有表结构与数据库约束。F ≥ 0 不是数据库约束（契约 3.1）；channel_allocations 的 pool_id
与入口绑定的池相同、入口是 platform 种类，都由服务层保证。行由服务在业务事务之前预建，
取默认值，所以行还不存在时 expected_version 为 0 与预建出的行一致。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CHAR,
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.catalog import EXACT_COLLATION, ID, NOW, STATUS, TABLE_OPTIONS, TIMESTAMP
from app.models.inventory import CODE32, REF, ZERO

# 安全库存、配额、已售未扣与人工确认的显示量的范围（契约 3.1）。
ALLOCATION_RANGE = "BETWEEN 0 AND 1000000"
DEFAULT_SAFETY_STOCK = text("2")

# review_hold 为真时才有、且必有的列（契约 3.2 的设上与解除）。
REVIEW_COLUMNS = ("review_reason", "review_since")

# 平台商品 ID 与规格 ID 区分大小写、不忽略重音：
# 表的默认排序规则会把只差大小写的 ID 当成同一个。
MAX_PLATFORM_ID_LENGTH = 64
PLATFORM_ID = String(MAX_PLATFORM_ID_LENGTH, collation=EXACT_COLLATION)


# 平台订单、订单行与事件上平台给的 ID 与键，以及由它们派生的 order_ref：照平台给的原样存，
# 不做字符集检查，同样区分大小写、不忽略重音。
PLATFORM_KEY = String(128, collation=EXACT_COLLATION)
# 给 payment_ref 等引用的后缀留位（契约 5.10 的引用格式）。
MAX_ORDER_REF_LENGTH = 112
LINE_REF = String(32, collation=EXACT_COLLATION)
CODE64 = String(64, collation=EXACT_COLLATION)
QUANTITY_RANGE = "BETWEEN 1 AND 1000000"

UNPAID = "unpaid"
STAGES = (UNPAID, "held", "committed", "cancelled")
# 按契约 5.10 状态链的先后。
EVENT_ACTIONS = (
    "created",
    "ready_to_ship",
    "shipped",
    "cancelled",
    "return_requested",
    "return_received",
)
EVENT_SOURCES = ("webhook", "pull", "manual")
EVENT_RESULTS = ("applied", "ignored", "held", "attention")
# 只有 manual 来源有、且必有的列（契约 5.10）。
MANUAL_COLUMNS = ("manual_key", "request_hash")


def _one_of(column: str, values: tuple[str, ...], name: str | None = None) -> CheckConstraint:
    allowed = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(f"{column} IN ({allowed})", name=name or column)


def _not_empty(column: str) -> CheckConstraint:
    # 可空的列为 NULL 时检查结果为 NULL、不拦；有值时不能是空串。
    return CheckConstraint(f"CHAR_LENGTH({column}) > 0", name=f"{column}_not_empty")


def _platform_id(column: str, name: str) -> CheckConstraint:
    # 1–64 个 [A-Za-z0-9._:-] 字符（契约 2.9）。以排除字符集外的字符来写：
    # ICU 的 $ 还会匹配末尾换行之前的位置。
    return CheckConstraint(
        f"CHAR_LENGTH({column}) BETWEEN 1 AND {MAX_PLATFORM_ID_LENGTH}"
        f" AND NOT REGEXP_LIKE({column}, '[^A-Za-z0-9._:-]', 'c')",
        name=name,
    )


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


class SkuListing(Base):
    """平台商品：一个平台规格一行，挂在某个 SKU 下（契约 2.9）。

    改挂与解除都改这一行、版本加一，历史在目录变更记录里。同一入口上一个 SKU 最多一条
    active、channel_id 是 platform 入口，都由命令在目录写锁内保证，不是数据库约束。
    """

    __tablename__ = "sku_listings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "channel_id"], ["sales_channels.tenant_id", "sales_channels.channel_id"]
        ),
        ForeignKeyConstraint(["tenant_id", "sku_id"], ["skus.tenant_id", "skus.sku_id"]),
        ForeignKeyConstraint(
            ["tenant_id", "credential_id"], ["credentials.tenant_id", "credentials.credential_id"]
        ),
        # 按列名的约定名超过 MySQL 的 64 个字符，这里显式命名。
        UniqueConstraint(
            "tenant_id",
            "channel_id",
            "platform_item_id",
            "platform_model_id",
            name="uq_sku_listings_platform_ids",
        ),
        # 按“入口 × SKU”找生效的平台商品。
        Index(
            "ix_sku_listings_tenant_id_channel_id_sku_id_status",
            "tenant_id",
            "channel_id",
            "sku_id",
            "status",
        ),
        # 约束名互不为前缀：测试按约束名的子串认定是哪一条拒绝的。
        _platform_id("platform_item_id", "item_id"),
        _platform_id("platform_model_id", "model_id"),
        CheckConstraint("status IN ('active', 'inactive')", name="status"),
        CheckConstraint("version >= 1", name="version"),
        CheckConstraint("(actor_ref IS NULL) = (actor_role IS NULL)", name="actor"),
        TABLE_OPTIONS,
    )

    listing_id: Mapped[str] = mapped_column(ID, primary_key=True)
    # 不另设到 tenants 的外键：几条复合外键都指向同租户的行（同 skus）。
    tenant_id: Mapped[str] = mapped_column(ID)
    channel_id: Mapped[str] = mapped_column(ID)
    platform_item_id: Mapped[str] = mapped_column(PLATFORM_ID)
    # 平台商品没有规格时存 "-"。
    platform_model_id: Mapped[str] = mapped_column(PLATFORM_ID)
    sku_id: Mapped[str] = mapped_column(ID)
    status: Mapped[str] = mapped_column(STATUS)
    version: Mapped[int] = mapped_column(Integer)
    # 最后一次改动的操作主体（契约第 10 节第 4 条）。
    credential_id: Mapped[str] = mapped_column(ID)
    actor_ref: Mapped[str | None] = mapped_column(REF)
    actor_role: Mapped[str | None] = mapped_column(CODE32)
    request_id: Mapped[str] = mapped_column(REF)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)


class PlatformOrder(Base):
    """平台订单一行（契约 5.10）。

    order_ref 超长或含不合格字符时怎么派生由服务层做；channel_id 是 platform 入口由服务层保证。
    """

    __tablename__ = "platform_orders"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "channel_id"], ["sales_channels.tenant_id", "sales_channels.channel_id"]
        ),
        # 复合外键 (tenant_id, platform_order_row_id) 的引用目标。
        UniqueConstraint("tenant_id", "platform_order_row_id"),
        UniqueConstraint("tenant_id", "channel_id", "platform_order_id"),
        UniqueConstraint("tenant_id", "channel_id", "order_ref"),
        # 约束名互不为子串：测试按约束名的子串认定是哪一条拒绝的。
        _not_empty("platform_order_id"),
        CheckConstraint(
            f"CHAR_LENGTH(order_ref) BETWEEN 1 AND {MAX_ORDER_REF_LENGTH}", name="order_ref_length"
        ),
        _one_of("stage", STAGES),
        _not_empty("attention"),
        _not_empty("attention_code"),
        CheckConstraint("stage <> 'held' OR attention IS NOT NULL", name="held_attention"),
        CheckConstraint(
            "attention_code IS NULL OR attention IS NOT NULL", name="attention_code_needs_attention"
        ),
        CheckConstraint("pull_attempts >= 0", name="pull_attempts"),
        TABLE_OPTIONS,
    )

    platform_order_row_id: Mapped[str] = mapped_column(ID, primary_key=True)
    # 不另设到 tenants 的外键：到 sales_channels 的复合外键指向同租户的行（同 sku_listings）。
    tenant_id: Mapped[str] = mapped_column(ID)
    channel_id: Mapped[str] = mapped_column(ID)
    platform_order_id: Mapped[str] = mapped_column(PLATFORM_KEY)
    order_ref: Mapped[str] = mapped_column(PLATFORM_KEY)
    stage: Mapped[str] = mapped_column(STATUS, server_default=UNPAID)
    # 只记第一个待处理原因；后来事件的原因记在 platform_events.result_code。
    attention: Mapped[str | None] = mapped_column(CODE64)
    attention_code: Mapped[str | None] = mapped_column(CODE64)
    ready_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    next_pull_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    pull_attempts: Mapped[int] = mapped_column(Integer, server_default=ZERO)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)


class PlatformOrderLine(Base):
    """平台订单行（契约 5.10），以及已知差异（契约 3.2）。

    剩余件数是 quantity − cancelled_qty。platform_item_id 与 platform_model_id 是下单时平台给的，
    映射补上后继续处理时按它们重新解析 SKU；字符不合格的找不到平台商品、按 unmapped 进待处理，
    所以不做字符集检查。
    """

    __tablename__ = "platform_order_lines"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "platform_order_row_id"],
            ["platform_orders.tenant_id", "platform_orders.platform_order_row_id"],
        ),
        # sku_id 为空时不检查。
        ForeignKeyConstraint(["tenant_id", "sku_id"], ["skus.tenant_id", "skus.sku_id"]),
        _not_empty("platform_line_id"),
        _not_empty("platform_item_id"),
        _not_empty("platform_model_id"),
        CheckConstraint(f"quantity {QUANTITY_RANGE}", name="quantity"),
        CheckConstraint("cancelled_qty >= 0 AND cancelled_qty <= quantity", name="cancelled_qty"),
        CheckConstraint(
            "quota_returned >= 0 AND quota_returned <= quota_taken AND quota_taken <= quantity",
            name="quota_taken_returned",
        ),
        CheckConstraint(
            "(known_discrepancy <> 0) = (known_discrepancy_note IS NOT NULL"
            " AND CHAR_LENGTH(known_discrepancy_note) > 0)",
            name="known_discrepancy",
        ),
        TABLE_OPTIONS,
    )

    tenant_id: Mapped[str] = mapped_column(ID, primary_key=True)
    platform_order_row_id: Mapped[str] = mapped_column(ID, primary_key=True)
    platform_line_id: Mapped[str] = mapped_column(PLATFORM_KEY, primary_key=True)
    platform_item_id: Mapped[str] = mapped_column(PLATFORM_KEY)
    platform_model_id: Mapped[str] = mapped_column(PLATFORM_KEY)
    # 第一次处理时按生效的平台商品定下，找不到为空（契约 2.9）。
    sku_id: Mapped[str | None] = mapped_column(ID)
    quantity: Mapped[int] = mapped_column(Integer)
    cancelled_qty: Mapped[int] = mapped_column(Integer, server_default=ZERO)
    quota_taken: Mapped[int] = mapped_column(Integer, server_default=ZERO)
    quota_returned: Mapped[int] = mapped_column(Integer, server_default=ZERO)
    line_ref: Mapped[str | None] = mapped_column(LINE_REF)
    known_discrepancy: Mapped[bool] = mapped_column(Boolean, server_default=ZERO)
    known_discrepancy_note: Mapped[str | None] = mapped_column(String(500))


class PlatformEvent(Base):
    """收件箱：一个平台订单事件一行，永久保留（契约 5.10）。

    applied_at 有值即处理过；去重记录与它带来的变化同一事务提交，不另写幂等记录。
    """

    __tablename__ = "platform_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "channel_id"], ["sales_channels.tenant_id", "sales_channels.channel_id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "credential_id"], ["credentials.tenant_id", "credentials.credential_id"]
        ),
        # 去重键。按列名的约定名超过 MySQL 的 64 个字符，这里显式命名。
        UniqueConstraint(
            "tenant_id",
            "channel_id",
            "platform_order_id",
            "platform_line_id",
            "action",
            "event_key",
            name="uq_platform_events_dedupe_key",
        ),
        # MySQL 唯一索引允许多个 NULL，自动来源不受影响。
        UniqueConstraint("tenant_id", "credential_id", "manual_key"),
        # 按订单取未处理的事件；约定名超长，显式命名。
        Index(
            "ix_platform_events_order_applied_at",
            "tenant_id",
            "channel_id",
            "platform_order_id",
            "applied_at",
        ),
        # 作业找有未处理事件的订单。
        Index(
            "ix_platform_events_applied_at_tenant_id_channel_id",
            "applied_at",
            "tenant_id",
            "channel_id",
        ),
        # 约束名互不为子串：测试按约束名的子串认定是哪一条拒绝的。
        _not_empty("platform_order_id"),
        _one_of("action", EVENT_ACTIONS),
        _not_empty("event_key"),
        _one_of("source", EVENT_SOURCES),
        CheckConstraint("JSON_TYPE(payload) = 'OBJECT'", name="payload"),
        CheckConstraint(
            " AND ".join(
                f"(source = 'manual') = ({column} IS NOT NULL)" for column in MANUAL_COLUMNS
            ),
            name="manual_columns",
        ),
        *(_not_empty(column) for column in MANUAL_COLUMNS),
        _one_of("result", EVENT_RESULTS, name="result_value"),
        CheckConstraint("(applied_at IS NULL) = (result IS NULL)", name="applied_result"),
        _not_empty("result_code"),
        # applied 时可有可无（成功但另标缺陷时记 defect）；未处理时为空。
        CheckConstraint(
            "CASE WHEN result = 'applied' THEN TRUE"
            " WHEN result IN ('held', 'attention') THEN result_code IS NOT NULL"
            " ELSE result_code IS NULL END",
            name="result_code_by_result",
        ),
        TABLE_OPTIONS,
    )

    event_row_id: Mapped[str] = mapped_column(ID, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ID)
    channel_id: Mapped[str] = mapped_column(ID)
    platform_order_id: Mapped[str] = mapped_column(PLATFORM_KEY)
    # 订单级的动作为空串。
    platform_line_id: Mapped[str] = mapped_column(PLATFORM_KEY)
    action: Mapped[str] = mapped_column(CODE32)
    # 平台事件 ID；平台给不出时为平台状态时间的 UTC 文本；后台登记的为这次提交的幂等键。
    event_key: Mapped[str] = mapped_column(PLATFORM_KEY)
    source: Mapped[str] = mapped_column(STATUS)
    # 只有改账要用的字段，不含顾客资料。
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    # 与 idempotency_records 的幂等键与请求摘要同类型。
    manual_key: Mapped[str | None] = mapped_column(REF)
    request_hash: Mapped[str | None] = mapped_column(CHAR(64, collation=EXACT_COLLATION))
    received_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    applied_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    result: Mapped[str | None] = mapped_column(STATUS)
    # 这个事件的待处理原因或 Core 错误码。
    result_code: Mapped[str | None] = mapped_column(CODE64)
    # 自动来源为平台事件的系统主体，人工登记为管理员（契约第 10 节第 4 条）。
    credential_id: Mapped[str] = mapped_column(ID)
