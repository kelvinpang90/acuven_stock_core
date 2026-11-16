"""整单预留与预留的查询（契约 §5.2、§5.6、§7.1、§8.1、§10 第 3、4 条），
以及预留有效期的配置。

在真 MySQL 上跑；每个测试建自己的租户、池、销售入口与凭据，池默认先切到 live，
SKU 与余额行由测试直接在库里造（自己提交的连接）。
并发的测试替换服务层的 require_live（第 1 步之后、锁余额行之前调用），
按幂等键让两个请求按约定的先后走。配置的测试除日志那条外不碰数据库。
"""

import logging
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import Engine, select, text, update

from app.cli import EXIT_FAILURE
from app.cli.__main__ import run_process
from app.config import Settings
from app.domain.requests import STOCK_OPERATOR
from app.domain.service_auth import ORDERS_READ, ORDERS_WRITE
from app.main import create_app
from app.models.catalog import Product, Sku
from app.models.inventory import StockBalance
from app.models.orders import OrderLine, Reservation
from app.services import reservations as reservation_service
from tests.integration.conftest import TEST_DATABASE_URL, add_sku, add_tenant, balance_insert
from tests.integration.test_api_auth import Caller, error_of, events, issue
from tests.integration.test_idempotency import add_skus, rid_of
from tests.integration.test_stock_imports_api import merge, set_live
from tests.integration.test_stock_service import entries, quantities_of

RESERVATIONS = "/v1/reservations"
TTL_VARIABLE = "CORE_RESERVATION_TTL_SECONDS"
ORDER_SCOPES = [ORDERS_WRITE, ORDERS_READ]
ACTOR = {"X-Actor-Ref": "shopper-7", "X-Actor-Role": STOCK_OPERATOR}
RESERVATION_KEYS = {
    "reservation_id",
    "order_ref",
    "pool_id",
    "status",
    "expires_at",
    "created_at",
    "released_at",
    "release_reason",
    "lines",
}


def make_client(**settings: object) -> TestClient:
    config = {"env": "demo", "git_sha": "c" * 40, "database_url": TEST_DATABASE_URL} | settings
    return TestClient(create_app(Settings(**config)))


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


def live_caller(engine: Engine, **options: object) -> Caller:
    issued = issue(engine, scopes=ORDER_SCOPES, **options)
    set_live(engine, issued)
    return issued


@pytest.fixture
def caller(engine: Engine) -> Caller:
    """池已 live、有销售入口的调用方。"""
    return live_caller(engine)


# --- 请求 ---


def new_key() -> str:
    return f"shop:reserve:{uuid4().hex}"


def new_ref() -> str:
    return f"order-{uuid4().hex}"


def line(line_ref: str, sku_id: str, quantity: object) -> dict:
    return {"line_ref": line_ref, "sku_id": sku_id, "quantity": quantity}


def order(lines: list[dict], order_ref: str | None = None) -> dict:
    return {"order_ref": order_ref or new_ref(), "lines": lines}


def reserve(
    client: TestClient,
    caller: Caller,
    body: dict,
    key: str | None = None,
    actor: dict | None = None,
):
    headers = caller.auth | {
        "Idempotency-Key": key or new_key(),
        "X-Request-ID": f"reserve-test-{uuid4().hex}",
    }
    return client.post(RESERVATIONS, json=body, headers=headers | (actor or {}))


def read_headers(caller: Caller) -> dict:
    return caller.auth | {"X-Request-ID": f"reserve-read-{uuid4().hex}"}


def get_reservation(client: TestClient, caller: Caller, reservation_id: str):
    return client.get(f"{RESERVATIONS}/{reservation_id}", headers=read_headers(caller))


def find_reservations(client: TestClient, caller: Caller, params):
    return client.get(RESERVATIONS, params=params, headers=read_headers(caller))


def data_of(response, status: int) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["request_id"] == response.headers["X-Request-ID"]
    return body["data"]


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


# --- 造数据与观察 ---


def stock(engine: Engine, caller: Caller, sku_id: str, quantity: int) -> None:
    """建好余额行，账面 quantity 件、都可售。"""
    with engine.begin() as db:
        db.execute(balance_insert(caller.tenant_id, caller.pool_id, sku_id, stock_balance=quantity))


def restock(engine: Engine, caller: Caller, sku_id: str, quantity: int) -> None:
    with engine.begin() as db:
        statement = update(StockBalance).where(
            StockBalance.tenant_id == caller.tenant_id,
            StockBalance.pool_id == caller.pool_id,
            StockBalance.sku_id == sku_id,
        )
        db.execute(statement.values(stock_balance=quantity))


def deactivate_sku(engine: Engine, sku_id: str) -> None:
    with engine.begin() as db:
        db.execute(update(Sku).where(Sku.sku_id == sku_id).values(status="inactive"))


def deactivate_product_of(engine: Engine, sku_id: str) -> None:
    with engine.begin() as db:
        product_id = db.execute(select(Sku.product_id).where(Sku.sku_id == sku_id)).scalar_one()
        statement = update(Product).where(Product.product_id == product_id)
        db.execute(statement.values(status="inactive"))


def rows_of(engine: Engine, model, caller: Caller) -> list[dict]:
    table = model.__table__
    statement = select(*table.columns).where(table.c.tenant_id == caller.tenant_id)
    with engine.connect() as db:
        return [dict(row) for row in db.execute(statement).mappings()]


def assert_nothing_reserved(engine: Engine, caller: Caller) -> None:
    """被拒的预留不写预留、订单行与流水（§5.2 第 1 条）。"""
    assert rows_of(engine, Reservation, caller) == []
    assert rows_of(engine, OrderLine, caller) == []
    assert entries(engine, caller) == []


def shift_into_the_past(engine: Engine, reservation_id: str) -> None:
    """把预留的创建与到期时间都往前挪 1 小时：已到期、还没被关单。"""
    statement = text(
        "UPDATE reservations SET created_at = created_at - INTERVAL 1 HOUR,"
        " expires_at = expires_at - INTERVAL 1 HOUR WHERE reservation_id = :id"
    )
    with engine.begin() as db:
        db.execute(statement, {"id": reservation_id})


# --- 成功路径 ---


def test_a_reservation_holds_every_line_and_writes_one_reserve_entry_per_line(
    client, engine, caller
):
    first_sku, second_sku = add_skus(engine, caller.tenant_id, 2)
    stock(engine, caller, first_sku, 5)
    stock(engine, caller, second_sku, 5)
    order_ref = new_ref()
    # 请求里 line_ref 不按顺序；表示按 line_ref 升序。
    body = order([line("l2", second_sku, 2), line("l1", first_sku, 3)], order_ref)
    key = new_key()
    response = reserve(client, caller, body, key, actor=ACTOR)
    data = data_of(response, 201)

    assert set(data) == RESERVATION_KEYS
    assert data["reservation_id"].startswith("rsv_")
    assert data["order_ref"] == order_ref
    assert data["pool_id"] == caller.pool_id
    assert data["status"] == "active"
    assert data["released_at"] is None
    assert data["release_reason"] is None
    assert data["lines"] == [line("l1", first_sku, 3), line("l2", second_sku, 2)]
    # 有效期来自配置（默认 900 秒），网店不能指定。
    created_at = parse_time(data["created_at"])
    assert parse_time(data["expires_at"]) == created_at + timedelta(seconds=900)

    # 每行的 reserved 增加该行数量，账面不变。
    assert quantities_of(engine, caller, first_sku) == (5, 3, 0, 0)
    assert quantities_of(engine, caller, second_sku) == (5, 2, 0, 0)

    [stored] = rows_of(engine, Reservation, caller)
    assert stored["reservation_id"] == data["reservation_id"]
    assert stored["status"] == "active"
    assert stored["ttl_seconds"] == 900
    assert stored["channel_id"] is not None
    assert stored["credential_id"] == caller.credential_id
    assert (stored["actor_ref"], stored["actor_role"]) == ("shopper-7", STOCK_OPERATOR)
    assert stored["request_id"] == rid_of(response)
    assert stored["closed_at"] is None
    assert stored["release_reason"] is None

    lines = sorted(rows_of(engine, OrderLine, caller), key=lambda row: row["line_ref"])
    assert [(row["line_ref"], row["sku_id"], row["quantity"]) for row in lines] == [
        ("l1", first_sku, 3),
        ("l2", second_sku, 2),
    ]
    for row in lines:
        assert row["order_line_id"].startswith("oln_")
        assert row["reservation_id"] == data["reservation_id"]
        assert row["pool_id"] == caller.pool_id
        assert row["committed_qty"] == row["dispatched_qty"] == 0

    written = sorted(entries(engine, caller), key=lambda entry: entry["line_ref"])
    assert [(e["line_ref"], e["sku_id"], e["reserved_delta"]) for e in written] == [
        ("l1", first_sku, 3),
        ("l2", second_sku, 2),
    ]
    for entry in written:
        assert entry["entry_type"] == "RESERVE"
        assert entry["stock_balance_delta"] == entry["unavailable_delta"] == 0
        assert entry["source_type"] == "reservation"
        assert entry["source_ref"] == order_ref
        assert entry["reason_code"] is None
        assert entry["note"] is None
        assert entry["channel_id"] == stored["channel_id"]
        assert entry["idempotency_key"] == key
        assert entry["actor_ref"] == "shopper-7"
        assert entry["occurred_at"] == entry["recorded_at"] == stored["created_at"]
    assert written[0]["command_id"] == written[1]["command_id"]

    # 同键重放原样返回，不再写任何东西。
    replay = reserve(client, caller, body, key)
    assert replay.status_code == 201
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == response.json()
    assert quantities_of(engine, caller, first_sku) == (5, 3, 0, 0)
    assert len(entries(engine, caller)) == 2


def test_the_expiry_follows_the_configured_ttl(engine):
    caller = live_caller(engine)
    [sku] = add_skus(engine, caller.tenant_id, 1)
    stock(engine, caller, sku, 1)
    short = make_client(reservation_ttl_seconds=60)
    try:
        data = data_of(reserve(short, caller, order([line("l1", sku, 1)])), 201)
    finally:
        short.app.state.engine.dispose()
    created_at = parse_time(data["created_at"])
    assert parse_time(data["expires_at"]) == created_at + timedelta(seconds=60)
    [stored] = rows_of(engine, Reservation, caller)
    assert stored["ttl_seconds"] == 60


# --- 拒绝 ---


def test_insufficient_stock_lists_every_short_line_and_a_restock_lets_a_new_key_succeed(
    client, engine, caller
):
    enough, short, empty = add_skus(engine, caller.tenant_id, 3)
    stock(engine, caller, enough, 5)
    stock(engine, caller, short, 1)
    # empty 没有余额行：预建出全零的一行，可售为 0。
    body = order([line("l1", enough, 2), line("l2", short, 2), line("l3", empty, 1)])
    error = error_of(reserve(client, caller, body), 409)
    assert error["code"] == "INSUFFICIENT_STOCK"
    assert error["details"] == {
        "lines": [
            {"line_ref": "l2", "sku_id": short, "quantity": 2, "available": 1},
            {"line_ref": "l3", "sku_id": empty, "quantity": 1, "available": 0},
        ]
    }
    assert_nothing_reserved(engine, caller)
    assert quantities_of(engine, caller, enough) == (5, 0, 0, 0)

    # order_ref 没被占用：补货之后换新幂等键可以成功。
    restock(engine, caller, short, 2)
    restock(engine, caller, empty, 1)
    data = data_of(reserve(client, caller, body), 201)
    assert data["order_ref"] == body["order_ref"]
    assert quantities_of(engine, caller, short) == (2, 2, 0, 0)
    assert quantities_of(engine, caller, empty) == (1, 1, 0, 0)


def test_an_inactive_sku_or_product_is_rejected_listing_all_in_request_order(
    client, engine, caller
):
    active, inactive_sku, inactive_product = add_skus(engine, caller.tenant_id, 3)
    for sku in (active, inactive_sku, inactive_product):
        stock(engine, caller, sku, 5)
    deactivate_sku(engine, inactive_sku)
    deactivate_product_of(engine, inactive_product)
    body = order(
        [line("l1", inactive_product, 1), line("l2", active, 1), line("l3", inactive_sku, 1)]
    )
    error = error_of(reserve(client, caller, body), 409)
    assert error["code"] == "SKU_INACTIVE"
    assert error["details"] == {"sku_ids": [inactive_product, inactive_sku]}
    assert_nothing_reserved(engine, caller)


def test_a_merged_sku_is_rejected(client, engine, caller):
    merged, canonical = add_skus(engine, caller.tenant_id, 2)
    stock(engine, caller, merged, 5)
    stock(engine, caller, canonical, 5)
    merge(engine, merged, canonical)
    body = order([line("l1", canonical, 1), line("l2", merged, 1)])
    error = error_of(reserve(client, caller, body), 409)
    assert error["code"] == "SKU_MERGED"
    assert error["details"] == {"sku_id": merged, "merged_into_sku_id": canonical}
    assert_nothing_reserved(engine, caller)


def test_a_missing_or_foreign_sku_is_not_found(client, engine, caller):
    [own] = add_skus(engine, caller.tenant_id, 1)
    stock(engine, caller, own, 5)
    with engine.begin() as db:
        foreign = add_sku(db, add_tenant(db))
    missing = f"sku_{uuid4().hex[:26].upper()}"
    body = order([line("l1", foreign, 1), line("l2", own, 1), line("l3", missing, 1)])
    response = reserve(client, caller, body)
    error = error_of(response, 404)
    assert error["code"] == "SKU_NOT_FOUND"
    assert error["details"] == {"sku_ids": [foreign, missing]}
    # 原来的拒绝是确定结果：第 3 步登记的 cross_tenant 保留。
    [event] = events(engine, rid_of(response))
    assert event["event_type"] == "cross_tenant"
    assert event["details"] == {"object_type": "sku", "id": foreign}
    assert_nothing_reserved(engine, caller)


def test_a_pool_that_is_not_live_is_rejected(client, engine):
    caller = issue(engine, scopes=ORDER_SCOPES)
    [sku] = add_skus(engine, caller.tenant_id, 1)
    stock(engine, caller, sku, 5)
    error = error_of(reserve(client, caller, order([line("l1", sku, 1)])), 409)
    assert error["code"] == "POOL_NOT_LIVE"
    assert_nothing_reserved(engine, caller)


@pytest.mark.parametrize("channel_status", [None, "inactive"])
def test_a_credential_without_a_sales_channel_is_forbidden(client, engine, channel_status):
    caller = live_caller(engine, channel_status=channel_status)
    [sku] = add_skus(engine, caller.tenant_id, 1)
    stock(engine, caller, sku, 5)
    response = reserve(client, caller, order([line("l1", sku, 1)]))
    assert error_of(response, 403)["code"] == "FORBIDDEN"
    [event] = events(engine, rid_of(response))
    assert event["event_type"] == "forbidden"
    assert event["details"] == {"category": "pool", "pool_id": None}
    assert_nothing_reserved(engine, caller)


# --- 正文 ---


@pytest.mark.parametrize(
    "case",
    [
        "duplicate_sku",
        "duplicate_line_ref",
        "zero_quantity",
        "boolean_quantity",
        "string_quantity",
        "too_large_quantity",
        "unknown_field",
        "unknown_line_field",
        "bad_order_ref",
        "bad_line_ref",
        "no_lines",
    ],
)
def test_an_invalid_body_is_rejected(client, engine, caller, case):
    first, second = add_skus(engine, caller.tenant_id, 2)
    lines = [line("l1", first, 1), line("l2", second, 1)]
    body = order(lines)
    if case == "duplicate_sku":
        lines[1]["sku_id"] = first
    elif case == "duplicate_line_ref":
        lines[1]["line_ref"] = "l1"
    elif case == "zero_quantity":
        lines[0]["quantity"] = 0
    elif case == "boolean_quantity":
        lines[0]["quantity"] = True
    elif case == "string_quantity":
        lines[0]["quantity"] = "1"
    elif case == "too_large_quantity":
        lines[0]["quantity"] = 1_000_001
    elif case == "unknown_field":
        body["ttl_seconds"] = 3600
    elif case == "unknown_line_field":
        lines[0]["price"] = 1
    elif case == "bad_order_ref":
        body["order_ref"] = "order 1"
    elif case == "bad_line_ref":
        lines[0]["line_ref"] = "l" * 129
    else:
        assert case == "no_lines"
        body["lines"] = []
    assert error_of(reserve(client, caller, body), 422)["code"] == "INVALID_REQUEST"
    assert_nothing_reserved(engine, caller)


def test_more_than_100_lines_is_too_large(client, engine, caller):
    body = order([line(f"l{index}", f"sku-{index}", 1) for index in range(101)])
    assert error_of(reserve(client, caller, body), 413)["code"] == "REQUEST_TOO_LARGE"
    assert_nothing_reserved(engine, caller)


# --- 业务引用 ---


def test_the_same_order_on_a_new_key_answers_with_the_current_reservation(client, engine, caller):
    [sku] = add_skus(engine, caller.tenant_id, 1)
    stock(engine, caller, sku, 5)
    body = order([line("l1", sku, 2)])
    created = data_of(reserve(client, caller, body), 201)

    again = reserve(client, caller, body)
    assert data_of(again, 200) == created
    assert "Idempotency-Replayed" not in again.headers
    assert events(engine, rid_of(again)) == []

    changed = body | {"lines": [line("l1", sku, 3)]}
    conflict = reserve(client, caller, changed)
    error = error_of(conflict, 409)
    assert error["code"] == "ORDER_REFERENCE_CONFLICT"
    assert error["details"] == {"ref_type": "order", "ref": body["order_ref"]}
    [event] = events(engine, rid_of(conflict))
    assert event["event_type"] == "business_ref_conflict"
    assert event["details"] == {"ref_type": "order", "ref": body["order_ref"]}

    # 只有一个预留，可售量只扣了一次。
    assert len(rows_of(engine, Reservation, caller)) == 1
    assert quantities_of(engine, caller, sku) == (5, 2, 0, 0)
    assert len(entries(engine, caller)) == 1

    # 到期之后同内容重报也只返回原预留（显示为 expired），不能续期。
    shift_into_the_past(engine, created["reservation_id"])
    expired = data_of(reserve(client, caller, body), 200)
    assert expired["reservation_id"] == created["reservation_id"]
    assert expired["status"] == "expired"
    assert len(rows_of(engine, Reservation, caller)) == 1


def test_a_concurrent_reservation_of_the_same_order_answers_with_the_winner(
    client, engine, caller, monkeypatch
):
    [sku] = add_skus(engine, caller.tenant_id, 1)
    # 可售量只够一单。
    stock(engine, caller, sku, 3)
    body = order([line("l1", sku, 2)])
    first_key, second_key = new_key(), new_key()
    second_looked_up = threading.Event()
    first_finished = threading.Event()
    passed: list[str] = []
    real_require_live = reservation_service.require_live

    def gated(execution, pool_id):
        # 第 1 步（按 order_ref 查预留）之后、锁余额行之前。
        key = execution.access.idempotency_key
        passed.append(key)
        if key == first_key:
            # 后到者预建完余额行、查过 order_ref（没有）之后，先到者才锁余额行并提交。
            assert second_looked_up.wait(10)
        else:
            second_looked_up.set()
            assert first_finished.wait(10)
        real_require_live(execution, pool_id)

    monkeypatch.setattr(reservation_service, "require_live", gated)
    other_client = TestClient(client.app)
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            first_future = pool.submit(reserve, client, caller, body, first_key)
            second_future = pool.submit(reserve, other_client, caller, body, second_key)
            first = first_future.result(timeout=20)
        finally:
            first_finished.set()
        second = second_future.result(timeout=20)

    first_data = data_of(first, 201)
    # 两个请求都在第 1 步没查到预留；后到者锁到余额行时可售量已被用掉，
    # 再查 order_ref 读到先到者的预留，按第 1 步回答。
    assert sorted(passed) == sorted([first_key, second_key])
    assert data_of(second, 200) == first_data
    assert events(engine, rid_of(first)) == []
    assert events(engine, rid_of(second)) == []
    assert len(rows_of(engine, Reservation, caller)) == 1
    assert quantities_of(engine, caller, sku) == (3, 2, 0, 0)
    assert len(entries(engine, caller)) == 1

    # 重新判定的结果是后到者那个键的确定结果。
    replay = reserve(client, caller, body, second_key)
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == second.json()


# --- 查询 ---


def test_queries_return_the_representation_and_show_a_due_reservation_as_expired(
    client, engine, caller
):
    [sku] = add_skus(engine, caller.tenant_id, 1)
    stock(engine, caller, sku, 5)
    body = order([line("l1", sku, 2)])
    created = data_of(reserve(client, caller, body), 201)
    reservation_id = created["reservation_id"]

    assert data_of(get_reservation(client, caller, reservation_id), 200) == created
    found = data_of(find_reservations(client, caller, {"order_ref": body["order_ref"]}), 200)
    assert found == {"reservations": [created]}
    nothing = find_reservations(client, caller, {"order_ref": new_ref()})
    assert data_of(nothing, 200) == {"reservations": []}

    shift_into_the_past(engine, reservation_id)
    by_id = data_of(get_reservation(client, caller, reservation_id), 200)
    found = data_of(find_reservations(client, caller, {"order_ref": body["order_ref"]}), 200)
    [by_ref] = found["reservations"]
    for shown in (by_id, by_ref):
        assert shown["status"] == "expired"
        assert shown["released_at"] is None
        assert shown["release_reason"] is None
    # 读接口不写库：库里仍是 active，reserved 没放回。
    [stored] = rows_of(engine, Reservation, caller)
    assert stored["status"] == "active"
    assert quantities_of(engine, caller, sku) == (5, 2, 0, 0)


def test_a_reservation_of_another_tenant_is_not_found(client, engine, caller):
    [sku] = add_skus(engine, caller.tenant_id, 1)
    stock(engine, caller, sku, 5)
    created = data_of(reserve(client, caller, order([line("l1", sku, 1)])), 201)
    reservation_id = created["reservation_id"]
    other = live_caller(engine)

    foreign = get_reservation(client, other, reservation_id)
    error = error_of(foreign, 404)
    assert error["code"] == "RESERVATION_NOT_FOUND"
    [event] = events(engine, rid_of(foreign))
    assert event["event_type"] == "cross_tenant"
    assert event["details"] == {"object_type": "reservation", "id": reservation_id}

    missing = get_reservation(client, other, f"rsv_{uuid4().hex[:26].upper()}")
    assert error_of(missing, 404) == error
    assert events(engine, rid_of(missing)) == []


def test_a_credential_without_a_sales_channel_finds_no_reservation(client, engine, caller):
    [sku] = add_skus(engine, caller.tenant_id, 1)
    stock(engine, caller, sku, 5)
    body = order([line("l1", sku, 1)])
    data_of(reserve(client, caller, body), 201)
    without_channel = live_caller(engine, channel_status=None)
    response = find_reservations(client, without_channel, {"order_ref": body["order_ref"]})
    assert data_of(response, 200) == {"reservations": []}


@pytest.mark.parametrize(
    "params", [{}, {"order_ref": "order 1"}, {"order_ref": "o" * 129}, {"ref": "order-1"}]
)
def test_a_missing_or_malformed_order_ref_is_rejected(client, caller, params):
    response = find_reservations(client, caller, params)
    assert error_of(response, 422)["code"] == "INVALID_REQUEST"


# --- 配置（§5.6，Q-53） ---


@pytest.mark.parametrize("value", ["59", "3601", "abc", "900.0", "", " 900", "-900"])
def test_an_invalid_ttl_fails_the_settings(monkeypatch, value):
    monkeypatch.setenv(TTL_VARIABLE, value)
    with pytest.raises(ValidationError):
        Settings(env="demo")


def test_the_ttl_defaults_to_900_and_accepts_the_bounds(monkeypatch):
    monkeypatch.delenv(TTL_VARIABLE, raising=False)
    assert Settings(env="demo").reservation_ttl_seconds == 900
    for value in (60, 3600):
        monkeypatch.setenv(TTL_VARIABLE, str(value))
        assert Settings(env="demo").reservation_ttl_seconds == value


def test_create_app_logs_the_ttl_in_effect(migrated_engine, caplog):
    caplog.set_level(logging.INFO, logger="app.api")
    test_client = make_client(reservation_ttl_seconds=120)
    test_client.app.state.engine.dispose()
    [record] = [r for r in caplog.records if "reservation ttl" in r.getMessage()]
    assert record.name == "app.api"
    assert record.getMessage() == "reservation ttl: 120 seconds"


def test_the_cli_names_the_ttl_when_the_configuration_is_invalid(monkeypatch, capsys):
    monkeypatch.setenv("CORE_ENV", "demo")
    monkeypatch.setenv(TTL_VARIABLE, "59")
    assert run_process() == EXIT_FAILURE
    err = capsys.readouterr().err
    assert err.startswith("error: invalid configuration")
    assert TTL_VARIABLE in err
