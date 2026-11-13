"""运维命令行的开通子命令（契约第 1 节、第 3 节、第 10 节第 4 条）。

在真 MySQL 上跑：直接调用入口函数 main，传入测试库的 Settings 与两个内存输出流。
命令行只能建、不能停用，所以「已停用」的租户与地点由测试用自己的连接直接写库。
"""

import io
import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass

import pytest
from sqlalchemy import Connection, Engine, func, insert, make_url, select

from app.cli.__main__ import main
from app.config import Settings
from app.models.catalog import Tenant
from app.models.inventory import Location, SalesChannel, StockPool
from tests.integration.conftest import ROOT, TEST_DATABASE_URL, new_id

TABLES = (Tenant, Location, StockPool, SalesChannel)


@dataclass
class Result:
    code: int
    out: str
    err: str

    def json(self) -> dict:
        assert self.code == 0, self.err
        assert self.err == ""
        assert self.out.count("\n") == 1 and self.out.endswith("\n")
        return json.loads(self.out)

    def one_error_line(self) -> str:
        assert self.out == ""
        assert self.err.count("\n") == 1 and self.err.endswith("\n")
        assert "Traceback" not in self.err
        return self.err


def run_cli(settings: Settings, *argv: str) -> Result:
    stdout, stderr = io.StringIO(), io.StringIO()
    code = main(list(argv), settings, stdout, stderr)
    return Result(code, stdout.getvalue(), stderr.getvalue())


def create(kind: str, name: str = "X", **refs: str) -> list[str]:
    argv = [kind, "create", "--name", name]
    for option, value in refs.items():
        argv += [f"--{option}", value]
    return argv


@pytest.fixture
def settings(migrated_engine: Engine) -> Settings:
    return Settings(env="demo", database_url=TEST_DATABASE_URL)


@pytest.fixture
def cli(settings: Settings) -> Callable[..., Result]:
    return lambda *argv: run_cli(settings, *argv)


def count(conn: Connection, model) -> int:
    return conn.execute(select(func.count()).select_from(model)).scalar_one()


def counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as conn:
        return {model.__tablename__: count(conn, model) for model in TABLES}


def row(engine: Engine, model, key: str) -> dict:
    """库里的一行，不含时间戳列。"""
    table = model.__table__
    primary_key = next(iter(table.primary_key))
    columns = [column for column in table.c if column.name not in ("created_at", "updated_at")]
    with engine.connect() as conn:
        return dict(conn.execute(select(*columns).where(primary_key == key)).one()._mapping)


@dataclass
class Provisioned:
    tenant_id: str
    location_id: str
    pool_id: str
    channel_id: str


def provision(cli: Callable[..., Result]) -> Provisioned:
    """用命令行本身开通一整套：租户、地点、库存池、销售入口。"""
    tenant_id = cli(*create("tenant", "Shop")).json()["tenant_id"]
    location_id = cli(*create("location", "Warehouse", tenant=tenant_id)).json()["location_id"]
    pool = cli(*create("pool", "Main", tenant=tenant_id, location=location_id)).json()
    channel = cli(*create("channel", "Web", tenant=tenant_id, pool=pool["pool_id"])).json()
    return Provisioned(tenant_id, location_id, pool["pool_id"], channel["channel_id"])


# --- create ---


def test_tenant_create_writes_an_active_tenant_with_a_trimmed_name(cli, migrated_engine):
    output = cli(*create("tenant", "  Acme Demo  ")).json()
    assert output["tenant_id"].startswith("ten_")
    assert output == {"tenant_id": output["tenant_id"], "name": "Acme Demo", "status": "active"}
    assert row(migrated_engine, Tenant, output["tenant_id"]) == output


def test_a_name_of_exactly_200_characters_is_accepted(cli, migrated_engine):
    output = cli(*create("tenant", "n" * 200)).json()
    assert row(migrated_engine, Tenant, output["tenant_id"])["name"] == "n" * 200


def test_location_create_writes_an_active_location_of_the_tenant(cli, migrated_engine):
    tenant_id = cli(*create("tenant", "Shop")).json()["tenant_id"]
    output = cli(*create("location", "\tWarehouse ", tenant=tenant_id)).json()
    assert output["location_id"].startswith("loc_")
    assert output == {
        "location_id": output["location_id"],
        "tenant_id": tenant_id,
        "name": "Warehouse",
        "status": "active",
    }
    assert row(migrated_engine, Location, output["location_id"]) == output


def test_pool_create_writes_an_initializing_pool_at_the_location(cli, migrated_engine):
    tenant_id = cli(*create("tenant", "Shop")).json()["tenant_id"]
    location_id = cli(*create("location", "W", tenant=tenant_id)).json()["location_id"]
    output = cli(*create("pool", " Main ", tenant=tenant_id, location=location_id)).json()
    assert output["pool_id"].startswith("pool_")
    # 期初只在 initializing 可用（契约第 6 节），新池不能一建就是 live。
    assert output == {
        "pool_id": output["pool_id"],
        "tenant_id": tenant_id,
        "location_id": location_id,
        "name": "Main",
        "status": "initializing",
    }
    assert row(migrated_engine, StockPool, output["pool_id"]) == output


def test_channel_create_writes_an_active_channel_bound_to_the_pool(cli, migrated_engine):
    tenant_id = cli(*create("tenant", "Shop")).json()["tenant_id"]
    location_id = cli(*create("location", "W", tenant=tenant_id)).json()["location_id"]
    pool_id = cli(*create("pool", "P", tenant=tenant_id, location=location_id)).json()["pool_id"]
    output = cli(*create("channel", "  Web shop ", tenant=tenant_id, pool=pool_id)).json()
    assert output["channel_id"].startswith("chan_")
    assert output == {
        "channel_id": output["channel_id"],
        "tenant_id": tenant_id,
        "pool_id": pool_id,
        "name": "Web shop",
        "status": "active",
    }
    assert row(migrated_engine, SalesChannel, output["channel_id"]) == output


# --- 校验 ---


@dataclass
class World:
    mine: Provisioned
    other: Provisioned
    inactive_tenant_id: str
    inactive_location_id: str
    free_pool_id: str


@pytest.fixture
def world(cli, migrated_engine) -> World:
    mine, other = provision(cli), provision(cli)
    # 本租户的第二个池还没有销售入口。
    spare = cli(*create("pool", "Spare", tenant=mine.tenant_id, location=mine.location_id))
    inactive_tenant_id, inactive_location_id = new_id("ten"), new_id("loc")
    with migrated_engine.begin() as db:
        db.execute(
            insert(Tenant).values(tenant_id=inactive_tenant_id, name="Gone", status="inactive")
        )
        db.execute(
            insert(Location).values(
                location_id=inactive_location_id,
                tenant_id=mine.tenant_id,
                name="Closed",
                status="inactive",
            )
        )
    return World(mine, other, inactive_tenant_id, inactive_location_id, spare.json()["pool_id"])


def rejected_commands(w: World) -> dict[str, list[str]]:
    tenant, dead = w.mine.tenant_id, w.inactive_tenant_id
    return {
        "unknown subcommand": ["frobnicate"],
        "unknown action": ["tenant", "delete", "--name", "x"],
        "no subcommand": [],
        "missing name": ["tenant", "create"],
        "missing tenant": ["location", "create", "--name", "W"],
        "missing pool": create("channel", tenant=tenant),
        "empty name": create("tenant", ""),
        "blank name": create("tenant", "   "),
        "name over 200": create("tenant", "n" * 201),
        "name with newline": create("tenant", "Acme\nDemo"),
        "name with control character": create("tenant", "Acme\x07"),
        "unknown tenant": create("location", tenant=new_id("ten")),
        "inactive tenant": create("location", tenant=dead),
        "inactive tenant (pool)": create("pool", tenant=dead, location=w.mine.location_id),
        "foreign location": create("pool", tenant=tenant, location=w.other.location_id),
        "location is inactive": create("pool", tenant=tenant, location=w.inactive_location_id),
        "foreign pool": create("channel", tenant=tenant, pool=w.other.pool_id),
        "pool already has a channel": create("channel", tenant=tenant, pool=w.mine.pool_id),
    }


REJECTED_CASES = [
    "unknown subcommand",
    "unknown action",
    "no subcommand",
    "missing name",
    "missing tenant",
    "missing pool",
    "empty name",
    "blank name",
    "name over 200",
    "name with newline",
    "name with control character",
    "unknown tenant",
    "inactive tenant",
    "inactive tenant (pool)",
    "foreign location",
    "location is inactive",
    "foreign pool",
    "pool already has a channel",
]


@pytest.mark.parametrize("case", REJECTED_CASES)
def test_rejected_commands_exit_2_with_one_line_and_write_nothing(
    case, cli, world, migrated_engine
):
    commands = rejected_commands(world)
    assert set(commands) == set(REJECTED_CASES)
    before = counts(migrated_engine)
    result = cli(*commands[case])
    assert result.code == 2
    result.one_error_line()
    assert counts(migrated_engine) == before


def test_the_free_pool_still_accepts_its_first_channel(cli, world):
    """对照：池已有入口被拒是因为那个池，同一租户别的池照样能建入口。"""
    tenant_id = world.mine.tenant_id
    output = cli(*create("channel", "Second", tenant=tenant_id, pool=world.free_pool_id)).json()
    assert output["pool_id"] == world.free_pool_id


def test_argument_errors_do_not_echo_the_values_passed(cli):
    secret = "csk_" + "A" * 43
    for argv in (
        ["tenant", "create", "--token", secret],
        ["tenant", "create", "--name", "x", secret],
        [secret],
    ):
        result = cli(*argv)
        assert result.code == 2
        assert secret not in result.one_error_line()


def test_validation_errors_do_not_echo_the_ids_passed(cli):
    secret = "csk_" + "B" * 43
    result = cli(*create("location", "W", tenant=secret))
    assert result.code == 2
    assert secret not in result.one_error_line()


def test_help_goes_to_stdout_and_exits_0(cli):
    for argv, expected in ((["--help"], "tenant"), (["pool", "create", "-h"], "--location")):
        result = cli(*argv)
        assert result.code == 0
        assert result.err == ""
        assert expected in result.out


# --- show ---


def test_show_without_arguments_lists_all_tenants(cli, migrated_engine):
    first, second = provision(cli), provision(cli)
    tenants = cli("show").json()["tenants"]
    ids = [tenant["tenant_id"] for tenant in tenants]
    assert {first.tenant_id, second.tenant_id} <= set(ids)
    with migrated_engine.connect() as conn:
        assert len(ids) == count(conn, Tenant)
    assert {"tenant_id": first.tenant_id, "name": "Shop", "status": "active"} in tenants


def test_show_tenant_lists_only_that_tenants_objects(cli):
    mine, other = provision(cli), provision(cli)
    result = cli("show", "--tenant", mine.tenant_id)
    assert result.json() == {
        "tenant": {"tenant_id": mine.tenant_id, "name": "Shop", "status": "active"},
        "locations": [
            {
                "location_id": mine.location_id,
                "tenant_id": mine.tenant_id,
                "name": "Warehouse",
                "status": "active",
            }
        ],
        "pools": [
            {
                "pool_id": mine.pool_id,
                "tenant_id": mine.tenant_id,
                "location_id": mine.location_id,
                "name": "Main",
                "status": "initializing",
            }
        ],
        "channels": [
            {
                "channel_id": mine.channel_id,
                "tenant_id": mine.tenant_id,
                "pool_id": mine.pool_id,
                "name": "Web",
                "status": "active",
            }
        ],
    }
    for foreign in (other.tenant_id, other.location_id, other.pool_id, other.channel_id):
        assert foreign not in result.out


def test_show_an_unknown_tenant_exits_2(cli):
    result = cli("show", "--tenant", new_id("ten"))
    assert result.code == 2
    result.one_error_line()


# --- 失败输出 ---


def test_an_unconfigured_database_exits_1():
    result = run_cli(Settings(env="demo", database_url=""), *create("tenant", "Shop"))
    assert result.code == 1
    result.one_error_line()


def test_an_unreachable_database_exits_1_without_the_connection_string(migrated_engine):
    # 同一台 MySQL 的一个没人监听的端口。
    url = make_url(TEST_DATABASE_URL).set(port=1)
    rendered = url.render_as_string(hide_password=False)
    before = counts(migrated_engine)
    for argv in (create("tenant", "Shop"), ["show"]):
        result = run_cli(Settings(env="demo", database_url=rendered), *argv)
        assert result.code == 1
        result.one_error_line()
        for text in (result.out, result.err):
            assert rendered not in text
            assert TEST_DATABASE_URL not in text
            assert not url.password or str(url.password) not in text
    assert counts(migrated_engine) == before


def process(*argv: str, **env: str | None) -> subprocess.CompletedProcess:
    """以 python -m app.cli 起一个进程；CORE_ 开头的变量只用这里给的。"""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("CORE_")}
    environment.update({key: value for key, value in env.items() if value is not None})
    return subprocess.run(
        [sys.executable, "-m", "app.cli", *argv],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_python_m_app_cli_runs_as_a_process():
    completed = process("--help", CORE_ENV="demo")
    assert completed.returncode == 0, completed.stderr
    assert "tenant" in completed.stdout


@pytest.mark.parametrize("core_env", [None, "staging"])
def test_a_bad_configuration_exits_1_with_one_line_and_no_traceback(core_env):
    completed = process("show", CORE_ENV=core_env)
    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr.count("\n") == 1
    assert "Traceback" not in completed.stderr
    assert "staging" not in completed.stderr
