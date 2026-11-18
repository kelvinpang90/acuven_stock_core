"""确认销售的故障补交新建：没有预留且 reservation_id 为 null（契约 §5.5、§5.7 第 5 步、
晚到确认、撞唯一约束与通用的“重新判定”，§7.1 第 1、2 条）。

在真 MySQL 上跑；每个测试建自己的租户、池、销售入口与凭据，
池先切到 live（不是 live 的那条除外）；
SKU 与余额行由测试直接在库里造，SKU 合并由测试直接改库模拟（自己提交的连接）。
闸门替换服务层的 lock_reservation_by_ref（第 1 步之后、锁预留行之前调用）、
lock_balances（算出合并去向之后、锁余额行之前调用），
或预留服务的 require_live（预留第 1 步之后）。
"""

from collections.abc import Iterator
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.domain.sale_commits import reserve_request_digest
from app.models.orders import OrderLine, Reservation
from app.services import reservations as reservation_service
from app.services import sale_commits as sale_commit_service
from app.services.stock import format_timestamp
from tests.integration.conftest import add_sku, add_tenant
from tests.integration.test_api_auth import Caller, error_of, events, issue
from tests.integration.test_idempotency import add_skus, records, rid_of
from tests.integration.test_reservation_release_api import reserved_order
from tests.integration.test_reservations_api import (
    ACTOR,
    ORDER_SCOPES,
    RESERVATION_KEYS,
    data_of,
    deactivate_product_of,
    deactivate_sku,
    get_reservation,
    line,
    live_caller,
    make_client,
    new_ref,
    order,
    reserve,
    rows_of,
    stock,
)
from tests.integration.test_sale_commit_concurrency import (
    commit_concurrently,
    gate_after_step_one,
)
from tests.integration.test_sale_commit_late_api import (
    LATE_PAYMENT_INSUFFICIENT_STOCK,
    LATER_PAID_AT,
    set_unavailable,
)
from tests.integration.test_sale_commits_api import (
    PAID_AT,
    PAID_AT_UTC,
    SALE_COMMITS_CREATE,
    commit,
    commit_body,
    new_key,
    new_payment_ref,
    sale_commits,
)
from tests.integration.test_stock_imports_api import merge
from tests.integration.test_stock_service import entries, quantities_of

# 与默认的 900 秒不同，验证有效期取应用当前的配置。
TTL_SECONDS = 120


@pytest.fixture
def client(migrated_engine: Engine) -> Iterator[TestClient]:
    test_client = make_client(reservation_ttl_seconds=TTL_SECONDS)
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


def fault_body(lines: list[dict], payment_ref: str | None = None, paid_at: str = PAID_AT) -> dict:
    """网店在故障期间放行、手里没有预留 ID 的确认正文。"""
    return {
        "reservation_id": None,
        "payment_ref": payment_ref or new_payment_ref(),
        "paid_at": paid_at,
        "lines": lines,
    }


def stocked_skus(engine: Engine, caller: Caller, *quantities: int) -> list[str]:
    skus = add_skus(engine, caller.tenant_id, len(quantities))
    for sku, quantity in zip(skus, quantities, strict=True):
        stock(engine, caller, sku, quantity)
    return skus


def the_reservation(engine: Engine, caller: Caller) -> dict:
    [stored] = rows_of(engine, Reservation, caller)
    return stored


def order_lines(engine: Engine, caller: Caller) -> list[dict]:
    return sorted(rows_of(engine, OrderLine, caller), key=lambda row: row["line_ref"])


def assert_nothing_created(engine: Engine, caller: Caller) -> None:
    assert rows_of(engine, Reservation, caller) == []
    assert rows_of(engine, OrderLine, caller) == []
    assert entries(engine, caller) == []


# --- 有货 ---


def test_a_fault_commit_with_stock_creates_a_committed_reservation(client, engine, caller):
    first_sku, second_sku = stocked_skus(engine, caller, 5, 5)
    order_ref = new_ref()
    # 请求里 line_ref 不按顺序；表示按 line_ref 升序。
    requested = [line("l2", second_sku, 2), line("l1", first_sku, 3)]
    body = fault_body(requested)
    key = new_key()
    response = commit(client, caller, order_ref, body, key, ACTOR)
    data = data_of(response, 200)

    assert set(data) == RESERVATION_KEYS
    assert data["reservation_id"].startswith("rsv_")
    assert data["order_ref"] == order_ref
    assert data["pool_id"] == caller.pool_id
    assert data["status"] == "committed"
    assert data["fault_mode"] is True
    assert data["late"] is True
    assert data["payment_ref"] == body["payment_ref"]
    assert data["paid_at"] == "2026-11-18T02:00:00.000000Z"
    assert data["released_at"] is None
    assert data["release_reason"] is None
    assert data["review_closed_at"] is None
    assert data["lines"] == [line("l1", first_sku, 3), line("l2", second_sku, 2)]

    # stock_balance 减该行数量，paid_pending_dispatch 加；没有预留过，reserved 不动。
    assert quantities_of(engine, caller, first_sku) == (2, 0, 0, 3)
    assert quantities_of(engine, caller, second_sku) == (3, 0, 0, 2)

    stored = the_reservation(engine, caller)
    assert stored["reservation_id"] == data["reservation_id"]
    assert stored["status"] == "committed"
    assert stored["fault_mode"]
    assert stored["late"]
    # 从未 active：closed_at 为创建时间，expires_at 为创建时间加当前配置的有效期。
    assert stored["closed_at"] == stored["created_at"]
    assert stored["committed_at"] == stored["created_at"]
    assert stored["ttl_seconds"] == TTL_SECONDS
    assert stored["expires_at"] == stored["created_at"] + timedelta(seconds=TTL_SECONDS)
    assert data["created_at"] == format_timestamp(stored["created_at"])
    assert data["expires_at"] == format_timestamp(stored["expires_at"])
    assert data["committed_at"] == format_timestamp(stored["committed_at"])
    # request_hash 是同内容预留请求的摘要；确认摘要是本请求的。
    expected_hash = reserve_request_digest(
        order_ref, [(item["line_ref"], item["sku_id"], item["quantity"]) for item in requested]
    )
    assert stored["request_hash"] == expected_hash
    [record] = records(engine, caller, key, SALE_COMMITS_CREATE)
    assert record["response_status"] == 200
    assert stored["commit_request_hash"] == record["request_hash"]
    assert stored["payment_ref"] == body["payment_ref"]
    assert stored["paid_at"] == PAID_AT_UTC
    # 创建与确认的凭据、操作者与请求 ID 都取本次请求。
    for prefix in ("", "commit_"):
        assert stored[f"{prefix}credential_id"] == caller.credential_id
        assert stored[f"{prefix}request_id"] == rid_of(response)
        assert (stored[f"{prefix}actor_ref"], stored[f"{prefix}actor_role"]) == (
            ACTOR["X-Actor-Ref"],
            ACTOR["X-Actor-Role"],
        )

    rows = order_lines(engine, caller)
    assert [(row["line_ref"], row["sku_id"], row["quantity"]) for row in rows] == [
        ("l1", first_sku, 3),
        ("l2", second_sku, 2),
    ]
    for row in rows:
        assert row["reservation_id"] == stored["reservation_id"]
        assert row["pool_id"] == caller.pool_id
        assert row["committed_qty"] == row["quantity"]

    written = sale_commits(engine, caller)
    assert [(e["line_ref"], e["sku_id"]) for e in written] == [
        ("l1", first_sku),
        ("l2", second_sku),
    ]
    for entry, quantity in zip(written, (3, 2), strict=True):
        assert entry["entry_type"] == "LATE_SALE_COMMIT"
        assert entry["reason_code"] == "fault_mode"
        assert entry["stock_balance_delta"] == -quantity
        assert entry["reserved_delta"] == 0
        assert entry["unavailable_delta"] == 0
        assert entry["paid_pending_dispatch_delta"] == quantity
        assert entry["source_type"] == "sale_commit"
        assert entry["source_ref"] == order_ref
        assert entry["idempotency_key"] == key
        assert entry["request_id"] == rid_of(response)
        assert entry["occurred_at"] == PAID_AT_UTC
        assert entry["recorded_at"] == stored["created_at"]
    assert written[0]["command_id"] == written[1]["command_id"]
    # 只有确认的流水：没有 RESERVE。
    assert len(entries(engine, caller)) == 2

    assert data_of(get_reservation(client, caller, data["reservation_id"]), 200) == data

    # 同键原样重放。
    replay = commit(client, caller, order_ref, body, key, ACTOR)
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == response.json()

    # 之后用同样的行补调预留：200 与这个预留；行不同 409 ORDER_REFERENCE_CONFLICT。
    again = reserve(client, caller, order(requested, order_ref))
    assert data_of(again, 200) == data
    assert events(engine, rid_of(again)) == []
    changed = reserve(client, caller, order([line("l1", first_sku, 3)], order_ref))
    error = error_of(changed, 409)
    assert error["code"] == "ORDER_REFERENCE_CONFLICT"
    assert error["details"] == {"ref_type": "order", "ref": order_ref}
    assert len(rows_of(engine, Reservation, caller)) == 1
    assert len(entries(engine, caller)) == 2


# --- 缺货 ---


def test_a_fault_commit_without_stock_goes_to_review_and_a_retry_after_restocking_commits(
    client, engine, caller
):
    short_sku, enough_sku = stocked_skus(engine, caller, 5, 5)
    # short_sku 可售 1，不够 2 件；enough_sku 够。
    set_unavailable(engine, caller, short_sku, 4)
    order_ref = new_ref()
    body = fault_body([line("l1", enough_sku, 1), line("l2", short_sku, 2)])
    key = new_key()
    response = commit(client, caller, order_ref, body, key, ACTOR)

    error = error_of(response, 409)
    assert error["code"] == LATE_PAYMENT_INSUFFICIENT_STOCK
    assert error["details"] == {
        "lines": [{"line_ref": "l2", "sku_id": short_sku, "quantity": 2, "available": 1}]
    }
    assert events(engine, rid_of(response)) == []

    # 被拒仍保留的写入：预留与订单行，没有流水，数量不变。
    stored = the_reservation(engine, caller)
    assert stored["order_ref"] == order_ref
    assert stored["status"] == "late_payment_review"
    assert stored["fault_mode"]
    assert not stored["late"]
    assert stored["committed_at"] is None
    assert stored["closed_at"] == stored["created_at"]
    assert stored["expires_at"] == stored["created_at"] + timedelta(seconds=TTL_SECONDS)
    assert stored["payment_ref"] == body["payment_ref"]
    assert stored["paid_at"] == PAID_AT_UTC
    [record] = records(engine, caller, key, SALE_COMMITS_CREATE)
    assert record["response_status"] == 409
    assert stored["commit_request_hash"] == record["request_hash"]
    assert stored["commit_request_id"] == stored["request_id"] == rid_of(response)
    assert stored["commit_actor_ref"] == ACTOR["X-Actor-Ref"]
    for row in order_lines(engine, caller):
        assert row["committed_qty"] == 0
    assert entries(engine, caller) == []
    assert quantities_of(engine, caller, short_sku) == (5, 0, 4, 0)
    assert quantities_of(engine, caller, enough_sku) == (5, 0, 0, 0)
    shown = data_of(get_reservation(client, caller, stored["reservation_id"]), 200)
    assert shown["status"] == "late_payment_review"
    assert shown["fault_mode"] is True
    assert shown["committed_at"] is None

    # 同键只重放 409。
    replay = commit(client, caller, order_ref, body, key, ACTOR)
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == response.json()
    assert len(rows_of(engine, Reservation, caller)) == 1

    # 补货后换键、同一 payment_ref 重试（付款时间报得不同也照第一次的）。
    set_unavailable(engine, caller, short_sku, 0)
    retry = fault_body(body["lines"], body["payment_ref"], paid_at=LATER_PAID_AT)
    retry_key = new_key()
    data = data_of(commit(client, caller, order_ref, retry, retry_key), 200)
    assert data["reservation_id"] == stored["reservation_id"]
    assert data["status"] == "committed"
    assert data["late"] is True
    assert data["fault_mode"] is True
    assert data["paid_at"] == "2026-11-18T02:00:00.000000Z"
    assert quantities_of(engine, caller, short_sku) == (3, 0, 0, 2)
    assert quantities_of(engine, caller, enough_sku) == (4, 0, 0, 1)

    committed = the_reservation(engine, caller)
    assert committed["status"] == "committed"
    assert committed["closed_at"] == stored["closed_at"]
    assert committed["paid_at"] == PAID_AT_UTC
    for row in order_lines(engine, caller):
        assert row["committed_qty"] == row["quantity"]
    written = sale_commits(engine, caller)
    assert [(e["line_ref"], e["entry_type"], e["reason_code"]) for e in written] == [
        ("l1", "LATE_SALE_COMMIT", "fault_mode"),
        ("l2", "LATE_SALE_COMMIT", "fault_mode"),
    ]
    for entry in written:
        assert entry["idempotency_key"] == retry_key
        assert entry["occurred_at"] == PAID_AT_UTC


# --- 拒绝与停用 ---


def test_a_missing_or_foreign_sku_is_not_found_and_creates_nothing(client, engine, caller):
    [own] = stocked_skus(engine, caller, 5)
    with engine.begin() as db:
        foreign = add_sku(db, add_tenant(db))
    missing = f"sku_{uuid4().hex[:26].upper()}"
    order_ref = new_ref()
    body = fault_body([line("l1", foreign, 1), line("l2", own, 1), line("l3", missing, 1)])
    key = new_key()
    response = commit(client, caller, order_ref, body, key)

    error = error_of(response, 404)
    assert error["code"] == "SKU_NOT_FOUND"
    assert error["details"] == {"sku_ids": [foreign, missing]}
    # 没有并发的确认：原来的拒绝是确定结果，登记的 cross_tenant 保留。
    [event] = events(engine, rid_of(response))
    assert event["event_type"] == "cross_tenant"
    assert event["details"] == {"object_type": "sku", "id": foreign}
    [record] = records(engine, caller, key, SALE_COMMITS_CREATE)
    assert record["response_status"] == 404
    assert_nothing_created(engine, caller)
    assert quantities_of(engine, caller, own) == (5, 0, 0, 0)

    # 不占用 order_ref 与 payment_ref：改正后换键可以成功。
    fixed = fault_body([line("l2", own, 1)], body["payment_ref"])
    assert data_of(commit(client, caller, order_ref, fixed), 200)["status"] == "committed"


def test_a_pool_that_is_not_live_is_rejected_and_creates_nothing(client, engine):
    caller = issue(engine, scopes=ORDER_SCOPES)
    [sku] = stocked_skus(engine, caller, 5)
    key = new_key()
    response = commit(client, caller, new_ref(), fault_body([line("l1", sku, 1)]), key)

    error = error_of(response, 409)
    assert error["code"] == "POOL_NOT_LIVE"
    assert events(engine, rid_of(response)) == []
    [record] = records(engine, caller, key, SALE_COMMITS_CREATE)
    assert record["response_status"] == 409
    assert_nothing_created(engine, caller)


def test_an_inactive_sku_or_product_is_committed_as_usual(client, engine, caller):
    inactive_sku, inactive_product = stocked_skus(engine, caller, 5, 5)
    deactivate_sku(engine, inactive_sku)
    deactivate_product_of(engine, inactive_product)
    body = fault_body([line("l1", inactive_sku, 1), line("l2", inactive_product, 2)])

    data = data_of(commit(client, caller, new_ref(), body), 200)
    assert data["status"] == "committed"
    assert data["fault_mode"] is True
    assert quantities_of(engine, caller, inactive_sku) == (4, 0, 0, 1)
    assert quantities_of(engine, caller, inactive_product) == (3, 0, 0, 2)
    assert len(sale_commits(engine, caller)) == 2


def test_a_rejection_answers_with_a_payment_ref_committed_after_step_one_dropping_cross_tenant(
    client, engine, caller, monkeypatch
):
    other, (other_sku,) = reserved_order(client, engine, caller, [1])
    [own] = stocked_skus(engine, caller, 5)
    with engine.begin() as db:
        foreign = add_sku(db, add_tenant(db))
    target_ref = new_ref()
    payment_ref = new_payment_ref()
    body = fault_body([line("l1", foreign, 1), line("l2", own, 1)], payment_ref)

    # 本请求做完第 1 步（没查到）之后，别的订单用同一 payment_ref 确认并提交。
    other_client = TestClient(client.app)
    other_responses = []
    real_lock = sale_commit_service.lock_reservation_by_ref

    def gated(connection, tenant_id, channel_id, order_ref):
        if order_ref == target_ref and not other_responses:
            other_body = commit_body(other, payment_ref)
            other_responses.append(commit(other_client, caller, other["order_ref"], other_body))
        return real_lock(connection, tenant_id, channel_id, order_ref)

    monkeypatch.setattr(sale_commit_service, "lock_reservation_by_ref", gated)
    key = new_key()
    response = commit(client, caller, target_ref, body, key)

    [other_response] = other_responses
    assert data_of(other_response, 200)["status"] == "committed"
    error = error_of(response, 409)
    assert error["code"] == "BUSINESS_REF_CONFLICT"
    assert error["details"] == {"ref_type": "payment", "ref": payment_ref}
    # 改按第 1 步回答：本次登记的 cross_tenant 丢弃，只有业务引用冲突。
    [event] = events(engine, rid_of(response))
    assert event["event_type"] == "business_ref_conflict"
    [record] = records(engine, caller, key, SALE_COMMITS_CREATE)
    assert record["response_status"] == 409
    assert [row["order_ref"] for row in rows_of(engine, Reservation, caller)] == [
        other["order_ref"]
    ]
    assert [e["sku_id"] for e in sale_commits(engine, caller)] == [other_sku]
    assert quantities_of(engine, caller, own) == (5, 0, 0, 0)


# --- 合并 ---


def test_a_merged_sku_in_the_request_puts_the_line_on_the_canonical_sku(client, engine, caller):
    merged, canonical = stocked_skus(engine, caller, 5, 5)
    merge(engine, merged, canonical)
    order_ref = new_ref()
    requested = [line("l1", merged, 2)]

    data = data_of(commit(client, caller, order_ref, fault_body(requested)), 200)
    assert data["status"] == "committed"
    assert data["lines"] == [line("l1", canonical, 2)]
    [row] = order_lines(engine, caller)
    assert (row["sku_id"], row["committed_qty"]) == (canonical, 2)
    assert quantities_of(engine, caller, canonical) == (3, 0, 0, 2)
    assert quantities_of(engine, caller, merged) == (5, 0, 0, 0)
    [entry] = sale_commits(engine, caller)
    assert (entry["sku_id"], entry["stock_balance_delta"]) == (canonical, -2)

    # request_hash 按请求的行（原 SKU）算：之后按原来下单的行补调预留也得到这个预留。
    assert data_of(reserve(client, caller, order(requested, order_ref)), 200) == data


def test_a_merge_committed_before_locking_balances_redoes_onto_the_new_destination(
    client, engine, caller, monkeypatch
):
    original, canonical = stocked_skus(engine, caller, 5, 5)
    locked: list[frozenset[str]] = []
    prepared: list[frozenset[str]] = []
    real_lock_balances = sale_commit_service.lock_balances
    real_close = sale_commit_service.close_due_before_reserving

    def close_and_record(connection, tenant_id, pool_id, sku_ids) -> int:
        prepared.append(frozenset(sku_ids))
        return real_close(connection, tenant_id, pool_id, sku_ids)

    def merge_then_lock(execution, pool_id, sku_ids):
        sku_ids = frozenset(sku_ids)
        locked.append(sku_ids)
        if len(locked) == 1:
            # 算出去向（original 自己）之后、锁余额行之前，一笔合并提交。
            merge(engine, original, canonical)
        return real_lock_balances(execution, pool_id, sku_ids)

    monkeypatch.setattr(sale_commit_service, "close_due_before_reserving", close_and_record)
    monkeypatch.setattr(sale_commit_service, "lock_balances", merge_then_lock)
    key = new_key()
    body = fault_body([line("l1", original, 2)])
    data = data_of(commit(client, caller, new_ref(), body, key), 200)

    # 锁余额行之后读 SKU 行看到已合并：重做一次，重做时连新去向一起预建、先关到期。
    assert locked == [frozenset({original}), frozenset({canonical})]
    assert prepared == [frozenset({original}), frozenset({original, canonical})]
    assert data["status"] == "committed"
    assert data["lines"] == [line("l1", canonical, 2)]
    [row] = order_lines(engine, caller)
    assert row["sku_id"] == canonical
    assert quantities_of(engine, caller, canonical) == (3, 0, 0, 2)
    assert quantities_of(engine, caller, original) == (5, 0, 0, 0)
    [entry] = sale_commits(engine, caller)
    assert entry["sku_id"] == canonical
    assert entry["idempotency_key"] == key
    [record] = records(engine, caller, key, SALE_COMMITS_CREATE)
    assert record["response_status"] == 200
    assert len(rows_of(engine, Reservation, caller)) == 1


# --- 并发 ---


def test_a_reservation_committed_before_the_fault_insert_is_committed_on_redo(
    client, engine, caller, monkeypatch
):
    [sku] = stocked_skus(engine, caller, 5)
    order_ref = new_ref()
    requested = [line("l1", sku, 2)]
    other_client = TestClient(client.app)
    reserved = []
    real_lock = sale_commit_service.lock_reservation_by_ref

    def gated(connection, tenant_id, channel_id, ref):
        found = real_lock(connection, tenant_id, channel_id, ref)
        if found is None and not reserved:
            # 本请求锁预留行没读到之后，同一 order_ref 的预留提交。
            reserved.append(reserve(other_client, caller, order(requested, order_ref)))
        return found

    monkeypatch.setattr(sale_commit_service, "lock_reservation_by_ref", gated)
    key = new_key()
    data = data_of(commit(client, caller, order_ref, fault_body(requested), key), 200)

    # 写预留撞上 order_ref 的唯一约束：重做后按已有的预留正常确认。
    [reserve_response] = reserved
    created = data_of(reserve_response, 201)
    assert data["reservation_id"] == created["reservation_id"]
    assert data["status"] == "committed"
    assert data["late"] is False
    assert data["fault_mode"] is True
    assert len(rows_of(engine, Reservation, caller)) == 1
    assert [e["entry_type"] for e in entries(engine, caller)] == ["RESERVE", "SALE_COMMIT"]
    [entry] = sale_commits(engine, caller)
    assert entry["reason_code"] == "fault_mode"
    assert entry["idempotency_key"] == key
    assert quantities_of(engine, caller, sku) == (3, 0, 0, 2)
    [record] = records(engine, caller, key, SALE_COMMITS_CREATE)
    assert record["response_status"] == 200


def test_a_reservation_after_a_concurrent_fault_commit_answers_with_the_created_one(
    client, engine, caller, monkeypatch
):
    [sku] = stocked_skus(engine, caller, 5)
    order_ref = new_ref()
    requested = [line("l1", sku, 2)]
    other_client = TestClient(client.app)
    committed = []
    real_require_live = reservation_service.require_live

    def gated(execution, pool_id):
        if not committed:
            # 预留做完第 1 步（没查到）之后，同一 order_ref 的故障补交提交。
            committed.append(commit(other_client, caller, order_ref, fault_body(requested)))
        real_require_live(execution, pool_id)

    monkeypatch.setattr(reservation_service, "require_live", gated)
    response = reserve(client, caller, order(requested, order_ref))

    [commit_response] = committed
    created = data_of(commit_response, 200)
    # 预留撞上 order_ref 的唯一约束，按已有的预留回答：摘要相同，200。
    assert data_of(response, 200) == created
    assert events(engine, rid_of(response)) == []
    assert len(rows_of(engine, Reservation, caller)) == 1
    assert [e["entry_type"] for e in entries(engine, caller)] == ["LATE_SALE_COMMIT"]
    assert quantities_of(engine, caller, sku) == (3, 0, 0, 2)


def test_two_keys_creating_one_order_from_fault_create_one_reservation(
    client, engine, caller, monkeypatch
):
    first_sku, second_sku = stocked_skus(engine, caller, 5, 5)
    order_ref = new_ref()
    body = fault_body([line("l1", first_sku, 3), line("l2", second_sku, 2)])
    keys = [new_key(), new_key()]
    passed = gate_after_step_one(monkeypatch, 2)

    responses = commit_concurrently(client, caller, [(order_ref, body, key) for key in keys])

    # 两个请求都没读到预留；后到者写预留撞唯一约束，重做时在第 1 步按先到者回答。
    assert passed == [order_ref, order_ref]
    first, second = (data_of(response, 200) for response in responses)
    assert first == second
    assert first["status"] == "committed"
    assert first["fault_mode"] is True
    for response in responses:
        assert events(engine, rid_of(response)) == []

    [stored] = rows_of(engine, Reservation, caller)
    assert stored["reservation_id"] == first["reservation_id"]
    assert len(rows_of(engine, OrderLine, caller)) == 2
    written = sale_commits(engine, caller)
    assert [(e["line_ref"], e["entry_type"]) for e in written] == [
        ("l1", "LATE_SALE_COMMIT"),
        ("l2", "LATE_SALE_COMMIT"),
    ]
    [winner] = {entry["idempotency_key"] for entry in written}
    assert winner in keys
    assert quantities_of(engine, caller, first_sku) == (2, 0, 0, 3)
    assert quantities_of(engine, caller, second_sku) == (3, 0, 0, 2)
    for key in keys:
        [record] = records(engine, caller, key, SALE_COMMITS_CREATE)
        assert record["response_status"] == 200
