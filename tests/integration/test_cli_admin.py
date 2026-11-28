"""运维命令行的管理员账号子命令（docs/ADMIN_AUTH.md 4.6，契约 7.2）。

在真 MySQL 上跑：直接调用入口函数 main（同 test_cli_provisioning），替换 getpass.getpass 提供输入。
账号命令只在 production 实例上可用：每次调用期间把测试库标记为 production，调用结束即改回 demo。
每个测试建自己的租户，已有的账号用服务函数直接建；用户名带随机后缀。
数据都在 finally 里按外键先后删掉（安全事件、会话、账号、主体、租户）：admin 主体挡 0012 的降级，
删掉之后会话结束时 migrated_engine 才能照常降到 base。
"""

import getpass
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

import pytest
from sqlalchemy import Connection, Engine, delete, func, insert, make_url, select

from app.cli import admin
from app.config import Settings
from app.domain.environment import DEMO, PRODUCTION
from app.domain.pw_hash import verify_password
from app.domain.service_auth import ADMIN
from app.models.admin_auth import AdminAccount, AdminLoginAttempt, AdminSession
from app.models.catalog import Tenant
from app.models.service_auth import Credential, SecurityEvent
from app.services import security_events
from app.services.admin_auth import create_account, disable_account
from tests.integration.conftest import TEST_DATABASE_URL, instance_marked, new_id
from tests.integration.test_cli_provisioning import Result, run_cli

PASSWORD = "correct horse battery"
NEW_PASSWORD = "staple paper clip 42"
CREATED_AT = datetime(2026, 11, 24, 7, 0)
ACTOR = "ops-7"
REASON = "new shop operator"
OPERATOR = ("--actor", ACTOR, "--reason", REASON)
LOGGER = "app.services.security_events"
EVENT_TYPES = (
    "admin_account_created",
    "admin_password_reset",
    "admin_account_disabled",
    "admin_account_enabled",
)
HANDLERS = (
    "create_account_command",
    "reset_password_command",
    "disable_account_command",
    "enable_account_command",
)
COMMANDS = ("create-account", "reset-password", "disable-account", "enable-account")


def new_username() -> str:
    return f"ops{uuid4().hex[:12]}"


# --- 造数据与清理 ---


@dataclass
class World:
    engine: Engine
    tenant_id: str = ""
    tenant_ids: list[str] = field(default_factory=list)

    def tenant(self, status: str = "active") -> str:
        tenant_id = new_id("ten")
        with self.engine.begin() as db:
            db.execute(insert(Tenant).values(tenant_id=tenant_id, name="Tenant", status=status))
        self.tenant_ids.append(tenant_id)
        return tenant_id

    def account(self, *, disabled: bool = False) -> str:
        """用服务函数建一个账号（密码为 PASSWORD），返回规范用户名。"""
        username = new_username()
        with self.engine.connect() as conn:
            create_account(
                conn,
                tenant_id=self.tenant_id,
                username=username,
                password=PASSWORD,
                now=CREATED_AT,
            )
            if disabled:
                disable_account(conn, username=username, now=CREATED_AT)
        return username


def cleanup(engine: Engine, world: World) -> None:
    if not world.tenant_ids:
        return
    with engine.begin() as db:
        for model in (SecurityEvent, AdminLoginAttempt, AdminSession, AdminAccount, Credential):
            db.execute(delete(model).where(model.tenant_id.in_(world.tenant_ids)))
        db.execute(delete(Tenant).where(Tenant.tenant_id.in_(world.tenant_ids)))


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


def production_settings(**overrides: object) -> Settings:
    values = {"env": PRODUCTION, "database_url": TEST_DATABASE_URL, "admin_enabled": True}
    return Settings(**(values | overrides))


@pytest.fixture
def cli(engine: Engine) -> Callable[..., Result]:
    """CORE_ENV=production、开关为真的命令行；调用期间库标记为 production（启动核对要求一致）。"""
    settings = production_settings()

    def run(*argv: str) -> Result:
        with instance_marked(engine, PRODUCTION):
            return run_cli(settings, *argv)

    return run


@dataclass
class Prompts:
    """替换 getpass.getpass：依次返回 answers 里的值，值是异常类时抛出它。

    同时记下传给子命令的连接：被调用时断言那个连接不在事务里。
    """

    answers: list[object] = field(default_factory=list)
    calls: int = 0
    connections: list[Connection] = field(default_factory=list)

    def __call__(self, prompt: str = "Password: ", stream: object = None) -> str:
        self.calls += 1
        assert self.connections, "getpass was called outside a subcommand"
        assert not self.connections[-1].in_transaction()
        answer = self.answers.pop(0)
        if isinstance(answer, type) and issubclass(answer, BaseException):
            raise answer()
        return answer


@pytest.fixture
def prompts(monkeypatch) -> Prompts:
    spy = Prompts()
    monkeypatch.setattr(getpass, "getpass", spy)
    for name in HANDLERS:
        original = getattr(admin, name)

        def wrapped(conn, args, original=original):
            spy.connections.append(conn)
            return original(conn, args)

        monkeypatch.setattr(admin, name, wrapped)
    return spy


# --- 命令行参数与读库 ---


def argv_for(command: str, username: str, tenant_id: str, operator=OPERATOR) -> list[str]:
    argv = ["admin", command, "--username", username, *operator]
    if command == "create-account":
        argv += ["--tenant", tenant_id]
    return argv


def account_row(engine: Engine, username: str) -> dict | None:
    statement = select(*AdminAccount.__table__.c).where(AdminAccount.username == username)
    with engine.connect() as conn:
        row = conn.execute(statement).mappings().one_or_none()
    return None if row is None else dict(row)


def credential_row(engine: Engine, credential_id: str) -> dict:
    statement = select(*Credential.__table__.c).where(Credential.credential_id == credential_id)
    with engine.connect() as conn:
        return dict(conn.execute(statement).mappings().one())


def events_of(engine: Engine, tenant_id: str) -> list[dict]:
    with engine.connect() as conn:
        statement = (
            select(*SecurityEvent.__table__.c)
            .where(SecurityEvent.tenant_id == tenant_id)
            .order_by(SecurityEvent.occurred_at, SecurityEvent.event_id)
        )
        return [dict(row) for row in conn.execute(statement).mappings()]


def counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as conn:
        return {
            model.__tablename__: conn.execute(select(func.count()).select_from(model)).scalar_one()
            for model in (Tenant, Credential, AdminAccount, AdminSession, SecurityEvent)
        }


def snapshot(engine: Engine, world: World) -> tuple:
    """拒绝前后要相同的：各表行数、本租户的账号与安全事件。"""
    with engine.connect() as conn:
        accounts = conn.execute(
            select(*AdminAccount.__table__.c)
            .where(AdminAccount.tenant_id == world.tenant_id)
            .order_by(AdminAccount.admin_account_id)
        ).all()
    return counts(engine), accounts, events_of(engine, world.tenant_id)


def assert_success(result: Result, username: str, outcome: str) -> dict:
    payload = result.json()
    assert payload["username"] == username
    assert payload["result"] == outcome
    assert payload["admin_account_id"].startswith("adm_")
    assert_no_secrets(result)
    return payload


def assert_no_secrets(result: Result) -> None:
    for secret in (PASSWORD, NEW_PASSWORD, "scrypt", TEST_DATABASE_URL):
        assert secret not in result.out
        assert secret not in result.err


def assert_event(engine: Engine, world: World, event_type: str, payload: dict) -> None:
    account = account_row(engine, payload["username"])
    [event] = [e for e in events_of(engine, world.tenant_id) if e["event_type"] == event_type]
    assert event["tenant_id"] == world.tenant_id
    assert event["credential_id"] == account["credential_id"]
    assert event["actor_ref"] == ACTOR
    assert event["details"] == {
        "reason": REASON,
        "admin_account_id": payload["admin_account_id"],
        "credential_id": account["credential_id"],
        "username": payload["username"],
    }
    assert PASSWORD not in str(event) and NEW_PASSWORD not in str(event)


# --- create-account ---


def test_create_account_prompts_twice_creates_the_account_and_records_an_event(
    cli, engine, world, prompts
):
    username = new_username()
    prompts.answers = [PASSWORD, PASSWORD]

    result = cli(*argv_for("create-account", username.upper(), world.tenant_id))

    payload = assert_success(result, username, "created")
    assert prompts.calls == 2
    account = account_row(engine, username)
    assert account["admin_account_id"] == payload["admin_account_id"]
    assert account["tenant_id"] == world.tenant_id
    assert account["disabled_at"] is None
    assert verify_password(PASSWORD, account["password_hash"])
    assert account["password_hash"] not in result.out
    principal = credential_row(engine, account["credential_id"])
    assert principal["kind"] == ADMIN
    assert principal["name"] == username
    assert_event(engine, world, "admin_account_created", payload)


def test_create_account_rejects_an_existing_username_before_prompting(cli, engine, world, prompts):
    username = world.account()
    before = snapshot(engine, world)

    result = cli(*argv_for("create-account", username.upper(), world.tenant_id))

    assert result.code == 2
    result.one_error_line()
    assert prompts.calls == 0
    assert snapshot(engine, world) == before


@pytest.mark.parametrize("tenant", ["inactive", "unknown"])
def test_create_account_rejects_a_tenant_that_is_not_active(tenant, cli, engine, world, prompts):
    tenant_id = world.tenant(status="inactive") if tenant == "inactive" else new_id("ten")
    prompts.answers = [PASSWORD, PASSWORD]
    before = counts(engine)

    result = cli(*argv_for("create-account", new_username(), tenant_id))

    assert result.code == 2
    result.one_error_line()
    assert counts(engine) == before


def test_create_account_rejects_a_weak_password(cli, engine, world, prompts):
    username = new_username()
    prompts.answers = ["short", "short"]
    before = snapshot(engine, world)

    result = cli(*argv_for("create-account", username, world.tenant_id))

    assert result.code == 2
    result.one_error_line()
    assert "short" not in result.err
    assert snapshot(engine, world) == before


# --- reset-password ---


def test_reset_password_replaces_the_hash_and_records_an_event(cli, engine, world, prompts):
    username = world.account()
    prompts.answers = [NEW_PASSWORD, NEW_PASSWORD]

    result = cli(*argv_for("reset-password", username.upper(), world.tenant_id))

    payload = assert_success(result, username, "password_reset")
    assert prompts.calls == 2
    account = account_row(engine, username)
    assert verify_password(NEW_PASSWORD, account["password_hash"])
    assert not verify_password(PASSWORD, account["password_hash"])
    assert_event(engine, world, "admin_password_reset", payload)


def test_reset_password_rejects_an_unknown_account_before_prompting(cli, engine, world, prompts):
    before = snapshot(engine, world)

    result = cli(*argv_for("reset-password", new_username(), world.tenant_id))

    assert result.code == 2
    result.one_error_line()
    assert prompts.calls == 0
    assert snapshot(engine, world) == before


# --- disable-account 与 enable-account ---


def test_disable_account_sets_disabled_and_records_an_event(cli, engine, world, prompts):
    username = world.account()

    result = cli(*argv_for("disable-account", username.upper(), world.tenant_id))

    payload = assert_success(result, username, "disabled")
    assert prompts.calls == 0
    assert account_row(engine, username)["disabled_at"] is not None
    assert_event(engine, world, "admin_account_disabled", payload)


def test_disabling_a_disabled_account_succeeds_without_an_event(cli, engine, world, prompts):
    username = world.account(disabled=True)
    before = snapshot(engine, world)

    result = cli(*argv_for("disable-account", username, world.tenant_id))

    assert_success(result, username, "already_disabled")
    assert snapshot(engine, world) == before


def test_enable_account_clears_disabled_and_records_an_event(cli, engine, world, prompts):
    username = world.account(disabled=True)

    result = cli(*argv_for("enable-account", username.upper(), world.tenant_id))

    payload = assert_success(result, username, "enabled")
    assert prompts.calls == 0
    assert account_row(engine, username)["disabled_at"] is None
    assert_event(engine, world, "admin_account_enabled", payload)


def test_enabling_an_enabled_account_succeeds_without_an_event(cli, engine, world, prompts):
    username = world.account()
    before = snapshot(engine, world)

    result = cli(*argv_for("enable-account", username, world.tenant_id))

    assert_success(result, username, "already_enabled")
    assert snapshot(engine, world) == before


@pytest.mark.parametrize("command", ["disable-account", "enable-account"])
def test_disable_and_enable_reject_an_unknown_account(command, cli, engine, world, prompts):
    before = snapshot(engine, world)

    result = cli(*argv_for(command, new_username(), world.tenant_id))

    assert result.code == 2
    result.one_error_line()
    assert snapshot(engine, world) == before


# --- 实例与开关、参数校验：都在连库之前 ---


def unreachable_url() -> str:
    # 同一台 MySQL 的一个没人监听的端口。
    return make_url(TEST_DATABASE_URL).set(port=1).render_as_string(hide_password=False)


UNAVAILABLE = {
    "demo": {"env": DEMO, "admin_enabled": False},
    "demo with the switch on": {"env": DEMO, "admin_enabled": True},
    "production with the switch off": {"env": PRODUCTION, "admin_enabled": False},
}


@pytest.mark.parametrize("database", ["test database", "unreachable database"])
@pytest.mark.parametrize("instance", list(UNAVAILABLE))
@pytest.mark.parametrize("command", COMMANDS)
def test_commands_are_unavailable_outside_production_with_the_switch_on(
    command, instance, database, engine, world, prompts
):
    username = world.account(disabled=command == "enable-account")
    url = TEST_DATABASE_URL if database == "test database" else unreachable_url()
    settings = production_settings(database_url=url, **UNAVAILABLE[instance])
    prompts.answers = [NEW_PASSWORD, NEW_PASSWORD]
    before = snapshot(engine, world)

    # 库标记成 production：拒绝不是出自启动核对。
    with instance_marked(engine, PRODUCTION):
        result = run_cli(settings, *argv_for(command, username, world.tenant_id))

    assert result.code == 2
    result.one_error_line()
    assert url not in result.err
    assert prompts.calls == 0
    assert prompts.connections == []
    assert snapshot(engine, world) == before


INVALID_ARGUMENTS = {
    "invalid username": ("x!", OPERATOR),
    "username too short": ("ab", OPERATOR),
    "actor with an email": ("valid-name", ("--actor", "a@b.c", "--reason", REASON)),
    "blank reason": ("valid-name", ("--actor", ACTOR, "--reason", "  ")),
}


@pytest.mark.parametrize("case", list(INVALID_ARGUMENTS))
@pytest.mark.parametrize("command", COMMANDS)
def test_invalid_arguments_exit_2_before_connecting(command, case, engine, world, prompts):
    username, operator = INVALID_ARGUMENTS[case]
    settings = production_settings(database_url=unreachable_url())

    result = run_cli(settings, *argv_for(command, username, world.tenant_id, operator))

    assert result.code == 2
    result.one_error_line()
    assert prompts.connections == []


def test_missing_arguments_exit_2(engine, world, prompts):
    settings = production_settings(database_url=unreachable_url())
    for argv in (
        ["admin"],
        ["admin", "create-account", "--username", "valid-name", *OPERATOR],
        ["admin", "disable-account", "--username", "valid-name"],
        ["admin", "reset-password", "--username", "valid-name", "--password", PASSWORD, *OPERATOR],
    ):
        result = run_cli(settings, *argv)
        assert result.code == 2
        result.one_error_line()
        assert PASSWORD not in result.err
    assert prompts.connections == []


def test_an_unreachable_database_exits_1_without_the_connection_string(engine, world, prompts):
    url = unreachable_url()
    settings = production_settings(database_url=url)

    result = run_cli(settings, *argv_for("disable-account", new_username(), world.tenant_id))

    assert result.code == 1
    result.one_error_line()
    assert url not in result.err
    assert prompts.connections == []


def test_an_instance_mismatch_exits_1_without_running_the_command(engine, world, prompts):
    username = world.account()
    before = snapshot(engine, world)

    # 库仍标记为 demo。
    result = run_cli(production_settings(), *argv_for("disable-account", username, ""))

    assert result.code == 1
    result.one_error_line()
    assert prompts.connections == []
    assert snapshot(engine, world) == before


def test_an_empty_database_url_exits_1(engine, world, prompts):
    result = run_cli(
        production_settings(database_url=""), *argv_for("disable-account", "valid-name", "")
    )

    assert result.code == 1
    result.one_error_line()


# --- 输入密码 ---


CANCELLED_INPUTS = {
    "mismatch": [NEW_PASSWORD, NEW_PASSWORD + "x"],
    "eof on the first": [EOFError],
    "eof on the second": [NEW_PASSWORD, EOFError],
    "ctrl-c": [KeyboardInterrupt],
}


@pytest.mark.parametrize("inputs", list(CANCELLED_INPUTS))
@pytest.mark.parametrize("command", ["create-account", "reset-password"])
def test_mismatched_or_cancelled_input_exits_2_and_writes_nothing(
    command, inputs, cli, engine, world, prompts
):
    username = new_username() if command == "create-account" else world.account()
    prompts.answers = list(CANCELLED_INPUTS[inputs])
    before = snapshot(engine, world)

    result = cli(*argv_for(command, username, world.tenant_id))

    assert result.code == 2
    result.one_error_line()
    assert_no_secrets(result)
    assert prompts.calls >= 1
    assert snapshot(engine, world) == before


# --- 安全事件写入失败 ---


@pytest.mark.parametrize("command", COMMANDS)
def test_a_failed_event_write_keeps_the_change_and_warns_in_one_line(
    command, cli, engine, world, prompts, monkeypatch, caplog
):
    caplog.set_level(logging.ERROR, logger=LOGGER)
    if command == "create-account":
        username = new_username()
    else:
        username = world.account(disabled=command == "enable-account")
    prompts.answers = [NEW_PASSWORD, NEW_PASSWORD]

    def broken_insert(connection, values):
        raise RuntimeError("disk full")

    monkeypatch.setattr(security_events, "_insert", broken_insert)
    result = cli(*argv_for(command, username, world.tenant_id))

    assert result.code == 0
    assert result.out.count("\n") == 1
    assert username in result.out
    assert result.err.count("\n") == 1
    assert result.err.startswith("warning: ")
    for marker in ("Traceback", "disk full", PASSWORD, NEW_PASSWORD):
        assert marker not in result.err
    account = account_row(engine, username)
    assert account is not None
    if command == "reset-password":
        assert verify_password(NEW_PASSWORD, account["password_hash"])
    if command == "disable-account":
        assert account["disabled_at"] is not None
    if command == "enable-account":
        assert account["disabled_at"] is None
    assert [e for e in events_of(engine, world.tenant_id) if e["event_type"] in EVENT_TYPES] == []
