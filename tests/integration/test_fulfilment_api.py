"""发货与退款回库：落账、累计量、单据的表示、各种不写东西的拒绝、业务引用与正文
（契约 §5.1、§5.3、§5.8、§7.1）。

在真 MySQL 上跑；每个测试建自己的租户、池、销售入口与凭据，池先切到 live，
预留与确认销售经接口建立；预留的其他状态与 SKU 合并的改挂由测试直接改库模拟（自己提交的连接）。
并发见 test_fulfilment_concurrency.py。
"""

from collections.abc import Iterator
from datetime import datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, update

from app.models.inventory import StockBalance
from app.models.orders import OrderDocument, OrderLine, Reservation
from app.services.stock import format_timestamp
from tests.integration.test_api_auth import Caller, error_of, events
from tests.integration.test_idempotency import add_skus, records, rid_of
from tests.integration.test_reservation_release_api import release, reserved_order
from tests.integration.test_reservations_api import (
    ACTOR,
    data_of,
    get_reservation,
    live_caller,
    make_client,
    new_ref,
    rows_of,
    shift_into_the_past,
    stock,
)
from tests.integration.test_sale_commit_late_api import expire
from tests.integration.test_sale_commits_api import (
    commit,
    commit_body,
    new_payment_ref,
    paid_columns,
    set_status,
    stored_reservation,
)
from tests.integration.test_stock_imports_api import merge
from tests.integration.test_stock_service import entries, quantities_of

DISPATCHES_CREATE = "dispatches.create"
REFUND_RESTOCKS_CREATE = "refund_restocks.create"
DISPATCHES = "dispatches"
REFUND_RESTOCKS = "refund-restocks"
# 路径、操作类型、引用字段、details.ref_type、单据类型、流水类型、document_id 前缀。
KINDS = {
    DISPATCHES: (DISPATCHES_CREATE, "shipment_ref", "shipment", "dispatch", "DISPATCH", "shp_"),
    REFUND_RESTOCKS: (
        REFUND_RESTOCKS_CREATE,
        "refund_ref",
        "refund",
        "refund_restock",
        "REFUND_RESTOCK",
        "rfr_",
    ),
}
COMMON_KEYS = {
    "document_id",
    "document_type",
    "order_ref",
    "reservation_id",
    "pool_id",
    "reason_code",
    "note",
    "occurred_at",
    "created_at",
    "lines",
}
# 东八区的 9 点，即 UTC 的 1 点。
OCCURRED_AT = "2026-11-19T09:00:00+08:00"
OCCURRED_AT_UTC = datetime(2026, 11, 19, 1, 0, 0)


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
    return f"shop:fulfil:{uuid4().hex}"


def new_document_ref() -> str:
    return f"doc-{uuid4().hex}"


def qline(line_ref: str, quantity: object) -> dict:
    return {"line_ref": line_ref, "quantity": quantity}


def body_of(kind: str, lines: list[dict], ref: str | None = None, **extra: object) -> dict:
    """本类型的正文：引用字段默认取一个新引用。"""
    ref_field = KINDS[kind][1]
    return {ref_field: ref or new_document_ref(), "lines": lines} | extra


def post(
    client: TestClient,
    caller: Caller,
    kind: str,
    order_ref: str,
    body: object,
    key: str | None = None,
    actor: dict | None = None,
):
    headers = caller.auth | {
        "Idempotency-Key": key or new_key(),
        "X-Request-ID": f"fulfil-test-{uuid4().hex}",
    }
    path = f"/v1/orders/{order_ref}/{kind}"
    return client.post(path, json=body, headers=headers | (actor or {}))


def committed_order(client: TestClient, engine: Engine, caller: Caller, quantities: list[int]):
    """每行一个新 SKU、各备 5 件，预留并确认；返回预留的表示与按行的 SKU。"""
    created, skus = reserved_order(client, engine, caller, quantities)
    data_of(commit(client, caller, created["order_ref"], commit_body(created)), 200)
    return created, skus


def of_type(engine: Engine, caller: Caller, entry_type: str) -> list[dict]:
    written = [entry for entry in entries(engine, caller) if entry["entry_type"] == entry_type]
    return sorted(written, key=lambda entry: (entry["line_ref"], entry["balance_version_after"]))


def documents(engine: Engine, caller: Caller) -> list[dict]:
    return sorted(
        rows_of(engine, OrderDocument, caller), key=lambda document: document["document_seq"]
    )


def order_lines(engine: Engine, caller: Caller) -> dict[str, dict]:
    return {row["line_ref"]: row for row in rows_of(engine, OrderLine, caller)}


def totals(engine: Engine, caller: Caller) -> dict[str, tuple[int, int]]:
    """按 line_ref 的 (dispatched_qty, refund_restocked_qty)。"""
    return {
        ref: (row["dispatched_qty"], row["refund_restocked_qty"])
        for ref, row in order_lines(engine, caller).items()
    }


def snapshot(engine: Engine, caller: Caller) -> tuple:
    """拒绝前后要相同的：流水、单据与订单行。"""
    return entries(engine, caller), documents(engine, caller), order_lines(engine, caller)


def move_to_canonical(
    engine: Engine, caller: Caller, reservation_id: str, merged: str, into: str, quantity: int
) -> None:
    """模拟 SKU 合并：来源 SKU 标为已合并、指向它的订单行改挂，
    paid_pending_dispatch 里的 quantity 件跟着挪到规范 SKU。
    """
    merge(engine, merged, into)
    with engine.begin() as db:
        lines = update(OrderLine).where(
            OrderLine.reservation_id == reservation_id, OrderLine.sku_id == merged
        )
        db.execute(lines.values(sku_id=into))
        for sku_id, delta in ((merged, -quantity), (into, quantity)):
            db.execute(
                update(StockBalance)
                .where(
                    StockBalance.tenant_id == caller.tenant_id,
                    StockBalance.pool_id == caller.pool_id,
                    StockBalance.sku_id == sku_id,
                )
                .values(paid_pending_dispatch=StockBalance.paid_pending_dispatch + delta)
            )


# --- 发货 ---


def test_a_dispatch_takes_paid_pending_dispatch_and_returns_the_document(client, engine, caller):
    created, (first_sku, second_sku) = committed_order(client, engine, caller, [3, 2])
    order_ref = created["order_ref"]
    reservation_id = created["reservation_id"]
    assert quantities_of(engine, caller, first_sku) == (2, 0, 0, 3)
    shipment_ref = new_document_ref()
    # 请求里的行按 line_ref 降序给出：表示与流水仍按升序。
    body = body_of(
        DISPATCHES,
        [qline("l2", 1), qline("l1", 3)],
        shipment_ref,
        reason_code="courier",
        note="left at the door",
        occurred_at=OCCURRED_AT,
    )
    key = new_key()
    response = post(client, caller, DISPATCHES, order_ref, body, key, ACTOR)
    data = data_of(response, 201)

    assert set(data) == COMMON_KEYS | {"shipment_ref"}
    assert data["document_id"].startswith("shp_")
    assert data["document_type"] == "dispatch"
    assert data["order_ref"] == order_ref
    assert data["reservation_id"] == reservation_id
    assert data["pool_id"] == caller.pool_id
    assert data["shipment_ref"] == shipment_ref
    assert data["reason_code"] == "courier"
    assert data["note"] == "left at the door"
    assert data["occurred_at"] == "2026-11-19T01:00:00.000000Z"

    # 只有 paid_pending_dispatch 减。
    assert quantities_of(engine, caller, first_sku) == (2, 0, 0, 0)
    assert quantities_of(engine, caller, second_sku) == (3, 0, 0, 1)

    written = of_type(engine, caller, "DISPATCH")
    assert [(e["line_ref"], e["sku_id"]) for e in written] == [
        ("l1", first_sku),
        ("l2", second_sku),
    ]
    channel_id = stored_reservation(engine, caller, reservation_id)["channel_id"]
    [document] = documents(engine, caller)
    for entry, quantity in zip(written, (3, 1), strict=True):
        assert entry["stock_balance_delta"] == 0
        assert entry["reserved_delta"] == 0
        assert entry["unavailable_delta"] == 0
        assert entry["paid_pending_dispatch_delta"] == -quantity
        assert entry["source_type"] == "dispatch"
        assert entry["source_ref"] == shipment_ref
        assert entry["reason_code"] == "courier"
        assert entry["note"] == "left at the door"
        assert entry["occurred_at"] == OCCURRED_AT_UTC
        assert entry["recorded_at"] == document["created_at"]
        assert entry["channel_id"] == channel_id
        assert entry["idempotency_key"] == key
        assert entry["credential_id"] == caller.credential_id
        assert entry["actor_ref"] == ACTOR["X-Actor-Ref"]
        assert entry["actor_role"] == ACTOR["X-Actor-Role"]
        assert entry["request_id"] == rid_of(response)
    assert written[0]["command_id"] == written[1]["command_id"]
    expected = (("l1", first_sku, 3), ("l2", second_sku, 1))
    assert data["lines"] == [
        {"line_ref": ref, "sku_id": sku, "quantity": quantity, "entry_id": entry["entry_id"]}
        for (ref, sku, quantity), entry in zip(expected, written, strict=True)
    ]

    # 订单行加 dispatched_qty；预留仍是 committed。
    assert totals(engine, caller) == {"l1": (3, 0), "l2": (1, 0)}
    assert stored_reservation(engine, caller, reservation_id)["status"] == "committed"

    # 单据一行，result 就是首次的表示。
    assert document["document_id"] == data["document_id"]
    assert document["document_type"] == "dispatch"
    assert document["document_ref"] == shipment_ref
    assert document["reservation_id"] == reservation_id
    assert document["pool_id"] == caller.pool_id
    assert document["channel_id"] == channel_id
    assert document["result"] == data
    assert document["credential_id"] == caller.credential_id
    assert (document["actor_ref"], document["actor_role"]) == (
        ACTOR["X-Actor-Ref"],
        ACTOR["X-Actor-Role"],
    )
    assert document["request_id"] == rid_of(response)
    assert data["created_at"] == format_timestamp(document["created_at"])
    [record] = records(engine, caller, key, DISPATCHES_CREATE)
    assert record["response_status"] == 201
    assert document["request_hash"] == record["request_hash"]


def test_partial_dispatches_on_new_shipment_refs_finish_the_order_and_keep_it_committed(
    client, engine, caller
):
    created, (first_sku, second_sku) = committed_order(client, engine, caller, [3, 2])
    order_ref = created["order_ref"]

    # 只报一部分行与件数；没带 occurred_at 时取命令时间。
    partial = body_of(DISPATCHES, [qline("l1", 1)])
    first = data_of(post(client, caller, DISPATCHES, order_ref, partial), 201)
    assert first["occurred_at"] == first["created_at"]
    assert (first["reason_code"], first["note"]) == (None, None)
    assert totals(engine, caller) == {"l1": (1, 0), "l2": (0, 0)}
    assert quantities_of(engine, caller, second_sku) == (3, 0, 0, 2)

    # 余下的换 shipment_ref 再报。
    rest = body_of(DISPATCHES, [qline("l1", 2), qline("l2", 2)])
    second = data_of(post(client, caller, DISPATCHES, order_ref, rest), 201)
    assert second["document_id"] != first["document_id"]
    assert totals(engine, caller) == {"l1": (3, 0), "l2": (2, 0)}
    assert quantities_of(engine, caller, first_sku) == (2, 0, 0, 0)
    assert quantities_of(engine, caller, second_sku) == (3, 0, 0, 0)
    assert len(of_type(engine, caller, "DISPATCH")) == 3
    assert len(documents(engine, caller)) == 2

    # 全部发完之后预留仍是 committed，查询照样返回它。
    assert stored_reservation(engine, caller, created["reservation_id"])["status"] == "committed"
    shown = data_of(get_reservation(client, caller, created["reservation_id"]), 200)
    assert shown["status"] == "committed"


# --- 退款回库 ---


def test_a_refund_restock_returns_the_units_to_the_stock_balance(client, engine, caller):
    created, (first_sku, second_sku) = committed_order(client, engine, caller, [3, 2])
    refund_ref = new_document_ref()
    body = body_of(REFUND_RESTOCKS, [qline("l1", 2)], refund_ref, reason_code="refund_approved")
    key = new_key()
    response = post(client, caller, REFUND_RESTOCKS, created["order_ref"], body, key)
    data = data_of(response, 201)

    assert set(data) == COMMON_KEYS | {"refund_ref"}
    assert data["document_id"].startswith("rfr_")
    assert data["document_type"] == "refund_restock"
    assert data["refund_ref"] == refund_ref
    assert data["reason_code"] == "refund_approved"

    # stock_balance 加、paid_pending_dispatch 减；没列的行不动。
    assert quantities_of(engine, caller, first_sku) == (4, 0, 0, 1)
    assert quantities_of(engine, caller, second_sku) == (3, 0, 0, 2)
    [entry] = of_type(engine, caller, "REFUND_RESTOCK")
    assert (entry["line_ref"], entry["sku_id"]) == ("l1", first_sku)
    assert entry["stock_balance_delta"] == 2
    assert entry["reserved_delta"] == 0
    assert entry["unavailable_delta"] == 0
    assert entry["paid_pending_dispatch_delta"] == -2
    assert entry["source_type"] == "refund_restock"
    assert entry["source_ref"] == refund_ref
    assert entry["reason_code"] == "refund_approved"
    assert entry["idempotency_key"] == key
    assert entry["request_id"] == rid_of(response)
    assert data["lines"] == [
        {"line_ref": "l1", "sku_id": first_sku, "quantity": 2, "entry_id": entry["entry_id"]}
    ]
    assert totals(engine, caller) == {"l1": (0, 2), "l2": (0, 0)}

    [document] = documents(engine, caller)
    assert document["document_type"] == "refund_restock"
    assert document["document_ref"] == refund_ref
    assert document["result"] == data


def test_dispatches_and_refund_restocks_together_stop_at_the_committed_quantity(
    client, engine, caller
):
    created, (first_sku, second_sku) = committed_order(client, engine, caller, [3, 2])
    order_ref = created["order_ref"]
    data_of(post(client, caller, DISPATCHES, order_ref, body_of(DISPATCHES, [qline("l1", 2)])), 201)
    refund = body_of(REFUND_RESTOCKS, [qline("l1", 1), qline("l2", 1)])
    data_of(post(client, caller, REFUND_RESTOCKS, order_ref, refund), 201)
    assert totals(engine, caller) == {"l1": (2, 1), "l2": (0, 1)}
    before = snapshot(engine, caller)

    # l1 已用完、l2 还剩 1：再多一件就超过。
    for kind in (DISPATCHES, REFUND_RESTOCKS):
        key = new_key()
        response = post(client, caller, kind, order_ref, body_of(kind, [qline("l1", 1)]), key)
        error = error_of(response, 409)
        assert error["code"] == "QUANTITY_EXCEEDS_OUTSTANDING"
        assert error["details"] == {
            "lines": [{"line_ref": "l1", "sku_id": first_sku, "quantity": 1, "limit": 0}]
        }
        assert events(engine, rid_of(response)) == []
        [record] = records(engine, caller, key, KINDS[kind][0])
        assert record["response_status"] == 409

    # 按请求顺序列出全部超过的行；没超过的行不列。
    lines = [qline("l2", 2), qline("l1", 1)]
    error = error_of(post(client, caller, DISPATCHES, order_ref, body_of(DISPATCHES, lines)), 409)
    assert error["details"] == {
        "lines": [
            {"line_ref": "l2", "sku_id": second_sku, "quantity": 2, "limit": 1},
            {"line_ref": "l1", "sku_id": first_sku, "quantity": 1, "limit": 0},
        ]
    }
    assert snapshot(engine, caller) == before

    # 剩下的 1 件照常可以报。
    data_of(post(client, caller, DISPATCHES, order_ref, body_of(DISPATCHES, [qline("l2", 1)])), 201)
    assert totals(engine, caller) == {"l1": (2, 1), "l2": (1, 1)}
    assert quantities_of(engine, caller, first_sku) == (3, 0, 0, 0)
    assert quantities_of(engine, caller, second_sku) == (4, 0, 0, 0)


# --- 拒绝 ---


@pytest.mark.parametrize("kind", [DISPATCHES, REFUND_RESTOCKS])
def test_an_order_without_a_reservation_is_not_found(client, engine, caller, kind):
    key = new_key()
    response = post(client, caller, kind, new_ref(), body_of(kind, [qline("l1", 1)]), key)
    error = error_of(response, 404)
    assert error["code"] == "RESERVATION_NOT_FOUND"
    assert events(engine, rid_of(response)) == []
    assert documents(engine, caller) == []
    assert entries(engine, caller) == []
    # 不写东西的拒绝只保存幂等记录。
    [record] = records(engine, caller, key, KINDS[kind][0])
    assert record["response_status"] == 404


@pytest.mark.parametrize(
    ("state", "status"),
    [
        ("active", "active"),
        ("due", "expired"),
        ("released", "released"),
        ("expired", "expired"),
        ("late_payment_review", "late_payment_review"),
        ("closed_refunded", "closed_refunded"),
    ],
)
def test_a_reservation_that_is_not_committed_is_a_state_conflict(
    client, engine, caller, state, status
):
    created, (sku,) = reserved_order(client, engine, caller, [2])
    reservation_id = created["reservation_id"]
    if state == "due":
        # 已到期还没关掉的 active 报 expired。
        shift_into_the_past(engine, reservation_id)
    elif state == "released":
        data_of(release(client, caller, reservation_id, {"reason": "cancelled"}), 200)
    elif state == "expired":
        expire(engine, caller, reservation_id)
    elif state == "late_payment_review":
        set_status(engine, reservation_id, state, **paid_columns(caller, new_payment_ref()))
    elif state == "closed_refunded":
        set_status(
            engine,
            reservation_id,
            state,
            **paid_columns(caller, new_payment_ref()),
            review_closed_at=Reservation.created_at,
            review_closed_credential_id=caller.credential_id,
            review_closed_request_id=f"close-test-{uuid4().hex}",
        )
    else:
        assert state == "active"
    before = snapshot(engine, caller)
    stored_before = stored_reservation(engine, caller, reservation_id)
    balance_before = quantities_of(engine, caller, sku)

    for kind in (DISPATCHES, REFUND_RESTOCKS):
        body = body_of(kind, [qline("l1", 1)])
        response = post(client, caller, kind, created["order_ref"], body)
        error = error_of(response, 409)
        assert error["code"] == "STATE_CONFLICT"
        assert error["details"] == {"status": status}
        assert events(engine, rid_of(response)) == []

    assert snapshot(engine, caller) == before
    assert stored_reservation(engine, caller, reservation_id) == stored_before
    assert quantities_of(engine, caller, sku) == balance_before


@pytest.mark.parametrize("kind", [DISPATCHES, REFUND_RESTOCKS])
def test_a_line_ref_that_is_not_on_the_order_is_a_mismatch(client, engine, caller, kind):
    created, skus = committed_order(client, engine, caller, [1, 1])
    before = snapshot(engine, caller)
    lines = [qline("l3", 1), qline("l1", 1), qline("l0", 1)]
    response = post(client, caller, kind, created["order_ref"], body_of(kind, lines))
    error = error_of(response, 409)
    assert error["code"] == "RESERVATION_MISMATCH"
    assert error["details"] == {"reason": "lines", "line_refs": ["l0", "l3"]}
    assert events(engine, rid_of(response)) == []
    assert snapshot(engine, caller) == before
    for sku in skus:
        assert quantities_of(engine, caller, sku) == (4, 0, 0, 1)


@pytest.mark.parametrize("channel_status", [None, "inactive"])
@pytest.mark.parametrize("kind", [DISPATCHES, REFUND_RESTOCKS])
def test_a_credential_without_a_sales_channel_is_forbidden(
    client, engine, caller, kind, channel_status
):
    created, (sku,) = committed_order(client, engine, caller, [1])
    without_channel = live_caller(engine, channel_status=channel_status)
    before = snapshot(engine, caller)
    key = new_key()

    body = body_of(kind, [qline("l1", 1)])
    response = post(client, without_channel, kind, created["order_ref"], body, key)
    assert error_of(response, 403)["code"] == "FORBIDDEN"
    [event] = events(engine, rid_of(response))
    assert event["event_type"] == "forbidden"
    assert event["details"] == {"category": "pool", "pool_id": None}
    assert records(engine, without_channel, key, KINDS[kind][0]) == []
    assert snapshot(engine, caller) == before
    assert quantities_of(engine, caller, sku) == (4, 0, 0, 1)


# --- 业务引用与重放 ---


@pytest.mark.parametrize("kind", [DISPATCHES, REFUND_RESTOCKS])
def test_replays_and_the_same_reference_on_a_new_key(client, engine, caller, kind):
    operation, ref_field, ref_type, _, entry_type, _ = KINDS[kind]
    created, (sku,) = committed_order(client, engine, caller, [2])
    other, _ = committed_order(client, engine, caller, [2])
    order_ref = created["order_ref"]
    body = body_of(kind, [qline("l1", 2)])
    ref = body[ref_field]
    key = new_key()
    first = post(client, caller, kind, order_ref, body, key)
    document = data_of(first, 201)

    # 同键原样重放。
    replay = post(client, caller, kind, order_ref, body, key)
    assert replay.status_code == 201
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == first.json()

    # 换键同内容：余量已用完也是 200 与首次的表示。
    again_key = new_key()
    again = post(client, caller, kind, order_ref, body, again_key)
    assert data_of(again, 200) == document
    assert "Idempotency-Replayed" not in again.headers
    assert events(engine, rid_of(again)) == []
    [record] = records(engine, caller, again_key, operation)
    assert record["response_status"] == 200

    # 换键、同引用、不同内容，或报到别的订单：409 BUSINESS_REF_CONFLICT。
    changed = body | {"lines": [qline("l1", 1)]}
    for target, content in ((order_ref, changed), (other["order_ref"], body)):
        conflict = post(client, caller, kind, target, content)
        error = error_of(conflict, 409)
        assert error["code"] == "BUSINESS_REF_CONFLICT"
        assert error["details"] == {"ref_type": ref_type, "ref": ref}
        [event] = events(engine, rid_of(conflict))
        assert event["event_type"] == "business_ref_conflict"
        assert event["details"] == {"ref_type": ref_type, "ref": ref}

    # 只生效一次；别的订单没动。
    [entry] = of_type(engine, caller, entry_type)
    assert entry["sku_id"] == sku
    assert len(documents(engine, caller)) == 1
    if kind == DISPATCHES:
        assert quantities_of(engine, caller, sku) == (3, 0, 0, 0)
    else:
        assert quantities_of(engine, caller, sku) == (5, 0, 0, 0)
    other_lines = [row for row in rows_of(engine, OrderLine, caller) if row["sku_id"] != sku]
    assert [(row["dispatched_qty"], row["refund_restocked_qty"]) for row in other_lines] == [(0, 0)]


def test_a_dispatch_and_a_refund_restock_may_share_a_reference_string(client, engine, caller):
    created, (sku,) = committed_order(client, engine, caller, [3])
    order_ref = created["order_ref"]
    ref = new_document_ref()

    dispatch = body_of(DISPATCHES, [qline("l1", 1)], ref)
    refund = body_of(REFUND_RESTOCKS, [qline("l1", 1)], ref)
    dispatched = post(client, caller, DISPATCHES, order_ref, dispatch)
    restocked = post(client, caller, REFUND_RESTOCKS, order_ref, refund)
    assert data_of(dispatched, 201)["shipment_ref"] == ref
    assert data_of(restocked, 201)["refund_ref"] == ref
    assert events(engine, rid_of(restocked)) == []
    stored = documents(engine, caller)
    assert [(d["document_type"], d["document_ref"]) for d in stored] == [
        ("dispatch", ref),
        ("refund_restock", ref),
    ]
    assert totals(engine, caller) == {"l1": (1, 1)}
    assert quantities_of(engine, caller, sku) == (3, 0, 0, 1)


# --- 合并改挂 ---


def test_after_a_merge_a_dispatch_takes_the_canonical_sku(client, engine, caller):
    created, (merged,) = committed_order(client, engine, caller, [2])
    [canonical] = add_skus(engine, caller.tenant_id, 1)
    stock(engine, caller, canonical, 5)
    move_to_canonical(engine, caller, created["reservation_id"], merged, canonical, 2)
    assert quantities_of(engine, caller, canonical) == (5, 0, 0, 2)

    body = body_of(DISPATCHES, [qline("l1", 2)])
    data = data_of(post(client, caller, DISPATCHES, created["order_ref"], body), 201)
    [entry] = of_type(engine, caller, "DISPATCH")
    assert (entry["sku_id"], entry["paid_pending_dispatch_delta"]) == (canonical, -2)
    assert data["lines"] == [
        {"line_ref": "l1", "sku_id": canonical, "quantity": 2, "entry_id": entry["entry_id"]}
    ]
    assert quantities_of(engine, caller, canonical) == (5, 0, 0, 0)
    assert quantities_of(engine, caller, merged) == (3, 0, 0, 0)


def test_two_lines_on_one_sku_after_a_merge_write_one_entry_each(client, engine, caller):
    created, (canonical, merged) = committed_order(client, engine, caller, [3, 2])
    move_to_canonical(engine, caller, created["reservation_id"], merged, canonical, 2)
    assert quantities_of(engine, caller, canonical) == (2, 0, 0, 5)

    body = body_of(DISPATCHES, [qline("l1", 3), qline("l2", 2)])
    data = data_of(post(client, caller, DISPATCHES, created["order_ref"], body), 201)
    first, second = of_type(engine, caller, "DISPATCH")
    assert [(e["line_ref"], e["sku_id"]) for e in (first, second)] == [
        ("l1", canonical),
        ("l2", canonical),
    ]
    # 余额行只锁一次，两条流水依次写在它上面。
    assert second["balance_version_after"] == first["balance_version_after"] + 1
    assert (first["paid_pending_dispatch_after"], second["paid_pending_dispatch_after"]) == (2, 0)
    assert [line["sku_id"] for line in data["lines"]] == [canonical, canonical]
    assert quantities_of(engine, caller, canonical) == (2, 0, 0, 0)
    assert quantities_of(engine, caller, merged) == (3, 0, 0, 0)
    assert totals(engine, caller) == {"l1": (3, 0), "l2": (2, 0)}


# --- 正文 ---


@pytest.mark.parametrize(
    "case",
    [
        "bad_ref",
        "missing_ref",
        "bad_line_ref",
        "zero_quantity",
        "fractional_quantity",
        "quantity_over_the_limit",
        "string_quantity",
        "duplicate_line_ref",
        "sku_id_in_a_line",
        "unknown_field",
        "missing_lines",
        "no_lines",
        "null_reason_code",
        "bad_reason_code",
        "bad_occurred_at",
        "bad_order_ref",
    ],
)
@pytest.mark.parametrize("kind", [DISPATCHES, REFUND_RESTOCKS])
def test_an_invalid_request_is_rejected(client, engine, caller, kind, case):
    created, (first_sku, second_sku) = committed_order(client, engine, caller, [1, 1])
    ref_field = KINDS[kind][1]
    body = body_of(kind, [qline("l1", 1), qline("l2", 1)])
    order_ref = created["order_ref"]
    if case == "bad_ref":
        body[ref_field] = "ship 1"
    elif case == "missing_ref":
        del body[ref_field]
    elif case == "bad_line_ref":
        body["lines"][0]["line_ref"] = "l 1"
    elif case == "zero_quantity":
        body["lines"][0]["quantity"] = 0
    elif case == "fractional_quantity":
        body["lines"][0]["quantity"] = 1.5
    elif case == "quantity_over_the_limit":
        body["lines"][0]["quantity"] = 1000001
    elif case == "string_quantity":
        body["lines"][0]["quantity"] = "1"
    elif case == "duplicate_line_ref":
        body["lines"][1]["line_ref"] = "l1"
    elif case == "sku_id_in_a_line":
        body["lines"][0]["sku_id"] = first_sku
    elif case == "unknown_field":
        body["amount"] = 100
    elif case == "missing_lines":
        del body["lines"]
    elif case == "no_lines":
        body["lines"] = []
    elif case == "null_reason_code":
        body["reason_code"] = None
    elif case == "bad_reason_code":
        body["reason_code"] = "Bad Code"
    elif case == "bad_occurred_at":
        body["occurred_at"] = "2026-11-19T09:00:00"
    else:
        assert case == "bad_order_ref"
        order_ref = "o" * 129
    before = snapshot(engine, caller)
    key = new_key()
    response = post(client, caller, kind, order_ref, body, key)
    assert error_of(response, 422)["code"] == "INVALID_REQUEST"
    assert records(engine, caller, key, KINDS[kind][0]) == []
    assert snapshot(engine, caller) == before


@pytest.mark.parametrize("kind", [DISPATCHES, REFUND_RESTOCKS])
def test_more_than_100_lines_is_too_large(client, engine, caller, kind):
    created, (sku,) = committed_order(client, engine, caller, [1])
    lines = [qline(f"l{index}", 1) for index in range(101)]
    key = new_key()
    response = post(client, caller, kind, created["order_ref"], body_of(kind, lines), key)
    assert error_of(response, 413)["code"] == "REQUEST_TOO_LARGE"
    assert records(engine, caller, key, KINDS[kind][0]) == []
    assert documents(engine, caller) == []
