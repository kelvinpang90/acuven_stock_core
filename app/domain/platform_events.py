"""平台订单事件不碰数据库的规则（契约 §5.10，Q-83、Q-98、Q-100）。

引用派生、订单锁名、payload 形状、事件排序、阶段判定、按 SKU 合并订单行、
派生的命令正文与补拉退避。取锁、开事务、改平台订单与调命令体由平台事件执行器做；
派生正文用 /v1 请求模型校验也在执行器里做（domain 不 import app/api）。
"""

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

# ---- 动作与阶段（§5.10） ----

CREATED = "created"
READY_TO_SHIP = "ready_to_ship"
SHIPPED = "shipped"
CANCELLED = "cancelled"  # 既是动作，也是阶段
RETURN_REQUESTED = "return_requested"
RETURN_RECEIVED = "return_received"
ACTIONS = (CREATED, READY_TO_SHIP, SHIPPED, CANCELLED, RETURN_REQUESTED, RETURN_RECEIVED)

UNPAID = "unpaid"
HELD = "held"
COMMITTED = "committed"
STAGES = (UNPAID, HELD, COMMITTED, CANCELLED)

# 状态链的先后，只用来排同一平台时间的事件与发现缺了前序动作。
CHAIN_ORDER = {
    CREATED: 1,
    READY_TO_SHIP: 2,
    SHIPPED: 3,
    CANCELLED: 3,
    RETURN_REQUESTED: 4,
    RETURN_RECEIVED: 5,
}

# ---- 引用（§5.10 引用格式） ----

PLATFORM_PREFIXES = {"shopee": "shp", "tiktok_shop": "tts"}
SHIPMENT = "d"
REFUND = "r"
RETURN = "x"
DOCUMENT_KINDS = (SHIPMENT, REFUND, RETURN)

ORDER_REF_MAX = 112
DOCUMENT_REF_MAX = 128
SEGMENT_MAX = 64
HASH_HEX_LENGTH = 32

# 契约的字符集去掉冒号：段里带冒号会让 A:d:B 加 X 与 A 加 B:d:X 拼出同一个引用。
_SEGMENT = re.compile(r"[A-Za-z0-9._-]+")
# 与换过的段撞形的原样段，以及与人工“平台没有给 ID”的 m<n> 撞形的单据 ID，也换掉。
_HASHED = re.compile(r"h[0-9a-f]{32}")
_MANUAL = re.compile(r"m[0-9]+")

LOCK_KIND = "platform_order"

# ---- payload（§5.10 存放） ----

PLATFORM_TIME = "platform_time"
LINES = "lines"
PLATFORM_LINE_ID = "platform_line_id"
PLATFORM_ITEM_ID = "platform_item_id"
PLATFORM_MODEL_ID = "platform_model_id"
QUANTITY = "quantity"
CANCELLED_TOTAL = "cancelled_total"
REFUND_ID = "refund_id"
PACKAGE_ID = "package_id"
RETURN_ID = "return_id"
SELLABLE_QUANTITY = "sellable_quantity"
UNSELLABLE_QUANTITY = "unsellable_quantity"

ID_MAX_LENGTH = 128
QUANTITY_MAX = 1000000
# 一单超过 100 个 SKU 也要能进收件箱，由执行器设 held、INVALID_REQUEST。
CREATED_LINES_MAX = 1000
LINES_MAX = 100
# DATETIME(6) 存 1000–9999 年，两头各留一年（同 app/domain/stock.py）。
MIN_YEAR = 1001
MAX_YEAR = 9998

# Cc 类：C0、DEL 与 C1。
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# 只认带 Z 的 UTC；T 与 Z 只认大写，数字只认 ASCII，小数秒 1–6 位。
_PLATFORM_TIME = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,6}))?Z"
)

# ---- 补拉退避（§5.10 第 4 步） ----

PULL_BACKOFF_MAX_MINUTES = 60


# ---- 引用 ----


def _hash_segment(value: str) -> str:
    return "h" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:HASH_HEX_LENGTH]


def _segment(value, *, document: bool = False) -> str:
    """平台给的一段：合格的原样用，不合格的按它本身确定性地换成 h 加 32 位十六进制。"""
    if not isinstance(value, str):
        raise ValueError(f"platform id must be a string, got {value!r}")
    if (
        len(value) > SEGMENT_MAX
        or not _SEGMENT.fullmatch(value)
        or _HASHED.fullmatch(value)
        or (document and _MANUAL.fullmatch(value))
    ):
        return _hash_segment(value)
    return value


def order_ref(platform: str, shop_id: str, platform_order_id: str) -> str:
    """<平台前缀>:<店铺 ID>:<平台订单号>，不超过 112 个字符。

    超长时先换平台订单号这一段，还超再换店铺 ID 这一段；未知平台抛 ValueError。
    """
    prefix = PLATFORM_PREFIXES.get(platform)
    if prefix is None:
        raise ValueError(f"unknown platform: {platform!r}")
    shop = _segment(shop_id)
    order = _segment(platform_order_id)
    if len(f"{prefix}:{shop}:{order}") > ORDER_REF_MAX:
        order = _hash_segment(platform_order_id)
    if len(f"{prefix}:{shop}:{order}") > ORDER_REF_MAX:
        shop = _hash_segment(shop_id)
    return f"{prefix}:{shop}:{order}"


def payment_ref(order_ref: str) -> str:
    """order_ref 加 :c（一单只确认一次）。"""
    return f"{order_ref}:c"


def _document_ref(order_ref: str, kind: str, segment: str) -> str:
    if kind not in DOCUMENT_KINDS:
        raise ValueError(f"unknown document kind: {kind!r}")
    if not isinstance(order_ref, str) or not order_ref:
        raise ValueError(f"invalid order_ref: {order_ref!r}")
    ref = f"{order_ref}:{kind}:{segment}"
    if len(ref) > DOCUMENT_REF_MAX:
        # Core 按引用本身判唯一，不要求它以 order_ref 开头。
        ref = f"{_hash_segment(order_ref)}:{kind}:{segment}"
    return ref


def document_ref(order_ref: str, kind: str, platform_id: str) -> str:
    """单据引用：order_ref 加 :d:、:r: 或 :x: 再加包裹、退款或退货 ID，不超过 128 个字符。"""
    return _document_ref(order_ref, kind, _segment(platform_id, document=True))


def manual_document_ref(order_ref: str, kind: str, n: int) -> str:
    """人工勾选“平台没有给 ID”时的单据引用，单据 ID 段为 m 加 n（n 从 1 起）。"""
    if type(n) is not int or n < 1:
        raise ValueError(f"manual document number must be a positive integer, got {n!r}")
    return _document_ref(order_ref, kind, f"m{n}")


# ---- 订单锁名（§5.10 第 1 步） ----


def order_lock_name(database: str, tenant_id: str, channel_id: str, platform_order_id: str) -> str:
    """按订单的命名锁锁名：五项以换行符连接后 UTF-8 的 SHA-256 小写十六进制。

    任一项为空或含换行符时拒绝，以免两个不同的订单拼出同一个锁名。
    """
    parts = (database, LOCK_KIND, tenant_id, channel_id, platform_order_id)
    for part in parts:
        if not isinstance(part, str) or not part or "\n" in part:
            raise ValueError(f"invalid lock name part: {part!r}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


# ---- payload ----


@dataclass(frozen=True)
class CreatedLine:
    platform_line_id: str
    platform_item_id: str
    platform_model_id: str
    quantity: int


@dataclass(frozen=True)
class CreatedPayload:
    platform_time: datetime | None
    lines: tuple[CreatedLine, ...]


@dataclass(frozen=True)
class StatusPayload:
    """ready_to_ship 与 return_requested：只有平台时间。"""

    platform_time: datetime | None


@dataclass(frozen=True)
class CancelledLine:
    platform_line_id: str
    cancelled_total: int  # 这一行在平台上累计已取消的件数
    quantity: int | None  # 这一次退款取消的件数，只在带 refund_id 时有


@dataclass(frozen=True)
class CancelledPayload:
    platform_time: datetime | None
    refund_id: str | None
    lines: tuple[CancelledLine, ...] | None  # None 为订单级取消


@dataclass(frozen=True)
class ShippedLine:
    platform_line_id: str
    quantity: int  # 这个包裹里这一行的件数


@dataclass(frozen=True)
class ShippedPayload:
    platform_time: datetime | None
    package_id: str | None
    lines: tuple[ShippedLine, ...]


@dataclass(frozen=True)
class ReturnLine:
    platform_line_id: str
    sellable_quantity: int
    unsellable_quantity: int


@dataclass(frozen=True)
class ReturnReceivedPayload:
    platform_time: datetime | None
    return_id: str | None
    lines: tuple[ReturnLine, ...]


Payload = CreatedPayload | StatusPayload | CancelledPayload | ShippedPayload | ReturnReceivedPayload


def _object(value, required: Iterable[str], optional: Iterable[str], where: str) -> dict:
    """JSON 对象，必填键都在、没有不认识的键。"""
    required = set(required)
    allowed = required | set(optional)
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be an object")
    for key in value:
        if key not in allowed:
            raise ValueError(f"{where} has unknown key {key!r}")
    for key in required:
        if key not in value:
            raise ValueError(f"{where} is missing {key!r}")
    return value


def _text(value, field: str) -> str:
    """1–128 个字符、不含控制字符；不做字符集检查。"""
    if not isinstance(value, str) or not 1 <= len(value) <= ID_MAX_LENGTH:
        raise ValueError(f"invalid {field}")
    if _CONTROL.search(value):
        raise ValueError(f"invalid {field}")
    return value


def _integer(value, field: str, minimum: int) -> int:
    """JSON 整数（bool 不算），minimum 到 1,000,000。"""
    if type(value) is not int or not minimum <= value <= QUANTITY_MAX:
        raise ValueError(f"invalid {field}")
    return value


def parse_platform_time(value) -> datetime:
    """带 Z 的 ISO 8601 UTC 文本，秒后可带 1–6 位小数；返回不带时区的 UTC datetime。"""
    if not isinstance(value, str):
        raise ValueError(f"invalid {PLATFORM_TIME}")
    match = _PLATFORM_TIME.fullmatch(value)
    if match is None:
        raise ValueError(f"invalid {PLATFORM_TIME}")
    year, month, day, hour, minute, second, fraction = match.groups()
    if not MIN_YEAR <= int(year) <= MAX_YEAR:
        raise ValueError(f"invalid {PLATFORM_TIME}")
    try:
        return datetime(
            int(year),
            int(month),
            int(day),
            int(hour),
            int(minute),
            int(second),
            int((fraction or "").ljust(6, "0")),
        )
    except ValueError:
        raise ValueError(f"invalid {PLATFORM_TIME}") from None


def _platform_time(payload: dict) -> datetime | None:
    if PLATFORM_TIME not in payload:
        return None
    return parse_platform_time(payload[PLATFORM_TIME])


def _lines(value, maximum: int, parse_line) -> tuple:
    """非空、不超过 maximum 项的列表，platform_line_id 不重复。"""
    if not isinstance(value, list) or not 1 <= len(value) <= maximum:
        raise ValueError(f"{LINES} must be a list of 1 to {maximum} items")
    lines = tuple(parse_line(item) for item in value)
    ids = [line.platform_line_id for line in lines]
    if len(set(ids)) != len(ids):
        raise ValueError(f"duplicate {PLATFORM_LINE_ID}")
    return lines


def _created_line(item) -> CreatedLine:
    _object(item, (PLATFORM_LINE_ID, PLATFORM_ITEM_ID, PLATFORM_MODEL_ID, QUANTITY), (), "line")
    return CreatedLine(
        platform_line_id=_text(item[PLATFORM_LINE_ID], PLATFORM_LINE_ID),
        platform_item_id=_text(item[PLATFORM_ITEM_ID], PLATFORM_ITEM_ID),
        platform_model_id=_text(item[PLATFORM_MODEL_ID], PLATFORM_MODEL_ID),
        quantity=_integer(item[QUANTITY], QUANTITY, 1),
    )


def _cancelled_line(with_refund: bool):
    def parse(item) -> CancelledLine:
        required = [PLATFORM_LINE_ID, CANCELLED_TOTAL]
        if with_refund:
            required.append(QUANTITY)
        _object(item, required, (), "line")
        return CancelledLine(
            platform_line_id=_text(item[PLATFORM_LINE_ID], PLATFORM_LINE_ID),
            cancelled_total=_integer(item[CANCELLED_TOTAL], CANCELLED_TOTAL, 0),
            quantity=_integer(item[QUANTITY], QUANTITY, 1) if with_refund else None,
        )

    return parse


def _shipped_line(item) -> ShippedLine:
    _object(item, (PLATFORM_LINE_ID, QUANTITY), (), "line")
    return ShippedLine(
        platform_line_id=_text(item[PLATFORM_LINE_ID], PLATFORM_LINE_ID),
        quantity=_integer(item[QUANTITY], QUANTITY, 1),
    )


def _return_line(item) -> ReturnLine:
    _object(item, (PLATFORM_LINE_ID, SELLABLE_QUANTITY, UNSELLABLE_QUANTITY), (), "line")
    line = ReturnLine(
        platform_line_id=_text(item[PLATFORM_LINE_ID], PLATFORM_LINE_ID),
        sellable_quantity=_integer(item[SELLABLE_QUANTITY], SELLABLE_QUANTITY, 0),
        unsellable_quantity=_integer(item[UNSELLABLE_QUANTITY], UNSELLABLE_QUANTITY, 0),
    )
    if line.sellable_quantity + line.unsellable_quantity == 0:
        raise ValueError(f"{SELLABLE_QUANTITY} and {UNSELLABLE_QUANTITY} are both 0")
    return line


def _optional_text(payload: dict, field: str) -> str | None:
    if field not in payload:
        return None
    return _text(payload[field], field)


def parse_payload(action: str, payload) -> Payload:
    """按动作校验收件箱 payload，返回不可变的数据类；不合格或未知动作抛 ValueError。"""
    if action == CREATED:
        _object(payload, (LINES,), (PLATFORM_TIME,), "payload")
        return CreatedPayload(
            platform_time=_platform_time(payload),
            lines=_lines(payload[LINES], CREATED_LINES_MAX, _created_line),
        )
    if action in (READY_TO_SHIP, RETURN_REQUESTED):
        _object(payload, (), (PLATFORM_TIME,), "payload")
        return StatusPayload(platform_time=_platform_time(payload))
    if action == CANCELLED:
        _object(payload, (), (PLATFORM_TIME, REFUND_ID, LINES), "payload")
        refund_id = _optional_text(payload, REFUND_ID)
        lines = None
        if LINES in payload:
            lines = _lines(payload[LINES], LINES_MAX, _cancelled_line(refund_id is not None))
        return CancelledPayload(
            platform_time=_platform_time(payload), refund_id=refund_id, lines=lines
        )
    if action == SHIPPED:
        _object(payload, (LINES,), (PLATFORM_TIME, PACKAGE_ID), "payload")
        return ShippedPayload(
            platform_time=_platform_time(payload),
            package_id=_optional_text(payload, PACKAGE_ID),
            lines=_lines(payload[LINES], LINES_MAX, _shipped_line),
        )
    if action == RETURN_RECEIVED:
        _object(payload, (LINES,), (PLATFORM_TIME, RETURN_ID), "payload")
        return ReturnReceivedPayload(
            platform_time=_platform_time(payload),
            return_id=_optional_text(payload, RETURN_ID),
            lines=_lines(payload[LINES], LINES_MAX, _return_line),
        )
    raise ValueError(f"unknown action: {action!r}")


# ---- 排序（§5.10 第 2 步） ----


def _naive(value, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is not None:
        raise ValueError(f"{field} must be a naive UTC datetime")
    return value


def sort_key(
    platform_time: datetime | None, action: str, received_at: datetime, event_row_id: str
) -> tuple:
    """平台时间（没有用 received_at）→ 状态链先后 → received_at → event_row_id（全序）。"""
    if action not in CHAIN_ORDER:
        raise ValueError(f"unknown action: {action!r}")
    received_at = _naive(received_at, "received_at")
    if platform_time is not None:
        platform_time = _naive(platform_time, PLATFORM_TIME)
    return (
        platform_time if platform_time is not None else received_at,
        CHAIN_ORDER[action],
        received_at,
        event_row_id,
    )


# ---- 阶段判定（§5.10 第 4 步） ----


class Outcome(StrEnum):
    APPLY = "apply"  # 照常处理
    IGNORED = "ignored"  # 落后于阶段
    MISSING_PRIOR = "missing_prior"  # 越过阶段，缺 missing 这个前序动作
    BLOCKED = "blocked"  # 这一轮停下、不补拉
    ATTENTION = "attention"  # 与阶段矛盾


@dataclass(frozen=True)
class Classification:
    outcome: Outcome
    missing: str | None = None  # 只在 MISSING_PRIOR 时有


_APPLY = Classification(Outcome.APPLY)
_IGNORED = Classification(Outcome.IGNORED)
_BLOCKED = Classification(Outcome.BLOCKED)
_ATTENTION = Classification(Outcome.ATTENTION)
_MISSING_CREATED = Classification(Outcome.MISSING_PRIOR, CREATED)
_MISSING_READY = Classification(Outcome.MISSING_PRIOR, READY_TO_SHIP)


def _after_commit(stage: str, ready_at_set: bool) -> Classification:
    """shipped 与 return_received：只在 committed 时做。"""
    if stage == COMMITTED:
        return _APPLY
    if stage == UNPAID:
        return _MISSING_READY
    if stage == HELD:
        # 待发货处理过、被拒在待处理：只能等后台重试，补拉也补不出来。
        return _BLOCKED if ready_at_set else _MISSING_READY
    return _ATTENTION


def classify(stage: str | None, action: str, ready_at_set: bool) -> Classification:
    """收件箱事件按平台订单当前阶段的判定；stage 为 None 表示平台订单还不存在。

    后台的待处理重试、映射补上后继续处理不经这里。未知阶段或动作抛 ValueError。
    cancelled 阶段收到 ready_to_ship 或 shipped 记 attention、held 阶段收到 shipped
    为 blocked，是据第 4、6 步补的。
    """
    if stage is not None and stage not in STAGES:
        raise ValueError(f"unknown stage: {stage!r}")
    if action not in ACTIONS:
        raise ValueError(f"unknown action: {action!r}")
    if action == RETURN_REQUESTED:
        return _IGNORED
    if action == CREATED:
        return _APPLY if stage is None else _IGNORED
    if stage is None:
        return _MISSING_CREATED
    if action == READY_TO_SHIP:
        if stage == UNPAID:
            return _APPLY
        if stage == HELD:
            return _IGNORED if ready_at_set else _APPLY
        if stage == COMMITTED:
            return _IGNORED
        return _ATTENTION
    if action == CANCELLED:
        # 调用方按阶段分扣数前后；已确认之后不带 lines 的取消由调用方写待处理。
        return _IGNORED if stage == CANCELLED else _APPLY
    return _after_commit(stage, ready_at_set)


# ---- 按 SKU 合并订单行（§5.10 platform_order_lines） ----


@dataclass(frozen=True)
class CoreLine:
    line_ref: str
    sku_id: str
    quantity: int


@dataclass(frozen=True)
class LineAssignment:
    lines: tuple[CoreLine, ...]  # 按 line_ref 编号的先后
    line_refs: Mapping[str, str]  # platform_line_id 到 line_ref；剩余为 0 的行不在里面


def assign_line_refs(lines: Iterable[tuple[str, str | None, int]]) -> LineAssignment:
    """进入待发货时把剩余件数大于 0 的平台行按 sku_id 合并，按 sku_id 升序编 L1、L2…

    lines 为 (platform_line_id, sku_id, 剩余件数)。剩余件数大于 0 而 sku_id 为 None、
    剩余件数为负或 platform_line_id 重复时抛 ValueError（unmapped 由调用方先判）。
    """
    totals: dict[str, int] = {}
    sku_by_line: dict[str, str] = {}
    seen: set[str] = set()
    for platform_line_id, sku_id, remaining in lines:
        if platform_line_id in seen:
            raise ValueError(f"duplicate platform_line_id: {platform_line_id!r}")
        seen.add(platform_line_id)
        if type(remaining) is not int or remaining < 0:
            raise ValueError(f"invalid remaining quantity for {platform_line_id!r}")
        if remaining == 0:
            continue
        if sku_id is None:
            raise ValueError(f"platform line {platform_line_id!r} has no sku_id")
        totals[sku_id] = totals.get(sku_id, 0) + remaining
        sku_by_line[platform_line_id] = sku_id
    ref_by_sku = {sku_id: f"L{index}" for index, sku_id in enumerate(sorted(totals), start=1)}
    return LineAssignment(
        lines=tuple(
            CoreLine(line_ref=ref_by_sku[sku_id], sku_id=sku_id, quantity=totals[sku_id])
            for sku_id in sorted(totals)
        ),
        line_refs={
            platform_line_id: ref_by_sku[sku_id] for platform_line_id, sku_id in sku_by_line.items()
        },
    )


# ---- 派生的命令正文（§5.10，Q-100） ----


def _core_lines(lines: Iterable[CoreLine]) -> list[dict] | None:
    """预留与确认销售的行：同一 line_ref 求和（SKU 须相同）、0 件不列、按 line_ref 升序。"""
    totals: dict[str, int] = {}
    skus: dict[str, str] = {}
    for line in lines:
        if skus.setdefault(line.line_ref, line.sku_id) != line.sku_id:
            raise ValueError(f"line_ref {line.line_ref!r} maps to more than one sku_id")
        totals[line.line_ref] = totals.get(line.line_ref, 0) + line.quantity
    result = [
        {"line_ref": line_ref, "sku_id": skus[line_ref], "quantity": totals[line_ref]}
        for line_ref in sorted(totals)
        if totals[line_ref] > 0
    ]
    return result or None


def _line_ref(line_refs: Mapping[str, str], platform_line_id: str) -> str:
    line_ref = line_refs.get(platform_line_id)
    if line_ref is None:
        raise ValueError(f"platform line {platform_line_id!r} has no line_ref")
    return line_ref


def _quantity_lines(
    pairs: Iterable[tuple[str, int]], line_refs: Mapping[str, str]
) -> list[dict] | None:
    """(platform_line_id, 件数) 拆到 Core 行：同一 Core 行求和、0 件不列、按 line_ref 升序。"""
    totals: dict[str, int] = {}
    for platform_line_id, quantity in pairs:
        line_ref = _line_ref(line_refs, platform_line_id)
        totals[line_ref] = totals.get(line_ref, 0) + quantity
    result = [
        {"line_ref": line_ref, "quantity": totals[line_ref]}
        for line_ref in sorted(totals)
        if totals[line_ref] > 0
    ]
    return result or None


def _format_paid_at(ready_at: datetime) -> str:
    """YYYY-MM-DDTHH:MM:SSZ；ready_at 须是截到整秒的不带时区 UTC datetime。"""
    ready_at = _naive(ready_at, "ready_at")
    if ready_at.microsecond:
        raise ValueError("ready_at must be truncated to whole seconds")
    return (
        f"{ready_at.year:04d}-{ready_at.month:02d}-{ready_at.day:02d}"
        f"T{ready_at.hour:02d}:{ready_at.minute:02d}:{ready_at.second:02d}Z"
    )


def reservation_body(order_ref: str, lines: Iterable[CoreLine]) -> tuple[dict, dict] | None:
    """reservations.create 的 (路径参数, 正文)；整理后一行不剩为 None。"""
    core_lines = _core_lines(lines)
    if core_lines is None:
        return None
    return {}, {"order_ref": order_ref, "lines": core_lines}


def sale_commit_body(
    order_ref: str, reservation_id: str, ready_at: datetime, lines: Iterable[CoreLine]
) -> tuple[dict, dict] | None:
    """sale_commits.create 的 (路径参数, 正文)；paid_at 取 ready_at，lines 同预留。"""
    core_lines = _core_lines(lines)
    if core_lines is None:
        return None
    body = {
        "reservation_id": reservation_id,
        "payment_ref": payment_ref(order_ref),
        "paid_at": _format_paid_at(ready_at),
        "lines": core_lines,
    }
    return {"order_ref": order_ref}, body


def dispatch_body(
    order_ref: str, shipment_ref: str, payload: ShippedPayload, line_refs: Mapping[str, str]
) -> tuple[dict, dict] | None:
    """dispatches.create：件数取平台这个包裹里的件数，按对照拆到 Core 行。"""
    lines = _quantity_lines(
        ((line.platform_line_id, line.quantity) for line in payload.lines), line_refs
    )
    if lines is None:
        return None
    return {"order_ref": order_ref}, {"shipment_ref": shipment_ref, "lines": lines}


def refund_restock_body(
    order_ref: str, refund_ref: str, payload: CancelledPayload, line_refs: Mapping[str, str]
) -> tuple[dict, dict] | None:
    """refund_restocks.create：件数取平台这次取消的件数，不取 Core 当前的余量。

    payload 须带 refund_id 与 lines（订单级取消由调用方先判、写待处理），否则抛 ValueError。
    """
    if payload.refund_id is None or payload.lines is None:
        raise ValueError("refund restock needs refund_id and lines")
    lines = _quantity_lines(
        ((line.platform_line_id, line.quantity) for line in payload.lines), line_refs
    )
    if lines is None:
        return None
    return {"order_ref": order_ref}, {"refund_ref": refund_ref, "lines": lines}


def return_receipt_body(
    order_ref: str,
    return_ref: str,
    payload: ReturnReceivedPayload,
    line_refs: Mapping[str, str],
) -> tuple[dict, dict] | None:
    """return_receipts.create：取管理员登记的可售与不可售件数，同一 Core 行分别求和。"""
    totals: dict[str, tuple[int, int]] = {}
    for line in payload.lines:
        line_ref = _line_ref(line_refs, line.platform_line_id)
        sellable, unsellable = totals.get(line_ref, (0, 0))
        totals[line_ref] = (
            sellable + line.sellable_quantity,
            unsellable + line.unsellable_quantity,
        )
    lines = [
        {
            "line_ref": line_ref,
            "sellable_quantity": totals[line_ref][0],
            "unsellable_quantity": totals[line_ref][1],
        }
        for line_ref in sorted(totals)
        if sum(totals[line_ref]) > 0
    ]
    if not lines:
        return None
    return {"order_ref": order_ref}, {"return_ref": return_ref, "lines": lines}


# ---- 补拉退避 ----


def pull_backoff(attempts: int) -> timedelta:
    """已补拉过 attempts 次后，下一次补拉前要等的时间：min(2^attempts, 60) 分钟。"""
    if type(attempts) is not int or attempts < 0:
        raise ValueError(f"attempts must be a non-negative integer, got {attempts!r}")
    # 2^6 已超过 60，不再算幂，免得次数很大时算大整数。
    if attempts >= 6:
        return timedelta(minutes=PULL_BACKOFF_MAX_MINUTES)
    return timedelta(minutes=min(2**attempts, PULL_BACKOFF_MAX_MINUTES))
