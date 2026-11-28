"""后台管理员的登录、锁定、会话、CSRF 与账号变更（docs/ADMIN_AUTH.md 4.3–4.5）。

在真 MySQL 上跑（conftest 的 migrated_engine）；时间都由参数传入，都是整秒。
每个测试建自己的租户；来源取随机的 10.x.y.z（或随机的 IPv6 /64），用户名带随机后缀，
所以各测试的登录尝试互不计数。锁定之前的失败直接插入登录尝试（不做 10 次 scrypt）。
数据都在 db 夹具之外提交，测试结束时在 finally 里按外键先后删掉（登录尝试、会话、账号、
主体、租户）：admin 主体挡 0012 的降级，删掉之后会话结束时 migrated_engine 才能照常降到 base。
"""

import hashlib
import hmac
import logging
import re
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import Connection, Engine, delete, event, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.domain.login_throttle import normalize_source, source_key, throttle_key
from app.domain.pw_hash import DUMMY_PASSWORD_HASH, normalize_username, verify_password
from app.domain.service_auth import ADMIN
from app.models.admin_auth import AdminAccount, AdminLoginAttempt, AdminSession
from app.models.catalog import Tenant
from app.models.service_auth import Credential, CredentialToken
from app.services import admin_auth
from app.services.admin_auth import (
    AccountError,
    ActiveSession,
    LoginResult,
    account_exists,
    authenticate,
    create_account,
    csrf_token_for,
    csrf_token_matches,
    disable_account,
    enable_account,
    get_account,
    reset_password,
    resolve_session,
    revoke_session,
    utc_now,
)
from tests.integration.conftest import new_id

PASSWORD = "correct horse battery"
NEW_PASSWORD = "staple paper clip 42"
WRONG_PASSWORD = "wrong horse battery"
CREATED_AT = datetime(2026, 11, 24, 7, 0)
NOW = datetime(2026, 11, 24, 10, 15)
LOCKED = LoginResult("locked")
INVALID = LoginResult("invalid_credentials")
FAILURES = ("bad_password", "unknown_user", "disabled")
ACCOUNTS_TABLE = re.compile(r"\badmin_accounts\b")


def at(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 11, 24, hour, minute, second)


# --- 造数据与清理 ---


@dataclass(frozen=True)
class Account:
    account_id: str
    tenant_id: str
    credential_id: str
    username: str


@dataclass
class World:
    engine: Engine
    tenant_id: str = ""
    tenant_ids: list[str] = field(default_factory=list)
    source_keys: set[str] = field(default_factory=set)

    def tenant(self, status: str = "active") -> str:
        tenant_id = new_id("ten")
        with self.engine.begin() as db:
            db.execute(insert(Tenant).values(tenant_id=tenant_id, name="Tenant", status=status))
        self.tenant_ids.append(tenant_id)
        return tenant_id

    def register(self, text: str) -> str:
        """登记一个来源原文，清理时按它的 source_key 删登录尝试。"""
        self.source_keys.add(source_key(normalize_source(text)))
        return text

    def source(self) -> str:
        raw = uuid4().bytes
        return self.register(f"10.{raw[0]}.{raw[1]}.{raw[2]}")

    def account(self, username: str = "", password: str = PASSWORD) -> Account:
        username = username or new_username()
        with self.engine.connect() as conn:
            create_account(
                conn,
                tenant_id=self.tenant_id,
                username=username,
                password=password,
                now=CREATED_AT,
            )
            info = get_account(conn, username=username)
        assert info is not None
        return Account(info.account_id, info.tenant_id, info.credential_id, info.username)


def new_username() -> str:
    # 以 kelvin 开头：L6 要把第二个字母换成 é。
    return f"kelvin{uuid4().hex[:12]}"


def cleanup(engine: Engine, world: World) -> None:
    tenants = AdminLoginAttempt.tenant_id.in_(world.tenant_ids)
    sources = AdminLoginAttempt.source_key.in_(sorted(world.source_keys))
    with engine.begin() as db:
        db.execute(delete(AdminLoginAttempt).where(or_(tenants, sources)))
        for model in (AdminSession, AdminAccount, Credential, Tenant):
            db.execute(delete(model).where(model.tenant_id.in_(world.tenant_ids)))


@pytest.fixture
def engine(migrated_engine: Engine) -> Engine:
    return migrated_engine


@pytest.fixture
def world(engine: Engine) -> Iterator[World]:
    built = World(engine)
    try:
        built.tenant_id = built.tenant()
        yield built
    finally:
        cleanup(engine, built)


def keys_of(source: str, username: str) -> tuple[str, str]:
    canonical = normalize_source(source)
    return throttle_key(canonical, normalize_username(username)), source_key(canonical)


def seed(
    engine: Engine,
    source: str,
    username: str,
    times: list[datetime],
    outcome: str = "bad_password",
    account: Account | None = None,
) -> None:
    """直接插入登录尝试，摘要按服务的算法算。"""
    throttle, source_hash = keys_of(source, username)
    rows = [
        {
            "occurred_at": occurred_at,
            "throttle_key": throttle,
            "source_key": source_hash,
            "tenant_id": None if account is None else account.tenant_id,
            "admin_account_id": None if account is None else account.account_id,
            "outcome": outcome,
        }
        for occurred_at in times
    ]
    with engine.begin() as db:
        db.execute(insert(AdminLoginAttempt), rows)


def l1_times() -> list[datetime]:
    """10:00:00～10:09:00 每分钟一次。"""
    return [at(10, minute) for minute in range(10)]


def new_token() -> str:
    return f"test-token-{uuid4().hex}"


def insert_session(
    db: Connection, account: Account, token: str, created_at: datetime = CREATED_AT
) -> None:
    db.execute(
        insert(AdminSession).values(
            tenant_id=account.tenant_id,
            admin_account_id=account.account_id,
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            created_at=created_at,
            expires_at=created_at + timedelta(hours=12),
        )
    )


# --- 观察 ---


def attempt_count(engine: Engine) -> int:
    with engine.connect() as db:
        return db.execute(select(func.count()).select_from(AdminLoginAttempt)).scalar_one()


def last_attempt(engine: Engine) -> dict:
    statement = select(AdminLoginAttempt.__table__).order_by(AdminLoginAttempt.id.desc()).limit(1)
    with engine.connect() as db:
        return dict(db.execute(statement).mappings().one())


def failures_of(engine: Engine, source: str, username: str) -> int:
    """这个来源与用户名的全部失败（不论时间）。"""
    statement = (
        select(func.count())
        .select_from(AdminLoginAttempt)
        .where(
            AdminLoginAttempt.throttle_key == keys_of(source, username)[0],
            AdminLoginAttempt.outcome.in_(FAILURES),
        )
    )
    with engine.connect() as db:
        return db.execute(statement).scalar_one()


def sessions_of(engine: Engine, account: Account) -> list[dict]:
    statement = (
        select(*AdminSession.__table__.columns)
        .where(AdminSession.admin_account_id == account.account_id)
        .order_by(AdminSession.id)
    )
    with engine.connect() as db:
        return [dict(row) for row in db.execute(statement).mappings()]


def account_row(engine: Engine, account: Account) -> dict:
    statement = select(*AdminAccount.__table__.columns).where(
        AdminAccount.admin_account_id == account.account_id
    )
    with engine.connect() as db:
        return dict(db.execute(statement).mappings().one())


def credentials_of(engine: Engine, tenant_id: str) -> list[dict]:
    statement = select(*Credential.__table__.columns).where(Credential.tenant_id == tenant_id)
    with engine.connect() as db:
        return [dict(row) for row in db.execute(statement).mappings()]


def record_sql(conn: Connection) -> list[str]:
    """记下这个连接上此后执行的 SQL 文本（监听挂在这个连接对象上，随它关闭而作废）。"""
    statements: list[str] = []

    def record(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(conn, "before_cursor_execute", record)
    return statements


def login_with_sql(
    engine: Engine, source: str, username: str, password: str = PASSWORD, now: datetime = NOW
) -> tuple[LoginResult, list[str]]:
    """登录一次；断言恰好多一条登录尝试、事务已提交。"""
    before = attempt_count(engine)
    with engine.connect() as conn:
        statements = record_sql(conn)
        result = authenticate(conn, source=source, username=username, password=password, now=now)
        assert not conn.in_transaction()
    assert attempt_count(engine) == before + 1
    return result, statements


def login(
    engine: Engine, source: str, username: str, password: str = PASSWORD, now: datetime = NOW
) -> LoginResult:
    return login_with_sql(engine, source, username, password, now)[0]


def queries_accounts(statements: list[str]) -> bool:
    return any(ACCOUNTS_TABLE.search(statement) for statement in statements)


def resolve(engine: Engine, token: object, now: datetime) -> ActiveSession | None:
    with engine.connect() as conn:
        return resolve_session(conn, token=token, now=now)


def spy_verify(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """替换服务用的校验函数，记下每次校验的哈希串。"""
    calls: list[str] = []

    def spy(password, stored):
        calls.append(stored)
        return verify_password(password, stored)

    monkeypatch.setattr(admin_auth, "verify_password", spy)
    return calls


def assert_attempt(engine: Engine, source: str, username: str, outcome: str, account=None):
    throttle, source_hash = keys_of(source, username)
    assert last_attempt(engine) | {"id": None} == {
        "id": None,
        "occurred_at": NOW,
        "throttle_key": throttle,
        "source_key": source_hash,
        "tenant_id": None if account is None else account.tenant_id,
        "admin_account_id": None if account is None else account.account_id,
        "outcome": outcome,
    }


def account_error(call, *args, **kwargs) -> str:
    with pytest.raises(AccountError) as caught:
        call(*args, **kwargs)
    return caught.value.code


# --- 登录的五种结果 ---


def test_success_creates_a_session_and_records_success(engine, world, monkeypatch):
    account = world.account()
    source = world.source()
    calls = spy_verify(monkeypatch)
    result, statements = login_with_sql(engine, source, account.username)

    assert result.outcome == "success"
    assert result.account_id == account.account_id
    assert result.username == account.username
    assert isinstance(result.session_id, int)
    assert result.expires_at == NOW + timedelta(hours=12)
    assert calls == [account_row(engine, account)["password_hash"]]
    # 查账号用 SELECT … FOR UPDATE。
    assert any(ACCOUNTS_TABLE.search(s) and "FOR UPDATE" in s for s in statements)

    [session] = sessions_of(engine, account)
    assert session == {
        "id": result.session_id,
        "tenant_id": account.tenant_id,
        "admin_account_id": account.account_id,
        "token_hash": hashlib.sha256(result.token.encode()).hexdigest(),
        "created_at": NOW,
        "expires_at": NOW + timedelta(hours=12),
        "revoked_at": None,
    }
    assert_attempt(engine, source, account.username, "success", account)


def test_bad_password_is_invalid_credentials(engine, world):
    account = world.account()
    source = world.source()
    assert login(engine, source, account.username, WRONG_PASSWORD) == INVALID
    assert sessions_of(engine, account) == []
    assert_attempt(engine, source, account.username, "bad_password", account)


def test_an_unknown_username_checks_the_dummy_hash_once(engine, world, monkeypatch):
    source, username = world.source(), new_username()
    calls = spy_verify(monkeypatch)
    result, statements = login_with_sql(engine, source, username)
    assert result == INVALID
    assert calls == [DUMMY_PASSWORD_HASH]
    assert queries_accounts(statements)
    assert_attempt(engine, source, username, "unknown_user")


@pytest.mark.parametrize("username", ["kélvin", "ab", "kelvin\n", "kel vin", "x" * 33, ""])
def test_an_invalid_username_does_not_query_accounts(engine, world, monkeypatch, username):
    source = world.source()
    calls = spy_verify(monkeypatch)
    result, statements = login_with_sql(engine, source, username)
    assert result == INVALID
    # 同 4.3 第 3 步：照样对假哈希校验恰好一次。
    assert calls == [DUMMY_PASSWORD_HASH]
    assert not queries_accounts(statements)
    assert_attempt(engine, source, username, "unknown_user")


def test_a_disabled_account_is_invalid_credentials_after_the_password_check(
    engine, world, monkeypatch
):
    account = world.account()
    with engine.connect() as conn:
        assert disable_account(conn, username=account.username, now=CREATED_AT)
    source = world.source()
    calls = spy_verify(monkeypatch)
    assert login(engine, source, account.username) == INVALID
    assert calls == [account_row(engine, account)["password_hash"]]
    assert_attempt(engine, source, account.username, "disabled", account)
    # 密码错误时先报 bad_password：先校验密码再看停用。
    assert login(engine, source, account.username, WRONG_PASSWORD) == INVALID
    assert_attempt(engine, source, account.username, "bad_password", account)
    assert sessions_of(engine, account) == []


def test_locked_does_not_query_accounts_or_check_the_password(engine, world, monkeypatch):
    account = world.account()
    source = world.source()
    seed(engine, source, account.username, l1_times(), account=account)
    calls = spy_verify(monkeypatch)
    result, statements = login_with_sql(engine, source, account.username)
    assert result == LOCKED
    assert calls == []
    assert not queries_accounts(statements)
    assert_attempt(engine, source, account.username, "locked")


# --- 锁定的例子 L1–L8 ---


def test_l1_ten_failures_lock_the_source_and_username(engine, world):
    account = world.account()
    source = world.source()
    seed(engine, source, account.username, l1_times(), account=account)

    assert login(engine, source, account.username, now=at(10, 15)) == LOCKED
    assert sessions_of(engine, account) == []
    assert failures_of(engine, source, account.username) == 10
    assert last_attempt(engine)["outcome"] == "locked"
    assert last_attempt(engine)["admin_account_id"] is None


def test_l2_the_oldest_failure_slides_out_and_the_login_succeeds(engine, world):
    account = world.account()
    source = world.source()
    seed(engine, source, account.username, l1_times(), account=account)

    # 10:00:00 那条不满足 > 10:00:00，只剩 9 条。
    result = login(engine, source, account.username, now=at(11, 0))
    assert result.outcome == "success"
    assert len(sessions_of(engine, account)) == 1
    assert last_attempt(engine)["outcome"] == "success"


def test_l3_another_source_is_not_locked(engine, world):
    account = world.account()
    source_a, source_b = world.source(), world.source()
    seed(engine, source_a, account.username, l1_times(), account=account)
    assert login(engine, source_b, account.username, now=at(10, 15)).outcome == "success"


def test_l4_an_unknown_username_locks_alike(engine, world):
    account = world.account()
    source = world.source()
    ghost = new_username()
    seed(engine, source, ghost, l1_times(), outcome="unknown_user")
    ghost_result = login(engine, source, ghost)

    other_source = world.source()
    seed(engine, other_source, account.username, l1_times(), account=account)
    known_result = login(engine, other_source, account.username)

    # 与用户名存在时的表现一致，不泄露用户名是否存在。
    assert ghost_result == known_result == LOCKED
    assert_attempt(engine, other_source, account.username, "locked")


def test_l5_success_does_not_reset_the_failures(engine, world):
    account = world.account()
    source = world.source()
    nine = [at(10, minute) for minute in range(9)]
    seed(engine, source, account.username, nine, account=account)
    seed(engine, source, account.username, [at(10, 9)], outcome="success", account=account)

    assert login(engine, source, account.username, WRONG_PASSWORD) == INVALID
    assert failures_of(engine, source, account.username) == 10
    assert login(engine, source, account.username) == LOCKED


def test_l6_case_variants_share_the_key_and_non_ascii_never_matches(engine, world):
    account = world.account()
    source = world.source()
    seed(engine, source, account.username, l1_times(), account=account)

    upper = account.username.upper()
    assert login(engine, source, upper) == LOCKED
    assert last_attempt(engine)["throttle_key"] == keys_of(source, account.username)[0]

    accented = "ké" + account.username[2:]
    assert login(engine, source, accented) == INVALID
    attempt = last_attempt(engine)
    assert attempt["outcome"] == "unknown_user"
    assert attempt["admin_account_id"] is None
    assert attempt["throttle_key"] == throttle_key(normalize_source(source), accented)
    assert attempt["throttle_key"] != keys_of(source, account.username)[0]

    # 换一个没有失败的来源，正确密码也登录不进 kelvin。
    fresh = world.source()
    assert login(engine, fresh, accented) == INVALID
    assert last_attempt(engine)["outcome"] == "unknown_user"
    assert sessions_of(engine, account) == []


def test_l7_thirty_failures_from_one_source_lock_every_username(engine, world):
    account = world.account()
    source_a, source_b = world.source(), world.source()
    # 09:30～09:59，都在 10:15 的窗口里。
    for index in range(30):
        seed(engine, source_a, new_username(), [at(9, 30 + index)], outcome="unknown_user")

    assert login(engine, source_a, account.username) == LOCKED
    assert failures_of(engine, source_a, account.username) == 0
    assert login(engine, source_b, account.username).outcome == "success"


def test_l8_ipv6_sources_are_counted_by_64_network(engine, world):
    account = world.account()
    raw = uuid4().bytes
    group_a, group_b = int.from_bytes(raw[:2]), int.from_bytes(raw[2:4]) % 0xFFFF
    failing = world.register(f"2001:db8:{group_a:x}:{group_b:x}::5")
    same_network = world.register(f"2001:db8:{group_a:x}:{group_b:x}::9")
    other_network = world.register(f"2001:db8:{group_a:x}:{group_b + 1:x}::5")
    assert normalize_source(failing) == normalize_source(same_network)
    assert normalize_source(failing) != normalize_source(other_network)
    seed(engine, failing, account.username, l1_times(), account=account)

    assert login(engine, same_network, account.username) == LOCKED
    assert login(engine, other_network, account.username).outcome == "success"


# --- 会话的例子 S1–S4 ---


def test_s1_a_session_expires_twelve_hours_after_login(engine, world):
    account = world.account()
    result = login(engine, world.source(), account.username, now=at(8, 0))
    assert result.expires_at == at(20, 0)

    expected = ActiveSession(
        session_id=result.session_id,
        account_id=account.account_id,
        username=account.username,
        tenant_id=account.tenant_id,
        credential_id=account.credential_id,
        expires_at=at(20, 0),
    )
    assert resolve(engine, result.token, at(19, 59)) == expected
    assert resolve(engine, result.token, at(19, 59, 59)) == expected
    # 要求 expires_at 严格大于现在。
    assert resolve(engine, result.token, at(20, 0)) is None
    assert resolve(engine, result.token, at(20, 0, 1)) is None


def test_s2_a_revoked_session_stays_revoked_at_the_first_time(engine, world):
    account = world.account()
    result = login(engine, world.source(), account.username, now=at(8, 0))
    with engine.connect() as conn:
        revoke_session(conn, session_id=result.session_id, now=at(9, 0))
        assert not conn.in_transaction()
    assert resolve(engine, result.token, at(9, 0)) is None

    with engine.connect() as conn:
        revoke_session(conn, session_id=result.session_id, now=at(10, 0))
    [session] = sessions_of(engine, account)
    assert session["revoked_at"] == at(9, 0)

    # 没有这个会话：什么都不做、不报错。
    with engine.connect() as conn:
        revoke_session(conn, session_id=result.session_id + 10**9, now=at(10, 0))


def test_s3_reset_password_revokes_every_session(engine, world):
    account = world.account()
    first = login(engine, world.source(), account.username, now=at(8, 0))
    second = login(engine, world.source(), account.username, now=at(8, 30))
    with engine.connect() as conn:
        reset_password(conn, username=account.username, password=NEW_PASSWORD, now=at(9, 0))
        assert not conn.in_transaction()

    assert [session["revoked_at"] for session in sessions_of(engine, account)] == [at(9, 0)] * 2
    assert resolve(engine, first.token, at(9, 0)) is None
    assert resolve(engine, second.token, at(9, 0)) is None
    assert account_row(engine, account)["password_updated_at"] == at(9, 0)

    source = world.source()
    assert login(engine, source, account.username, NEW_PASSWORD).outcome == "success"
    assert login(engine, source, account.username, PASSWORD) == INVALID
    assert_attempt(engine, source, account.username, "bad_password", account)


def test_s4_disable_revokes_and_enable_does_not_revive(engine, world):
    account = world.account()
    old = login(engine, world.source(), account.username, now=at(8, 0))
    with engine.connect() as conn:
        assert disable_account(conn, username=account.username, now=at(9, 0)) is True
    [session] = sessions_of(engine, account)
    assert session["revoked_at"] == at(9, 0)
    assert resolve(engine, old.token, at(9, 0)) is None

    source = world.source()
    assert login(engine, source, account.username) == INVALID
    assert_attempt(engine, source, account.username, "disabled", account)

    with engine.connect() as conn:
        assert enable_account(conn, username=account.username) is True
    assert account_row(engine, account)["disabled_at"] is None
    new = login(engine, source, account.username)
    assert new.outcome == "success"
    assert resolve(engine, new.token, NOW) is not None
    assert resolve(engine, old.token, NOW) is None


def test_disable_does_not_revoke_the_principal(engine, world):
    account = world.account()
    with engine.connect() as conn:
        disable_account(conn, username=account.username, now=at(9, 0))
    [principal] = credentials_of(engine, account.tenant_id)
    assert (principal["status"], principal["revoked_at"]) == ("active", None)


def test_a_disabled_account_has_no_valid_session_even_if_one_is_unrevoked(engine, world):
    account = world.account()
    token = new_token()
    with engine.begin() as db:
        insert_session(db, account, token)
        db.execute(
            update(AdminAccount)
            .where(AdminAccount.admin_account_id == account.account_id)
            .values(disabled_at=CREATED_AT)
        )
    assert resolve(engine, token, NOW) is None


@pytest.mark.parametrize("token", [None, "", 42, b"token", "abc\ud800", "no-such-token"])
def test_resolve_returns_none_for_unusable_tokens(engine, token):
    assert resolve(engine, token, NOW) is None


# --- CSRF ---


def test_csrf_token_is_an_hmac_of_the_session_token():
    token = new_token()
    expected = hmac.new(token.encode(), b"core-admin-csrf", hashlib.sha256).hexdigest()
    assert csrf_token_for(token) == expected
    assert csrf_token_for(token) == csrf_token_for(token)
    assert csrf_token_for(token) != csrf_token_for(new_token())


def test_csrf_token_matches():
    token = new_token()
    csrf = csrf_token_for(token)
    assert csrf_token_matches(token, csrf)
    assert not csrf_token_matches(token, csrf_token_for(new_token()))
    assert not csrf_token_matches(token, csrf.upper())
    assert not csrf_token_matches(token, csrf[:-1])
    # 不是字符串、为空或含非 ASCII 字符时为 False，不抛异常。
    for presented in (None, "", 42, csrf.encode(), csrf[:-1] + "é", "\ud800"):
        assert not csrf_token_matches(token, presented)
    for bad_token in ("", "abc\ud800", None):
        assert not csrf_token_matches(bad_token, csrf)


def test_csrf_token_for_does_not_echo_the_token():
    token = "secret-session-token\ud800"
    with pytest.raises(ValueError) as caught:
        csrf_token_for(token)
    assert token not in str(caught.value)
    assert "secret-session-token" not in str(caught.value)


# --- 账号变更（4.5） ---


def test_create_account_builds_an_admin_principal_and_the_account(engine, world):
    username = new_username()
    with engine.connect() as conn:
        account_id = create_account(
            conn, tenant_id=world.tenant_id, username=username.upper(), password=PASSWORD, now=NOW
        )
        assert not conn.in_transaction()
    assert account_id.startswith("adm_")

    [principal] = credentials_of(engine, world.tenant_id)
    assert principal["credential_id"].startswith("cred_")
    assert principal == {
        "credential_id": principal["credential_id"],
        "tenant_id": world.tenant_id,
        "channel_id": None,
        "name": username,
        "scopes": [],
        "kind": ADMIN,
        "status": "active",
        "revoked_at": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    with engine.connect() as db:
        tokens = select(CredentialToken.token_id).where(
            CredentialToken.credential_id == principal["credential_id"]
        )
        assert db.execute(tokens).all() == []

    account = Account(account_id, world.tenant_id, principal["credential_id"], username)
    row = account_row(engine, account)
    assert row | {"password_hash": None} == {
        "admin_account_id": account_id,
        "tenant_id": world.tenant_id,
        "credential_id": principal["credential_id"],
        "username": username,
        "password_hash": None,
        "created_at": NOW,
        "password_updated_at": NOW,
        "disabled_at": None,
    }
    assert verify_password(PASSWORD, row["password_hash"])

    result = login(engine, world.source(), username)
    session = resolve(engine, result.token, NOW)
    assert session is not None
    assert (session.tenant_id, session.credential_id) == (world.tenant_id, account.credential_id)
    assert session.account_id == account_id


def test_get_account(engine, world):
    account = world.account()
    with engine.connect() as conn:
        info = get_account(conn, username=account.username.upper())
        assert info == admin_auth.AccountInfo(
            account_id=account.account_id,
            tenant_id=account.tenant_id,
            credential_id=account.credential_id,
            username=account.username,
            disabled=False,
        )
        assert get_account(conn, username=new_username()) is None
        disable_account(conn, username=account.username, now=NOW)
        assert get_account(conn, username=account.username).disabled is True
    with pytest.raises(FrozenInstanceError):
        info.disabled = True


@pytest.mark.parametrize("username", ["kélvin", "ab", "kelvin\n", None])
def test_get_account_with_an_invalid_username_does_not_query(engine, world, username):
    with engine.connect() as conn:
        statements = record_sql(conn)
        assert get_account(conn, username=username) is None
    assert statements == []


def test_account_exists(engine, world):
    account = world.account()
    with engine.connect() as conn:
        assert account_exists(conn, username=account.username)
        assert account_exists(conn, username=account.username.upper())
        assert not account_exists(conn, username=new_username())


@pytest.mark.parametrize("tenant", ["missing", "inactive"])
def test_create_account_needs_an_active_tenant(engine, world, tenant):
    tenant_id = new_id("ten") if tenant == "missing" else world.tenant("inactive")
    with engine.connect() as conn:
        code = account_error(
            create_account,
            conn,
            tenant_id=tenant_id,
            username=new_username(),
            password=PASSWORD,
            now=NOW,
        )
    assert code == "invalid_tenant"
    assert credentials_of(engine, tenant_id) == []


def test_create_checks_the_username_before_the_tenant(engine, world):
    with engine.connect() as conn:
        statements = record_sql(conn)
        code = account_error(
            create_account, conn, tenant_id=new_id("ten"), username="kélvin", password="x", now=NOW
        )
    assert code == "invalid_username"
    assert statements == []


def test_create_checks_the_tenant_before_the_account(engine, world):
    account = world.account()
    for tenant_id in (new_id("ten"), world.tenant("inactive")):
        with engine.connect() as conn:
            code = account_error(
                create_account,
                conn,
                tenant_id=tenant_id,
                username=account.username,
                password=PASSWORD,
                now=NOW,
            )
        assert code == "invalid_tenant"


def test_create_checks_the_account_before_the_password(engine, world):
    account = world.account()
    with engine.connect() as conn:
        code = account_error(
            create_account,
            conn,
            tenant_id=world.tenant_id,
            username=account.username.upper(),
            password="short",
            now=NOW,
        )
    assert code == "already_exists"


def weak_passwords(username: str) -> list[str]:
    return ["a" * 11, "a" * 129, username.upper(), "abcdefghijk\ud800"]


def test_create_rejects_weak_passwords_and_writes_nothing(engine, world, caplog):
    username = new_username()
    for password in weak_passwords(username):
        with engine.connect() as conn:
            with pytest.raises(AccountError) as caught:
                create_account(
                    conn, tenant_id=world.tenant_id, username=username, password=password, now=NOW
                )
            assert not conn.in_transaction()
        assert caught.value.code == "weak_password"
        assert password not in str(caught.value)
    assert credentials_of(engine, world.tenant_id) == []
    with engine.connect() as conn:
        assert not account_exists(conn, username=username)


def test_a_unique_violation_on_create_becomes_already_exists(engine, world, monkeypatch):
    account = world.account()
    # 模拟并发：预检看不到同名账号，插入撞上 uq_admin_accounts_username。
    monkeypatch.setattr(admin_auth, "_find_account", lambda connection, username: None)
    with engine.connect() as conn:
        code = account_error(
            create_account,
            conn,
            tenant_id=world.tenant_id,
            username=account.username,
            password=PASSWORD,
            now=NOW,
        )
        assert not conn.in_transaction()
    assert code == "already_exists"
    # 回滚了：没有多出主体。
    assert len(credentials_of(engine, world.tenant_id)) == 1


@pytest.mark.parametrize("username", ["kélvin", "ab", "kelvin\n", ".kelvin", None])
def test_every_account_function_rejects_an_invalid_username_without_querying(
    engine, world, username
):
    calls = [
        lambda conn: create_account(
            conn, tenant_id=world.tenant_id, username=username, password=PASSWORD, now=NOW
        ),
        lambda conn: reset_password(conn, username=username, password=PASSWORD, now=NOW),
        lambda conn: disable_account(conn, username=username, now=NOW),
        lambda conn: enable_account(conn, username=username),
        lambda conn: account_exists(conn, username=username),
    ]
    for call in calls:
        with engine.connect() as conn:
            statements = record_sql(conn)
            assert account_error(call, conn) == "invalid_username"
        assert statements == []


def test_missing_accounts_are_not_found(engine, world):
    username = new_username()
    with engine.connect() as conn:
        codes = [
            # 账号存在与否先于密码规则。
            account_error(reset_password, conn, username=username, password="short", now=NOW),
            account_error(disable_account, conn, username=username, now=NOW),
            account_error(enable_account, conn, username=username),
        ]
    assert codes == ["not_found"] * 3


def test_reset_password_rejects_weak_passwords_and_keeps_sessions(engine, world):
    account = world.account()
    result = login(engine, world.source(), account.username)
    before = account_row(engine, account)
    for password in weak_passwords(account.username):
        with engine.connect() as conn:
            code = account_error(
                reset_password, conn, username=account.username, password=password, now=NOW
            )
        assert code == "weak_password"
    assert account_row(engine, account) == before
    assert resolve(engine, result.token, NOW) is not None


def test_disable_and_enable_report_when_nothing_changes(engine, world):
    account = world.account()
    with engine.connect() as conn:
        assert enable_account(conn, username=account.username) is False
        assert disable_account(conn, username=account.username, now=at(9, 0)) is True
        assert disable_account(conn, username=account.username, now=at(10, 0)) is False
        assert not conn.in_transaction()
    assert account_row(engine, account)["disabled_at"] == at(9, 0)


# --- 时间 ---


def test_utc_now_is_naive_and_whole_seconds():
    before = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    now = utc_now()
    assert now.tzinfo is None
    assert now.microsecond == 0
    assert before <= now <= datetime.now(UTC).replace(tzinfo=None)


@pytest.mark.parametrize("bad_now", [NOW.replace(tzinfo=UTC), NOW.replace(microsecond=500000)])
def test_functions_that_take_now_reject_aware_or_fractional_times(engine, world, bad_now):
    account = world.account()
    result = login(engine, world.source(), account.username)
    source, username = world.source(), new_username()
    attempts = attempt_count(engine)
    calls = [
        lambda conn: authenticate(
            conn, source=source, username=account.username, password=PASSWORD, now=bad_now
        ),
        lambda conn: resolve_session(conn, token=result.token, now=bad_now),
        lambda conn: revoke_session(conn, session_id=result.session_id, now=bad_now),
        lambda conn: create_account(
            conn, tenant_id=world.tenant_id, username=username, password=PASSWORD, now=bad_now
        ),
        lambda conn: reset_password(
            conn, username=account.username, password=NEW_PASSWORD, now=bad_now
        ),
        lambda conn: disable_account(conn, username=account.username, now=bad_now),
    ]
    for call in calls:
        with engine.connect() as conn, pytest.raises(ValueError):
            call(conn)
    assert attempt_count(engine) == attempts
    assert len(credentials_of(engine, world.tenant_id)) == 1
    assert account_row(engine, account)["disabled_at"] is None
    assert resolve(engine, result.token, NOW) is not None


# --- 结果、出错与机密 ---


def test_login_result_is_immutable_and_its_repr_has_no_token(engine, world):
    account = world.account()
    result = login(engine, world.source(), account.username)
    assert result.token not in repr(result)
    assert "token" not in repr(result)
    with pytest.raises(FrozenInstanceError):
        result.outcome = "locked"
    assert INVALID.account_id is None and INVALID.token is None and INVALID.expires_at is None


def test_a_database_error_rolls_back_and_is_raised(engine, world, monkeypatch):
    account = world.account()
    first = login(engine, world.source(), account.username)
    attempts = attempt_count(engine)
    # 新会话的令牌撞上已有会话的 token_hash：建会话时 IntegrityError。
    monkeypatch.setattr(admin_auth, "_new_token", lambda: first.token)
    with engine.connect() as conn:
        with pytest.raises(IntegrityError) as caught:
            authenticate(
                conn, source=world.source(), username=account.username, password=PASSWORD, now=NOW
            )
        assert not conn.in_transaction()
    assert attempt_count(engine) == attempts
    assert len(sessions_of(engine, account)) == 1
    assert first.token not in str(caught.value)
    assert PASSWORD not in str(caught.value)


def test_secrets_do_not_reach_logs_or_error_messages(engine, world, caplog):
    caplog.set_level(logging.DEBUG)
    # SQL 语句照常记；DEBUG 会记结果行（含哈希），所以 SQLAlchemy 只开到 INFO。
    caplog.set_level(logging.INFO, logger="sqlalchemy.engine")
    account = world.account()
    source = world.source()
    result = login(engine, source, account.username)
    login(engine, source, account.username, WRONG_PASSWORD)
    resolve(engine, result.token, NOW)
    csrf = csrf_token_for(result.token)
    with engine.connect() as conn:
        reset_password(conn, username=account.username, password=NEW_PASSWORD, now=NOW)
        with pytest.raises(AccountError) as caught:
            reset_password(conn, username=account.username, password=account.username, now=NOW)

    password_hash = account_row(engine, account)["password_hash"]
    throttle, source_hash = keys_of(source, account.username)
    secrets_used = [
        PASSWORD,
        NEW_PASSWORD,
        WRONG_PASSWORD,
        result.token,
        csrf,
        password_hash,
        hashlib.sha256(result.token.encode()).hexdigest(),
        throttle,
        source_hash,
    ]
    assert caplog.text
    for secret in secrets_used:
        assert secret not in caplog.text
        assert secret not in str(caught.value)


# --- 并发（4.4） ---


def test_disable_waits_for_a_locked_account_row_and_then_revokes_every_session(engine, world):
    account = world.account()
    old = login(engine, world.source(), account.username, now=at(8, 0))
    new = new_token()

    def disable() -> bool:
        with engine.connect() as conn:
            return disable_account(conn, username=account.username, now=at(9, 0))

    with ThreadPoolExecutor(max_workers=1) as pool, engine.connect() as holder:
        try:
            # 像一次进行中的登录：锁住账号行，在同一事务里建会话，还没提交。
            locked = (
                select(AdminAccount.admin_account_id)
                .where(AdminAccount.username == account.username)
                .with_for_update()
            )
            assert holder.execute(locked).scalar_one() == account.account_id
            insert_session(holder, account, new, created_at=at(8, 59))
            future = pool.submit(disable)
            # 停用在账号行上等着这个连接提交。
            time.sleep(1)
            assert not future.done()
            holder.commit()
            assert future.result(timeout=30) is True
        finally:
            holder.rollback()

    sessions = sessions_of(engine, account)
    assert len(sessions) == 2
    assert all(session["revoked_at"] == at(9, 0) for session in sessions)
    assert resolve(engine, old.token, at(9, 0)) is None
    assert resolve(engine, new, at(9, 0)) is None
