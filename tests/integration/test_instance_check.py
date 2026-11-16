"""启动核对：API 与运维命令行在开始处理请求或命令之前比对库里的实例标记与 CORE_ENV（契约 6.1）。

在真 MySQL 上跑。改标记的测试用 conftest 的 instance_marked（退出时改回 demo），
删掉标记行的测试退出时把它写回去，所以之后的测试照常以 demo 通过核对。
"""

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, func, insert, make_url, select

from app.config import Settings
from app.domain.environment import DEMO, PRODUCTION
from app.main import create_app
from app.models.catalog import Tenant
from app.models.instance import CoreInstance
from app.models.service_auth import Credential, CredentialToken, SecurityEvent
from app.services.instance import read_instance_marker
from tests.integration.conftest import TEST_DATABASE_URL, instance_marked
from tests.integration.test_cli_provisioning import create, provision, run_cli
from tests.integration.test_cli_service_auth import issue_argv

TABLES = (Tenant, Credential, CredentialToken, SecurityEvent)


@contextmanager
def instance_row_missing(engine: Engine) -> Iterator[None]:
    """core_instance 有表无行：删掉那一行并提交，退出时以 demo 写回。"""
    with engine.begin() as conn:
        conn.execute(delete(CoreInstance))
    try:
        yield
    finally:
        with engine.begin() as conn:
            conn.execute(insert(CoreInstance).values(singleton=1, core_env=DEMO))


def unreachable_url() -> str:
    """同一台 MySQL 的一个没人监听的端口，连同密码原样渲染。"""
    return make_url(TEST_DATABASE_URL).set(port=1).render_as_string(hide_password=False)


def counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as conn:
        return {
            model.__tablename__: conn.execute(select(func.count()).select_from(model)).scalar_one()
            for model in TABLES
        }


def marker(engine: Engine) -> str | None:
    with engine.connect() as conn:
        return read_instance_marker(conn)


def assert_one_line_without_secrets(err: str, *urls: str) -> str:
    assert err.count("\n") == 1 and err.endswith("\n")
    assert "Traceback" not in err
    for url in (TEST_DATABASE_URL, *urls):
        if not url:
            continue
        assert url not in err
        password = make_url(url).password
        assert not password or str(password) not in err
    return err


# --- 读标记 ---


def test_read_instance_marker_returns_the_row_or_none(migrated_engine):
    assert marker(migrated_engine) == DEMO
    with instance_marked(migrated_engine, PRODUCTION):
        assert marker(migrated_engine) == PRODUCTION
    assert marker(migrated_engine) == DEMO
    with instance_row_missing(migrated_engine):
        assert marker(migrated_engine) is None
    assert marker(migrated_engine) == DEMO


# --- API ---


def api_settings(env: str = DEMO, database_url: str = TEST_DATABASE_URL) -> Settings:
    return Settings(env=env, git_sha="e" * 40, database_url=database_url)


@pytest.mark.parametrize("env", [DEMO, PRODUCTION])
def test_the_app_starts_when_the_marker_matches_core_env(migrated_engine, env):
    with instance_marked(migrated_engine, env):
        app = create_app(api_settings(env))
    try:
        assert app.state.engine is not None
        assert TestClient(app).get("/healthz").json()["env"] == env
    finally:
        app.state.engine.dispose()
    assert marker(migrated_engine) == DEMO


def test_the_app_refuses_to_start_on_a_production_marker_with_demo_config(migrated_engine, capsys):
    with instance_marked(migrated_engine, PRODUCTION), pytest.raises(SystemExit) as exited:
        create_app(api_settings(DEMO))
    assert exited.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    line = assert_one_line_without_secrets(captured.err)
    assert PRODUCTION in line and DEMO in line
    assert marker(migrated_engine) == DEMO


def test_the_app_refuses_to_start_when_core_instance_has_no_row(migrated_engine, capsys):
    with instance_row_missing(migrated_engine), pytest.raises(SystemExit) as exited:
        create_app(api_settings(DEMO))
    assert exited.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert_one_line_without_secrets(captured.err)
    assert marker(migrated_engine) == DEMO


def test_the_app_refuses_to_start_without_a_database_url(capsys):
    with pytest.raises(SystemExit) as exited:
        create_app(api_settings(database_url=""))
    assert exited.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert_one_line_without_secrets(captured.err)


def test_the_app_refuses_to_start_when_the_database_is_unreachable(migrated_engine, capsys):
    url = unreachable_url()
    with pytest.raises(SystemExit) as exited:
        create_app(api_settings(database_url=url))
    assert exited.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert_one_line_without_secrets(captured.err, url)


# --- 运维命令行 ---


def test_the_cli_refuses_a_command_when_the_marker_differs_from_core_env(migrated_engine):
    demo = Settings(env=DEMO, database_url=TEST_DATABASE_URL)
    shop = provision(lambda *argv: run_cli(demo, *argv))
    # 改变权限的命令：执行了就会写安全事件。
    issue = issue_argv(shop.tenant_id, channel=shop.channel_id)
    before = counts(migrated_engine)
    with instance_marked(migrated_engine, PRODUCTION):
        results = [run_cli(demo, *argv) for argv in (create("tenant", "Shop"), issue)]
    for result in results:
        assert result.code == 1
        line = assert_one_line_without_secrets(result.err)
        assert result.out == ""
        assert PRODUCTION in line and DEMO in line
    # 命令没有执行，也没有写安全事件。
    assert counts(migrated_engine) == before

    # 标记改回 demo 之后同样的命令照常执行。
    assert marker(migrated_engine) == DEMO
    assert run_cli(demo, *issue).json()["tenant_id"] == shop.tenant_id


def test_the_cli_refuses_a_command_when_core_instance_has_no_row(migrated_engine):
    demo = Settings(env=DEMO, database_url=TEST_DATABASE_URL)
    before = counts(migrated_engine)
    with instance_row_missing(migrated_engine):
        result = run_cli(demo, *create("tenant", "Shop"))
    assert result.code == 1
    assert result.out == ""
    assert_one_line_without_secrets(result.err)
    assert counts(migrated_engine) == before


def test_help_and_argument_errors_do_not_touch_the_database(migrated_engine):
    url = unreachable_url()
    with instance_marked(migrated_engine, PRODUCTION):
        for database_url in (TEST_DATABASE_URL, url, ""):
            settings = Settings(env=DEMO, database_url=database_url)
            helped = run_cli(settings, "--help")
            assert helped.code == 0
            assert helped.err == ""
            assert "tenant" in helped.out
            wrong = run_cli(settings, "tenant", "create")
            assert wrong.code == 2
            assert "invalid arguments" in assert_one_line_without_secrets(wrong.err, url)
    assert marker(migrated_engine) == DEMO
