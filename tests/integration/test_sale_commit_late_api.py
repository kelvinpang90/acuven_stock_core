"""确认销售的晚到确认与转入待审（契约 §5.2 第 6 条、§5.7 晚到确认与存放、§7.1 第 1 条）。

在真 MySQL 上跑；每个测试建自己的租户、池、销售入口与凭据，池先切到 live，预留经接口建立。
到期关单由测试把预留挪到过去再调 close_if_due（自己提交的连接）；
已到期未关的预留要留到业务事务里关时，只把开事务前钩子里关单那一步换成空操作。
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, update

from app.domain.sale_commits import sku_shortfalls
from app.models.inventory import StockBalance
from app.models.orders import OrderLine
from app.services import reservations as reservation_service
from app.services import sale_commits as sale_commit_service
from app.services.stock import format_timestamp
from tests.integration.test_api_auth import Caller, error_of
from tests.integration.test_idempotency import records, rid_of
from tests.integration.test_reservation_release_api import reserved_order
from tests.integration.test_reservations_api import (
    ACTOR,
    data_of,
    get_reservation,
    line,
    live_caller,
    make_client,
    order,
    reserve,
    rows_of,
    shift_into_the_past,
)
from tests.integration.test_sale_commits_api import (
    PAID_AT_UTC,
    SALE_COMMITS_CREATE,
    commit,
    commit_body,
    new_key,
    sale_commits,
    stored_reservation,
)
from tests.integration.test_stock_imports_api import merge
from tests.integration.test_stock_service import entries, quantities_of

LATE_PAYMENT_INSUFFICIENT_STOCK = "LATE_PAYMENT_INSUFFICIENT_STOCK"
# 待审重试时换一个付款时间：流水与预留仍取第一次的。
LATER_PAID_AT = "2026-11-18T11:00:00+08:00"


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


@pytest.fixture
def unclosed(monkeypatch) -> list[frozenset[str]]:
    """开事务前钩子里的关单换成空操作；记下每次收到的 SKU（balance_rows 照常算出的集合）。"""
    seen: list[frozenset[str]] = []

    def skip(connection, tenant_id, pool_id, sku_ids) -> int:
        seen.append(frozenset(sku_ids))
        return 0

    monkeypatch.setattr(sale_commit_service, "close_due_before_reserving", skip)
    return seen


# --- 准备 ---


def expire(engine: Engine, caller: Caller, reservation_id: str) -> None:
    """挪到过去并关单：预留为 expired，reserved 已放回。"""
    shift_into_the_past(engine, reservation_id)
    with engine.begin() as db:
        assert reservation_service.close_if_due(db, caller.tenant_id, reservation_id)


def expired_order(client: TestClient, engine: Engine, caller: Caller, quantities: list[int]):
    created, skus = reserved_order(client, engine, caller, quantities)
    expire(engine, caller, created["reservation_id"])
    return created, skus


def set_unavailable(engine: Engine, caller: Caller, sku_id: str, quantity: int) -> None:
    with engine.begin() as db:
        statement = update(StockBalance).where(
            StockBalance.tenant_id == caller.tenant_id,
            StockBalance.pool_id == caller.pool_id,
            StockBalance.sku_id == sku_id,
        )
        db.execute(statement.values(unavailable=quantity))


def of_type(engine: Engine, caller: Caller, entry_type: str) -> list[dict]:
    return [entry for entry in entries(engine, caller) if entry["entry_type"] == entry_type]


# --- 有货 ---


def test_an_expired_reservation_with_stock_is_committed_late(client, engine, caller):
    created, (first_sku, second_sku) = expired_order(client, engine, caller, [3, 2])
    reservation_id = created["reservation_id"]
    closed_at = stored_reservation(engine, caller, reservation_id)["closed_at"]
    body = commit_body(created)
    key = new_key()
    response = commit(client, caller, created["order_ref"], body, key, ACTOR)
    data = data_of(response, 200)

    assert data["status"] == "committed"
    assert data["late"] is True
    assert data["fault_mode"] is False
    assert data["payment_ref"] == body["payment_ref"]
    assert data["paid_at"] == "2026-11-18T02:00:00.000000Z"
    assert data["lines"] == created["lines"]

    # 关单已放回 reserved：stock_balance 减该行数量，paid_pending_dispatch 加。
    assert quantities_of(engine, caller, first_sku) == (2, 0, 0, 3)
    assert quantities_of(engine, caller, second_sku) == (3, 0, 0, 2)

    stored = stored_reservation(engine, caller, reservation_id)
    assert stored["status"] == "committed"
    assert stored["late"]
    assert not stored["fault_mode"]
    # 经到期再确认的保留到期关单时间。
    assert stored["closed_at"] == closed_at
    assert stored["committed_at"] is not None
    assert data["committed_at"] == format_timestamp(stored["committed_at"])
    assert stored["payment_ref"] == body["payment_ref"]
    assert stored["paid_at"] == PAID_AT_UTC
    [record] = records(engine, caller, key, SALE_COMMITS_CREATE)
    assert stored["commit_request_hash"] == record["request_hash"]
    assert stored["commit_credential_id"] == caller.credential_id
    assert stored["commit_request_id"] == rid_of(response)
    assert stored["commit_actor_ref"] == ACTOR["X-Actor-Ref"]

    for row in rows_of(engine, OrderLine, caller):
        assert row["committed_qty"] == row["quantity"]

    written = sale_commits(engine, caller)
    assert [(e["line_ref"], e["sku_id"]) for e in written] == [
        ("l1", first_sku),
        ("l2", second_sku),
    ]
    for entry, quantity in zip(written, (3, 2), strict=True):
        assert entry["entry_type"] == "LATE_SALE_COMMIT"
        assert entry["reason_code"] == "late_payment"
        assert entry["stock_balance_delta"] == -quantity
        assert entry["reserved_delta"] == 0
        assert entry["unavailable_delta"] == 0
        assert entry["paid_pending_dispatch_delta"] == quantity
        assert entry["source_type"] == "sale_commit"
        assert entry["source_ref"] == created["order_ref"]
        assert entry["idempotency_key"] == key
        assert entry["request_id"] == rid_of(response)
        assert entry["occurred_at"] == PAID_AT_UTC
        assert entry["recorded_at"] == stored["committed_at"]
    assert written[0]["command_id"] == written[1]["command_id"]

    assert data_of(get_reservation(client, caller, reservation_id), 200) == data


def test_a_late_commit_without_a_reservation_id_is_marked_fault_mode(client, engine, caller):
    created, (sku,) = expired_order(client, engine, caller, [2])
    body = commit_body(created, reservation_id=None)
    data = data_of(commit(client, caller, created["order_ref"], body), 200)

    assert data["status"] == "committed"
    assert data["late"] is True
    assert data["fault_mode"] is True
    stored = stored_reservation(engine, caller, created["reservation_id"])
    assert stored["fault_mode"]
    assert stored["late"]
    [entry] = sale_commits(engine, caller)
    assert entry["entry_type"] == "LATE_SALE_COMMIT"
    assert entry["reason_code"] == "fault_mode"
    assert quantities_of(engine, caller, sku) == (3, 0, 0, 2)


# --- 缺货 ---


def test_an_expired_reservation_without_stock_goes_to_review(client, engine, caller):
    created, (first_sku, merged, third_sku) = expired_order(client, engine, caller, [2, 1, 2])
    reservation_id = created["reservation_id"]
    # l2 的 SKU 合并进 l1 的：同一订单两行指向同一个 SKU，按 SKU 合计 3 件。
    merge(engine, merged, first_sku)
    with engine.begin() as db:
        statement = update(OrderLine).where(
            OrderLine.reservation_id == reservation_id, OrderLine.line_ref == "l2"
        )
        db.execute(statement.values(sku_id=first_sku))
    # first_sku 可售 2：每行单看都够，合计不够；third_sku 够。
    set_unavailable(engine, caller, first_sku, 3)
    before_quantities = [quantities_of(engine, caller, sku) for sku in (first_sku, third_sku)]
    before_entries = entries(engine, caller)
    closed_at = stored_reservation(engine, caller, reservation_id)["closed_at"]

    # 网店按原来下单的行、以 l3、l2、l1 的顺序报。
    requested = [line("l3", third_sku, 2), line("l2", merged, 1), line("l1", first_sku, 2)]
    body = commit_body(created, lines=requested)
    key = new_key()
    response = commit(client, caller, created["order_ref"], body, key, ACTOR)
    error = error_of(response, 409)
    assert error["code"] == LATE_PAYMENT_INSUFFICIENT_STOCK
    assert error["details"] == {
        "lines": [
            {"line_ref": "l2", "sku_id": first_sku, "quantity": 1, "available": 2},
            {"line_ref": "l1", "sku_id": first_sku, "quantity": 2, "available": 2},
        ]
    }

    # 不改任何数量。
    after_quantities = [quantities_of(engine, caller, sku) for sku in (first_sku, third_sku)]
    assert after_quantities == before_quantities
    assert entries(engine, caller) == before_entries

    stored = stored_reservation(engine, caller, reservation_id)
    assert stored["status"] == "late_payment_review"
    assert stored["payment_ref"] == body["payment_ref"]
    assert stored["paid_at"] == PAID_AT_UTC
    [record] = records(engine, caller, key, SALE_COMMITS_CREATE)
    assert record["response_status"] == 409
    assert stored["commit_request_hash"] == record["request_hash"]
    assert stored["commit_credential_id"] == caller.credential_id
    assert stored["commit_request_id"] == rid_of(response)
    assert (stored["commit_actor_ref"], stored["commit_actor_role"]) == (
        ACTOR["X-Actor-Ref"],
        ACTOR["X-Actor-Role"],
    )
    assert stored["closed_at"] == closed_at
    assert stored["committed_at"] is None
    assert not stored["late"]
    assert not stored["fault_mode"]
    shown = data_of(get_reservation(client, caller, reservation_id), 200)
    assert shown["status"] == "late_payment_review"
    assert shown["payment_ref"] == body["payment_ref"]

    # 同键只重放 409。
    replay = commit(client, caller, created["order_ref"], body, key, ACTOR)
    assert replay.status_code == 409
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == response.json()
    assert entries(engine, caller) == before_entries


# --- 待审重试 ---


def test_a_review_retry_after_restocking_commits_with_the_first_payment(client, engine, caller):
    created, (sku,) = expired_order(client, engine, caller, [2])
    reservation_id = created["reservation_id"]
    set_unavailable(engine, caller, sku, 4)
    first = commit_body(created)
    first_response = commit(client, caller, created["order_ref"], first)
    assert error_of(first_response, 409)["code"] == LATE_PAYMENT_INSUFFICIENT_STOCK
    reviewed = stored_reservation(engine, caller, reservation_id)
    assert reviewed["commit_actor_ref"] is None

    # 补货后换键、同一 payment_ref 重试（付款时间报得不同也照第一次的）。
    set_unavailable(engine, caller, sku, 0)
    retry = commit_body(created, first["payment_ref"], paid_at=LATER_PAID_AT)
    key = new_key()
    response = commit(client, caller, created["order_ref"], retry, key, ACTOR)
    data = data_of(response, 200)
    assert data["status"] == "committed"
    assert data["late"] is True
    assert data["payment_ref"] == first["payment_ref"]
    assert data["paid_at"] == "2026-11-18T02:00:00.000000Z"
    assert quantities_of(engine, caller, sku) == (3, 0, 0, 2)

    [entry] = sale_commits(engine, caller)
    assert entry["entry_type"] == "LATE_SALE_COMMIT"
    assert entry["reason_code"] == "late_payment"
    assert entry["occurred_at"] == PAID_AT_UTC
    assert entry["idempotency_key"] == key

    stored = stored_reservation(engine, caller, reservation_id)
    assert stored["status"] == "committed"
    assert stored["payment_ref"] == reviewed["payment_ref"]
    assert stored["paid_at"] == reviewed["paid_at"] == PAID_AT_UTC
    assert stored["closed_at"] == reviewed["closed_at"]
    # 确认摘要与 commit_* 改记成功的那次。
    [record] = records(engine, caller, key, SALE_COMMITS_CREATE)
    assert stored["commit_request_hash"] == record["request_hash"]
    assert stored["commit_request_hash"] != reviewed["commit_request_hash"]
    assert stored["commit_request_id"] == rid_of(response)
    assert stored["commit_request_id"] != reviewed["commit_request_id"]
    assert (stored["commit_actor_ref"], stored["commit_actor_role"]) == (
        ACTOR["X-Actor-Ref"],
        ACTOR["X-Actor-Role"],
    )


def test_a_review_retry_still_short_changes_nothing(client, engine, caller):
    created, (sku,) = expired_order(client, engine, caller, [2])
    reservation_id = created["reservation_id"]
    set_unavailable(engine, caller, sku, 4)
    # 第一次不带 reservation_id：转入待审时 fault_mode 设为真。
    first = commit_body(created, reservation_id=None)
    response = commit(client, caller, created["order_ref"], first)
    assert error_of(response, 409)["code"] == LATE_PAYMENT_INSUFFICIENT_STOCK
    reviewed = stored_reservation(engine, caller, reservation_id)
    assert reviewed["status"] == "late_payment_review"
    assert reviewed["fault_mode"]
    before_entries = entries(engine, caller)

    retry = commit_body(created, first["payment_ref"], paid_at=LATER_PAID_AT)
    key = new_key()
    again = commit(client, caller, created["order_ref"], retry, key, ACTOR)
    error = error_of(again, 409)
    assert error["code"] == LATE_PAYMENT_INSUFFICIENT_STOCK
    assert error["details"] == {
        "lines": [{"line_ref": "l1", "sku_id": sku, "quantity": 2, "available": 1}]
    }
    assert stored_reservation(engine, caller, reservation_id) == reviewed
    assert entries(engine, caller) == before_entries
    assert quantities_of(engine, caller, sku) == (5, 0, 4, 0)
    assert len(records(engine, caller, key, SALE_COMMITS_CREATE)) == 1


# --- 已到期未关 ---


def test_a_due_reservation_is_closed_then_committed_late_in_one_transaction(
    client, engine, caller, unclosed
):
    created, (sku,) = reserved_order(client, engine, caller, [2])
    reservation_id = created["reservation_id"]
    shift_into_the_past(engine, reservation_id)
    key = new_key()
    data = data_of(commit(client, caller, created["order_ref"], commit_body(created), key), 200)

    # 钩子照常收到 balance_rows 记下的集合，只是没有关单。
    assert unclosed == [frozenset({sku})]
    assert data["status"] == "committed"
    assert data["late"] is True
    assert quantities_of(engine, caller, sku) == (3, 0, 0, 2)

    reserve_entry, release, late = entries(engine, caller)
    assert [e["entry_type"] for e in (reserve_entry, release, late)] == [
        "RESERVE",
        "RELEASE",
        "LATE_SALE_COMMIT",
    ]
    assert release["reason_code"] == "expired"
    assert release["reserved_delta"] == -2
    # 关单的流水不带凭据、幂等键与请求 ID。
    assert release["idempotency_key"] is None
    assert release["credential_id"] is None
    assert release["request_id"] is None
    assert late["reason_code"] == "late_payment"
    assert late["idempotency_key"] == key
    stored = stored_reservation(engine, caller, reservation_id)
    assert stored["status"] == "committed"
    assert stored["closed_at"] == release["recorded_at"]
    assert stored["committed_at"] == late["recorded_at"]


def test_a_due_reservation_short_of_stock_keeps_the_closing_and_goes_to_review(
    client, engine, caller, unclosed, monkeypatch
):
    """关单放回的正是这一单的数量，按库里的数关单之后一定够；
    这里另把判可售看到的可售量换成 0，验证业务拒绝撤销保存点之后保留写入重新关单。
    """

    def short_of_everything(lines, available_by_sku):
        return sku_shortfalls(lines, dict.fromkeys(available_by_sku, 0))

    monkeypatch.setattr(sale_commit_service, "sku_shortfalls", short_of_everything)
    created, (sku,) = reserved_order(client, engine, caller, [2])
    reservation_id = created["reservation_id"]
    shift_into_the_past(engine, reservation_id)
    body = commit_body(created)
    key = new_key()
    response = commit(client, caller, created["order_ref"], body, key)

    error = error_of(response, 409)
    assert error["code"] == LATE_PAYMENT_INSUFFICIENT_STOCK
    assert error["details"] == {
        "lines": [{"line_ref": "l1", "sku_id": sku, "quantity": 2, "available": 0}]
    }
    assert unclosed == [frozenset({sku})]

    # 到期关单与转入待审一起提交：RELEASE 只有一条，reserved 已减回。
    [release] = of_type(engine, caller, "RELEASE")
    assert release["reason_code"] == "expired"
    assert release["idempotency_key"] is None
    assert sale_commits(engine, caller) == []
    assert quantities_of(engine, caller, sku) == (5, 0, 0, 0)

    stored = stored_reservation(engine, caller, reservation_id)
    assert stored["status"] == "late_payment_review"
    assert stored["closed_at"] == release["recorded_at"]
    assert stored["payment_ref"] == body["payment_ref"]
    assert stored["paid_at"] == PAID_AT_UTC
    assert stored["commit_request_id"] == rid_of(response)
    [record] = records(engine, caller, key, SALE_COMMITS_CREATE)
    assert record["response_status"] == 409
    assert stored["commit_request_hash"] == record["request_hash"]


# --- 先关到期 ---


def test_closing_due_reservations_first_frees_stock_for_a_late_commit(client, engine, caller):
    created, (sku,) = expired_order(client, engine, caller, [2])
    # 别的订单占着这个 SKU 的全部可售量，已到期、还没被关。
    other = data_of(reserve(client, caller, order([line("l1", sku, 5)])), 201)
    shift_into_the_past(engine, other["reservation_id"])
    assert quantities_of(engine, caller, sku) == (5, 5, 0, 0)

    data = data_of(commit(client, caller, created["order_ref"], commit_body(created)), 200)
    assert data["status"] == "committed"
    assert data["late"] is True
    assert stored_reservation(engine, caller, other["reservation_id"])["status"] == "expired"
    assert quantities_of(engine, caller, sku) == (3, 0, 0, 2)
    assert [e["entry_type"] for e in sale_commits(engine, caller)] == ["LATE_SALE_COMMIT"]
