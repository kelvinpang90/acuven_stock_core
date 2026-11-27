"""平台事件执行器（契约 §5.10，§3.1 下单扣配额）。

§5.10 的存放、处理顺序第 1–4、7、8 步与“每个动作调哪个命令体”的
created 一行。只有函数，不挂作业、不挂接口（由以后的任务接）：
- record_auto_event：webhook 与补拉把一个事件写进收件箱；
- orders_with_pending_events：找有未处理事件的订单，供作业用；
- process_order：处理一个订单这一轮的事件。
规则用 app/domain/platform_events.py 与 app/domain/allocations.py
的纯函数；这里取锁、开事务、读写库。

process_order 的先后：
1. 在没有打开事务的连接上 GET_LOCK(订单锁名, 5)；
   取不到或出错什么也不做，返回 LOCK_BUSY。
2. 取这个订单全部未处理的事件，按 sort_key 排序，
   逐个处理，每个事件一笔事务。
3. 开事务之前：解析涉及的订单行的 SKU（已有平台订单行取
   已定下的，created 的各行按当前 active 的平台商品，普通读）；
   用短事务预建余额行、pool_allocations 行与这个池全部
   platform 入口在这些 SKU 上的 channel_allocations 行；
   会话设为 READ COMMITTED，再关这些 SKU 上已到期的预留
   （close_due_before_reserving，同 HTTP 执行器的先后）。
   之后调模块级钩子 _between_prepare_and_lock（默认为空）。
4. 开事务，照 §5.10 第 4 步的锁序加锁，用不到的层跳过：
   收件箱行 FOR UPDATE（已处理就提交空事务）
   → 平台订单行 FOR UPDATE
   → created 再按 sku_id 升序 FOR UPDATE 锁这个入口在
     这些 SKU 上的 channel_allocations 行
   → 平台商品 FOR SHARE 重新解析 created 各行的 SKU：
     与第 3 步不同就回滚、从第 3 步重做一次，
     再不同就回滚、这一轮停下（LISTING_CHANGED）。
   然后按阶段判定：ignored 记 ignored、不改任何量；
   created 的 apply 建平台订单并扣配额；其余情形（别的动作，
   以及 missing_prior、blocked、attention）还没有处理办法：
   回滚、这一轮停下、不改任何东西（NOT_HANDLED）。
7. 写收件箱行的 applied_at（数据库时间）与 result，
   与改动一起提交。一轮结束时在同一连接上设回隔离级别、
   RELEASE_ALL_LOCKS()，出错就作废这个连接
   （同 app/services/idempotency.py 的 _release_locks）。
8. 数据库错误或程序异常：整笔回滚，收件箱行仍未处理，
   这一轮停下（FAILED）；异常不向外抛，记一行进程日志
   （不含 payload）。

created（§3.1、0.24）：插入平台订单（stage unpaid）与各行；
按 platform_line_id 升序逐行，有 SKU 的行用 take_quota
从这个入口这个 SKU 的 Q 扣走、记 quota_taken，pending 加
这一行的件数；manual_confirmed_quota 非空时同时减去
min(件数, manual_confirmed_quota)，不论入口模式，
不写 platform_ops_log。有行找不到生效平台商品时订单为 held、
attention 为 unmapped，那些行不计 pending、不扣配额，
result 记 held、result_code 记 unmapped。
"""

import hashlib
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Connection, Engine, Transaction, and_, func, insert, select, text, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.sql.expression import Executable

from app.domain.allocations import MANUAL, take_quota
from app.domain.catalog import ACTIVE
from app.domain.channels import PLATFORM
from app.domain.ids import new_id
from app.domain.platform_events import (
    CREATED,
    HASH_HEX_LENGTH,
    HELD,
    ID_MAX_LENGTH,
    PACKAGE_ID,
    REFUND_ID,
    RETURN_ID,
    RETURN_RECEIVED,
    UNPAID,
    CreatedLine,
    CreatedPayload,
    Outcome,
    Payload,
    classify,
    order_lock_name,
    order_ref,
    parse_payload,
    sort_key,
)
from app.domain.service_auth import SYSTEM
from app.models.catalog import NOW
from app.models.inventory import SalesChannel, StockBalance
from app.models.platform import (
    ChannelAllocation,
    PlatformEvent,
    PlatformOrder,
    PlatformOrderLine,
    PoolAllocation,
    SkuListing,
)
from app.models.platform_ops import PlatformConnection
from app.models.service_auth import Credential
from app.services.reservations import close_due_before_reserving

# 自动来源（§5.10 存放）；manual 由后台登记，不经这里。
WEBHOOK = "webhook"
PULL = "pull"
AUTO_SOURCES = (WEBHOOK, PULL)

# 收件箱的 result 与待处理原因。
APPLIED = "applied"
IGNORED = "ignored"
UNMAPPED = "unmapped"

EVENT_ID_PREFIX = "pev"
ORDER_ID_PREFIX = "pfo"
# 存进 event_key 的单据 ID：补拉用状态时间作 event_key，同一行同一时刻的两个包裹或两次取消
# 否则会撞去重键、后一个被静默丢掉。
DOCUMENT_ID_FIELDS = (PACKAGE_ID, REFUND_ID, RETURN_ID)

# 取订单锁最多等待的秒数（§5.10 第 1 步）；测试可以换成更短的值。
ORDER_LOCK_WAIT_SECONDS = 5
# 第 4 步重新解析的 SKU 与第 3 步不同时最多做几遍第 3、4 步。
RESOLVE_ATTEMPTS = 2

# 这一轮停下的原因。
LOCK_BUSY = "lock_busy"
LISTING_CHANGED = "listing_changed"
NOT_HANDLED = "not_handled"
FAILED = "failed"

_NO_REFERENCED_ROW = 1452  # MySQL ER_NO_REFERENCED_ROW_2：外键指向的行不存在
_READ_COMMITTED = "SET SESSION transaction_isolation = 'READ-COMMITTED'"
_DEFAULT_ISOLATION = "SET SESSION transaction_isolation = DEFAULT"

# 第 3 步之后、开事务之前在同一连接上调用（默认为空）；测试用 monkeypatch 设它。
_between_prepare_and_lock: Callable[[Connection], None] | None = None

# 写到 app.api 的子日志器：app/api/requests.py 给 app.api 挂了输出，生产上看得到。
_log = logging.getLogger("app.api.platform_events")


# --- 收件箱写入（§5.10 存放、引用格式第 1 条） ---


def _check_platform_key(value, field: str, *, allow_empty: bool) -> None:
    """平台给的订单号与行号：字符串，不超过 128 个字符；allow_empty 为假时不能是空串。"""
    if not isinstance(value, str) or len(value) > ID_MAX_LENGTH:
        raise ValueError(f"invalid {field}: {value!r}")
    if not allow_empty and not value:
        raise ValueError(f"invalid {field}: {value!r}")


def _hash_key(value: str) -> str:
    return "h" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:HASH_HEX_LENGTH]


def _stored_event_key(event_key: str, payload: Payload) -> str:
    """传入的 event_key，payload 带包裹、退款或退货 ID 时加竖线再加这个 ID；
    整串超过 128 个字符时换成 h 加整串 UTF-8 的 SHA-256 前 32 位。
    """
    stored = event_key
    for field in DOCUMENT_ID_FIELDS:
        document_id = getattr(payload, field, None)
        if document_id is not None:
            stored = f"{event_key}|{document_id}"
            break
    if len(stored) > ID_MAX_LENGTH:
        return _hash_key(stored)
    return stored


def _system_credential(connection: Connection, tenant_id: str, channel_id: str) -> str | None:
    """入口是这个租户 active 的 platform 入口、连接不是人工模式时，
    返回这个租户的 system 主体，否则 None。
    可写而租户没有 system 主体是开通的缺陷，抛 RuntimeError。
    """
    statement = (
        select(SalesChannel.kind, SalesChannel.status, PlatformConnection.mode)
        .select_from(SalesChannel)
        .outerjoin(
            PlatformConnection,
            and_(
                PlatformConnection.tenant_id == SalesChannel.tenant_id,
                PlatformConnection.channel_id == SalesChannel.channel_id,
            ),
        )
        .where(SalesChannel.tenant_id == tenant_id, SalesChannel.channel_id == channel_id)
    )
    channel = connection.execute(statement).first()
    if (
        channel is None
        or channel.kind != PLATFORM
        or channel.status != ACTIVE
        or channel.mode is None
        or channel.mode == MANUAL
    ):
        return None
    credential_id = connection.execute(
        select(Credential.credential_id)
        .where(Credential.tenant_id == tenant_id, Credential.kind == SYSTEM)
        .order_by(Credential.credential_id)
        .limit(1)
    ).scalar_one_or_none()
    if credential_id is None:
        raise RuntimeError(f"tenant {tenant_id} has no system principal")
    return credential_id


def record_auto_event(
    engine: Engine,
    tenant_id: str,
    channel_id: str,
    platform_order_id: str,
    platform_line_id: str,
    action: str,
    event_key: str,
    source: str,
    payload,
) -> bool:
    """把 webhook 或补拉的一个事件写进收件箱；写了（含重复送达、什么也不改）返回真。

    参数不合规（source 不是 webhook、pull，action 为 return_received，payload 不合格，
    created 带了平台行，订单号为空或超长）是调用方的缺陷，抛 ValueError、不写。
    入口不是这个租户 active 的 platform 入口、或它的连接为人工模式时不写，返回假
    （§5.10 引用格式第 1 条）。数据库错误照常抛出（webhook 据此回 5xx）。
    """
    if source not in AUTO_SOURCES:
        raise ValueError(f"not an automatic event source: {source!r}")
    if action == RETURN_RECEIVED:
        raise ValueError("return_received is only registered from the backoffice")
    parsed = parse_payload(action, payload)
    _check_platform_key(platform_order_id, "platform_order_id", allow_empty=False)
    # 含换行符的订单号取不了订单锁（order_lock_name 拒绝），写进来就永远处理不了。
    if "\n" in platform_order_id:
        raise ValueError(f"invalid platform_order_id: {platform_order_id!r}")
    _check_platform_key(platform_line_id, "platform_line_id", allow_empty=True)
    if action == CREATED and platform_line_id != "":
        raise ValueError("created is an order-level action; platform_line_id must be empty")
    if not isinstance(event_key, str) or not event_key:
        raise ValueError(f"invalid event_key: {event_key!r}")
    stored_key = _stored_event_key(event_key, parsed)

    with engine.connect() as connection:
        credential_id = _system_credential(connection, tenant_id, channel_id)
        # 结束读隐式开的事务：写入在自己的短事务里提交。
        connection.rollback()
        if credential_id is None:
            return False
        table = PlatformEvent.__table__
        statement = mysql_insert(table).values(
            event_row_id=new_id(EVENT_ID_PREFIX),
            tenant_id=tenant_id,
            channel_id=channel_id,
            platform_order_id=platform_order_id,
            platform_line_id=platform_line_id,
            action=action,
            event_key=stored_key,
            source=source,
            payload=payload,
            received_at=NOW,
            credential_id=credential_id,
        )
        # 重复送达撞上去重键时只把 event_row_id 设为自身，即什么也不改（§5.10 存放）。
        statement = statement.on_duplicate_key_update(event_row_id=table.c.event_row_id)
        with connection.begin():
            connection.execute(statement)
    return True


# --- 找有未处理事件的订单 ---


@dataclass(frozen=True)
class PendingOrder:
    tenant_id: str
    channel_id: str
    platform_order_id: str


def orders_with_pending_events(engine: Engine, limit: int) -> list[PendingOrder]:
    """有未处理事件的订单：按各订单最早未处理事件的 received_at 升序、
    再按三列升序取前 limit 个。

    一直处理不掉的订单会排在前面；作业怎样不被它们占满由作业定。
    """
    if type(limit) is not int or limit < 1:
        raise ValueError(f"limit must be a positive integer, got {limit!r}")
    key = (PlatformEvent.tenant_id, PlatformEvent.channel_id, PlatformEvent.platform_order_id)
    statement = (
        select(*key)
        .where(PlatformEvent.applied_at.is_(None))
        .group_by(*key)
        .order_by(func.min(PlatformEvent.received_at), *key)
        .limit(limit)
    )
    with engine.connect() as connection:
        return [PendingOrder(*row) for row in connection.execute(statement)]


# --- 处理一个订单 ---


@dataclass(frozen=True)
class RoundResult:
    """这一轮的结果：写了 applied_at 的事件个数，停在哪个事件（event_row_id）与原因。

    处理完时 stopped_at 与 reason 都为 None；锁忙时 stopped_at 为 None。
    """

    processed: int
    stopped_at: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class _Order:
    tenant_id: str
    channel_id: str
    platform_order_id: str


@dataclass(frozen=True)
class _Channel:
    pool_id: str
    platform: str | None
    platform_shop_id: str | None


@dataclass(frozen=True)
class _Event:
    event_row_id: str
    action: str
    payload: Payload
    received_at: datetime


@dataclass(frozen=True)
class _Prepared:
    """第 3 步的结果：created 各行解析出的 SKU（找不到为 None；
    别的动作为空）与涉及的 SKU。
    """

    skus: Mapping[str, str | None]
    sku_ids: tuple[str, ...]  # 升序


@dataclass
class _Allocation:
    """锁住的 channel_allocations 行，扣配额时就地改。"""

    quota: int
    pending: int
    manual_confirmed_quota: int | None


# 第 4 步的结果（不向外）。
_DONE = "done"  # 写了 applied_at
_ALREADY_APPLIED = "already_applied"  # 收件箱行已处理过，提交了空事务
_RESOLVE_AGAIN = "resolve_again"  # 重新解析的 SKU 与第 3 步不同


def _check_ttl(reservation_ttl_seconds) -> None:
    if type(reservation_ttl_seconds) is not int or reservation_ttl_seconds < 1:
        raise ValueError(
            f"reservation_ttl_seconds must be a positive integer, got {reservation_ttl_seconds!r}"
        )


def process_order(
    engine: Engine,
    tenant_id: str,
    channel_id: str,
    platform_order_id: str,
    *,
    reservation_ttl_seconds: int,
) -> RoundResult:
    """处理一个订单这一轮的事件（§5.10 处理顺序）；异常不向外抛。

    预留有效期由调用方传入（以后作业从 Settings 取），执行器不读配置。
    """
    _check_ttl(reservation_ttl_seconds)
    order = _Order(tenant_id, channel_id, platform_order_id)
    name = order_lock_name(engine.url.database or "", tenant_id, channel_id, platform_order_id)
    try:
        connection = engine.connect()
    except SQLAlchemyError:
        _log.warning("connecting for a platform order failed", exc_info=True)
        return RoundResult(0, None, LOCK_BUSY)
    try:
        if not _acquire(connection, name):
            return RoundResult(0, None, LOCK_BUSY)
        return _run_round(connection, order)
    finally:
        _finish(connection)


def _acquire(connection: Connection, name: str) -> bool:
    """GET_LOCK(订单锁名, 等待秒数)：1 取到；0（超时）、NULL 或出错
    都算取不到，什么也不写。
    """
    try:
        acquired = connection.execute(
            text("SELECT GET_LOCK(:name, :timeout)"),
            {"name": name, "timeout": ORDER_LOCK_WAIT_SECONDS},
        ).scalar()
        # 结束这条语句隐式开的事务：之后的读在取到锁之后才建立快照。
        connection.rollback()
    except SQLAlchemyError:
        _log.warning("GET_LOCK on a platform order failed", exc_info=True)
        return False
    return acquired == 1


def _finish(connection: Connection) -> None:
    """设回默认隔离级别、释放命名锁，再放回连接池；连接已作废或出错时作废它、不放回。"""
    if not connection.invalidated:
        try:
            connection.rollback()
            connection.execute(text(_DEFAULT_ISOLATION))
            connection.execute(text("SELECT RELEASE_ALL_LOCKS()"))
            connection.rollback()
        except SQLAlchemyError:
            _log.warning(
                "releasing a platform order failed, discarding the connection", exc_info=True
            )
            try:
                connection.invalidate()
            except SQLAlchemyError:
                _log.warning("invalidating a connection failed", exc_info=True)
    try:
        connection.close()
    except SQLAlchemyError:
        _log.warning("closing a platform order connection failed", exc_info=True)


def _log_failure(order: _Order, event_row_id: str | None, error: BaseException) -> None:
    """一行进程日志：订单、事件、异常类型与 MySQL 错误码，不含 payload 与语句参数。"""
    code = getattr(getattr(error, "orig", None), "args", ())[:1]
    _log.warning(
        "platform event processing failed (tenant_id=%s, channel_id=%s, platform_order_id=%s,"
        " event_row_id=%s): %s%s",
        order.tenant_id,
        order.channel_id,
        order.platform_order_id,
        event_row_id,
        type(error).__name__,
        f" {code[0]}" if code else "",
    )


def _rollback_quietly(connection: Connection) -> None:
    if connection.invalidated:
        return
    try:
        connection.rollback()
    except SQLAlchemyError:
        _log.warning("rolling back failed", exc_info=True)


def _run_round(connection: Connection, order: _Order) -> RoundResult:
    try:
        channel = _read_channel(connection, order)
        events = _pending_events(connection, order)
        connection.rollback()
    except Exception as error:
        _rollback_quietly(connection)
        _log_failure(order, None, error)
        return RoundResult(0, None, FAILED)

    processed = 0
    for event in events:
        outcome = _process_event(connection, order, channel, event)
        if outcome == _DONE:
            processed += 1
        elif outcome != _ALREADY_APPLIED:
            return RoundResult(processed, event.event_row_id, outcome)
    return RoundResult(processed)


def _read_channel(connection: Connection, order: _Order) -> _Channel:
    """订单的入口；收件箱行的外键保证它在这个租户里，读不到是程序错误。"""
    columns = (SalesChannel.pool_id, SalesChannel.platform, SalesChannel.platform_shop_id)
    statement = select(*columns).where(
        SalesChannel.tenant_id == order.tenant_id,
        SalesChannel.channel_id == order.channel_id,
    )
    row = connection.execute(statement).first()
    if row is None:
        raise RuntimeError(f"channel {order.channel_id} is not in the tenant")
    return _Channel(row.pool_id, row.platform, row.platform_shop_id)


def _pending_events(connection: Connection, order: _Order) -> list[_Event]:
    """第 2 步：这个订单全部未处理的事件，按 sort_key 排好。"""
    statement = select(
        PlatformEvent.event_row_id,
        PlatformEvent.action,
        PlatformEvent.payload,
        PlatformEvent.received_at,
    ).where(
        PlatformEvent.tenant_id == order.tenant_id,
        PlatformEvent.channel_id == order.channel_id,
        PlatformEvent.platform_order_id == order.platform_order_id,
        PlatformEvent.applied_at.is_(None),
    )
    events = [
        _Event(
            event_row_id=row.event_row_id,
            action=row.action,
            payload=parse_payload(row.action, row.payload),
            received_at=row.received_at,
        )
        for row in connection.execute(statement)
    ]
    return sorted(
        events,
        key=lambda event: sort_key(
            event.payload.platform_time, event.action, event.received_at, event.event_row_id
        ),
    )


def _process_event(connection: Connection, order: _Order, channel: _Channel, event: _Event) -> str:
    """第 3、4、7、8 步；返回 _DONE、_ALREADY_APPLIED 或这一轮停下的原因。"""
    for _attempt in range(RESOLVE_ATTEMPTS):
        try:
            prepared = _prepare(connection, order, channel, event)
            hook = _between_prepare_and_lock
            if hook is not None:
                hook(connection)
                # 结束钩子读库时隐式开的事务：第 4 步的事务在这之后才开。
                connection.rollback()
            outcome = _locked(connection, order, channel, event, prepared)
        except Exception as error:
            _rollback_quietly(connection)
            _log_failure(order, event.event_row_id, error)
            return FAILED
        if outcome != _RESOLVE_AGAIN:
            return outcome
    return LISTING_CHANGED


# --- 第 3 步：开事务之前 ---


def _resolve_listings(
    connection: Connection, order: _Order, lines: Iterable[CreatedLine], *, share: bool
) -> dict[str, str | None]:
    """按当前 active 的平台商品解析各行的 SKU：platform_line_id → sku_id，找不到为 None。

    share 为真时 FOR SHARE 读（第 4 步）；按 (平台商品 ID, 规格 ID) 升序逐个读。
    """
    lines = list(lines)
    keys = sorted({(line.platform_item_id, line.platform_model_id) for line in lines})
    found: dict[tuple[str, str], str | None] = {}
    for item_id, model_id in keys:
        statement = select(SkuListing.sku_id, SkuListing.status).where(
            SkuListing.tenant_id == order.tenant_id,
            SkuListing.channel_id == order.channel_id,
            SkuListing.platform_item_id == item_id,
            SkuListing.platform_model_id == model_id,
        )
        if share:
            statement = statement.with_for_update(read=True)
        row = connection.execute(statement).first()
        # 平台规格在这个入口上唯一（uq_sku_listings_platform_ids）；
        # inactive 的行算找不到。
        active = row is not None and row.status == ACTIVE
        found[(item_id, model_id)] = row.sku_id if active else None
    return {
        line.platform_line_id: found[(line.platform_item_id, line.platform_model_id)]
        for line in lines
    }


def _settled_skus(connection: Connection, order: _Order) -> list[str]:
    """已有平台订单行上已定下的 SKU（升序）。

    created 以外的动作还没有处理办法，取这个订单的全部订单行（多建配额行无害）。
    """
    statement = (
        select(PlatformOrderLine.sku_id)
        .join(
            PlatformOrder,
            and_(
                PlatformOrder.tenant_id == PlatformOrderLine.tenant_id,
                PlatformOrder.platform_order_row_id == PlatformOrderLine.platform_order_row_id,
            ),
        )
        .where(
            PlatformOrder.tenant_id == order.tenant_id,
            PlatformOrder.channel_id == order.channel_id,
            PlatformOrder.platform_order_id == order.platform_order_id,
            PlatformOrderLine.sku_id.is_not(None),
        )
        .distinct()
    )
    return sorted(connection.execute(statement).scalars())


def _platform_channels(connection: Connection, tenant_id: str, pool_id: str) -> list[str]:
    """这个池全部 platform 入口（不论状态），按 channel_id 升序。"""
    statement = (
        select(SalesChannel.channel_id)
        .where(
            SalesChannel.tenant_id == tenant_id,
            SalesChannel.pool_id == pool_id,
            SalesChannel.kind == PLATFORM,
        )
        .order_by(SalesChannel.channel_id)
    )
    return list(connection.execute(statement).scalars())


def _balance_upsert(tenant_id: str, pool_id: str, sku_id: str) -> Executable:
    """同 app/services/idempotency.py 的余额行预建：只给四列，已有的行不改。"""
    table = StockBalance.__table__
    statement = mysql_insert(table).values(
        balance_id=new_id("bal"), tenant_id=tenant_id, pool_id=pool_id, sku_id=sku_id
    )
    return statement.on_duplicate_key_update(balance_id=table.c.balance_id)


def _pool_allocation_upsert(tenant_id: str, pool_id: str, sku_id: str) -> Executable:
    table = PoolAllocation.__table__
    statement = mysql_insert(table).values(tenant_id=tenant_id, pool_id=pool_id, sku_id=sku_id)
    return statement.on_duplicate_key_update(sku_id=table.c.sku_id)


def _channel_allocation_upsert(
    tenant_id: str, channel_id: str, pool_id: str, sku_id: str
) -> Executable:
    table = ChannelAllocation.__table__
    statement = mysql_insert(table).values(
        tenant_id=tenant_id, channel_id=channel_id, pool_id=pool_id, sku_id=sku_id
    )
    return statement.on_duplicate_key_update(sku_id=table.c.sku_id)


def _upsert(connection: Connection, statement: Executable) -> None:
    """外键失败（1452）只回滚这一条语句、跳过；别的错误照抛。"""
    try:
        connection.execute(statement)
    except IntegrityError as exc:
        if getattr(exc.orig, "args", ())[:1] != (_NO_REFERENCED_ROW,):
            raise


def _prepare(connection: Connection, order: _Order, channel: _Channel, event: _Event) -> _Prepared:
    """第 3 步：解析 SKU，预建余额行与两级配额行，设 READ COMMITTED，关已到期的预留。

    返回时连接上没有打开的事务。
    """
    if event.action == CREATED:
        payload: CreatedPayload = event.payload
        skus = _resolve_listings(connection, order, payload.lines, share=False)
        sku_ids = sorted({sku_id for sku_id in skus.values() if sku_id is not None})
    else:
        skus = {}
        sku_ids = _settled_skus(connection, order)
    channel_ids = _platform_channels(connection, order.tenant_id, channel.pool_id)
    # 结束读隐式开的事务：预建在自己的短事务里提交。
    connection.rollback()
    if sku_ids:
        tenant_id, pool_id = order.tenant_id, channel.pool_id
        with connection.begin():
            for sku_id in sku_ids:
                _upsert(connection, _balance_upsert(tenant_id, pool_id, sku_id))
            for sku_id in sku_ids:
                _upsert(connection, _pool_allocation_upsert(tenant_id, pool_id, sku_id))
            for sku_id in sku_ids:
                for channel_id in channel_ids:
                    statement = _channel_allocation_upsert(tenant_id, channel_id, pool_id, sku_id)
                    _upsert(connection, statement)
    connection.execute(text(_READ_COMMITTED))
    connection.rollback()
    if sku_ids:
        close_due_before_reserving(connection, order.tenant_id, channel.pool_id, sku_ids)
        connection.rollback()
    return _Prepared(skus, tuple(sku_ids))


# --- 第 4、7 步：事务里 ---


def _locked(
    connection: Connection, order: _Order, channel: _Channel, event: _Event, prepared: _Prepared
) -> str:
    """开事务加锁、判定、改量并写收件箱行；返回 _DONE、_ALREADY_APPLIED、_RESOLVE_AGAIN 或
    NOT_HANDLED。只有 _DONE 与 _ALREADY_APPLIED 提交，其余回滚；异常时回滚再抛出。
    """
    transaction = connection.begin()
    try:
        outcome = _decide(connection, order, channel, event, prepared)
        if outcome in (_DONE, _ALREADY_APPLIED):
            transaction.commit()
        else:
            transaction.rollback()
        return outcome
    except BaseException:
        _rollback_transaction(transaction)
        raise


def _rollback_transaction(transaction: Transaction) -> None:
    if not transaction.is_active:
        return
    try:
        transaction.rollback()
    except SQLAlchemyError:
        _log.warning("rolling back failed", exc_info=True)


def _decide(
    connection: Connection, order: _Order, channel: _Channel, event: _Event, prepared: _Prepared
) -> str:
    applied_at = connection.execute(
        select(PlatformEvent.applied_at)
        .where(
            PlatformEvent.tenant_id == order.tenant_id,
            PlatformEvent.event_row_id == event.event_row_id,
        )
        .with_for_update()
    ).scalar_one()
    if applied_at is not None:
        # 处理过了（去重）：提交空事务。
        return _ALREADY_APPLIED

    platform_order = connection.execute(
        select(PlatformOrder.stage, PlatformOrder.ready_at)
        .where(
            PlatformOrder.tenant_id == order.tenant_id,
            PlatformOrder.channel_id == order.channel_id,
            PlatformOrder.platform_order_id == order.platform_order_id,
        )
        .with_for_update()
    ).first()
    stage = None if platform_order is None else platform_order.stage
    ready_at_set = platform_order is not None and platform_order.ready_at is not None

    allocations: dict[str, _Allocation] = {}
    if event.action == CREATED and platform_order is None:
        allocations = _lock_allocations(connection, order, prepared.sku_ids)
        skus = _resolve_listings(connection, order, event.payload.lines, share=True)
        if skus != prepared.skus:
            return _RESOLVE_AGAIN

    classification = classify(stage, event.action, ready_at_set)
    if classification.outcome is Outcome.IGNORED:
        _mark_applied(connection, order, event, IGNORED, None)
        return _DONE
    if classification.outcome is Outcome.APPLY and event.action == CREATED:
        result, result_code = _apply_created(
            connection, order, channel, event.payload, prepared.skus, allocations
        )
        _mark_applied(connection, order, event, result, result_code)
        return _DONE
    return NOT_HANDLED


def _lock_allocations(
    connection: Connection, order: _Order, sku_ids: Iterable[str]
) -> dict[str, _Allocation]:
    """按 sku_id 升序 FOR UPDATE 锁这个入口在这些 SKU 上的 channel_allocations 行。

    行在第 3 步预建过；读不到是程序错误。
    """
    allocations: dict[str, _Allocation] = {}
    for sku_id in sorted(set(sku_ids)):
        row = connection.execute(
            select(
                ChannelAllocation.quota,
                ChannelAllocation.pending,
                ChannelAllocation.manual_confirmed_quota,
            )
            .where(
                ChannelAllocation.tenant_id == order.tenant_id,
                ChannelAllocation.channel_id == order.channel_id,
                ChannelAllocation.sku_id == sku_id,
            )
            .with_for_update()
        ).first()
        if row is None:
            raise RuntimeError(f"channel allocation for sku {sku_id} was not pre-created")
        allocations[sku_id] = _Allocation(row.quota, row.pending, row.manual_confirmed_quota)
    return allocations


def _insert_order(
    connection: Connection,
    order: _Order,
    platform_order_row_id: str,
    ref: str,
    stage: str,
    attention: str | None,
) -> None:
    """写平台订单一行；其余列取数据库默认值。"""
    connection.execute(
        insert(PlatformOrder).values(
            platform_order_row_id=platform_order_row_id,
            tenant_id=order.tenant_id,
            channel_id=order.channel_id,
            platform_order_id=order.platform_order_id,
            order_ref=ref,
            stage=stage,
            attention=attention,
            attention_code=None,
        )
    )


def _apply_created(
    connection: Connection,
    order: _Order,
    channel: _Channel,
    payload: CreatedPayload,
    skus: Mapping[str, str | None],
    allocations: dict[str, _Allocation],
) -> tuple[str, str | None]:
    """建平台订单与各行，按 §3.1 扣配额、加 pending；
    返回收件箱行的 result 与 result_code。
    """
    unmapped = any(skus[line.platform_line_id] is None for line in payload.lines)
    platform_order_row_id = new_id(ORDER_ID_PREFIX)
    _insert_order(
        connection,
        order,
        platform_order_row_id,
        order_ref(channel.platform, channel.platform_shop_id, order.platform_order_id),
        HELD if unmapped else UNPAID,
        UNMAPPED if unmapped else None,
    )

    rows = []
    changed: set[str] = set()
    for line in sorted(payload.lines, key=lambda line: line.platform_line_id):
        sku_id = skus[line.platform_line_id]
        taken = 0
        if sku_id is not None:
            allocation = allocations[sku_id]
            taken, allocation.quota = take_quota(line.quantity, allocation.quota)
            allocation.pending += line.quantity
            # 0.24：卖家后台的显示量随平台卖出自己减，最多减到 0（§3.2）。
            if allocation.manual_confirmed_quota is not None:
                allocation.manual_confirmed_quota -= min(
                    line.quantity, allocation.manual_confirmed_quota
                )
            changed.add(sku_id)
        rows.append(
            {
                "tenant_id": order.tenant_id,
                "platform_order_row_id": platform_order_row_id,
                "platform_line_id": line.platform_line_id,
                "platform_item_id": line.platform_item_id,
                "platform_model_id": line.platform_model_id,
                "sku_id": sku_id,
                "quantity": line.quantity,
                "quota_taken": taken,
            }
        )
    connection.execute(insert(PlatformOrderLine), rows)

    for sku_id in sorted(changed):
        allocation = allocations[sku_id]
        connection.execute(
            update(ChannelAllocation)
            .where(
                ChannelAllocation.tenant_id == order.tenant_id,
                ChannelAllocation.channel_id == order.channel_id,
                ChannelAllocation.sku_id == sku_id,
            )
            .values(
                quota=allocation.quota,
                pending=allocation.pending,
                manual_confirmed_quota=allocation.manual_confirmed_quota,
                updated_at=NOW,
            )
        )
    if unmapped:
        return HELD, UNMAPPED
    return APPLIED, None


def _mark_applied(
    connection: Connection, order: _Order, event: _Event, result: str, result_code: str | None
) -> None:
    """第 7 步：applied_at 取数据库时间，与改动同一事务提交。"""
    connection.execute(
        update(PlatformEvent)
        .where(
            PlatformEvent.tenant_id == order.tenant_id,
            PlatformEvent.event_row_id == event.event_row_id,
        )
        .values(applied_at=NOW, result=result, result_code=result_code)
    )
