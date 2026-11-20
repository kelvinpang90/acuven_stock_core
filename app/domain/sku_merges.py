"""SKU 合并不碰数据库的规则（契约 §2.4、§2.8）。

锁行、读写合并单与余额行由上层在数据库里做；这里只放状态常量、各命令按合并单状态的判定、
停售判定、正文校验与执行时的迁移和定数。正文不合规抛 InvalidField（带字段名，接口回 422）；
定数落账后违反不变量抛 CountBelowCommitments（接口回 409 COUNT_BELOW_COMMITMENTS）。
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from app.domain.catalog import REASON, InvalidField, normalize_reason
from app.domain.ledger import apply_count_adjustment, count_deltas, is_stale
from app.domain.quantities import Quantities
from app.domain.stock import parse_non_negative_quantity

# ---- 合并单状态（§2.4、§2.8 表示） ----

SELLING_PAUSED = "selling_paused"
COUNTED = "counted"
RECONCILE_PENDING = "reconcile_pending"
COMPLETED = "completed"
CANCELLED = "cancelled"
STATUSES = (SELLING_PAUSED, COUNTED, RECONCILE_PENDING, COMPLETED, CANCELLED)

# 未结束的合并单：一个 SKU 同时最多属于一张（§2.4 合并单互不重叠）。
OPEN_STATUSES = frozenset({SELLING_PAUSED, COUNTED, RECONCILE_PENDING})

# new_id 的前缀：合并单 smg_…。
MERGE_ID_PREFIX = "smg"
# 执行写的三条流水的 source_type（§2.8 执行的流水）。
SOURCE_TYPE = "sku_merge"

# ---- 正文字段（§2.8） ----

SOURCE_SKU_ID = "source_sku_id"
CANONICAL_SKU_ID = "canonical_sku_id"
POOL_ID = "pool_id"
SHELF_COUNT = "shelf_count"
UNSELLABLE_COUNT = "unsellable_count"

CREATE_FIELDS = frozenset({SOURCE_SKU_ID, CANONICAL_SKU_ID, REASON})
COUNT_FIELDS = frozenset({POOL_ID, SHELF_COUNT, UNSELLABLE_COUNT})

# ---- SKU 在合并单上的角色 ----

SOURCE = "source"
CANONICAL = "canonical"


class CommandDecision(StrEnum):
    EXECUTE = "execute"  # 照命令改合并单
    CURRENT = "current"  # 200，返回当前表示，不改任何东西
    CONFLICT = "conflict"  # 409 STATE_CONFLICT，details 为 {"status": 合并单状态}


def _require_status(status):
    if status not in STATUSES:
        raise ValueError(f"unknown sku merge status: {status!r}")


def _decide(status, execute: Iterable[str], current: Iterable[str]) -> CommandDecision:
    _require_status(status)
    if status in execute:
        return CommandDecision.EXECUTE
    if status in current:
        return CommandDecision.CURRENT
    return CommandDecision.CONFLICT


# ---- 各命令按合并单状态的判定（§2.8）；未知状态抛 ValueError ----


def count_decision(status: str) -> CommandDecision:
    """录入实数：selling_paused、counted 录入（重新录入整份替换），其余 409。

    pool_id 与原来不同的 409（reason pool_mismatch）由调用方在这之后另判。
    """
    return _decide(status, (SELLING_PAUSED, COUNTED), ())


def execute_decision(status: str) -> CommandDecision:
    """执行：counted 执行；reconcile_pending、completed 返回当前表示；其余 409。

    锁内复核池的 403 在这之前由调用方判（§2.8 执行第 1 步）。
    """
    return _decide(status, (COUNTED,), (RECONCILE_PENDING, COMPLETED))


def complete_decision(status: str) -> CommandDecision:
    """完成：reconcile_pending 改 completed；已是 completed 返回当前表示；其余 409。"""
    return _decide(status, (RECONCILE_PENDING,), (COMPLETED,))


def cancel_decision(status: str) -> CommandDecision:
    """取消：selling_paused、counted 改 cancelled；已是 cancelled 返回当前表示；其余 409。"""
    return _decide(status, (SELLING_PAUSED, COUNTED), (CANCELLED,))


def pauses_selling(status: str, role: str) -> bool:
    """这张合并单是否使该 SKU 停售（§2.8 停售的判定）。

    selling_paused、counted 停两个 SKU；reconcile_pending 只停规范 SKU
    （来源已合并，按已合并处理）；completed、cancelled 不停售。
    role 为 SOURCE 或 CANONICAL，其他值抛 ValueError。
    """
    _require_status(status)
    if role not in (SOURCE, CANONICAL):
        raise ValueError(f"unknown sku merge role: {role!r}")
    if status in (SELLING_PAUSED, COUNTED):
        return True
    return status == RECONCILE_PENDING and role == CANONICAL


# ---- 正文校验（§2.8 新建、录入实数） ----


def _require_fields(body, allowed: frozenset[str], required: Iterable[str]) -> None:
    """正文须是对象、只带 allowed 里的字段、带齐 required；否则按字段名抛 InvalidField。"""
    if not isinstance(body, Mapping):
        raise InvalidField("body")
    for field in body:
        if field not in allowed:
            raise InvalidField(field)
    for field in required:
        if field not in body:
            raise InvalidField(field)


def _require_str(body: Mapping, field: str) -> str:
    value = body[field]
    if not isinstance(value, str):
        raise InvalidField(field)
    return value


def parse_create_body(body) -> dict:
    """新建正文：source_sku_id、canonical_sku_id 是字符串且不同，reason 可选（§2.6 第 1 条）。

    返回三项都在的 dict，reason 规范化后（没带为 None）。
    不校验 ID 格式，查不到由事务里报 404。
    """
    _require_fields(body, CREATE_FIELDS, (SOURCE_SKU_ID, CANONICAL_SKU_ID))
    source_sku_id = _require_str(body, SOURCE_SKU_ID)
    canonical_sku_id = _require_str(body, CANONICAL_SKU_ID)
    if source_sku_id == canonical_sku_id:
        raise InvalidField(CANONICAL_SKU_ID)
    return {
        SOURCE_SKU_ID: source_sku_id,
        CANONICAL_SKU_ID: canonical_sku_id,
        REASON: normalize_reason(body.get(REASON)),
    }


def parse_count_body(body) -> dict:
    """录入实数正文：pool_id 是字符串，两项实数是 0–1,000,000 的 JSON 整数（§6.2 第 5 条）。"""
    _require_fields(body, COUNT_FIELDS, (POOL_ID, SHELF_COUNT, UNSELLABLE_COUNT))
    return {
        POOL_ID: _require_str(body, POOL_ID),
        SHELF_COUNT: parse_non_negative_quantity(body[SHELF_COUNT], SHELF_COUNT),
        UNSELLABLE_COUNT: parse_non_negative_quantity(body[UNSELLABLE_COUNT], UNSELLABLE_COUNT),
    }


# ---- 快照作废（§2.4 第 0 步后半、§2.8 执行第 6 步） ----


@dataclass(frozen=True)
class SnapshotVersions:
    """一个 SKU 锁住的余额行上的 last_book_only_version 与它的快照版本。"""

    sku_id: str
    last_book_only_version: int
    snapshot_version: int


def stale_sku_ids(source: SnapshotVersions, canonical: SnapshotVersions) -> list[str]:
    """快照之后有只改账流水的 SKU，按 sku_id 升序。

    非空即 409 COUNT_STALE，列表就是 details.sku_ids。
    """
    return sorted(
        versions.sku_id
        for versions in (source, canonical)
        if is_stale(versions.last_book_only_version, versions.snapshot_version)
    )


# ---- 迁移与定数（§2.4 第 1–3 步） ----


def _add(left: Quantities, right: Quantities) -> Quantities:
    # 两个各自满足不变量的账面相加仍满足不变量（§2.4 第 1 步）。
    return Quantities(
        left.stock_balance + right.stock_balance,
        left.reserved + right.reserved,
        left.unavailable + right.unavailable,
        left.paid_pending_dispatch + right.paid_pending_dispatch,
    )


def merge_deltas(
    source_snapshot: Quantities,
    canonical_snapshot: Quantities,
    shelf_count: int,
    unsellable_count: int,
) -> tuple[int, int]:
    """(d_shelf, d_u)：两项合计实数与两个快照之和的差额（§2.4 第 2 步）。

    表示里 count 的 d_shelf、d_u 也用它算。
    """
    return count_deltas(_add(source_snapshot, canonical_snapshot), shelf_count, unsellable_count)


@dataclass(frozen=True)
class MergeExecution:
    """一次执行的落账结果。

    transfer 是 MERGE_TRANSFER_OUT / IN 的迁移量，即来源执行时的四个量，
    全为 0 时照样写这一对流水；
    canonical_after_transfer 是迁移后的规范四个量；canonical_after 是定数后的规范四个量，
    writes_adjustment 为 False 时（两个差额都为 0）不写 COUNT_ADJUSTMENT，与迁移后相同。
    来源执行后归零。
    """

    transfer: Quantities
    canonical_after_transfer: Quantities
    d_shelf: int
    d_u: int
    canonical_after: Quantities
    writes_adjustment: bool


def plan_execution(
    source_snapshot: Quantities,
    canonical_snapshot: Quantities,
    shelf_count: int,
    unsellable_count: int,
    source_current: Quantities,
    canonical_current: Quantities,
) -> MergeExecution:
    """按 §2.4 第 1–3 步算执行的迁移与定数；current 是锁住的余额行上执行时的四个量。

    差额加到迁移后的规范账面上；落账后违反不变量时抛 CountBelowCommitments，
    带落账后规范 SKU 的四个量与 §6 的缺口数（details 只有规范 SKU 一项）。
    """
    snapshot = _add(source_snapshot, canonical_snapshot)
    d_shelf, d_u = count_deltas(snapshot, shelf_count, unsellable_count)
    after_transfer = _add(canonical_current, source_current)
    writes_adjustment = d_shelf != 0 or d_u != 0
    after = after_transfer
    if writes_adjustment:
        after = apply_count_adjustment(after_transfer, snapshot, shelf_count, unsellable_count)
    return MergeExecution(
        transfer=source_current,
        canonical_after_transfer=after_transfer,
        d_shelf=d_shelf,
        d_u=d_u,
        canonical_after=after,
        writes_adjustment=writes_adjustment,
    )
