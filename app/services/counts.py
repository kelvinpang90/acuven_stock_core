"""盘点单的开单、提交实数、确认落账、取消与查询（契约第 6 节、§4.1、§6.2、§6.6）。

写命令都在幂等执行器以 READ COMMITTED 开的事务里、用 execution.connection 运行，
自己不开、不提交、不回滚事务。

开单：业务引用（count_ref，按租户与凭据的销售入口）→ 池为 live → 写盘点单。
业务引用用普通读查；被业务规则拒绝或撞 (tenant_id, channel_id, count_ref) 唯一约束时，
照 app/services/stock.py 的 with_document 的做法抛 BusinessRefTaken，在新事务里重新判定一次。

提交实数：FOR UPDATE 读盘点单行 → 不是 open 409 → SKU 不存在 → 普通读读已有的盘点行
→ 升序锁余额行 → SKU 已合并 → 提交后 SKU 总数超过 100 为 422 → 记快照与差额，整行替换。
已有的盘点行在盘点单行锁之后用普通读读：READ COMMITTED 下普通读看得到最新提交，
而同一张单的提交、确认、取消都先锁盘点单行，读到的就是最新的；
不用加锁读，它对还没有盘点行的单会锁住空范围的间隙，两张单的首次提交会互相等待而死锁。

确认：FOR UPDATE 读盘点单行 → 不是 open 409 → 普通读读盘点行，没有为 422
→ 升序锁全部盘点行 SKU 的余额行 → COUNT_STALE → COUNT_BELOW_COMMITMENTS
→ 差额不全为 0 的写 COUNT_ADJUSTMENT，全为 0 的 mark_verified
→ 盘点行记 entry_id、盘点单改为 confirmed。
不查 SKU 已合并（§6.6 确认的步骤里没有）：
提交之后被合并的 SKU 有成对的迁移流水，由 COUNT_STALE 拦下。
两个判定列：last_book_only_version 取写的流水的版本或当时的 balance_version，
last_reconciled_version 无论写没写都取快照版本（§4.1，点数核实的是快照那一刻）。

取消：FOR UPDATE 读盘点单行 → 不是 open 409 → 改为 cancelled、记取消人 → 删除盘点行。
盘点行不是流水，没有落账，取消丢弃已提交的实数与快照（第 6 节）。

锁序：盘点单行 → 余额行（FOR UPDATE，按 (pool_id, sku_id) 升序）→ SKU 行（FOR SHARE）。
只有确认写流水。

表示由 count_representation 从盘点单行与盘点行算出，它不读库：
写命令把锁内读到的行加上本次的改动传进去，查询接口用普通读读出后传进去，确认也照此复用。
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, fields, replace
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.api.auth import ObjectPool
from app.api.errors import INVALID_REQUEST, ApiError
from app.domain.ids import new_id
from app.domain.ledger import (
    CountBelowCommitments,
    EntryType,
    apply_count_adjustment,
    count_deltas,
    is_stale,
)
from app.domain.quantities import Quantities
from app.models.inventory import QUANTITIES, StockCount, StockCountLine
from app.services.idempotency import BusinessRefTaken, CommandResult, Execution
from app.services.stock import (
    BUSINESS_REF_CONFLICT,
    BUSINESS_REF_CONFLICT_EVENT,
    LockedBalance,
    append_entry,
    command_now,
    format_timestamp,
    lock_balances,
    mark_verified,
    new_command_id,
    require_live,
    require_not_merged,
    require_skus_exist,
)

# 盘点单状态（§6.6）。
OPEN = "open"
CONFIRMED = "confirmed"
CANCELLED = "cancelled"

# 对象类型与错误码（§6.6、§8.3）。
COUNT = "count"
COUNT_NOT_FOUND = "COUNT_NOT_FOUND"
STATE_CONFLICT = "STATE_CONFLICT"
COUNT_STALE = "COUNT_STALE"
COUNT_BELOW_COMMITMENTS = "COUNT_BELOW_COMMITMENTS"
COUNT_SKU_LIMIT = "count_sku_limit"
COUNT_HAS_NO_LINES = "count_has_no_lines"
# 一张盘点单最多的 SKU 数（§6.6 提交第 3 步）。
MAX_COUNT_SKUS = 100

_DUPLICATE_ENTRY = 1062  # MySQL ER_DUP_ENTRY


# --- 读出的行 ---


@dataclass(frozen=True)
class CountRecord:
    """stock_counts 的一行（表示与判定要用的列）。"""

    count_id: str
    count_ref: str
    pool_id: str
    status: str
    note: str | None
    request_hash: str
    created_at: datetime
    confirmed_at: datetime | None
    confirmed_reason_code: str | None
    confirm_note: str | None
    cancelled_at: datetime | None


@dataclass(frozen=True)
class CountLineRecord:
    """stock_count_lines 的一行（表示要用的列）。"""

    sku_id: str
    shelf_count: int
    unsellable_count: int
    snapshot_stock_balance: int
    snapshot_reserved: int
    snapshot_unavailable: int
    snapshot_paid_pending_dispatch: int
    snapshot_balance_version: int
    submitted_at: datetime
    d_shelf: int
    d_u: int
    entry_id: str | None


@dataclass(frozen=True)
class CountSku:
    """提交里的一个 SKU，已经过第 7 步校验。"""

    sku_id: str
    shelf_count: int
    unsellable_count: int


_COUNT_COLUMNS = tuple(getattr(StockCount, field.name) for field in fields(CountRecord))
_LINE_COLUMNS = tuple(getattr(StockCountLine, field.name) for field in fields(CountLineRecord))


def _count_query(tenant_id: str):
    return select(*_COUNT_COLUMNS).where(StockCount.tenant_id == tenant_id)


def _count(row) -> CountRecord | None:
    return None if row is None else CountRecord(**row)


def read_count(connection: Connection, tenant_id: str, count_id: str) -> CountRecord | None:
    """普通读按租户读盘点单；不存在或属于别的租户为 None。"""
    statement = _count_query(tenant_id).where(StockCount.count_id == count_id)
    return _count(connection.execute(statement).mappings().first())


def lock_count(connection: Connection, tenant_id: str, count_id: str) -> CountRecord | None:
    """同 read_count，用 SELECT … FOR UPDATE 读。"""
    statement = _count_query(tenant_id).where(StockCount.count_id == count_id).with_for_update()
    return _count(connection.execute(statement).mappings().first())


def find_count_by_ref(
    connection: Connection, tenant_id: str, channel_id: str, count_ref: str
) -> CountRecord | None:
    """普通读按租户与销售入口找 count_ref。"""
    statement = _count_query(tenant_id).where(
        StockCount.channel_id == channel_id, StockCount.count_ref == count_ref
    )
    return _count(connection.execute(statement).mappings().first())


def count_owner(connection: Connection, count_id: str) -> str | None:
    """盘点单所属的租户；不存在为 None。"""
    statement = select(StockCount.tenant_id).where(StockCount.count_id == count_id)
    return connection.execute(statement).scalar_one_or_none()


def find_count_pool(connection: Connection, count_id: str) -> ObjectPool | None:
    """池授权用：盘点单所属的租户与池，不按租户过滤；不存在为 None。"""
    statement = select(StockCount.tenant_id, StockCount.pool_id).where(
        StockCount.count_id == count_id
    )
    row = connection.execute(statement).first()
    return None if row is None else ObjectPool(*row)


def read_lines(connection: Connection, tenant_id: str, count_id: str) -> list[CountLineRecord]:
    """普通读读盘点单的盘点行，按 sku_id 升序。"""
    statement = (
        select(*_LINE_COLUMNS)
        .where(StockCountLine.tenant_id == tenant_id, StockCountLine.count_id == count_id)
        .order_by(StockCountLine.sku_id)
    )
    return [CountLineRecord(**row) for row in connection.execute(statement).mappings()]


# --- 表示（§6.6） ---


def _timestamp(value: datetime | None) -> str | None:
    return None if value is None else format_timestamp(value)


def line_representation(line: CountLineRecord) -> dict[str, Any]:
    return {
        "sku_id": line.sku_id,
        "shelf_count": line.shelf_count,
        "unsellable_count": line.unsellable_count,
        "snapshot": {
            **{name: getattr(line, f"snapshot_{name}") for name in QUANTITIES},
            "balance_version": line.snapshot_balance_version,
        },
        "submitted_at": format_timestamp(line.submitted_at),
        "d_shelf": line.d_shelf,
        "d_u": line.d_u,
        "entry_id": line.entry_id,
    }


def count_representation(count: CountRecord, lines: Iterable[CountLineRecord]) -> dict[str, Any]:
    """盘点单的表示：键固定且都出现，lines 按 sku_id 升序。不读库。"""
    return {
        "count_id": count.count_id,
        "count_ref": count.count_ref,
        "pool_id": count.pool_id,
        "status": count.status,
        "note": count.note,
        "created_at": format_timestamp(count.created_at),
        "confirmed_at": _timestamp(count.confirmed_at),
        "reason_code": count.confirmed_reason_code,
        "confirm_note": count.confirm_note,
        "cancelled_at": _timestamp(count.cancelled_at),
        "lines": [
            line_representation(line) for line in sorted(lines, key=lambda line: line.sku_id)
        ],
    }


def current_representation(connection: Connection, tenant_id: str, count_id: str) -> dict | None:
    """普通读读出盘点单与盘点行后的表示；不存在或属于别的租户为 None。"""
    count = read_count(connection, tenant_id, count_id)
    if count is None:
        return None
    return count_representation(count, read_lines(connection, tenant_id, count_id))


# --- 共用的判定 ---


def _tenant_id(execution: Execution) -> str:
    return execution.access.principal.tenant_id


def _actor_ref(execution: Execution) -> str:
    """盘点单命令都要求角色（§10 第 3 条），操作者必在。"""
    actor = execution.access.actor
    if actor is None:
        raise RuntimeError("a count command needs the actor headers")
    return actor.ref


def _lock_open_count(execution: Execution, count_id: str) -> CountRecord:
    """FOR UPDATE 读盘点单行；不是 open：409 STATE_CONFLICT。

    池授权已确认它属于本租户，盘点单又不删除，读不到是程序错误。
    """
    count = lock_count(execution.connection, _tenant_id(execution), count_id)
    if count is None:
        raise RuntimeError(f"count {count_id} is not in the tenant")
    if count.status != OPEN:
        details = {"status": count.status}
        raise ApiError(409, STATE_CONFLICT, "the count is not open", details=details)
    return count


# --- 开单 ---


def _outside_create(execution: Execution) -> CommandResult:
    """_insert_count 抛的 BusinessRefTaken 由 create_count 换上真正的重新判定。"""
    raise RuntimeError("a count reference was taken outside create_count")


def _answer_existing(execution: Execution, count: CountRecord) -> CommandResult:
    """换键重报同一 count_ref：摘要相同 200 与当前表示；
    不同 409 BUSINESS_REF_CONFLICT 并记 business_ref_conflict 事件。
    """
    if count.request_hash == execution.request_digest:
        lines = read_lines(execution.connection, _tenant_id(execution), count.count_id)
        return CommandResult(count_representation(count, lines), 200)
    details = {"ref_type": COUNT, "ref": count.count_ref}
    execution.record_security_event(BUSINESS_REF_CONFLICT_EVENT, details)
    raise ApiError(
        409,
        BUSINESS_REF_CONFLICT,
        "the business reference was used with different content",
        details=details,
    )


def _insert_count(execution: Execution, record: CountRecord) -> None:
    """撞唯一约束时抛 BusinessRefTaken，由 create_count 重新判定。"""
    access = execution.access
    principal = access.principal
    values = {
        "count_id": record.count_id,
        "tenant_id": principal.tenant_id,
        "channel_id": principal.channel_id,
        "pool_id": record.pool_id,
        "count_ref": record.count_ref,
        "status": record.status,
        "note": record.note,
        "request_hash": record.request_hash,
        "credential_id": principal.credential_id,
        "actor_ref": _actor_ref(execution),
        "request_id": execution.request.state.request_id,
        "created_at": record.created_at,
    }
    try:
        execution.connection.execute(insert(StockCount).values(values))
    except IntegrityError as exc:
        if getattr(exc.orig, "args", ())[:1] == (_DUPLICATE_ENTRY,):
            raise BusinessRefTaken(_outside_create) from None
        raise


def create_count(
    execution: Execution, pool_id: str, count_ref: str, note: str | None
) -> CommandResult:
    """开单：已有同一 count_ref 时按 _answer_existing 回答；否则池须 live，写盘点单，201。

    命令被业务规则拒绝或撞唯一约束时抛 BusinessRefTaken：执行器在新事务里调用 judge，
    重新找 count_ref，有则回答，没有则再运行一次命令，这一次的结果（含拒绝）就是确定结果。
    """
    principal = execution.access.principal

    def find(execution: Execution) -> CountRecord | None:
        connection = execution.connection
        return find_count_by_ref(connection, principal.tenant_id, principal.channel_id, count_ref)

    def command(execution: Execution) -> CommandResult:
        now = command_now(execution)
        require_live(execution, pool_id)
        record = CountRecord(
            count_id=new_id("cnt"),
            count_ref=count_ref,
            pool_id=pool_id,
            status=OPEN,
            note=note,
            request_hash=execution.request_digest,
            created_at=now,
            confirmed_at=None,
            confirmed_reason_code=None,
            confirm_note=None,
            cancelled_at=None,
        )
        _insert_count(execution, record)
        return CommandResult(count_representation(record, []), 201)

    def judge(execution: Execution) -> CommandResult:
        existing = find(execution)
        if existing is not None:
            return _answer_existing(execution, existing)
        return command(execution)

    existing = find(execution)
    if existing is not None:
        return _answer_existing(execution, existing)
    try:
        return command(execution)
    except ApiError as error:
        if not 400 <= error.status <= 499:
            raise
        raise BusinessRefTaken(judge) from None
    except BusinessRefTaken:
        raise BusinessRefTaken(judge) from None


# --- 提交实数 ---


def _require_sku_limit(existing: Iterable[CountLineRecord], sku_ids: Iterable[str]) -> None:
    """提交后这张盘点单的 SKU 总数超过 100：422，details.reason 为 count_sku_limit。"""
    total = len({line.sku_id for line in existing} | set(sku_ids))
    if total > MAX_COUNT_SKUS:
        details = {"reason": COUNT_SKU_LIMIT, "limit": MAX_COUNT_SKUS, "sku_count": total}
        raise ApiError(422, INVALID_REQUEST, "a count can hold at most 100 skus", details=details)


def _counted_line(sku: CountSku, balance: LockedBalance, now: datetime) -> CountLineRecord:
    """记下余额行当时的四个量与版本作为快照，按 §6 的公式算差额。"""
    snapshot = balance.quantities
    d_shelf, d_u = count_deltas(snapshot, sku.shelf_count, sku.unsellable_count)
    return CountLineRecord(
        sku_id=sku.sku_id,
        shelf_count=sku.shelf_count,
        unsellable_count=sku.unsellable_count,
        **{f"snapshot_{name}": getattr(snapshot, name) for name in QUANTITIES},
        snapshot_balance_version=balance.balance_version,
        submitted_at=now,
        d_shelf=d_shelf,
        d_u=d_u,
        entry_id=None,
    )


def _write_line(
    execution: Execution, count: CountRecord, line: CountLineRecord, *, exists: bool
) -> None:
    """写入一行；已有的行整行替换（只保留最后一次的实数与快照，§6.6）。"""
    values = {
        **{field.name: getattr(line, field.name) for field in fields(CountLineRecord)},
        "actor_ref": _actor_ref(execution),
        "request_id": execution.request.state.request_id,
    }
    connection = execution.connection
    tenant_id = _tenant_id(execution)
    if exists:
        statement = (
            update(StockCountLine)
            .where(
                StockCountLine.tenant_id == tenant_id,
                StockCountLine.count_id == count.count_id,
                StockCountLine.sku_id == line.sku_id,
            )
            .values(values)
        )
    else:
        identity = {"count_id": count.count_id, "tenant_id": tenant_id, "pool_id": count.pool_id}
        statement = insert(StockCountLine).values(values | identity)
    connection.execute(statement)


def submit_count(execution: Execution, count_id: str, skus: Sequence[CountSku]) -> CommandResult:
    """提交实数，200 与盘点单的表示。不写流水。

    余额行由执行器按盘点单的池与请求里的 SKU 预建，所以还没有余额行的 SKU 记全零快照。
    """
    connection = execution.connection
    tenant_id = _tenant_id(execution)
    now = command_now(execution)
    count = _lock_open_count(execution, count_id)
    sku_ids = [sku.sku_id for sku in skus]
    require_skus_exist(execution, sku_ids)
    existing = {line.sku_id: line for line in read_lines(connection, tenant_id, count_id)}
    balances = lock_balances(execution, count.pool_id, sku_ids)
    require_not_merged(execution, sku_ids)
    _require_sku_limit(existing.values(), sku_ids)

    lines = dict(existing)
    for sku in sorted(skus, key=lambda sku: sku.sku_id):
        line = _counted_line(sku, balances[sku.sku_id], now)
        _write_line(execution, count, line, exists=sku.sku_id in existing)
        lines[sku.sku_id] = line
    return CommandResult(count_representation(count, lines.values()))


# --- 确认 ---


def _snapshot(line: CountLineRecord) -> Quantities:
    return Quantities(*(getattr(line, f"snapshot_{name}") for name in QUANTITIES))


def _require_lines(lines: Sequence[CountLineRecord]) -> None:
    """一个盘点行都没有：422，details.reason 为 count_has_no_lines。"""
    if not lines:
        details = {"reason": COUNT_HAS_NO_LINES}
        raise ApiError(422, INVALID_REQUEST, "the count has no counted skus", details=details)


def _require_fresh(lines: Sequence[CountLineRecord], balances: dict[str, LockedBalance]) -> None:
    """快照之后有只改账的流水的 SKU：整笔 409 COUNT_STALE，details.sku_ids 升序。"""
    stale = sorted(
        line.sku_id
        for line in lines
        if is_stale(balances[line.sku_id].last_book_only_version, line.snapshot_balance_version)
    )
    if stale:
        raise ApiError(
            409,
            COUNT_STALE,
            "skus have book-only entries after their snapshots",
            details={"sku_ids": stale},
        )


def _require_within_commitments(
    lines: Sequence[CountLineRecord], balances: dict[str, LockedBalance]
) -> None:
    """按差额落账后违反不变量的 SKU：整笔 409 COUNT_BELOW_COMMITMENTS，
    details.skus 按 sku_id 升序，每项带落账后的四个量与缺口数。
    """
    violations = []
    for line in sorted(lines, key=lambda line: line.sku_id):
        latest, snapshot = balances[line.sku_id].quantities, _snapshot(line)
        try:
            apply_count_adjustment(latest, snapshot, line.shelf_count, line.unsellable_count)
        except CountBelowCommitments as exc:
            violations.append(
                {
                    "sku_id": line.sku_id,
                    **{name: getattr(exc, name) for name in QUANTITIES},
                    "shortfall": exc.shortfall,
                }
            )
    if violations:
        raise ApiError(
            409,
            COUNT_BELOW_COMMITMENTS,
            "the count would break the balance invariants",
            details={"skus": violations},
        )


def _settle_line(
    execution: Execution,
    count: CountRecord,
    line: CountLineRecord,
    balance: LockedBalance,
    command_id: str,
    now: datetime,
    reason_code: str,
    note: str | None,
) -> CountLineRecord:
    """落账一个 SKU，返回记下 entry_id 的盘点行。

    差额不全为 0：写 COUNT_ADJUSTMENT（stock_balance 增 d_shelf + d_u、unavailable 增 d_u），
    并把 entry_id 写回盘点行；全为 0：不写流水，只核实。
    两种情况 last_reconciled_version 都取快照版本（§4.1、§6.6 确认第 6 步）。
    """
    snapshot_version = line.snapshot_balance_version
    if line.d_shelf == 0 and line.d_u == 0:
        mark_verified(execution, balance, reconciled_version=snapshot_version)
        return line
    entry = append_entry(
        execution,
        balance,
        EntryType.COUNT_ADJUSTMENT,
        (line.d_shelf + line.d_u, 0, line.d_u, 0),
        command_id,
        now=now,
        source_type=COUNT,
        source_ref=count.count_ref,
        reason_code=reason_code,
        note=note,
        reconciled_version=snapshot_version,
    )
    entry_id = entry["entry_id"]
    execution.connection.execute(
        update(StockCountLine)
        .where(
            StockCountLine.tenant_id == _tenant_id(execution),
            StockCountLine.count_id == count.count_id,
            StockCountLine.sku_id == line.sku_id,
        )
        .values(entry_id=entry_id)
    )
    return replace(line, entry_id=entry_id)


def confirm_count(
    execution: Execution, count_id: str, reason_code: str, note: str | None
) -> CommandResult:
    """确认落账，200 与盘点单的表示。被拒时什么也不写（执行器撤销，只保存幂等记录）。

    余额行在提交时已预建，盘点行又有外键指向它，lock_balances 读得到。
    """
    connection = execution.connection
    tenant_id = _tenant_id(execution)
    now = command_now(execution)
    count = _lock_open_count(execution, count_id)
    lines = read_lines(connection, tenant_id, count_id)
    _require_lines(lines)
    balances = lock_balances(execution, count.pool_id, [line.sku_id for line in lines])
    _require_fresh(lines, balances)
    _require_within_commitments(lines, balances)

    command_id = new_command_id()
    settled = [
        _settle_line(
            execution, count, line, balances[line.sku_id], command_id, now, reason_code, note
        )
        for line in lines
    ]
    connection.execute(
        update(StockCount)
        .where(StockCount.tenant_id == tenant_id, StockCount.count_id == count_id)
        .values(
            status=CONFIRMED,
            confirmed_credential_id=execution.access.principal.credential_id,
            confirmed_actor_ref=_actor_ref(execution),
            confirmed_request_id=execution.request.state.request_id,
            confirmed_at=now,
            confirmed_reason_code=reason_code,
            confirm_note=note,
        )
    )
    confirmed = replace(
        count,
        status=CONFIRMED,
        confirmed_at=now,
        confirmed_reason_code=reason_code,
        confirm_note=note,
    )
    return CommandResult(count_representation(confirmed, settled))


# --- 取消 ---


def cancel_count(execution: Execution, count_id: str) -> CommandResult:
    """改为 cancelled 并记取消人，删除盘点行；不写流水。200 与表示，lines 为空。"""
    connection = execution.connection
    tenant_id = _tenant_id(execution)
    now = command_now(execution)
    count = _lock_open_count(execution, count_id)
    where = (StockCount.tenant_id == tenant_id, StockCount.count_id == count_id)
    connection.execute(
        update(StockCount)
        .where(*where)
        .values(
            status=CANCELLED,
            cancelled_credential_id=execution.access.principal.credential_id,
            cancelled_actor_ref=_actor_ref(execution),
            cancelled_request_id=execution.request.state.request_id,
            cancelled_at=now,
        )
    )
    connection.execute(
        delete(StockCountLine).where(
            StockCountLine.tenant_id == tenant_id, StockCountLine.count_id == count_id
        )
    )
    cancelled = replace(count, status=CANCELLED, cancelled_at=now)
    return CommandResult(count_representation(cancelled, []))
