"""SKU 合并的新建、录入实数、完成、取消与查询（契约 §2.4、§2.8）。

写命令都在幂等执行器以 READ COMMITTED 开的事务里、用 execution.connection 运行，
自己不开、不提交、不回滚事务。按状态的判定与差额公式用 app/domain/sku_merges.py。

新建（执行器已取目录写锁）：普通读查两个 SKU，不存在或属于别的租户 404
→ 按 sku_id 升序 FOR UPDATE 锁两个 SKU 行 → 普通读查涉及这两个 SKU 的未结束合并单：
同一对的返回 200 与它的当前表示 → 已合并 409 → 规范停用 409
→ 别的未结束单 409 SKU_MERGE_IN_PROGRESS → 写合并单，201。
重叠检查用普通读：READ COMMITTED 下看得到锁 SKU 行之前已提交的合并单，
涉及同一个 SKU 的两笔新建在 SKU 行锁上排队，所以“合并单互不重叠”不靠数据库约束也成立。

录入实数（执行器已按正文的池预建两个余额行）：
FOR UPDATE 读合并单行，不存在或属于别的租户 404
→ 状态不是 selling_paused、counted 409 → 已录入过而池不同 409（pool_mismatch）
→ FOR SHARE 读池行，不是 live 409 → 按 (pool_id, sku_id) 升序锁两个余额行，
记下快照，整份替换已有的快照，合并单改为 counted。不写流水，不查 SKU 的状态。

完成与取消：FOR UPDATE 读合并单行，不存在或属于别的租户 404，再按状态判定。
三条命令的 404 都在事务里判出，作为确定结果保存；
属于别的租户的经执行器登记 cross_tenant。

锁序：合并单行 → 池行（FOR SHARE）→ 余额行（FOR UPDATE）；新建只锁 SKU 行。

表示由 merge_representation 从合并单行与快照算出，它不读库：
写命令把锁内读到的行加上本次的改动传进去，查询接口用普通读读出后传进去。
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, fields, replace
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, delete, insert, or_, select, update

from app.api.errors import ApiError
from app.domain.catalog import INACTIVE
from app.domain.ids import new_id
from app.domain.quantities import Quantities
from app.domain.requests import Actor
from app.domain.sku_merges import (
    CANCELLED,
    COMPLETED,
    COUNTED,
    MERGE_ID_PREFIX,
    OPEN_STATUSES,
    SELLING_PAUSED,
    CommandDecision,
    cancel_decision,
    complete_decision,
    count_decision,
    merge_deltas,
)
from app.models.catalog import Sku
from app.models.inventory import QUANTITIES
from app.models.merges import SkuMerge, SkuMergeSnapshot
from app.services.idempotency import CommandResult, Execution
from app.services.security_events import CROSS_TENANT
from app.services.stock import (
    SKU_MERGED,
    LockedBalance,
    command_now,
    format_timestamp,
    lock_balances,
    require_live,
    require_skus_exist,
)

# 对象类型与错误码（§2.8、§8.3）。
SKU_MERGE = "sku_merge"
SKU_MERGE_NOT_FOUND = "SKU_MERGE_NOT_FOUND"
SKU_MERGE_IN_PROGRESS = "SKU_MERGE_IN_PROGRESS"
SKU_INACTIVE = "SKU_INACTIVE"
STATE_CONFLICT = "STATE_CONFLICT"
POOL_MISMATCH = "pool_mismatch"


# --- 读出的行 ---


@dataclass(frozen=True)
class MergeRecord:
    """sku_merges 的一行（表示与判定要用的列）。"""

    merge_id: str
    source_sku_id: str
    canonical_sku_id: str
    status: str
    reason: str | None
    pool_id: str | None
    created_at: datetime
    shelf_count: int | None
    unsellable_count: int | None
    counted_at: datetime | None
    executed_at: datetime | None
    transfer_out_entry_id: str | None
    transfer_in_entry_id: str | None
    adjustment_entry_id: str | None
    repointed_line_count: int | None
    completed_at: datetime | None
    cancelled_at: datetime | None

    @property
    def sku_ids(self) -> tuple[str, str]:
        return self.source_sku_id, self.canonical_sku_id


@dataclass(frozen=True)
class SnapshotRecord:
    """sku_merge_snapshots 的一行：录入实数时一个 SKU 余额行的四个量与版本。"""

    sku_id: str
    stock_balance: int
    reserved: int
    unavailable: int
    paid_pending_dispatch: int
    balance_version: int

    @property
    def quantities(self) -> Quantities:
        return Quantities(*(getattr(self, name) for name in QUANTITIES))


_MERGE_COLUMNS = tuple(getattr(SkuMerge, field.name) for field in fields(MergeRecord))
_SNAPSHOT_COLUMNS = tuple(getattr(SkuMergeSnapshot, field.name) for field in fields(SnapshotRecord))


def _merge_query(tenant_id: str):
    return select(*_MERGE_COLUMNS).where(SkuMerge.tenant_id == tenant_id)


def _merge(row) -> MergeRecord | None:
    return None if row is None else MergeRecord(**row)


def read_merge(connection: Connection, tenant_id: str, merge_id: str) -> MergeRecord | None:
    """普通读按租户读合并单；不存在或属于别的租户为 None。"""
    statement = _merge_query(tenant_id).where(SkuMerge.merge_id == merge_id)
    return _merge(connection.execute(statement).mappings().first())


def lock_merge(connection: Connection, tenant_id: str, merge_id: str) -> MergeRecord | None:
    """同 read_merge，用 SELECT … FOR UPDATE 读。"""
    statement = _merge_query(tenant_id).where(SkuMerge.merge_id == merge_id).with_for_update()
    return _merge(connection.execute(statement).mappings().first())


def merge_owner(connection: Connection, merge_id: str) -> str | None:
    """合并单所属的租户；不存在为 None。"""
    statement = select(SkuMerge.tenant_id).where(SkuMerge.merge_id == merge_id)
    return connection.execute(statement).scalar_one_or_none()


def merge_sku_ids(connection: Connection, tenant_id: str, merge_id: str) -> tuple[str, ...]:
    """录入实数预建余额行用：普通读按租户取合并单的两个 SKU；不存在或属于别的租户为空。"""
    merge = read_merge(connection, tenant_id, merge_id)
    return () if merge is None else merge.sku_ids


def read_snapshots(connection: Connection, tenant_id: str, merge_id: str) -> list[SnapshotRecord]:
    """普通读读合并单的快照，按 sku_id 升序。"""
    statement = (
        select(*_SNAPSHOT_COLUMNS)
        .where(SkuMergeSnapshot.tenant_id == tenant_id, SkuMergeSnapshot.merge_id == merge_id)
        .order_by(SkuMergeSnapshot.sku_id)
    )
    return [SnapshotRecord(**row) for row in connection.execute(statement).mappings()]


# --- 表示（§2.8） ---


def _timestamp(value: datetime | None) -> str | None:
    return None if value is None else format_timestamp(value)


def _count_representation(
    merge: MergeRecord, snapshots: Iterable[SnapshotRecord]
) -> dict[str, Any] | None:
    """录入实数前为 None；之后带实数、快照（按 sku_id 升序）与 §2.4 第 2 步的两个差额。"""
    if merge.counted_at is None:
        return None
    by_sku = {snapshot.sku_id: snapshot for snapshot in snapshots}
    d_shelf, d_u = merge_deltas(
        by_sku[merge.source_sku_id].quantities,
        by_sku[merge.canonical_sku_id].quantities,
        merge.shelf_count,
        merge.unsellable_count,
    )
    return {
        "shelf_count": merge.shelf_count,
        "unsellable_count": merge.unsellable_count,
        "counted_at": format_timestamp(merge.counted_at),
        "snapshots": [
            {
                "sku_id": snapshot.sku_id,
                **{name: getattr(snapshot, name) for name in QUANTITIES},
                "balance_version": snapshot.balance_version,
            }
            for snapshot in sorted(by_sku.values(), key=lambda snapshot: snapshot.sku_id)
        ],
        "d_shelf": d_shelf,
        "d_u": d_u,
    }


def _execution_representation(merge: MergeRecord) -> dict[str, Any] | None:
    if merge.executed_at is None:
        return None
    return {
        "executed_at": format_timestamp(merge.executed_at),
        "transfer_out_entry_id": merge.transfer_out_entry_id,
        "transfer_in_entry_id": merge.transfer_in_entry_id,
        "adjustment_entry_id": merge.adjustment_entry_id,
        "repointed_line_count": merge.repointed_line_count,
    }


def merge_representation(merge: MergeRecord, snapshots: Iterable[SnapshotRecord]) -> dict:
    """合并单的表示：键固定且都出现。不读库。"""
    return {
        "merge_id": merge.merge_id,
        "source_sku_id": merge.source_sku_id,
        "canonical_sku_id": merge.canonical_sku_id,
        "status": merge.status,
        "reason": merge.reason,
        "pool_id": merge.pool_id,
        "created_at": format_timestamp(merge.created_at),
        "count": _count_representation(merge, snapshots),
        "execution": _execution_representation(merge),
        "completed_at": _timestamp(merge.completed_at),
        "cancelled_at": _timestamp(merge.cancelled_at),
    }


def current_representation(connection: Connection, tenant_id: str, merge_id: str) -> dict | None:
    """普通读读出合并单与快照后的表示；不存在或属于别的租户为 None。"""
    merge = read_merge(connection, tenant_id, merge_id)
    if merge is None:
        return None
    return merge_representation(merge, read_snapshots(connection, tenant_id, merge_id))


# --- 共用的判定 ---


def _tenant_id(execution: Execution) -> str:
    return execution.access.principal.tenant_id


def _actor(execution: Execution) -> Actor:
    """合并单命令都要求角色（§10 第 3 条），操作者必在。"""
    actor = execution.access.actor
    if actor is None:
        raise RuntimeError("a sku merge command needs the actor headers")
    return actor


def _signature(execution: Execution, prefix: str, now: datetime) -> dict[str, Any]:
    """命令记在合并单上的凭据、操作者、角色、请求 ID 与时间，列名带 prefix。"""
    actor = _actor(execution)
    return {
        f"{prefix}_credential_id": execution.access.principal.credential_id,
        f"{prefix}_actor_ref": actor.ref,
        f"{prefix}_actor_role": actor.role,
        f"{prefix}_request_id": execution.request.state.request_id,
        f"{prefix}_at": now,
    }


def _current_result(execution: Execution, merge: MergeRecord) -> CommandResult:
    snapshots = read_snapshots(execution.connection, _tenant_id(execution), merge.merge_id)
    return CommandResult(merge_representation(merge, snapshots))


def _lock_existing_merge(execution: Execution, merge_id: str) -> MergeRecord:
    """FOR UPDATE 读合并单行；不存在或属于别的租户 404 SKU_MERGE_NOT_FOUND，
    属于别的租户的经执行器登记 cross_tenant。
    """
    connection = execution.connection
    merge = lock_merge(connection, _tenant_id(execution), merge_id)
    if merge is not None:
        return merge
    if merge_owner(connection, merge_id) is not None:
        execution.record_security_event(CROSS_TENANT, {"object_type": SKU_MERGE, "id": merge_id})
    raise ApiError(404, SKU_MERGE_NOT_FOUND, f"{SKU_MERGE} not found")


def _state_conflict(merge: MergeRecord, **extra: str) -> ApiError:
    details = {"status": merge.status, **extra}
    message = "the sku merge is in another state"
    return ApiError(409, STATE_CONFLICT, message, details=details)


def _set_status(
    execution: Execution, merge: MergeRecord, status: str, values: dict[str, Any]
) -> None:
    execution.connection.execute(
        update(SkuMerge)
        .where(SkuMerge.tenant_id == _tenant_id(execution), SkuMerge.merge_id == merge.merge_id)
        .values(status=status, **values)
    )


# --- 新建 ---


@dataclass(frozen=True)
class _LockedSku:
    sku_id: str
    status: str
    merged_into_sku_id: str | None


def _lock_skus(execution: Execution, sku_ids: Sequence[str]) -> dict[str, _LockedSku]:
    """按 sku_id 升序 FOR UPDATE 锁 SKU 行。之前已查过存在与归属，读不到是程序错误。"""
    tenant_id = _tenant_id(execution)
    locked: dict[str, _LockedSku] = {}
    for sku_id in sorted(set(sku_ids)):
        statement = (
            select(Sku.sku_id, Sku.status, Sku.merged_into_sku_id)
            .where(Sku.tenant_id == tenant_id, Sku.sku_id == sku_id)
            .with_for_update()
        )
        row = execution.connection.execute(statement).mappings().first()
        if row is None:
            raise RuntimeError(f"sku {sku_id} is not in the tenant")
        locked[sku_id] = _LockedSku(**row)
    return locked


def _open_merges(execution: Execution, sku_ids: Sequence[str]) -> list[MergeRecord]:
    """普通读查任一 SKU 作来源或规范的未结束合并单，按 merge_id 升序。"""
    statement = (
        _merge_query(_tenant_id(execution))
        .where(
            SkuMerge.status.in_(sorted(OPEN_STATUSES)),
            or_(SkuMerge.source_sku_id.in_(sku_ids), SkuMerge.canonical_sku_id.in_(sku_ids)),
        )
        .order_by(SkuMerge.merge_id)
    )
    return [MergeRecord(**row) for row in execution.connection.execute(statement).mappings()]


def _require_not_merged(skus: dict[str, _LockedSku], sku_ids: Sequence[str]) -> None:
    """按给定顺序（先来源）第一个已合并的：409 SKU_MERGED。"""
    for sku_id in sku_ids:
        merged_into = skus[sku_id].merged_into_sku_id
        if merged_into is not None:
            details = {"sku_id": sku_id, "merged_into_sku_id": merged_into}
            raise ApiError(409, SKU_MERGED, "sku has been merged", details=details)


def _require_active(sku: _LockedSku) -> None:
    """只看 SKU 自己的状态，不看商品。"""
    if sku.status == INACTIVE:
        details = {"sku_ids": [sku.sku_id]}
        raise ApiError(409, SKU_INACTIVE, "sku is inactive", details=details)


def _merge_in_progress(merge: MergeRecord) -> ApiError:
    details = {
        "merge_id": merge.merge_id,
        "source_sku_id": merge.source_sku_id,
        "canonical_sku_id": merge.canonical_sku_id,
    }
    return ApiError(
        409, SKU_MERGE_IN_PROGRESS, "a sku is in another open sku merge", details=details
    )


def create_merge(
    execution: Execution, source_sku_id: str, canonical_sku_id: str, reason: str | None
) -> CommandResult:
    """新建合并单，状态 selling_paused，201 与表示。

    同一对 SKU 已有未结束的单时 200 与它的当前表示，reason 不同也不改它。
    """
    connection = execution.connection
    now = command_now(execution)
    sku_ids = (source_sku_id, canonical_sku_id)
    require_skus_exist(execution, sku_ids)
    skus = _lock_skus(execution, sku_ids)
    open_merges = _open_merges(execution, sku_ids)
    for merge in open_merges:
        if merge.sku_ids == sku_ids:
            return _current_result(execution, merge)
    _require_not_merged(skus, sku_ids)
    _require_active(skus[canonical_sku_id])
    if open_merges:
        raise _merge_in_progress(open_merges[0])

    actor = _actor(execution)
    record = MergeRecord(
        merge_id=new_id(MERGE_ID_PREFIX),
        source_sku_id=source_sku_id,
        canonical_sku_id=canonical_sku_id,
        status=SELLING_PAUSED,
        reason=reason,
        pool_id=None,
        created_at=now,
        shelf_count=None,
        unsellable_count=None,
        counted_at=None,
        executed_at=None,
        transfer_out_entry_id=None,
        transfer_in_entry_id=None,
        adjustment_entry_id=None,
        repointed_line_count=None,
        completed_at=None,
        cancelled_at=None,
    )
    connection.execute(
        insert(SkuMerge).values(
            merge_id=record.merge_id,
            tenant_id=_tenant_id(execution),
            source_sku_id=source_sku_id,
            canonical_sku_id=canonical_sku_id,
            status=record.status,
            reason=reason,
            credential_id=execution.access.principal.credential_id,
            actor_ref=actor.ref,
            actor_role=actor.role,
            request_id=execution.request.state.request_id,
            created_at=now,
        )
    )
    return CommandResult(merge_representation(record, []), 201)


# --- 录入实数 ---


def _snapshot(balance: LockedBalance) -> SnapshotRecord:
    """记下余额行当时的四个量与版本（同 app/services/counts.py 提交实数）。"""
    return SnapshotRecord(
        sku_id=balance.sku_id,
        **{name: getattr(balance, name) for name in QUANTITIES},
        balance_version=balance.balance_version,
    )


def _replace_snapshots(
    execution: Execution, merge_id: str, pool_id: str, snapshots: Sequence[SnapshotRecord]
) -> None:
    """整份替换（还没落账，不留历史）。"""
    connection = execution.connection
    tenant_id = _tenant_id(execution)
    connection.execute(
        delete(SkuMergeSnapshot).where(
            SkuMergeSnapshot.tenant_id == tenant_id, SkuMergeSnapshot.merge_id == merge_id
        )
    )
    for snapshot in snapshots:
        values = {field.name: getattr(snapshot, field.name) for field in fields(SnapshotRecord)}
        identity = {"merge_id": merge_id, "tenant_id": tenant_id, "pool_id": pool_id}
        connection.execute(insert(SkuMergeSnapshot).values(values | identity))


def count_merge(
    execution: Execution, merge_id: str, pool_id: str, shelf_count: int, unsellable_count: int
) -> CommandResult:
    """录入两个 SKU 合计的实数并记快照，合并单改为 counted；200 与表示。不写流水。

    两个余额行由执行器按正文的池预建，所以还没有余额行的 SKU 记全零快照。
    """
    now = command_now(execution)
    merge = _lock_existing_merge(execution, merge_id)
    if count_decision(merge.status) is not CommandDecision.EXECUTE:
        raise _state_conflict(merge)
    if merge.pool_id is not None and merge.pool_id != pool_id:
        raise _state_conflict(merge, reason=POOL_MISMATCH)
    require_live(execution, pool_id)
    balances = lock_balances(execution, pool_id, merge.sku_ids)
    snapshots = [_snapshot(balances[sku_id]) for sku_id in sorted(merge.sku_ids)]

    _replace_snapshots(execution, merge_id, pool_id, snapshots)
    values = {
        "pool_id": pool_id,
        "shelf_count": shelf_count,
        "unsellable_count": unsellable_count,
        **_signature(execution, "counted", now),
    }
    _set_status(execution, merge, COUNTED, values)
    counted = replace(
        merge,
        status=COUNTED,
        pool_id=pool_id,
        shelf_count=shelf_count,
        unsellable_count=unsellable_count,
        counted_at=now,
    )
    return CommandResult(merge_representation(counted, snapshots))


# --- 完成与取消 ---


def complete_merge(execution: Execution, merge_id: str) -> CommandResult:
    """reconcile_pending 改为 completed；已是 completed 返回当前表示；其他状态 409。"""
    now = command_now(execution)
    merge = _lock_existing_merge(execution, merge_id)
    decision = complete_decision(merge.status)
    if decision is CommandDecision.CONFLICT:
        raise _state_conflict(merge)
    if decision is CommandDecision.EXECUTE:
        _set_status(execution, merge, COMPLETED, _signature(execution, "completed", now))
        merge = replace(merge, status=COMPLETED, completed_at=now)
    return _current_result(execution, merge)


def cancel_merge(execution: Execution, merge_id: str) -> CommandResult:
    """selling_paused、counted 改为 cancelled，实数与快照留着备查；
    已是 cancelled 返回当前表示；reconcile_pending、completed 409。
    """
    now = command_now(execution)
    merge = _lock_existing_merge(execution, merge_id)
    decision = cancel_decision(merge.status)
    if decision is CommandDecision.CONFLICT:
        raise _state_conflict(merge)
    if decision is CommandDecision.EXECUTE:
        _set_status(execution, merge, CANCELLED, _signature(execution, "cancelled", now))
        merge = replace(merge, status=CANCELLED, cancelled_at=now)
    return _current_result(execution, merge)
