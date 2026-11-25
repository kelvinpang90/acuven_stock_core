"""运维命令行的开通子命令（契约第 1 节、第 3 节、第 10 节第 4 条）。

在真 MySQL 上跑：直接调用入口函数 main，传入测试库的 Settings 与两个内存输出流。
命令行只能建、不能停用，所以「已停用」的租户与地点由测试用自己的连接直接写库。
提交进共享 demo 库的 platform、backoffice 入口与 system 主体由各测试在退出时删掉
（platform_tenants）：会话结束时 demo 库要降到 base。
"""

import io
import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import pytest
from sqlalchemy import Connection, Engine, delete, event, func, insert, make_url, select

from app.cli.__main__ import main
from app.cli.provisioning import SHOP_HAS_CHANNEL
from app.config import Settings
from app.domain.channels import (
    API,
    API_CHANNEL_NOT_ALONE,
    BACKOFFICE,
    PLATFORM,
    POOL_HAS_API_CHANNEL,
    POOL_HAS_BACKOFFICE_CHANNEL,
    SHOPEE,
    TIKTOK_SHOP,
)
from app.domain.service_auth import SYSTEM
from app.models.catalog import Tenant
from app.models.inventory import WENT_LIVE_COLUMNS, Location, SalesChannel, StockPool
from app.models.service_auth import Credential, SecurityEvent
from tests.integration.conftest import ROOT, TEST_DATABASE_URL, new_id

# 开通 platform 入口时会建 system 主体，被拒时凭据表也不能多出行。
TABLES = (Tenant, Location, StockPool, SalesChannel, Credential)
NOT_PLATFORM = {"kind": API, "platform": None, "platform_shop_id": None}


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


def channel_argv(
    tenant_id: str,
    pool_id: str,
    kind: str | None = None,
    platform: str | None = None,
    shop_id: str | None = None,
    name: str = "X",
) -> list[str]:
    """channel create；种类、平台与店铺 ID 不给就不带那个选项。"""
    argv = create("channel", name, tenant=tenant_id, pool=pool_id)
    for option, value in (("kind", kind), ("platform", platform), ("platform-shop-id", shop_id)):
        if value is not None:
            argv += [f"--{option}", value]
    return argv


def remove_platform_channels(engine: Engine, tenant_ids: list[str]) -> None:
    """删掉这些租户的 platform、backoffice 入口与 system 主体（没有别的行引用它们）。

    共享的 demo 库在会话结束时要降到 base：0013 的降级在一个池有多个入口时失败，
    0012 的降级在库里有 system 主体时失败。
    """
    if not tenant_ids:
        return
    principals = Credential.tenant_id.in_(tenant_ids), Credential.kind == SYSTEM
    channels = SalesChannel.tenant_id.in_(tenant_ids), SalesChannel.kind != API
    with engine.begin() as conn:
        conn.execute(delete(Credential).where(*principals))
        conn.execute(delete(SalesChannel).where(*channels))


@contextmanager
def platform_tenants(engine: Engine) -> Iterator[list[str]]:
    """收集测试里开通了 platform 或 backoffice 入口的租户，退出时（不论成败）删掉那些行。"""
    tenants: list[str] = []
    try:
        yield tenants
    finally:
        remove_platform_channels(engine, tenants)


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
    # 新池还没有上线记录（契约 6.3），库里的 went_live 四列为空。
    not_live = dict.fromkeys(WENT_LIVE_COLUMNS)
    assert row(migrated_engine, StockPool, output["pool_id"]) == output | not_live


def test_channel_create_writes_an_active_channel_bound_to_the_pool(cli, migrated_engine):
    tenant_id = cli(*create("tenant", "Shop")).json()["tenant_id"]
    location_id = cli(*create("location", "W", tenant=tenant_id)).json()["location_id"]
    pool_id = cli(*create("pool", "P", tenant=tenant_id, location=location_id)).json()["pool_id"]
    # 不带 --kind 的现有用法得 api 入口，输出没有 system_credential_id。
    output = cli(*create("channel", "  Web shop ", tenant=tenant_id, pool=pool_id)).json()
    assert output["channel_id"].startswith("chan_")
    assert output == {
        "channel_id": output["channel_id"],
        "tenant_id": tenant_id,
        "pool_id": pool_id,
        "name": "Web shop",
        "status": "active",
        **NOT_PLATFORM,
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
    # 参数错误开在还没有入口的池上：被拒只因为参数。
    free, used = w.free_pool_id, w.mine.pool_id

    def shop(shop_id: str) -> list[str]:
        return channel_argv(tenant, free, PLATFORM, SHOPEE, shop_id)

    return {
        "unknown kind": channel_argv(tenant, free, kind="web"),
        "kind in the wrong case": channel_argv(tenant, free, kind="API"),
        "unknown platform": channel_argv(tenant, free, PLATFORM, "lazada", "shop-1"),
        "platform in the wrong case": channel_argv(tenant, free, PLATFORM, "Shopee", "shop-1"),
        "platform channel without platform": channel_argv(tenant, free, PLATFORM, shop_id="s1"),
        "platform channel without shop ID": channel_argv(tenant, free, PLATFORM, SHOPEE),
        "api channel with platform": channel_argv(tenant, free, platform=SHOPEE),
        "api channel with shop ID": channel_argv(tenant, free, API, shop_id="shop-1"),
        "backoffice channel with platform": channel_argv(
            tenant, free, BACKOFFICE, SHOPEE, "shop-1"
        ),
        "empty shop ID": shop(""),
        "shop ID over 64": shop("s" * 65),
        "shop ID with a space": shop("shop 1"),
        "shop ID with a slash": shop("shop/1"),
        "platform channel on a pool with an api channel": channel_argv(
            tenant, used, PLATFORM, TIKTOK_SHOP, "shop-1"
        ),
        "backoffice channel on a pool with an api channel": channel_argv(tenant, used, BACKOFFICE),
        "platform channel on a foreign pool": channel_argv(
            tenant, w.other.pool_id, PLATFORM, SHOPEE, "shop-1"
        ),
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
    "unknown kind",
    "kind in the wrong case",
    "unknown platform",
    "platform in the wrong case",
    "platform channel without platform",
    "platform channel without shop ID",
    "api channel with platform",
    "api channel with shop ID",
    "backoffice channel with platform",
    "empty shop ID",
    "shop ID over 64",
    "shop ID with a space",
    "shop ID with a slash",
    "platform channel on a pool with an api channel",
    "backoffice channel on a pool with an api channel",
    "platform channel on a foreign pool",
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


# --- 入口的种类与组合（契约第 3 节，Q-81） ---

BAD_SHOP_ID = "platform shop ID must be 1 to 64 letters, digits or . _ : -"
# MySQL 上 with_for_update(read=True) 的两种写法。
SHARED_LOCK = (" FOR SHARE", " LOCK IN SHARE MODE")


def bare_pool(cli, tenant_id: str | None = None) -> tuple[str, str]:
    """用命令行建一个还没有入口的池；不给租户就新建一个。返回 (租户, 池)。"""
    if tenant_id is None:
        tenant_id = cli(*create("tenant", "Shop")).json()["tenant_id"]
    location_id = cli(*create("location", "W", tenant=tenant_id)).json()["location_id"]
    pool_id = cli(*create("pool", "P", tenant=tenant_id, location=location_id)).json()["pool_id"]
    return tenant_id, pool_id


def system_ids(engine: Engine, tenant_id: str) -> list[str]:
    where = Credential.tenant_id == tenant_id, Credential.kind == SYSTEM
    with engine.connect() as conn:
        return list(conn.execute(select(Credential.credential_id).where(*where)).scalars())


def security_events_of(engine: Engine, tenant_id: str) -> int:
    events = select(func.count()).select_from(SecurityEvent)
    statement = events.where(SecurityEvent.tenant_id == tenant_id)
    with engine.connect() as conn:
        return conn.execute(statement).scalar_one()


def refused(result: Result, reason: str) -> None:
    assert result.code == 2
    assert result.one_error_line() == f"error: {reason}\n"


def test_platform_channels_and_one_backoffice_channel_share_a_pool(cli, migrated_engine):
    with platform_tenants(migrated_engine) as tenants:
        tenant_id, pool_id = bare_pool(cli)
        tenants.append(tenant_id)
        shopee = cli(*channel_argv(tenant_id, pool_id, PLATFORM, SHOPEE, "shopee:my.1")).json()
        system_id = shopee["system_credential_id"]
        assert shopee == {
            "channel_id": shopee["channel_id"],
            "tenant_id": tenant_id,
            "pool_id": pool_id,
            "name": "X",
            "status": "active",
            "kind": PLATFORM,
            "platform": SHOPEE,
            "platform_shop_id": "shopee:my.1",
            "system_credential_id": system_id,
        }
        stored = {key: value for key, value in shopee.items() if key != "system_credential_id"}
        assert row(migrated_engine, SalesChannel, shopee["channel_id"]) == stored

        # 第一个 platform 入口建出这个租户的 system 主体，不记安全事件。
        assert system_ids(migrated_engine, tenant_id) == [system_id]
        assert row(migrated_engine, Credential, system_id) == {
            "credential_id": system_id,
            "tenant_id": tenant_id,
            "channel_id": None,
            "name": "platform_events",
            "scopes": [],
            "kind": SYSTEM,
            "status": "active",
            "revoked_at": None,
        }

        tiktok = cli(*channel_argv(tenant_id, pool_id, PLATFORM, TIKTOK_SHOP, "7001")).json()
        assert tiktok["system_credential_id"] == system_id
        backoffice = cli(*channel_argv(tenant_id, pool_id, BACKOFFICE)).json()
        assert "system_credential_id" not in backoffice
        expected = NOT_PLATFORM | {"kind": BACKOFFICE}
        assert {key: backoffice[key] for key in expected} == expected

        # 第二个 backoffice、再加 api 都被拒，什么也不写。
        before = counts(migrated_engine)
        refused(cli(*channel_argv(tenant_id, pool_id, BACKOFFICE)), POOL_HAS_BACKOFFICE_CHANNEL)
        refused(cli(*channel_argv(tenant_id, pool_id, API)), API_CHANNEL_NOT_ALONE)
        refused(cli(*channel_argv(tenant_id, pool_id)), API_CHANNEL_NOT_ALONE)
        assert counts(migrated_engine) == before

        # 两个 platform 加一个 backoffice 之后，platform 照样可以再加。
        third = cli(*channel_argv(tenant_id, pool_id, PLATFORM, SHOPEE, "shopee:my.2")).json()
        assert third["system_credential_id"] == system_id
        assert system_ids(migrated_engine, tenant_id) == [system_id]
        assert security_events_of(migrated_engine, tenant_id) == 0

        channels = cli("show", "--tenant", tenant_id).json()["channels"]
        shown = {(item["kind"], item["platform"], item["platform_shop_id"]) for item in channels}
        assert shown == {
            (PLATFORM, SHOPEE, "shopee:my.1"),
            (PLATFORM, TIKTOK_SHOP, "7001"),
            (PLATFORM, SHOPEE, "shopee:my.2"),
            (BACKOFFICE, None, None),
        }


def test_a_pool_with_an_api_channel_accepts_no_other_channel(cli, migrated_engine):
    shop = provision(cli)
    before = counts(migrated_engine)
    for argv in (
        channel_argv(shop.tenant_id, shop.pool_id),
        channel_argv(shop.tenant_id, shop.pool_id, API),
        channel_argv(shop.tenant_id, shop.pool_id, PLATFORM, SHOPEE, "shop-1"),
        channel_argv(shop.tenant_id, shop.pool_id, BACKOFFICE),
    ):
        refused(cli(*argv), POOL_HAS_API_CHANNEL)
    assert counts(migrated_engine) == before
    assert system_ids(migrated_engine, shop.tenant_id) == []


def test_a_backoffice_channel_may_come_first(cli, migrated_engine):
    with platform_tenants(migrated_engine) as tenants:
        tenant_id, pool_id = bare_pool(cli)
        tenants.append(tenant_id)
        backoffice = cli(*channel_argv(tenant_id, pool_id, BACKOFFICE)).json()
        assert backoffice["kind"] == BACKOFFICE
        assert "system_credential_id" not in backoffice
        # 只有 backoffice 的中间状态不建 system 主体。
        assert system_ids(migrated_engine, tenant_id) == []
        refused(cli(*channel_argv(tenant_id, pool_id, API)), API_CHANNEL_NOT_ALONE)
        platform = cli(*channel_argv(tenant_id, pool_id, PLATFORM, SHOPEE, "shop-1")).json()
        assert system_ids(migrated_engine, tenant_id) == [platform["system_credential_id"]]


def test_one_system_principal_per_tenant_and_one_channel_per_platform_shop(cli, migrated_engine):
    with platform_tenants(migrated_engine) as tenants:
        tenant_id, first_pool = bare_pool(cli)
        _, second_pool = bare_pool(cli, tenant_id)
        other_id, other_pool = bare_pool(cli)
        tenants += [tenant_id, other_id]
        first = cli(*channel_argv(tenant_id, first_pool, PLATFORM, SHOPEE, "shop-1")).json()
        system_id = first["system_credential_id"]
        # 同租户另一个池上的 platform 入口不再建 system 主体。
        second = cli(*channel_argv(tenant_id, second_pool, PLATFORM, SHOPEE, "shop-2")).json()
        assert second["system_credential_id"] == system_id
        assert system_ids(migrated_engine, tenant_id) == [system_id]

        # 同租户同平台同店铺 ID 已有：不论开在哪个池。
        before = counts(migrated_engine)
        for pool_id in (first_pool, second_pool):
            argv = channel_argv(tenant_id, pool_id, PLATFORM, SHOPEE, "shop-1")
            refused(cli(*argv), SHOP_HAS_CHANNEL)
        assert counts(migrated_engine) == before

        # 别的平台的同号店铺、别的租户的同一店铺照样可以；别的租户有自己的 system 主体。
        cli(*channel_argv(tenant_id, second_pool, PLATFORM, TIKTOK_SHOP, "shop-1")).json()
        other = cli(*channel_argv(other_id, other_pool, PLATFORM, SHOPEE, "shop-1")).json()
        assert other["system_credential_id"] != system_id
        assert system_ids(migrated_engine, other_id) == [other["system_credential_id"]]
        assert system_ids(migrated_engine, tenant_id) == [system_id]
        assert security_events_of(migrated_engine, tenant_id) == 0


def test_refusals_follow_arguments_tenant_pool_combination_then_shop(cli, world, migrated_engine):
    tenant, free, used = world.mine.tenant_id, world.free_pool_id, world.mine.pool_id
    unknown, dead, foreign = new_id("ten"), world.inactive_tenant_id, world.other.pool_id
    with platform_tenants(migrated_engine) as tenants:
        tenants.append(tenant)
        cli(*channel_argv(tenant, free, PLATFORM, SHOPEE, "dup-1")).json()
        cases = [
            (channel_argv(unknown, foreign, PLATFORM, SHOPEE, "dup 1"), BAD_SHOP_ID),
            (channel_argv(unknown, foreign, BACKOFFICE), "tenant not found"),
            (channel_argv(dead, foreign, BACKOFFICE), "tenant is not active"),
            (channel_argv(tenant, foreign, BACKOFFICE), "pool not found for this tenant"),
            (channel_argv(tenant, used, PLATFORM, SHOPEE, "dup-1"), POOL_HAS_API_CHANNEL),
            (channel_argv(tenant, free, PLATFORM, SHOPEE, "dup-1"), SHOP_HAS_CHANNEL),
        ]
        before = counts(migrated_engine)
        for argv, reason in cases:
            refused(cli(*argv), reason)
        assert counts(migrated_engine) == before


@contextmanager
def statements() -> Iterator[list[str]]:
    """这段时间里各引擎发给数据库的语句，空白压成一个空格。"""
    seen: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:
        seen.append(" ".join(statement.split()))

    event.listen(Engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(Engine, "before_cursor_execute", record)


def test_channel_create_locks_the_pool_then_the_tenant_and_reads_with_shared_locks(
    cli, migrated_engine
):
    with platform_tenants(migrated_engine) as tenants:
        tenant_id, pool_id = bare_pool(cli)
        tenants.append(tenant_id)
        with statements() as seen:
            cli(*channel_argv(tenant_id, pool_id, PLATFORM, SHOPEE, "shop-1")).json()

    def position(select_from: str, locked: bool, ending: tuple[str, ...] = ()) -> int:
        found = [
            index
            for index, statement in enumerate(seen)
            if statement.startswith(f"SELECT {select_from} ")
            and statement.endswith((" FOR UPDATE", *SHARED_LOCK)) == locked
            and (not ending or statement.endswith(ending))
        ]
        assert len(found) == 1, seen
        return found[0]

    plain_tenant = position("tenants.status FROM tenants", locked=False)
    pool = position("stock_pools.pool_id FROM stock_pools", True, (" FOR UPDATE",))
    tenant = position("tenants.status FROM tenants", True, (" FOR UPDATE",))
    kinds = position("sales_channels.kind FROM sales_channels", True, SHARED_LOCK)
    system = position("credentials.credential_id FROM credentials", True, SHARED_LOCK)
    # 先普通读核对租户，再锁池行、再锁租户行，之后才读已有入口与 system 主体。
    assert plain_tenant < pool < tenant < kinds < system
    # 不对租户行先取共享锁（不调 require_tenant），免得锁升级。
    assert not any(
        statement.startswith("SELECT tenants.") and statement.endswith(SHARED_LOCK)
        for statement in seen
    )


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
                **NOT_PLATFORM,
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
