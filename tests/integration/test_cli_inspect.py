"""运维命令行的只读排查 inspect（契约 8.6 第 1 条、4.4 的 0.22 说明）。

在真 MySQL 上跑：直接调用入口函数 main（同 test_cli_provisioning）。
每个测试建自己的租户、池、销售入口与凭据，池先切到 live；预留、确认销售与发货经接口建立。
三个子命令的输出与 TestClient 用该租户凭据以同样参数调对应读接口得到的 data 相比。
"""

from collections.abc import Callable, Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select

from app.config import Settings
from app.domain.environment import DEMO, PRODUCTION
from app.domain.service_auth import STOCK_READ
from app.models.service_auth import SecurityEvent
from tests.integration.conftest import TEST_DATABASE_URL, instance_marked, new_id, qline
from tests.integration.test_api_auth import Caller
from tests.integration.test_cli_provisioning import Result, run_cli
from tests.integration.test_idempotency import add_skus
from tests.integration.test_invariants_api import BUSINESS_TABLES
from tests.integration.test_order_stock_api import channel_of, committed, dispatch, order_stock
from tests.integration.test_reservations_api import (
    data_of,
    get_reservation,
    live_caller,
    make_client,
    new_ref,
)
from tests.integration.test_stock_imports_api import credential_of

LEDGER = "/v1/ledger"
FROM = "2000-01-01T00:00:00Z"
TO = "2999-01-01T00:00:00Z"
NOT_FOUND_CODE = 2


# --- 夹具 ---


@pytest.fixture
def client(migrated_engine: Engine) -> Iterator[TestClient]:
    test_client = make_client()
    try:
        yield test_client
    finally:
        test_client.app.state.engine.dispose()


@pytest.fixture
def engine(migrated_engine: Engine) -> Engine:
    return migrated_engine


@pytest.fixture
def caller(engine: Engine) -> Caller:
    """池已 live、有销售入口的调用方（orders.read 与 orders.write）。"""
    return live_caller(engine)


def cli_for(env: str) -> Callable[..., Result]:
    settings = Settings(env=env, database_url=TEST_DATABASE_URL)
    return lambda *argv: run_cli(settings, *argv)


@pytest.fixture
def cli(migrated_engine: Engine) -> Callable[..., Result]:
    return cli_for(DEMO)


@pytest.fixture
def production_cli(migrated_engine: Engine) -> Callable[..., Result]:
    """CORE_ENV 为 production 的命令行：每次调用期间把库标记为 production，调用结束即改回 demo。"""
    run = cli_for(PRODUCTION)

    def production(*argv: str) -> Result:
        with instance_marked(migrated_engine, PRODUCTION):
            return run(*argv)

    return production


@pytest.fixture
def shipped(client: TestClient, engine: Engine, caller: Caller) -> tuple[dict, str]:
    """一单 5 件，确认销售后分 3 次发货；返回预留的表示与那个 SKU。"""
    confirmed, (sku,) = committed(client, engine, caller, [5])
    for _ in range(3):
        dispatch(client, caller, confirmed["order_ref"], [qline("l1", 1)])
    return confirmed, sku


# --- 请求与参数 ---


def ledger_api(client: TestClient, reader: Caller, params: dict) -> dict:
    headers = reader.auth | {"X-Request-ID": f"inspect-ledger-{uuid4().hex}"}
    return data_of(client.get(LEDGER, params=params, headers=headers), 200)


def ledger_argv(caller: Caller, sku: str, params: dict | None = None) -> list[str]:
    argv = ["inspect", "ledger", "--tenant", caller.tenant_id, "--pool", caller.pool_id]
    argv += ["--sku", sku]
    for name, value in (params or {}).items():
        argv += [f"--{name}", value]
    return argv


def reservation_argv(tenant_id: str, reservation_id: str) -> list[str]:
    return ["inspect", "reservation", "--tenant", tenant_id, "--reservation-id", reservation_id]


def order_stock_argv(
    tenant_id: str, channel_id: str, order_ref: str, params: dict | None = None
) -> list[str]:
    argv = ["inspect", "order-stock", "--tenant", tenant_id, "--channel", channel_id]
    argv += ["--order-ref", order_ref]
    for name, value in (params or {}).items():
        argv += [f"--{name}", value]
    return argv


def not_found(result: Result, *values: str) -> str:
    """退出码 2、标准错误恰好一行、标准输出为空，说明不带传入的参数值。"""
    assert result.code == NOT_FOUND_CODE, result.err
    line = result.one_error_line()
    assert all(value not in line for value in values)
    return line


def rejected(result: Result, *values: str) -> str:
    assert result.code != 0
    line = result.one_error_line()
    assert all(value not in line for value in values)
    return line


def row_counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as db:
        return {
            model.__tablename__: db.execute(select(func.count()).select_from(model)).scalar_one()
            for model in (*BUSINESS_TABLES, SecurityEvent)
        }


# --- ledger ---


def test_ledger_matches_the_api_page_by_page(cli, client, engine, caller, shipped):
    _, sku = shipped
    reader = credential_of(engine, caller, [STOCK_READ])
    base = {"pool_id": caller.pool_id, "sku_id": sku}

    whole = ledger_api(client, reader, base)
    assert cli(*ledger_argv(caller, sku)).json() == whole
    assert len(whole["entries"]) >= 3

    pages = []
    after = None
    while True:
        params = {"limit": "2"} | ({} if after is None else {"after": after})
        expected = ledger_api(client, reader, base | params)
        assert cli(*ledger_argv(caller, sku, params)).json() == expected
        if not expected["entries"]:
            # 最后一页之后：没有记录，next_cursor 原样为 after。
            assert expected["next_cursor"] == after
            break
        pages.append(expected["entries"])
        after = expected["next_cursor"]
    assert len(pages) > 1
    assert [entry for page in pages for entry in page] == whole["entries"]


def test_ledger_takes_from_and_to_like_the_api(cli, client, engine, caller, shipped):
    _, sku = shipped
    reader = credential_of(engine, caller, [STOCK_READ])
    base = {"pool_id": caller.pool_id, "sku_id": sku, "from": FROM, "to": TO}
    first = ledger_api(client, reader, base | {"limit": "1"})
    output = cli(*ledger_argv(caller, sku, {"from": FROM, "to": TO, "limit": "1"})).json()
    assert output == first
    params = {"from": FROM, "to": TO, "after": first["next_cursor"]}
    expected = ledger_api(client, reader, base | {"after": first["next_cursor"]})
    assert cli(*ledger_argv(caller, sku, params)).json() == expected

    # 时间段之外：没有记录。
    empty = {"from": FROM, "to": "2000-01-02T00:00:00Z"}
    assert cli(*ledger_argv(caller, sku, empty)).json() == {"entries": [], "next_cursor": None}


def test_ledger_of_a_missing_or_foreign_pool_or_sku_is_not_found(cli, engine, caller, shipped):
    _, sku = shipped
    other = live_caller(engine)
    [foreign_sku] = add_skus(engine, other.tenant_id, 1)
    for pool_id in (new_id("pool"), other.pool_id):
        argv = ["inspect", "ledger", "--tenant", caller.tenant_id, "--pool", pool_id]
        line = not_found(cli(*argv, "--sku", sku), caller.tenant_id, pool_id, sku)
        assert "pool" in line
    for sku_id in (new_id("sku"), foreign_sku):
        line = not_found(cli(*ledger_argv(caller, sku_id)), caller.tenant_id, sku_id)
        assert "sku" in line
    # 租户本身不存在：池同样找不到。
    missing_tenant = new_id("ten")
    argv = ["inspect", "ledger", "--tenant", missing_tenant, "--pool", caller.pool_id]
    not_found(cli(*argv, "--sku", sku), missing_tenant, caller.pool_id)


@pytest.mark.parametrize(
    "params",
    [
        {"limit": "0"},
        {"limit": "101"},
        {"limit": "abc"},
        {"after": "abc"},
        {"after": "v1.1.0123456789abcdef"},
        {"from": "yesterday"},
        {"from": TO, "to": FROM},
    ],
)
def test_ledger_rejects_invalid_parameters(cli, engine, caller, shipped, params):
    _, sku = shipped
    rejected(cli(*ledger_argv(caller, sku, params)), *params.values())


def test_ledger_rejects_a_cursor_of_other_filters(cli, client, engine, caller, shipped):
    _, sku = shipped
    first = cli(*ledger_argv(caller, sku, {"limit": "1"})).json()
    cursor = first["next_cursor"]
    rejected(cli(*ledger_argv(caller, sku, {"after": cursor, "from": FROM})), cursor)


# --- reservation ---


def test_reservation_matches_the_api(cli, client, engine, caller, shipped):
    confirmed, _ = shipped
    reservation_id = confirmed["reservation_id"]
    expected = data_of(get_reservation(client, caller, reservation_id), 200)
    assert cli(*reservation_argv(caller.tenant_id, reservation_id)).json() == expected


def test_a_missing_or_foreign_reservation_is_not_found(cli, client, engine, caller, shipped):
    other = live_caller(engine)
    theirs, _ = committed(client, engine, other, [1])
    for reservation_id in (new_id("rsv"), theirs["reservation_id"]):
        result = cli(*reservation_argv(caller.tenant_id, reservation_id))
        assert "reservation" in not_found(result, caller.tenant_id, reservation_id)


# --- order-stock ---


def test_order_stock_matches_the_api_page_by_page(cli, client, engine, caller, shipped):
    confirmed, _ = shipped
    order_ref = confirmed["order_ref"]
    channel_id = channel_of(engine, caller)

    whole = data_of(order_stock(client, caller, order_ref), 200)
    assert cli(*order_stock_argv(caller.tenant_id, channel_id, order_ref)).json() == whole
    assert len(whole["documents"]) == 3

    pages = []
    after = None
    for _ in range(3):
        params = {"limit": "2"} | ({} if after is None else {"after": after})
        expected = data_of(order_stock(client, caller, order_ref, params), 200)
        argv = order_stock_argv(caller.tenant_id, channel_id, order_ref, params)
        assert cli(*argv).json() == expected
        pages.append(expected["documents"])
        after = expected["next_cursor"]
    assert [len(page) for page in pages] == [2, 1, 0]
    assert [document for page in pages for document in page] == whole["documents"]


def test_a_missing_or_foreign_order_is_not_found(cli, client, engine, caller, shipped):
    confirmed, _ = shipped
    channel_id = channel_of(engine, caller)
    other = live_caller(engine)
    theirs, _ = committed(client, engine, other, [1])
    other_channel = channel_of(engine, other)

    cases = [
        (caller.tenant_id, channel_id, new_ref()),
        # 别的租户的订单：用本租户查不到，用它的入口也不行。
        (caller.tenant_id, channel_id, theirs["order_ref"]),
        (caller.tenant_id, other_channel, theirs["order_ref"]),
        # 本租户的订单换成别的租户的入口。
        (caller.tenant_id, other_channel, confirmed["order_ref"]),
        (other.tenant_id, channel_id, confirmed["order_ref"]),
        (caller.tenant_id, new_id("chn"), confirmed["order_ref"]),
    ]
    for tenant_id, channel, order_ref in cases:
        result = cli(*order_stock_argv(tenant_id, channel, order_ref))
        assert "order" in not_found(result, tenant_id, channel, order_ref)


@pytest.mark.parametrize(
    "params",
    [
        {"limit": "0"},
        {"limit": "101"},
        {"limit": "abc"},
        {"after": "abc"},
        {"after": "v1.0.0123456789abcdef"},
    ],
)
def test_order_stock_rejects_invalid_parameters(cli, engine, caller, shipped, params):
    confirmed, _ = shipped
    channel_id = channel_of(engine, caller)
    argv = order_stock_argv(caller.tenant_id, channel_id, confirmed["order_ref"], params)
    rejected(cli(*argv), *params.values())


def test_order_stock_rejects_a_malformed_order_ref_and_a_foreign_cursor(
    cli, client, engine, caller, shipped
):
    confirmed, _ = shipped
    channel_id = channel_of(engine, caller)
    rejected(cli(*order_stock_argv(caller.tenant_id, channel_id, "o" * 129)), "o" * 129)

    mine, _ = committed(client, engine, caller, [1])
    foreign = data_of(order_stock(client, caller, confirmed["order_ref"]), 200)["next_cursor"]
    assert foreign is not None
    argv = order_stock_argv(caller.tenant_id, channel_id, mine["order_ref"], {"after": foreign})
    rejected(cli(*argv), foreign, mine["order_ref"])


# --- 只读、生产实例与帮助 ---


def test_inspecting_writes_nothing(cli, client, engine, caller, shipped):
    confirmed, sku = shipped
    channel_id = channel_of(engine, caller)
    before = row_counts(engine)
    cli(*ledger_argv(caller, sku, {"limit": "2"})).json()
    cli(*reservation_argv(caller.tenant_id, confirmed["reservation_id"])).json()
    cli(*order_stock_argv(caller.tenant_id, channel_id, confirmed["order_ref"])).json()
    not_found(cli(*reservation_argv(caller.tenant_id, new_id("rsv"))))
    not_found(cli(*order_stock_argv(caller.tenant_id, channel_id, new_ref())))
    not_found(cli(*ledger_argv(caller, new_id("sku"))))
    rejected(cli(*ledger_argv(caller, sku, {"limit": "0"})))
    assert row_counts(engine) == before


def test_inspecting_works_on_a_production_instance(
    cli, production_cli, client, engine, caller, shipped
):
    confirmed, sku = shipped
    channel_id = channel_of(engine, caller)
    for argv in (
        ledger_argv(caller, sku, {"limit": "2"}),
        reservation_argv(caller.tenant_id, confirmed["reservation_id"]),
        order_stock_argv(caller.tenant_id, channel_id, confirmed["order_ref"]),
    ):
        before = row_counts(engine)
        assert production_cli(*argv).json() == cli(*argv).json()
        assert row_counts(engine) == before
    missing = reservation_argv(caller.tenant_id, new_id("rsv"))
    not_found(production_cli(*missing))


@pytest.mark.parametrize("action", ["ledger", "reservation", "order-stock"])
def test_help_does_not_touch_the_database(action):
    # 没有连接串：碰了数据库就以 1 结束。
    result = run_cli(Settings(env=DEMO, database_url=""), "inspect", action, "--help")
    assert result.code == 0
    assert result.err == ""
    assert "--tenant" in result.out


def test_missing_required_options_are_usage_errors(cli):
    result = cli("inspect", "ledger", "--tenant", "ten_x", "--pool", "pool_x")
    assert result.code == 2
    assert "ten_x" not in result.one_error_line()
