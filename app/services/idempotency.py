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
"""

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import Connection, Transaction, insert, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

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
from app.models.inventory import IdempotencyRecord
from app.services.security_events import SecurityEventRecord, record_security_event

REPLAYED = "Idempotency-Replayed"
# 安全事件类型（契约 §7.2）。
IDEMPOTENCY_CONFLICT_EVENT = "idempotency_conflict"

_OPERATION = re.compile(r"[a-z][a-z0-9_-]*(\.[a-z][a-z0-9_-]*)+")
_OPERATION_MAX_LENGTH = 64
_DUPLICATE_ENTRY = 1062  # MySQL ER_DUP_ENTRY

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

    def __init__(self, request: Request, connection: Connection, operation: str):
        self.request = request
        self.connection = connection
        self.operation = operation
        self.events: list[SecurityEventRecord] = []
        self.kept_writes: list[Callable[[Connection], None]] = []

    @property
    def access(self) -> Access:
        return get_access(self.request)

    def record_security_event(self, event_type: str, details: dict[str, Any]) -> None:
        """暂存一条安全事件，事务结束、命名锁释放之后写。"""
        self.events.append(_event(self.request, event_type, details))

    def keep_on_rejection(self, write: Callable[[Connection], None]) -> None:
        """命令以业务 4xx 拒绝时仍要保留的写入：撤销命令的写入之后，在同一事务里执行。"""
        self.kept_writes.append(write)


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
) -> Response:
    """第 10 步：取命名锁，重放、冲突或执行命令，释放锁，再写安全事件。"""
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
    name = lock_name(
        connection.engine.url.database or "",
        scope.tenant_id,
        scope.credential_id,
        scope.operation,
        scope.idempotency_key,
    )
    try:
        _acquire(request, connection, name)
        response, events = _execute(request, connection, scope, digest, command)
    finally:
        _release_locks(request, connection)
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


def _release_locks(request: Request, connection: Connection) -> None:
    """无论结果如何都在同一连接上释放（同一会话取锁可重入，只释放一次放不干净）。

    释放出错或连接已坏时把连接从连接池作废：连接断开时 MySQL 释放它持有的锁。
    """
    if connection.invalidated:
        # 已坏的连接下次使用会自动另取一个，在那上面释放没有意义。
        invalidate_connection(request)
        return
    try:
        connection.rollback()  # 不会提交任何东西；正常流程走到这里时事务都已结束
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
    execution = Execution(request, connection, scope.operation)
    transaction = connection.begin()
    try:
        stored = _find(connection, scope)
        decision = decide(None if stored is None else stored.request_hash, digest)
        if decision is not Decision.EXECUTE:
            transaction.rollback()
            return _answer(request, scope, stored, decision)

        response, final = _run_command(execution, command)
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
