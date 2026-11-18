"""确认销售的并发：撞唯一约束的重做、报业务拒绝之前重查业务引用、合并去向变了的重做
（契约 §5.7 通用的“重新判定”、报业务拒绝之前重查业务引用、撞唯一约束，§7.1 第 2 条）。

在真 MySQL 上跑；每个测试建自己的租户、池、销售入口与凭据，池先切到 live，预留经接口建立。
闸门替换服务层的 lock_reservation_by_ref（第 1 步之后、锁预留行之前调用）
或开事务前钩子里的关单（开事务前那一步之后、业务事务之前调用）。
"""

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.services import sale_commits as sale_commit_service
from tests.integration.test_api_auth import Caller, error_of, events
from tests.integration.test_idempotency import add_skus, records, rid_of
from tests.integration.test_reservation_release_api import release, reserved_order
from tests.integration.test_reservations_api import (
    data_of,
    get_reservation,
    line,
    live_caller,
    make_client,
    new_ref,
    stock,
)
from tests.integration.test_sale_commit_late_api import (
    LATE_PAYMENT_INSUFFICIENT_STOCK,
    expired_order,
    set_unavailable,
)
from tests.integration.test_sale_commits_api import (
    PAID_AT,
    SALE_COMMITS_CREATE,
    commit,
    commit_body,
    new_key,
    new_payment_ref,
    sale_commits,
    simulate_merge,
    stored_reservation,
)
from tests.integration.test_stock_service import entries, quantities_of

BUSINESS_REF_CONFLICT = "BUSINESS_REF_CONFLICT"


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


# --- 闸门 ---


def gate_after_step_one(monkeypatch, parties: int) -> list[str]:
    """前 parties 次锁预留行都等到彼此做完第 1 步才往下走；返回经过闸门的 order_ref。

    之后的调用（例如重做）直接通过。
    """
    barrier = threading.Barrier(parties, timeout=10)
    guard = threading.Lock()
    passed: list[str] = []
    real_lock = sale_commit_service.lock_reservation_by_ref

    def gated(connection, tenant_id, channel_id, order_ref):
        with guard:
            passed.append(order_ref)
            first_round = len(passed) <= parties
        if first_round:
            barrier.wait()
        return real_lock(connection, tenant_id, channel_id, order_ref)

    monkeypatch.setattr(sale_commit_service, "lock_reservation_by_ref", gated)
    return passed


def commit_concurrently(
    client: TestClient, caller: Caller, requests: list[tuple[str, dict, str]]
) -> list:
    """requests 为 (order_ref, 正文, 幂等键)，各用一个客户端同时发出，按原顺序返回响应。"""
    clients = [client] + [TestClient(client.app) for _ in requests[1:]]
    with ThreadPoolExecutor(max_workers=len(requests)) as pool:
        futures = [
            pool.submit(commit, each, caller, order_ref, body, key)
            for each, (order_ref, body, key) in zip(clients, requests, strict=True)
        ]
        return [future.result(timeout=30) for future in futures]


# --- 撞唯一约束与同一订单的并发 ---


def test_two_keys_committing_one_order_with_one_payment_ref_write_one_set_of_entries(
    client, engine, caller, monkeypatch
):
    created, (first_sku, second_sku) = reserved_order(client, engine, caller, [3, 2])
    order_ref = created["order_ref"]
    body = commit_body(created)
    keys = [new_key(), new_key()]
    passed = gate_after_step_one(monkeypatch, 2)

    responses = commit_concurrently(client, caller, [(order_ref, body, key) for key in keys])

    # 两个请求都在第 1 步没查到；后到者锁到预留行时它已 committed，按第 1 步回答。
    assert passed == [order_ref, order_ref]
    first, second = (data_of(response, 200) for response in responses)
    assert first == second
    assert first["status"] == "committed"
    assert first["payment_ref"] == body["payment_ref"]
    assert data_of(get_reservation(client, caller, created["reservation_id"]), 200) == first
    for response in responses:
        assert events(engine, rid_of(response)) == []

    # 只写一组流水，都是先到者的。
    written = sale_commits(engine, caller)
    assert [(e["line_ref"], e["entry_type"]) for e in written] == [
        ("l1", "SALE_COMMIT"),
        ("l2", "SALE_COMMIT"),
    ]
    [winner] = {entry["idempotency_key"] for entry in written}
    assert winner in keys
    assert quantities_of(engine, caller, first_sku) == (2, 0, 0, 3)
    assert quantities_of(engine, caller, second_sku) == (3, 0, 0, 2)

    # 后到者的 200 是它那个键的确定结果。
    loser = keys[1 - keys.index(winner)]
    loser_response = responses[keys.index(loser)]
    replay = commit(client, caller, order_ref, body, loser)
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == loser_response.json()


def test_two_orders_committing_one_payment_ref_concurrently_conflict_for_the_later(
    client, engine, caller, monkeypatch
):
    first, (first_sku,) = reserved_order(client, engine, caller, [1])
    second, (second_sku,) = reserved_order(client, engine, caller, [2])
    payment_ref = new_payment_ref()
    orders = [first, second]
    requests = [(o["order_ref"], commit_body(o, payment_ref), new_key()) for o in orders]
    passed = gate_after_step_one(monkeypatch, 2)

    responses = commit_concurrently(client, caller, requests)

    # 闸门只放过两次：后到者撞上唯一约束重做时，在第 1 步就按先到者回答。
    assert sorted(passed) == sorted(o["order_ref"] for o in orders)
    assert sorted(response.status_code for response in responses) == [200, 409]
    won = 0 if responses[0].status_code == 200 else 1
    lost = 1 - won

    committed = data_of(responses[won], 200)
    assert committed["status"] == "committed"
    assert committed["payment_ref"] == payment_ref
    assert events(engine, rid_of(responses[won])) == []

    error = error_of(responses[lost], 409)
    assert error["code"] == BUSINESS_REF_CONFLICT
    assert error["details"] == {"ref_type": "payment", "ref": payment_ref}
    [event] = events(engine, rid_of(responses[lost]))
    assert event["event_type"] == "business_ref_conflict"
    assert event["details"] == {"ref_type": "payment", "ref": payment_ref}
    [record] = records(engine, caller, requests[lost][2], SALE_COMMITS_CREATE)
    assert record["response_status"] == 409

    # 后到的订单什么也没改。
    skus = [first_sku, second_sku]
    lost_stored = stored_reservation(engine, caller, orders[lost]["reservation_id"])
    assert lost_stored["status"] == "active"
    assert lost_stored["payment_ref"] is None
    assert quantities_of(engine, caller, skus[lost]) == (5, [1, 2][lost], 0, 0)
    assert [e["sku_id"] for e in sale_commits(engine, caller)] == [skus[won]]


def test_two_short_orders_going_to_review_with_one_payment_ref_conflict_for_the_later(
    client, engine, caller, monkeypatch
):
    orders = []
    for _ in range(2):
        created, (sku,) = expired_order(client, engine, caller, [2])
        # 可售 1，不够 2 件：两个订单都要转入待审。
        set_unavailable(engine, caller, sku, 4)
        orders.append(created)
    before_entries = entries(engine, caller)
    payment_ref = new_payment_ref()
    requests = [(o["order_ref"], commit_body(o, payment_ref), new_key()) for o in orders]
    passed = gate_after_step_one(monkeypatch, 2)

    responses = commit_concurrently(client, caller, requests)

    assert sorted(passed) == sorted(o["order_ref"] for o in orders)
    codes = [error_of(response, 409)["code"] for response in responses]
    assert sorted(codes) == sorted([LATE_PAYMENT_INSUFFICIENT_STOCK, BUSINESS_REF_CONFLICT])
    won = codes.index(LATE_PAYMENT_INSUFFICIENT_STOCK)
    lost = 1 - won

    assert events(engine, rid_of(responses[won])) == []
    error = error_of(responses[lost], 409)
    assert error["details"] == {"ref_type": "payment", "ref": payment_ref}
    [event] = events(engine, rid_of(responses[lost]))
    assert event["event_type"] == "business_ref_conflict"

    # 只有先到的订单转入待审；没有新的流水。
    reviewed = stored_reservation(engine, caller, orders[won]["reservation_id"])
    assert reviewed["status"] == "late_payment_review"
    assert reviewed["payment_ref"] == payment_ref
    untouched = stored_reservation(engine, caller, orders[lost]["reservation_id"])
    assert untouched["status"] == "expired"
    assert untouched["payment_ref"] is None
    assert untouched["commit_request_hash"] is None
    assert entries(engine, caller) == before_entries
    for _, _, key in requests:
        [record] = records(engine, caller, key, SALE_COMMITS_CREATE)
        assert record["response_status"] == 409


# --- 报业务拒绝之前重查业务引用 ---


@pytest.mark.parametrize(
    ("case", "original"),
    [
        ("released", "STATE_CONFLICT"),
        ("lines", "RESERVATION_MISMATCH"),
        ("reservation_id", "RESERVATION_MISMATCH"),
        ("not_found", "RESERVATION_NOT_FOUND"),
    ],
)
def test_a_rejection_answers_with_a_payment_ref_committed_after_step_one(
    client, engine, caller, monkeypatch, case, original
):
    other, (other_sku,) = reserved_order(client, engine, caller, [1])
    target, (target_sku,) = reserved_order(client, engine, caller, [2])
    target_ref = target["order_ref"]
    reservation_id = target["reservation_id"]
    lines = None
    if case == "released":
        data_of(release(client, caller, target["reservation_id"], {"reason": "cancelled"}), 200)
    elif case == "lines":
        lines = [line("l1", target_sku, 3)]
    elif case == "reservation_id":
        reservation_id = f"rsv_{uuid4().hex[:26].upper()}"
    else:
        assert case == "not_found"
        target_ref = new_ref()
        reservation_id = f"rsv_{uuid4().hex[:26].upper()}"
    payment_ref = new_payment_ref()
    body = commit_body(target, payment_ref, reservation_id=reservation_id, lines=lines)
    before_target = stored_reservation(engine, caller, target["reservation_id"])

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
    assert error["code"] == BUSINESS_REF_CONFLICT
    assert error["details"] == {"ref_type": "payment", "ref": payment_ref}
    [event] = events(engine, rid_of(response))
    assert event["event_type"] == "business_ref_conflict"
    assert event["details"] == {"ref_type": "payment", "ref": payment_ref}
    [record] = records(engine, caller, key, SALE_COMMITS_CREATE)
    assert record["response_status"] == 409

    # 本请求的订单什么也没改；只扣了别的订单。
    assert stored_reservation(engine, caller, target["reservation_id"]) == before_target
    assert [e["sku_id"] for e in sale_commits(engine, caller)] == [other_sku]

    # 换一个没人用过的 payment_ref 再报：没有并发的确认，报原来的拒绝。
    monkeypatch.setattr(sale_commit_service, "lock_reservation_by_ref", real_lock)
    fresh = commit_body(target, reservation_id=reservation_id, lines=lines)
    plain = commit(client, caller, target_ref, fresh)
    assert error_of(plain, 404 if case == "not_found" else 409)["code"] == original
    assert events(engine, rid_of(plain)) == []


# --- 合并去向变了的重做 ---


def test_a_merge_committed_after_preparing_redoes_a_late_commit_once(
    client, engine, caller, monkeypatch
):
    created, (merged,) = expired_order(client, engine, caller, [2])
    reservation_id = created["reservation_id"]
    [canonical] = add_skus(engine, caller.tenant_id, 1)
    stock(engine, caller, canonical, 5)
    seen: list[frozenset[str]] = []
    real_close = sale_commit_service.close_due_before_reserving

    def close_then_merge(connection, tenant_id, pool_id, sku_ids) -> int:
        seen.append(frozenset(sku_ids))
        closed = real_close(connection, tenant_id, pool_id, sku_ids)
        if len(seen) == 1:
            # 开事务前那一步之后、业务事务之前提交：已到期的订单行改挂到规范 SKU。
            simulate_merge(engine, caller, reservation_id, merged, canonical, 0)
        return closed

    monkeypatch.setattr(sale_commit_service, "close_due_before_reserving", close_then_merge)
    # 网店按原来下单的 SKU 报行。
    body = commit_body(created, lines=[line("l1", merged, 2)], paid_at=PAID_AT)
    key = new_key()
    data = data_of(commit(client, caller, created["order_ref"], body, key), 200)

    # 第一次只处理了原 SKU；去向不在里面，重做一次，重做时连规范 SKU 一起预建、先关到期。
    assert seen == [frozenset({merged}), frozenset({merged, canonical})]
    assert data["status"] == "committed"
    assert data["late"] is True
    assert data["lines"] == [line("l1", canonical, 2)]

    # 扣的是规范 SKU。
    assert quantities_of(engine, caller, canonical) == (3, 0, 0, 2)
    assert quantities_of(engine, caller, merged) == (5, 0, 0, 0)
    [entry] = sale_commits(engine, caller)
    assert entry["entry_type"] == "LATE_SALE_COMMIT"
    assert (entry["sku_id"], entry["stock_balance_delta"]) == (canonical, -2)
    assert entry["idempotency_key"] == key
    [record] = records(engine, caller, key, SALE_COMMITS_CREATE)
    assert record["response_status"] == 200
