"""SKU 合并执行的复核与重做（契约 §2.4 第 1 步、§2.8 执行第 2–4 步、§5.7 第 5 步）。

在真 MySQL 上跑；每个测试建自己的租户、池、销售入口与凭据，池先切到 live，预留经接口建立。
闸门替换服务层的 read_repoint_candidates（名单的普通读，锁预留行之前调用）：
读出名单之后，用另一个客户端经接口提交一笔来源 SKU 上的故障补交
（同 test_sale_commit_concurrency.py 在闸门里发出并发的确认），
它新建的订单行不在名单里，复核时多出来。
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.domain.service_auth import ORDERS_READ
from app.services import sku_merges as sku_merge_service
from tests.integration.test_api_auth import ALL_SCOPES, Caller, error_of, events
from tests.integration.test_counts_api import live_caller
from tests.integration.test_idempotency import add_skus, rid_of
from tests.integration.test_reservations_api import data_of, line, make_client, new_ref, stock
from tests.integration.test_sale_commits_api import commit, commit_body
from tests.integration.test_sku_merge_execute_api import (
    assert_settled,
    counted_merge,
    execute,
    execute_records,
    line_skus,
    merge_entries,
    reserve_on,
    sku_row,
)
from tests.integration.test_sku_merges_api import new_key, stored_merge
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
    """池已 live、有销售入口、带目录、库存与订单 scope 的调用方。"""
    return live_caller(engine, scopes=[*ALL_SCOPES, ORDERS_READ])


# --- 闸门 ---


def fault_after_listing(
    monkeypatch, client: TestClient, caller: Caller, sku_id: str, times: int
) -> list[tuple[list[str], str | None]]:
    """前 times 次读出名单之后，另一个客户端在 sku_id 上提交一笔 1 件的故障补交新建。

    返回每次读名单的 (名单, 这次补交的 reservation_id；没补交为 None)。
    """
    other_client = TestClient(client.app)
    calls: list[tuple[list[str], str | None]] = []
    real_read = sku_merge_service.read_repoint_candidates

    def read_then_fault(connection, tenant_id, pool_id, source_sku_id):
        found = real_read(connection, tenant_id, pool_id, source_sku_id)
        added = None
        if len(calls) < times:
            body = commit_body({"reservation_id": None, "lines": [line("l1", sku_id, 1)]})
            data = data_of(commit(other_client, caller, new_ref(), body), 200)
            assert (data["status"], data["fault_mode"]) == ("committed", True)
            added = data["reservation_id"]
        calls.append((found, added))
        return found

    monkeypatch.setattr(sku_merge_service, "read_repoint_candidates", read_then_fault)
    return calls


# --- 复核与重做 ---


def test_a_fault_line_added_after_listing_is_repointed_on_the_redo(
    client, engine, caller, monkeypatch
):
    source, canonical = add_skus(engine, caller.tenant_id, 2)
    stock(engine, caller, source, 5)
    kept = reserve_on(client, caller, source, 1)
    merge_id = counted_merge(client, caller, source, canonical, 5, 0)
    calls = fault_after_listing(monkeypatch, client, caller, source, 1)
    key = new_key()

    response = execute(client, caller, merge_id, key=key)

    data = data_of(response, 200)
    # 第一遍读名单之后多出一行，复核时回滚、从预建那一步重做；第二遍的名单里有它。
    [(first_list, fault_id), (second_list, none_added)] = calls
    assert first_list == [kept["reservation_id"]]
    assert none_added is None
    assert second_list == sorted([kept["reservation_id"], fault_id])
    assert data["execution"]["repointed_line_count"] == 2
    skus = line_skus(engine, caller)
    assert skus[(kept["reservation_id"], "l1")] == canonical
    assert skus[(fault_id, "l1")] == canonical
    # 故障补交在来源上扣成已付款待发（在架件数不变），差额为 0。
    assert_settled(engine, caller, data, (4, 1, 0, 1), (0, 0))
    [record] = execute_records(engine, caller)
    assert (record["idempotency_key"], record["response_status"]) == (key, 200)
    assert record["response_body"] == response.json()
    assert events(engine, rid_of(response)) == []


def test_extra_lines_on_every_attempt_are_unavailable_and_save_nothing(
    client, engine, caller, monkeypatch
):
    source, canonical = add_skus(engine, caller.tenant_id, 2)
    stock(engine, caller, source, 5)
    kept = reserve_on(client, caller, source, 1)
    merge_id = counted_merge(client, caller, source, canonical, 5, 0)
    real_read = sku_merge_service.read_repoint_candidates
    calls = fault_after_listing(monkeypatch, client, caller, source, 3)
    key = new_key()

    response = execute(client, caller, merge_id, key=key)

    # 一个请求最多做 3 遍；第 3 遍仍多出：503，什么也不保存。
    error = error_of(response, 503)
    assert error["code"] == "UNAVAILABLE"
    assert error["retryable"] is True
    assert len(calls) == 3
    faults = [added for _, added in calls]
    assert all(faults)
    assert stored_merge(engine, merge_id)["status"] == "counted"
    assert merge_entries(engine, caller, merge_id) == {}
    assert execute_records(engine, caller) == []
    assert events(engine, rid_of(response)) == []
    assert sku_row(engine, source)["merged_into_sku_id"] is None
    skus = line_skus(engine, caller)
    for reservation_id in [kept["reservation_id"], *faults]:
        assert skus[(reservation_id, "l1")] == source
    assert quantities_of(engine, caller, source) == (2, 1, 0, 3)

    # 没保存幂等记录：同键重试照常执行，把四行一并改挂。
    monkeypatch.setattr(sku_merge_service, "read_repoint_candidates", real_read)
    retry = execute(client, caller, merge_id, key=key)
    assert "Idempotency-Replayed" not in retry.headers
    data = data_of(retry, 200)
    assert data["execution"]["repointed_line_count"] == 4
    assert_settled(engine, caller, data, (2, 1, 0, 3), (0, 0))
