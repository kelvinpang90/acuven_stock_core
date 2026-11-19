"""退货验收：落账、累计量、单据的表示、余量、各种不写东西的拒绝、业务引用与正文
（契约 §5.1、§5.8、§7.1）。

在真 MySQL 上跑；每个测试建自己的租户、池、销售入口与凭据，池先切到 live，
预留、确认销售、发货与退款回库经接口建立；预留的其他状态由测试直接改库模拟。
请求与观察的辅助函数沿用 test_fulfilment_api.py。
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.models.orders import OrderLine
from app.services.stock import format_timestamp
from tests.integration.test_api_auth import Caller, error_of, events
from tests.integration.test_fulfilment_api import (
    COMMON_KEYS,
    DISPATCHES,
    OCCURRED_AT,
    OCCURRED_AT_UTC,
    REFUND_RESTOCKS,
    body_of,
    committed_order,
    documents,
    new_document_ref,
    new_key,
    order_lines,
    post,
    qline,
    snapshot,
)
from tests.integration.test_idempotency import records, rid_of
from tests.integration.test_reservation_release_api import reserved_order
from tests.integration.test_reservations_api import (
    ACTOR,
    data_of,
    live_caller,
    make_client,
    rows_of,
    shift_into_the_past,
)
from tests.integration.test_sale_commits_api import (
    new_payment_ref,
    paid_columns,
    set_status,
    stored_reservation,
)
from tests.integration.test_stock_service import entries, quantities_of

RETURN_RECEIPTS_CREATE = "return_receipts.create"
RETURN_RECEIPTS = "return-receipts"
RETURN_TYPES = ("RETURN_SELLABLE", "RETURN_UNSELLABLE")


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


def rline(line_ref: str, sellable: object, unsellable: object) -> dict:
    return {"line_ref": line_ref, "sellable_quantity": sellable, "unsellable_quantity": unsellable}


def return_body(lines: list[dict], ref: str | None = None, **extra: object) -> dict:
    return {"return_ref": ref or new_document_ref(), "lines": lines} | extra


def receive(
    client: TestClient,
    caller: Caller,
    order_ref: str,
    body: object,
    key: str | None = None,
    actor: dict | None = None,
):
    return post(client, caller, RETURN_RECEIPTS, order_ref, body, key, actor)


def dispatch(client: TestClient, caller: Caller, order_ref: str, lines: list[dict]) -> None:
    body = body_of(DISPATCHES, lines)
    data_of(post(client, caller, DISPATCHES, order_ref, body), 201)


def dispatched_order(client: TestClient, engine: Engine, caller: Caller, quantities: list[int]):
    """确认之后每行全部发货；返回预留的表示与按行的 SKU。"""
    created, skus = committed_order(client, engine, caller, quantities)
    lines = [qline(f"l{index + 1}", quantity) for index, quantity in enumerate(quantities)]
    dispatch(client, caller, created["order_ref"], lines)
    return created, skus


def returns(engine: Engine, caller: Caller) -> list[dict]:
    """退货流水，按 line_ref、再按写入先后。"""
    written = [entry for entry in entries(engine, caller) if entry["entry_type"] in RETURN_TYPES]
    return sorted(written, key=lambda entry: (entry["line_ref"], entry["balance_version_after"]))


def receipts(engine: Engine, caller: Caller) -> list[dict]:
    return [d for d in documents(engine, caller) if d["document_type"] == "return_receipt"]


def dispatched(engine: Engine, caller: Caller) -> dict[str, int]:
    return {ref: row["dispatched_qty"] for ref, row in order_lines(engine, caller).items()}


def returned(engine: Engine, caller: Caller) -> dict[str, tuple[int, int]]:
    """按 line_ref 的 (returned_sellable_qty, returned_unsellable_qty)。"""
    return {
        ref: (row["returned_sellable_qty"], row["returned_unsellable_qty"])
        for ref, row in order_lines(engine, caller).items()
    }


def available_of(engine: Engine, caller: Caller, sku_id: str) -> int:
    stock_balance, reserved, unavailable, _ = quantities_of(engine, caller, sku_id)
    return stock_balance - reserved - unavailable


# --- 退货验收 ---


def test_one_line_with_sellable_and_unsellable_units(client, engine, caller):
    created, (first_sku, second_sku) = dispatched_order(client, engine, caller, [3, 2])
    order_ref = created["order_ref"]
    reservation_id = created["reservation_id"]
    assert quantities_of(engine, caller, first_sku) == (2, 0, 0, 0)
    available_before = available_of(engine, caller, first_sku)
    assert dispatched(engine, caller) == {"l1": 3, "l2": 2}
    return_ref = new_document_ref()
    body = return_body(
        [rline("l1", 2, 1)],
        return_ref,
        reason_code="customer_return",
        note="box damaged",
        occurred_at=OCCURRED_AT,
    )
    key = new_key()
    response = receive(client, caller, order_ref, body, key, ACTOR)
    data = data_of(response, 201)

    assert set(data) == COMMON_KEYS | {"return_ref"}
    assert data["document_id"].startswith("rtn_")
    assert data["document_type"] == "return_receipt"
    assert data["order_ref"] == order_ref
    assert data["reservation_id"] == reservation_id
    assert data["pool_id"] == caller.pool_id
    assert data["return_ref"] == return_ref
    assert data["reason_code"] == "customer_return"
    assert data["note"] == "box damaged"
    assert data["occurred_at"] == "2026-11-19T01:00:00.000000Z"

    # stock_balance 加两数之和、unavailable 加不可售数、可售量只加可售数；没列的行不动。
    assert quantities_of(engine, caller, first_sku) == (5, 0, 1, 0)
    assert available_of(engine, caller, first_sku) == available_before + 2
    assert quantities_of(engine, caller, second_sku) == (3, 0, 0, 0)

    # 先一条 RETURN_SELLABLE，再一条 RETURN_UNSELLABLE，写在同一余额行上。
    sellable, unsellable = returns(engine, caller)
    assert (sellable["entry_type"], unsellable["entry_type"]) == RETURN_TYPES
    assert unsellable["balance_version_after"] == sellable["balance_version_after"] + 1
    deltas = ("stock_balance_delta", "reserved_delta", "unavailable_delta")
    assert tuple(sellable[name] for name in deltas) == (2, 0, 0)
    assert tuple(unsellable[name] for name in deltas) == (1, 0, 1)
    channel_id = stored_reservation(engine, caller, reservation_id)["channel_id"]
    [document] = receipts(engine, caller)
    for entry in (sellable, unsellable):
        assert entry["sku_id"] == first_sku
        assert entry["paid_pending_dispatch_delta"] == 0
        assert entry["line_ref"] == "l1"
        assert entry["source_type"] == "return_receipt"
        assert entry["source_ref"] == return_ref
        assert entry["reason_code"] == "customer_return"
        assert entry["note"] == "box damaged"
        assert entry["occurred_at"] == OCCURRED_AT_UTC
        assert entry["recorded_at"] == document["created_at"]
        assert entry["channel_id"] == channel_id
        assert entry["idempotency_key"] == key
        assert entry["credential_id"] == caller.credential_id
        assert entry["actor_ref"] == ACTOR["X-Actor-Ref"]
        assert entry["actor_role"] == ACTOR["X-Actor-Role"]
        assert entry["request_id"] == rid_of(response)
    assert sellable["command_id"] == unsellable["command_id"]
    assert data["lines"] == [
        {
            "line_ref": "l1",
            "sku_id": first_sku,
            "sellable_quantity": 2,
            "unsellable_quantity": 1,
            "sellable_entry_id": sellable["entry_id"],
            "unsellable_entry_id": unsellable["entry_id"],
        }
    ]

    # 订单行两个累计量各加，已发货量不变；预留仍是 committed。
    assert returned(engine, caller) == {"l1": (2, 1), "l2": (0, 0)}
    assert dispatched(engine, caller) == {"l1": 3, "l2": 2}
    assert stored_reservation(engine, caller, reservation_id)["status"] == "committed"

    # 单据一行，result 就是首次的表示。
    assert document["document_id"] == data["document_id"]
    assert document["document_ref"] == return_ref
    assert document["reservation_id"] == reservation_id
    assert document["pool_id"] == caller.pool_id
    assert document["channel_id"] == channel_id
    assert document["result"] == data
    assert document["request_id"] == rid_of(response)
    assert data["created_at"] == format_timestamp(document["created_at"])
    [record] = records(engine, caller, key, RETURN_RECEIPTS_CREATE)
    assert record["response_status"] == 201
    assert document["request_hash"] == record["request_hash"]


def test_a_line_with_one_kind_of_units_writes_one_entry(client, engine, caller):
    created, (first_sku, second_sku, third_sku) = dispatched_order(
        client, engine, caller, [2, 2, 1]
    )
    # 只报一部分行；请求按 line_ref 降序给出，表示仍按升序。
    body = return_body([rline("l2", 0, 1), rline("l1", 2, 0)])
    data = data_of(receive(client, caller, created["order_ref"], body), 201)
    assert data["occurred_at"] == data["created_at"]
    assert (data["reason_code"], data["note"]) == (None, None)

    first, second = returns(engine, caller)
    assert [(e["line_ref"], e["sku_id"], e["entry_type"]) for e in (first, second)] == [
        ("l1", first_sku, "RETURN_SELLABLE"),
        ("l2", second_sku, "RETURN_UNSELLABLE"),
    ]
    assert data["lines"] == [
        {
            "line_ref": "l1",
            "sku_id": first_sku,
            "sellable_quantity": 2,
            "unsellable_quantity": 0,
            "sellable_entry_id": first["entry_id"],
            "unsellable_entry_id": None,
        },
        {
            "line_ref": "l2",
            "sku_id": second_sku,
            "sellable_quantity": 0,
            "unsellable_quantity": 1,
            "sellable_entry_id": None,
            "unsellable_entry_id": second["entry_id"],
        },
    ]
    assert quantities_of(engine, caller, first_sku) == (5, 0, 0, 0)
    assert quantities_of(engine, caller, second_sku) == (4, 0, 1, 0)
    assert quantities_of(engine, caller, third_sku) == (4, 0, 0, 0)
    assert returned(engine, caller) == {"l1": (2, 0), "l2": (0, 1), "l3": (0, 0)}


# --- 余量 ---


def test_a_line_that_was_not_dispatched_cannot_be_returned(client, engine, caller):
    created, (sku,) = committed_order(client, engine, caller, [2])
    before = snapshot(engine, caller)
    key = new_key()
    body = return_body([rline("l1", 1, 0)])
    response = receive(client, caller, created["order_ref"], body, key)
    error = error_of(response, 409)
    assert error["code"] == "QUANTITY_EXCEEDS_OUTSTANDING"
    assert error["details"] == {
        "lines": [{"line_ref": "l1", "sku_id": sku, "quantity": 1, "limit": 0}]
    }
    assert events(engine, rid_of(response)) == []
    [record] = records(engine, caller, key, RETURN_RECEIPTS_CREATE)
    assert record["response_status"] == 409
    assert snapshot(engine, caller) == before
    assert quantities_of(engine, caller, sku) == (3, 0, 0, 2)


def test_returns_stop_at_the_dispatched_quantity(client, engine, caller):
    created, (first_sku, second_sku) = committed_order(client, engine, caller, [3, 2])
    order_ref = created["order_ref"]
    # l1 发货 2 件、退款回库 1 件：退款回库的件数不算进退货余量，l1 可退 2 件。
    dispatch(client, caller, order_ref, [qline("l1", 2)])
    refund = body_of(REFUND_RESTOCKS, [qline("l1", 1)])
    data_of(post(client, caller, REFUND_RESTOCKS, order_ref, refund), 201)
    assert quantities_of(engine, caller, first_sku) == (3, 0, 0, 0)

    data_of(receive(client, caller, order_ref, return_body([rline("l1", 1, 0)])), 201)
    before = snapshot(engine, caller)

    # 余量 1：两数之和为 2 超过；按请求顺序列出全部超过的行，quantity 为两数之和。
    lines = [rline("l2", 1, 0), rline("l1", 1, 1)]
    error = error_of(receive(client, caller, order_ref, return_body(lines)), 409)
    assert error["code"] == "QUANTITY_EXCEEDS_OUTSTANDING"
    assert error["details"] == {
        "lines": [
            {"line_ref": "l2", "sku_id": second_sku, "quantity": 1, "limit": 0},
            {"line_ref": "l1", "sku_id": first_sku, "quantity": 2, "limit": 1},
        ]
    }
    assert snapshot(engine, caller) == before

    # 合计到 dispatched_qty 为止。
    data_of(receive(client, caller, order_ref, return_body([rline("l1", 0, 1)])), 201)
    assert returned(engine, caller) == {"l1": (1, 1), "l2": (0, 0)}
    assert quantities_of(engine, caller, first_sku) == (5, 0, 1, 0)
    before = snapshot(engine, caller)

    # 再多一件就超过。
    error = error_of(receive(client, caller, order_ref, return_body([rline("l1", 0, 1)])), 409)
    assert error["code"] == "QUANTITY_EXCEEDS_OUTSTANDING"
    assert error["details"] == {
        "lines": [{"line_ref": "l1", "sku_id": first_sku, "quantity": 1, "limit": 0}]
    }
    assert snapshot(engine, caller) == before


# --- 拒绝 ---


@pytest.mark.parametrize(
    ("state", "status"),
    [
        ("active", "active"),
        ("due", "expired"),
        ("late_payment_review", "late_payment_review"),
    ],
)
def test_a_reservation_that_is_not_committed_is_a_state_conflict(
    client, engine, caller, state, status
):
    created, (sku,) = reserved_order(client, engine, caller, [2])
    reservation_id = created["reservation_id"]
    if state == "due":
        shift_into_the_past(engine, reservation_id)
    elif state == "late_payment_review":
        set_status(engine, reservation_id, state, **paid_columns(caller, new_payment_ref()))
    else:
        assert state == "active"
    before = snapshot(engine, caller)
    balance_before = quantities_of(engine, caller, sku)

    response = receive(client, caller, created["order_ref"], return_body([rline("l1", 1, 0)]))
    error = error_of(response, 409)
    assert error["code"] == "STATE_CONFLICT"
    assert error["details"] == {"status": status}
    assert events(engine, rid_of(response)) == []
    assert snapshot(engine, caller) == before
    assert quantities_of(engine, caller, sku) == balance_before


def test_a_line_ref_that_is_not_on_the_order_is_a_mismatch(client, engine, caller):
    created, skus = dispatched_order(client, engine, caller, [1, 1])
    before = snapshot(engine, caller)
    lines = [rline("l3", 1, 0), rline("l1", 1, 0), rline("l0", 0, 1)]
    response = receive(client, caller, created["order_ref"], return_body(lines))
    error = error_of(response, 409)
    assert error["code"] == "RESERVATION_MISMATCH"
    assert error["details"] == {"reason": "lines", "line_refs": ["l0", "l3"]}
    assert events(engine, rid_of(response)) == []
    assert snapshot(engine, caller) == before
    for sku in skus:
        assert quantities_of(engine, caller, sku) == (4, 0, 0, 0)


# --- 业务引用与重放 ---


def test_replays_and_the_same_return_ref_on_a_new_key(client, engine, caller):
    created, (sku,) = dispatched_order(client, engine, caller, [2])
    other, _ = dispatched_order(client, engine, caller, [2])
    order_ref = created["order_ref"]
    body = return_body([rline("l1", 1, 1)])
    ref = body["return_ref"]
    key = new_key()
    first = receive(client, caller, order_ref, body, key)
    document = data_of(first, 201)

    # 同键原样重放。
    replay = receive(client, caller, order_ref, body, key)
    assert replay.status_code == 201
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == first.json()

    # 换键同内容：余量已用完也是 200 与首次的表示。
    again_key = new_key()
    again = receive(client, caller, order_ref, body, again_key)
    assert data_of(again, 200) == document
    assert "Idempotency-Replayed" not in again.headers
    assert events(engine, rid_of(again)) == []
    [record] = records(engine, caller, again_key, RETURN_RECEIPTS_CREATE)
    assert record["response_status"] == 200

    # 换键、同 return_ref、不同内容，或报到别的订单：409 BUSINESS_REF_CONFLICT。
    changed = body | {"lines": [rline("l1", 2, 0)]}
    for target, content in ((order_ref, changed), (other["order_ref"], body)):
        conflict = receive(client, caller, target, content)
        error = error_of(conflict, 409)
        assert error["code"] == "BUSINESS_REF_CONFLICT"
        assert error["details"] == {"ref_type": "return", "ref": ref}
        [event] = events(engine, rid_of(conflict))
        assert event["event_type"] == "business_ref_conflict"
        assert event["details"] == {"ref_type": "return", "ref": ref}

    # 只生效一次；别的订单没动。
    assert [entry["sku_id"] for entry in returns(engine, caller)] == [sku, sku]
    assert len(receipts(engine, caller)) == 1
    assert quantities_of(engine, caller, sku) == (5, 0, 1, 0)
    by_sku = {
        row["sku_id"]: (row["returned_sellable_qty"], row["returned_unsellable_qty"])
        for row in rows_of(engine, OrderLine, caller)
    }
    assert by_sku.pop(sku) == (1, 1)
    assert list(by_sku.values()) == [(0, 0)]


# --- 正文 ---


@pytest.mark.parametrize(
    "case",
    [
        "both_zero",
        "missing_sellable",
        "missing_unsellable",
        "negative_sellable",
        "negative_unsellable",
        "fractional_quantity",
        "quantity_over_the_limit",
        "string_quantity",
        "boolean_quantity",
        "quantity_instead",
        "duplicate_line_ref",
        "sku_id_in_a_line",
        "unknown_field",
        "bad_return_ref",
        "missing_return_ref",
        "bad_line_ref",
        "no_lines",
        "null_reason_code",
        "bad_occurred_at",
        "bad_order_ref",
    ],
)
def test_an_invalid_request_is_rejected(client, engine, caller, case):
    created, (first_sku, _) = dispatched_order(client, engine, caller, [1, 1])
    body = return_body([rline("l1", 1, 0), rline("l2", 0, 1)])
    line = body["lines"][0]
    order_ref = created["order_ref"]
    if case == "both_zero":
        line["sellable_quantity"] = 0
    elif case == "missing_sellable":
        del line["sellable_quantity"]
    elif case == "missing_unsellable":
        del line["unsellable_quantity"]
    elif case == "negative_sellable":
        line["sellable_quantity"] = -1
        line["unsellable_quantity"] = 2
    elif case == "negative_unsellable":
        line["unsellable_quantity"] = -1
    elif case == "fractional_quantity":
        line["sellable_quantity"] = 1.5
    elif case == "quantity_over_the_limit":
        line["unsellable_quantity"] = 1000001
    elif case == "string_quantity":
        line["sellable_quantity"] = "1"
    elif case == "boolean_quantity":
        line["sellable_quantity"] = True
    elif case == "quantity_instead":
        body["lines"][0] = qline("l1", 1)
    elif case == "duplicate_line_ref":
        body["lines"][1]["line_ref"] = "l1"
    elif case == "sku_id_in_a_line":
        line["sku_id"] = first_sku
    elif case == "unknown_field":
        body["amount"] = 100
    elif case == "bad_return_ref":
        body["return_ref"] = "return 1"
    elif case == "missing_return_ref":
        del body["return_ref"]
    elif case == "bad_line_ref":
        line["line_ref"] = "l 1"
    elif case == "no_lines":
        body["lines"] = []
    elif case == "null_reason_code":
        body["reason_code"] = None
    elif case == "bad_occurred_at":
        body["occurred_at"] = "2026-11-19T09:00:00"
    else:
        assert case == "bad_order_ref"
        order_ref = "o" * 129
    before = snapshot(engine, caller)
    key = new_key()
    response = receive(client, caller, order_ref, body, key)
    assert error_of(response, 422)["code"] == "INVALID_REQUEST"
    assert records(engine, caller, key, RETURN_RECEIPTS_CREATE) == []
    assert snapshot(engine, caller) == before


def test_more_than_100_lines_is_too_large(client, engine, caller):
    created, _ = dispatched_order(client, engine, caller, [1])
    lines = [rline(f"l{index}", 1, 0) for index in range(101)]
    key = new_key()
    response = receive(client, caller, created["order_ref"], return_body(lines), key)
    assert error_of(response, 413)["code"] == "REQUEST_TOO_LARGE"
    assert records(engine, caller, key, RETURN_RECEIPTS_CREATE) == []
    assert returns(engine, caller) == []
