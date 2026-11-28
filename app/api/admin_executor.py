"""后台接口共用的检查与后台执行器（契约 §8.6 第 2 条、§5.10“命令体共用”、§7.1，Q-84、Q-86）。

只有公共部分，不挂路由；后台接口只在后台开关为真时由 create_app 挂上（§6.1）。

写接口的检查顺序（§8.6 第 2 条，只报最先遇到的一种）：
  ①② 在 RequestMiddleware 里：路由 404/405、请求体超过 128 KiB 413；
  ③④ 会话与 CSRF：require_admin_csrf（app/api/admin_session.py），
      拒绝用 {"detail"} 的形状；
  ⑤ Idempotency-Key 缺少或格式不对 400 INVALID_REQUEST（/v1 的形状）；
  ③–⑤ 合在 require_admin_write 里；
  ⑥ 请求体：json_body（app/api/requests.py），同样的 422 与批量上限 413；
  ⑦ 角色：assert_role(接受的角色)，得出断言的角色；
  ⑧ 本租户：各接口给出池或对象；池用 backoffice_entry，
      别的对象属于别的租户时用 admin_not_found；
  ⑨ 后台执行器：admin_context 组命令上下文，run_admin 调执行器核心。
读接口只做 ③ 与 ⑧：挂 require_admin，再由接口按本租户查。

挂法（参数按 ③–⑤、⑥、⑦ 的顺序写，FastAPI 按参数顺序解析依赖；⑧ 在函数体里）：

    receipt_body = json_body(Receipt)

    @router.post("/api/v1/admin/receipts")
    def create_receipt(
        request: Request,
        write: Annotated[AdminWrite, Depends(require_admin_write)],
        body: Annotated[ParsedBody[Receipt], Depends(receipt_body)],
        role: Annotated[str, Depends(assert_role({STOCK_OPERATOR, STOCK_ADMIN}))],
    ) -> Response:
        pool_id = body.model.pool_id
        channel_id = backoffice_entry(request, write.principal, pool_id)
        context = admin_context(
            request,
            write.principal,
            role=role,
            idempotency_key=write.idempotency_key,
            channel_id=channel_id,
            pool_id=pool_id,
        )
        return run_admin(request, context, RECEIPTS_CREATE, command, body=body)

目录、平台商品类命令没有池：channel_id 与 pool_id 都给 None，不调 backoffice_entry。
后台对平台订单的人工操作不走这里（§5.10“后台的人工操作”，不写幂等记录）。

安全事件（§7.2）的身份是管理员：租户与操作主体取会话，actor_ref 是账号 ID（adm_…）。
写事件前先结束本请求连接上的读事务；查库出错 503 UNAVAILABLE（还没进入幂等，确定未生效）。
"""

import logging
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Request
from fastapi.responses import Response
from sqlalchemy import Connection, select
from sqlalchemy.exc import SQLAlchemyError

from app.api.admin_session import AdminPrincipal, require_admin, require_admin_csrf
from app.api.auth import FORBIDDEN, POOL_NOT_FOUND
from app.api.errors import INVALID_REQUEST, ApiError, invalid_request
from app.api.fulfilment import DISPATCHES_CREATE, REFUND_RESTOCKS_CREATE, RETURN_RECEIPTS_CREATE
from app.api.requests import (
    ParsedBody,
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

# V1 每个管理员都持有这三个角色（§8.6 第 2 条，Q-86）；以后按账号的角色表取。
ADMIN_ROLES = frozenset({CATALOG_ADMIN, STOCK_ADMIN, STOCK_OPERATOR})
# §5 的订单命令：入口是 backoffice 时不能经后台执行器执行（§5.10“命令体共用”）。
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
class AdminWrite:
    """③–⑤ 的结果：已登录、CSRF 相符的管理员与本次的提交键。"""

    principal: AdminPrincipal
    idempotency_key: str


# --- 安全事件 ---


def _end_transaction(request: Request) -> None:
    """结束本请求连接上的读事务；出错时丢掉这个连接，事件从连接池另取一个写。"""
    connection = current_connection(request)
    if connection is None or connection.closed or connection.invalidated:
        return
    try:
        connection.rollback()
    except SQLAlchemyError:
        _log.warning("rollback failed (request_id=%s)", request.state.request_id, exc_info=True)
        discard_connection(request)


def _write_event(request: Request, event: SecurityEventRecord) -> None:
    _end_transaction(request)
    engine = getattr(request.app.state, "engine", None)
    record_security_event(event, engine=engine, connection=current_connection(request))


def record_admin_event(
    request: Request, principal: AdminPrincipal, event_type: str, details: dict[str, Any]
) -> None:
    """以管理员的身份写一条安全事件（§7.2）；写入失败只记进程日志。"""
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


def _not_found(object_type: str, code: str) -> ApiError:
    """不存在与属于别的租户用同一个错误码与 message（§8.3），同 app/api/auth.py。"""
    return ApiError(404, code, f"{object_type} not found")


def admin_not_found(
    request: Request, principal: AdminPrincipal, *, object_type: str, object_id: str, code: str
) -> ApiError:
    """⑧ 找到的池、对象或入口属于别的租户：记一条 cross_tenant，对外与不存在完全相同。"""
    details = {"object_type": object_type, "id": object_id}
    record_admin_event(request, principal, security_events.CROSS_TENANT, details)
    return _not_found(object_type, code)


def _forbidden() -> ApiError:
    return ApiError(403, FORBIDDEN, "forbidden")


# --- ③–⑤ ---


def require_admin_write(
    request: Request, principal: Annotated[AdminPrincipal, Depends(require_admin_csrf)]
) -> AdminWrite:
    """写接口的 ③–⑤：会话、CSRF，再判 Idempotency-Key 的格式。"""
    try:
        idempotency_key = parse_idempotency_key(request.headers.get(IDEMPOTENCY_KEY))
    except InvalidHeader as exc:
        raise ApiError(400, INVALID_REQUEST, f"invalid {exc.header} header") from None
    return AdminWrite(principal, idempotency_key)


# --- ⑦ 角色 ---


def asserted_role(accepted: Collection[str]) -> str:
    """Q-86 写死的规则：只接受一个的断言它；接受操作员与库存管理员的断言 stock_admin；
    不要求角色的（订单命令）断言 stock_admin。别的组合没有规则，ValueError。
    """
    roles = frozenset(accepted)
    if roles - ACTOR_ROLES:
        raise ValueError(f"unknown roles: {sorted(roles - ACTOR_ROLES)}")
    if len(roles) == 1:
        return next(iter(roles))
    if not roles or roles == {STOCK_OPERATOR, STOCK_ADMIN}:
        return STOCK_ADMIN
    raise ValueError(f"no rule for the roles {sorted(roles)}")


def assert_role(accepted: Collection[str]) -> Callable[..., str]:
    """⑦ 的依赖：返回断言的角色；管理员不持有它时 403 FORBIDDEN，记一条 forbidden。"""
    role = asserted_role(accepted)

    def dependency(
        request: Request, principal: Annotated[AdminPrincipal, Depends(require_admin)]
    ) -> str:
        # 每次按模块里的 ADMIN_ROLES 判：以后换成账号的角色表。
        if role not in ADMIN_ROLES:
            details = {"category": "role", "role": role}
            record_admin_event(request, principal, security_events.FORBIDDEN, details)
            raise _forbidden()
        return role

    return dependency


# --- ⑧ 池与后台入口 ---


def _read[T](request: Request, query: Callable[[Connection], T]) -> T:
    """用本请求的连接普通读，读完回滚结束事务；出错 503。"""
    connection = request_connection(request)
    try:
        result = query(connection)
        connection.rollback()
    except SQLAlchemyError:
        _log.warning(
            "database error reading for an admin request (request_id=%s)",
            request.state.request_id,
            exc_info=True,
        )
        discard_connection(request)
        raise unavailable() from None
    return result


# 池所属的租户与池的 backoffice 入口 ID（没有时为 None）。
PoolEntry = tuple[str | None, str | None]


def _pool_entry(tenant_id: str, pool_id: str) -> Callable[[Connection], PoolEntry]:
    """池所属的租户（不按租户过滤），以及池属于本租户时它 active 的 backoffice 入口。"""

    def query(conn: Connection) -> PoolEntry:
        owner_statement = select(StockPool.tenant_id).where(StockPool.pool_id == pool_id)
        owner = conn.execute(owner_statement).scalar_one_or_none()
        if owner != tenant_id:
            return owner, None
        entry_statement = select(SalesChannel.channel_id).where(
            SalesChannel.tenant_id == tenant_id,
            SalesChannel.pool_id == pool_id,
            SalesChannel.kind == BACKOFFICE,
            SalesChannel.status == ACTIVE,
        )
        return owner, conn.execute(entry_statement).scalars().first()

    return query


def backoffice_entry(request: Request, principal: AdminPrincipal, pool_id: str) -> str:
    """⑧：所选池的 backoffice 入口 ID（§8.6 第 2 条“后台发起的库存管理命令”）。

    池不存在 404 POOL_NOT_FOUND；属于别的租户另记 cross_tenant，对外相同；
    池没有 active 的 backoffice 入口 422 INVALID_REQUEST（只有组合 2 的池有后台入口）。
    """
    owner, entry = _read(request, _pool_entry(principal.tenant_id, pool_id))
    if owner is None:
        raise _not_found("pool", POOL_NOT_FOUND)
    if owner != principal.tenant_id:
        raise admin_not_found(
            request, principal, object_type="pool", object_id=pool_id, code=POOL_NOT_FOUND
        )
    if entry is None:
        raise invalid_request("pool has no active backoffice channel", {"pool_id": pool_id})
    return entry


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
    """后台执行器的命令上下文（§5.10“命令体共用”）。

    操作主体是管理员的主体，操作者是 (账号 ID, 断言的角色)；入口与池由接口给出
    （目录、平台商品类命令两者都为 None）。后台不发起订单命令，预留有效期为 None。
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
    operation: str,
    command: Callable[[Execution], CommandResult],
    *,
    body: ParsedBody | None,
    **options: Any,
) -> Response:
    """⑨：调执行器核心 run_with_context；options 是它的执行器选项（catalog_lock 等）。

    本执行器的入口只有所选池的 backoffice 入口（或没有入口）：operation 是 §5 的订单命令时
    403 FORBIDDEN、记一条 forbidden，在取锁之前判出、不保存（§5.10）。
    幂等作用域、重放与冲突、安全事件都照执行器核心。
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
