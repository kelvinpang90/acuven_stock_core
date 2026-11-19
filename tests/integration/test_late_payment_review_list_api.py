"""待审列表 GET /v1/reservations?status=late_payment_review（契约 §5.9，§2.5、§5.6）。

在真 MySQL 上跑；每个测试建自己的租户、池、销售入口与凭据，池先切到 live，预留经接口建立。
待审由晚到确认可售不足转入（经接口，同 test_late_payment_review_api.py 的 order_in_review）。
"""

from collections.abc import Iterator
from datetime import datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.domain.reconciliation import format_reconciliation_cursor, format_review_cursor
from app.domain.service_auth import ORDERS_WRITE
from tests.integration.test_api_auth import Caller, error_of, events
from tests.integration.test_idempotency import rid_of
from tests.integration.test_late_payment_review_api import close, order_in_review
from tests.integration.test_order_stock_api import channel_of, credential_for, other_channel
from tests.integration.test_reservation_release_api import reserved_order
from tests.integration.test_reservations_api import (
    RESERVATION_KEYS,
    data_of,
    get_reservation,
    live_caller,
    make_client,
)

RESERVATIONS = "/v1/reservations"
REVIEW = {"status": "late_payment_review"}
# 一个形状合格的预留 ID（Crockford Base32 大写 26 位）。
SOME_RESERVATION = "rsv_01ARZ3NDEKTSV4RRFFQ69G5FAV"


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


# --- 请求 ---


def list_reservations(client: TestClient, caller: Caller, params=None):
    headers = caller.auth | {"X-Request-ID": f"review-list-{uuid4().hex}"}
    return client.get(RESERVATIONS, params=params, headers=headers)


def ids_of(data: dict) -> list[str]:
    return [reservation["reservation_id"] for reservation in data["reservations"]]


def invalid_parameter(response, parameter: str) -> None:
    error = error_of(response, 422)
    assert error["code"] == "INVALID_REQUEST"
    assert error["details"] == {"parameter": parameter}


# --- 列表 ---


def test_only_reservations_in_review_are_listed(client, engine, caller):
    in_review = [order_in_review(client, engine, caller)[0] for _ in range(2)]
    reserved_order(client, engine, caller, [1])
    # 别的入口的待审不出现。
    other = other_channel(engine, caller)
    theirs, _, _ = order_in_review(client, engine, other)

    response = list_reservations(client, caller, REVIEW)
    data = data_of(response, 200)
    assert set(data) == {"reservations", "next_cursor"}
    expected = sorted(created["reservation_id"] for created in in_review)
    assert ids_of(data) == expected
    for shown in data["reservations"]:
        assert set(shown) == RESERVATION_KEYS
        assert shown["status"] == "late_payment_review"
        # 现有的表示：与按 ID 查相同。
        assert shown == data_of(get_reservation(client, caller, shown["reservation_id"]), 200)
    assert data["next_cursor"] == format_review_cursor(expected[-1], "late_payment_review")
    assert events(engine, rid_of(response)) == []

    assert ids_of(data_of(list_reservations(client, other, REVIEW), 200)) == [
        theirs["reservation_id"]
    ]


def test_pages_follow_reservation_id_and_closed_reviews_drop_out(client, engine, caller):
    in_review = [order_in_review(client, engine, caller)[0] for _ in range(3)]
    expected = sorted(created["reservation_id"] for created in in_review)

    def page(after: str | None) -> dict:
        params = REVIEW | {"limit": "2"} | ({} if after is None else {"after": after})
        return data_of(list_reservations(client, caller, params), 200)

    first = page(None)
    after = first["next_cursor"]
    assert after == format_review_cursor(expected[1], "late_payment_review")
    second = page(after)
    assert ids_of(first) + ids_of(second) == expected
    assert len(ids_of(second)) == 1
    last = second["next_cursor"]
    assert last == format_review_cursor(expected[2], "late_payment_review")

    # 最后一页之后再翻：空数组，next_cursor 等于请求的 after。
    assert page(last) == {"reservations": [], "next_cursor": last}

    # 结案之后不再出现。
    closed = next(c for c in in_review if c["reservation_id"] == expected[0])
    data_of(close(client, caller, closed["order_ref"]), 200)
    remaining = data_of(list_reservations(client, caller, REVIEW), 200)
    assert ids_of(remaining) == expected[1:]


def test_no_reviews_returns_null_cursor(client, engine, caller):
    reserved_order(client, engine, caller, [1])
    assert data_of(list_reservations(client, caller, REVIEW), 200) == {
        "reservations": [],
        "next_cursor": None,
    }


def test_the_order_ref_query_is_unchanged(client, engine, caller):
    created, _, _ = order_in_review(client, engine, caller)
    found = data_of(list_reservations(client, caller, {"order_ref": created["order_ref"]}), 200)
    assert set(found) == {"reservations"}
    [shown] = found["reservations"]
    assert shown["reservation_id"] == created["reservation_id"]
    assert shown["status"] == "late_payment_review"
    # 带 order_ref 时仍不收 after、limit。
    for extra in ({"limit": "2"}, {"after": format_review_cursor(SOME_RESERVATION, "x")}):
        response = list_reservations(client, caller, {"order_ref": created["order_ref"]} | extra)
        assert error_of(response, 422)["code"] == "INVALID_REQUEST"


# --- 拒绝 ---


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"order_ref": "order-1", "status": "late_payment_review"},
        {"limit": "2"},
        {"after": format_review_cursor(SOME_RESERVATION, "late_payment_review")},
        REVIEW | {"order_ref": "order-1"},
        REVIEW | {"cursor": "x"},
        REVIEW | {"changed_from": "2026-01-01T00:00:00Z"},
        [("status", "late_payment_review"), ("status", "late_payment_review")],
        [("status", "late_payment_review"), ("limit", "2"), ("limit", "2")],
    ],
)
def test_missing_both_unknown_or_repeated_parameters_are_rejected(client, caller, params):
    assert error_of(list_reservations(client, caller, params), 422)["code"] == "INVALID_REQUEST"


@pytest.mark.parametrize(
    "status", ["active", "committed", "LATE_PAYMENT_REVIEW", "", "late_payment_review "]
)
def test_other_status_values_are_rejected(client, caller, status):
    invalid_parameter(list_reservations(client, caller, {"status": status}), "status")


@pytest.mark.parametrize("limit", ["0", "101", "abc", "", "-1", "01", "1.5"])
def test_an_invalid_limit_is_rejected(client, caller, limit):
    invalid_parameter(list_reservations(client, caller, REVIEW | {"limit": limit}), "limit")


@pytest.mark.parametrize(
    "after",
    [
        "",
        "abc",
        f"v1.{SOME_RESERVATION}",
        f"v2.{SOME_RESERVATION}.0123456789abcdef",
        "v1.rsv_x.0123456789abcdef",
        f"v1.{SOME_RESERVATION}.0123456789ABCDEF",
        f"v1.{SOME_RESERVATION}.0123456789abcde",
        # 对账查询的游标形状不对。
        format_reconciliation_cursor(
            datetime(2026, 1, 1), SOME_RESERVATION, datetime(2026, 1, 1), datetime(2026, 1, 2)
        ),
    ],
)
def test_a_malformed_cursor_is_rejected(client, caller, after):
    invalid_parameter(list_reservations(client, caller, REVIEW | {"after": after}), "after")


def test_a_cursor_of_another_status_is_rejected(client, engine, caller):
    order_in_review(client, engine, caller)
    foreign = format_review_cursor(SOME_RESERVATION, "active")
    response = list_reservations(client, caller, REVIEW | {"after": foreign})
    invalid_parameter(response, "after")
    assert events(engine, rid_of(response)) == []
    # 格式不对先于游标与 status 不符。
    invalid_parameter(
        list_reservations(client, caller, REVIEW | {"limit": "0", "after": foreign}), "limit"
    )


def test_the_list_needs_orders_read(client, engine, caller):
    writer = credential_for(
        engine, caller.tenant_id, caller.pool_id, channel_of(engine, caller), [ORDERS_WRITE]
    )
    response = list_reservations(client, writer, REVIEW)
    assert error_of(response, 403)["code"] == "FORBIDDEN"
    [event] = events(engine, rid_of(response))
    assert event["event_type"] == "forbidden"
    assert event["details"] == {"category": "scope", "missing_scopes": ["orders.read"]}


# --- 没有销售入口 ---


@pytest.mark.parametrize("channel_status", [None, "inactive"])
def test_a_credential_without_a_sales_channel_gets_an_empty_list(
    client, engine, caller, channel_status
):
    order_in_review(client, engine, caller)
    without_channel = live_caller(engine, channel_status=channel_status)
    after = format_review_cursor(SOME_RESERVATION, "late_payment_review")

    response = list_reservations(client, without_channel, REVIEW)
    assert data_of(response, 200) == {"reservations": [], "next_cursor": None}
    assert events(engine, rid_of(response)) == []
    with_after = list_reservations(client, without_channel, REVIEW | {"after": after})
    assert data_of(with_after, 200) == {"reservations": [], "next_cursor": after}

    # 参数不合格时仍是 422。
    invalid_parameter(list_reservations(client, without_channel, {"status": "active"}), "status")
    invalid_parameter(
        list_reservations(client, without_channel, REVIEW | {"limit": "101"}), "limit"
    )
    foreign = format_review_cursor(SOME_RESERVATION, "active")
    invalid_parameter(
        list_reservations(client, without_channel, REVIEW | {"after": foreign}), "after"
    )
    both = REVIEW | {"order_ref": "order-1"}
    assert error_of(list_reservations(client, without_channel, both), 422)["code"] == (
        "INVALID_REQUEST"
    )
