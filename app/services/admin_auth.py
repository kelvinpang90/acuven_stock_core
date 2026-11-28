"""后台管理员的登录、会话、CSRF 与账号变更（docs/ADMIN_AUTH.md 4.3–4.5）。

所有函数收 SQLAlchemy Connection；“提交”“回滚”指在这个连接上提交、回滚事务。改数据的函数
各自完成并提交一个事务，出错时回滚并原样抛出；只读的不提交。时间由参数传入，取当前时间只用
utc_now()。命令（4.6）与接口（第 5 节）只调这里的函数，不另写查询。
密码、哈希、会话令牌、CSRF 令牌、throttle_key 与 source_key 不进日志与异常消息。
"""

import hashlib
import hmac
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import ColumnElement, Connection, Row, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.domain.ids import new_id
from app.domain.login_throttle import (
    FAILURE_OUTCOMES,
    lock_reason,
    normalize_source,
    require_whole_second,
    source_key,
    throttle_key,
    window_start,
)
from app.domain.pw_hash import (
    DUMMY_PASSWORD_HASH,
    hash_password,
    is_valid_password,
    is_valid_username,
    normalize_username,
    verify_password,
)
from app.domain.service_auth import ADMIN
from app.models.admin_auth import AdminAccount, AdminLoginAttempt, AdminSession
from app.models.catalog import Tenant
from app.models.service_auth import Credential

ACTIVE = "active"
SESSION_LIFETIME = timedelta(hours=12)
SESSION_TOKEN_BYTES = 32
CSRF_MESSAGE = b"core-admin-csrf"

# LoginResult.outcome
SUCCESS = "success"
INVALID_CREDENTIALS = "invalid_credentials"
LOCKED = "locked"

# admin_login_attempts.outcome
BAD_PASSWORD = "bad_password"
UNKNOWN_USER = "unknown_user"
DISABLED = "disabled"

# AccountError.code
INVALID_USERNAME = "invalid_username"
INVALID_TENANT = "invalid_tenant"
ALREADY_EXISTS = "already_exists"
NOT_FOUND = "not_found"
WEAK_PASSWORD = "weak_password"

_USERNAME_UNIQUE = "uq_admin_accounts_username"


class AccountError(Exception):
    """账号变更的业务错误；消息就是 code，不含密码。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class LoginResult:
    """outcome 为 success 时其余各项有值，否则都为 None；repr 不含 token。"""

    outcome: str
    account_id: str | None = None
    username: str | None = None
    session_id: int | None = None
    token: str | None = field(default=None, repr=False)
    expires_at: datetime | None = None


@dataclass(frozen=True)
class ActiveSession:
    """一条有效会话；credential_id 是账号的操作主体（kind 为 admin）。"""

    session_id: int
    account_id: str
    username: str
    tenant_id: str
    credential_id: str
    expires_at: datetime


@dataclass(frozen=True)
class AccountInfo:
    """账号命令输出与记安全事件用（4.6）。"""

    account_id: str
    tenant_id: str
    credential_id: str
    username: str
    disabled: bool


def utc_now() -> datetime:
    """不带时区的 UTC 当前时间，截到整秒（截断，不四舍五入）。"""
    return datetime.now(UTC).replace(tzinfo=None, microsecond=0)


@contextmanager
def _committed(connection: Connection) -> Iterator[None]:
    """块内的语句在一个事务里：正常结束提交，出错回滚并原样抛出。"""
    try:
        yield
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _token_hash(token: object) -> str | None:
    if not isinstance(token, str) or not token:
        return None
    try:
        raw = token.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return hashlib.sha256(raw).hexdigest()


def _new_token() -> str:
    return secrets.token_urlsafe(SESSION_TOKEN_BYTES)


_ACCOUNT_COLUMNS = (
    AdminAccount.admin_account_id,
    AdminAccount.tenant_id,
    AdminAccount.credential_id,
    AdminAccount.username,
    AdminAccount.password_hash,
    AdminAccount.disabled_at,
)


def _lock_account(connection: Connection, username: str) -> Row | None:
    """按规范用户名 SELECT … FOR UPDATE，账号行锁到本事务提交（4.4 的并发）。"""
    statement = select(*_ACCOUNT_COLUMNS).where(AdminAccount.username == username).with_for_update()
    return connection.execute(statement).one_or_none()


def _find_account(connection: Connection, username: str) -> Row | None:
    statement = select(*_ACCOUNT_COLUMNS).where(AdminAccount.username == username)
    return connection.execute(statement).one_or_none()


def _revoke_all(connection: Connection, account: Row, now: datetime) -> None:
    connection.execute(
        update(AdminSession)
        .where(
            AdminSession.tenant_id == account.tenant_id,
            AdminSession.admin_account_id == account.admin_account_id,
            AdminSession.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )


# --- 登录（4.3） ---


def _count_failures(connection: Connection, key: ColumnElement[bool], now: datetime) -> int:
    statement = (
        select(func.count())
        .select_from(AdminLoginAttempt)
        .where(
            key,
            AdminLoginAttempt.occurred_at > window_start(now),
            AdminLoginAttempt.occurred_at <= now,
            AdminLoginAttempt.outcome.in_(sorted(FAILURE_OUTCOMES)),
        )
    )
    return connection.execute(statement).scalar_one()


def _record(
    connection: Connection,
    keys: tuple[str, str],
    outcome: str,
    now: datetime,
    account: Row | None = None,
) -> None:
    connection.execute(
        insert(AdminLoginAttempt).values(
            occurred_at=now,
            throttle_key=keys[0],
            source_key=keys[1],
            tenant_id=None if account is None else account.tenant_id,
            admin_account_id=None if account is None else account.admin_account_id,
            outcome=outcome,
        )
    )


def _login(
    connection: Connection, keys: tuple[str, str], username: str, password: str, now: datetime
) -> LoginResult:
    source_failures = _count_failures(connection, AdminLoginAttempt.source_key == keys[1], now)
    throttle_failures = _count_failures(connection, AdminLoginAttempt.throttle_key == keys[0], now)
    reason = lock_reason(source_failures=source_failures, throttle_failures=throttle_failures)
    if reason is not None:
        # 不查账号、不校验密码。
        _record(connection, keys, LOCKED, now)
        return LoginResult(LOCKED)

    # 规范用户名不合法就不查库，按用户名不存在处理。
    account = _lock_account(connection, username) if is_valid_username(username) else None
    if account is None:
        verify_password(password, DUMMY_PASSWORD_HASH)
        _record(connection, keys, UNKNOWN_USER, now)
        return LoginResult(INVALID_CREDENTIALS)
    if not verify_password(password, account.password_hash):
        _record(connection, keys, BAD_PASSWORD, now, account)
        return LoginResult(INVALID_CREDENTIALS)
    # 先校验密码再看停用：与密码错误不可区分，耗时也一致。
    if account.disabled_at is not None:
        _record(connection, keys, DISABLED, now, account)
        return LoginResult(INVALID_CREDENTIALS)

    token = _new_token()
    expires_at = now + SESSION_LIFETIME
    inserted = connection.execute(
        insert(AdminSession).values(
            tenant_id=account.tenant_id,
            admin_account_id=account.admin_account_id,
            token_hash=_token_hash(token),
            created_at=now,
            expires_at=expires_at,
        )
    )
    _record(connection, keys, SUCCESS, now, account)
    return LoginResult(
        SUCCESS,
        account_id=account.admin_account_id,
        username=account.username,
        session_id=inserted.inserted_primary_key[0],
        token=token,
        expires_at=expires_at,
    )


def authenticate(
    connection: Connection, *, source: str, username: str, password: str, now: datetime
) -> LoginResult:
    """一次登录：在这个连接上完成并提交一个事务，恰好写一条登录尝试。

    同步函数（数据库与 scrypt 都会阻塞），接口层放到线程池里调。
    数据库出错（SQLAlchemyError）时回滚并原样抛出。
    """
    require_whole_second(now)
    canonical_source = normalize_source(source)
    name = normalize_username(username)
    keys = (throttle_key(canonical_source, name), source_key(canonical_source))
    with _committed(connection):
        return _login(connection, keys, name, password, now)


# --- 会话与 CSRF（4.4） ---


def resolve_session(
    connection: Connection, *, token: object, now: datetime
) -> ActiveSession | None:
    """未撤销、expires_at > now、账号未停用才返回；只读，不提交。"""
    require_whole_second(now)
    token_hash = _token_hash(token)
    if token_hash is None:
        return None
    statement = (
        select(
            AdminSession.id,
            AdminSession.expires_at,
            AdminSession.revoked_at,
            AdminAccount.admin_account_id,
            AdminAccount.username,
            AdminAccount.tenant_id,
            AdminAccount.credential_id,
            AdminAccount.disabled_at,
        )
        .join(
            AdminAccount,
            (AdminAccount.tenant_id == AdminSession.tenant_id)
            & (AdminAccount.admin_account_id == AdminSession.admin_account_id),
        )
        .where(AdminSession.token_hash == token_hash)
    )
    row = connection.execute(statement).one_or_none()
    if row is None or row.revoked_at is not None or row.disabled_at is not None:
        return None
    if not row.expires_at > now:
        return None
    return ActiveSession(
        session_id=row.id,
        account_id=row.admin_account_id,
        username=row.username,
        tenant_id=row.tenant_id,
        credential_id=row.credential_id,
        expires_at=row.expires_at,
    )


def revoke_session(connection: Connection, *, session_id: int, now: datetime) -> None:
    """设 revoked_at 并提交；已撤销的不改原值，没有这个会话时什么都不做。"""
    require_whole_second(now)
    with _committed(connection):
        connection.execute(
            update(AdminSession)
            .where(AdminSession.id == session_id, AdminSession.revoked_at.is_(None))
            .values(revoked_at=now)
        )


def csrf_token_for(token: str) -> str:
    """HMAC-SHA256(key=会话令牌的 UTF-8 字节, msg=b"core-admin-csrf") 的十六进制；不入库。"""
    if not isinstance(token, str) or not token:
        raise ValueError("session token must be a non-empty string")
    try:
        key = token.encode("utf-8")
    except UnicodeEncodeError:
        # 不带原文：UnicodeEncodeError 的对象里存着整个令牌。
        raise ValueError("session token must be UTF-8 encodable") from None
    return hmac.new(key, CSRF_MESSAGE, hashlib.sha256).hexdigest()


def csrf_token_matches(token: str, presented: object) -> bool:
    """presented 不是字符串、为空或含非 ASCII 字符时为 False，否则常量时间比较。"""
    if not isinstance(presented, str) or not presented or not presented.isascii():
        return False
    if _token_hash(token) is None:
        return False
    expected = csrf_token_for(token)
    return hmac.compare_digest(expected.encode("ascii"), presented.encode("ascii"))


# --- 账号变更（4.5） ---


def _username(username: object) -> str:
    """规范用户名；不合法抛 invalid_username，不查库。"""
    if not isinstance(username, str):
        raise AccountError(INVALID_USERNAME)
    name = normalize_username(username)
    if not is_valid_username(name):
        raise AccountError(INVALID_USERNAME)
    return name


def _require_account(connection: Connection, username: str) -> Row:
    account = _lock_account(connection, username)
    if account is None:
        raise AccountError(NOT_FOUND)
    return account


def _require_password(password: object, username: str) -> None:
    if not is_valid_password(password, username=username):
        raise AccountError(WEAK_PASSWORD)


def _create(
    connection: Connection, tenant_id: str, username: str, password: str, now: datetime
) -> str:
    # FOR SHARE：租户行锁到本事务提交，与并发停用租户串行。
    tenant_status = connection.execute(
        select(Tenant.status).where(Tenant.tenant_id == tenant_id).with_for_update(read=True)
    ).scalar_one_or_none()
    if tenant_status != ACTIVE:
        raise AccountError(INVALID_TENANT)
    if _find_account(connection, username) is not None:
        raise AccountError(ALREADY_EXISTS)
    _require_password(password, username)

    credential_id, account_id = new_id("cred"), new_id("adm")
    connection.execute(
        insert(Credential).values(
            credential_id=credential_id,
            tenant_id=tenant_id,
            channel_id=None,
            name=username,
            scopes=[],
            kind=ADMIN,
            status=ACTIVE,
            created_at=now,
            updated_at=now,
        )
    )
    connection.execute(
        insert(AdminAccount).values(
            admin_account_id=account_id,
            tenant_id=tenant_id,
            credential_id=credential_id,
            username=username,
            password_hash=hash_password(password),
            created_at=now,
            password_updated_at=now,
        )
    )
    return account_id


def create_account(
    connection: Connection, *, tenant_id: str, username: str, password: str, now: datetime
) -> str:
    """同一事务里建 admin 主体与指向它的账号，返回 adm_…。

    检查顺序：用户名 → 租户（存在且 active）→ 账号存在与否 → 密码规则。
    """
    require_whole_second(now)
    name = _username(username)
    try:
        with _committed(connection):
            return _create(connection, tenant_id, name, password, now)
    except IntegrityError as error:
        # 并发建同名账号：预检之后撞上唯一约束（已回滚）。
        if _USERNAME_UNIQUE in str(error.orig):
            raise AccountError(ALREADY_EXISTS) from None
        raise


def reset_password(connection: Connection, *, username: str, password: str, now: datetime) -> None:
    """换哈希与 password_updated_at，撤销该账号全部未撤销的会话。"""
    require_whole_second(now)
    name = _username(username)
    with _committed(connection):
        account = _require_account(connection, name)
        _require_password(password, name)
        connection.execute(
            update(AdminAccount)
            .where(AdminAccount.admin_account_id == account.admin_account_id)
            .values(password_hash=hash_password(password), password_updated_at=now)
        )
        _revoke_all(connection, account, now)


def disable_account(connection: Connection, *, username: str, now: datetime) -> bool:
    """设 disabled_at 并撤销全部会话；False 表示原本已停用、未改。不吊销账号的主体。"""
    require_whole_second(now)
    name = _username(username)
    with _committed(connection):
        account = _require_account(connection, name)
        if account.disabled_at is not None:
            return False
        connection.execute(
            update(AdminAccount)
            .where(AdminAccount.admin_account_id == account.admin_account_id)
            .values(disabled_at=now)
        )
        _revoke_all(connection, account, now)
        return True


def enable_account(connection: Connection, *, username: str) -> bool:
    """清空 disabled_at，不恢复已撤销的会话；False 表示原本未停用、未改。"""
    name = _username(username)
    with _committed(connection):
        account = _require_account(connection, name)
        if account.disabled_at is None:
            return False
        connection.execute(
            update(AdminAccount)
            .where(AdminAccount.admin_account_id == account.admin_account_id)
            .values(disabled_at=None)
        )
        return True


def account_exists(connection: Connection, *, username: str) -> bool:
    """只读，供命令在提示输入密码之前检查；用户名不合法抛 invalid_username。"""
    return _find_account(connection, _username(username)) is not None


def get_account(connection: Connection, *, username: str) -> AccountInfo | None:
    """只读；用户名规范化后不合法返回 None、不查库。"""
    if not isinstance(username, str):
        return None
    name = normalize_username(username)
    if not is_valid_username(name):
        return None
    account = _find_account(connection, name)
    if account is None:
        return None
    return AccountInfo(
        account_id=account.admin_account_id,
        tenant_id=account.tenant_id,
        credential_id=account.credential_id,
        username=account.username,
        disabled=account.disabled_at is not None,
    )
