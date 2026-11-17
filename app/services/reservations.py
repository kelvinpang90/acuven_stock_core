"""整单预留、释放、到期关单与预留的查询（契约 §5.2、§5.6、§7.1、§8.1）。

预留命令在幂等执行器以 READ COMMITTED 开的事务里、用 execution.connection 运行，
自己不开、不提交、不回滚事务；余额行由执行器在开业务事务之前预建。

判定顺序（§5.6 预留）：
1. 业务引用：普通读按 (tenant_id, channel_id, order_ref) 查预留；有则摘要相同 200 与当前表示，
   不同 409 ORDER_REFERENCE_CONFLICT 并记 business_ref_conflict；
2. 池为 live（FOR SHARE 读池行）；
3. SKU 不存在或属于别的租户 404（普通读，属于别的租户的记 cross_tenant）；
4. 按 (pool_id, sku_id) 升序锁余额行；
5. 按 sku_id 升序 FOR SHARE 读 SKU 行、按 product_id 升序 FOR SHARE 读商品行：
   已合并 409 SKU_MERGED，SKU 或所属商品停用 409 SKU_INACTIVE；
6. 可售不足 409 INSUFFICIENT_STOCK；
7. 取一次数据库时间，写预留、订单行与每行一条 RESERVE 流水，201。

并发重报：第 1 步是普通读，同一 order_ref 换键并发的两个请求都可能读不到预留，
后到者被先到者用掉的可售量（或其他第 2–6 步的规则）拒绝，而不是撞上唯一约束。
所以第 2–6 步的业务拒绝之后，在同一事务里再用普通读查一次 order_ref
（READ COMMITTED 下看得到已提交的那一条）：查到则抛 BusinessRefTaken，
执行器撤销本次的写入与登记的事件，在新事务里按第 1 步重新判定；查不到则照常抛出原来的拒绝，
第 3 步登记的 cross_tenant 事件随拒绝保留。写预留撞唯一约束同样抛 BusinessRefTaken。

锁序（§5.6）：预留行（FOR UPDATE）→ 订单行（FOR SHARE）→ 余额行（FOR UPDATE，按 sku_id 升序）。
订单行的 sku_id 在 SKU 合并时会改挂到规范 SKU，所以释放与关单用加锁读读订单行，
按读到的 sku_id 锁余额行：加锁读等正在进行的合并提交，读到改挂之后的 sku_id。

释放（release_reservation）在执行器的事务里按 release_decision：
RELEASE 每行一条 RELEASE（reason_code 为请求的原因），预留改为 released，记原因、时间与请求方；
EXPIRE 在同一事务里按 close_if_due 关单；UNCHANGED 不写；CONFLICT 409 STATE_CONFLICT。

到期关单（close_if_due）在调用方给的事务里运行、自己不提交；锁内复核状态与到期，
几条路径同时关同一个预留只关一次。流水只带预留的销售入口（channel_id），
不带凭据、幂等键、操作者与请求 ID，reason_code 为 expired，每次关单一个新的 command_id。
所有关单的事务都以 READ COMMITTED 开启（§5.6 隔离级别）：
- 新预留之前（close_due_before_reserving，执行器的 before_transaction 钩子，
  连接已设好 READ COMMITTED）；
- 定时任务（sweep_expired_reservations，自己从连接池取连接、设隔离级别，用完复原再放回）。
两者都用普通读查候选，逐个在各自的事务里关单并提交；一个出错回滚它、记进程日志、继续下一个；
连接已作废、回滚出错或连接断开时抛 ClosingStopped，不再继续。

表示由 reservation_representation 从预留行、订单行与数据库时间算出，它不读库：
active 而数据库时间已到 expires_at 的显示为 expired（读接口不写库，§5.6）。
"""

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, fields, replace
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, DateTime, Engine, insert, literal_column, select, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.api.auth import ObjectPool
from app.api.errors import ApiError
from app.domain.catalog import ACTIVE as CATALOG_ACTIVE
from app.domain.ids import new_id
from app.domain.ledger import EntryType
from app.domain.reservations import (
    ACTIVE,
    EXPIRED,
    EXPIRED_REASON_CODE,
    ORDER_LINE_ID_PREFIX,
    RELEASED,
    RESERVATION_ID_PREFIX,
    SOURCE_TYPE,
    ReleaseDecision,
    compute_expires_at,
    displayed_status,
    find_shortfalls,
    is_due,
    release_decision,
)
from app.models.catalog import Product, Sku
from app.models.orders import OrderLine, Reservation
from app.services.idempotency import BusinessRefTaken, CommandResult, Execution
from app.services.stock import (
    BUSINESS_REF_CONFLICT_EVENT,
    EntryIdentity,
    append_entry,
    command_now,
    entry_identity,
    format_timestamp,
    lock_balance_rows,
    lock_balances,
    new_command_id,
    require_live,
    require_not_merged,
    require_skus_exist,
    write_entry,
)

# 对象类型与错误码（§5.6、§8.3）。
RESERVATION = "reservation"
RESERVATION_NOT_FOUND = "RESERVATION_NOT_FOUND"
ORDER_REFERENCE_CONFLICT = "ORDER_REFERENCE_CONFLICT"
SKU_INACTIVE = "SKU_INACTIVE"
INSUFFICIENT_STOCK = "INSUFFICIENT_STOCK"
STATE_CONFLICT = "STATE_CONFLICT"
# ORDER_REFERENCE_CONFLICT 的 details.ref_type。
ORDER = "order"

# 新预留之前最多关的到期预留数（§5.6），超出的留给定时任务。
CLOSE_BEFORE_RESERVING_LIMIT = 100

_DUPLICATE_ENTRY = 1062  # MySQL ER_DUP_ENTRY
_DATABASE_NOW = literal_column("CURRENT_TIMESTAMP(6)", DateTime())
# 定时任务的连接用完、放回连接池之前把隔离级别设回服务器默认（§5.6 隔离级别）。
_READ_COMMITTED = "SET SESSION transaction_isolation = 'READ-COMMITTED'"
_DEFAULT_ISOLATION = "SET SESSION transaction_isolation = DEFAULT"

# 写到 app.api 的子日志器：app/api/requests.py 给 app.api 挂了输出，生产上看得到。
_log = logging.getLogger("app.api.reservations")


# --- 读出的行 ---


@dataclass(frozen=True)
class ReservationRecord:
    """reservations 的一行（表示与判定要用的列）。"""

    reservation_id: str
    channel_id: str
    order_ref: str
    pool_id: str
    status: str
    request_hash: str
    expires_at: datetime
    created_at: datetime
    release_reason: str | None
    closed_at: datetime | None


@dataclass(frozen=True)
class ReservationLine:
    """订单行的 line_ref、sku_id 与 quantity；预留请求的一行也用它。"""

    line_ref: str
    sku_id: str
    quantity: int


_RESERVATION_COLUMNS = tuple(
    getattr(Reservation, field.name) for field in fields(ReservationRecord)
)
_LINE_COLUMNS = tuple(getattr(OrderLine, field.name) for field in fields(ReservationLine))


def _reservation(row) -> ReservationRecord | None:
    return None if row is None else ReservationRecord(**row)


def read_reservation(
    connection: Connection, tenant_id: str, reservation_id: str
) -> ReservationRecord | None:
    """普通读按租户读预留；不存在或属于别的租户为 None。"""
    statement = select(*_RESERVATION_COLUMNS).where(
        Reservation.tenant_id == tenant_id, Reservation.reservation_id == reservation_id
    )
    return _reservation(connection.execute(statement).mappings().first())


def find_reservation_by_ref(
    connection: Connection, tenant_id: str, channel_id: str, order_ref: str
) -> ReservationRecord | None:
    """普通读按租户与销售入口找 order_ref。"""
    statement = select(*_RESERVATION_COLUMNS).where(
        Reservation.tenant_id == tenant_id,
        Reservation.channel_id == channel_id,
        Reservation.order_ref == order_ref,
    )
    return _reservation(connection.execute(statement).mappings().first())


def reservation_owner(connection: Connection, reservation_id: str) -> str | None:
    """预留所属的租户；不存在为 None。"""
    statement = select(Reservation.tenant_id).where(Reservation.reservation_id == reservation_id)
    return connection.execute(statement).scalar_one_or_none()


def find_reservation_pool(connection: Connection, reservation_id: str) -> ObjectPool | None:
    """池授权用：预留所属的租户与池，不按租户过滤；不存在为 None。"""
    statement = select(Reservation.tenant_id, Reservation.pool_id).where(
        Reservation.reservation_id == reservation_id
    )
    row = connection.execute(statement).first()
    return None if row is None else ObjectPool(*row)


def lock_reservation(
    connection: Connection, tenant_id: str, reservation_id: str
) -> ReservationRecord | None:
    """FOR UPDATE 按租户读预留行；不存在或属于别的租户为 None。"""
    statement = (
        select(*_RESERVATION_COLUMNS)
        .where(Reservation.tenant_id == tenant_id, Reservation.reservation_id == reservation_id)
        .with_for_update()
    )
    return _reservation(connection.execute(statement).mappings().first())


def read_lines(
    connection: Connection, tenant_id: str, reservation_id: str
) -> list[ReservationLine]:
    """普通读读预留的订单行，按 line_ref 升序。"""
    statement = (
        select(*_LINE_COLUMNS)
        .where(OrderLine.tenant_id == tenant_id, OrderLine.reservation_id == reservation_id)
        .order_by(OrderLine.line_ref)
    )
    return [ReservationLine(**row) for row in connection.execute(statement).mappings()]


def database_now(connection: Connection) -> datetime:
    """数据库时间（UTC，会话时区由 app/db.py 固定）；查询接口判到期用。"""
    return connection.execute(text("SELECT CURRENT_TIMESTAMP(6)")).scalar_one()


# --- 表示（§5.6） ---


def reservation_representation(
    reservation: ReservationRecord, lines: Sequence[ReservationLine], now: datetime
) -> dict[str, Any]:
    """预留的表示：键固定且都出现，lines 按 line_ref 升序。不读库。

    released_at 与 release_reason 只在 released 时有值；released_at 即离开 active 的时间。
    """
    released = reservation.status == RELEASED
    return {
        "reservation_id": reservation.reservation_id,
        "order_ref": reservation.order_ref,
        "pool_id": reservation.pool_id,
        "status": displayed_status(reservation.status, reservation.expires_at, now),
        "expires_at": format_timestamp(reservation.expires_at),
        "created_at": format_timestamp(reservation.created_at),
        "released_at": format_timestamp(reservation.closed_at) if released else None,
        "release_reason": reservation.release_reason if released else None,
        "lines": [
            {"line_ref": line.line_ref, "sku_id": line.sku_id, "quantity": line.quantity}
            for line in sorted(lines, key=lambda line: line.line_ref)
        ],
    }


def current_representation(
    connection: Connection, tenant_id: str, reservation: ReservationRecord
) -> dict[str, Any]:
    """普通读读出订单行与数据库时间后的表示。"""
    lines = read_lines(connection, tenant_id, reservation.reservation_id)
    return reservation_representation(reservation, lines, database_now(connection))


# --- 预留 ---


def _tenant_id(execution: Execution) -> str:
    return execution.access.principal.tenant_id


def _outside_create(execution: Execution) -> CommandResult:
    """_insert_reservation 抛的 BusinessRefTaken 由 create_reservation 换上真正的重新判定。"""
    raise RuntimeError("an order reference was taken outside create_reservation")


def _answer_existing(execution: Execution, reservation: ReservationRecord) -> CommandResult:
    """第 1 步：摘要相同 200 与当前表示（可能已是释放或到期，不能续期）；
    不同 409 ORDER_REFERENCE_CONFLICT 并记 business_ref_conflict 事件。
    """
    if reservation.request_hash == execution.request_digest:
        connection = execution.connection
        representation = current_representation(connection, _tenant_id(execution), reservation)
        return CommandResult(representation, 200)
    details = {"ref_type": ORDER, "ref": reservation.order_ref}
    execution.record_security_event(BUSINESS_REF_CONFLICT_EVENT, details)
    raise ApiError(
        409,
        ORDER_REFERENCE_CONFLICT,
        "the order reference was used with different content",
        details=details,
    )


def _require_sellable_skus(execution: Execution, sku_ids: Sequence[str]) -> None:
    """第 5 步的停用判定（已合并由 require_not_merged 先判）：
    按 sku_id 升序 FOR SHARE 读 SKU 行，再按 product_id 升序 FOR SHARE 读所属商品行；
    SKU 或所属商品 inactive 的按请求顺序列全，409 SKU_INACTIVE。
    """
    tenant_id = _tenant_id(execution)
    connection = execution.connection
    sku_statement = (
        select(Sku.sku_id, Sku.status, Sku.product_id)
        .where(Sku.tenant_id == tenant_id, Sku.sku_id.in_(set(sku_ids)))
        .order_by(Sku.sku_id)
        .with_for_update(read=True)
    )
    skus = connection.execute(sku_statement).all()
    product_ids = {row.product_id for row in skus}
    product_statement = (
        select(Product.product_id, Product.status)
        .where(Product.tenant_id == tenant_id, Product.product_id.in_(product_ids))
        .order_by(Product.product_id)
        .with_for_update(read=True)
    )
    products = connection.execute(product_statement).all()
    active_products = {product_id for product_id, status in products if status == CATALOG_ACTIVE}
    sellable = {
        row.sku_id
        for row in skus
        if row.status == CATALOG_ACTIVE and row.product_id in active_products
    }
    inactive = [sku_id for sku_id in sku_ids if sku_id not in sellable]
    if inactive:
        details = {"sku_ids": inactive}
        raise ApiError(409, SKU_INACTIVE, "sku is inactive", details=details)


def _insert_reservation(execution: Execution, values: dict[str, Any]) -> None:
    """撞 (tenant_id, channel_id, order_ref) 唯一约束时抛 BusinessRefTaken，
    由 create_reservation 换上重新判定。
    """
    try:
        execution.connection.execute(insert(Reservation).values(values))
    except IntegrityError as exc:
        if getattr(exc.orig, "args", ())[:1] == (_DUPLICATE_ENTRY,):
            raise BusinessRefTaken(_outside_create) from None
        raise


def _reserve(
    execution: Execution,
    pool_id: str,
    order_ref: str,
    lines: Sequence[ReservationLine],
    ttl_seconds: int,
) -> CommandResult:
    """第 2–7 步。"""
    require_live(execution, pool_id)
    sku_ids = [line.sku_id for line in lines]
    require_skus_exist(execution, sku_ids)
    balances = lock_balances(execution, pool_id, sku_ids)
    require_not_merged(execution, sku_ids)
    _require_sellable_skus(execution, sku_ids)
    available = {sku_id: balance.quantities.available for sku_id, balance in balances.items()}
    requested = [(line.line_ref, line.sku_id, line.quantity) for line in lines]
    shortfalls = find_shortfalls(requested, available)
    if shortfalls:
        details = {"lines": shortfalls}
        raise ApiError(409, INSUFFICIENT_STOCK, "not enough available stock", details=details)

    now = command_now(execution)
    access = execution.access
    principal = access.principal
    record = ReservationRecord(
        reservation_id=new_id(RESERVATION_ID_PREFIX),
        channel_id=principal.channel_id,
        order_ref=order_ref,
        pool_id=pool_id,
        status=ACTIVE,
        request_hash=execution.request_digest,
        expires_at=compute_expires_at(now, ttl_seconds),
        created_at=now,
        release_reason=None,
        closed_at=None,
    )
    _insert_reservation(
        execution,
        {
            "reservation_id": record.reservation_id,
            "tenant_id": principal.tenant_id,
            "channel_id": record.channel_id,
            "pool_id": pool_id,
            "order_ref": order_ref,
            "status": record.status,
            "ttl_seconds": ttl_seconds,
            "expires_at": record.expires_at,
            "request_hash": record.request_hash,
            "credential_id": principal.credential_id,
            "actor_ref": None if access.actor is None else access.actor.ref,
            "actor_role": None if access.actor is None else access.actor.role,
            "request_id": execution.request.state.request_id,
            "created_at": now,
        },
    )
    connection = execution.connection
    command_id = new_command_id()
    for line in lines:
        connection.execute(
            insert(OrderLine).values(
                order_line_id=new_id(ORDER_LINE_ID_PREFIX),
                tenant_id=principal.tenant_id,
                reservation_id=record.reservation_id,
                pool_id=pool_id,
                sku_id=line.sku_id,
                line_ref=line.line_ref,
                quantity=line.quantity,
            )
        )
        append_entry(
            execution,
            balances[line.sku_id],
            EntryType.RESERVE,
            (0, line.quantity, 0, 0),
            command_id,
            now=now,
            source_type=SOURCE_TYPE,
            source_ref=order_ref,
            line_ref=line.line_ref,
        )
    return CommandResult(reservation_representation(record, lines, now), 201)


def create_reservation(
    execution: Execution,
    pool_id: str,
    order_ref: str,
    lines: Sequence[ReservationLine],
    ttl_seconds: int,
) -> CommandResult:
    """整单预留：pool_id 是凭据销售入口绑定的池，
    lines 已经过请求校验（sku_id、line_ref 不重复）。

    已有同一 order_ref 时按 _answer_existing 回答；否则按第 2–7 步判定并写入，201。
    第 2–6 步的业务拒绝之后再查一次 order_ref，
    查到（并发的另一个请求已提交）或写预留撞唯一约束时抛 BusinessRefTaken，
    执行器在新事务里按第 1 步重新判定。
    """
    principal = execution.access.principal

    def find(execution: Execution) -> ReservationRecord | None:
        connection = execution.connection
        return find_reservation_by_ref(
            connection, principal.tenant_id, principal.channel_id, order_ref
        )

    def rejudge(execution: Execution) -> CommandResult:
        existing = find(execution)
        if existing is None:
            # 预留不删除：撞上唯一约束或再查查到之后，新事务里不会读不到。
            raise RuntimeError(f"order reference {order_ref} is not visible on rejudging")
        return _answer_existing(execution, existing)

    existing = find(execution)
    if existing is not None:
        return _answer_existing(execution, existing)
    try:
        return _reserve(execution, pool_id, order_ref, lines, ttl_seconds)
    except ApiError as error:
        if not 400 <= error.status <= 499 or find(execution) is None:
            raise
        raise BusinessRefTaken(rejudge) from None
    except BusinessRefTaken:
        raise BusinessRefTaken(rejudge) from None


# --- 放回预留的库存（释放与到期关单共用） ---


def _lock_lines(
    connection: Connection, tenant_id: str, reservation_id: str
) -> list[ReservationLine]:
    """FOR SHARE 读预留的订单行，按 line_ref 升序；等正在进行的 SKU 合并提交。"""
    statement = (
        select(*_LINE_COLUMNS)
        .where(OrderLine.tenant_id == tenant_id, OrderLine.reservation_id == reservation_id)
        .order_by(OrderLine.line_ref)
        .with_for_update(read=True)
    )
    return [ReservationLine(**row) for row in connection.execute(statement).mappings()]


def _release_lines(
    connection: Connection,
    tenant_id: str,
    reservation: ReservationRecord,
    identity: EntryIdentity,
    reason_code: str,
    now: datetime,
) -> list[ReservationLine]:
    """预留行已锁住：FOR SHARE 读订单行，按读到的 sku_id 升序锁余额行，
    每行写一条 RELEASE（reserved 减该行 quantity），共用一个 command_id。返回订单行。

    合并改挂之后两行可能指向同一个 SKU：余额行只锁一次，两条流水依次写在它上面。
    """
    lines = _lock_lines(connection, tenant_id, reservation.reservation_id)
    sku_ids = [line.sku_id for line in lines]
    balances = lock_balance_rows(connection, tenant_id, reservation.pool_id, sku_ids)
    command_id = new_command_id()
    for line in lines:
        write_entry(
            connection,
            tenant_id,
            identity,
            balances[line.sku_id],
            EntryType.RELEASE,
            (0, -line.quantity, 0, 0),
            command_id,
            now=now,
            source_type=SOURCE_TYPE,
            source_ref=reservation.order_ref,
            line_ref=line.line_ref,
            reason_code=reason_code,
        )
    return lines


# --- 到期关单（§5.2 第 4 条、§5.6） ---


def close_if_due(connection: Connection, tenant_id: str, reservation_id: str) -> bool:
    """一次关单，在调用方给的事务里运行、自己不提交。关掉了返回 True。

    FOR UPDATE 读预留行；不存在、不是 active 或数据库时间早于 expires_at，什么也不做。
    否则每行一条 RELEASE（reason_code 为 expired，身份只有预留的销售入口），
    预留改为 expired、closed_at 记数据库时间。
    """
    reservation = lock_reservation(connection, tenant_id, reservation_id)
    if reservation is None:
        return False
    now = database_now(connection)
    if not is_due(reservation.status, reservation.expires_at, now):
        return False
    identity = EntryIdentity(channel_id=reservation.channel_id)
    _release_lines(connection, tenant_id, reservation, identity, EXPIRED_REASON_CODE, now)
    statement = update(Reservation).where(
        Reservation.tenant_id == tenant_id, Reservation.reservation_id == reservation_id
    )
    connection.execute(statement.values(status=EXPIRED, closed_at=now))
    return True


class ClosingStopped(Exception):
    """连接已作废、回滚出错或连接断开：不再在这个连接上关单。"""


def _rollback_or_stop(connection: Connection) -> None:
    """一个关单出错之后回滚它；连接已作废、回滚出错时抛 ClosingStopped。"""
    if connection.invalidated:
        raise ClosingStopped("the connection was invalidated")
    try:
        connection.rollback()
    except SQLAlchemyError as exc:
        raise ClosingStopped("rolling back a failed closing failed") from exc
    if connection.invalidated:
        raise ClosingStopped("the connection was invalidated")


def _close_each(connection: Connection, candidates: Iterable[tuple[str, str]]) -> int:
    """candidates 为 (tenant_id, reservation_id)，逐个在各自的事务里关单并提交，返回关掉的个数。

    调用时连接上没有打开的事务。一个出错回滚它、记进程日志、继续下一个；
    回滚不了时抛 ClosingStopped。
    """
    closed = 0
    for tenant_id, reservation_id in candidates:
        try:
            transaction = connection.begin()
            done = close_if_due(connection, tenant_id, reservation_id)
            transaction.commit()
        except Exception:
            _log.exception("closing expired reservation %s failed", reservation_id)
            _rollback_or_stop(connection)
            continue
        if done:
            closed += 1
    return closed


def close_due_before_reserving(
    connection: Connection, tenant_id: str, pool_id: str, sku_ids: Iterable[str]
) -> int:
    """新预留开业务事务之前（执行器的 before_transaction 钩子，连接已设好 READ COMMITTED）：
    关掉本租户、本池里所列 SKU 上 active 而已到期的预留，按 expires_at 升序最多 100 个。

    查候选的普通读出错或抛 ClosingStopped 时异常传出，执行器回 503、什么也不保存。
    """
    has_line = (
        select(OrderLine.order_line_id)
        .where(
            OrderLine.tenant_id == tenant_id,
            OrderLine.pool_id == pool_id,
            OrderLine.reservation_id == Reservation.reservation_id,
            OrderLine.sku_id.in_(set(sku_ids)),
        )
        .exists()
    )
    statement = (
        select(Reservation.reservation_id)
        .where(
            Reservation.tenant_id == tenant_id,
            Reservation.pool_id == pool_id,
            Reservation.status == ACTIVE,
            Reservation.expires_at <= _DATABASE_NOW,
            has_line,
        )
        .order_by(Reservation.expires_at, Reservation.reservation_id)
        .limit(CLOSE_BEFORE_RESERVING_LIMIT)
    )
    reservation_ids = list(connection.execute(statement).scalars())
    # 结束这条读隐式开的事务：每个关单在自己的事务里。
    connection.rollback()
    return _close_each(connection, [(tenant_id, rid) for rid in reservation_ids])


def sweep_expired_reservations(engine: Engine) -> int:
    """定时任务的一次扫描：从连接池取一个连接，设为 READ COMMITTED，
    关掉全部 active 而已到期的预留（按 expires_at 升序，每个一个事务），返回关掉的个数。

    查候选出错或抛 ClosingStopped 时停止本轮、异常传出（定时任务记进程日志），
    不在新连接上接着关。连接用完先把隔离级别设回默认再放回连接池，设回出错就作废它。
    """
    connection = engine.connect()
    try:
        connection.execute(text(_READ_COMMITTED))
        connection.rollback()
        statement = (
            select(Reservation.tenant_id, Reservation.reservation_id)
            .where(Reservation.status == ACTIVE, Reservation.expires_at <= _DATABASE_NOW)
            .order_by(Reservation.expires_at, Reservation.reservation_id)
        )
        candidates = [tuple(row) for row in connection.execute(statement)]
        connection.rollback()
        closed = _close_each(connection, candidates)
    finally:
        _return_to_pool(connection)
    if closed:
        _log.info("closed %d expired reservations", closed)
    return closed


def _return_to_pool(connection: Connection) -> None:
    """设回默认隔离级别再放回连接池；连接已作废或设回出错时作废它、不放回。"""
    if not connection.invalidated:
        try:
            connection.rollback()
            connection.execute(text(_DEFAULT_ISOLATION))
            connection.rollback()
        except SQLAlchemyError:
            _log.warning(
                "resetting the isolation level failed, discarding the connection", exc_info=True
            )
            try:
                connection.invalidate()
            except SQLAlchemyError:
                _log.warning("invalidating a connection failed", exc_info=True)
    try:
        connection.close()
    except SQLAlchemyError:
        _log.warning("closing the sweep connection failed", exc_info=True)


# --- 释放（§5.6） ---


def release_reservation(execution: Execution, reservation_id: str, reason: str) -> CommandResult:
    """释放命令：FOR UPDATE 读预留行后按 release_decision。

    池授权已确认预留属于本租户，预留又不删除，读不到是程序错误。
    """
    connection = execution.connection
    tenant_id = _tenant_id(execution)
    reservation = lock_reservation(connection, tenant_id, reservation_id)
    if reservation is None:
        raise RuntimeError(f"reservation {reservation_id} is not in the tenant")
    now = command_now(execution)
    decision = release_decision(reservation.status, reservation.expires_at, now)
    if decision is ReleaseDecision.CONFLICT:
        details = {"status": reservation.status}
        raise ApiError(
            409, STATE_CONFLICT, "the reservation can no longer be released", details=details
        )
    if decision is ReleaseDecision.UNCHANGED:
        return CommandResult(current_representation(connection, tenant_id, reservation), 200)
    if decision is ReleaseDecision.EXPIRE:
        if not close_if_due(connection, tenant_id, reservation_id):
            raise RuntimeError(f"reservation {reservation_id} was due but not closed")
        closed = read_reservation(connection, tenant_id, reservation_id)
        return CommandResult(current_representation(connection, tenant_id, closed), 200)

    # 流水的 channel_id 取预留的销售入口（§5.6），其余身份取本请求的。
    identity = replace(entry_identity(execution), channel_id=reservation.channel_id)
    lines = _release_lines(connection, tenant_id, reservation, identity, reason, now)
    access = execution.access
    statement = update(Reservation).where(
        Reservation.tenant_id == tenant_id, Reservation.reservation_id == reservation_id
    )
    connection.execute(
        statement.values(
            status=RELEASED,
            release_reason=reason,
            released_credential_id=access.principal.credential_id,
            released_actor_ref=identity.actor_ref,
            released_actor_role=identity.actor_role,
            released_request_id=identity.request_id,
            closed_at=now,
        )
    )
    released = replace(reservation, status=RELEASED, release_reason=reason, closed_at=now)
    return CommandResult(reservation_representation(released, lines, now), 200)
