"""后台管理员的登录、取当前会话与退出，及后台接口共用的依赖。

见 docs/ADMIN_AUTH.md 第 5 节。只在后台开关为真时由 create_app 挂上（契约 6.1）。
响应用 {"detail": …} 的形状（契约 §8.6 第 2 条），本模块给出的响应都带
Cache-Control: no-store；处理中数据库出错 503 database_unreachable，不含异常内容。

数据库连接：get_connection 返回本请求唯一的连接（request_connection），
请求结束由中间件归还；依赖与 GET、DELETE 查完库就在它上面回滚、结束读事务，
之后的后台接口照旧用同一个连接。登录接口在线程池里自己从 app.state.engine
取连接、用完归还。

查库与 scrypt 都不在事件循环线程上跑：依赖与 GET、DELETE 是普通 def
（FastAPI 放进线程池）；登录接口显式放进线程池，同时最多两个 authenticate。
"""

import ipaddress
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Annotated

import anyio
import anyio.to_thread
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from app.api.errors import ApiError
from app.api.requests import invalidate_connection, request_connection
from app.services.admin_auth import (
    INVALID_CREDENTIALS,
    LOCKED,
    SESSION_LIFETIME,
    SUCCESS,
    ActiveSession,
    LoginResult,
    authenticate,
    csrf_token_for,
    csrf_token_matches,
    resolve_session,
    revoke_session,
    utc_now,
)

SESSION_PATH = "/api/v1/admin/session"
# 5.2：__Host- 前缀要求 Secure、Path=/、不带 Domain。
SESSION_COOKIE = "__Host-core_admin_session"
SESSION_MAX_AGE = int(SESSION_LIFETIME.total_seconds())
CSRF_HEADER = "X-CSRF-Token"
REAL_IP_HEADER = "x-real-ip"
UNKNOWN_SOURCE = "unknown"

MAX_LOGIN_BODY_BYTES = 4096
USERNAME_LENGTH = (1, 64)
PASSWORD_LENGTH = (1, 256)
MAX_LOGINS_IN_PROGRESS = 10
# 同时最多两个 authenticate 在线程里运行（5.1 第 6 步）；
# 其余在事件循环里等，不占线程、连接与行锁。
LOGIN_LIMITER = anyio.CapacityLimiter(2)
# 进行中的登录（已过第 5 步、还没回答的）；只在事件循环线程里加减，不需要锁。
logins_in_progress = 0

NO_STORE = {"Cache-Control": "no-store"}

# detail
CROSS_SITE_REQUEST = "cross_site_request"
UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
INVALID_REQUEST = "invalid_request"
LOGIN_BUSY = "login_busy"
LOGIN_LOCKED = "login_locked"
NOT_AUTHENTICATED = "not_authenticated"
CSRF_FAILED = "csrf_failed"
DATABASE_UNREACHABLE = "database_unreachable"

_log = logging.getLogger("app.api")

router = APIRouter()


@dataclass(frozen=True)
class AdminPrincipal:
    """已登录的管理员：操作主体是 credential_id，actor_ref 是 account_id（5.4）。"""

    account_id: str
    username: str
    session_id: int
    tenant_id: str
    credential_id: str


@dataclass(frozen=True)
class _Current:
    """解析出的有效会话与 cookie 里的令牌原文（CSRF 令牌由它重算）。"""

    session: ActiveSession
    token: str


def _detail(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status, NO_STORE)


def _reject(status: int, detail: str) -> HTTPException:
    return HTTPException(status, detail, headers=NO_STORE)


def _format_expires_at(value: datetime) -> str:
    # 库里本就是整秒，原样格式化。
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _session_body(username: str, expires_at: datetime, token: str) -> dict[str, str]:
    return {
        "username": username,
        "expires_at": _format_expires_at(expires_at),
        "csrf_token": csrf_token_for(token),
    }


def _set_cookie(response: Response, token: str, max_age: int) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max_age,
        path="/",
        secure=True,
        httponly=True,
        samesite="Strict",
    )


# --- 依赖（5.4） ---


def get_connection(request: Request) -> Connection:
    """本请求唯一的连接；取不到时 503 database_unreachable（detail 形状）。"""
    try:
        return request_connection(request)
    except (ApiError, SQLAlchemyError):
        raise _reject(503, DATABASE_UNREACHABLE) from None


def _current_session(request: Request) -> _Current:
    """先看 cookie，再查库；查完就回滚、结束读事务。"""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise _reject(401, NOT_AUTHENTICATED)
    connection = get_connection(request)
    try:
        session = resolve_session(connection, token=token, now=utc_now())
        connection.rollback()
    except SQLAlchemyError:
        _log.warning(
            "database error resolving an admin session (request_id=%s)",
            request.state.request_id,
            exc_info=True,
        )
        invalidate_connection(request)
        raise _reject(503, DATABASE_UNREACHABLE) from None
    if session is None:
        raise _reject(401, NOT_AUTHENTICATED)
    return _Current(session, token)


CurrentSession = Annotated[_Current, Depends(_current_session)]


def require_admin(current: CurrentSession) -> AdminPrincipal:
    """须为已登录管理员：会话无效 401 not_authenticated。"""
    session = current.session
    return AdminPrincipal(
        account_id=session.account_id,
        username=session.username,
        session_id=session.session_id,
        tenant_id=session.tenant_id,
        credential_id=session.credential_id,
    )


def require_admin_csrf(
    request: Request,
    current: CurrentSession,
    principal: Annotated[AdminPrincipal, Depends(require_admin)],
) -> AdminPrincipal:
    """先做 require_admin，再校验 X-CSRF-Token，不符 403 csrf_failed。

    之后改数据的后台接口都用它。
    """
    if not csrf_token_matches(current.token, request.headers.get(CSRF_HEADER)):
        raise _reject(403, CSRF_FAILED)
    return principal


# --- 登录（5.1） ---


def login_source(request: Request) -> str:
    """5.3：X-Real-IP 恰好一个且是合法地址时用它，否则用连接对端地址。

    再没有时用 unknown。不读 X-Forwarded-For。
    """
    values = request.headers.getlist(REAL_IP_HEADER)
    if len(values) == 1:
        try:
            ipaddress.ip_address(values[0])
        except ValueError:
            pass
        else:
            return values[0]
    client = request.client
    if client is not None and client.host:
        return client.host
    return UNKNOWN_SOURCE


def _media_type(value: str | None) -> str:
    return (value or "").split(";")[0].strip().lower()


async def _read_login_body(request: Request) -> bytes | None:
    """第 4 步：Content-Length 或实际读到的字节超过上限时返回 None。"""
    length = request.headers.get("content-length")
    if length is not None and length.isdigit() and int(length) > MAX_LOGIN_BODY_BYTES:
        return None
    # 逐块读，超过即停。
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_LOGIN_BODY_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _within(value: object, bounds: tuple[int, int]) -> bool:
    return isinstance(value, str) and bounds[0] <= len(value) <= bounds[1]


def _parse_credentials(body: bytes) -> tuple[str, str] | None:
    """第 5 步：不合格返回 None；多出来的字段忽略。"""
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (ValueError, RecursionError):
        return None
    if not isinstance(parsed, dict):
        return None
    username, password = parsed.get("username"), parsed.get("password")
    if not _within(username, USERNAME_LENGTH) or not _within(password, PASSWORD_LENGTH):
        return None
    return username, password


def _authenticate(engine: Engine, source: str, username: str, password: str) -> LoginResult:
    with engine.connect() as connection:
        return authenticate(
            connection, source=source, username=username, password=password, now=utc_now()
        )


def _revoke_previous(engine: Engine, token: str, new_session_id: int | None) -> None:
    """第 7 步：请求带来的旧会话仍有效、且不是刚建的新会话时撤销它。"""
    with engine.connect() as connection:
        previous = resolve_session(connection, token=token, now=utc_now())
        connection.rollback()
        if previous is not None and previous.session_id != new_session_id:
            revoke_session(connection, session_id=previous.session_id, now=utc_now())


async def _login(request: Request, username: str, password: str) -> Response:
    engine: Engine = request.app.state.engine
    source = login_source(request)
    try:
        result = await anyio.to_thread.run_sync(
            partial(_authenticate, engine, source, username, password), limiter=LOGIN_LIMITER
        )
    except SQLAlchemyError:
        _log.warning(
            "database error during admin login (request_id=%s)",
            request.state.request_id,
            exc_info=True,
        )
        return _detail(503, DATABASE_UNREACHABLE)
    if result.outcome == LOCKED:
        return _detail(429, LOGIN_LOCKED)
    if result.outcome != SUCCESS:
        return _detail(401, INVALID_CREDENTIALS)

    previous = request.cookies.get(SESSION_COOKIE)
    if previous:
        try:
            await anyio.to_thread.run_sync(
                partial(_revoke_previous, engine, previous, result.session_id)
            )
        except SQLAlchemyError:
            # 不改变回答：新会话已建，照样设新 cookie；旧会话留到自然到期。
            _log.warning(
                "revoking the previous admin session failed (request_id=%s)",
                request.state.request_id,
                exc_info=True,
            )
    body = _session_body(result.username, result.expires_at, result.token)
    response = JSONResponse(body, 200, NO_STORE)
    _set_cookie(response, result.token, SESSION_MAX_AGE)
    return response


@router.post(SESSION_PATH)
async def login(request: Request) -> Response:
    """按 5.1 第 2–7 步判定；前四步的拒绝都不写登录尝试。"""
    global logins_in_progress
    if request.headers.get("sec-fetch-site", "").strip().lower() == "cross-site":
        return _detail(403, CROSS_SITE_REQUEST)
    if _media_type(request.headers.get("content-type")) != "application/json":
        return _detail(415, UNSUPPORTED_MEDIA_TYPE)
    body = await _read_login_body(request)
    # 不用框架默认的 422：它会把提交的值（含密码）放进响应。
    if body is None:
        return _detail(422, INVALID_REQUEST)
    credentials = _parse_credentials(body)
    if credentials is None:
        return _detail(422, INVALID_REQUEST)
    if logins_in_progress >= MAX_LOGINS_IN_PROGRESS:
        return _detail(503, LOGIN_BUSY)
    logins_in_progress += 1
    try:
        return await _login(request, *credentials)
    finally:
        logins_in_progress -= 1


# --- 当前会话与退出 ---


@router.get(SESSION_PATH)
def current_session(current: CurrentSession) -> Response:
    session = current.session
    body = _session_body(session.username, session.expires_at, current.token)
    return JSONResponse(body, 200, NO_STORE)


@router.delete(SESSION_PATH, status_code=204)
def logout(
    request: Request, principal: Annotated[AdminPrincipal, Depends(require_admin_csrf)]
) -> Response:
    """撤销会话，以同名同属性、Max-Age=0 清除 cookie。"""
    connection = get_connection(request)
    try:
        revoke_session(connection, session_id=principal.session_id, now=utc_now())
    except SQLAlchemyError:
        _log.warning(
            "database error revoking an admin session (request_id=%s)",
            request.state.request_id,
            exc_info=True,
        )
        invalidate_connection(request)
        raise _reject(503, DATABASE_UNREACHABLE) from None
    response = Response(status_code=204, headers=NO_STORE)
    _set_cookie(response, "", 0)
    return response
