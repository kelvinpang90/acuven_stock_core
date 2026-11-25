"""库存写命令共用的数据库函数（契约 §4.1、§4.3、§6.2、§6.8、§8.1）。

这些函数都在幂等执行器（app/services/idempotency.py）已开的事务里、用 execution.connection 运行，
自己不开、不提交、不回滚事务。业务拒绝抛 ApiError；程序错误抛别的异常，执行器整笔回滚、回 500，
同键重试会重新执行。具体命令的规则（期初、收货、调整、盘点、更正）不在这里，余额行的预建由执行器做。

一条单据命令（收货为例）的骨架：

    def _create_receipt(execution: Execution, body: Receipt, pool_id: str) -> CommandResult:
        def command(execution: Execution) -> CommandResult:
            now = command_now(execution)
            require_live(execution, pool_id)
            sku_ids = [line.sku_id for line in body.lines]
            require_skus_exist(execution, sku_ids)
            balances = lock_balances(execution, pool_id, sku_ids)
            require_not_merged(execution, sku_ids)
            command_id = new_command_id()
            entries = [
                append_entry(
                    execution, balances[line.sku_id], EntryType.RECEIPT, (line.quantity, 0, 0, 0),
                    command_id, now=now, source_type="receipt", source_ref=body.receipt_ref,
                    line_ref=line.line_ref, ...
                )
                for line in body.lines
            ]
            document_id = new_document_id(RECEIPT)
            result = {"document_id": document_id, ...}
            insert_document(execution, RECEIPT, body.receipt_ref, document_id, now, result)
            return CommandResult(result, 201)

        return with_document(execution, RECEIPT, body.receipt_ref, command)

判定顺序照 §6.2 第 4 条：业务引用（with_document）→ 池状态 → SKU 不存在 → SKU 已合并
→ 命令自己的规则。

锁序固定为：池行（FOR SHARE，池切 live 用 FOR UPDATE）
→ 余额行（FOR UPDATE，按 (pool_id, sku_id) 升序）→ SKU 行（FOR SHARE，按 sku_id 升序）。
SKU 合并的执行先锁余额行再改 SKU 行，与此一致，不成环。

为什么 SKU 检查分两步：读快照建立在执行器查幂等记录时，普通读看不到之后提交的 SKU 合并。
require_skus_exist 在锁余额行之前用普通读查存在与归属（SKU 不删，也不换租户）；
require_not_merged 在锁余额行之后用加锁读查合并，加锁读看得到最新提交。

没有请求的调用方（预留的到期关单，§5.6）不经执行器：lock_balance_rows 与 write_entry
只收连接、租户与流水的身份（EntryIdentity），lock_balances 与 append_entry 从 Execution
取出这些再调用它们。

命令时间：command_now 在命令开头读一次数据库时间，命令自己保存、
传给 append_entry 与 insert_document；同一命令的各条流水（调用方没给 occurred_at 时）
与单据的时间因此一致。with_document 在新事务里重新运行命令时，命令照样在开头重读一次。

为什么单据命令统一经 with_document：业务引用用普通读查，
看不到并发的另一个请求在读快照之后提交的同一引用；
那个请求的写入又会改变本命令锁行后看到的账面，使本命令被业务规则拒绝（例如同一不可售调整换键重报，
先到者已用掉可售量，后到者会得到 409 INSUFFICIENT_STOCK 而不是原结果）。
所以命令的业务拒绝、单据撞唯一约束、
CORRECTION 撞流水表的 (tenant_id, corrects_entry_id) 唯一约束，都改抛 BusinessRefTaken，
在执行器开的新事务里（读快照看得到已提交的全部写入）重新判定；重新判定里的拒绝就是确定结果，
再撞唯一约束（第三个并发请求）按 500 处理，同键重试会重新执行。代价是被拒的单据命令多一次事务。
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, NoReturn

from sqlalchemy import Connection, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from app.api.errors import ApiError
from app.domain.ids import new_id
from app.domain.ledger import (
    EntryType,
    last_book_only_version_after,
    last_reconciled_version_after,
)
from app.domain.quantities import Quantities
from app.models.catalog import Sku
from app.models.inventory import QUANTITIES, LedgerEntry, StockBalance, StockDocument, StockPool
from app.services.idempotency import BusinessRefTaken, CommandResult, Execution
from app.services.security_events import CROSS_TENANT

# 池状态（§6.2 第 2 条）。
INITIALIZING = "initializing"
LIVE = "live"

# 错误码（§6.2、§8.3）。
POOL_NOT_LIVE = "POOL_NOT_LIVE"
POOL_ALREADY_LIVE = "POOL_ALREADY_LIVE"
SKU_NOT_FOUND = "SKU_NOT_FOUND"
SKU_MERGED = "SKU_MERGED"
BUSINESS_REF_CONFLICT = "BUSINESS_REF_CONFLICT"
# 安全事件类型（§7.2）。
BUSINESS_REF_CONFLICT_EVENT = "business_ref_conflict"

# 单据类型（§6.2 第 7 条）与各自的 ID 前缀。
RECEIPT = "receipt"
UNAVAILABLE_ADJUSTMENT = "unavailable_adjustment"
CORRECTION = "correction"
_DOCUMENT_PREFIXES = {RECEIPT: "rcp", UNAVAILABLE_ADJUSTMENT: "adj", CORRECTION: "cor"}

SKU = "sku"

_DUPLICATE_ENTRY = 1062  # MySQL ER_DUP_ENTRY
# 一条流水最多被更正一次（§6.7）；约束名按 app/db.py 的命名约定。
_CORRECTS_ENTRY_KEY = "uq_ledger_entries_tenant_id_corrects_entry_id"

# 四个量的增量，顺序同 QUANTITIES：stock_balance、reserved、unavailable、paid_pending_dispatch。
Deltas = tuple[int, int, int, int]


def _tenant_id(execution: Execution) -> str:
    return execution.context.tenant_id


# --- 命令时间 ---


def command_now(execution: Execution) -> datetime:
    """命令开头读一次数据库时间（UTC，会话时区由 app/db.py 固定），由命令自己保存、往下传。"""
    return execution.connection.execute(text("SELECT CURRENT_TIMESTAMP(6)")).scalar_one()


def new_command_id() -> str:
    """同一命令写的各条流水共用的 command_id。"""
    return new_id("cmd")


# --- 池状态（§6.2 第 2 条） ---


def lock_pool(execution: Execution, pool_id: str, for_update: bool = False) -> str:
    """按租户加锁读池行，返回状态；平时 FOR SHARE，池切 live 用 FOR UPDATE。

    池已由池授权确认属于本租户，读不到是程序错误。
    """
    statement = (
        select(StockPool.status)
        .where(StockPool.tenant_id == _tenant_id(execution), StockPool.pool_id == pool_id)
        .with_for_update(read=not for_update)
    )
    status = execution.connection.execute(statement).scalar_one_or_none()
    if status is None:
        raise RuntimeError(f"pool {pool_id} is not in the tenant")
    return status


def require_live(execution: Execution, pool_id: str) -> None:
    """池不是 live：409 POOL_NOT_LIVE。"""
    status = lock_pool(execution, pool_id)
    if status != LIVE:
        details = {"pool_status": status}
        raise ApiError(409, POOL_NOT_LIVE, "the pool is not live", details=details)


def require_initializing(execution: Execution, pool_id: str) -> None:
    """池已 live：409 POOL_ALREADY_LIVE。"""
    status = lock_pool(execution, pool_id)
    if status == LIVE:
        details = {"pool_status": status}
        raise ApiError(409, POOL_ALREADY_LIVE, "the pool is already live", details=details)


# --- SKU（§6.2 第 3 条） ---


def require_skus_exist(execution: Execution, sku_ids: Sequence[str]) -> None:
    """锁余额行之前用普通读按请求顺序查 SKU。

    不存在或属于别的租户的收集起来，404 SKU_NOT_FOUND，details.sku_ids 按请求顺序列全；
    其中属于别的租户的每个登记一条 cross_tenant。停用的 SKU（含所属商品停用）照常通过。
    """
    owners: dict[str, str] = {}
    if sku_ids:
        statement = select(Sku.sku_id, Sku.tenant_id).where(Sku.sku_id.in_(set(sku_ids)))
        owners = dict(execution.connection.execute(statement).all())
    tenant_id = _tenant_id(execution)
    missing = []
    for sku_id in sku_ids:
        owner = owners.get(sku_id)
        if owner == tenant_id:
            continue
        missing.append(sku_id)
        if owner is not None:
            execution.record_security_event(CROSS_TENANT, {"object_type": SKU, "id": sku_id})
    if missing:
        raise ApiError(404, SKU_NOT_FOUND, "sku not found", details={"sku_ids": missing})


def require_not_merged(execution: Execution, sku_ids: Iterable[str]) -> None:
    """lock_balances 之后按 sku_id 升序 FOR SHARE 读 SKU 行（加锁读看得到最新提交的合并）。

    第一个已合并的：409 SKU_MERGED，details 带 sku_id 与 merged_into_sku_id。
    调用前已经过 require_skus_exist，读不到是程序错误。
    """
    tenant_id = _tenant_id(execution)
    for sku_id in sorted(set(sku_ids)):
        statement = (
            select(Sku.merged_into_sku_id)
            .where(Sku.tenant_id == tenant_id, Sku.sku_id == sku_id)
            .with_for_update(read=True)
        )
        row = execution.connection.execute(statement).first()
        if row is None:
            raise RuntimeError(f"sku {sku_id} is not in the tenant")
        if row.merged_into_sku_id is not None:
            details = {"sku_id": sku_id, "merged_into_sku_id": row.merged_into_sku_id}
            raise ApiError(409, SKU_MERGED, "sku has been merged", details=details)


# --- 余额行（§4.1、§8.1） ---


@dataclass
class LockedBalance:
    """已 FOR UPDATE 锁住的余额行。append_entry 与 mark_verified 就地更新它。"""

    pool_id: str
    sku_id: str
    stock_balance: int
    reserved: int
    unavailable: int
    paid_pending_dispatch: int
    balance_version: int
    last_book_only_version: int
    last_reconciled_version: int
    opening_entry_id: str | None

    @property
    def quantities(self) -> Quantities:
        return Quantities(*(getattr(self, name) for name in QUANTITIES))


_BALANCE_COLUMNS = (
    StockBalance.pool_id,
    StockBalance.sku_id,
    *(getattr(StockBalance, name) for name in QUANTITIES),
    StockBalance.balance_version,
    StockBalance.last_book_only_version,
    StockBalance.last_reconciled_version,
    StockBalance.opening_entry_id,
)


def _balance_where(tenant_id: str, pool_id: str, sku_id: str) -> tuple:
    return (
        StockBalance.tenant_id == tenant_id,
        StockBalance.pool_id == pool_id,
        StockBalance.sku_id == sku_id,
    )


def lock_balance_rows(
    connection: Connection, tenant_id: str, pool_id: str, sku_ids: Iterable[str]
) -> dict[str, LockedBalance]:
    """lock_balances 的底层：在给定连接的事务里按租户锁行，规则相同。"""
    balances: dict[str, LockedBalance] = {}
    for sku_id in sorted(set(sku_ids)):
        statement = (
            select(*_BALANCE_COLUMNS)
            .where(*_balance_where(tenant_id, pool_id, sku_id))
            .with_for_update()
        )
        row = connection.execute(statement).mappings().first()
        if row is None:
            raise RuntimeError(f"no balance row for pool {pool_id} and sku {sku_id}")
        balances[sku_id] = LockedBalance(**row)
    return balances


def lock_balances(
    execution: Execution, pool_id: str, sku_ids: Iterable[str]
) -> dict[str, LockedBalance]:
    """按 (pool_id, sku_id) 升序逐行 SELECT … FOR UPDATE，返回按 sku_id 索引的余额行。

    行已由执行器预建。某行不存在（SKU 在预建之后才建成，极少见）是程序错误：
    抛异常、500，同键重试时预建会补上。
    """
    return lock_balance_rows(execution.connection, _tenant_id(execution), pool_id, sku_ids)


def _checked_reconciled(balance: LockedBalance, reconciled_version: int, upper: int) -> int:
    """给定的 last_reconciled_version 不能变小，也不能超过 last_book_only_version（§4.1）。"""
    if not balance.last_reconciled_version <= reconciled_version <= upper:
        raise ValueError(
            f"reconciled_version {reconciled_version} is outside "
            f"[{balance.last_reconciled_version}, {upper}]"
        )
    return reconciled_version


def _is_duplicate(exc: IntegrityError, key: str | None = None) -> bool:
    args = getattr(exc.orig, "args", ())
    if args[:1] != (_DUPLICATE_ENTRY,):
        return False
    return key is None or (len(args) > 1 and key in str(args[1]))


def _outside_with_document(execution: Execution) -> CommandResult:
    """append_entry 与 insert_document 抛的 BusinessRefTaken
    由 with_document 换上真正的重新判定。
    """
    raise RuntimeError("a business reference was taken outside with_document")


@dataclass(frozen=True)
class EntryIdentity:
    """流水上记的来源与身份（§4.3）。Core 自己的到期关单只有 channel_id，其余为空。"""

    channel_id: str | None
    idempotency_key: str | None = None
    credential_id: str | None = None
    actor_ref: str | None = None
    actor_role: str | None = None
    request_id: str | None = None


def entry_identity(execution: Execution) -> EntryIdentity:
    """本请求的身份：凭据的销售入口、幂等键、凭据、操作者与请求 ID。"""
    context = execution.context
    return EntryIdentity(
        channel_id=context.channel_id,
        idempotency_key=context.idempotency_key,
        credential_id=context.credential_id,
        actor_ref=None if context.actor is None else context.actor.ref,
        actor_role=None if context.actor is None else context.actor.role,
        request_id=context.request_id,
    )


def append_entry(
    execution: Execution,
    balance: LockedBalance,
    entry_type: EntryType | str,
    deltas: Deltas,
    command_id: str,
    *,
    now: datetime,
    source_type: str | None = None,
    source_ref: str | None = None,
    line_ref: str | None = None,
    reason_code: str | None = None,
    note: str | None = None,
    occurred_at: datetime | None = None,
    corrects_entry_id: str | None = None,
    reconciled_version: int | None = None,
) -> dict[str, Any]:
    """在锁住的余额行上写一条流水（§4.3），返回它的表示（§6.8）；身份取本请求的。

    规则见 write_entry。
    """
    return write_entry(
        execution.connection,
        _tenant_id(execution),
        entry_identity(execution),
        balance,
        entry_type,
        deltas,
        command_id,
        now=now,
        source_type=source_type,
        source_ref=source_ref,
        line_ref=line_ref,
        reason_code=reason_code,
        note=note,
        occurred_at=occurred_at,
        corrects_entry_id=corrects_entry_id,
        reconciled_version=reconciled_version,
    )


def write_entry(
    connection: Connection,
    tenant_id: str,
    identity: EntryIdentity,
    balance: LockedBalance,
    entry_type: EntryType | str,
    deltas: Deltas,
    command_id: str,
    *,
    now: datetime,
    source_type: str | None = None,
    source_ref: str | None = None,
    line_ref: str | None = None,
    reason_code: str | None = None,
    note: str | None = None,
    occurred_at: datetime | None = None,
    corrects_entry_id: str | None = None,
    reconciled_version: int | None = None,
) -> dict[str, Any]:
    """append_entry 的底层：在给定连接的事务里、以给定的租户与身份写一条流水。

    写后的四个量用 Quantities 校验，违反不变量时抛 InvariantViolation，由调用方处理。
    balance_version 加 1，两个判定列按 app/domain/ledger.py 的默认规则更新；
    给了 reconciled_version 时 last_reconciled_version 取它（盘点确认取快照版本，§4.1 的例外），
    它小于当前值或大于写后的 last_book_only_version 是程序错误。
    OPENING_BALANCE 把 opening_entry_id 设为这条流水；
    CORRECTION 更正的正是 opening_entry_id 时清空它。
    now 是 command_now 的值：occurred_at 为空时取它，recorded_at 也显式写它。
    CORRECTION 撞上 (tenant_id, corrects_entry_id) 唯一约束（并发的另一笔更正先提交）时
    抛 BusinessRefTaken，在 with_document 里重新判定。
    传入的 balance 就地更新，同一命令对同一行再写流水时用更新后的值。
    """
    entry_type = EntryType(entry_type)
    after = Quantities(
        *(getattr(balance, name) + delta for name, delta in zip(QUANTITIES, deltas, strict=True))
    )
    version = balance.balance_version + 1
    book_only = last_book_only_version_after(entry_type, balance.last_book_only_version, version)
    if reconciled_version is None:
        reconciled = last_reconciled_version_after(
            entry_type, balance.last_reconciled_version, version
        )
    else:
        reconciled = _checked_reconciled(balance, reconciled_version, book_only)
    entry_id = new_id("ent")
    opening_entry_id = balance.opening_entry_id
    if entry_type is EntryType.OPENING_BALANCE:
        opening_entry_id = entry_id
    elif entry_type is EntryType.CORRECTION and corrects_entry_id is not None:
        if corrects_entry_id == opening_entry_id:
            opening_entry_id = None

    values: dict[str, Any] = {
        "entry_id": entry_id,
        "tenant_id": tenant_id,
        "pool_id": balance.pool_id,
        "sku_id": balance.sku_id,
        "entry_type": entry_type.value,
        **{f"{name}_delta": delta for name, delta in zip(QUANTITIES, deltas, strict=True)},
        **{f"{name}_after": getattr(after, name) for name in QUANTITIES},
        "balance_version_after": version,
        "channel_id": identity.channel_id,
        "source_type": source_type,
        "source_ref": source_ref,
        "line_ref": line_ref,
        "command_id": command_id,
        "idempotency_key": identity.idempotency_key,
        "credential_id": identity.credential_id,
        "actor_ref": identity.actor_ref,
        "actor_role": identity.actor_role,
        "reason_code": reason_code,
        "note": note,
        "request_id": identity.request_id,
        "corrects_entry_id": corrects_entry_id,
        "occurred_at": now if occurred_at is None else occurred_at,
        "recorded_at": now,
    }
    try:
        connection.execute(insert(LedgerEntry).values(values))
    except IntegrityError as exc:
        if entry_type is EntryType.CORRECTION and _is_duplicate(exc, _CORRECTS_ENTRY_KEY):
            raise BusinessRefTaken(_outside_with_document) from None
        raise
    columns = {name: getattr(after, name) for name in QUANTITIES} | {
        "balance_version": version,
        "last_book_only_version": book_only,
        "last_reconciled_version": reconciled,
        "opening_entry_id": opening_entry_id,
    }
    where = _balance_where(tenant_id, balance.pool_id, balance.sku_id)
    connection.execute(update(StockBalance).where(*where).values(columns))
    for name, value in columns.items():
        setattr(balance, name, value)
    return entry_representation(values)


def mark_verified(
    execution: Execution, balance: LockedBalance, reconciled_version: int | None = None
) -> None:
    """差额为 0、不写流水的核实（§4.1）：last_book_only_version 设为当前 balance_version，
    last_reconciled_version 设为当前 balance_version 或给定的 reconciled_version
    （检查同 append_entry）。
    """
    version = balance.balance_version
    reconciled = version
    if reconciled_version is not None:
        reconciled = _checked_reconciled(balance, reconciled_version, version)
    columns = {"last_book_only_version": version, "last_reconciled_version": reconciled}
    where = _balance_where(_tenant_id(execution), balance.pool_id, balance.sku_id)
    execution.connection.execute(update(StockBalance).where(*where).values(columns))
    balance.last_book_only_version = version
    balance.last_reconciled_version = reconciled


# --- 流水的表示（§6.8） ---


def format_timestamp(value: datetime) -> str:
    """UTC 的 RFC 3339，微秒加 Z。数据库读出的时间不带时区，按 UTC 处理。"""
    if value.tzinfo is not None:
        value = (value - value.utcoffset()).replace(tzinfo=None)
    return value.isoformat(timespec="microseconds") + "Z"


def entry_representation(entry: Mapping[str, Any]) -> dict[str, Any]:
    """流水的表示；entry 是按 ledger_entries 列名取值的映射（写入的值或读出的行）。

    不含 idempotency_key 与 credential_id。
    """
    plain = (
        "entry_id",
        "pool_id",
        "sku_id",
        "entry_type",
    )
    references = (
        "channel_id",
        "source_type",
        "source_ref",
        "line_ref",
        "command_id",
        "actor_ref",
        "actor_role",
        "reason_code",
        "note",
        "request_id",
        "corrects_entry_id",
    )
    return {
        **{name: entry[name] for name in plain},
        "deltas": {name: entry[f"{name}_delta"] for name in QUANTITIES},
        "after": {name: entry[f"{name}_after"] for name in QUANTITIES},
        "balance_version_after": entry["balance_version_after"],
        **{name: entry[name] for name in references},
        "occurred_at": format_timestamp(entry["occurred_at"]),
        "recorded_at": format_timestamp(entry["recorded_at"]),
    }


# --- 单据与业务引用（§6.2 第 7 条、§7.1 第 2 条） ---


@dataclass(frozen=True)
class Document:
    """stock_documents 里已生效的一行。"""

    document_id: str
    document_type: str
    document_ref: str
    request_hash: str
    result: dict[str, Any]


# 摘要不同时的回答：收已有的单据，抛 ApiError。
OnConflict = Callable[[Document], NoReturn]


def new_document_id(document_type: str) -> str:
    """单据 ID，前缀按类型是 rcp_、adj_、cor_。"""
    prefix = _DOCUMENT_PREFIXES.get(document_type)
    if prefix is None:
        raise ValueError(f"unknown document type {document_type!r}")
    return new_id(prefix)


def find_document(execution: Execution, document_type: str, document_ref: str) -> Document | None:
    """按租户与凭据的销售入口用普通读查单据。"""
    context = execution.context
    statement = select(
        StockDocument.document_id,
        StockDocument.document_type,
        StockDocument.document_ref,
        StockDocument.request_hash,
        StockDocument.result,
    ).where(
        StockDocument.tenant_id == context.tenant_id,
        StockDocument.channel_id == context.channel_id,
        StockDocument.document_type == document_type,
        StockDocument.document_ref == document_ref,
    )
    row = execution.connection.execute(statement).mappings().first()
    return None if row is None else Document(**row)


def business_ref_conflict(execution: Execution, document: Document) -> ApiError:
    """默认的摘要不同的回答：登记 business_ref_conflict，返回 409 BUSINESS_REF_CONFLICT。"""
    details = {"ref_type": document.document_type, "ref": document.document_ref}
    execution.record_security_event(BUSINESS_REF_CONFLICT_EVENT, details)
    return ApiError(
        409,
        BUSINESS_REF_CONFLICT,
        "the business reference was used with different content",
        details=details,
    )


def answer_existing_document(
    execution: Execution, document: Document, on_conflict: OnConflict | None = None
) -> CommandResult:
    """换幂等键重报同一引用：摘要相同返回 200 与 result；不同时由 on_conflict 回答，
    不给时 409 BUSINESS_REF_CONFLICT 并登记事件（更正给自己的 already_corrected，§6.7）。
    """
    if document.request_hash == execution.request_digest:
        return CommandResult(document.result, 200)
    if on_conflict is None:
        raise business_ref_conflict(execution, document)
    on_conflict(document)
    raise RuntimeError("on_conflict must raise an ApiError")


def insert_document(
    execution: Execution,
    document_type: str,
    document_ref: str,
    document_id: str,
    created_at: datetime,
    result: dict[str, Any],
) -> None:
    """写一行单据：池取凭据销售入口绑定的池（池授权已确认命令的池就是它），
    request_hash 是本请求的摘要。撞唯一约束时抛 BusinessRefTaken，在 with_document 里重新判定。
    """
    context = execution.context
    values = {
        "document_id": document_id,
        "tenant_id": context.tenant_id,
        "channel_id": context.channel_id,
        "pool_id": context.pool_id,
        "document_type": document_type,
        "document_ref": document_ref,
        "request_hash": execution.request_digest,
        "result": result,
        "credential_id": context.credential_id,
        "actor_ref": None if context.actor is None else context.actor.ref,
        "actor_role": None if context.actor is None else context.actor.role,
        "request_id": context.request_id,
        "created_at": created_at,
    }
    try:
        execution.connection.execute(insert(StockDocument).values(values))
    except IntegrityError as exc:
        if _is_duplicate(exc):
            raise BusinessRefTaken(_outside_with_document) from None
        raise


def with_document(
    execution: Execution,
    document_type: str,
    document_ref: str,
    command: Callable[[Execution], CommandResult],
    on_conflict: OnConflict | None = None,
) -> CommandResult:
    """单据命令的入口：已有单据就按 answer_existing_document 回答，没有就运行 command。

    command 锁行、写流水、最后 insert_document 并返回结果。它的业务拒绝（ApiError 4xx）、
    单据或 CORRECTION 撞唯一约束都改抛 BusinessRefTaken：执行器在新事务里调用重新判定，
    重新 find_document，有则回答，没有则再运行一次 command，这一次的结果（含拒绝）就是确定结果。
    on_conflict 在首次判定与重新判定里都用。
    """

    def judge(execution: Execution) -> CommandResult:
        existing = find_document(execution, document_type, document_ref)
        if existing is not None:
            return answer_existing_document(execution, existing, on_conflict)
        return command(execution)

    existing = find_document(execution, document_type, document_ref)
    if existing is not None:
        return answer_existing_document(execution, existing, on_conflict)
    try:
        return command(execution)
    except ApiError as error:
        if not 400 <= error.status <= 499:
            raise
        raise BusinessRefTaken(judge) from None
    except BusinessRefTaken:
        raise BusinessRefTaken(judge) from None
