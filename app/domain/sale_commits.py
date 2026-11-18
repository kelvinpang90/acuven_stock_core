"""确认销售、晚到支付、待审结案与故障补交不碰数据库的规则（契约 §5.7，§5.2 第 5–7 条）。

锁行、查合并去向与可售量、写预留与流水由上层在数据库里做；这里只放常量与判定。
状态取值与到期判定沿用 reservations，流水类型沿用 ledger，请求摘要沿用 idempotency。
"""

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from enum import StrEnum

from app.domain.idempotency import request_digest
from app.domain.ledger import EntryType
from app.domain.reservations import (
    ACTIVE,
    CLOSED_REFUNDED,
    COMMITTED,
    EXPIRED,
    LATE_PAYMENT_REVIEW,
    RELEASED,
    is_due,
)

# ---- 流水（§5.7 通用） ----

SOURCE_TYPE = "sale_commit"
LATE_PAYMENT_REASON_CODE = "late_payment"
FAULT_MODE_REASON_CODE = "fault_mode"


class CommitDecision(StrEnum):
    COMMIT = "commit"  # active 且未到期：正常确认
    EXPIRE_THEN_LATE = "expire_then_late"  # active 而已到期：先到期关闭，再晚到确认
    LATE = "late"  # expired，或待审且 payment_ref 相同：晚到确认
    PAYMENT_REF_CONFLICT = "payment_ref_conflict"  # 待审而 payment_ref 不同：409 STATE_CONFLICT
    ANSWER_EXISTING = "answer_existing"  # 已确认且 payment_ref 相同：按第 1 步回答
    STATE_CONFLICT = "state_conflict"  # 409 STATE_CONFLICT，不改任何东西


def merge_destination(sku_id: str, merged_into: Mapping[str, str | None]) -> str | None:
    """顺着 merged_into 走到未合并的 SKU（§5.7 合并去向）；没合并过的就是它自己。

    merged_into 是 sku_id 到 merged_into_sku_id（未合并为 None）的映射。
    sku_id 不在映射里、或链走到不在映射里的 SKU 时返回 None；成环抛 ValueError。
    """
    current = sku_id
    steps = 0
    while True:
        if current not in merged_into:
            return None
        target = merged_into[current]
        if target is None:
            return current
        steps += 1
        if steps > len(merged_into):
            raise ValueError(f"merge chain from {sku_id!r} has a cycle")
        current = target


def mismatched_line_refs(
    request_lines: Iterable[tuple[str, str, int]],
    order_lines: Iterable[tuple[str, str, int]],
    destinations: Mapping[str, str | None],
) -> list[str]:
    """行集比对（§5.7 第 3 步，Q-59）：不一致的、请求里多出的与缺少的 line_ref，升序。

    两组都是 (line_ref, sku_id, quantity)；
    destinations 是 sku_id 到合并去向（没有为 None）的映射。
    请求 SKU 没有去向时这一行算不一致；全部一致时为空列表。
    """
    requested = {line_ref: (sku_id, quantity) for line_ref, sku_id, quantity in request_lines}
    existing = {line_ref: (sku_id, quantity) for line_ref, sku_id, quantity in order_lines}
    mismatched = set(requested.keys() ^ existing.keys())
    for line_ref in requested.keys() & existing.keys():
        request_sku, request_quantity = requested[line_ref]
        order_sku, order_quantity = existing[line_ref]
        request_destination = destinations.get(request_sku)
        if (
            request_quantity != order_quantity
            or request_destination is None
            or request_destination != destinations.get(order_sku)
        ):
            mismatched.add(line_ref)
    return sorted(mismatched)


def commit_decision(
    status: str,
    expires_at: datetime,
    now: datetime,
    recorded_payment_ref: str | None,
    payment_ref: str,
) -> CommitDecision:
    """确认销售按锁住的预留行状态的判定（§5.7 第 4 步，Q-57）；未知状态抛 ValueError。"""
    if status == ACTIVE:
        if is_due(status, expires_at, now):
            return CommitDecision.EXPIRE_THEN_LATE
        return CommitDecision.COMMIT
    if status == EXPIRED:
        return CommitDecision.LATE
    if status == LATE_PAYMENT_REVIEW:
        if recorded_payment_ref == payment_ref:
            return CommitDecision.LATE
        return CommitDecision.PAYMENT_REF_CONFLICT
    if status == COMMITTED:
        if recorded_payment_ref == payment_ref:
            return CommitDecision.ANSWER_EXISTING
        return CommitDecision.STATE_CONFLICT
    if status in (RELEASED, CLOSED_REFUNDED):
        return CommitDecision.STATE_CONFLICT
    raise ValueError(f"unknown reservation status: {status!r}")


def sku_shortfalls(
    lines: Sequence[tuple[str, str, int]], available_by_sku: Mapping[str, int]
) -> list[dict]:
    """按 SKU 合计判可售（§5.7）：所在 SKU 合计超过 available 的全部行，按请求顺序。

    lines 为 (line_ref, sku_id, quantity)；
    每项 line_ref、sku_id、quantity、available（该 SKU 的可售量），
    即 LATE_PAYMENT_INSUFFICIENT_STOCK 的 details.lines；都够时为空列表。
    """
    totals: dict[str, int] = {}
    for _, sku_id, quantity in lines:
        totals[sku_id] = totals.get(sku_id, 0) + quantity
    shortfalls = []
    for line_ref, sku_id, quantity in lines:
        available = available_by_sku[sku_id]
        if totals[sku_id] > available:
            shortfalls.append(
                {
                    "line_ref": line_ref,
                    "sku_id": sku_id,
                    "quantity": quantity,
                    "available": available,
                }
            )
    return shortfalls


def commit_reason_code(entry_type: EntryType, fault_mode: bool) -> str | None:
    """确认流水的 reason_code（§5.7 通用）。

    故障补交为 fault_mode，否则晚到为 late_payment、正常为空；其他流水类型抛 ValueError。
    """
    if entry_type not in (EntryType.SALE_COMMIT, EntryType.LATE_SALE_COMMIT):
        raise ValueError(f"{entry_type} is not a sale commit entry")
    if fault_mode:
        return FAULT_MODE_REASON_CODE
    if entry_type == EntryType.LATE_SALE_COMMIT:
        return LATE_PAYMENT_REASON_CODE
    return None


def reserve_request_digest(order_ref: str, lines: Iterable[tuple[str, str, int]]) -> str:
    """“同内容的预留请求”的摘要（§5.7 第 5 步），作故障补交新建预留的 request_hash。

    lines 为 (line_ref, sku_id, quantity)；path 为 {}，body 为 {"order_ref": …, "lines": […]}。
    """
    body = {
        "order_ref": order_ref,
        "lines": [
            {"line_ref": line_ref, "sku_id": sku_id, "quantity": quantity}
            for line_ref, sku_id, quantity in lines
        ],
    }
    return request_digest({}, body)
