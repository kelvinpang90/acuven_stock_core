"""确认销售：有效预留的正常确认、各种不写东西的拒绝与表示（契约 §5.2 第 5–7 条、§5.7、§7.1）。

在真 MySQL 上跑；每个测试建自己的租户、池、销售入口与凭据，池先切到 live，
预留经接口建立；到期关单、待审、结案与 SKU 合并的改挂由测试直接改库模拟（自己提交的连接）。
晚到确认见 test_sale_commit_late_api.py，故障补交新建见 test_sale_commit_fault_api.py。
标记的测试另挂测试路由，经执行器核心 run_with_context 调确认命令体，
记下 answered_by_reference。
"""

from collections.abc import Iterator
from datetime import datetime
from typing import Annotated
from uuid import uuid4

import pytest
from fastapi import Depends, Request
from fastapi.responses import Response
from fastapi.testclient import TestClient
from sqlalchemy import Engine, update

from app.api.auth import authorize, get_access, require_sales_pool
from app.api.sale_commits import SaleCommitBody
from app.domain.service_auth import ORDERS_WRITE
from app.models.inventory import StockBalance
from app.models.orders import OrderLine, Reservation
from app.services import sale_commits as sale_commit_service
from app.services.idempotency import CommandResult, Execution, http_context, run_with_context
from app.services.sale_commits import CommitSkus
from app.services.stock import format_timestamp
from tests.integration.test_api_auth import Caller, error_of, events
from tests.integration.test_idempotency import Flag, add_skus, flagging, overtake, records, rid_of
from tests.integration.test_reservation_release_api import release, reserved_order
from tests.integration.test_reservations_api import (
    ACTOR,
    RESERVATION_KEYS,
    data_of,
    find_reservations,
    get_reservation,
    line,
    live_caller,
    make_client,
    new_ref,
    rows_of,
    stock,
)
from tests.integration.test_stock_imports_api import merge
from tests.integration.test_stock_service import entries, quantities_of

SALE_COMMITS_CREATE = "sale_commits.create"
# 东八区的 10 点，即 UTC 的 2 点。
PAID_AT = "2026-11-18T10:00:00+08:00"
PAID_AT_UTC = datetime(2026, 11, 18, 2, 0, 0)
_SAME = object()


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
    return f"shop:commit:{uuid4().hex}"


def new_payment_ref() -> str:
    return f"pay-{uuid4().hex}"


def commit_body(
    created: dict,
    payment_ref: str | None = None,
    reservation_id: object = _SAME,
    paid_at: str = PAID_AT,
    lines: list[dict] | None = None,
) -> dict:
    """按预留的表示组装确认正文；reservation_id 默认取预留的 ID。"""
    return {
        "reservation_id": created["reservation_id"] if reservation_id is _SAME else reservation_id,
        "payment_ref": payment_ref or new_payment_ref(),
        "paid_at": paid_at,
        "lines": created["lines"] if lines is None else lines,
    }


def commit(
    client: TestClient,
    caller: Caller,
    order_ref: str,
    body: object,
    key: str | None = None,
    actor: dict | None = None,
):
    headers = caller.auth | {
        "Idempotency-Key": key or new_key(),
        "X-Request-ID": f"commit-test-{uuid4().hex}",
    }
    path = f"/v1/orders/{order_ref}/sale-commits"
    return client.post(path, json=body, headers=headers | (actor or {}))


def stored_reservation(engine: Engine, caller: Caller, reservation_id: str) -> dict:
    [stored] = [
        row
        for row in rows_of(engine, Reservation, caller)
        if row["reservation_id"] == reservation_id
    ]
    return stored


def sale_commits(engine: Engine, caller: Caller) -> list[dict]:
    written = [
        e for e in entries(engine, caller) if e["entry_type"] in ("SALE_COMMIT", "LATE_SALE_COMMIT")
    ]
    return sorted(written, key=lambda entry: entry["line_ref"])


def paid_columns(caller: Caller, payment_ref: str) -> dict:
    """直接改库为待审或结案时，契约 5.7 的约束要求的付款与确认列。"""
    return {
        "payment_ref": payment_ref,
        "paid_at": Reservation.created_at,
        "commit_request_hash": Reservation.request_hash,
        "commit_credential_id": caller.credential_id,
        "commit_request_id": f"commit-test-{uuid4().hex}",
        "closed_at": Reservation.created_at,
    }


def set_status(engine: Engine, reservation_id: str, status: str, **values: object) -> None:
    with engine.begin() as db:
        statement = update(Reservation).where(Reservation.reservation_id == reservation_id)
        db.execute(statement.values(status=status, **values))


def simulate_merge(
    engine: Engine, caller: Caller, reservation_id: str, merged: str, into: str, quantity: int
) -> None:
    """模拟 SKU 合并：来源 SKU 标为已合并、订单行改挂。

    reserved 里的 quantity 件跟着挪到规范 SKU。
    """
    merge(engine, merged, into)
    with engine.begin() as db:
        lines = update(OrderLine).where(OrderLine.reservation_id == reservation_id)
        db.execute(lines.values(sku_id=into))
        for sku_id, delta in ((merged, -quantity), (into, quantity)):
            db.execute(
                update(StockBalance)
                .where(
                    StockBalance.tenant_id == caller.tenant_id,
                    StockBalance.pool_id == caller.pool_id,
                    StockBalance.sku_id == sku_id,
                )
                .values(reserved=StockBalance.reserved + delta)
            )


# --- 正常确认 ---


def test_committing_an_active_reservation_moves_reserved_to_paid_pending_dispatch(
    client, engine, caller
):
    created, (first_sku, second_sku) = reserved_order(client, engine, caller, [3, 2])
    reservation_id = created["reservation_id"]
    order_ref = created["order_ref"]
    body = commit_body(created)
    key = new_key()
    response = commit(client, caller, order_ref, body, key, ACTOR)
    data = data_of(response, 200)

    assert set(data) == RESERVATION_KEYS
    assert data["reservation_id"] == reservation_id
    assert data["status"] == "committed"
    assert data["payment_ref"] == body["payment_ref"]
    assert data["paid_at"] == "2026-11-18T02:00:00.000000Z"
    assert data["late"] is False
    assert data["fault_mode"] is False
    assert data["review_closed_at"] is None
    assert data["released_at"] is None
    assert data["lines"] == created["lines"]

    # stock_balance 与 reserved 各减该行数量，paid_pending_dispatch 加。
    assert quantities_of(engine, caller, first_sku) == (2, 0, 0, 3)
    assert quantities_of(engine, caller, second_sku) == (3, 0, 0, 2)

    stored = stored_reservation(engine, caller, reservation_id)
    assert stored["status"] == "committed"
    assert stored["payment_ref"] == body["payment_ref"]
    assert stored["paid_at"] == PAID_AT_UTC
    assert stored["committed_at"] is not None
    assert stored["closed_at"] == stored["committed_at"]
    assert data["committed_at"] == format_timestamp(stored["committed_at"])
    assert stored["commit_request_hash"] is not None
    assert stored["commit_credential_id"] == caller.credential_id
    assert stored["commit_request_id"] == rid_of(response)
    assert (stored["commit_actor_ref"], stored["commit_actor_role"]) == (
        ACTOR["X-Actor-Ref"],
        ACTOR["X-Actor-Role"],
    )
    assert not stored["late"]
    assert not stored["fault_mode"]

    for row in rows_of(engine, OrderLine, caller):
        assert row["committed_qty"] == row["quantity"]

    written = sale_commits(engine, caller)
    assert [(e["line_ref"], e["sku_id"]) for e in written] == [
        ("l1", first_sku),
        ("l2", second_sku),
    ]
    for entry, quantity in zip(written, (3, 2), strict=True):
        assert entry["entry_type"] == "SALE_COMMIT"
        assert entry["stock_balance_delta"] == -quantity
        assert entry["reserved_delta"] == -quantity
        assert entry["unavailable_delta"] == 0
        assert entry["paid_pending_dispatch_delta"] == quantity
        assert entry["source_type"] == "sale_commit"
        assert entry["source_ref"] == order_ref
        assert entry["reason_code"] is None
        assert entry["note"] is None
        assert entry["channel_id"] == stored["channel_id"]
        assert entry["idempotency_key"] == key
        assert entry["credential_id"] == caller.credential_id
        assert entry["actor_ref"] == ACTOR["X-Actor-Ref"]
        assert entry["request_id"] == rid_of(response)
        # 业务发生时间是付款时间；记录时间是确认时间。
        assert entry["occurred_at"] == PAID_AT_UTC
        assert entry["recorded_at"] == stored["committed_at"]
    assert written[0]["command_id"] == written[1]["command_id"]

    # 两个查询都返回新字段，与确认的表示相同。
    assert data_of(get_reservation(client, caller, reservation_id), 200) == data
    found = data_of(find_reservations(client, caller, {"order_ref": order_ref}), 200)
    assert found == {"reservations": [data]}


def test_a_commit_without_a_reservation_id_is_marked_fault_mode(client, engine, caller):
    created, (sku,) = reserved_order(client, engine, caller, [2])
    body = commit_body(created, reservation_id=None)
    data = data_of(commit(client, caller, created["order_ref"], body), 200)

    assert data["status"] == "committed"
    assert data["fault_mode"] is True
    assert data["late"] is False
    assert quantities_of(engine, caller, sku) == (3, 0, 0, 2)
    assert stored_reservation(engine, caller, created["reservation_id"])["fault_mode"]
    [entry] = sale_commits(engine, caller)
    assert entry["entry_type"] == "SALE_COMMIT"
    assert entry["reason_code"] == "fault_mode"


# --- 拒绝 ---


def test_a_released_reservation_is_a_state_conflict(client, engine, caller):
    created, (sku,) = reserved_order(client, engine, caller, [2])
    data_of(release(client, caller, created["reservation_id"], {"reason": "cancelled"}), 200)
    before = entries(engine, caller)

    response = commit(client, caller, created["order_ref"], commit_body(created))
    error = error_of(response, 409)
    assert error["code"] == "STATE_CONFLICT"
    assert error["details"] == {"status": "released"}
    assert entries(engine, caller) == before
    assert quantities_of(engine, caller, sku) == (5, 0, 0, 0)
    assert stored_reservation(engine, caller, created["reservation_id"])["status"] == "released"


def test_a_closed_refunded_reservation_is_a_state_conflict(client, engine, caller):
    created, (sku,) = reserved_order(client, engine, caller, [2])
    set_status(
        engine,
        created["reservation_id"],
        "closed_refunded",
        **paid_columns(caller, new_payment_ref()),
        review_closed_at=Reservation.created_at,
        review_closed_credential_id=caller.credential_id,
        review_closed_request_id=f"close-test-{uuid4().hex}",
    )
    before = entries(engine, caller)

    error = error_of(commit(client, caller, created["order_ref"], commit_body(created)), 409)
    assert error["code"] == "STATE_CONFLICT"
    assert error["details"] == {"status": "closed_refunded"}
    assert entries(engine, caller) == before
    assert stored_reservation(engine, caller, created["reservation_id"])["status"] == (
        "closed_refunded"
    )


def test_a_reservation_committed_with_another_payment_ref_is_a_state_conflict(
    client, engine, caller
):
    created, (sku,) = reserved_order(client, engine, caller, [2])
    first = commit_body(created)
    data_of(commit(client, caller, created["order_ref"], first), 200)
    before = entries(engine, caller)

    response = commit(client, caller, created["order_ref"], commit_body(created))
    error = error_of(response, 409)
    assert error["code"] == "STATE_CONFLICT"
    assert error["details"] == {"status": "committed"}
    assert events(engine, rid_of(response)) == []
    assert entries(engine, caller) == before
    assert quantities_of(engine, caller, sku) == (3, 0, 0, 2)
    stored = stored_reservation(engine, caller, created["reservation_id"])
    assert stored["payment_ref"] == first["payment_ref"]


def test_a_review_with_another_payment_ref_is_a_state_conflict(client, engine, caller):
    created, (sku,) = reserved_order(client, engine, caller, [2])
    recorded = new_payment_ref()
    set_status(
        engine, created["reservation_id"], "late_payment_review", **paid_columns(caller, recorded)
    )
    before = entries(engine, caller)

    error = error_of(commit(client, caller, created["order_ref"], commit_body(created)), 409)
    assert error["code"] == "STATE_CONFLICT"
    assert error["details"] == {"reason": "payment_ref", "status": "late_payment_review"}
    assert entries(engine, caller) == before
    stored = stored_reservation(engine, caller, created["reservation_id"])
    assert stored["status"] == "late_payment_review"
    assert stored["payment_ref"] == recorded


def test_a_payment_ref_used_on_another_order_is_a_business_ref_conflict(client, engine, caller):
    first, (first_sku,) = reserved_order(client, engine, caller, [1])
    second, (second_sku,) = reserved_order(client, engine, caller, [2])
    payment_ref = new_payment_ref()
    data_of(commit(client, caller, first["order_ref"], commit_body(first, payment_ref)), 200)

    response = commit(client, caller, second["order_ref"], commit_body(second, payment_ref))
    error = error_of(response, 409)
    assert error["code"] == "BUSINESS_REF_CONFLICT"
    assert error["details"] == {"ref_type": "payment", "ref": payment_ref}
    [event] = events(engine, rid_of(response))
    assert event["event_type"] == "business_ref_conflict"
    assert event["details"] == {"ref_type": "payment", "ref": payment_ref}

    assert stored_reservation(engine, caller, second["reservation_id"])["status"] == "active"
    assert quantities_of(engine, caller, second_sku) == (5, 2, 0, 0)
    assert [e["sku_id"] for e in sale_commits(engine, caller)] == [first_sku]


def test_a_different_reservation_id_is_a_mismatch(client, engine, caller):
    created, (sku,) = reserved_order(client, engine, caller, [2])
    other_id = f"rsv_{uuid4().hex[:26].upper()}"
    body = commit_body(created, reservation_id=other_id)

    error = error_of(commit(client, caller, created["order_ref"], body), 409)
    assert error["code"] == "RESERVATION_MISMATCH"
    assert error["details"] == {"reason": "reservation_id"}
    assert sale_commits(engine, caller) == []
    assert stored_reservation(engine, caller, created["reservation_id"])["status"] == "active"


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("more", ["l4"]),
        ("fewer", ["l1"]),
        ("quantity", ["l2", "l3"]),
        ("sku", ["l2"]),
        ("missing_sku", ["l1"]),
        ("mixed", ["l0", "l2", "l3"]),
    ],
)
def test_lines_that_differ_are_a_mismatch_listing_line_refs_in_order(
    client, engine, caller, case, expected
):
    created, skus = reserved_order(client, engine, caller, [1, 2, 3])
    [other] = add_skus(engine, caller.tenant_id, 1)
    stock(engine, caller, other, 5)
    lines = {item["line_ref"]: dict(item) for item in created["lines"]}
    if case == "more":
        lines["l4"] = line("l4", other, 1)
    elif case == "fewer":
        del lines["l1"]
    elif case == "quantity":
        lines["l3"]["quantity"] = 4
        lines["l2"]["quantity"] = 1
    elif case == "sku":
        lines["l2"]["sku_id"] = other
    elif case == "missing_sku":
        lines["l1"]["sku_id"] = f"sku_{uuid4().hex[:26].upper()}"
    else:
        assert case == "mixed"
        del lines["l3"]
        lines["l0"] = line("l0", other, 1)
        lines["l2"]["quantity"] = 5
    # 请求里的行按 line_ref 降序给出：details 里仍按升序。
    requested = [lines[ref] for ref in sorted(lines, reverse=True)]
    body = commit_body(created, lines=requested)

    response = commit(client, caller, created["order_ref"], body)
    error = error_of(response, 409)
    assert error["code"] == "RESERVATION_MISMATCH"
    assert error["details"] == {"reason": "lines", "line_refs": expected}
    assert events(engine, rid_of(response)) == []
    assert sale_commits(engine, caller) == []
    assert stored_reservation(engine, caller, created["reservation_id"])["status"] == "active"
    for sku, quantity in zip(skus, (1, 2, 3), strict=True):
        assert quantities_of(engine, caller, sku) == (5, quantity, 0, 0)


def test_a_reservation_id_without_a_reservation_is_not_found(client, engine, caller):
    [sku] = add_skus(engine, caller.tenant_id, 1)
    stock(engine, caller, sku, 5)
    body = {
        "reservation_id": f"rsv_{uuid4().hex[:26].upper()}",
        "payment_ref": new_payment_ref(),
        "paid_at": PAID_AT,
        "lines": [line("l1", sku, 1)],
    }
    key = new_key()
    response = commit(client, caller, new_ref(), body, key)
    error = error_of(response, 404)
    assert error["code"] == "RESERVATION_NOT_FOUND"
    assert events(engine, rid_of(response)) == []
    assert rows_of(engine, Reservation, caller) == []
    assert entries(engine, caller) == []
    # 不写东西的拒绝只保存幂等记录。
    assert len(records(engine, caller, key, SALE_COMMITS_CREATE)) == 1


@pytest.mark.parametrize("channel_status", [None, "inactive"])
def test_a_credential_without_a_sales_channel_is_forbidden(client, engine, caller, channel_status):
    created, (sku,) = reserved_order(client, engine, caller, [1])
    without_channel = live_caller(engine, channel_status=channel_status)
    key = new_key()

    response = commit(client, without_channel, created["order_ref"], commit_body(created), key)
    assert error_of(response, 403)["code"] == "FORBIDDEN"
    [event] = events(engine, rid_of(response))
    assert event["event_type"] == "forbidden"
    assert event["details"] == {"category": "pool", "pool_id": None}
    assert records(engine, without_channel, key, SALE_COMMITS_CREATE) == []
    assert stored_reservation(engine, caller, created["reservation_id"])["status"] == "active"
    assert quantities_of(engine, caller, sku) == (5, 1, 0, 0)


# --- 业务引用与重放 ---


def test_replays_and_the_same_payment_ref_on_a_new_key(client, engine, caller):
    created, (sku,) = reserved_order(client, engine, caller, [2])
    order_ref = created["order_ref"]
    body = commit_body(created)
    key = new_key()
    first = commit(client, caller, order_ref, body, key)
    committed = data_of(first, 200)

    # 同键原样重放。
    replay = commit(client, caller, order_ref, body, key)
    assert replay.status_code == 200
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == first.json()

    # 换键同内容：200 与当前表示。
    again = commit(client, caller, order_ref, body)
    assert data_of(again, 200) == committed
    assert "Idempotency-Replayed" not in again.headers
    assert events(engine, rid_of(again)) == []

    # 换键、同 payment_ref、不同内容：409 BUSINESS_REF_CONFLICT。
    changed = body | {"paid_at": "2026-11-18T11:00:00+08:00"}
    conflict = commit(client, caller, order_ref, changed)
    error = error_of(conflict, 409)
    assert error["code"] == "BUSINESS_REF_CONFLICT"
    assert error["details"] == {"ref_type": "payment", "ref": body["payment_ref"]}
    [event] = events(engine, rid_of(conflict))
    assert event["event_type"] == "business_ref_conflict"

    # 只扣了一次。
    assert len(sale_commits(engine, caller)) == 1
    assert quantities_of(engine, caller, sku) == (3, 0, 0, 2)


# --- 合并比对 ---


def test_committing_by_the_original_sku_after_a_merge_takes_the_canonical_sku(
    client, engine, caller
):
    created, (merged,) = reserved_order(client, engine, caller, [2])
    [canonical] = add_skus(engine, caller.tenant_id, 1)
    stock(engine, caller, canonical, 5)
    simulate_merge(engine, caller, created["reservation_id"], merged, canonical, 2)
    assert quantities_of(engine, caller, canonical) == (5, 2, 0, 0)

    # 网店按原来下单的 SKU 报行。
    body = commit_body(created, lines=[line("l1", merged, 2)])
    data = data_of(commit(client, caller, created["order_ref"], body), 200)
    assert data["status"] == "committed"
    assert data["lines"] == [line("l1", canonical, 2)]
    assert quantities_of(engine, caller, canonical) == (3, 0, 0, 2)
    assert quantities_of(engine, caller, merged) == (5, 0, 0, 0)
    [entry] = sale_commits(engine, caller)
    assert (entry["sku_id"], entry["stock_balance_delta"]) == (canonical, -2)


def test_committing_a_released_order_after_a_merge_is_a_state_conflict(client, engine, caller):
    created, (merged,) = reserved_order(client, engine, caller, [2])
    data_of(release(client, caller, created["reservation_id"], {"reason": "cancelled"}), 200)
    [canonical] = add_skus(engine, caller.tenant_id, 1)
    # 已释放的订单行不改挂，只把来源 SKU 标为已合并。
    merge(engine, merged, canonical)

    body = commit_body(created, lines=[line("l1", merged, 2)])
    error = error_of(commit(client, caller, created["order_ref"], body), 409)
    assert error["code"] == "STATE_CONFLICT"
    assert error["details"] == {"status": "released"}
    assert sale_commits(engine, caller) == []


# --- 正文 ---


@pytest.mark.parametrize(
    "case",
    [
        "paid_at_without_offset",
        "paid_at_not_a_time",
        "bad_payment_ref",
        "long_reservation_id",
        "empty_reservation_id",
        "numeric_reservation_id",
        "duplicate_line_ref",
        "duplicate_sku",
        "missing_reservation_id",
        "missing_payment_ref",
        "no_lines",
        "unknown_field",
        "bad_order_ref",
    ],
)
def test_an_invalid_request_is_rejected(client, engine, caller, case):
    created, (first_sku, second_sku) = reserved_order(client, engine, caller, [1, 1])
    body = commit_body(created, lines=[dict(item) for item in created["lines"]])
    order_ref = created["order_ref"]
    if case == "paid_at_without_offset":
        body["paid_at"] = "2026-11-18T10:00:00"
    elif case == "paid_at_not_a_time":
        body["paid_at"] = "yesterday"
    elif case == "bad_payment_ref":
        body["payment_ref"] = "pay 1"
    elif case == "long_reservation_id":
        body["reservation_id"] = "r" * 33
    elif case == "empty_reservation_id":
        body["reservation_id"] = ""
    elif case == "numeric_reservation_id":
        body["reservation_id"] = 1
    elif case == "duplicate_line_ref":
        body["lines"][1]["line_ref"] = "l1"
    elif case == "duplicate_sku":
        body["lines"][1]["sku_id"] = first_sku
    elif case == "missing_reservation_id":
        del body["reservation_id"]
    elif case == "missing_payment_ref":
        del body["payment_ref"]
    elif case == "no_lines":
        body["lines"] = []
    elif case == "unknown_field":
        body["amount"] = 100
    else:
        assert case == "bad_order_ref"
        order_ref = "o" * 129
    key = new_key()
    assert error_of(commit(client, caller, order_ref, body, key), 422)["code"] == "INVALID_REQUEST"
    assert records(engine, caller, key, SALE_COMMITS_CREATE) == []
    assert sale_commits(engine, caller) == []
    assert stored_reservation(engine, caller, created["reservation_id"])["status"] == "active"


def test_a_32_character_reservation_id_is_accepted_and_compared(client, engine, caller):
    created, (sku,) = reserved_order(client, engine, caller, [1])
    body = commit_body(created, reservation_id="r" * 32)
    error = error_of(commit(client, caller, created["order_ref"], body), 409)
    assert error["code"] == "RESERVATION_MISMATCH"
    assert error["details"] == {"reason": "reservation_id"}


def test_more_than_100_lines_is_too_large(client, engine, caller):
    created, (sku,) = reserved_order(client, engine, caller, [1])
    lines = [line(f"l{index}", f"sku-{index}", 1) for index in range(101)]
    response = commit(client, caller, created["order_ref"], commit_body(created, lines=lines))
    assert error_of(response, 413)["code"] == "REQUEST_TOO_LARGE"
    assert sale_commits(engine, caller) == []


# --- 按业务引用返回原结果的标记 ---

FLAGGED = "/test/flagged"
# 标记路由按 request_id 记下的标记；flagged_client 结束时清空。
FLAGS: dict[str, Flag] = {}


def add_flagged_route(app) -> None:
    """标记路由：路径参数、依赖、请求体与命令同 POST /v1/orders/{order_ref}/sale-commits，
    经执行器核心 run_with_context 调命令体，按 request_id 在 FLAGS 记下标记。
    """

    @app.post(
        f"{FLAGGED}/orders/{{order_ref}}/sale-commits",
        dependencies=[Depends(authorize(ORDERS_WRITE, write=True))],
    )
    def create_flagged_sale_commit(
        request: Request,
        body: SaleCommitBody,
        order_ref: str,
        pool_id: Annotated[str, Depends(require_sales_pool)],
    ) -> Response:
        sale_commit = body.model.sale_commit()
        tenant_id = get_access(request).principal.tenant_id
        skus = CommitSkus(tenant_id, pool_id, [each.sku_id for each in sale_commit.lines])

        def command(execution: Execution) -> CommandResult:
            return sale_commit_service.create_sale_commit(execution, skus, order_ref, sale_commit)

        return run_with_context(
            request,
            http_context(request),
            operation=SALE_COMMITS_CREATE,
            body=body,
            command=flagging(command, FLAGS),
            balance_rows=skus.balance_rows,
            read_committed=True,
            before_transaction=skus.close_due,
        )


@pytest.fixture
def flagged_client(migrated_engine: Engine) -> Iterator[TestClient]:
    test_client = make_client()
    add_flagged_route(test_client.app)
    try:
        yield test_client
    finally:
        test_client.app.state.engine.dispose()
        FLAGS.clear()


def commit_flagged(
    client: TestClient, caller: Caller, order_ref: str, body: dict, key: str | None = None
):
    headers = caller.auth | {
        "Idempotency-Key": key or new_key(),
        "X-Request-ID": f"commit-flag-{uuid4().hex}",
    }
    path = f"{FLAGGED}/orders/{order_ref}/sale-commits"
    return client.post(path, json=body, headers=headers)


def test_the_mark_is_set_only_when_the_payment_ref_answers_with_the_original(
    flagged_client, engine, caller
):
    created, (sku,) = reserved_order(flagged_client, engine, caller, [2])
    order_ref = created["order_ref"]
    body = commit_body(created)

    first = commit_flagged(flagged_client, caller, order_ref, body)
    committed = data_of(first, 200)
    assert FLAGS[rid_of(first)] == Flag(answered_by_reference=False, rejudged=False)

    # 换键重报同一 payment_ref、内容相同：第 1 步按 payment_ref 返回原结果，打标记。
    again = commit_flagged(flagged_client, caller, order_ref, body)
    assert data_of(again, 200) == committed
    assert FLAGS[rid_of(again)] == Flag(answered_by_reference=True, rejudged=False)

    # 内容不同：409 BUSINESS_REF_CONFLICT，不打标记。
    changed = body | {"paid_at": "2026-11-18T11:00:00+08:00"}
    conflict = commit_flagged(flagged_client, caller, order_ref, changed)
    assert error_of(conflict, 409)["code"] == "BUSINESS_REF_CONFLICT"
    assert FLAGS[rid_of(conflict)] == Flag(answered_by_reference=False, rejudged=False)
    assert len(sale_commits(engine, caller)) == 1
    assert quantities_of(engine, caller, sku) == (3, 0, 0, 2)


@pytest.mark.parametrize("same_content", [True, False])
def test_the_mark_on_a_reservation_committed_after_step_one(
    flagged_client, engine, caller, monkeypatch, same_content
):
    created, (sku,) = reserved_order(flagged_client, engine, caller, [2])
    order_ref = created["order_ref"]
    body = commit_body(created)
    other_body = body if same_content else body | {"paid_at": "2026-11-18T11:00:00+08:00"}
    other_client = TestClient(flagged_client.app)

    # 停在第 1 步（按 payment_ref 查，没有）之后、锁预留行之前，另一笔换键确认同一订单并提交；
    # 停住的那笔锁到预留行时它已 committed，按预留行上记下的确认回答。
    held, other = overtake(
        monkeypatch,
        sale_commit_service,
        "lock_reservation_by_ref",
        lambda: commit_flagged(flagged_client, caller, order_ref, body),
        lambda: commit_flagged(other_client, caller, order_ref, other_body),
    )

    winner = data_of(other, 200)
    assert winner["status"] == "committed"
    assert FLAGS[rid_of(other)] == Flag(answered_by_reference=False, rejudged=False)
    if same_content:
        assert data_of(held, 200) == winner
        assert FLAGS[rid_of(held)] == Flag(answered_by_reference=True, rejudged=False)
    else:
        error = error_of(held, 409)
        assert error["code"] == "BUSINESS_REF_CONFLICT"
        assert error["details"] == {"ref_type": "payment", "ref": body["payment_ref"]}
        assert FLAGS[rid_of(held)] == Flag(answered_by_reference=False, rejudged=False)
    assert len(sale_commits(engine, caller)) == 1
    assert quantities_of(engine, caller, sku) == (3, 0, 0, 2)
