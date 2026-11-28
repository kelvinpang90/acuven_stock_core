"""后台接口的公共检查与后台执行器。

见契约 §8.6 第 2 条、§5.10“命令体共用”（Q-84、Q-86）。

写接口的检查顺序（只报最先遇到的一种）：
  ①② 在 RequestMiddleware 里：路由 404/405、请求体超过 128 KiB 413；
  ③④ 会话与 CSRF：require_admin_csrf（app/api/admin_session.py），
     拒绝时是 {"detail"} 形状；
  ⑤ Idempotency-Key 缺少或格式不对 400 INVALID_REQUEST（/v1 的错误形状）；
  ⑥ 请求体：json_body（同 §8.1 第 7 步的 422 与批量上限 413），
     没有请求体的接口用 no_body；
  ⑦ 角色：assert_role，按 Q-86 的写死规则得出断言的角色；
  ⑧ 本租户：由各接口给出池或对象——正文带 pool_id 的用 backoffice_entry，
     别的对象属于别的租户时用 admin_not_found；
  ⑨ 后台执行器：admin_context 组命令上下文，run_admin 调执行器核心。
③–⑦ 由 admin_write 组成一个依赖，挂法：

    note_write = admin_write(json_body(Note), STOCK_OPERATOR, STOCK_ADMIN)

    @router.post("/api/v1/admin/notes")
    def create_note(
        request: Request, write: Annotated[AdminWrite[Note], Depends(note_write)]
    ) -> Response:
        pool_id = write.body.model.pool_id
        channel_id = backoffice_entry(request, write.principal, pool_id)  # ⑧
        context = admin_context(
            request,
            write.principal,
            role=write.role,
            idempotency_key=write.idempotency_key,
            channel_id=channel_id,
            pool_id=pool_id,
        )
        return run_admin(request, context, operation=NOTES_CREATE, body=write.body, command=...)

目录与平台商品类命令不选池：channel_id 与 pool_id 都传 None。
读接口只做 require_admin 与本租户（③、⑧）。

安全事件的身份取管理员：tenant_id、credential_id 为管理员的租户与主体，
actor_ref 为账号 ID。
平台订单的人工操作不走这里（不写幂等记录，§5.10“后台的人工操作”）。
"""

import logging
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Request
from fastapi.responses import Response
from sqlalchemy import Connection, and_, select
from sqlalchemy.exc import SQLAlchemyError

from app.api.admin_session import AdminPrincipal, require_admin, require_admin_csrf
from app.api.auth import FORBIDDEN, POOL_NOT_FOUND
from app.api.errors import INVALID_REQUEST, ApiError, invalid_request
from app.api.fulfilment import DISPATCHES_CREATE, REFUND_RESTOCKS_CREATE, RETURN_RECEIPTS_CREATE
from app.api.requests import (
    ParsedBody,
    RequestBody,
    current_connection,
    discard_connection,
    request_connection,
    unavailable,
)
from app.api.reservations import RESERVATIONS_CREATE, RESERVATIONS_RELEASE
from app.api.sale_commits import LATE_PAYMENT_REVIEWS_CLOSE, SALE_COMMITS_CREATE
from app.domain.channels import BACKOFFICE
from app.domain.requests import (
    ACTOR_ROLES,
    CATALOG_ADMIN,
    IDEMPOTENCY_KEY,
    STOCK_ADMIN,
    STOCK_OPERATOR,
    Actor,
    InvalidHeader,
    parse_idempotency_key,
)
from app.domain.service_auth import ACTIVE
from app.models.inventory import SalesChannel, StockPool
from app.services import security_events
from app.services.command_context import CommandContext
from app.services.idempotency import CommandResult, Execution, run_with_context
from app.services.security_events import SecurityEventRecord, record_security_event

# V1 每个管理员都持有这三个角色（§8.6 第 2 条）；以后按账号的角色表给出。
ADMIN_ROLES = frozenset({CATALOG_ADMIN, STOCK_ADMIN, STOCK_OPERATOR})
# §5 的七个订单命令：后台执行器不跑它们（§5.10）。
ORDER_OPERATIONS = frozenset(
    {
        RESERVATIONS_CREATE,
        RESERVATIONS_RELEASE,
        SALE_COMMITS_CREATE,
        DISPATCHES_CREATE,
        REFUND_RESTOCKS_CREATE,
        RETURN_RECEIPTS_CREATE,
        LATE_PAYMENT_REVIEWS_CLOSE,
    }
)

_log = logging.getLogger("app.api")


@dataclass(frozen=True)
class AdminWrite[M: RequestBody]:
    """③–⑦ 的结果：管理员、提交键、请求体（no_body 时为 None）与断言的角色。"""

    principal: AdminPrincipal
    idempotency_key: str
    body: ParsedBody[M] | None
    role: str


# --- 安全事件 ---


def _end_read(request: Request) -> None:
    """记事件前结束本请求连接上的读事务；回滚出错时不再用这个连接。"""
    connection = current_connection(request)
    if connection is None or not connection.in_transaction():
        return
    try:
        connection.rollback()
    except SQLAlchemyError:
        _log.warning(
            "rollback before a security event failed (request_id=%s)",
            request.state.request_id,
            exc_info=True,
        )
        discard_connection(request)


def _write_event(request: Request, event: SecurityEventRecord) -> None:
    _end_read(request)
    engine = getattr(request.app.state, "engine", None)
    record_security_event(event, engine=engine, connection=current_connection(request))


def record_admin_event(
    request: Request, principal: AdminPrincipal, event_type: str, details: dict[str, Any]
) -> None:
    """以管理员的身份记一条安全事件；返回响应之前写（§7.2）。"""
    event = SecurityEventRecord(
        event_type=event_type,
        request_id=request.state.request_id,
        tenant_id=principal.tenant_id,
        credential_id=principal.credential_id,
        actor_ref=principal.account_id,
        http_method=request.method,
        path=request.scope["path"],  # 不含查询串
        user_agent=request.headers.get("user-agent"),
        details=details,
    )
    _write_event(request, event)


def _forbidden() -> ApiError:
    return ApiError(403, FORBIDDEN, "forbidden")


def _not_found(object_type: str, code: str) -> ApiError:
    """不存在与属于别的租户用同一个错误码与 message（§8.3）。"""
    return ApiError(404, code, f"{object_type} not found")


def admin_not_found(
    request: Request, principal: AdminPrincipal, *, object_type: str, object_id: str, code: str
) -> ApiError:
    """按 ID 找到的池、对象或入口属于别的租户：记一条 cross_tenant，对外与不存在完全相同。"""
    details = {"object_type": object_type, "id": object_id}
    record_admin_event(request, principal, security_events.CROSS_TENANT, details)
    return _not_found(object_type, code)


# --- ⑤ 提交键 ---


def admin_idempotency_key(
    request: Request, principal: Annotated[AdminPrincipal, Depends(require_admin_csrf)]
) -> str:
    """在会话与 CSRF 之后：缺少或格式不对 400 INVALID_REQUEST。"""
    try:
        return parse_idempotency_key(request.headers.get(IDEMPOTENCY_KEY))
    except InvalidHeader as exc:
        raise ApiError(400, INVALID_REQUEST, f"invalid {exc.header} header") from None


# --- ⑦ 角色 ---


def asserted_role(accepted: Collection[str]) -> str:
    """Q-86 的写死规则得出断言的角色。

    只接受一个的断言它；接受 stock_operator 与 stock_admin 的断言 stock_admin；
    不要求角色（空集合，管理员发起的订单命令）的断言 stock_admin。
    """
    roles = frozenset(accepted)
    if roles - ACTOR_ROLES:
        raise ValueError(f"unknown roles: {sorted(roles - ACTOR_ROLES)}")
    if not roles:
        return STOCK_ADMIN
    if len(roles) == 1:
        return next(iter(roles))
    if roles == {STOCK_OPERATOR, STOCK_ADMIN}:
        return STOCK_ADMIN
    raise ValueError(f"no fixed role to assert for {sorted(roles)}")


def assert_role(*accepted: str) -> Callable[..., str]:
    """⑦ 的依赖：返回断言的角色。

    断言的角色不在管理员持有的角色里时 403 FORBIDDEN，并记一条 forbidden。
    """
    role = asserted_role(accepted)

    def dependency(
        request: Request, principal: Annotated[AdminPrincipal, Depends(require_admin)]
    ) -> str:
        if role not in ADMIN_ROLES:
            details = {"category": "role", "role": role}
            record_admin_event(request, principal, security_events.FORBIDDEN, details)
            raise _forbidden()
        return role

    return dependency


# --- ③–⑦ 组合 ---


def admin_write(body_dependency: Callable[..., Any], *accepted: str) -> Callable[..., AdminWrite]:
    """写接口的依赖：会话与 CSRF → 提交键 → 请求体 → 角色。

    body_dependency 是 json_body(模型) 或 no_body；
    accepted 是命令接受的角色（§10 第 3 条），不要求角色时不写。
    FastAPI 按参数顺序解析子依赖，所以参数顺序即检查顺序。
    """
    role_dependency = assert_role(*accepted)

    def dependency(
        principal: Annotated[AdminPrincipal, Depends(require_admin_csrf)],
        idempotency_key: Annotated[str, Depends(admin_idempotency_key)],
        body: Annotated[ParsedBody | None, Depends(body_dependency)],
        role: Annotated[str, Depends(role_dependency)],
    ) -> AdminWrite:
        return AdminWrite(principal, idempotency_key, body, role)

    return dependency


# --- ⑧ 所选池的后台入口 ---


def _pool_entry_query(pool_id: str):
    """池的租户与它 active 的 backoffice 入口（没有时为空）；不按租户过滤。"""
    return (
        select(StockPool.tenant_id, SalesChannel.channel_id)
        .select_from(StockPool)
        .outerjoin(
            SalesChannel,
            and_(
                SalesChannel.tenant_id == StockPool.tenant_id,
                SalesChannel.pool_id == StockPool.pool_id,
                SalesChannel.kind == BACKOFFICE,
                SalesChannel.status == ACTIVE,
            ),
        )
        .where(StockPool.pool_id == pool_id)
    )


def _read[T](request: Request, query: Callable[[Connection], T]) -> T:
    """用本请求的连接普通读，读完回滚结束事务；出错 503。"""
    connection = request_connection(request)
    try:
        result = query(connection)
        connection.rollback()
    except SQLAlchemyError:
        _log.warning(
            "database error checking an admin request (request_id=%s)",
            request.state.request_id,
            exc_info=True,
        )
        discard_connection(request)
        raise unavailable() from None
    return result


def backoffice_entry(request: Request, principal: AdminPrincipal, pool_id: str) -> str:
    """所选池的 backoffice 入口 ID。

    池不存在 404 POOL_NOT_FOUND；属于别的租户记 cross_tenant 后同样 404；
    池没有 active 的 backoffice 入口 422 INVALID_REQUEST（只有组合 2 的池有后台入口）。
    """
    row = _read(request, lambda conn: conn.execute(_pool_entry_query(pool_id)).first())
    if row is None:
        raise _not_found("pool", POOL_NOT_FOUND)
    if row.tenant_id != principal.tenant_id:
        raise admin_not_found(
            request, principal, object_type="pool", object_id=pool_id, code=POOL_NOT_FOUND
        )
    if row.channel_id is None:
        raise invalid_request("the pool has no active backoffice entry")
    return row.channel_id


# --- ⑨ 后台执行器 ---


def admin_context(
    request: Request,
    principal: AdminPrincipal,
    *,
    role: str,
    idempotency_key: str,
    channel_id: str | None,
    pool_id: str | None,
) -> CommandContext:
    """后台执行器的命令上下文（§5.10）。

    身份与操作者取管理员，入口与池取参数，request_id 取中间件生成的，预留有效期为空。
    """
    return CommandContext(
        tenant_id=principal.tenant_id,
        channel_id=channel_id,
        pool_id=pool_id,
        credential_id=principal.credential_id,
        actor=Actor(principal.account_id, role),
        request_id=request.state.request_id,
        idempotency_key=idempotency_key,
        reservation_ttl_seconds=None,
        http_method=request.method,
        path=request.scope["path"],  # 不含查询串
        user_agent=request.headers.get("user-agent"),
    )


def run_admin(
    request: Request,
    context: CommandContext,
    *,
    operation: str,
    body: ParsedBody | None,
    command: Callable[[Execution], CommandResult],
    **options: Any,
) -> Response:
    """后台写命令的第 ⑨ 步。

    后台执行器的入口只能是 backoffice 入口（或不选池时没有入口），订单命令一律
    403 FORBIDDEN 并记 forbidden（§5.10）：在调执行器之前判出，不取锁、不保存。
    其余照执行器核心 run_with_context：幂等作用域、重放与冲突、安全事件都与 /v1 相同；
    options 是它的执行器选项（catalog_lock、balance_rows 等）。
    """
    if operation in ORDER_OPERATIONS:
        event = SecurityEventRecord(
            event_type=security_events.FORBIDDEN,
            request_id=context.request_id,
            tenant_id=context.tenant_id,
            credential_id=context.credential_id,
            actor_ref=None if context.actor is None else context.actor.ref,
            http_method=context.http_method,
            path=context.path,
            user_agent=context.user_agent,
            details={"category": "entry_kind", "operation": operation},
        )
        _write_event(request, event)
        raise _forbidden()
    return run_with_context(
        request, context, operation=operation, body=body, command=command, **options
    )
