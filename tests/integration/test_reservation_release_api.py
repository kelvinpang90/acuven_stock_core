"""预留的释放（契约 §5.2、§5.3、§5.6、§7.1、§10 第 4 条）。

在真 MySQL 上跑；每个测试建自己的租户、池、销售入口与凭据，池先切到 live，
预留经接口建立；到期、committed 与 SKU 合并的改挂由测试直接改库模拟（自己提交的连接）。
"""

from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, update

from app.models.inventory import StockBalance
from app.models.orders import OrderLine, Reservation
from app.services.stock import format_timestamp
from tests.integration.test_api_auth import Caller, error_of, events
from tests.integration.test_idempotency import add_skus, rid_of
from tests.integration.test_reservations_api import (
    ACTOR,
    RESERVATION_KEYS,
    data_of,
    line,
    live_caller,
    make_client,
    order,
    reserve,
    rows_of,
    shift_into_the_past,
    stock,
)
from tests.integration.test_stock_service import entries, quantities_of


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


# --- 请求与观察 ---


def new_key() -> str:
    return f"shop:release:{uuid4().hex}"


def release(
    client: TestClient,
    caller: Caller,
    reservation_id: str,
    body: object,
    key: str | None = None,
    actor: dict | None = None,
):
    headers = caller.auth | {
        "Idempotency-Key": key or new_key(),
        "X-Request-ID": f"release-test-{uuid4().hex}",
    }
    path = f"/v1/reservations/{reservation_id}/release"
    return client.post(path, json=body, headers=headers | (actor or {}))


def reserved_order(client: TestClient, engine: Engine, caller: Caller, quantities: list[int]):
    """每行一个新 SKU，各备 5 件；返回预留的表示与按行的 SKU。"""
    skus = add_skus(engine, caller.tenant_id, len(quantities))
    for sku in skus:
        stock(engine, caller, sku, 5)
    lines = [
        line(f"l{index + 1}", sku, quantity)
        for index, (sku, quantity) in enumerate(zip(skus, quantities, strict=True))
    ]
    data = data_of(reserve(client, caller, order(lines), actor=ACTOR), 201)
    return data, skus


def stored_reservation(engine: Engine, caller: Caller) -> dict:
    [stored] = rows_of(engine, Reservation, caller)
    return stored


def releases(engine: Engine, caller: Caller) -> list[dict]:
    written = [e for e in entries(engine, caller) if e["entry_type"] == "RELEASE"]
    return sorted(written, key=lambda entry: entry["line_ref"])


def commit_columns(caller: Caller) -> dict:
    """直接改库为 committed 时，契约 5.7 的约束要求的列；时间都取创建时间。"""
    return {
        "payment_ref": f"pay-{uuid4().hex}",
        "paid_at": Reservation.created_at,
        "commit_request_hash": Reservation.request_hash,
        "commit_credential_id": caller.credential_id,
        "commit_request_id": f"commit-test-{uuid4().hex}",
        "committed_at": Reservation.created_at,
        "closed_at": Reservation.created_at,
    }


# --- 释放 ---


def test_releasing_an_active_reservation_returns_the_reserved_quantity_line_by_line(
    client, engine, caller
):
    created, (first_sku, second_sku) = reserved_order(client, engine, caller, [3, 2])
    reservation_id = created["reservation_id"]
    assert quantities_of(engine, caller, first_sku) == (5, 3, 0, 0)

    key = new_key()
    response = release(client, caller, reservation_id, {"reason": "cancelled"}, key, ACTOR)
    data = data_of(response, 200)

    assert set(data) == RESERVATION_KEYS
    assert data["status"] == "released"
    assert data["release_reason"] == "cancelled"
    assert data["lines"] == created["lines"]
    assert data["expires_at"] == created["expires_at"]
    assert quantities_of(engine, caller, first_sku) == (5, 0, 0, 0)
    assert quantities_of(engine, caller, second_sku) == (5, 0, 0, 0)

    stored = stored_reservation(engine, caller)
    assert stored["status"] == "released"
    assert stored["release_reason"] == "cancelled"
    assert stored["released_credential_id"] == caller.credential_id
    assert stored["released_request_id"] == rid_of(response)
    assert (stored["released_actor_ref"], stored["released_actor_role"]) == (
        ACTOR["X-Actor-Ref"],
        ACTOR["X-Actor-Role"],
    )
    assert stored["closed_at"] is not None
    assert data["released_at"] == format_timestamp(stored["closed_at"])

    written = releases(engine, caller)
    assert [(e["line_ref"], e["sku_id"], e["reserved_delta"]) for e in written] == [
        ("l1", first_sku, -3),
        ("l2", second_sku, -2),
    ]
    for entry in written:
        assert entry["stock_balance_delta"] == entry["unavailable_delta"] == 0
        assert entry["reason_code"] == "cancelled"
        assert entry["source_type"] == "reservation"
        assert entry["source_ref"] == created["order_ref"]
        assert entry["note"] is None
        assert entry["channel_id"] == stored["channel_id"]
        assert entry["idempotency_key"] == key
        assert entry["credential_id"] == caller.credential_id
        assert entry["actor_ref"] == ACTOR["X-Actor-Ref"]
        assert entry["request_id"] == rid_of(response)
        assert entry["occurred_at"] == entry["recorded_at"] == stored["closed_at"]
    assert written[0]["command_id"] == written[1]["command_id"]

    # 同键重放原样返回，不再写任何东西。
    replay = release(client, caller, reservation_id, {"reason": "cancelled"}, key)
    assert replay.status_code == 200
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == response.json()
    assert len(releases(engine, caller)) == 2


def test_releasing_again_writes_nothing_and_keeps_the_original_reason(client, engine, caller):
    created, (sku,) = reserved_order(client, engine, caller, [2])
    reservation_id = created["reservation_id"]
    first = data_of(release(client, caller, reservation_id, {"reason": "cancelled"}), 200)
    before = entries(engine, caller)

    for reason in ("payment_failed", "cancelled"):
        again = release(client, caller, reservation_id, {"reason": reason})
        assert data_of(again, 200) == first
        assert "Idempotency-Replayed" not in again.headers

    assert entries(engine, caller) == before
    assert quantities_of(engine, caller, sku) == (5, 0, 0, 0)
    assert stored_reservation(engine, caller)["release_reason"] == "cancelled"


def test_releasing_a_due_reservation_closes_it_as_expired(client, engine, caller):
    created, (sku,) = reserved_order(client, engine, caller, [2])
    reservation_id = created["reservation_id"]
    shift_into_the_past(engine, reservation_id)

    response = release(client, caller, reservation_id, {"reason": "payment_failed"}, actor=ACTOR)
    data = data_of(response, 200)
    assert data["status"] == "expired"
    assert data["released_at"] is None
    assert data["release_reason"] is None
    assert quantities_of(engine, caller, sku) == (5, 0, 0, 0)

    stored = stored_reservation(engine, caller)
    assert stored["status"] == "expired"
    assert stored["closed_at"] is not None
    for column in (
        "release_reason",
        "released_credential_id",
        "released_actor_ref",
        "released_actor_role",
        "released_request_id",
    ):
        assert stored[column] is None

    # 关单的流水不带凭据、幂等键、操作者与请求 ID，原因是 expired。
    [entry] = releases(engine, caller)
    assert entry["reserved_delta"] == -2
    assert entry["reason_code"] == "expired"
    assert entry["channel_id"] == stored["channel_id"]
    for column in ("idempotency_key", "credential_id", "actor_ref", "actor_role", "request_id"):
        assert entry[column] is None
    assert entry["occurred_at"] == stored["closed_at"]

    # 再释放：不写任何东西，仍是 expired。
    again = data_of(release(client, caller, reservation_id, {"reason": "cancelled"}), 200)
    assert again == data
    assert len(releases(engine, caller)) == 1


def test_a_committed_reservation_is_a_state_conflict(client, engine, caller):
    created, (sku,) = reserved_order(client, engine, caller, [2])
    reservation_id = created["reservation_id"]
    with engine.begin() as db:
        statement = update(Reservation).where(Reservation.reservation_id == reservation_id)
        db.execute(statement.values(status="committed", **commit_columns(caller)))
    before = entries(engine, caller)

    error = error_of(release(client, caller, reservation_id, {"reason": "cancelled"}), 409)
    assert error["code"] == "STATE_CONFLICT"
    assert error["details"] == {"status": "committed"}
    assert entries(engine, caller) == before
    assert quantities_of(engine, caller, sku) == (5, 2, 0, 0)
    assert stored_reservation(engine, caller)["status"] == "committed"


def test_releasing_after_a_merge_reduces_the_reserved_of_the_canonical_sku(client, engine, caller):
    created, (merged,) = reserved_order(client, engine, caller, [2])
    [canonical] = add_skus(engine, caller.tenant_id, 1)
    stock(engine, caller, canonical, 5)
    # 模拟 SKU 合并的改挂：订单行指向规范 SKU，reserved 跟着挪过去。
    with engine.begin() as db:
        db.execute(
            update(OrderLine)
            .where(OrderLine.reservation_id == created["reservation_id"])
            .values(sku_id=canonical)
        )
        for sku_id, delta in ((merged, -2), (canonical, 2)):
            db.execute(
                update(StockBalance)
                .where(
                    StockBalance.tenant_id == caller.tenant_id,
                    StockBalance.pool_id == caller.pool_id,
                    StockBalance.sku_id == sku_id,
                )
                .values(reserved=StockBalance.reserved + delta)
            )

    data = data_of(release(client, caller, created["reservation_id"], {"reason": "cancelled"}), 200)
    assert data["status"] == "released"
    assert quantities_of(engine, caller, canonical) == (5, 0, 0, 0)
    assert quantities_of(engine, caller, merged) == (5, 0, 0, 0)
    [entry] = releases(engine, caller)
    assert (entry["sku_id"], entry["reserved_delta"]) == (canonical, -2)


# --- 池授权 ---


def test_a_reservation_of_another_tenant_is_not_found(client, engine, caller):
    created, (sku,) = reserved_order(client, engine, caller, [1])
    reservation_id = created["reservation_id"]
    other = live_caller(engine)

    foreign = release(client, other, reservation_id, {"reason": "cancelled"})
    error = error_of(foreign, 404)
    assert error["code"] == "RESERVATION_NOT_FOUND"
    [event] = events(engine, rid_of(foreign))
    assert event["event_type"] == "cross_tenant"
    assert event["details"] == {"object_type": "reservation", "id": reservation_id}

    missing = release(client, other, f"rsv_{uuid4().hex[:26].upper()}", {"reason": "cancelled"})
    assert error_of(missing, 404) == error
    assert events(engine, rid_of(missing)) == []

    assert stored_reservation(engine, caller)["status"] == "active"
    assert quantities_of(engine, caller, sku) == (5, 1, 0, 0)


@pytest.mark.parametrize("channel_status", [None, "inactive"])
def test_a_credential_without_a_sales_channel_is_forbidden(client, engine, caller, channel_status):
    created, (sku,) = reserved_order(client, engine, caller, [1])
    reservation_id = created["reservation_id"]
    without_channel = live_caller(engine, channel_status=channel_status)

    response = release(client, without_channel, reservation_id, {"reason": "cancelled"})
    assert error_of(response, 403)["code"] == "FORBIDDEN"
    [event] = events(engine, rid_of(response))
    assert event["event_type"] == "forbidden"
    assert event["details"]["category"] == "pool"
    assert stored_reservation(engine, caller)["status"] == "active"
    assert quantities_of(engine, caller, sku) == (5, 1, 0, 0)


# --- 正文 ---


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"reason": "expired"},
        {"reason": "Cancelled"},
        {"reason": None},
        {"reason": 1},
        {"reason": "cancelled", "note": "x"},
    ],
)
def test_an_invalid_body_is_rejected(client, engine, caller, body):
    created, (sku,) = reserved_order(client, engine, caller, [1])
    response = release(client, caller, created["reservation_id"], body)
    assert error_of(response, 422)["code"] == "INVALID_REQUEST"
    assert stored_reservation(engine, caller)["status"] == "active"
    assert quantities_of(engine, caller, sku) == (5, 1, 0, 0)
    assert releases(engine, caller) == []


def test_releasing_one_reservation_leaves_the_others_alone(client, engine, caller):
    """订单行按本租户与预留读：同一租户的别的预留不受影响。"""
    first, (first_sku,) = reserved_order(client, engine, caller, [1])
    second, (second_sku,) = reserved_order(client, engine, caller, [2])
    data_of(release(client, caller, first["reservation_id"], {"reason": "cancelled"}), 200)
    assert quantities_of(engine, caller, first_sku) == (5, 0, 0, 0)
    assert quantities_of(engine, caller, second_sku) == (5, 2, 0, 0)
    statement = select(Reservation.status).where(
        Reservation.reservation_id == second["reservation_id"]
    )
    with engine.connect() as db:
        assert db.execute(statement).scalar_one() == "active"
