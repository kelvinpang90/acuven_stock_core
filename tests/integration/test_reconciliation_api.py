"""对账查询：选取、分页、拒绝与没有销售入口（契约 §5.9，§2.5）。

在真 MySQL 上跑；每个测试建自己的租户、池、销售入口与凭据，池先切到 live，
预留、确认销售与发货经接口建立；last_changed_at 由测试直接改库放进过去的固定窗口
（窗口终点要早于数据库时间减 300 秒）。
"""

from collections.abc import Iterator
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, update

from app.domain.reconciliation import format_reconciliation_cursor, format_review_cursor
from app.domain.service_auth import ORDERS_WRITE
from app.models.orders import Reservation
from app.services.reservations import database_now
from app.services.stock import format_timestamp
from tests.integration.conftest import qline
from tests.integration.test_api_auth import Caller, error_of, events
from tests.integration.test_idempotency import rid_of
from tests.integration.test_order_stock_api import (
    channel_of,
    committed,
    credential_for,
    dispatch,
    order_stock,
    other_channel,
)
from tests.integration.test_reservation_release_api import reserved_order
from tests.integration.test_reservations_api import (
    RESERVATION_KEYS,
    data_of,
    live_caller,
    make_client,
    rows_of,
    shift_into_the_past,
)

RECONCILIATION = "/v1/reconciliation/orders"
FROM = datetime(2026, 1, 1)
TO = datetime(2026, 1, 2)
WINDOW = {"changed_from": "2026-01-01T00:00:00Z", "changed_to": "2026-01-02T00:00:00Z"}
# 一个形状合格的预留 ID（Crockford Base32 大写 26 位）。
SOME_RESERVATION = "rsv_01ARZ3NDEKTSV4RRFFQ69G5FAV"
ORDER_KEYS = {"reservation", "last_changed_at", "lines"}


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
    """池已 live、有销售入口的调用方。"""
    return live_caller(engine)


# --- 请求与准备 ---


def reconcile(client: TestClient, caller: Caller, params=None):
    headers = caller.auth | {"X-Request-ID": f"reconcile-{uuid4().hex}"}
    return client.get(RECONCILIATION, params=params, headers=headers)


def set_changed_at(engine: Engine, reservation_id: str, value: datetime) -> None:
    with engine.begin() as db:
        statement = update(Reservation).where(Reservation.reservation_id == reservation_id)
        db.execute(statement.values(last_changed_at=value))


def placed(client, engine, caller: Caller, changed_at: datetime, quantities=(1,)) -> dict:
    """预留一单并把 last_changed_at 设为 changed_at；返回预留的表示。"""
    created, _ = reserved_order(client, engine, caller, list(quantities))
    set_changed_at(engine, created["reservation_id"], changed_at)
    return created


def utc(value: datetime) -> str:
    return value.isoformat(timespec="microseconds") + "Z"


def db_now(engine: Engine) -> datetime:
    with engine.connect() as db:
        return database_now(db)


def invalid_parameter(response, parameter: str) -> None:
    error = error_of(response, 422)
    assert error["code"] == "INVALID_REQUEST"
    assert error["details"] == {"parameter": parameter}


# --- 选取 ---


def test_the_window_includes_its_start_and_excludes_its_end(client, engine, caller):
    at_start = placed(client, engine, caller, FROM)
    inside = placed(client, engine, caller, FROM + timedelta(hours=1))
    just_before_end = placed(client, engine, caller, TO - timedelta(microseconds=1))
    placed(client, engine, caller, TO)
    placed(client, engine, caller, FROM - timedelta(microseconds=1))
    # 别的入口的预留在窗口里也不出现。
    other = other_channel(engine, caller)
    foreign = placed(client, engine, other, FROM + timedelta(hours=2))

    response = reconcile(client, caller, WINDOW)
    data = data_of(response, 200)
    assert set(data) == {"orders", "next_cursor"}
    ids = [item["reservation"]["reservation_id"] for item in data["orders"]]
    assert ids == [
        at_start["reservation_id"],
        inside["reservation_id"],
        just_before_end["reservation_id"],
    ]
    assert [item["last_changed_at"] for item in data["orders"]] == [
        utc(FROM),
        utc(FROM + timedelta(hours=1)),
        utc(TO - timedelta(microseconds=1)),
    ]
    last = data["orders"][-1]
    assert data["next_cursor"] == format_reconciliation_cursor(
        TO - timedelta(microseconds=1), last["reservation"]["reservation_id"], FROM, TO
    )
    assert events(engine, rid_of(response)) == []

    # 别的入口只看到自己的。
    theirs = data_of(reconcile(client, other, WINDOW), 200)["orders"]
    assert [item["reservation"]["reservation_id"] for item in theirs] == [foreign["reservation_id"]]


def test_each_order_matches_the_order_stock_query(client, engine, caller):
    shipped, _ = committed(client, engine, caller, [3, 2])
    dispatch(client, caller, shipped["order_ref"], [qline("l1", 2), qline("l2", 1)])
    set_changed_at(engine, shipped["reservation_id"], FROM + timedelta(hours=1))
    unpaid = placed(client, engine, caller, FROM + timedelta(hours=2), quantities=(2,))
    due = placed(client, engine, caller, FROM + timedelta(hours=3))
    shift_into_the_past(engine, due["reservation_id"])

    data = data_of(reconcile(client, caller, WINDOW), 200)
    assert [item["reservation"]["reservation_id"] for item in data["orders"]] == [
        shipped["reservation_id"],
        unpaid["reservation_id"],
        due["reservation_id"],
    ]
    for item in data["orders"]:
        assert set(item) == ORDER_KEYS
        assert set(item["reservation"]) == RESERVATION_KEYS
        expected = data_of(order_stock(client, caller, item["reservation"]["order_ref"]), 200)
        assert item["reservation"] == expected["reservation"]
        assert item["lines"] == expected["lines"]

    first, second, third = data["orders"]
    # 经发货的订单显示发货件数。
    assert first["reservation"]["status"] == "committed"
    shipped_lines = [
        (row["line_ref"], row["committed_qty"], row["dispatched_qty"]) for row in first["lines"]
    ]
    assert shipped_lines == [("l1", 3, 2), ("l2", 2, 1)]
    assert second["reservation"]["status"] == "active"
    assert [row["committed_qty"] for row in second["lines"]] == [0]
    # 已到期还没关掉的 active 显示为 expired；查询不写库。
    assert third["reservation"]["status"] == "expired"
    stored = {row["reservation_id"]: row for row in rows_of(engine, Reservation, caller)}
    assert stored[due["reservation_id"]]["status"] == "active"
    assert stored[due["reservation_id"]]["last_changed_at"] == FROM + timedelta(hours=3)


# --- 分页 ---


def test_pages_follow_last_changed_at_then_reservation_id(client, engine, caller):
    # 三单同一时刻：按 reservation_id 排；另两单一早一晚。
    same = FROM + timedelta(hours=5)
    late = placed(client, engine, caller, FROM + timedelta(hours=9))
    tied = [placed(client, engine, caller, same) for _ in range(3)]
    early = placed(client, engine, caller, FROM + timedelta(hours=1))
    expected = [
        early["reservation_id"],
        *sorted(order["reservation_id"] for order in tied),
        late["reservation_id"],
    ]

    pages = []
    after = None
    for _ in range(3):
        params = WINDOW | {"limit": "2"} | ({} if after is None else {"after": after})
        page = data_of(reconcile(client, caller, params), 200)
        pages.append([item["reservation"]["reservation_id"] for item in page["orders"]])
        after = page["next_cursor"]
    assert [len(page) for page in pages] == [2, 2, 1]
    assert [rid for page in pages for rid in page] == expected
    assert after == format_reconciliation_cursor(
        FROM + timedelta(hours=9), late["reservation_id"], FROM, TO
    )

    # 最后一页之后再翻：空数组，next_cursor 等于请求的 after。
    beyond = data_of(reconcile(client, caller, WINDOW | {"limit": "2", "after": after}), 200)
    assert beyond == {"orders": [], "next_cursor": after}

    # 不带 limit 为 100：一页取完。
    whole = data_of(reconcile(client, caller, WINDOW), 200)
    assert [item["reservation"]["reservation_id"] for item in whole["orders"]] == expected
    assert whole["next_cursor"] == after


def test_an_empty_window_returns_null_cursor(client, engine, caller):
    placed(client, engine, caller, TO + timedelta(hours=1))
    assert data_of(reconcile(client, caller, WINDOW), 200) == {"orders": [], "next_cursor": None}


# --- 拒绝 ---


@pytest.mark.parametrize(
    ("params", "parameter"),
    [
        ({"changed_to": WINDOW["changed_to"]}, "changed_from"),
        ({"changed_from": WINDOW["changed_from"]}, "changed_to"),
        ({}, "changed_from"),
        (WINDOW | {"changed_from": "2026-01-01T00:00:00"}, "changed_from"),
        (WINDOW | {"changed_to": "2026-01-02T00:00:00"}, "changed_to"),
        (WINDOW | {"changed_from": "2026-01-01"}, "changed_from"),
        (WINDOW | {"changed_from": "1999-12-31T23:59:59.999999Z"}, "changed_from"),
        (
            {"changed_from": "1999-01-01T00:00:00Z", "changed_to": "1999-12-31T00:00:00Z"},
            "changed_from",
        ),
        (WINDOW | {"changed_to": "1999-01-01T00:00:00Z"}, "changed_to"),
        (WINDOW | {"changed_from": WINDOW["changed_to"]}, "changed_from"),
        (WINDOW | {"changed_from": "2026-01-03T00:00:00Z"}, "changed_from"),
        # 换算成 UTC 之后相等，也不早于。
        (WINDOW | {"changed_from": "2026-01-02T08:00:00+08:00"}, "changed_from"),
        (WINDOW | {"limit": "0"}, "limit"),
        (WINDOW | {"limit": "101"}, "limit"),
        (WINDOW | {"limit": "abc"}, "limit"),
        (WINDOW | {"after": ""}, "after"),
        (WINDOW | {"after": "abc"}, "after"),
        (WINDOW | {"after": f"v1.0.{SOME_RESERVATION}.0123456789abcdef"}, "after"),
        (WINDOW | {"after": f"v1.01.{SOME_RESERVATION}.0123456789abcdef"}, "after"),
        (WINDOW | {"after": f"v2.1.{SOME_RESERVATION}.0123456789abcdef"}, "after"),
        (WINDOW | {"after": "v1.1.rsv_x.0123456789abcdef"}, "after"),
        (WINDOW | {"after": f"v1.1.{SOME_RESERVATION}.0123456789ABCDEF"}, "after"),
        (WINDOW | {"after": f"v1.{SOME_RESERVATION}.0123456789abcdef"}, "after"),
    ],
)
def test_malformed_parameters_are_rejected(client, engine, caller, params, parameter):
    placed(client, engine, caller, FROM + timedelta(hours=1))
    invalid_parameter(reconcile(client, caller, params), parameter)


@pytest.mark.parametrize(
    "params",
    [
        WINDOW | {"cursor": "x"},
        WINDOW | {"order_ref": "order-1"},
        [*WINDOW.items(), ("changed_from", WINDOW["changed_from"])],
        [*WINDOW.items(), ("limit", "2"), ("limit", "2")],
        [*WINDOW.items(), ("after", "a"), ("after", "b")],
    ],
)
def test_unknown_or_repeated_query_parameters_are_rejected(client, caller, params):
    assert error_of(reconcile(client, caller, params), 422)["code"] == "INVALID_REQUEST"


def test_a_window_ending_later_than_five_minutes_ago_is_rejected(client, engine, caller):
    now = db_now(engine)
    params = {"changed_from": utc(FROM), "changed_to": utc(now - timedelta(seconds=60))}
    error = error_of(reconcile(client, caller, params), 422)
    assert error["code"] == "INVALID_REQUEST"
    assert set(error["details"]) == {"latest_changed_to"}
    latest = datetime.fromisoformat(error["details"]["latest_changed_to"].removesuffix("Z"))
    assert now - timedelta(seconds=300) <= latest < now - timedelta(seconds=240)
    assert error["details"]["latest_changed_to"] == format_timestamp(latest)

    # 终点早于数据库时间减 300 秒的照常返回。
    early_enough = {"changed_from": utc(FROM), "changed_to": utc(now - timedelta(seconds=400))}
    assert data_of(reconcile(client, caller, early_enough), 200)["next_cursor"] is None


def test_a_cursor_of_another_window_is_rejected(client, engine, caller):
    placed(client, engine, caller, FROM + timedelta(hours=1))
    placed(client, engine, caller, FROM + timedelta(hours=2))
    cursor = data_of(reconcile(client, caller, WINDOW | {"limit": "1"}), 200)["next_cursor"]
    assert cursor is not None

    wider = {"changed_from": "2025-12-31T00:00:00Z", "changed_to": WINDOW["changed_to"]}
    response = reconcile(client, caller, wider | {"after": cursor})
    invalid_parameter(response, "after")
    assert events(engine, rid_of(response)) == []
    # 待审列表的游标形状不对。
    review = format_review_cursor(SOME_RESERVATION, "late_payment_review")
    invalid_parameter(reconcile(client, caller, WINDOW | {"after": review}), "after")
    # 本窗口的游标照常翻页。
    assert len(data_of(reconcile(client, caller, WINDOW | {"after": cursor}), 200)["orders"]) == 1


def test_the_rejections_follow_the_contract_order(client, engine, caller):
    now = db_now(engine)
    too_late = utc(now - timedelta(seconds=60))
    foreign = format_reconciliation_cursor(FROM, SOME_RESERVATION, FROM - timedelta(days=1), TO)

    # 格式（limit、游标）先于先后。
    reversed_window = {"changed_from": WINDOW["changed_to"], "changed_to": WINDOW["changed_from"]}
    invalid_parameter(reconcile(client, caller, reversed_window | {"limit": "0"}), "limit")
    invalid_parameter(reconcile(client, caller, reversed_window | {"after": "x"}), "after")
    # 时间早于 2000 年也是格式，先于先后。
    early = {"changed_from": "2026-01-03T00:00:00Z", "changed_to": "1999-01-01T00:00:00Z"}
    invalid_parameter(reconcile(client, caller, early), "changed_to")
    # 先后先于终点太晚。
    backwards_too_late = {"changed_from": utc(now), "changed_to": too_late}
    invalid_parameter(reconcile(client, caller, backwards_too_late), "changed_from")
    # 终点太晚先于游标与窗口不符。
    late_window = {"changed_from": utc(FROM), "changed_to": too_late, "after": foreign}
    error = error_of(reconcile(client, caller, late_window), 422)
    assert set(error["details"]) == {"latest_changed_to"}
    # 最后才是游标与窗口不符。
    invalid_parameter(reconcile(client, caller, WINDOW | {"after": foreign}), "after")


def test_the_query_needs_orders_read(client, engine, caller):
    writer = credential_for(
        engine, caller.tenant_id, caller.pool_id, channel_of(engine, caller), [ORDERS_WRITE]
    )
    response = reconcile(client, writer, WINDOW)
    assert error_of(response, 403)["code"] == "FORBIDDEN"
    [event] = events(engine, rid_of(response))
    assert event["event_type"] == "forbidden"
    assert event["details"] == {"category": "scope", "missing_scopes": ["orders.read"]}


# --- 没有销售入口 ---


@pytest.mark.parametrize("channel_status", [None, "inactive"])
def test_a_credential_without_a_sales_channel_gets_an_empty_list(
    client, engine, caller, channel_status
):
    placed(client, engine, caller, FROM + timedelta(hours=1))
    without_channel = live_caller(engine, channel_status=channel_status)
    after = format_reconciliation_cursor(FROM, SOME_RESERVATION, FROM, TO)

    response = reconcile(client, without_channel, WINDOW)
    assert data_of(response, 200) == {"orders": [], "next_cursor": None}
    assert events(engine, rid_of(response)) == []
    with_after = reconcile(client, without_channel, WINDOW | {"after": after})
    assert data_of(with_after, 200) == {"orders": [], "next_cursor": after}

    # 参数不合格时仍是 422。
    invalid_parameter(reconcile(client, without_channel, WINDOW | {"limit": "0"}), "limit")
    reversed_window = {"changed_from": WINDOW["changed_to"], "changed_to": WINDOW["changed_from"]}
    invalid_parameter(reconcile(client, without_channel, reversed_window), "changed_from")
    too_late = {"changed_from": utc(FROM), "changed_to": utc(db_now(engine))}
    error = error_of(reconcile(client, without_channel, too_late), 422)
    assert set(error["details"]) == {"latest_changed_to"}
    foreign = format_reconciliation_cursor(FROM, SOME_RESERVATION, FROM - timedelta(days=1), TO)
    invalid_parameter(reconcile(client, without_channel, WINDOW | {"after": foreign}), "after")
