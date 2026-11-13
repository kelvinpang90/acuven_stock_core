"""鉴权与授权的依赖（契约 §7.2、§8.1 检查顺序第 4–6、8、9 步，§10 第 3、4 条）。

规则判断用 app/domain/service_auth.py 的纯函数，这里只查库、挂依赖与写安全事件。
/healthz、/readyz 不挂这些依赖；其余接口都挂 authorize。

挂法（第 4–6 步在请求体之前，第 8、9 步在请求体之后）：

    receipt_body = json_body(Receipt)

    def receipt_pool(body: Annotated[ParsedBody[Receipt], Depends(receipt_body)]) -> str:
        return body.model.pool_id

    @app.post("/v1/receipts", dependencies=[Depends(authorize(STOCK_WRITE, write=True))])
    def create_receipt(
        request: Request,
        # 第 7、8、9 步
        body: Annotated[ParsedBody[Receipt], Depends(receipt_body)],
        actor: Annotated[Actor, Depends(require_role(STOCK_OPERATOR, STOCK_ADMIN))],
        pool_id: Annotated[str, Depends(require_pool(receipt_pool))],
    ): ...

FastAPI 先按列表顺序解析装饰器的依赖，再按参数顺序解析参数里的依赖，
所以参数按请求体、角色、池的顺序写；没有请求体的接口把 no_body 写在装饰器里 authorize 之后。
同一个请求体依赖要用同一个 json_body(...) 对象，FastAPI 才只解析一次。

鉴权与授权查库用处理这个请求的连接，读完就结束事务；
查库出错 503 UNAVAILABLE（还没进入幂等，确定未生效）。
401、403 与跨租户 404 在返回响应前各写一条安全事件（§7.2）。
"""

import logging
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Annotated, Any, NamedTuple

from fastapi import Depends, Request
from sqlalchemy import Connection, DateTime, and_, literal_column, select
from sqlalchemy.exc import SQLAlchemyError

from app.api.errors import INVALID_REQUEST, ApiError
from app.api.requests import (
    current_connection,
    discard_connection,
    request_connection,
    unavailable,
)
from app.domain.requests import (
    ACTOR_REF,
    ACTOR_ROLE,
    ACTOR_ROLES,
    IDEMPOTENCY_KEY,
    Actor,
    InvalidHeader,
    parse_actor,
    parse_idempotency_key,
)
from app.domain.service_auth import (
    ACTIVE,
    SCOPES,
    PoolDecision,
    authorize_pool,
    authorize_sales_command,
    is_token_usable,
    parse_authorization,
    scopes_cover,
    token_digest,
)
from app.models.catalog import Tenant
from app.models.inventory import SalesChannel, StockPool
from app.models.service_auth import Credential, CredentialToken
from app.services import security_events
from app.services.security_events import SecurityEventRecord, record_security_event

UNAUTHORIZED = "UNAUTHORIZED"
FORBIDDEN = "FORBIDDEN"
POOL_NOT_FOUND = "POOL_NOT_FOUND"

# 401 的内部原因，只记在安全事件里、不进响应（§7.2）。
# 同时有几种令牌原因时按 TOKEN_NOT_FOUND 到 TENANT_INACTIVE 的顺序记第一种。
MISSING_HEADER = "missing_header"
MALFORMED_HEADER = "malformed_header"
TOKEN_NOT_FOUND = "token_not_found"
TOKEN_REVOKED = "token_revoked"
TOKEN_EXPIRED = "token_expired"
CREDENTIAL_REVOKED = "credential_revoked"
TENANT_INACTIVE = "tenant_inactive"

_log = logging.getLogger("app.api")


@dataclass(frozen=True)
class Principal:
    """鉴权的结果。销售入口已停用时 channel_id 与 pool_id 都为 None（视同没有入口）。"""

    tenant_id: str
    credential_id: str
    channel_id: str | None
    pool_id: str | None  # 销售入口绑定的池，即获准操作的池
    scopes: frozenset[str]


@dataclass(frozen=True)
class Access:
    """第 4–6 步的结果：鉴权、scope 与其他请求头都已通过。"""

    principal: Principal
    actor: Actor | None  # 两个操作者头都不带时为 None
    idempotency_key: str | None  # 只有写接口有


class ObjectPool(NamedTuple):
    """按 ID 指向池内对象的命令：对象所属的租户与池。"""

    tenant_id: str
    pool_id: str


# --- 错误与安全事件 ---


def _bad_header(header: str) -> ApiError:
    return ApiError(400, INVALID_REQUEST, f"invalid {header} header")


def _event_actor_ref(request: Request) -> str | None:
    """两个操作者头都带且都合格时才记操作者（§7.2）；第 6 步之前的事件也按这个判断。"""
    try:
        actor = parse_actor(request.headers.get(ACTOR_REF), request.headers.get(ACTOR_ROLE))
    except InvalidHeader:
        return None
    return None if actor is None else actor.ref


def _record(
    request: Request, event_type: str, details: dict[str, Any], principal: Principal | None
) -> None:
    """在返回响应前写；用本请求的连接（其上没有打开的事务），还没取就从连接池取一个。"""
    event = SecurityEventRecord(
        event_type=event_type,
        request_id=request.state.request_id,
        tenant_id=None if principal is None else principal.tenant_id,
        credential_id=None if principal is None else principal.credential_id,
        actor_ref=_event_actor_ref(request),
        http_method=request.method,
        path=request.scope["path"],  # 不含查询串
        user_agent=request.headers.get("user-agent"),
        details=details,
    )
    engine = getattr(request.app.state, "engine", None)
    record_security_event(event, engine=engine, connection=current_connection(request))


def _unauthorized(request: Request, reason: str) -> ApiError:
    """所有原因对外同一个错误体。"""
    _record(request, security_events.INVALID_CREDENTIAL, {"reason": reason}, None)
    return ApiError(401, UNAUTHORIZED, "invalid credentials")


def _forbidden(request: Request, principal: Principal, details: dict[str, Any]) -> ApiError:
    _record(request, security_events.FORBIDDEN, details, principal)
    return ApiError(403, FORBIDDEN, "forbidden")


def _not_found(object_type: str, code: str) -> ApiError:
    """不存在与属于别的租户用同一个错误码与 message（§8.3）。"""
    return ApiError(404, code, f"{object_type} not found")


def cross_tenant_not_found(
    request: Request, *, object_type: str, object_id: str, code: str
) -> ApiError:
    """按 ID 找到的对象属于别的租户：记一条 cross_tenant，对外与不存在完全相同。

    调用前先结束本请求连接上的业务事务（§7.2）。
    """
    details = {"object_type": object_type, "id": object_id}
    _record(request, security_events.CROSS_TENANT, details, _principal(request))
    return _not_found(object_type, code)


def _read[T](request: Request, query: Callable[[Connection], T]) -> T:
    """鉴权与授权的查库：用本请求的连接，读完回滚结束事务；出错 503。"""
    connection = request_connection(request)
    try:
        result = query(connection)
        connection.rollback()
    except SQLAlchemyError:
        _log.warning(
            "database error during authorization (request_id=%s)",
            request.state.request_id,
            exc_info=True,
        )
        discard_connection(request)
        raise unavailable() from None
    return result


# --- 第 4 步：鉴权 ---


def _principal_query(digest: str):
    """按摘要查令牌，同一条查询里联查凭据、租户、销售入口与入口绑定的池；带上数据库时间。"""
    return (
        select(
            CredentialToken.revoked_at.label("token_revoked_at"),
            CredentialToken.expires_at,
            Credential.tenant_id,
            Credential.credential_id,
            Credential.scopes,
            Credential.status.label("credential_status"),
            Tenant.status.label("tenant_status"),
            SalesChannel.channel_id,
            SalesChannel.status.label("channel_status"),
            StockPool.pool_id,
            literal_column("UTC_TIMESTAMP(6)", DateTime()).label("db_now"),
        )
        .select_from(CredentialToken)
        .join(
            Credential,
            and_(
                Credential.tenant_id == CredentialToken.tenant_id,
                Credential.credential_id == CredentialToken.credential_id,
            ),
        )
        .join(Tenant, Tenant.tenant_id == Credential.tenant_id)
        .outerjoin(
            SalesChannel,
            and_(
                SalesChannel.tenant_id == Credential.tenant_id,
                SalesChannel.channel_id == Credential.channel_id,
            ),
        )
        .outerjoin(
            StockPool,
            and_(
                StockPool.tenant_id == SalesChannel.tenant_id,
                StockPool.pool_id == SalesChannel.pool_id,
            ),
        )
        .where(CredentialToken.token_digest == digest)
    )


def _invalid_reason(row) -> str | None:
    """可用时为 None；不可用时按固定顺序给出第一种原因。"""
    if row is None:
        return TOKEN_NOT_FOUND
    now = row["db_now"]
    usable = is_token_usable(
        token_revoked_at=row["token_revoked_at"],
        expires_at=row["expires_at"],
        credential_status=row["credential_status"],
        tenant_status=row["tenant_status"],
        now=now,
    )
    if usable:
        return None
    if row["token_revoked_at"] is not None:
        return TOKEN_REVOKED
    if row["expires_at"] is not None and now >= row["expires_at"]:
        return TOKEN_EXPIRED
    if row["credential_status"] != ACTIVE:
        return CREDENTIAL_REVOKED
    return TENANT_INACTIVE


def _authenticate(request: Request) -> Principal:
    """每个请求都查库、不缓存：吊销立即生效。"""
    header = request.headers.get("authorization")
    token = parse_authorization(header)
    if token is None:
        raise _unauthorized(request, MISSING_HEADER if header is None else MALFORMED_HEADER)
    digest = token_digest(token)
    row = _read(request, lambda conn: conn.execute(_principal_query(digest)).mappings().first())
    reason = _invalid_reason(row)
    if reason is not None:
        raise _unauthorized(request, reason)
    channel_active = row["channel_status"] == ACTIVE
    return Principal(
        tenant_id=row["tenant_id"],
        credential_id=row["credential_id"],
        channel_id=row["channel_id"] if channel_active else None,
        pool_id=row["pool_id"] if channel_active else None,
        scopes=frozenset(row["scopes"]),
    )


# --- 第 4–6 步 ---


def authorize(*scopes: str, write: bool = False) -> Callable[[Request], Access]:
    """第 4–6 步的依赖：鉴权、接口要求的 scope（几个都要）、其他请求头。

    write 为真时 Idempotency-Key 必带；操作者头带了就校验格式（要么都带、要么都不带）。
    结果放进 request.state，第 8、9 步与命令用 get_access 取。
    """
    unknown = set(scopes) - SCOPES
    if not scopes or unknown:
        raise ValueError(f"an endpoint needs known scopes, got {sorted(scopes)}")
    required = frozenset(scopes)

    def dependency(request: Request) -> Access:
        principal = _authenticate(request)
        if not scopes_cover(principal.scopes, required):
            missing = sorted(required - principal.scopes)
            raise _forbidden(request, principal, {"category": "scope", "missing_scopes": missing})
        idempotency_key = None
        try:
            if write:
                idempotency_key = parse_idempotency_key(request.headers.get(IDEMPOTENCY_KEY))
            actor = parse_actor(request.headers.get(ACTOR_REF), request.headers.get(ACTOR_ROLE))
        except InvalidHeader as exc:
            raise _bad_header(exc.header) from None
        access = Access(principal, actor, idempotency_key)
        request.state.access = access
        return access

    return dependency


def get_access(request: Request) -> Access:
    access = getattr(request.state, "access", None)
    if access is None:
        raise RuntimeError("the endpoint has no authorize(...) dependency")
    return access


def _principal(request: Request) -> Principal:
    return get_access(request).principal


# --- 第 8 步：角色 ---


def _accepted_roles(roles: Collection[str]) -> frozenset[str]:
    accepted = frozenset(roles)
    if accepted - ACTOR_ROLES:
        raise ValueError(f"unknown roles: {sorted(accepted - ACTOR_ROLES)}")
    return accepted


def _check_role(request: Request, accepted: frozenset[str]) -> Actor:
    access = get_access(request)
    if access.actor is None:
        raise ApiError(400, INVALID_REQUEST, f"{ACTOR_REF} and {ACTOR_ROLE} headers are required")
    if access.actor.role not in accepted:
        details = {"category": "role", "role": access.actor.role}
        raise _forbidden(request, access.principal, details)
    return access.actor


def require_role(*roles: str) -> Callable[[Request], Actor]:
    """接口接受的角色（§10 第 3 条角色表）。要求角色却没带操作者头 400，角色不被接受 403。"""
    if not roles:
        raise ValueError("require_role needs at least one role")
    accepted = _accepted_roles(roles)

    def dependency(request: Request) -> Actor:
        return _check_role(request, accepted)

    return dependency


def require_role_by_body[B](
    body_dependency: Callable[..., Any], roles_for: Callable[[B], Collection[str]]
) -> Callable[..., Actor | None]:
    """按请求体里的动作定角色（如报损）。

    roles_for 收第 7 步的结果，返回接受的角色；空集合表示不要求角色。
    body_dependency 要与接口参数里的请求体依赖是同一个对象。
    """

    def dependency(
        request: Request, body: Annotated[Any, Depends(body_dependency)]
    ) -> Actor | None:
        accepted = _accepted_roles(roles_for(body))
        if not accepted:
            return get_access(request).actor
        return _check_role(request, accepted)

    return dependency


# --- 第 9 步：池授权 ---


def require_sales_pool(request: Request) -> str:
    """销售类命令不带 pool_id，池由销售入口决定；凭据没有（有效的）销售入口时 403。"""
    principal = _principal(request)
    if authorize_sales_command(principal.pool_id) is PoolDecision.FORBIDDEN:
        raise _forbidden(request, principal, {"category": "pool", "pool_id": None})
    return principal.pool_id


def _pool_owner(pool_id: str) -> Callable[[Connection], str | None]:
    def query(conn: Connection) -> str | None:
        statement = select(StockPool.tenant_id).where(StockPool.pool_id == pool_id)
        return conn.execute(statement).scalar_one_or_none()

    return query


def require_pool(pool_id_dependency: Callable[..., str]) -> Callable[..., str]:
    """正文或路径带 pool_id 的命令：pool_id_dependency 取出请求里的 pool_id。

    按 §10 第 4 条的顺序：没有销售入口 403；池不属于本租户 404 POOL_NOT_FOUND
    （属于别的租户时另记 cross_tenant）；属于本租户但不是获准的池 403。
    """

    def dependency(request: Request, pool_id: Annotated[str, Depends(pool_id_dependency)]) -> str:
        principal = _principal(request)
        owner = _read(request, _pool_owner(pool_id))
        in_tenant = owner == principal.tenant_id
        decision = authorize_pool(principal.pool_id, pool_id, pool_in_tenant=in_tenant)
        if decision is PoolDecision.NOT_FOUND:
            if owner is not None:
                raise cross_tenant_not_found(
                    request, object_type="pool", object_id=pool_id, code=POOL_NOT_FOUND
                )
            raise _not_found("pool", POOL_NOT_FOUND)
        if decision is PoolDecision.FORBIDDEN:
            raise _forbidden(request, principal, {"category": "pool", "pool_id": pool_id})
        return pool_id

    return dependency


def require_object_pool(
    object_id_dependency: Callable[..., str],
    find_pool: Callable[[Connection, str], ObjectPool | None],
    *,
    object_type: str,
    not_found_code: str,
) -> Callable[..., str]:
    """按 ID 指向池内对象的命令（盘点单的提交、确认、取消等），按对象所属的池判定。

    find_pool 由接口提供：按对象 ID 查它所属的租户与池，不存在时返回 None（不按租户过滤）。
    顺序同 require_pool；对象不存在与属于别的租户都是 404 not_found_code，后者另记 cross_tenant。
    返回对象所属的池。
    """

    def dependency(
        request: Request, object_id: Annotated[str, Depends(object_id_dependency)]
    ) -> str:
        principal = _principal(request)
        found = _read(request, lambda conn: find_pool(conn, object_id))
        in_tenant = found is not None and found.tenant_id == principal.tenant_id
        object_pool = "" if found is None else found.pool_id
        decision = authorize_pool(principal.pool_id, object_pool, pool_in_tenant=in_tenant)
        if decision is PoolDecision.NOT_FOUND:
            if found is not None:
                raise cross_tenant_not_found(
                    request, object_type=object_type, object_id=object_id, code=not_found_code
                )
            raise _not_found(object_type, not_found_code)
        if decision is PoolDecision.FORBIDDEN:
            details = {
                "category": "pool",
                "pool_id": found.pool_id if in_tenant else None,
                "object_type": object_type,
                "object_id": object_id,
            }
            raise _forbidden(request, principal, details)
        return found.pool_id

    return dependency
