"""配额、安全库存与平台已售未扣的规则（契约 §3.1，0.22，Q-81、Q-82、Q-97、Q-102；§3.2 Q-99）。

这里只有不碰数据库的判断：锁池行、余额行、配额行，读平台商品与人工核对由上层做，
读到的快照交给这里判定；时间由调用方传入（数据库时间）。
参数本身不合规（负数、bool 当整数、带时区的时间）是调用方的缺陷，抛 ValueError；
改配额的业务拒绝以 Rejection 返回，接口照 code 回 422 或 409。
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.domain.catalog import InvalidField, normalize_reason

AUTO = "auto"
MANUAL = "manual"
MODES = (AUTO, MANUAL)

# platform_connections.status 里能接收配额的那一个（§3.1 恢复条件第 1 项）。
CONNECTION_ACTIVE = "active"

# §3.2：距最近一次核对超过这么久即逾期（严格大于）。
MANUAL_CHECK_LIMIT = timedelta(hours=12)

# §3.1 存放：safety_stock、quota 的检查 0–1,000,000。
ALLOCATION_MAX = 1000000

INVALID_REQUEST = "INVALID_REQUEST"
VERSION_CONFLICT = "VERSION_CONFLICT"
QUOTA_CHANGED = "QUOTA_CHANGED"
NO_ACTIVE_LISTING = "NO_ACTIVE_LISTING"
MANUAL_CHECK_OVERDUE = "MANUAL_CHECK_OVERDUE"
ALLOCATION_EXCEEDS_UNALLOCATED = "ALLOCATION_EXCEEDS_UNALLOCATED"

# ---- 改配额的正文字段（§3.1 改配额） ----

POOL_ID = "pool_id"
SKU_ID = "sku_id"
EXPECTED_VERSION = "expected_version"
SAFETY_STOCK = "safety_stock"
QUOTAS = "quotas"
REASON = "reason"
REQUEST_FIELDS = frozenset({POOL_ID, SKU_ID, EXPECTED_VERSION, SAFETY_STOCK, QUOTAS, REASON})

CHANNEL_ID = "channel_id"
EXPECTED_QUOTA = "expected_quota"
QUOTA = "quota"
QUOTA_FIELDS = frozenset({CHANNEL_ID, EXPECTED_QUOTA, QUOTA})


def _is_int(value) -> bool:
    # bool 是 int 的子类，JSON 的 true 不能当 1。
    return isinstance(value, int) and not isinstance(value, bool)


def _require_count(name: str, value) -> int:
    if not _is_int(value) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer: {value!r}")
    return value


def _require_mode(mode):
    if mode not in MODES:
        raise ValueError(f"unknown platform connection mode: {mode!r}")


# ---- 未分配与随事件改 Q（§3.1 各事件怎么改量） ----


def unallocated(
    available: int, safety_stock: int, quotas: Iterable[int], pendings: Iterable[int]
) -> int:
    """F = available − S − ΣQ − Σpending，求和对这个池的全部 platform 入口；可以为负（超额）。"""
    _require_count("available", available)
    _require_count("safety_stock", safety_stock)
    quota_total = sum(_require_count("quota", quota) for quota in quotas)
    pending_total = sum(_require_count("pending", pending) for pending in pendings)
    return available - safety_stock - quota_total - pending_total


def take_quota(quantity: int, quota: int) -> tuple[int, int]:
    """平台新订单从 Q 扣走的件数（记为 quota_taken）与新的 Q。

    Q 只减到 0：平台卖出超过剩余配额时多出的件不从 Q 扣（Q-97），照样进 pending，由 F 体现。
    """
    _require_count("quantity", quantity)
    _require_count("quota", quota)
    taken = min(quantity, quota)
    return taken, quota - taken


def add_back(cancelled: int, quota_taken: int, quota_returned: int, restorable: bool) -> int:
    """取消时加回 Q 的件数：不满足恢复条件为 0，否则 min(取消的件数, quota_taken − quota_returned)。

    待处理中的订单（Core 从未扣数）被取消一件也不加回，由调用方传 restorable 为假。
    """
    _require_count("cancelled", cancelled)
    _require_count("quota_taken", quota_taken)
    _require_count("quota_returned", quota_returned)
    if quota_returned > quota_taken:
        raise ValueError("quota_returned must not exceed quota_taken")
    if not restorable:
        return 0
    return min(cancelled, quota_taken - quota_returned)


def restore_allowed(
    mode: str,
    connection_status: str,
    manual_overdue: bool,
    has_active_listing: bool,
    review_hold: bool,
    unallocated_after: int,
) -> bool:
    """§3.1 恢复条件四项是否全满足；不满足时这些件数整体进 F（不部分加回）。

    unallocated_after 是按加回之后算的 F：调用方先减掉取消的 pending、再加上要加回的配额来算。
    """
    _require_mode(mode)
    if not _is_int(unallocated_after):
        raise ValueError(f"unallocated_after must be an integer: {unallocated_after!r}")
    if mode == AUTO:
        can_receive = connection_status == CONNECTION_ACTIVE
    else:
        can_receive = not manual_overdue
    return can_receive and has_active_listing and not review_hold and unallocated_after >= 0


# ---- 人工核对逾期（§3.2，Q-99） ----


def _require_naive(name: str, value) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{name} must be a datetime: {value!r}")
    if value.tzinfo is not None:
        raise ValueError(f"{name} must be a naive UTC datetime")
    return value


def manual_check_overdue(
    mode: str,
    last_checked_until: datetime | None,
    mode_changed_at: datetime,
    now: datetime,
) -> bool:
    """人工模式的入口距起点超过 12 小时（严格大于）即逾期；自动模式总是假。

    起点是最近一次核对的 checked_until 与 mode_changed_at 中较晚的：切到人工之前的核对不算数，
    没核对过就从切换时刻算。时间都是不带时区的 UTC datetime。
    """
    _require_mode(mode)
    if mode_changed_at is None:
        raise ValueError("mode_changed_at is required")
    _require_naive("mode_changed_at", mode_changed_at)
    _require_naive("now", now)
    if last_checked_until is not None:
        _require_naive("last_checked_until", last_checked_until)
    if mode == AUTO:
        return False
    start = mode_changed_at
    if last_checked_until is not None and last_checked_until > start:
        start = last_checked_until
    return now - start > MANUAL_CHECK_LIMIT


# ---- 改配额与安全库存（§3.1 allocations.update） ----


@dataclass(frozen=True)
class Rejection:
    """改配额被拒：code 是顶层错误码，details 是响应的 details。"""

    code: str
    details: dict


@dataclass(frozen=True)
class QuotaChange:
    """正文 quotas 的一项。"""

    channel_id: str
    expected_quota: int
    quota: int


@dataclass(frozen=True)
class AllocationRequest:
    """校验过形状的改配额正文；没带 safety_stock 或 quotas 的为 None，reason 已去首尾空白。"""

    pool_id: object
    sku_id: object
    expected_version: int
    safety_stock: int | None
    quotas: tuple[QuotaChange, ...] | None
    reason: str


@dataclass(frozen=True)
class ChannelAllocation:
    """锁内读到的一个 platform 入口在这个 SKU 上的配额行、平台商品与人工核对。"""

    channel_id: str
    quota: int
    pending: int
    has_active_listing: bool
    manual_overdue: bool


@dataclass(frozen=True)
class AllocationSnapshot:
    """锁内读到的池行 version、S、available，与这个池全部 platform 入口的配额行。"""

    version: int
    safety_stock: int
    available: int
    channels: tuple[ChannelAllocation, ...]


@dataclass(frozen=True)
class AllocationUpdate:
    """判定通过后要写的新值：S 与每个 platform 入口的 Q（没改的入口是当前值）。"""

    safety_stock: int
    quotas: dict[str, int]


def judge_allocation_update(
    current: AllocationSnapshot, request: AllocationRequest
) -> AllocationUpdate | Rejection:
    """按 §3.1 改配额的第 2–5 项给出第一个不满足的；SKU 不存在（第 1 项）由服务判。

    只减配额、只改 S 不做第 3–5 项。
    """
    channels = {channel.channel_id: channel for channel in current.channels}
    if len(channels) != len(current.channels):
        raise ValueError("snapshot lists a channel more than once")
    changes = request.quotas or ()
    for change in changes:
        if change.channel_id not in channels:
            raise ValueError(f"channel {change.channel_id!r} is not in the snapshot")

    if request.expected_version != current.version:
        return Rejection(VERSION_CONFLICT, {"current_version": current.version})

    changed = sorted(
        change.channel_id
        for change in changes
        if channels[change.channel_id].quota != change.expected_quota
    )
    if changed:
        quotas = [
            {CHANNEL_ID: channel_id, QUOTA: channels[channel_id].quota} for channel_id in changed
        ]
        return Rejection(QUOTA_CHANGED, {QUOTAS: quotas})

    new_quotas = {channel.channel_id: channel.quota for channel in current.channels}
    for change in changes:
        new_quotas[change.channel_id] = change.quota
    safety_stock = current.safety_stock if request.safety_stock is None else request.safety_stock
    update = AllocationUpdate(safety_stock=safety_stock, quotas=new_quotas)

    increased = sorted(
        change.channel_id for change in changes if change.quota > channels[change.channel_id].quota
    )
    if not increased:
        return update

    for channel_id in increased:
        if not channels[channel_id].has_active_listing:
            return Rejection(NO_ACTIVE_LISTING, {CHANNEL_ID: channel_id})

    overdue = sorted(channel.channel_id for channel in current.channels if channel.manual_overdue)
    for channel_id in overdue:
        if any(other != channel_id for other in increased):
            return Rejection(MANUAL_CHECK_OVERDUE, {CHANNEL_ID: channel_id})

    pendings = [channel.pending for channel in current.channels]
    quota_total = sum(new_quotas.values())
    pending_total = sum(pendings)
    unallocated_after = unallocated(current.available, safety_stock, new_quotas.values(), pendings)
    if unallocated_after < 0:
        return Rejection(
            ALLOCATION_EXCEEDS_UNALLOCATED,
            {
                "available": current.available,
                "safety_stock": safety_stock,
                "quota_total": quota_total,
                "pending_total": pending_total,
                "unallocated": unallocated_after,
            },
        )
    return update


# ---- 改配额的正文校验（§3.1 改配额的正文、§2.6 第 1 条的 reason） ----


def _invalid(field: str) -> Rejection:
    return Rejection(INVALID_REQUEST, {"field": field})


def _is_allocation(value) -> bool:
    return _is_int(value) and 0 <= value <= ALLOCATION_MAX


def _parse_quotas(value, platform_channel_ids: frozenset) -> tuple[QuotaChange, ...] | None:
    """quotas 合格时返回各项，否则 None。"""
    if not isinstance(value, list) or not value:
        return None
    changes = []
    seen = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) != QUOTA_FIELDS:
            return None
        channel_id = item[CHANNEL_ID]
        if not isinstance(channel_id, str) or channel_id not in platform_channel_ids:
            return None
        if channel_id in seen:
            return None
        seen.add(channel_id)
        if not _is_allocation(item[EXPECTED_QUOTA]) or not _is_allocation(item[QUOTA]):
            return None
        changes.append(QuotaChange(channel_id, item[EXPECTED_QUOTA], item[QUOTA]))
    return tuple(changes)


def validate_allocation_request(
    body, platform_channel_ids: Iterable[str]
) -> AllocationRequest | Rejection:
    """改配额正文的形状；不合格返回 INVALID_REQUEST，details.field 是第一个出错的字段。

    先报多出来的键（按键名升序取第一个），再按 pool_id、sku_id、expected_version、safety_stock、
    quotas、reason 的先后；值为 null 不当作没给。safety_stock 与 quotas 都没给时报 quotas。
    platform_channel_ids 是这个池的 platform 入口；pool_id、sku_id 只要求存在，格式由服务判。
    """
    if not isinstance(body, Mapping):
        return _invalid("body")
    extra = sorted(str(field) for field in body if field not in REQUEST_FIELDS)
    if extra:
        return _invalid(extra[0])
    for field in (POOL_ID, SKU_ID):
        if body.get(field) is None:
            return _invalid(field)

    expected_version = body.get(EXPECTED_VERSION)
    if not _is_int(expected_version) or expected_version < 0:
        return _invalid(EXPECTED_VERSION)

    safety_stock = None
    if SAFETY_STOCK in body:
        safety_stock = body[SAFETY_STOCK]
        if not _is_allocation(safety_stock):
            return _invalid(SAFETY_STOCK)
    if SAFETY_STOCK not in body and QUOTAS not in body:
        return _invalid(QUOTAS)

    quotas = None
    if QUOTAS in body:
        quotas = _parse_quotas(body[QUOTAS], frozenset(platform_channel_ids))
        if quotas is None:
            return _invalid(QUOTAS)

    if body.get(REASON) is None:
        return _invalid(REASON)
    try:
        reason = normalize_reason(body[REASON])
    except InvalidField:
        return _invalid(REASON)

    return AllocationRequest(
        pool_id=body[POOL_ID],
        sku_id=body[SKU_ID],
        expected_version=expected_version,
        safety_stock=safety_stock,
        quotas=quotas,
        reason=reason,
    )
