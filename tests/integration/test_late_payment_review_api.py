"""待审结案（契约 §5.7 待审结案与表示、§7.1、§8.1 第 7 步）。

在真 MySQL 上跑；每个测试建自己的租户、池、销售入口与凭据，池先切到 live，预留经接口建立。
待审由晚到确认可售不足转入（经接口）；到期关单由测试把预留挪到过去再调 close_if_due。
"""

from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.models.orders import Reservation
from app.services.stock import format_timestamp
from tests.integration.test_api_auth import Caller, error_of, events
from tests.integration.test_idempotency import records, rid_of
from tests.integration.test_reservation_release_api import release, reserved_order
from tests.integration.test_reservations_api import (
    ACTOR,
    RESERVATION_KEYS,
    data_of,
    get_reservation,
    live_caller,
    make_client,
    new_ref,
    rows_of,
    shift_into_the_past,
)
from tests.integration.test_sale_commit_late_api import expired_order, set_unavailable
from tests.integration.test_sale_commits_api import commit, commit_body, stored_reservation
from tests.integration.test_stock_service import entries, quantities_of

LATE_PAYMENT_REVIEWS_CLOSE = "late_payment_reviews.close"


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


def new_key() -> str:
    return f"shop:review-close:{uuid4().hex}"


def close(
    client: TestClient,
    caller: Caller,
    order_ref: str,
    key: str | None = None,
    actor: dict | None = None,
    body: bytes | None = None,
):
    headers = caller.auth | {
        "Idempotency-Key": key or new_key(),
        "X-Request-ID": f"review-close-test-{uuid4().hex}",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    path = f"/v1/orders/{order_ref}/late-payment-review/close"
    return client.post(path, content=body, headers=headers | (actor or {}))


def order_in_review(client: TestClient, engine: Engine, caller: Caller):
    """到期关单后晚到付款、可售不足：预留转入待审。返回预留的表示、SKU 与确认正文。"""
    created, (sku,) = expired_order(client, engine, caller, [2])
    # 账面 5 件，4 件不可用：可售 1 件，不够这一行的 2 件。
    set_unavailable(engine, caller, sku, 4)
    body = commit_body(created)
    error = error_of(commit(client, caller, created["order_ref"], body), 409)
    assert error["code"] == "LATE_PAYMENT_INSUFFICIENT_STOCK"
    assert stored_reservation(engine, caller, created["reservation_id"])["status"] == (
        "late_payment_review"
    )
    return created, sku, body


# --- 结案 ---


def test_closing_a_review_records_closed_refunded_without_entries(client, engine, caller):
    created, sku, paid = order_in_review(client, engine, caller)
    reservation_id = created["reservation_id"]
    before_entries = entries(engine, caller)
    before_quantities = quantities_of(engine, caller, sku)
    before = stored_reservation(engine, caller, reservation_id)

    response = close(client, caller, created["order_ref"], actor=ACTOR)
    data = data_of(response, 200)

    assert set(data) == RESERVATION_KEYS
    assert data["reservation_id"] == reservation_id
    assert data["status"] == "closed_refunded"
    assert data["review_closed_at"] is not None
    assert data["payment_ref"] == paid["payment_ref"]
    assert data["paid_at"] == "2026-11-18T02:00:00.000000Z"
    assert data["committed_at"] is None
    assert data["late"] is False
    assert data["lines"] == created["lines"]

    stored = stored_reservation(engine, caller, reservation_id)
    assert stored["status"] == "closed_refunded"
    assert data["review_closed_at"] == format_timestamp(stored["review_closed_at"])
    assert stored["review_closed_credential_id"] == caller.credential_id
    assert stored["review_closed_request_id"] == rid_of(response)
    assert (stored["review_closed_actor_ref"], stored["review_closed_actor_role"]) == (
        ACTOR["X-Actor-Ref"],
        ACTOR["X-Actor-Role"],
    )
    # 付款、确认与关单的列不变。
    for column in ("payment_ref", "paid_at", "commit_request_hash", "commit_request_id"):
        assert stored[column] == before[column]
    assert stored["closed_at"] == before["closed_at"]

    # 不写流水，数量不变。
    assert entries(engine, caller) == before_entries
    assert quantities_of(engine, caller, sku) == before_quantities
    # 查询返回同样的表示。
    assert data_of(get_reservation(client, caller, reservation_id), 200) == data


def test_closing_without_an_actor_header_leaves_the_actor_empty(client, engine, caller):
    created, sku, paid = order_in_review(client, engine, caller)
    response = close(client, caller, created["order_ref"])
    assert data_of(response, 200)["status"] == "closed_refunded"

    stored = stored_reservation(engine, caller, created["reservation_id"])
    assert stored["review_closed_credential_id"] == caller.credential_id
    assert stored["review_closed_request_id"] == rid_of(response)
    assert stored["review_closed_actor_ref"] is None
    assert stored["review_closed_actor_role"] is None


# --- 重复与状态 ---


def test_closing_again_writes_nothing(client, engine, caller):
    created, sku, paid = order_in_review(client, engine, caller)
    order_ref = created["order_ref"]
    key = new_key()
    first = close(client, caller, order_ref, key, ACTOR)
    closed = data_of(first, 200)
    stored = stored_reservation(engine, caller, created["reservation_id"])
    before_entries = entries(engine, caller)

    # 同键原样重放。
    replay = close(client, caller, order_ref, key, ACTOR)
    assert replay.status_code == 200
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == first.json()

    # 换键：200 与当前表示，不改结案的列。
    again = close(client, caller, order_ref)
    assert data_of(again, 200) == closed
    assert "Idempotency-Replayed" not in again.headers
    assert stored_reservation(engine, caller, created["reservation_id"]) == stored
    assert entries(engine, caller) == before_entries


def test_committing_after_closing_is_a_state_conflict(client, engine, caller):
    created, sku, paid = order_in_review(client, engine, caller)
    data_of(close(client, caller, created["order_ref"]), 200)
    # 补了货也不再扣：网店已退款结案。
    set_unavailable(engine, caller, sku, 0)
    before_entries = entries(engine, caller)

    error = error_of(commit(client, caller, created["order_ref"], paid), 409)
    assert error["code"] == "STATE_CONFLICT"
    assert error["details"] == {"status": "closed_refunded"}
    assert entries(engine, caller) == before_entries
    assert quantities_of(engine, caller, sku) == (5, 0, 0, 0)
    assert stored_reservation(engine, caller, created["reservation_id"])["status"] == (
        "closed_refunded"
    )


def _active(client, engine, caller):
    created, skus = reserved_order(client, engine, caller, [2])
    return created


def _due(client, engine, caller):
    created, skus = reserved_order(client, engine, caller, [2])
    shift_into_the_past(engine, created["reservation_id"])
    return created


def _expired(client, engine, caller):
    created, skus = expired_order(client, engine, caller, [2])
    return created


def _released(client, engine, caller):
    created, skus = reserved_order(client, engine, caller, [2])
    data_of(release(client, caller, created["reservation_id"], {"reason": "cancelled"}), 200)
    return created


def _committed(client, engine, caller):
    created, skus = reserved_order(client, engine, caller, [2])
    data_of(commit(client, caller, created["order_ref"], commit_body(created)), 200)
    return created


@pytest.mark.parametrize(
    ("prepare", "displayed", "stored_status"),
    [
        (_active, "active", "active"),
        # 已到期未关的 active 报表示里的 expired，结案不替它关单。
        (_due, "expired", "active"),
        (_expired, "expired", "expired"),
        (_released, "released", "released"),
        (_committed, "committed", "committed"),
    ],
)
def test_other_states_are_a_state_conflict(
    client, engine, caller, prepare, displayed, stored_status
):
    created = prepare(client, engine, caller)
    before = stored_reservation(engine, caller, created["reservation_id"])
    before_entries = entries(engine, caller)
    key = new_key()

    response = close(client, caller, created["order_ref"], key)
    error = error_of(response, 409)
    assert error["code"] == "STATE_CONFLICT"
    assert error["details"] == {"status": displayed}
    assert events(engine, rid_of(response)) == []

    stored = stored_reservation(engine, caller, created["reservation_id"])
    assert stored == before
    assert stored["status"] == stored_status
    assert stored["review_closed_at"] is None
    assert entries(engine, caller) == before_entries
    # 不写东西的拒绝只保存幂等记录。
    assert len(records(engine, caller, key, LATE_PAYMENT_REVIEWS_CLOSE)) == 1


# --- 拒绝 ---


def test_an_order_without_a_reservation_is_not_found(client, engine, caller):
    key = new_key()
    response = close(client, caller, new_ref(), key)
    error = error_of(response, 404)
    assert error["code"] == "RESERVATION_NOT_FOUND"
    assert events(engine, rid_of(response)) == []
    assert rows_of(engine, Reservation, caller) == []
    assert entries(engine, caller) == []
    assert len(records(engine, caller, key, LATE_PAYMENT_REVIEWS_CLOSE)) == 1


@pytest.mark.parametrize("channel_status", [None, "inactive"])
def test_a_credential_without_a_sales_channel_is_forbidden(client, engine, caller, channel_status):
    created, sku, paid = order_in_review(client, engine, caller)
    without_channel = live_caller(engine, channel_status=channel_status)
    key = new_key()

    response = close(client, without_channel, created["order_ref"], key)
    assert error_of(response, 403)["code"] == "FORBIDDEN"
    [event] = events(engine, rid_of(response))
    assert event["event_type"] == "forbidden"
    assert event["details"] == {"category": "pool", "pool_id": None}
    assert records(engine, without_channel, key, LATE_PAYMENT_REVIEWS_CLOSE) == []
    assert stored_reservation(engine, caller, created["reservation_id"])["status"] == (
        "late_payment_review"
    )


@pytest.mark.parametrize("body", [b"{}", b'{"refund_ref": "refund-1"}', b"null"])
def test_a_request_body_is_rejected(client, engine, caller, body):
    created, sku, paid = order_in_review(client, engine, caller)
    key = new_key()
    response = close(client, caller, created["order_ref"], key, body=body)
    assert error_of(response, 422)["code"] == "INVALID_REQUEST"
    assert records(engine, caller, key, LATE_PAYMENT_REVIEWS_CLOSE) == []
    assert stored_reservation(engine, caller, created["reservation_id"])["status"] == (
        "late_payment_review"
    )


@pytest.mark.parametrize("order_ref", ["o" * 129, "order 1"])
def test_an_invalid_order_ref_is_rejected(client, engine, caller, order_ref):
    key = new_key()
    response = close(client, caller, order_ref, key)
    assert error_of(response, 422)["code"] == "INVALID_REQUEST"
    assert records(engine, caller, key, LATE_PAYMENT_REVIEWS_CLOSE) == []
