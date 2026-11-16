"""整单预留与预留的查询（契约 §5.2、§5.6、§7.1、§8.1）。

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

到期关单（新预留之前的关单、定时任务）与释放不在这里。

表示由 reservation_representation 从预留行、订单行与数据库时间算出，它不读库：
active 而数据库时间已到 expires_at 的显示为 expired（读接口不写库，§5.6）。
"""

from collections.abc import Sequence
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, insert, select, text
from sqlalchemy.exc import IntegrityError

from app.api.errors import ApiError
from app.domain.catalog import ACTIVE as CATALOG_ACTIVE
from app.domain.ids import new_id
from app.domain.ledger import EntryType
from app.domain.reservations import (
    ACTIVE,
    ORDER_LINE_ID_PREFIX,
    RELEASED,
    RESERVATION_ID_PREFIX,
    SOURCE_TYPE,
    compute_expires_at,
    displayed_status,
    find_shortfalls,
)
from app.models.catalog import Product, Sku
from app.models.orders import OrderLine, Reservation
from app.services.idempotency import BusinessRefTaken, CommandResult, Execution
from app.services.stock import (
    BUSINESS_REF_CONFLICT_EVENT,
    append_entry,
    command_now,
    format_timestamp,
    lock_balances,
    new_command_id,
    require_live,
    require_not_merged,
    require_skus_exist,
)

# 对象类型与错误码（§5.6、§8.3）。
RESERVATION = "reservation"
RESERVATION_NOT_FOUND = "RESERVATION_NOT_FOUND"
ORDER_REFERENCE_CONFLICT = "ORDER_REFERENCE_CONFLICT"
SKU_INACTIVE = "SKU_INACTIVE"
INSUFFICIENT_STOCK = "INSUFFICIENT_STOCK"
# ORDER_REFERENCE_CONFLICT 的 details.ref_type。
ORDER = "order"

_DUPLICATE_ENTRY = 1062  # MySQL ER_DUP_ENTRY


# --- 读出的行 ---


@dataclass(frozen=True)
class ReservationRecord:
    """reservations 的一行（表示与判定要用的列）。"""

    reservation_id: str
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
            "channel_id": principal.channel_id,
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
