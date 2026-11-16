"""预留、释放、到期与可售量不碰数据库的规则（契约 §5.2、§5.6）。

锁行、读写预留与订单行由上层在数据库里做；这里只放取值常量与判定。
时间一律传数据库 UTC 时间（同一种表示：都带时区或都不带）。
"""

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from enum import StrEnum

# ---- 预留状态（§5.2） ----

ACTIVE = "active"
RELEASED = "released"
EXPIRED = "expired"
COMMITTED = "committed"
LATE_PAYMENT_REVIEW = "late_payment_review"
CLOSED_REFUNDED = "closed_refunded"
STATUSES = (ACTIVE, RELEASED, EXPIRED, COMMITTED, LATE_PAYMENT_REVIEW, CLOSED_REFUNDED)

# ---- 释放原因与流水（§5.6） ----

CANCELLED = "cancelled"
PAYMENT_FAILED = "payment_failed"
CHECKOUT_REPLACED = "checkout_replaced"
RELEASE_REASONS = (CANCELLED, PAYMENT_FAILED, CHECKOUT_REPLACED)

# 到期关单的 RELEASE 流水的 reason_code。
EXPIRED_REASON_CODE = "expired"
SOURCE_TYPE = "reservation"

# new_id 的前缀：预留 rsv_…，订单行 oln_…。
RESERVATION_ID_PREFIX = "rsv"
ORDER_LINE_ID_PREFIX = "oln"

# ---- 有效期（§5.6，Q-53）与订单行数量 ----

TTL_DEFAULT = 900
TTL_MIN = 60
TTL_MAX = 3600

LINE_QUANTITY_MIN = 1
LINE_QUANTITY_MAX = 1000000

# ---- 可售量的状态（§5.6） ----

MERGED = "merged"
INACTIVE = "inactive"
SELLABLE = "sellable"


class ReleaseDecision(StrEnum):
    RELEASE = "release"  # active 且未到期：放回库存，改 released
    EXPIRE = "expire"  # active 而已到期：按到期关闭关掉，返回 expired
    UNCHANGED = "unchanged"  # 已是 released 或 expired：不写任何东西
    CONFLICT = "conflict"  # committed 及之后：409 STATE_CONFLICT


def parse_ttl_seconds(value) -> int:
    """60–3600 的整数；bool 与其他类型都不接受。"""
    # bool 是 int 的子类，True 不能当 1 秒。
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"invalid reservation ttl: {value!r}")
    if not TTL_MIN <= value <= TTL_MAX:
        raise ValueError(f"invalid reservation ttl: {value!r}")
    return value


def compute_expires_at(created_at: datetime, ttl_seconds: int) -> datetime:
    """expires_at = created_at + ttl_seconds（§5.6）。"""
    return created_at + timedelta(seconds=ttl_seconds)


def is_due(status: str, expires_at: datetime, now: datetime) -> bool:
    """只有 active 会到期；now ≥ expires_at 即到期（§5.2 第 4 条）。"""
    return status == ACTIVE and now >= expires_at


def displayed_status(status: str, expires_at: datetime, now: datetime) -> str:
    """读接口不写库，已到期还没关的 active 显示为 expired（Q-54）。"""
    return EXPIRED if is_due(status, expires_at, now) else status


def release_decision(status: str, expires_at: datetime, now: datetime) -> ReleaseDecision:
    """释放命令按锁住的预留行状态的判定（§5.6 释放，Q-55）；未知状态抛 ValueError。"""
    if status == ACTIVE:
        return ReleaseDecision.EXPIRE if now >= expires_at else ReleaseDecision.RELEASE
    if status in (RELEASED, EXPIRED):
        return ReleaseDecision.UNCHANGED
    if status in (COMMITTED, LATE_PAYMENT_REVIEW, CLOSED_REFUNDED):
        return ReleaseDecision.CONFLICT
    raise ValueError(f"unknown reservation status: {status!r}")


def find_shortfalls(
    lines: Iterable[tuple[str, str, int]], available_by_sku: Mapping[str, int]
) -> list[dict]:
    """可售不足的行（§5.6 预留第 6 步），按请求顺序；都够时为空列表。

    lines 为 (line_ref, sku_id, quantity)；每项 line_ref、sku_id、quantity、available，
    即 INSUFFICIENT_STOCK 的 details.lines。
    """
    shortfalls = []
    for line_ref, sku_id, quantity in lines:
        available = available_by_sku[sku_id]
        if quantity > available:
            shortfalls.append(
                {
                    "line_ref": line_ref,
                    "sku_id": sku_id,
                    "quantity": quantity,
                    "available": available,
                }
            )
    return shortfalls


def availability_status(
    merged_into_sku_id: str | None, sku_active: bool, product_active: bool
) -> str:
    """可售量的状态：已合并先于停用，停用先于可售（§5.6 可售量）。"""
    if merged_into_sku_id is not None:
        return MERGED
    if not (sku_active and product_active):
        return INACTIVE
    return SELLABLE
