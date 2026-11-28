"""后台账号、会话与登录尝试各表的约束在真 MySQL 上生效（docs/ADMIN_AUTH.md 第 3 节）。

alembic check 不比对 CHECK 约束，所以对迁移建出的表测（conftest 的 migrated_engine），
不用 create_all。例子 D1–D9 与安全事件用 db 夹具（每个测试一个事务、结束时回滚）；
租户、admin 主体与账号由测试自己插入。
0018 的升降照 test_demo_reset_schema 的先例在 production 库上做，不动共享的 demo 库；
无论成败都在 finally 里升回 head，提交进去的行也在 finally 里删掉。
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import Connection, Engine, delete, insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql.expression import Executable

from alembic import command
from app.db import make_engine
from app.domain.environment import PRODUCTION
from app.domain.service_auth import ADMIN, generate_token, token_digest
from app.models.admin_auth import AdminAccount, AdminLoginAttempt, AdminSession
from app.models.service_auth import SecurityEvent
from tests.integration.conftest import (
    TEST_DATABASE_URL,
    add_credential,
    add_tenant,
    assert_rejected_by,
    new_id,
)
from tests.integration.test_demo_reset_schema import (
    alembic_version,
    has_table,
    head_revision,
    migrate_production,
    rolled_back,
)
from tests.integration.test_service_auth_schema import event_insert

CREATED_AT = datetime(2026, 11, 24, 9, 0)
EXPIRES_AT = CREATED_AT + timedelta(hours=12)
KEY = "0" * 64
OUTCOMES = ("success", "bad_password", "unknown_user", "disabled", "locked")
ADMIN_EVENT_TYPES = (
    "admin_account_created",
    "admin_password_reset",
    "admin_account_disabled",
    "admin_account_enabled",
)
ADMIN_TABLES = ("admin_accounts", "admin_sessions", "admin_login_attempts")

USERNAME_UNIQUE = "uq_admin_accounts_username"
USERNAME_LENGTH = "ck_admin_accounts_username_length"
CREDENTIAL_UNIQUE = "uq_admin_accounts_credential_id"
ACCOUNT_CREDENTIAL_FK = "fk_admin_accounts_tenant_id_credentials"
TOKEN_HASH_UNIQUE = "uq_admin_sessions_token_hash"
TOKEN_HASH_CHECK = "ck_admin_sessions_token_hash"
EXPIRES_CHECK = "ck_admin_sessions_expires_after_created"
SESSION_ACCOUNT_FK = "fk_admin_sessions_tenant_id_admin_accounts"
ATTEMPT_ACCOUNT_FK = "fk_admin_login_attempts_tenant_id_admin_accounts"
THROTTLE_KEY_CHECK = "ck_admin_login_attempts_throttle_key_length"
SOURCE_KEY_CHECK = "ck_admin_login_attempts_source_key_length"
ACCOUNT_TENANT_CHECK = "ck_admin_login_attempts_account_tenant"
OUTCOME_CHECK = "ck_admin_login_attempts_outcome_known"
EVENT_TYPE_CHECK = "ck_security_events_event_type"


@dataclass
class Account:
    """一个账号、它的租户与它指向的 admin 主体。"""

    tenant_id: str
    credential_id: str
    admin_account_id: str
    username: str


def new_username() -> str:
    return f"adm{uuid4().hex[:12]}"


def add_principal(db: Connection, tenant_id: str) -> str:
    """建号的服务以后建的 admin 主体：kind 为 admin、scopes 为空数组。"""
    return add_credential(db, tenant_id, kind=ADMIN, scopes=[])


def account_insert(tenant_id: str, credential_id: str, **overrides: object) -> Executable:
    values = {
        "admin_account_id": new_id("adm"),
        "tenant_id": tenant_id,
        "credential_id": credential_id,
        "username": new_username(),
        "password_hash": "scrypt$placeholder",
        "created_at": CREATED_AT,
        "password_updated_at": CREATED_AT,
    }
    return insert(AdminAccount).values(values | overrides)


def add_account(db: Connection, tenant_id: str | None = None, username: str = "") -> Account:
    tenant_id = tenant_id or add_tenant(db)
    credential_id = add_principal(db, tenant_id)
    account = Account(tenant_id, credential_id, new_id("adm"), username or new_username())
    db.execute(
        account_insert(
            tenant_id,
            credential_id,
            admin_account_id=account.admin_account_id,
            username=account.username,
        )
    )
    return account


def session_insert(account: Account, **overrides: object) -> Executable:
    values = {
        "tenant_id": account.tenant_id,
        "admin_account_id": account.admin_account_id,
        "token_hash": token_digest(generate_token()),
        "created_at": CREATED_AT,
        "expires_at": EXPIRES_AT,
    }
    return insert(AdminSession).values(values | overrides)


def attempt_insert(**overrides: object) -> Executable:
    """默认是一次认不出账号的尝试：tenant_id 与 admin_account_id 为空。"""
    values = {
        "occurred_at": CREATED_AT,
        "throttle_key": KEY,
        "source_key": KEY,
        "outcome": "unknown_user",
    }
    return insert(AdminLoginAttempt).values(values | overrides)


def account_attempt(account: Account, **overrides: object) -> Executable:
    values = {
        "tenant_id": account.tenant_id,
        "admin_account_id": account.admin_account_id,
        "outcome": "success",
    }
    return attempt_insert(**(values | overrides))


def delete_account(account: Account) -> Executable:
    return delete(AdminAccount).where(AdminAccount.admin_account_id == account.admin_account_id)


# 约束的例子（设计第 3 节）


def test_d1_username_is_unique(db: Connection):
    first = add_account(db, username="kelvin")
    principal = add_principal(db, first.tenant_id)
    assert_rejected_by(
        db, account_insert(first.tenant_id, principal, username="kelvin"), USERNAME_UNIQUE
    )


def test_d2_username_has_3_to_32_characters(db: Connection):
    tenant = add_tenant(db)
    for username in ("ab", "a", ""):
        statement = account_insert(tenant, add_principal(db, tenant), username=username)
        assert_rejected_by(db, statement, USERNAME_LENGTH)
    add_account(db, tenant, username="abc")
    add_account(db, tenant, username=f"z{uuid4().hex[:31]}")


def test_d3_token_hash_is_64_lowercase_hex_characters(db: Connection):
    account = add_account(db)
    for token_hash in ("0" * 63, "0" * 63 + "A", "A" * 64, "0" * 63 + "g", "g" * 64, "-" * 64):
        assert_rejected_by(db, session_insert(account, token_hash=token_hash), TOKEN_HASH_CHECK)
    db.execute(session_insert(account))


def test_token_hash_is_unique_across_tenants(db: Connection):
    account, other = add_account(db), add_account(db)
    token_hash = token_digest(generate_token())
    db.execute(session_insert(account, token_hash=token_hash))
    for owner in (account, other):
        assert_rejected_by(db, session_insert(owner, token_hash=token_hash), TOKEN_HASH_UNIQUE)


def test_d4_session_expires_after_it_is_created(db: Connection):
    account = add_account(db)
    for expires_at in (CREATED_AT, CREATED_AT - timedelta(seconds=1)):
        assert_rejected_by(db, session_insert(account, expires_at=expires_at), EXPIRES_CHECK)
    db.execute(session_insert(account, expires_at=CREATED_AT + timedelta(seconds=1)))


def test_d5_attempt_outcome_and_key_lengths(db: Connection):
    assert_rejected_by(db, attempt_insert(outcome="ok"), OUTCOME_CHECK)
    assert_rejected_by(db, attempt_insert(source_key="0" * 63), SOURCE_KEY_CHECK)
    assert_rejected_by(db, attempt_insert(throttle_key="0" * 63), THROTTLE_KEY_CHECK)
    for outcome in OUTCOMES:
        db.execute(attempt_insert(outcome=outcome))


@pytest.mark.parametrize(
    ("child", "constraint"),
    [(session_insert, SESSION_ACCOUNT_FK), (account_attempt, ATTEMPT_ACCOUNT_FK)],
)
def test_d6_account_with_a_session_or_attempt_cannot_be_deleted(
    db: Connection, child: Callable[[Account], Executable], constraint: str
):
    account = add_account(db)
    db.execute(child(account))
    assert_rejected_by(db, delete_account(account), constraint)
    # 没有会话与登录尝试的账号，外键不拦。
    db.execute(delete_account(add_account(db)))


def test_d7_attempt_without_an_account_is_accepted(db: Connection):
    db.execute(attempt_insert())
    db.execute(attempt_insert(outcome="locked"))


def test_d8_sessions_attempts_and_usernames_across_tenants(db: Connection):
    account, other = add_account(db), add_account(db)
    assert_rejected_by(db, session_insert(account, tenant_id=other.tenant_id), SESSION_ACCOUNT_FK)
    assert_rejected_by(db, account_attempt(account, tenant_id=None), ACCOUNT_TENANT_CHECK)
    assert_rejected_by(db, account_attempt(account, tenant_id=other.tenant_id), ATTEMPT_ACCOUNT_FK)
    same_name = account_insert(
        other.tenant_id, add_principal(db, other.tenant_id), username=account.username
    )
    assert_rejected_by(db, same_name, USERNAME_UNIQUE)
    db.execute(session_insert(account))
    db.execute(account_attempt(account))


def test_d9_account_principal_is_unique_and_in_the_same_tenant(db: Connection):
    account = add_account(db)
    assert_rejected_by(
        db, account_insert(account.tenant_id, account.credential_id), CREDENTIAL_UNIQUE
    )
    foreign_principal = add_principal(db, add_tenant(db))
    assert_rejected_by(
        db, account_insert(account.tenant_id, foreign_principal), ACCOUNT_CREDENTIAL_FK
    )


# 安全事件的四种账号操作类型（契约 7.2 的 0.22 一条）


def test_admin_account_event_types_are_accepted(db: Connection):
    account = add_account(db)
    for event_type in ADMIN_EVENT_TYPES:
        db.execute(
            event_insert(
                event_type=event_type,
                tenant_id=account.tenant_id,
                credential_id=account.credential_id,
                actor_ref="ops",
            )
        )


def test_event_types_outside_the_list_are_still_rejected(db: Connection):
    for event_type in ("admin_account_deleted", "admin_login", "ADMIN_ACCOUNT_CREATED", ""):
        assert_rejected_by(db, event_insert(event_type=event_type), EVENT_TYPE_CHECK)


# make_engine 的 hide_parameters（设计第 3 节）


def test_engine_errors_do_not_carry_bound_parameters(migrated_engine: Engine):
    password_hash = f"scrypt$must-not-leak-{uuid4().hex}"
    engine = make_engine(TEST_DATABASE_URL)
    try:
        with engine.connect() as conn:
            conn.begin()
            try:
                account = add_account(conn)
                duplicate = account_insert(
                    account.tenant_id,
                    add_principal(conn, account.tenant_id),
                    username=account.username,
                    password_hash=password_hash,
                )
                with pytest.raises(IntegrityError) as caught, conn.begin_nested():
                    conn.execute(duplicate)
            finally:
                conn.rollback()
    finally:
        engine.dispose()
    # MySQL 自己的报错文本含重复的键值（用户名），所以只断言不在唯一键里的参数。
    assert USERNAME_UNIQUE in str(caught.value)
    assert password_hash not in str(caught.value)


# 0018 的升降（production 库）


def assert_admin_tables(engine: Engine, present: bool) -> None:
    for table in ADMIN_TABLES:
        assert has_table(engine, table) is present


def test_downgrade_to_0017_and_back(production_engine: Engine):
    try:
        migrate_production(command.downgrade, "0017", PRODUCTION)
        assert alembic_version(production_engine) == "0017"
        assert_admin_tables(production_engine, False)
        # 0003 的原约束回来了。
        with rolled_back(production_engine) as conn:
            for event_type in ADMIN_EVENT_TYPES:
                assert_rejected_by(conn, event_insert(event_type=event_type), EVENT_TYPE_CHECK)
            conn.execute(event_insert(event_type="credential_issued"))
    finally:
        migrate_production(command.upgrade, "head", PRODUCTION)
    assert alembic_version(production_engine) == head_revision()
    assert_admin_tables(production_engine, True)
    with rolled_back(production_engine) as conn:
        account = add_account(conn)
        conn.execute(session_insert(account))
        conn.execute(account_attempt(account))
        for event_type in ADMIN_EVENT_TYPES:
            conn.execute(event_insert(event_type=event_type))


def test_downgrade_fails_without_changes_while_admin_account_events_exist(
    production_engine: Engine,
):
    event_id = new_id("sev")
    with production_engine.begin() as conn:
        conn.execute(event_insert(event_id=event_id, event_type="admin_account_created"))
    try:
        with pytest.raises(RuntimeError):
            migrate_production(command.downgrade, "0017", PRODUCTION)
        assert alembic_version(production_engine) == "0018"
        assert_admin_tables(production_engine, True)
        with rolled_back(production_engine) as conn:
            conn.execute(event_insert(event_type="admin_account_enabled"))
    finally:
        with production_engine.begin() as conn:
            conn.execute(delete(SecurityEvent).where(SecurityEvent.event_id == event_id))
        migrate_production(command.upgrade, "head", PRODUCTION)
