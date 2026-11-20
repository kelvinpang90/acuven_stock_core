"""SKU 合并的停售：新预留与可售量（契约 §2.4、§2.8 停售的判定、§5.6、§5.7）。

在真 MySQL 上跑；每个测试建自己的租户、池、销售入口与凭据，池先切到 live。
SKU 由测试直接在库里造，库存用收货接口收进来（留下本租户的收货流水），合并单用合并接口建。
执行不在本任务里：要 reconcile_pending 的单时直接改库，执行那组列的两个迁移流水 ID
复用本租户已有的一条收货流水，来源 SKU 按执行的结果置为已合并。
"""

from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.config import Settings
from app.domain.requests import STOCK_ADMIN
from app.domain.service_auth import ORDERS_READ
from app.main import create_app
from app.models.orders import OrderLine, Reservation
from tests.integration.conftest import TEST_DATABASE_URL
from tests.integration.test_api_auth import ALL_SCOPES, Caller, error_of
from tests.integration.test_catalog_create_api import created
from tests.integration.test_counts_api import live_caller, receive
from tests.integration.test_idempotency import add_skus
from tests.integration.test_reservation_release_api import release
from tests.integration.test_reservations_api import (
    ACTOR,
    data_of,
    deactivate_sku,
    line,
    new_ref,
    order,
    reserve,
    rows_of,
)
from tests.integration.test_sale_commit_fault_api import fault_body
from tests.integration.test_sale_commits_api import commit, commit_body
from tests.integration.test_sku_merges_api import (
    ACTOR_REF,
    EXECUTED_AT,
    cancel,
    complete,
    count,
    create,
    request_id,
    set_merge,
    stored_merge,
)
from tests.integration.test_stock_imports_api import merge
from tests.integration.test_stock_service import entries, quantities_of

AVAILABILITY = "/v1/availability"


@pytest.fixture
def client(migrated_engine: Engine) -> Iterator[TestClient]:
    app = create_app(Settings(env="demo", git_sha="c" * 40, database_url=TEST_DATABASE_URL))
    test_client = TestClient(app)
    try:
        yield test_client
    finally:
        app.state.engine.dispose()


@pytest.fixture
def engine(migrated_engine: Engine) -> Engine:
    return migrated_engine


@pytest.fixture
def caller(engine: Engine) -> Caller:
    """池已 live、有销售入口，能管合并、收货、预留、确认与查可售量的调用方。"""
    return live_caller(engine, scopes=[*ALL_SCOPES, ORDERS_READ])


# --- 造数据与观察 ---


def stocked_skus(client: TestClient, engine: Engine, caller: Caller, number: int) -> list[str]:
    """number 个新 SKU，各收货 10 件。"""
    skus = add_skus(engine, caller.tenant_id, number)
    for sku in skus:
        receive(client, caller, sku, 10)
    return skus


def new_merge(client: TestClient, caller: Caller, source: str, canonical: str) -> str:
    data = created(create(client, caller, source, canonical))
    assert data["status"] == "selling_paused"
    return data["merge_id"]


def mark_executed(engine: Engine, caller: Caller, merge_id: str) -> None:
    """直接改库把录入过实数的单置为 reconcile_pending，来源 SKU 置为已合并到规范 SKU。

    两个迁移流水 ID 复用本租户已有的一条收货流水。
    """
    entry_id = entries(engine, caller)[0]["entry_id"]
    set_merge(
        engine,
        merge_id,
        status="reconcile_pending",
        executed_credential_id=caller.credential_id,
        executed_actor_ref=ACTOR_REF,
        executed_actor_role=STOCK_ADMIN,
        executed_request_id=request_id(),
        executed_at=EXECUTED_AT,
        transfer_out_entry_id=entry_id,
        transfer_in_entry_id=entry_id,
        repointed_line_count=0,
    )
    row = stored_merge(engine, merge_id)
    merge(engine, row["source_sku_id"], row["canonical_sku_id"])


def statuses(client: TestClient, caller: Caller, sku_ids: list[str]) -> dict[str, str]:
    headers = caller.auth | {"X-Request-ID": f"paused-test-{uuid4().hex}"}
    response = client.get(AVAILABILITY, params={"sku_ids": ",".join(sku_ids)}, headers=headers)
    return {item["sku_id"]: item["status"] for item in data_of(response, 200)["skus"]}


def assert_paused(response, sku_ids: list[str]) -> None:
    error = error_of(response, 409)
    assert error["code"] == "SKU_SELLING_PAUSED"
    assert error["details"] == {"sku_ids": sku_ids}


def assert_nothing_reserved(engine: Engine, caller: Caller, skus: list[str]) -> None:
    assert rows_of(engine, Reservation, caller) == []
    assert rows_of(engine, OrderLine, caller) == []
    for sku in skus:
        assert quantities_of(engine, caller, sku) == (10, 0, 0, 0)


# --- 新预留 ---


def test_a_new_merge_pauses_new_reservations_of_both_skus(client, engine, caller):
    source, canonical, other = stocked_skus(client, engine, caller, 3)
    merge_id = new_merge(client, caller, source, canonical)

    # 按请求顺序列出全部停售中的 SKU，不停售的不列。
    body = order([line("l1", other, 1), line("l2", canonical, 2), line("l3", source, 3)])
    key = f"shop:reserve:{uuid4().hex}"
    response = reserve(client, caller, body, key)
    assert_paused(response, [canonical, source])
    assert_nothing_reserved(engine, caller, [source, canonical, other])
    # 确定结果按幂等键保存。
    replay = reserve(client, caller, body, key)
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == response.json()

    # 录入实数之后（counted）两个 SKU 仍停售。
    data_of(count(client, caller, merge_id, 20), 200)
    assert_paused(reserve(client, caller, body), [canonical, source])
    assert_paused(reserve(client, caller, order([line("l1", source, 1)])), [source])
    assert_nothing_reserved(engine, caller, [source, canonical, other])

    # 不在合并单上的 SKU 照常预留。
    data_of(reserve(client, caller, order([line("l1", other, 1)])), 201)


def test_merged_and_inactive_are_reported_before_selling_paused(client, engine, caller):
    source, canonical, merged, target, inactive = stocked_skus(client, engine, caller, 5)
    merge(engine, merged, target)
    deactivate_sku(engine, inactive)
    new_merge(client, caller, source, canonical)

    body = order([line("l1", source, 1), line("l2", merged, 1)])
    error = error_of(reserve(client, caller, body), 409)
    assert error["code"] == "SKU_MERGED"
    assert error["details"] == {"sku_id": merged, "merged_into_sku_id": target}

    body = order([line("l1", canonical, 1), line("l2", inactive, 1)])
    error = error_of(reserve(client, caller, body), 409)
    assert error["code"] == "SKU_INACTIVE"
    assert error["details"] == {"sku_ids": [inactive]}

    # 停用的来源 SKU 也可以建合并单：报停用，不报停售。
    inactive_source, other_canonical = stocked_skus(client, engine, caller, 2)
    deactivate_sku(engine, inactive_source)
    new_merge(client, caller, inactive_source, other_canonical)
    error = error_of(reserve(client, caller, order([line("l1", inactive_source, 1)])), 409)
    assert error["code"] == "SKU_INACTIVE"
    body = order([line("l1", other_canonical, 1)])
    assert_paused(reserve(client, caller, body), [other_canonical])
    assert rows_of(engine, Reservation, caller) == []


@pytest.mark.parametrize("counted", [False, True])
def test_cancelling_lets_the_same_order_ref_reserve_on_a_new_key(client, engine, caller, counted):
    source, canonical = stocked_skus(client, engine, caller, 2)
    merge_id = new_merge(client, caller, source, canonical)
    if counted:
        data_of(count(client, caller, merge_id, 20), 200)
    order_ref = new_ref()
    body = order([line("l1", source, 2), line("l2", canonical, 3)], order_ref)
    assert_paused(reserve(client, caller, body), [source, canonical])
    # 拒绝不占用 order_ref。
    assert_nothing_reserved(engine, caller, [source, canonical])

    assert data_of(cancel(client, caller, merge_id), 200)["status"] == "cancelled"
    data = data_of(reserve(client, caller, body), 201)
    assert (data["order_ref"], data["status"]) == (order_ref, "active")
    assert quantities_of(engine, caller, source) == (10, 2, 0, 0)
    assert quantities_of(engine, caller, canonical) == (10, 3, 0, 0)


def test_reconcile_pending_keeps_the_canonical_paused_until_completed(client, engine, caller):
    source, canonical = stocked_skus(client, engine, caller, 2)
    merge_id = new_merge(client, caller, source, canonical)
    data_of(count(client, caller, merge_id, 20), 200)
    mark_executed(engine, caller, merge_id)

    order_ref = new_ref()
    body = order([line("l1", canonical, 4)], order_ref)
    assert_paused(reserve(client, caller, body), [canonical])
    # 来源已合并，按已合并报。
    error = error_of(reserve(client, caller, order([line("l1", source, 1)])), 409)
    assert error["code"] == "SKU_MERGED"
    assert error["details"] == {"sku_id": source, "merged_into_sku_id": canonical}
    assert rows_of(engine, Reservation, caller) == []

    assert data_of(complete(client, caller, merge_id), 200)["status"] == "completed"
    data = data_of(reserve(client, caller, body), 201)
    assert (data["order_ref"], data["status"]) == (order_ref, "active")
    assert quantities_of(engine, caller, canonical) == (10, 4, 0, 0)


# --- 可售量 ---


def test_availability_reports_selling_paused_until_cancelled(client, engine, caller):
    source, canonical, other = stocked_skus(client, engine, caller, 3)
    requested = [source, canonical, other]
    assert statuses(client, caller, requested) == dict.fromkeys(requested, "sellable")

    merge_id = new_merge(client, caller, source, canonical)
    paused = {source: "selling_paused", canonical: "selling_paused", other: "sellable"}
    assert statuses(client, caller, requested) == paused
    data_of(count(client, caller, merge_id, 20), 200)
    assert statuses(client, caller, requested) == paused

    data_of(cancel(client, caller, merge_id), 200)
    assert statuses(client, caller, requested) == dict.fromkeys(requested, "sellable")


def test_availability_reports_selling_paused_until_completed(client, engine, caller):
    source, canonical, inactive = stocked_skus(client, engine, caller, 3)
    deactivate_sku(engine, inactive)
    merge_id = new_merge(client, caller, source, canonical)
    data_of(count(client, caller, merge_id, 20), 200)
    mark_executed(engine, caller, merge_id)

    # 来源已合并报 merged；规范仍停售。
    assert statuses(client, caller, [source, canonical]) == {
        source: "merged",
        canonical: "selling_paused",
    }
    data_of(complete(client, caller, merge_id), 200)
    assert statuses(client, caller, [source, canonical]) == {
        source: "merged",
        canonical: "sellable",
    }

    # 停用的来源 SKU 在合并单上报 inactive，不报 selling_paused。
    other_canonical = stocked_skus(client, engine, caller, 1)[0]
    new_merge(client, caller, inactive, other_canonical)
    assert statuses(client, caller, [inactive, other_canonical]) == {
        inactive: "inactive",
        other_canonical: "selling_paused",
    }


# --- 停售只停新预留 ---


def test_existing_reservations_and_fault_commits_ignore_the_pause(client, engine, caller):
    source, canonical = stocked_skus(client, engine, caller, 2)
    to_release = data_of(
        reserve(client, caller, order([line("l1", source, 2), line("l2", canonical, 1)])), 201
    )
    to_commit = data_of(
        reserve(client, caller, order([line("l1", source, 3), line("l2", canonical, 2)])), 201
    )
    merge_id = new_merge(client, caller, source, canonical)
    assert quantities_of(engine, caller, source) == (10, 5, 0, 0)
    assert quantities_of(engine, caller, canonical) == (10, 3, 0, 0)

    # 已有预留照常释放。
    path_id = to_release["reservation_id"]
    released = data_of(release(client, caller, path_id, {"reason": "cancelled"}), 200)
    assert released["status"] == "released"
    assert quantities_of(engine, caller, source) == (10, 3, 0, 0)
    assert quantities_of(engine, caller, canonical) == (10, 2, 0, 0)

    # 已有预留照常确认销售。
    response = commit(client, caller, to_commit["order_ref"], commit_body(to_commit), actor=ACTOR)
    assert data_of(response, 200)["status"] == "committed"
    assert quantities_of(engine, caller, source) == (7, 0, 0, 3)
    assert quantities_of(engine, caller, canonical) == (8, 0, 0, 2)

    # 故障补交照常新建（已付款的订单，§5.7）。
    body = fault_body([line("l1", source, 1), line("l2", canonical, 1)])
    fault = data_of(commit(client, caller, new_ref(), body, actor=ACTOR), 200)
    assert (fault["status"], fault["fault_mode"]) == ("committed", True)
    assert quantities_of(engine, caller, source) == (6, 0, 0, 4)
    assert quantities_of(engine, caller, canonical) == (7, 0, 0, 3)

    # 合并单仍停售，新预留照样拒绝。
    assert stored_merge(engine, merge_id)["status"] == "selling_paused"
    assert_paused(reserve(client, caller, order([line("l1", source, 1)])), [source])
