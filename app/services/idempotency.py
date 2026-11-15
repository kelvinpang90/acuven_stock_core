"""幂等执行器：所有 /v1 写命令共用（契约 §7.1、§7.2、§8.1 检查顺序第 10 步、§8.3）。

摘要、锁名、重放判定与保存范围用 app/domain/idempotency.py 的纯函数；
这里取命名锁、开事务、查与写幂等记录、执行命令、释放锁、写安全事件。

接入写接口（第 1–9 步都是依赖，进入接口函数时已经通过；
它们的拒绝不保存，调用方改正后可以同键重发）：

    RECEIPTS_CREATE = "receipts.create"

    @app.post("/v1/receipts", dependencies=[Depends(authorize(STOCK_WRITE, write=True))])
    def create_receipt(
        request: Request,
        body: Annotated[ParsedBody[Receipt], Depends(receipt_body)],
        actor: Annotated[Actor, Depends(require_role(STOCK_OPERATOR, STOCK_ADMIN))],
        pool_id: Annotated[str, Depends(require_pool(receipt_pool))],
    ) -> Response:
        return run_idempotent(
            request,
            operation=RECEIPTS_CREATE,
            body=body,
            command=lambda execution: _create_receipt(execution, body.model, actor, pool_id),
        )

- 操作类型：每个写接口一个固定名字，写成模块级常量；点分的小写名（如 reservations.create），
  最长 64 字符。它与路径参数名进摘要或锁名，定下后不改名。
- 路径参数由执行器从 request.path_params 放进摘要；请求体传第 7 步的 ParsedBody
  （摘要用它的 raw，不补默认值），没有请求体的接口传 None。
- authorize 要写 write=True：幂等键从第 6 步的结果取。
- 命令 command(execution) 在执行器已开的事务里运行，用 execution.connection，
  自己不开、不提交、不回滚事务：
  - 成功返回 CommandResult(data, status)，status 为 2xx；
    响应体是 {"data": data, "request_id": ...}，data 要能直接序列化成 JSON（时间先转成字符串）；
  - 业务拒绝抛 ApiError（4xx）：默认撤销命令的全部写入，只保存幂等记录。
    契约规定被拒时也要留下的写入（如 §5.2 晚到支付缺货转待审）
    在抛出之前用 execution.keep_on_rejection(write) 登记：
    撤销之后在同一事务里执行 write(connection)，与幂等记录一起提交；
  - 抛别的异常或数据库出错：整笔回滚、不保存，返回 500 INTERNAL_ERROR，同键重试会重新执行；
  - 事务里发现的安全事件（跨租户 404、业务引用冲突）
    用 execution.record_security_event(类型, 补充说明) 登记，
    不要在事务里直接写（不调 record_security_event 与 cross_tenant_not_found）。
    执行器在事务结束、命名锁释放之后写：命令成功、业务拒绝或抛异常时都写；
    写幂等记录撞上唯一约束、命令被回滚时丢弃；重放时命令不执行，也就不写。
- 接口函数原样返回 run_idempotent 的结果：首次结果、重放或冲突；
  重放带 Idempotency-Replayed: true，X-Request-ID 头是本次的。
  保存的响应体是首次返回的完整响应体（含当时的 request_id）；首次的响应头不保存。

目录写锁（契约 §2.7、§7.1 执行顺序第 3 步）：

- 写目录的命令调用 run_idempotent(..., catalog_lock=True)；
  不写目录的命令不传（默认 False），行为不变。
  凡是写目录变更记录（catalog_changes）的路径都要这样声明。
- 声明了的命令在取到幂等锁之后、开事务之前，在同一连接上、没有打开事务时
  GET_LOCK(目录锁名, CATALOG_LOCK_WAIT_SECONDS) 取本租户的目录写锁；锁名用
  app/domain/catalog.py 的 catalog_lock_name，数据库名的取法与幂等锁名相同。
  取到之后才开事务，读快照建立在目录锁之后。
- 返回 0（等待超时）、NULL 或数据库出错：503 UNAVAILABLE，什么也不写、
  不保存幂等记录，同键重试会重新执行。
  幂等锁返回 0 时照旧立即 409 REQUEST_IN_PROGRESS，不去取目录锁。
- 目录写锁与幂等锁都由 RELEASE_ALL_LOCKS() 一并释放，无论命令成功、业务拒绝、抛异常还是 503。
- 开事务前的先后固定为：幂等锁 → 目录写锁（只有写目录的命令）→ 余额行预建的短事务
  （§4.1，只有要锁余额行的命令）→ 开业务事务。不在打开事务或持有行锁时带等待地 GET_LOCK。

余额行预建（契约 §4.1、§6.2 第 5 条）：

- 要锁余额行的命令调用 run_idempotent(..., balance_rows=函数)；函数收连接、返回要预建的
  (pool_id, sku_id) 列表。不传（默认 None）的命令不预建，行为不变。
- 在取到幂等锁（与目录写锁）之后、开业务事务之前，执行器在同一连接上调用它；它可以读库
  （例如期初确认按导入记录取 SKU），不提交、不写；读完由执行器结束这条读隐式开的事务。
- 然后开一个短事务：去重后按 (pool_id, sku_id) 升序逐行
  INSERT … ON DUPLICATE KEY UPDATE balance_id = balance_id，只给 balance_id（前缀 bal_）、
  tenant_id（凭据的租户）、pool_id、sku_id 四列，其余取数据库默认值（四个量与各版本为 0、
  opening_entry_id 为空）；已有的行不改。
  不用 INSERT IGNORE（会把外键、检查约束错误当警告吞掉）。
- 某行因外键失败（MySQL 1452：SKU 或池不存在、属于别的租户）跳过、继续下一行，不报错；
  业务事务照该命令自己的规则报 404。全部插完提交，之后才开业务事务。
- 预建遇到别的错误（含函数自己出错）：什么也不保存，500 INTERNAL_ERROR，同键重试会重新执行。
- 同键重放时预建照样做：全零的行与没有行等价，多建无害。
- 业务事务里照 §8.1 升序 SELECT … FOR UPDATE 锁这些行；
  不在业务事务里对不存在的行加锁再插入。

本请求的摘要：

- execution.request_digest 是本请求的规范化请求摘要（§7.1），由路径参数与请求体算出，
  与幂等记录的 request_hash 相同。命令写业务引用时存它，换键重报同一引用时拿它比较内容是否相同
  （§6.2 第 7 条、§7.1 第 2 条）。

并发撞上业务引用（契约 §7.1 第 2 条最后一段、§6.2 第 7 条）：

- 两个不同幂等键的请求并发写同一业务引用时，后到的插入撞上唯一约束（MySQL 1062）。
  命令捕获这个错误后 raise BusinessRefTaken(rejudge)；rejudge(execution) 按已生效的那一条
  重新判定，返回 CommandResult 或抛 ApiError，规则与命令判定换键重报时相同。
- 执行器收到后：回滚命令的全部写入，丢弃命令登记的安全事件与 keep_on_rejection 的写入，
  在同一连接上开新事务（读快照建立在回滚之后，看得到已提交的那一条），调用 rejudge；
  它的结果（2xx 或 4xx）照常作为后到请求那个幂等键的确定结果保存并提交，同键重试原样重放。
  rejudge 登记的安全事件与 keep_on_rejection 照常处理。
- rejudge 抛别的异常（包括再抛 BusinessRefTaken）或数据库出错：回滚、不保存、500。
  写幂等记录撞唯一约束时照上面第 4 步重放或冲突。

READ COMMITTED（契约 §4.1 下文、§6.6）：

- run_idempotent(..., read_committed=True) 的业务事务（以及 BusinessRefTaken 的重新判定事务）
  以 READ COMMITTED 开启：开业务事务之前把会话的隔离级别设为 READ-COMMITTED，
  请求结束、释放命名锁时设回服务器默认（SET SESSION transaction_isolation = DEFAULT）；
  设回出错时把连接作废，不放回池里。不传（默认 False）的命令照旧用默认的 REPEATABLE READ。
- READ COMMITTED 下普通读每条语句都看到最新已提交的数据，加锁读不加间隙锁。
  盘点单命令用它：锁住盘点单行之后用普通读读盘点行
  （REPEATABLE READ 下加锁读一个空范围会锁间隙，两张单的首次提交互相等待而死锁）。
- 设隔离级别出错：503 UNAVAILABLE，什么也不写、不保存，同键重试会重新执行。
"""

import json
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import Connection, Transaction, insert, select, text
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.sql.expression import Executable

from app.api.auth import Access, get_access
from app.api.errors import ApiError, error_response, internal_error, ok
from app.api.requests import (
    ParsedBody,
    current_connection,
    discard_connection,
    invalidate_connection,
    request_connection,
    unavailable,
)
from app.domain.catalog import catalog_lock_name
from app.domain.idempotency import (
    IDEMPOTENCY_CONFLICT,
    REQUEST_IN_PROGRESS,
    Decision,
    decide,
    is_final_result,
    lock_name,
    request_digest,
)
from app.domain.ids import new_id
from app.models.inventory import IdempotencyRecord, StockBalance
from app.services.security_events import SecurityEventRecord, record_security_event

REPLAYED = "Idempotency-Replayed"
# 安全事件类型（契约 §7.2）。
IDEMPOTENCY_CONFLICT_EVENT = "idempotency_conflict"
# 取目录写锁最多等待的秒数（§2.7）；测试可以换成更短的值。
CATALOG_LOCK_WAIT_SECONDS = 5

_OPERATION = re.compile(r"[a-z][a-z0-9_-]*(\.[a-z][a-z0-9_-]*)+")
_OPERATION_MAX_LENGTH = 64
_DUPLICATE_ENTRY = 1062  # MySQL ER_DUP_ENTRY
_NO_REFERENCED_ROW = 1452  # MySQL ER_NO_REFERENCED_ROW_2：外键指向的行不存在

# 余额行预建函数：收连接，返回要预建的 (pool_id, sku_id)。
BalanceRows = Callable[[Connection], Iterable[tuple[str, str]]]

_log = logging.getLogger("app.api")


@dataclass(frozen=True)
class CommandResult:
    """命令成功的结果；status 为 2xx。"""

    data: Any
    status: int = 200


@dataclass(frozen=True)
class _Scope:
    """契约 §7.1 的作用域。"""

    tenant_id: str
    credential_id: str
    operation: str
    idempotency_key: str


class Execution:
    """交给命令的执行上下文：本请求、事务已开的连接，以及登记事件与被拒仍保留的写入的方法。"""

    def __init__(self, request: Request, connection: Connection, operation: str, digest: str):
        self.request = request
        self.connection = connection
        self.operation = operation
        self._digest = digest
        self.events: list[SecurityEventRecord] = []
        self.kept_writes: list[Callable[[Connection], None]] = []

    @property
    def access(self) -> Access:
        return get_access(self.request)

    @property
    def request_digest(self) -> str:
        """本请求的规范化请求摘要（§7.1），与幂等记录的 request_hash 相同。"""
        return self._digest

    def record_security_event(self, event_type: str, details: dict[str, Any]) -> None:
        """暂存一条安全事件，事务结束、命名锁释放之后写。"""
        self.events.append(_event(self.request, event_type, details))

    def keep_on_rejection(self, write: Callable[[Connection], None]) -> None:
        """命令以业务 4xx 拒绝时仍要保留的写入：撤销命令的写入之后，在同一事务里执行。"""
        self.kept_writes.append(write)


class BusinessRefTaken(Exception):
    """命令写业务引用撞上唯一约束时抛出（§7.1 第 2 条最后一段）。

    rejudge(execution) 在新事务里按已生效的那一条重新判定，返回 CommandResult 或抛 ApiError。
    """

    def __init__(self, rejudge: Callable[[Execution], CommandResult]):
        super().__init__("the business reference was taken by a concurrent request")
        self.rejudge = rejudge


def _event(request: Request, event_type: str, details: dict[str, Any]) -> SecurityEventRecord:
    access = get_access(request)
    return SecurityEventRecord(
        event_type=event_type,
        request_id=request.state.request_id,
        tenant_id=access.principal.tenant_id,
        credential_id=access.principal.credential_id,
        # 第 6 步已校验：操作者头要么都带且都合格，要么都不带。
        actor_ref=None if access.actor is None else access.actor.ref,
        http_method=request.method,
        path=request.scope["path"],  # 不含查询串
        user_agent=request.headers.get("user-agent"),
        details=details,
    )


def run_idempotent(
    request: Request,
    *,
    operation: str,
    body: ParsedBody | None,
    command: Callable[[Execution], CommandResult],
    catalog_lock: bool = False,
    balance_rows: BalanceRows | None = None,
    read_committed: bool = False,
) -> Response:
    """第 10 步：取命名锁，重放、冲突或执行命令，释放锁，再写安全事件。

    catalog_lock 为真时（写目录的命令）取到幂等锁之后、开事务之前再取本租户的目录写锁。
    balance_rows 给了时，取锁之后、开业务事务之前用单独的短事务预建它返回的余额行（§4.1）。
    read_committed 为真时业务事务（与重新判定事务）以 READ COMMITTED 开启。
    """
    if (
        not isinstance(operation, str)
        or len(operation) > _OPERATION_MAX_LENGTH
        or not _OPERATION.fullmatch(operation)
    ):
        raise ValueError(f"invalid operation name: {operation!r}")
    access = get_access(request)
    if access.idempotency_key is None:
        raise RuntimeError("a write endpoint needs authorize(..., write=True)")
    scope = _Scope(
        access.principal.tenant_id,
        access.principal.credential_id,
        operation,
        access.idempotency_key,
    )
    digest = request_digest(dict(request.path_params), None if body is None else body.raw)

    connection = _idle_connection(request)
    database = connection.engine.url.database or ""
    name = lock_name(
        database,
        scope.tenant_id,
        scope.credential_id,
        scope.operation,
        scope.idempotency_key,
    )
    try:
        _acquire(request, connection, name)
        if catalog_lock:
            _acquire_catalog(request, connection, catalog_lock_name(database, scope.tenant_id))
        if balance_rows is not None:
            _prepare_balances(request, connection, scope.tenant_id, balance_rows)
        if read_committed:
            _read_committed(request, connection)
        response, events = _execute(request, connection, scope, digest, command)
    finally:
        _release_locks(request, connection, reset_isolation=read_committed)
    # 用同一个连接写；它已被作废时 record_security_event 从池里另取一个（§7.2）。
    engine = getattr(request.app.state, "engine", None)
    for event in events:
        record_security_event(event, engine=engine, connection=current_connection(request))
    return response


def _idle_connection(request: Request) -> Connection:
    """处理这个请求的连接，先结束它上面的事务（取锁时不能有打开的事务）。"""
    connection = request_connection(request)
    try:
        connection.rollback()
    except SQLAlchemyError:
        _log.warning("rollback failed (request_id=%s)", request.state.request_id, exc_info=True)
        discard_connection(request)
        raise unavailable() from None
    return connection


def _acquire(request: Request, connection: Connection, name: str) -> None:
    """GET_LOCK(锁名, 0)：1 取到；0 别的请求持有，409；NULL 或出错 503。都不写任何东西。"""
    try:
        acquired = connection.execute(text("SELECT GET_LOCK(:name, 0)"), {"name": name}).scalar()
        # 结束这条语句隐式开的事务：之后的事务在取锁之后才建立读快照（§7.1 执行顺序第 3 步）。
        connection.rollback()
    except SQLAlchemyError:
        _log.warning("GET_LOCK failed (request_id=%s)", request.state.request_id, exc_info=True)
        raise unavailable() from None
    if acquired == 1:
        return
    if acquired == 0:
        raise ApiError(
            409,
            REQUEST_IN_PROGRESS,
            "a request with this idempotency key is in progress",
            retryable=True,
            headers={"Retry-After": "1"},
        )
    raise unavailable()


def _acquire_catalog(request: Request, connection: Connection, name: str) -> None:
    """GET_LOCK(目录锁名, 等待秒数)：1 取到；0（超时）、NULL 或出错 503。都不写任何东西。

    调用时连接上没有打开的事务：_acquire 已结束它的语句隐式开的事务。
    """
    try:
        acquired = connection.execute(
            text("SELECT GET_LOCK(:name, :timeout)"),
            {"name": name, "timeout": CATALOG_LOCK_WAIT_SECONDS},
        ).scalar()
        # 同 _acquire：之后的事务在取到目录锁之后才建立读快照（§2.7）。
        connection.rollback()
    except SQLAlchemyError:
        _log.warning(
            "GET_LOCK on the catalog failed (request_id=%s)",
            request.state.request_id,
            exc_info=True,
        )
        raise unavailable() from None
    if acquired != 1:
        raise unavailable()


def _prepare_balances(
    request: Request, connection: Connection, tenant_id: str, balance_rows: BalanceRows
) -> None:
    """余额行预建（§4.1）：在业务事务之前用单独的短事务按 (pool_id, sku_id) 升序逐行建好。

    外键失败（1452）的行跳过；别的错误什么也不保存，500。调用时连接上没有打开的事务。
    """
    try:
        rows = sorted({(pool_id, sku_id) for pool_id, sku_id in balance_rows(connection)})
        # 结束函数读库时隐式开的事务：预建在自己的短事务里提交。
        connection.rollback()
        with connection.begin():
            for pool_id, sku_id in rows:
                try:
                    connection.execute(_balance_upsert(tenant_id, pool_id, sku_id))
                except IntegrityError as exc:
                    # 外键失败只回滚这一条语句，事务照常继续。
                    if getattr(exc.orig, "args", ())[:1] != (_NO_REFERENCED_ROW,):
                        raise
    except Exception:
        request_id = request.state.request_id
        _log.exception("pre-creating balance rows failed (request_id=%s)", request_id)
        try:
            connection.rollback()
        except SQLAlchemyError:
            _log.warning("rolling back failed", exc_info=True)
        raise internal_error() from None


def _balance_upsert(tenant_id: str, pool_id: str, sku_id: str) -> Executable:
    """只给四列，其余取数据库默认值；已有的行只把 balance_id 设为自身，即不改。"""
    table = StockBalance.__table__
    statement = mysql_insert(table).values(
        balance_id=new_id("bal"), tenant_id=tenant_id, pool_id=pool_id, sku_id=sku_id
    )
    return statement.on_duplicate_key_update(balance_id=table.c.balance_id)


def _read_committed(request: Request, connection: Connection) -> None:
    """之后开的事务用 READ COMMITTED；由 _release_locks 设回默认。出错 503，什么也不写。"""
    try:
        connection.execute(text("SET SESSION transaction_isolation = 'READ-COMMITTED'"))
        connection.rollback()
    except SQLAlchemyError:
        _log.warning(
            "setting READ COMMITTED failed (request_id=%s)",
            request.state.request_id,
            exc_info=True,
        )
        raise unavailable() from None


def _release_locks(request: Request, connection: Connection, *, reset_isolation: bool) -> None:
    """无论结果如何都在同一连接上释放（同一会话取锁可重入，只释放一次放不干净）。

    reset_isolation 为真时先把会话的隔离级别设回服务器默认。
    释放或设回出错、连接已坏时把连接从连接池作废：连接断开时 MySQL 释放它持有的锁与会话设置。
    """
    if connection.invalidated:
        # 已坏的连接下次使用会自动另取一个，在那上面释放没有意义。
        invalidate_connection(request)
        return
    try:
        connection.rollback()  # 不会提交任何东西；正常流程走到这里时事务都已结束
        if reset_isolation:
            connection.execute(text("SET SESSION transaction_isolation = DEFAULT"))
        connection.execute(text("SELECT RELEASE_ALL_LOCKS()"))
        connection.rollback()
    except SQLAlchemyError:
        _log.warning(
            "releasing named locks failed, discarding the connection (request_id=%s)",
            request.state.request_id,
            exc_info=True,
        )
        invalidate_connection(request)


def _rollback_quietly(transaction: Transaction) -> None:
    if not transaction.is_active:
        return
    try:
        transaction.rollback()
    except SQLAlchemyError:
        _log.warning("rolling back failed", exc_info=True)


def _execute(
    request: Request,
    connection: Connection,
    scope: _Scope,
    digest: str,
    command: Callable[[Execution], CommandResult],
) -> tuple[Response, list[SecurityEventRecord]]:
    """取到锁之后：在同一连接上开事务，按作用域查记录，重放、冲突或执行命令并保存结果。

    返回响应与要在释放锁之后写的安全事件。
    """
    request_id = request.state.request_id
    execution = Execution(request, connection, scope.operation, digest)
    transaction = connection.begin()
    try:
        stored = _find(connection, scope)
        decision = decide(None if stored is None else stored.request_hash, digest)
        if decision is not Decision.EXECUTE:
            transaction.rollback()
            return _answer(request, scope, stored, decision)

        try:
            response, final = _run_command(execution, command)
        except BusinessRefTaken as taken:
            # 并发撞上业务引用（§7.1 第 2 条最后一段）：撤销命令，
            # 丢弃它登记的事件与被拒仍保留的写入，在新事务里按已生效的那一条重新判定；
            # 再撞上（或别的异常）就是 500。
            transaction.rollback()
            execution.events = []
            execution.kept_writes = []
            transaction = connection.begin()
            response, final = _run_command(execution, taken.rejudge)
        if not final:
            transaction.rollback()
            return response, execution.events
        try:
            _save(connection, scope, digest, response, request_id)
        except IntegrityError as exc:
            if getattr(exc.orig, "args", ())[:1] != (_DUPLICATE_ENTRY,):
                raise
            # 锁意外失效等（§7.1 执行顺序第 4 步）：回滚命令，丢弃它登记的事件，
            # 按已有记录重放或冲突。
            transaction.rollback()
            execution.events = []
            return _answer_existing(request, connection, scope, digest)
        transaction.commit()
        return response, execution.events
    except Exception:
        _log.exception("idempotent command failed (request_id=%s)", request_id)
        _rollback_quietly(transaction)
        return error_response(request_id, internal_error()), execution.events


def _run_command(
    execution: Execution, command: Callable[[Execution], CommandResult]
) -> tuple[Response, bool]:
    """在保存点里执行命令；返回响应与它是否作为确定结果保存。"""
    request = execution.request
    savepoint = execution.connection.begin_nested()
    try:
        result = command(execution)
    except ApiError as error:
        savepoint.rollback()
        response = error_response(request.state.request_id, error)
        if not is_final_result(error.status, error.code):
            return response, False
        for write in execution.kept_writes:
            write(execution.connection)
        return response, True
    savepoint.commit()
    if not isinstance(result, CommandResult) or not 200 <= result.status <= 299:
        raise TypeError(f"a command must return a 2xx CommandResult, got {result!r}")
    return ok(request, result.data, result.status), True


def _find(connection: Connection, scope: _Scope):
    statement = select(
        IdempotencyRecord.request_hash,
        IdempotencyRecord.response_status,
        IdempotencyRecord.response_body,
    ).where(
        IdempotencyRecord.tenant_id == scope.tenant_id,
        IdempotencyRecord.credential_id == scope.credential_id,
        IdempotencyRecord.operation == scope.operation,
        IdempotencyRecord.idempotency_key == scope.idempotency_key,
    )
    return connection.execute(statement).first()


def _save(
    connection: Connection, scope: _Scope, digest: str, response: Response, request_id: str
) -> None:
    connection.execute(
        insert(IdempotencyRecord).values(
            idempotency_record_id=new_id("idem"),
            tenant_id=scope.tenant_id,
            credential_id=scope.credential_id,
            operation=scope.operation,
            idempotency_key=scope.idempotency_key,
            request_hash=digest,
            response_status=response.status_code,
            response_body=json.loads(response.body),
            request_id=request_id,
        )
    )


def _answer_existing(
    request: Request, connection: Connection, scope: _Scope, digest: str
) -> tuple[Response, list[SecurityEventRecord]]:
    """另开事务读出已有记录，按重放或冲突返回。"""
    with connection.begin():
        stored = _find(connection, scope)
    if stored is None:
        raise RuntimeError("the conflicting idempotency record is not visible")
    return _answer(request, scope, stored, decide(stored.request_hash, digest))


def _answer(
    request: Request, scope: _Scope, stored, decision: Decision
) -> tuple[Response, list[SecurityEventRecord]]:
    if decision is Decision.REPLAY:
        headers = {REPLAYED: "true"}
        return JSONResponse(stored.response_body, stored.response_status, headers), []
    error = ApiError(409, IDEMPOTENCY_CONFLICT, "idempotency key was used with different content")
    event = _event(request, IDEMPOTENCY_CONFLICT_EVENT, {"operation": scope.operation})
    return error_response(request.state.request_id, error), [event]
