"""SKU 合并的新建、录入实数、执行、完成、取消与查询（契约 §2.4、§2.8）。

写命令都在幂等执行器以 READ COMMITTED 开的事务里、用 execution.connection 运行，
自己不开、不提交、不回滚事务。按状态的判定、差额公式与执行的定数用 app/domain/sku_merges.py。

新建（执行器已取目录写锁）：普通读查两个 SKU，不存在或属于别的租户 404
→ 按 sku_id 升序 FOR UPDATE 锁两个 SKU 行 → 普通读查涉及这两个 SKU 的未结束合并单：
同一对的返回 200 与它的当前表示 → 已合并 409 → 规范停用 409
→ 别的未结束单 409 SKU_MERGE_IN_PROGRESS
→ FOR SHARE 读两个 SKU 的 active 平台商品，有的 409 SKU_HAS_ACTIVE_LISTINGS（0.22）
→ 写合并单，201。
重叠检查用普通读：READ COMMITTED 下看得到锁 SKU 行之前已提交的合并单，
涉及同一个 SKU 的两笔新建在 SKU 行锁上排队，所以“合并单互不重叠”不靠数据库约束也成立。

录入实数（执行器已按正文的池预建两个余额行）：
FOR UPDATE 读合并单行，不存在或属于别的租户 404
→ 状态不是 selling_paused、counted 409 → 已录入过而池不同 409（pool_mismatch）
→ FOR SHARE 读池行，不是 live 409 → 按 (pool_id, sku_id) 升序锁两个余额行，
记下快照，整份替换已有的快照，合并单改为 counted。不写流水，不查 SKU 的状态。

执行（执行器已取目录写锁；开事务前由 ExecutionSkus 在池授权时读到的池上
预建两个余额行、先关两个 SKU 上已到期的预留，读到时还没有池就两步都跳过）：
1. FOR UPDATE 读合并单行；池不是凭据获准的池 403（登记 forbidden 事件，作为确定结果保存）
   → 按状态：reconcile_pending、completed 200 与当前表示；selling_paused、cancelled 409；
2. 名单：普通读查来源 SKU（合并单的池）上有订单行、状态为四种之一的预留，
   按 reservation_id 升序 FOR UPDATE 锁住；锁住时仍是四种之一的，
   再按 order_line_id 升序 FOR UPDATE 锁它们落在来源 SKU 上的订单行；
3. 按 (pool_id, sku_id) 升序锁两个余额行；
4. 复核：普通读查来源 SKU 上所属预留为四种之一的订单行，有第 2 步没锁住的就抛 RedoCommand，
   执行器回滚后从预建那一步重做（一个请求最多做 3 遍，最后一遍仍多出 503）；
5. 按 sku_id 升序 FOR UPDATE 锁两个 SKU 行：已合并 409 SKU_MERGED，规范停用 409 SKU_INACTIVE；
6. 快照之后有只改账的流水 409 COUNT_STALE；
7. 按 plan_execution 算迁移与定数，违反不变量 409 COUNT_BELOW_COMMITMENTS；
   写 MERGE_TRANSFER_OUT / IN（全为 0 也写）、改挂订单行并设所属预留的 last_changed_at、
   差额不全为 0 时写 COUNT_ADJUSTMENT，否则只核实规范 SKU 的余额行；
8. 来源 SKU 改为 inactive、merged_into_sku_id 设为规范 SKU、版本加 1，写一条 sku_merged；
9. 合并单改为 reconcile_pending，记下执行的凭据、操作者、请求 ID、时间、三条流水与改挂行数。
每次进入执行分支都在进程日志写一行合并单 ID、改挂的订单行数与事务用时。

完成与取消：FOR UPDATE 读合并单行，不存在或属于别的租户 404，再按状态判定。
录入实数、执行、完成与取消的 404 都在事务里判出，作为确定结果保存；
属于别的租户的经执行器登记 cross_tenant。

锁序：合并单行 → 预留行（FOR UPDATE）→ 订单行（FOR UPDATE）→ 池行（FOR SHARE）
→ 余额行（FOR UPDATE）→ SKU 行（FOR UPDATE）；用不到的层跳过。
新建只锁 SKU 行（之后 FOR SHARE 读平台商品）；录入实数跳过预留行与订单行；执行跳过池行。

表示由 merge_representation 从合并单行与快照算出，它不读库：
写命令把锁内读到的行加上本次的改动传进去，查询接口用普通读读出后传进去。
"""

import logging
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, fields, replace
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, and_, delete, insert, or_, select, update

from app.api.auth import FORBIDDEN, ObjectPool
from app.api.errors import ApiError
from app.domain.catalog import ACTIVE, INACTIVE, SKU, STATUS
from app.domain.catalog import SKU_MERGED as SKU_MERGED_EVENT
from app.domain.ids import new_id
from app.domain.ledger import CountBelowCommitments, EntryType
from app.domain.quantities import Quantities
from app.domain.requests import Actor
from app.domain.reservations import ACTIVE as RESERVATION_ACTIVE
from app.domain.reservations import COMMITTED, EXPIRED, LATE_PAYMENT_REVIEW
from app.domain.sku_merges import (
    CANCELLED,
    COMPLETED,
    COUNTED,
    MERGE_ID_PREFIX,
    OPEN_STATUSES,
    RECONCILE_PENDING,
    SELLING_PAUSED,
    SOURCE_TYPE,
    CommandDecision,
    SnapshotVersions,
    cancel_decision,
    complete_decision,
    count_decision,
    execute_decision,
    merge_deltas,
    plan_execution,
    stale_sku_ids,
)
from app.models.catalog import NOW, Sku
from app.models.inventory import QUANTITIES
from app.models.merges import SkuMerge, SkuMergeSnapshot
from app.models.orders import OrderLine, Reservation
from app.models.platform import SkuListing
from app.services import security_events
from app.services.catalog import ChangeAuthor, record_change
from app.services.idempotency import CommandResult, Execution, RedoCommand
from app.services.reservations import close_due_before_reserving
from app.services.security_events import CROSS_TENANT
from app.services.stock import (
    SKU_MERGED,
    LockedBalance,
    append_entry,
    command_now,
    format_timestamp,
    lock_balances,
    mark_verified,
    new_command_id,
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
COUNT_STALE = "COUNT_STALE"
COUNT_BELOW_COMMITMENTS = "COUNT_BELOW_COMMITMENTS"
# 停用 SKU 或商品与新建合并共用（§2.6 第 5 条、§2.8，0.22）。
SKU_HAS_ACTIVE_LISTINGS = "SKU_HAS_ACTIVE_LISTINGS"
MERGED_INTO_SKU_ID = "merged_into_sku_id"

# 改挂订单行的预留状态，名单与复核都按这四种取（§2.4 第 1 步）；
# released、closed_refunded 的不改挂。
REPOINTED_STATUSES = (RESERVATION_ACTIVE, EXPIRED, LATE_PAYMENT_REVIEW, COMMITTED)
# 执行一个请求最多做几遍：复核多出订单行时回滚重做（§2.8 执行第 4 步）。
EXECUTE_ATTEMPTS = 3

# 写到 app.api 的子日志器：app/api/requests.py 给 app.api 挂了输出，生产上看得到。
_log = logging.getLogger("app.api.sku_merges")


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


def find_merge_pool(connection: Connection, merge_id: str) -> ObjectPool | None:
    """执行的池授权用：合并单所属的租户与录入实数时的池（还没录入为 None），
    不按租户过滤；不存在为 None。
    """
    statement = select(SkuMerge.tenant_id, SkuMerge.pool_id).where(SkuMerge.merge_id == merge_id)
    row = connection.execute(statement).first()
    return None if row is None else ObjectPool(*row)


def merge_sku_ids(connection: Connection, tenant_id: str, merge_id: str) -> tuple[str, ...]:
    """录入实数与执行预建余额行用：普通读按租户取合并单的两个 SKU；不存在或属于别的租户为空。"""
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
    return execution.context.tenant_id


def _actor(execution: Execution) -> Actor:
    """合并单命令都要求角色（§10 第 3 条），操作者必在。"""
    actor = execution.context.actor
    if actor is None:
        raise RuntimeError("a sku merge command needs the actor headers")
    return actor


def _signature(execution: Execution, prefix: str, now: datetime) -> dict[str, Any]:
    """命令记在合并单上的凭据、操作者、角色、请求 ID 与时间，列名带 prefix。"""
    actor = _actor(execution)
    return {
        f"{prefix}_credential_id": execution.context.credential_id,
        f"{prefix}_actor_ref": actor.ref,
        f"{prefix}_actor_role": actor.role,
        f"{prefix}_request_id": execution.context.request_id,
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
    product_id: str
    status: str
    merged_into_sku_id: str | None
    version: int


def _lock_skus(execution: Execution, sku_ids: Sequence[str]) -> dict[str, _LockedSku]:
    """按 sku_id 升序 FOR UPDATE 锁 SKU 行。之前已查过存在与归属，读不到是程序错误。"""
    tenant_id = _tenant_id(execution)
    locked: dict[str, _LockedSku] = {}
    for sku_id in sorted(set(sku_ids)):
        statement = (
            select(Sku.sku_id, Sku.product_id, Sku.status, Sku.merged_into_sku_id, Sku.version)
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


def read_active_listings(
    connection: Connection, tenant_id: str, sku_ids: Iterable[str]
) -> dict[str, list[str]]:
    """FOR SHARE 读这些 SKU 在任何入口上 active 的平台商品（§2.9）：
    有的 SKU 对应它的 listing_id（升序），没有的不出现。
    调用方先锁住 SKU 或商品行；停用 SKU、商品与新建合并共用。
    """
    statement = (
        select(SkuListing.sku_id, SkuListing.listing_id)
        .where(
            SkuListing.tenant_id == tenant_id,
            SkuListing.sku_id.in_(sorted(set(sku_ids))),
            SkuListing.status == ACTIVE,
        )
        .order_by(SkuListing.listing_id)
        .with_for_update(read=True)
    )
    listings: dict[str, list[str]] = {}
    for sku_id, listing_id in connection.execute(statement):
        listings.setdefault(sku_id, []).append(listing_id)
    return listings


def has_active_listings(details: dict[str, Any]) -> ApiError:
    """409 SKU_HAS_ACTIVE_LISTINGS；details 至少带 listing_ids。"""
    message = "the sku has active platform listings"
    return ApiError(409, SKU_HAS_ACTIVE_LISTINGS, message, details=details)


def _require_no_active_listings(execution: Execution, sku_ids: Sequence[str]) -> None:
    """按给定顺序（先来源）第一个有 active 平台商品的：409，details 带 sku_id 与 listing_ids。"""
    listings = read_active_listings(execution.connection, _tenant_id(execution), sku_ids)
    for sku_id in sku_ids:
        if sku_id in listings:
            raise has_active_listings({"sku_id": sku_id, "listing_ids": listings[sku_id]})


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
    _require_no_active_listings(execution, sku_ids)

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
            credential_id=execution.context.credential_id,
            actor_ref=actor.ref,
            actor_role=actor.role,
            request_id=execution.context.request_id,
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


# --- 执行 ---


class ExecutionSkus:
    """一个执行请求开业务事务之前的两步（§2.8 执行，§2.7 的先后）。

    pool_id 是池授权时读到的合并单的池；为 None（读到时还没录入实数）时两步都跳过：
    余额行录入实数时已建，没关到的到期预留随订单行迁到规范 SKU，之后照常关单。
    - balance_rows（执行器的余额行预建）：普通读取合并单的两个 SKU，记进 sku_ids，
      返回它们在该池的余额行；
    - close_due（执行器的 before_transaction 钩子）：关掉这两个 SKU 在该池上
      已到期的 active 预留，上限与出错处理同新预留（close_due_before_reserving）。
    执行器重做时两步都再做一遍。
    """

    def __init__(self, tenant_id: str, merge_id: str, pool_id: str | None):
        self.tenant_id = tenant_id
        self.merge_id = merge_id
        self.pool_id = pool_id
        self.sku_ids: tuple[str, ...] = ()

    def balance_rows(self, connection: Connection) -> list[tuple[str, str]]:
        if self.pool_id is None:
            return []
        # 合并单不存在或属于别的租户时为空（池授权已拦下，不会走到这里）。
        self.sku_ids = merge_sku_ids(connection, self.tenant_id, self.merge_id)
        return [(self.pool_id, sku_id) for sku_id in self.sku_ids]

    def close_due(self, connection: Connection) -> None:
        if self.pool_id is None or not self.sku_ids:
            return
        close_due_before_reserving(connection, self.tenant_id, self.pool_id, self.sku_ids)


def _require_granted_pool(execution: Execution, merge: MergeRecord) -> None:
    """第 1 步锁内复核池：已录入的池不是凭据获准的池，403 FORBIDDEN，
    登记 forbidden 事件（被拒的类别为池，记合并单的 pool_id）。
    """
    if merge.pool_id is None or merge.pool_id == execution.context.pool_id:
        return
    details = {
        "category": "pool",
        "pool_id": merge.pool_id,
        "object_type": SKU_MERGE,
        "object_id": merge.merge_id,
    }
    execution.record_security_event(security_events.FORBIDDEN, details)
    raise ApiError(403, FORBIDDEN, "forbidden")


def read_repoint_candidates(
    connection: Connection, tenant_id: str, pool_id: str, sku_id: str
) -> list[str]:
    """第 2 步的名单：普通读查在该池、该 SKU 上有订单行、状态为改挂的四种之一的预留，
    按 reservation_id 升序。
    """
    has_line = (
        select(OrderLine.order_line_id)
        .where(
            OrderLine.tenant_id == tenant_id,
            OrderLine.pool_id == pool_id,
            OrderLine.reservation_id == Reservation.reservation_id,
            OrderLine.sku_id == sku_id,
        )
        .exists()
    )
    statement = (
        select(Reservation.reservation_id)
        .where(
            Reservation.tenant_id == tenant_id,
            Reservation.pool_id == pool_id,
            Reservation.status.in_(REPOINTED_STATUSES),
            has_line,
        )
        .order_by(Reservation.reservation_id)
    )
    return list(connection.execute(statement).scalars())


@dataclass(frozen=True)
class _LockedLine:
    order_line_id: str
    reservation_id: str


def _lock_repointed_lines(
    execution: Execution, pool_id: str, source_sku_id: str
) -> list[_LockedLine]:
    """第 2 步：按 reservation_id 升序 FOR UPDATE 锁名单里的预留行，
    锁住时已变成 released、closed_refunded 的不改挂；再按 order_line_id 升序 FOR UPDATE
    锁其余预留落在来源 SKU 上的订单行。返回锁住的订单行，按 order_line_id 升序。
    """
    connection = execution.connection
    tenant_id = _tenant_id(execution)
    kept: list[str] = []
    for reservation_id in read_repoint_candidates(connection, tenant_id, pool_id, source_sku_id):
        statement = (
            select(Reservation.status)
            .where(
                Reservation.tenant_id == tenant_id,
                Reservation.reservation_id == reservation_id,
            )
            .with_for_update()
        )
        if connection.execute(statement).scalar_one() in REPOINTED_STATUSES:
            kept.append(reservation_id)
    if not kept:
        return []
    # 预留行已锁住：它们的订单行只有 SKU 合并会改，普通读出的就是要锁的。
    statement = (
        select(OrderLine.order_line_id)
        .where(
            OrderLine.tenant_id == tenant_id,
            OrderLine.pool_id == pool_id,
            OrderLine.sku_id == source_sku_id,
            OrderLine.reservation_id.in_(kept),
        )
        .order_by(OrderLine.order_line_id)
    )
    lines: list[_LockedLine] = []
    for order_line_id in list(connection.execute(statement).scalars()):
        locking = (
            select(OrderLine.order_line_id, OrderLine.reservation_id)
            .where(
                OrderLine.tenant_id == tenant_id,
                OrderLine.order_line_id == order_line_id,
                OrderLine.sku_id == source_sku_id,
            )
            .with_for_update()
        )
        row = connection.execute(locking).mappings().first()
        if row is not None:
            lines.append(_LockedLine(**row))
    return lines


def _has_unlisted_lines(
    execution: Execution, pool_id: str, source_sku_id: str, listed: Iterable[_LockedLine]
) -> bool:
    """第 4 步复核：普通读查来源 SKU 上所属预留为四种之一的订单行，
    有不在第 2 步锁住的（名单之后提交的故障补交新建）返回 True。
    """
    statement = (
        select(OrderLine.order_line_id)
        .join(
            Reservation,
            and_(
                Reservation.tenant_id == OrderLine.tenant_id,
                Reservation.reservation_id == OrderLine.reservation_id,
            ),
        )
        .where(
            OrderLine.tenant_id == _tenant_id(execution),
            OrderLine.pool_id == pool_id,
            OrderLine.sku_id == source_sku_id,
            Reservation.status.in_(REPOINTED_STATUSES),
        )
    )
    locked = {line.order_line_id for line in listed}
    found = execution.connection.execute(statement).scalars()
    return any(line_id not in locked for line_id in found)


def _require_fresh(
    merge: MergeRecord,
    balances: dict[str, LockedBalance],
    snapshots: dict[str, SnapshotRecord],
) -> None:
    """第 6 步：快照之后有只改账流水的 SKU，409 COUNT_STALE，details.sku_ids 升序。"""
    source, canonical = (
        SnapshotVersions(
            sku_id=sku_id,
            last_book_only_version=balances[sku_id].last_book_only_version,
            snapshot_version=snapshots[sku_id].balance_version,
        )
        for sku_id in merge.sku_ids
    )
    stale = stale_sku_ids(source, canonical)
    if stale:
        raise ApiError(
            409,
            COUNT_STALE,
            "skus have book-only entries after their snapshots",
            details={"sku_ids": stale},
        )


def _repoint_lines(
    execution: Execution, canonical_sku_id: str, lines: Sequence[_LockedLine], now: datetime
) -> None:
    """第 7 步的改挂：订单行 sku_id 设为规范 SKU（连同累计量），
    所属预留的 last_changed_at 设为 now（§5.9）。
    """
    if not lines:
        return
    connection = execution.connection
    tenant_id = _tenant_id(execution)
    connection.execute(
        update(OrderLine)
        .where(
            OrderLine.tenant_id == tenant_id,
            OrderLine.order_line_id.in_([line.order_line_id for line in lines]),
        )
        .values(sku_id=canonical_sku_id)
    )
    reservation_ids = sorted({line.reservation_id for line in lines})
    connection.execute(
        update(Reservation)
        .where(Reservation.tenant_id == tenant_id, Reservation.reservation_id.in_(reservation_ids))
        .values(last_changed_at=now)
    )


def _mark_source_merged(
    execution: Execution, merge: MergeRecord, source: _LockedSku, canonical_sku_id: str
) -> None:
    """第 8 步：来源 SKU 改为 inactive、merged_into_sku_id 设为规范 SKU、版本加 1，
    写一条 sku_merged（changes 含 merged_into_sku_id，来源原为 active 时还含 status；
    reason 取合并单的）。
    """
    connection = execution.connection
    tenant_id = _tenant_id(execution)
    changes: dict[str, dict[str, Any]] = {}
    if source.status == ACTIVE:
        changes[STATUS] = {"before": ACTIVE, "after": INACTIVE}
    changes[MERGED_INTO_SKU_ID] = {"before": None, "after": canonical_sku_id}
    version = source.version + 1
    connection.execute(
        update(Sku)
        .where(Sku.tenant_id == tenant_id, Sku.sku_id == source.sku_id)
        .values(
            status=INACTIVE, merged_into_sku_id=canonical_sku_id, version=version, updated_at=NOW
        )
    )
    actor = _actor(execution)
    context = execution.context
    author = ChangeAuthor(
        credential_id=context.credential_id,
        actor_ref=actor.ref,
        actor_role=actor.role,
        request_id=context.request_id,
        idempotency_key=context.idempotency_key,
        reason=merge.reason,
    )
    record_change(
        connection,
        tenant_id,
        author,
        event_type=SKU_MERGED_EVENT,
        object_type=SKU,
        object_id=source.sku_id,
        product_id=source.product_id,
        version=version,
        changes=changes,
    )


def _execute_counted(execution: Execution, merge: MergeRecord) -> CommandResult:
    """counted 的合并单：第 2–9 步。"""
    connection = execution.connection
    tenant_id = _tenant_id(execution)
    pool_id = merge.pool_id
    if pool_id is None:
        raise RuntimeError(f"counted sku merge {merge.merge_id} has no pool")
    source_sku_id, canonical_sku_id = merge.sku_ids

    lines = _lock_repointed_lines(execution, pool_id, source_sku_id)
    balances = lock_balances(execution, pool_id, merge.sku_ids)
    if _has_unlisted_lines(execution, pool_id, source_sku_id, lines):
        raise RedoCommand()

    skus = _lock_skus(execution, merge.sku_ids)
    _require_not_merged(skus, merge.sku_ids)
    _require_active(skus[canonical_sku_id])

    recorded = read_snapshots(connection, tenant_id, merge.merge_id)
    snapshots = {snapshot.sku_id: snapshot for snapshot in recorded}
    _require_fresh(merge, balances, snapshots)
    source, canonical = balances[source_sku_id], balances[canonical_sku_id]
    try:
        plan = plan_execution(
            snapshots[source_sku_id].quantities,
            snapshots[canonical_sku_id].quantities,
            merge.shelf_count,
            merge.unsellable_count,
            source.quantities,
            canonical.quantities,
        )
    except CountBelowCommitments as exc:
        violation = {
            "sku_id": canonical_sku_id,
            **{name: getattr(exc, name) for name in QUANTITIES},
            "shortfall": exc.shortfall,
        }
        raise ApiError(
            409,
            COUNT_BELOW_COMMITMENTS,
            "the merge would break the balance invariants",
            details={"skus": [violation]},
        ) from None

    # 锁都已取到：这之后取的数据库时间不早于改挂的预留此前的任何改动。
    now = command_now(execution)
    command_id = new_command_id()
    entry = {"now": now, "source_type": SOURCE_TYPE, "source_ref": merge.merge_id}
    transfer = tuple(getattr(plan.transfer, name) for name in QUANTITIES)
    transfer_out = append_entry(
        execution,
        source,
        EntryType.MERGE_TRANSFER_OUT,
        tuple(-quantity for quantity in transfer),
        command_id,
        **entry,
    )
    transfer_in = append_entry(
        execution, canonical, EntryType.MERGE_TRANSFER_IN, transfer, command_id, **entry
    )
    _repoint_lines(execution, canonical_sku_id, lines, now)
    adjustment_entry_id = None
    if plan.writes_adjustment:
        deltas = (plan.d_shelf + plan.d_u, 0, plan.d_u, 0)
        adjustment = append_entry(
            execution, canonical, EntryType.COUNT_ADJUSTMENT, deltas, command_id, **entry
        )
        adjustment_entry_id = adjustment["entry_id"]
    else:
        # 两个差额都为 0：不写流水，只核实（两个判定列设为当时的 balance_version）。
        mark_verified(execution, canonical)
    if canonical.quantities != plan.canonical_after:
        raise RuntimeError(f"sku merge {merge.merge_id} settled differently from its plan")

    _mark_source_merged(execution, merge, skus[source_sku_id], canonical_sku_id)
    execution_values = {
        "transfer_out_entry_id": transfer_out["entry_id"],
        "transfer_in_entry_id": transfer_in["entry_id"],
        "adjustment_entry_id": adjustment_entry_id,
        "repointed_line_count": len(lines),
    }
    signature = _signature(execution, "executed", now)
    _set_status(execution, merge, RECONCILE_PENDING, signature | execution_values)
    executed = replace(merge, status=RECONCILE_PENDING, executed_at=now, **execution_values)
    return CommandResult(merge_representation(executed, snapshots.values()))


def execute_merge(execution: Execution, merge_id: str) -> CommandResult:
    """执行合并（§2.4 第 0–3 步、§2.8 执行第 1–9 步），200 与表示。

    第 1、5–7 步的拒绝由执行器撤销全部写入，合并单留在原状态，作为确定结果保存；
    第 4 步复核多出订单行时抛 RedoCommand。
    """
    started = time.monotonic()
    merge = _lock_existing_merge(execution, merge_id)
    _require_granted_pool(execution, merge)
    decision = execute_decision(merge.status)
    if decision is CommandDecision.CONFLICT:
        raise _state_conflict(merge)
    if decision is CommandDecision.CURRENT:
        return _current_result(execution, merge)

    outcome, repointed = "failed", 0
    try:
        result = _execute_counted(execution, merge)
        outcome, repointed = "executed", result.data["execution"]["repointed_line_count"]
        return result
    except ApiError as error:
        outcome = f"rejected {error.code}"
        raise
    except RedoCommand:
        outcome = "redo"
        raise
    finally:
        _log.info(
            "sku merge %s execution %s: %d order lines repointed, %.3f s in the transaction",
            merge_id,
            outcome,
            repointed,
            time.monotonic() - started,
        )


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
