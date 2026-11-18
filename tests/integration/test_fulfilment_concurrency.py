"""发货与退款回库的并发：同一引用换键并发重报（契约 §5.8 报业务拒绝之前重查业务引用、
撞唯一约束，§7.1 第 2 条）。

在真 MySQL 上跑；每个测试建自己的租户、池、销售入口与凭据，池先切到 live，预留与确认经接口建立。
闸门替换服务层的 lock_reservation_by_ref（第 1 步之后、锁预留行之前调用），
让两个请求都做完第 1 步（都没查到单据）再去锁预留行；
另把 insert_order_document 换成记下调用次数的包装，区分后到者走的是哪一支。
"""

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.services import fulfilment as fulfilment_service
from tests.integration.test_api_auth import Caller, error_of, events
from tests.integration.test_fulfilment_api import (
    DISPATCHES,
    KINDS,
    REFUND_RESTOCKS,
    body_of,
    committed_order,
    documents,
    new_key,
    of_type,
    post,
    qline,
    totals,
)
from tests.integration.test_idempotency import records, rid_of
from tests.integration.test_reservations_api import data_of, live_caller, make_client
from tests.integration.test_stock_service import quantities_of


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

    之后的调用直接通过。
    """
    barrier = threading.Barrier(parties, timeout=10)
    guard = threading.Lock()
    passed: list[str] = []
    real_lock = fulfilment_service.lock_reservation_by_ref

    def gated(connection, tenant_id, channel_id, order_ref):
        with guard:
            passed.append(order_ref)
            first_round = len(passed) <= parties
        if first_round:
            barrier.wait()
        return real_lock(connection, tenant_id, channel_id, order_ref)

    monkeypatch.setattr(fulfilment_service, "lock_reservation_by_ref", gated)
    return passed


def count_inserts(monkeypatch) -> list[str]:
    """记下每次写订单单据的尝试（含撞上唯一约束的那次），返回记下的 document_ref。"""
    guard = threading.Lock()
    attempts: list[str] = []
    real_insert = fulfilment_service.insert_order_document

    def counted(execution, document_type, document_ref, *args):
        with guard:
            attempts.append(document_ref)
        return real_insert(execution, document_type, document_ref, *args)

    monkeypatch.setattr(fulfilment_service, "insert_order_document", counted)
    return attempts


def post_concurrently(
    client: TestClient, caller: Caller, kind: str, order_ref: str, requests: list[tuple[dict, str]]
) -> list:
    """requests 为 (正文, 幂等键)，各用一个客户端同时发出，按原顺序返回响应。"""
    clients = [client] + [TestClient(client.app) for _ in requests[1:]]
    with ThreadPoolExecutor(max_workers=len(requests)) as pool:
        futures = [
            pool.submit(post, each, caller, kind, order_ref, body, key)
            for each, (body, key) in zip(clients, requests, strict=True)
        ]
        return [future.result(timeout=30) for future in futures]


# --- 同一引用、换键、同内容 ---


@pytest.mark.parametrize("kind", [DISPATCHES, REFUND_RESTOCKS])
@pytest.mark.parametrize(
    ("ordered", "inserts"),
    [
        # 余量只够一次：后到者锁到订单行时余量已被用掉，报拒绝之前重查业务引用，按第 1 步回答。
        (2, 1),
        # 余量够两次：后到者照常写到单据，撞上唯一约束，回滚、另开事务按第 1 步重新判定。
        (4, 2),
    ],
)
def test_one_reference_on_two_keys_concurrently_gives_both_the_same_document(
    client, engine, caller, monkeypatch, kind, ordered, inserts
):
    operation, _, _, _, entry_type, _ = KINDS[kind]
    created, (sku,) = committed_order(client, engine, caller, [ordered])
    order_ref = created["order_ref"]
    body = body_of(kind, [qline("l1", 2)])
    keys = [new_key(), new_key()]
    passed = gate_after_step_one(monkeypatch, 2)
    attempts = count_inserts(monkeypatch)

    responses = post_concurrently(client, caller, kind, order_ref, [(body, key) for key in keys])

    # 两个请求都在第 1 步没查到；不出 5xx，后到者也不得 409。
    assert passed == [order_ref, order_ref]
    assert sorted(response.status_code for response in responses) == [200, 201]
    assert len(attempts) == inserts
    won = 0 if responses[0].status_code == 201 else 1
    lost = 1 - won
    document = data_of(responses[won], 201)
    assert data_of(responses[lost], 200) == document
    for response in responses:
        assert events(engine, rid_of(response)) == []

    # 只写一份流水与一行单据，都是先到者的。
    [entry] = of_type(engine, caller, entry_type)
    assert entry["idempotency_key"] == keys[won]
    assert entry["entry_id"] == document["lines"][0]["entry_id"]
    [stored] = documents(engine, caller)
    assert stored["document_id"] == document["document_id"]
    assert stored["request_id"] == rid_of(responses[won])
    dispatched, restocked = totals(engine, caller)["l1"]
    assert dispatched + restocked == 2
    balance = quantities_of(engine, caller, sku)
    assert balance[3] == ordered - 2

    # 两个键各有自己的确定结果；后到者的 200 原样重放。
    for key, status in ((keys[won], 201), (keys[lost], 200)):
        [record] = records(engine, caller, key, operation)
        assert record["response_status"] == status
    replay = post(client, caller, kind, order_ref, body, keys[lost])
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == responses[lost].json()


# --- 同一引用、换键、不同内容 ---


@pytest.mark.parametrize("kind", [DISPATCHES, REFUND_RESTOCKS])
def test_one_reference_with_different_content_concurrently_conflicts_for_the_later(
    client, engine, caller, monkeypatch, kind
):
    operation, ref_field, ref_type, _, entry_type, _ = KINDS[kind]
    created, (sku,) = committed_order(client, engine, caller, [4])
    order_ref = created["order_ref"]
    first = body_of(kind, [qline("l1", 2)])
    ref = first[ref_field]
    second = body_of(kind, [qline("l1", 1)], ref)
    requests = [(first, new_key()), (second, new_key())]
    passed = gate_after_step_one(monkeypatch, 2)

    responses = post_concurrently(client, caller, kind, order_ref, requests)

    assert passed == [order_ref, order_ref]
    assert sorted(response.status_code for response in responses) == [201, 409]
    won = 0 if responses[0].status_code == 201 else 1
    lost = 1 - won
    document = data_of(responses[won], 201)
    assert events(engine, rid_of(responses[won])) == []

    error = error_of(responses[lost], 409)
    assert error["code"] == "BUSINESS_REF_CONFLICT"
    assert error["details"] == {"ref_type": ref_type, "ref": ref}
    [event] = events(engine, rid_of(responses[lost]))
    assert event["event_type"] == "business_ref_conflict"
    assert event["details"] == {"ref_type": ref_type, "ref": ref}
    [record] = records(engine, caller, requests[lost][1], operation)
    assert record["response_status"] == 409

    # 只有先到者生效。
    quantity = requests[won][0]["lines"][0]["quantity"]
    [entry] = of_type(engine, caller, entry_type)
    assert entry["idempotency_key"] == requests[won][1]
    assert entry["paid_pending_dispatch_delta"] == -quantity
    [stored] = documents(engine, caller)
    assert stored["document_id"] == document["document_id"]
    dispatched, restocked = totals(engine, caller)["l1"]
    assert dispatched + restocked == quantity
    assert quantities_of(engine, caller, sku)[3] == 4 - quantity
