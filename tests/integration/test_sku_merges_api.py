"""SKU 合并的新建、录入实数、完成、取消与查询（契约 §2.4、§2.7、§2.8、§7.2、§10 第 3、4 条）。

在真 MySQL 上跑；每个测试建自己的租户、池与凭据，池默认先切到 live，
SKU 与余额行由测试直接在库里造（自己提交的连接）。
执行不在本任务里：要已执行的单时，直接改库把录入过实数的单置为 reconcile_pending，
执行那组列的两个迁移流水 ID 复用本租户已有的一条收货流水。
"""

import time
from collections.abc import Iterator
from datetime import datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, update

from app.config import Settings
from app.domain.requests import CATALOG_ADMIN, STOCK_ADMIN, STOCK_OPERATOR
from app.domain.service_auth import CATALOG_READ, CATALOG_WRITE, STOCK_READ, STOCK_WRITE
from app.main import create_app
from app.models.inventory import IdempotencyRecord, StockPool
from app.models.merges import SkuMerge, SkuMergeSnapshot
from app.services.stock import format_timestamp
from tests.integration.conftest import TEST_DATABASE_URL, new_id
from tests.integration.test_api_auth import Caller, add_other_pool, error_of, events, issue
from tests.integration.test_catalog_create_api import created
from tests.integration.test_catalog_queries_api import data_of
from tests.integration.test_counts_api import balance_row, live_caller, preset_balance, receive
from tests.integration.test_idempotency import add_skus, catalog_lock, get_lock, release_lock
from tests.integration.test_reservations_api import deactivate_product_of, deactivate_sku
from tests.integration.test_stock_imports_api import merge
from tests.integration.test_stock_service import entries, quantities_of

MERGES = "/v1/sku-merges"
ACTOR_REF = "admin-3"
EXECUTED_AT = datetime(2026, 11, 19, 9, 0)
QUANTITY_NAMES = ("stock_balance", "reserved", "unavailable", "paid_pending_dispatch")
MERGE_KEYS = {
    "merge_id",
    "source_sku_id",
    "canonical_sku_id",
    "status",
    "reason",
    "pool_id",
    "created_at",
    "count",
    "execution",
    "completed_at",
    "cancelled_at",
}
COUNT_KEYS = {"shelf_count", "unsellable_count", "counted_at", "snapshots", "d_shelf", "d_u"}
SNAPSHOT_KEYS = {
    "sku_id",
    "stock_balance",
    "reserved",
    "unavailable",
    "paid_pending_dispatch",
    "balance_version",
}
EXECUTION_KEYS = {
    "executed_at",
    "transfer_out_entry_id",
    "transfer_in_entry_id",
    "adjustment_entry_id",
    "repointed_line_count",
}


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
    """池已 live、带全部 scope（含 catalog.write 与 stock.write）的调用方。"""
    return live_caller(engine)


# --- 请求 ---


def new_key() -> str:
    return f"shop:merge:{uuid4().hex}"


def request_id() -> str:
    return f"merge-test-{uuid4().hex}"


def write_headers(
    caller: Caller, key: str | None = None, rid: str | None = None, role: str = STOCK_ADMIN
) -> dict:
    extra = {"Idempotency-Key": key or new_key(), "X-Request-ID": rid or request_id()}
    return caller.auth | {"X-Actor-Ref": ACTOR_REF, "X-Actor-Role": role} | extra


def creation(source: object, canonical: object, reason: object = None, **extra: object) -> dict:
    body = {"source_sku_id": source, "canonical_sku_id": canonical}
    if reason is not None:
        body["reason"] = reason
    return body | extra


def post_create(client: TestClient, caller: Caller, body: dict, **headers):
    return client.post(MERGES, json=body, headers=write_headers(caller, **headers))


def create(
    client: TestClient,
    caller: Caller,
    source: str,
    canonical: str,
    reason: str | None = None,
    **headers,
):
    return post_create(client, caller, creation(source, canonical, reason), **headers)


def counting(caller: Caller, shelf: object, unsellable: object = 0, pool_id: str | None = None):
    pool_id = pool_id or caller.pool_id
    return {"pool_id": pool_id, "shelf_count": shelf, "unsellable_count": unsellable}


def post_count(
    client: TestClient,
    caller: Caller,
    merge_id: str,
    body: dict,
    role: str = STOCK_OPERATOR,
    **headers,
):
    url = f"{MERGES}/{merge_id}/count"
    return client.post(url, json=body, headers=write_headers(caller, role=role, **headers))


def count(
    client: TestClient,
    caller: Caller,
    merge_id: str,
    shelf: int,
    unsellable: int = 0,
    pool_id: str | None = None,
    **headers,
):
    body = counting(caller, shelf, unsellable, pool_id)
    return post_count(client, caller, merge_id, body, **headers)


def complete(client: TestClient, caller: Caller, merge_id: str, **headers):
    url = f"{MERGES}/{merge_id}/complete"
    return client.post(url, headers=write_headers(caller, **headers))


def cancel(client: TestClient, caller: Caller, merge_id: str, **headers):
    url = f"{MERGES}/{merge_id}/cancel"
    return client.post(url, headers=write_headers(caller, **headers))


def get_merge(client: TestClient, caller: Caller, merge_id: str, rid: str | None = None):
    headers = caller.auth | {"X-Request-ID": rid or request_id()}
    return client.get(f"{MERGES}/{merge_id}", headers=headers)


def new_merge(client: TestClient, engine: Engine, caller: Caller) -> dict:
    source, canonical = add_skus(engine, caller.tenant_id, 2)
    return created(create(client, caller, source, canonical))


# --- 造数据与观察 ---


def stored_merge(engine: Engine, merge_id: str) -> dict:
    statement = select(*SkuMerge.__table__.columns).where(SkuMerge.merge_id == merge_id)
    with engine.connect() as db:
        return dict(db.execute(statement).mappings().one())


def merges_of(engine: Engine, caller: Caller) -> list[dict]:
    statement = select(*SkuMerge.__table__.columns).where(SkuMerge.tenant_id == caller.tenant_id)
    with engine.connect() as db:
        return [dict(row) for row in db.execute(statement).mappings()]


def stored_snapshots(engine: Engine, merge_id: str) -> list[dict]:
    statement = (
        select(*SkuMergeSnapshot.__table__.columns)
        .where(SkuMergeSnapshot.merge_id == merge_id)
        .order_by(SkuMergeSnapshot.sku_id)
    )
    with engine.connect() as db:
        return [dict(row) for row in db.execute(statement).mappings()]


def records(engine: Engine, caller: Caller) -> list[dict]:
    statement = select(*IdempotencyRecord.__table__.columns).where(
        IdempotencyRecord.tenant_id == caller.tenant_id
    )
    with engine.connect() as db:
        return [dict(row) for row in db.execute(statement).mappings()]


def set_merge(engine: Engine, merge_id: str, **values: object) -> None:
    with engine.begin() as db:
        db.execute(update(SkuMerge).where(SkuMerge.merge_id == merge_id).values(**values))


def mark_executed(client: TestClient, engine: Engine, caller: Caller, merge_id: str) -> str:
    """直接改库把录入过实数的单置为 reconcile_pending。

    两个迁移流水 ID 复用本租户已有的流水：先给规范 SKU 收 1 件货，取这条收货流水。
    """
    canonical = stored_merge(engine, merge_id)["canonical_sku_id"]
    receive(client, caller, canonical, 1)
    received = [entry for entry in entries(engine, caller) if entry["sku_id"] == canonical]
    entry_id = received[-1]["entry_id"]
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
    return entry_id


def mark_completed(engine: Engine, caller: Caller, merge_id: str) -> None:
    """直接改库把 reconcile_pending 的单置为 completed。"""
    set_merge(
        engine,
        merge_id,
        status="completed",
        completed_credential_id=caller.credential_id,
        completed_actor_ref=ACTOR_REF,
        completed_actor_role=STOCK_ADMIN,
        completed_request_id=request_id(),
        completed_at=EXECUTED_AT,
    )


def set_initializing(engine: Engine, caller: Caller) -> None:
    """把池置回 initializing（切 live 时记的四列一并清空）。"""
    with engine.begin() as db:
        statement = update(StockPool).where(StockPool.pool_id == caller.pool_id)
        db.execute(
            statement.values(
                status="initializing",
                went_live_at=None,
                went_live_credential_id=None,
                went_live_actor_ref=None,
                went_live_request_id=None,
            )
        )


def preset(engine: Engine, caller: Caller, sku_id: str, quantities: tuple, version: int) -> None:
    values = dict(zip(QUANTITY_NAMES, quantities, strict=True))
    preset_balance(engine, caller, sku_id, balance_version=version, **values)


def snapshot(sku_id: str, quantities: tuple, version: int) -> dict:
    stock_balance, reserved, unavailable, paid_pending_dispatch = quantities
    return {
        "sku_id": sku_id,
        "stock_balance": stock_balance,
        "reserved": reserved,
        "unavailable": unavailable,
        "paid_pending_dispatch": paid_pending_dispatch,
        "balance_version": version,
    }


def formula(snapshots: list[dict], shelf: int, unsellable: int) -> tuple[int, int]:
    """§2.4 第 2 步：(d_shelf, d_u)。"""
    d_u = unsellable - sum(item["unavailable"] for item in snapshots)
    on_shelf = sum(
        item["stock_balance"] - item["unavailable"] + item["paid_pending_dispatch"]
        for item in snapshots
    )
    return shelf - on_shelf, d_u


def assert_state_conflict(response, details: dict) -> None:
    error = error_of(response, 409)
    assert error["code"] == "STATE_CONFLICT"
    assert error["details"] == details


# --- 新建 ---


def test_creating_a_merge_returns_201_and_the_representation(client, engine, caller):
    source, canonical = add_skus(engine, caller.tenant_id, 2)
    key, rid = new_key(), request_id()
    data = created(create(client, caller, source, canonical, "  same tee  ", key=key, rid=rid))

    assert set(data) == MERGE_KEYS
    assert data["merge_id"].startswith("smg_")
    assert (data["source_sku_id"], data["canonical_sku_id"]) == (source, canonical)
    assert (data["status"], data["reason"], data["pool_id"]) == ("selling_paused", "same tee", None)
    for name in ("count", "execution", "completed_at", "cancelled_at"):
        assert data[name] is None

    row = stored_merge(engine, data["merge_id"])
    assert row["tenant_id"] == caller.tenant_id
    assert (row["status"], row["reason"], row["pool_id"]) == ("selling_paused", "same tee", None)
    assert row["credential_id"] == caller.credential_id
    assert (row["actor_ref"], row["actor_role"], row["request_id"]) == (ACTOR_REF, STOCK_ADMIN, rid)
    assert format_timestamp(row["created_at"]) == data["created_at"]
    # 新建不写流水、不建余额行。
    assert entries(engine, caller) == []
    assert balance_row(engine, caller, source) is None

    # 同键：原样重放。
    replay = create(client, caller, source, canonical, "  same tee  ", key=key)
    assert replay.status_code == 201
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json()["data"] == data
    assert data_of(get_merge(client, caller, data["merge_id"])) == data


def test_missing_and_foreign_skus_are_404(client, engine, caller):
    foreign = issue(engine)
    [foreign_sku] = add_skus(engine, foreign.tenant_id, 1)
    [known] = add_skus(engine, caller.tenant_id, 1)
    missing = new_id("sku")

    rid = request_id()
    error = error_of(create(client, caller, foreign_sku, missing, rid=rid), 404)
    assert error["code"] == "SKU_NOT_FOUND"
    # 按来源、规范的顺序列全。
    assert error["details"] == {"sku_ids": [foreign_sku, missing]}
    [event] = events(engine, rid)
    assert event["event_type"] == "cross_tenant"
    assert event["details"] == {"object_type": "sku", "id": foreign_sku}

    rid = request_id()
    error = error_of(create(client, caller, known, missing, rid=rid), 404)
    assert error["details"] == {"sku_ids": [missing]}
    assert events(engine, rid) == []
    error = error_of(create(client, caller, missing, foreign_sku), 404)
    assert error["details"] == {"sku_ids": [missing, foreign_sku]}
    assert merges_of(engine, caller) == []


def test_reporting_the_same_pair_again_returns_the_open_merge(client, engine, caller):
    source, canonical = add_skus(engine, caller.tenant_id, 2)
    data = created(create(client, caller, source, canonical, "first"))

    # 换键重报：200 与原单，reason 不同或没带都一样，不改它。
    for reason in ("second", None):
        again = create(client, caller, source, canonical, reason)
        assert "Idempotency-Replayed" not in again.headers
        assert data_of(again) == data

    # 录入实数之后重报，返回的是当前表示。
    counted = data_of(count(client, caller, data["merge_id"], 3))
    assert data_of(create(client, caller, source, canonical, "third")) == counted

    [row] = merges_of(engine, caller)
    assert (row["merge_id"], row["reason"]) == (data["merge_id"], "first")

    # 反过来的一对不是同一张单：两个 SKU 都在这张未结束的单里。
    error = error_of(create(client, caller, canonical, source), 409)
    assert error["code"] == "SKU_MERGE_IN_PROGRESS"
    assert len(merges_of(engine, caller)) == 1


def test_a_merged_source_or_canonical_is_409(client, engine, caller):
    source, canonical, target = add_skus(engine, caller.tenant_id, 3)
    merge(engine, source, target)
    error = error_of(create(client, caller, source, canonical), 409)
    assert error["code"] == "SKU_MERGED"
    assert error["details"] == {"sku_id": source, "merged_into_sku_id": target}

    fresh, merged_canonical, other_target = add_skus(engine, caller.tenant_id, 3)
    merge(engine, merged_canonical, other_target)
    error = error_of(create(client, caller, fresh, merged_canonical), 409)
    assert error["code"] == "SKU_MERGED"
    assert error["details"] == {"sku_id": merged_canonical, "merged_into_sku_id": other_target}

    # 两个都已合并：先查来源。
    error = error_of(create(client, caller, source, merged_canonical), 409)
    assert error["details"] == {"sku_id": source, "merged_into_sku_id": target}
    assert merges_of(engine, caller) == []


def test_an_inactive_canonical_is_409_and_an_inactive_source_is_not(client, engine, caller):
    source, canonical = add_skus(engine, caller.tenant_id, 2)
    deactivate_sku(engine, canonical)
    error = error_of(create(client, caller, source, canonical), 409)
    assert error["code"] == "SKU_INACTIVE"
    assert error["details"] == {"sku_ids": [canonical]}
    assert merges_of(engine, caller) == []

    # 来源停用不拦；规范 SKU 只看自己的状态，所属商品停用也不拦。
    inactive_source, active_canonical = add_skus(engine, caller.tenant_id, 2)
    deactivate_sku(engine, inactive_source)
    deactivate_product_of(engine, active_canonical)
    data = created(create(client, caller, inactive_source, active_canonical))
    assert data["status"] == "selling_paused"


def test_a_sku_in_another_open_merge_is_409(client, engine, caller):
    a, b, c, d, e = add_skus(engine, caller.tenant_id, 5)
    first = created(create(client, caller, a, b))
    second = created(create(client, caller, c, d))
    data_of(count(client, caller, second["merge_id"], 1))
    lowest = min(first, second, key=lambda item: item["merge_id"])

    def in_progress(item: dict) -> dict:
        return {
            "merge_id": item["merge_id"],
            "source_sku_id": item["source_sku_id"],
            "canonical_sku_id": item["canonical_sku_id"],
        }

    cases = [
        (e, a, first),  # 作别的单的来源
        (b, e, first),  # 作别的单的规范
        (e, d, second),  # counted 的单同样未结束
        (a, c, lowest),  # 两张都碰到：取 merge_id 最小的
    ]
    for source, canonical, expected in cases:
        error = error_of(create(client, caller, source, canonical), 409)
        assert error["code"] == "SKU_MERGE_IN_PROGRESS"
        assert error["details"] == in_progress(expected)

    # 执行之后（reconcile_pending）也没结束。
    mark_executed(client, engine, caller, second["merge_id"])
    error = error_of(create(client, caller, e, c), 409)
    assert error["details"] == in_progress(second)
    assert len(merges_of(engine, caller)) == 2


def test_after_cancelling_the_same_pair_can_be_created_again(client, engine, caller):
    source, canonical = add_skus(engine, caller.tenant_id, 2)
    first = created(create(client, caller, source, canonical))
    data_of(count(client, caller, first["merge_id"], 2))
    assert data_of(cancel(client, caller, first["merge_id"]))["status"] == "cancelled"

    second = created(create(client, caller, source, canonical))
    assert second["merge_id"] != first["merge_id"]
    assert (second["status"], second["count"]) == ("selling_paused", None)
    statuses = {row["merge_id"]: row["status"] for row in merges_of(engine, caller)}
    assert statuses == {first["merge_id"]: "cancelled", second["merge_id"]: "selling_paused"}


@pytest.mark.parametrize(
    "case",
    [
        "same_skus",
        "missing_canonical",
        "unknown_field",
        "number_id",
        "null_id",
        "blank_reason",
        "number_reason",
    ],
)
def test_a_bad_creation_body_is_422(client, engine, caller, case):
    source, canonical = add_skus(engine, caller.tenant_id, 2)
    body = {
        "same_skus": creation(source, source),
        "missing_canonical": {"source_sku_id": source},
        "unknown_field": creation(source, canonical, pool_id=caller.pool_id),
        "number_id": creation(source, 7),
        "null_id": {"source_sku_id": source, "canonical_sku_id": None},
        "blank_reason": creation(source, canonical, "   "),
        "number_reason": creation(source, canonical, 5),
    }[case]
    assert error_of(post_create(client, caller, body), 422)["code"] == "INVALID_REQUEST"
    assert merges_of(engine, caller) == []
    assert records(engine, caller) == []


def test_creating_takes_the_catalog_lock(client, engine, caller):
    source, canonical = add_skus(engine, caller.tenant_id, 2)
    key = new_key()
    name = catalog_lock(caller)
    with engine.connect() as holder:
        assert get_lock(holder, name) == 1
        try:
            started = time.monotonic()
            busy = create(client, caller, source, canonical, key=key)
            elapsed = time.monotonic() - started
        finally:
            release_lock(holder, name)

    error = error_of(busy, 503)
    assert error["code"] == "UNAVAILABLE"
    assert error["retryable"] is True
    # 等满目录写锁的 5 秒才放弃。
    assert elapsed >= 4.5
    assert merges_of(engine, caller) == []
    assert records(engine, caller) == []
    assert events(engine, busy.headers["X-Request-ID"]) == []

    # 什么也没保存：同键重试照常执行。
    response = create(client, caller, source, canonical, key=key)
    assert "Idempotency-Replayed" not in response.headers
    assert created(response)["status"] == "selling_paused"


# --- 录入实数 ---


def test_counting_records_the_snapshots(client, engine, caller):
    source, canonical = add_skus(engine, caller.tenant_id, 2)
    preset_balance(
        engine,
        caller,
        source,
        stock_balance=10,
        reserved=2,
        unavailable=3,
        paid_pending_dispatch=4,
        balance_version=7,
    )
    merge_id = created(create(client, caller, source, canonical))["merge_id"]
    assert balance_row(engine, caller, canonical) is None

    # stock_operator 可以录入。
    rid = request_id()
    data = data_of(count(client, caller, merge_id, 15, 2, role=STOCK_OPERATOR, rid=rid))

    assert set(data) == MERGE_KEYS
    assert (data["status"], data["pool_id"]) == ("counted", caller.pool_id)
    assert data["execution"] is None
    assert set(data["count"]) == COUNT_KEYS
    result = data["count"]
    assert (result["shelf_count"], result["unsellable_count"]) == (15, 2)
    # 快照等于录入时的四个量与版本，按 sku_id 升序；还没有余额行的 SKU 预建后记全零。
    expected = sorted(
        [snapshot(source, (10, 2, 3, 4), 7), snapshot(canonical, (0, 0, 0, 0), 0)],
        key=lambda item: item["sku_id"],
    )
    assert result["snapshots"] == expected
    assert all(set(item) == SNAPSHOT_KEYS for item in result["snapshots"])
    assert (result["d_shelf"], result["d_u"]) == formula(expected, 15, 2) == (4, -1)

    row = stored_merge(engine, merge_id)
    assert (row["status"], row["pool_id"]) == ("counted", caller.pool_id)
    assert (row["shelf_count"], row["unsellable_count"]) == (15, 2)
    assert row["counted_credential_id"] == caller.credential_id
    assert (row["counted_actor_ref"], row["counted_actor_role"]) == (ACTOR_REF, STOCK_OPERATOR)
    assert row["counted_request_id"] == rid
    assert format_timestamp(row["counted_at"]) == result["counted_at"]
    stored = stored_snapshots(engine, merge_id)
    assert len(stored) == 2
    assert {(item["tenant_id"], item["pool_id"]) for item in stored} == {
        (caller.tenant_id, caller.pool_id)
    }
    stored_values = [{name: item[name] for name in SNAPSHOT_KEYS} for item in stored]
    assert stored_values == result["snapshots"]

    # 不写流水，账面不变。
    assert entries(engine, caller) == []
    assert quantities_of(engine, caller, source) == (10, 2, 3, 4)
    assert quantities_of(engine, caller, canonical) == (0, 0, 0, 0)
    assert balance_row(engine, caller, canonical)["balance_version"] == 0
    assert data_of(get_merge(client, caller, merge_id)) == data


def test_recounting_replaces_the_whole_count(client, engine, caller):
    source, canonical = add_skus(engine, caller.tenant_id, 2)
    preset_balance(engine, caller, source, stock_balance=5, balance_version=1)
    preset_balance(engine, caller, canonical, stock_balance=8, balance_version=2)
    merge_id = created(create(client, caller, source, canonical))["merge_id"]
    first = data_of(count(client, caller, merge_id, 13, 0))
    assert first["count"]["d_shelf"] == 0

    # 收货改了规范 SKU 的账面之后重新点数：两份快照与实数整份替换。
    receive(client, caller, canonical, 4)
    rid = request_id()
    second = data_of(count(client, caller, merge_id, 16, 1, role=STOCK_ADMIN, rid=rid))
    assert second["status"] == "counted"
    expected = sorted(
        [snapshot(source, (5, 0, 0, 0), 1), snapshot(canonical, (12, 0, 0, 0), 3)],
        key=lambda item: item["sku_id"],
    )
    assert second["count"]["snapshots"] == expected
    assert (second["count"]["shelf_count"], second["count"]["unsellable_count"]) == (16, 1)
    assert (second["count"]["d_shelf"], second["count"]["d_u"]) == formula(expected, 16, 1)

    stored = stored_snapshots(engine, merge_id)
    assert len(stored) == 2
    stored_values = [{name: item[name] for name in SNAPSHOT_KEYS} for item in stored]
    assert stored_values == expected
    row = stored_merge(engine, merge_id)
    assert (row["shelf_count"], row["unsellable_count"]) == (16, 1)
    assert (row["counted_actor_role"], row["counted_request_id"]) == (STOCK_ADMIN, rid)
    assert data_of(get_merge(client, caller, merge_id)) == second


def test_counting_a_cancelled_or_executed_merge_is_409(client, engine, caller):
    cancelled = new_merge(client, engine, caller)["merge_id"]
    data_of(cancel(client, caller, cancelled))
    key = new_key()
    response = count(client, caller, cancelled, 1, key=key)
    assert_state_conflict(response, {"status": "cancelled"})
    assert stored_snapshots(engine, cancelled) == []
    # 按数据库状态判出，作为确定结果保存。
    replay = count(client, caller, cancelled, 1, key=key)
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == response.json()

    executed = new_merge(client, engine, caller)["merge_id"]
    data_of(count(client, caller, executed, 3))
    mark_executed(client, engine, caller, executed)
    before = stored_snapshots(engine, executed)
    assert_state_conflict(count(client, caller, executed, 1), {"status": "reconcile_pending"})
    mark_completed(engine, caller, executed)
    assert_state_conflict(count(client, caller, executed, 1), {"status": "completed"})
    assert stored_snapshots(engine, executed) == before
    assert stored_merge(engine, executed)["shelf_count"] == 3


def test_counting_on_a_different_pool_is_409(client, engine, caller):
    merge_id = new_merge(client, engine, caller)["merge_id"]
    data_of(count(client, caller, merge_id, 2))
    # V1 每个租户只有一个池，正常碰不到：直接改库把合并单的池换成本租户的另一个池。
    other_pool = add_other_pool(engine, caller.tenant_id)
    set_merge(engine, merge_id, pool_id=other_pool)
    before = stored_snapshots(engine, merge_id)

    response = count(client, caller, merge_id, 5)
    assert_state_conflict(response, {"status": "counted", "reason": "pool_mismatch"})
    assert stored_snapshots(engine, merge_id) == before
    assert stored_merge(engine, merge_id)["shelf_count"] == 2


def test_counting_a_missing_or_foreign_merge_is_404(client, engine, caller):
    foreign = live_caller(engine)
    foreign_id = new_merge(client, engine, foreign)["merge_id"]
    missing_id = new_id("smg")

    missing_rid, foreign_rid = request_id(), request_id()
    missing = error_of(count(client, caller, missing_id, 1, rid=missing_rid), 404)
    key = new_key()
    response = count(client, caller, foreign_id, 1, rid=foreign_rid, key=key)
    assert error_of(response, 404) == missing
    assert missing["code"] == "SKU_MERGE_NOT_FOUND"
    assert events(engine, missing_rid) == []
    [event] = events(engine, foreign_rid)
    assert event["event_type"] == "cross_tenant"
    assert event["details"] == {"object_type": "sku_merge", "id": foreign_id}
    # 事务里判出的 404 作为确定结果保存。
    replay = count(client, caller, foreign_id, 1, key=key)
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == response.json()

    row = stored_merge(engine, foreign_id)
    assert (row["status"], row["pool_id"]) == ("selling_paused", None)
    assert stored_snapshots(engine, foreign_id) == []


def test_counting_on_a_pool_that_is_not_live_is_409(client, engine, caller):
    merge_id = new_merge(client, engine, caller)["merge_id"]
    set_initializing(engine, caller)
    error = error_of(count(client, caller, merge_id, 1), 409)
    assert error["code"] == "POOL_NOT_LIVE"
    assert stored_merge(engine, merge_id)["status"] == "selling_paused"
    assert stored_snapshots(engine, merge_id) == []


def test_counting_authorizes_the_pool_in_the_body(client, engine, caller):
    merge_id = new_merge(client, engine, caller)["merge_id"]

    # 凭据没有销售入口：403。
    no_channel = issue(engine, channel_status=None)
    response = count(client, no_channel, merge_id, 1)
    assert error_of(response, 403)["code"] == "FORBIDDEN"

    # 正文的 pool_id 属于别的租户：404，记 cross_tenant。
    foreign = live_caller(engine)
    rid = request_id()
    error = error_of(count(client, caller, merge_id, 1, pool_id=foreign.pool_id, rid=rid), 404)
    assert error["code"] == "POOL_NOT_FOUND"
    [event] = events(engine, rid)
    assert event["details"] == {"object_type": "pool", "id": foreign.pool_id}

    assert stored_merge(engine, merge_id)["status"] == "selling_paused"
    assert stored_snapshots(engine, merge_id) == []
    # 池授权的拒绝在取幂等锁之前，不保存。
    operations = [record["operation"] for record in records(engine, caller)]
    assert operations == ["sku_merges.create"]


@pytest.mark.parametrize(
    "case",
    ["negative", "too_many", "string", "bool", "missing", "unknown_field", "number_pool"],
)
def test_a_bad_count_body_is_422(client, engine, caller, case):
    merge_id = new_merge(client, engine, caller)["merge_id"]
    body = {
        "negative": counting(caller, -1),
        "too_many": counting(caller, 1_000_001),
        "string": counting(caller, "5"),
        "bool": counting(caller, 1, True),
        "missing": {"pool_id": caller.pool_id, "shelf_count": 1},
        "unknown_field": counting(caller, 1) | {"note": "x"},
        "number_pool": {"pool_id": 1, "shelf_count": 1, "unsellable_count": 0},
    }[case]
    response = post_count(client, caller, merge_id, body)
    assert error_of(response, 422)["code"] == "INVALID_REQUEST"
    assert stored_merge(engine, merge_id)["status"] == "selling_paused"


def test_zero_and_a_million_are_accepted(client, engine, caller):
    merge_id = new_merge(client, engine, caller)["merge_id"]
    data = data_of(count(client, caller, merge_id, 1_000_000, 0))
    assert (data["count"]["shelf_count"], data["count"]["unsellable_count"]) == (1_000_000, 0)


# --- 完成与取消 ---


def test_completing_only_works_on_reconcile_pending(client, engine, caller):
    merge_id = new_merge(client, engine, caller)["merge_id"]
    assert_state_conflict(complete(client, caller, merge_id), {"status": "selling_paused"})
    data_of(count(client, caller, merge_id, 4))
    assert_state_conflict(complete(client, caller, merge_id), {"status": "counted"})
    entry_id = mark_executed(client, engine, caller, merge_id)

    rid = request_id()
    data = data_of(complete(client, caller, merge_id, rid=rid))
    assert set(data) == MERGE_KEYS
    assert data["status"] == "completed"
    assert data["cancelled_at"] is None
    assert data["count"]["shelf_count"] == 4
    assert set(data["execution"]) == EXECUTION_KEYS
    assert data["execution"] == {
        "executed_at": format_timestamp(EXECUTED_AT),
        "transfer_out_entry_id": entry_id,
        "transfer_in_entry_id": entry_id,
        "adjustment_entry_id": None,
        "repointed_line_count": 0,
    }
    row = stored_merge(engine, merge_id)
    assert row["status"] == "completed"
    assert row["completed_credential_id"] == caller.credential_id
    assert (row["completed_actor_ref"], row["completed_actor_role"]) == (ACTOR_REF, STOCK_ADMIN)
    assert row["completed_request_id"] == rid
    assert format_timestamp(row["completed_at"]) == data["completed_at"]

    # 已完成的再完成：200 与当前表示，不改记录。
    again = complete(client, caller, merge_id)
    assert "Idempotency-Replayed" not in again.headers
    assert data_of(again) == data
    assert stored_merge(engine, merge_id)["completed_request_id"] == rid
    assert data_of(get_merge(client, caller, merge_id)) == data

    # 已取消的单不能完成。
    cancelled = new_merge(client, engine, caller)["merge_id"]
    data_of(cancel(client, caller, cancelled))
    assert_state_conflict(complete(client, caller, cancelled), {"status": "cancelled"})
    assert stored_merge(engine, cancelled)["completed_at"] is None


@pytest.mark.parametrize("counted", [False, True])
def test_cancelling_works_before_execution(client, engine, caller, counted):
    merge_id = new_merge(client, engine, caller)["merge_id"]
    if counted:
        data_of(count(client, caller, merge_id, 6, 1))
    before = stored_snapshots(engine, merge_id)

    rid = request_id()
    data = data_of(cancel(client, caller, merge_id, rid=rid))
    assert set(data) == MERGE_KEYS
    assert (data["status"], data["completed_at"], data["execution"]) == ("cancelled", None, None)
    row = stored_merge(engine, merge_id)
    assert row["status"] == "cancelled"
    assert row["cancelled_credential_id"] == caller.credential_id
    assert (row["cancelled_actor_ref"], row["cancelled_actor_role"]) == (ACTOR_REF, STOCK_ADMIN)
    assert row["cancelled_request_id"] == rid
    assert format_timestamp(row["cancelled_at"]) == data["cancelled_at"]
    # 已录入的实数与快照留在合并单上备查。
    assert stored_snapshots(engine, merge_id) == before
    if counted:
        assert data["pool_id"] == caller.pool_id
        assert (data["count"]["shelf_count"], data["count"]["unsellable_count"]) == (6, 1)
    else:
        assert (data["pool_id"], data["count"]) == (None, None)
    assert entries(engine, caller) == []

    # 已取消的再取消：200 与当前表示。
    again = cancel(client, caller, merge_id)
    assert "Idempotency-Replayed" not in again.headers
    assert data_of(again) == data
    assert stored_merge(engine, merge_id)["cancelled_request_id"] == rid
    assert data_of(get_merge(client, caller, merge_id)) == data


def test_cancelling_an_executed_merge_is_409(client, engine, caller):
    merge_id = new_merge(client, engine, caller)["merge_id"]
    data_of(count(client, caller, merge_id, 2))
    mark_executed(client, engine, caller, merge_id)
    assert_state_conflict(cancel(client, caller, merge_id), {"status": "reconcile_pending"})
    mark_completed(engine, caller, merge_id)
    assert_state_conflict(cancel(client, caller, merge_id), {"status": "completed"})
    assert stored_merge(engine, merge_id)["cancelled_at"] is None


def test_complete_and_cancel_take_no_body(client, engine, caller):
    merge_id = new_merge(client, engine, caller)["merge_id"]
    for action in ("complete", "cancel"):
        url = f"{MERGES}/{merge_id}/{action}"
        response = client.post(url, json={"reason": "x"}, headers=write_headers(caller))
        assert error_of(response, 422)["code"] == "INVALID_REQUEST"
    assert stored_merge(engine, merge_id)["status"] == "selling_paused"


def test_complete_and_cancel_of_a_missing_or_foreign_merge_are_404(client, engine, caller):
    foreign = live_caller(engine)
    foreign_id = new_merge(client, engine, foreign)["merge_id"]
    missing_id = new_id("smg")
    for send in (complete, cancel):
        missing_rid, foreign_rid = request_id(), request_id()
        missing = error_of(send(client, caller, missing_id, rid=missing_rid), 404)
        key = new_key()
        response = send(client, caller, foreign_id, rid=foreign_rid, key=key)
        assert error_of(response, 404) == missing
        assert missing["code"] == "SKU_MERGE_NOT_FOUND"
        assert events(engine, missing_rid) == []
        [event] = events(engine, foreign_rid)
        assert event["event_type"] == "cross_tenant"
        assert event["details"] == {"object_type": "sku_merge", "id": foreign_id}
        # 在事务里判出、作为确定结果保存。
        replay = send(client, caller, foreign_id, key=key)
        assert replay.headers["Idempotency-Replayed"] == "true"
        assert replay.json() == response.json()
    assert stored_merge(engine, foreign_id)["status"] == "selling_paused"


# --- 查询与表示 ---


@pytest.mark.parametrize(
    ("source_quantities", "canonical_quantities", "shelf", "unsellable", "deltas"),
    [
        # §2.4 合并定数的例子 1–4（盘点时的快照与录入的实数）。
        ((5, 0, 0, 2), (10, 0, 0, 0), 17, 0, (0, 0)),
        ((5, 2, 0, 0), (10, 0, 0, 0), 14, 0, (-1, 0)),
        ((5, 0, 1, 0), (10, 0, 0, 0), 14, 1, (0, 0)),
        ((2, 2, 0, 0), (1, 0, 0, 0), 0, 0, (-3, 0)),
    ],
)
def test_the_count_carries_the_deltas_of_the_formula(
    client, engine, caller, source_quantities, canonical_quantities, shelf, unsellable, deltas
):
    source, canonical = add_skus(engine, caller.tenant_id, 2)
    preset(engine, caller, source, source_quantities, 3)
    preset(engine, caller, canonical, canonical_quantities, 5)
    merge_id = created(create(client, caller, source, canonical))["merge_id"]

    before = data_of(get_merge(client, caller, merge_id))
    assert set(before) == MERGE_KEYS
    assert (before["count"], before["execution"], before["pool_id"]) == (None, None, None)

    data_of(count(client, caller, merge_id, shelf, unsellable))
    after = data_of(get_merge(client, caller, merge_id))
    assert after["execution"] is None
    expected = sorted(
        [snapshot(source, source_quantities, 3), snapshot(canonical, canonical_quantities, 5)],
        key=lambda item: item["sku_id"],
    )
    result = after["count"]
    assert result["snapshots"] == expected
    assert (result["d_shelf"], result["d_u"]) == formula(expected, shelf, unsellable) == deltas


def test_a_foreign_merge_looks_exactly_like_a_missing_one(client, engine, caller):
    foreign = live_caller(engine)
    foreign_id = new_merge(client, engine, foreign)["merge_id"]
    missing_id = new_id("smg")

    missing_rid, foreign_rid = request_id(), request_id()
    missing = error_of(get_merge(client, caller, missing_id, missing_rid), 404)
    found = error_of(get_merge(client, caller, foreign_id, foreign_rid), 404)
    assert missing == found
    assert missing["code"] == "SKU_MERGE_NOT_FOUND"
    assert events(engine, missing_rid) == []
    [event] = events(engine, foreign_rid)
    assert event["event_type"] == "cross_tenant"
    assert event["details"] == {"object_type": "sku_merge", "id": foreign_id}


# --- 鉴权 ---


@pytest.mark.parametrize(
    "scopes",
    [
        [CATALOG_WRITE],
        [STOCK_WRITE],
        [CATALOG_READ, CATALOG_WRITE, STOCK_READ],
        [CATALOG_READ, STOCK_READ, STOCK_WRITE],
    ],
)
def test_every_endpoint_needs_both_write_scopes(client, engine, caller, scopes):
    merge_id = new_merge(client, engine, caller)["merge_id"]
    source, canonical = add_skus(engine, caller.tenant_id, 2)
    # 另一个租户的凭据：scope 在鉴权之后、一切业务判定之前就拒绝。
    narrow = live_caller(engine, scopes=scopes)
    missing_scope = [STOCK_WRITE] if CATALOG_WRITE in scopes else [CATALOG_WRITE]
    sends = [
        lambda rid: create(client, narrow, source, canonical, rid=rid),
        lambda rid: count(client, narrow, merge_id, 1, rid=rid),
        lambda rid: complete(client, narrow, merge_id, rid=rid),
        lambda rid: cancel(client, narrow, merge_id, rid=rid),
        lambda rid: get_merge(client, narrow, merge_id, rid),
    ]
    for send in sends:
        rid = request_id()
        assert error_of(send(rid), 403)["code"] == "FORBIDDEN"
        [event] = events(engine, rid)
        assert event["details"] == {"category": "scope", "missing_scopes": missing_scope}
    assert stored_merge(engine, merge_id)["status"] == "selling_paused"
    assert len(merges_of(engine, caller)) == 1


def test_create_complete_and_cancel_need_stock_admin(client, engine, caller):
    source, canonical = add_skus(engine, caller.tenant_id, 2)
    merge_id = new_merge(client, engine, caller)["merge_id"]
    for role in (STOCK_OPERATOR, CATALOG_ADMIN):
        sends = [
            lambda rid, role=role: create(client, caller, source, canonical, rid=rid, role=role),
            lambda rid, role=role: complete(client, caller, merge_id, rid=rid, role=role),
            lambda rid, role=role: cancel(client, caller, merge_id, rid=rid, role=role),
        ]
        for send in sends:
            rid = request_id()
            assert error_of(send(rid), 403)["code"] == "FORBIDDEN"
            [event] = events(engine, rid)
            assert event["details"] == {"category": "role", "role": role}

    # 录入实数不接受 catalog_admin。
    rid = request_id()
    response = count(client, caller, merge_id, 1, role=CATALOG_ADMIN, rid=rid)
    assert error_of(response, 403)["code"] == "FORBIDDEN"
    [event] = events(engine, rid)
    assert event["details"] == {"category": "role", "role": CATALOG_ADMIN}

    # 缺操作者头 400。
    headers = write_headers(caller)
    del headers["X-Actor-Ref"], headers["X-Actor-Role"]
    response = client.post(MERGES, json=creation(source, canonical), headers=headers)
    assert error_of(response, 400)["code"] == "INVALID_REQUEST"
    response = client.post(f"{MERGES}/{merge_id}/cancel", headers=headers)
    assert error_of(response, 400)["code"] == "INVALID_REQUEST"

    assert stored_merge(engine, merge_id)["status"] == "selling_paused"
    assert len(merges_of(engine, caller)) == 1
