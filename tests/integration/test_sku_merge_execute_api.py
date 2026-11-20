"""SKU 合并的执行（契约 §2.4、§2.5、§2.7、§2.8 执行、§7.2、§10 第 3、4 条）。

在真 MySQL 上跑；每个测试建自己的租户、池、销售入口与凭据，池先切到 live。
起始的余额行、预留的待审与结案状态由测试直接在库里造（自己提交的连接）；
预留、确认销售、释放、发货、退款回库、退货验收、不可售调整、盘点、收货与更正经接口。
名单读出之后的并发释放替换服务层的 read_repoint_candidates；
锁内复核池替换接口模块引用的 run_idempotent。复核重做见 test_sku_merge_execute_concurrency.py。
"""

import logging
from collections.abc import Iterator
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from app.api import sku_merges as sku_merge_api
from app.domain.requests import STOCK_ADMIN, STOCK_OPERATOR
from app.domain.service_auth import ORDERS_READ, generate_token, token_digest
from app.models.catalog import Sku
from app.models.inventory import IdempotencyRecord
from app.models.orders import OrderLine, Reservation
from app.models.service_auth import Credential
from app.services import sku_merges as sku_merge_service
from app.services.stock import format_timestamp
from tests.integration.conftest import (
    DISPATCHES,
    REFUND_RESTOCKS,
    add_credential,
    body_of,
    new_id,
    post,
    qline,
)
from tests.integration.test_api_auth import ALL_SCOPES, Caller, add_other_pool, error_of, events
from tests.integration.test_catalog_changes_api import pull
from tests.integration.test_catalog_create_api import created, rows
from tests.integration.test_corrections_api import adjust, correct, not_correctable
from tests.integration.test_corrections_api import receive as receive_entry
from tests.integration.test_count_confirm_api import confirm
from tests.integration.test_counts_api import balance_row, counted, live_caller, new_count, submit
from tests.integration.test_idempotency import add_skus, rid_of
from tests.integration.test_reservation_release_api import release
from tests.integration.test_reservations_api import (
    ACTOR,
    data_of,
    deactivate_sku,
    line,
    make_client,
    order,
    reserve,
    rows_of,
    stock,
)
from tests.integration.test_return_receipts_api import receive as receive_return
from tests.integration.test_return_receipts_api import return_body, rline
from tests.integration.test_sale_commit_late_api import expire
from tests.integration.test_sale_commits_api import commit, commit_body, paid_columns, set_status
from tests.integration.test_service_auth_schema import token_insert
from tests.integration.test_sku_merges_api import (
    ACTOR_REF,
    EXECUTED_AT,
    EXECUTION_KEYS,
    MERGE_KEYS,
    MERGES,
    assert_state_conflict,
    cancel,
    complete,
    count,
    create,
    get_merge,
    new_key,
    preset,
    request_id,
    set_merge,
    stored_merge,
    write_headers,
)
from tests.integration.test_stock_service import entries, quantities_of

SKU_MERGES_EXECUTE = "sku_merges.execute"
TRANSFER_OUT = "MERGE_TRANSFER_OUT"
TRANSFER_IN = "MERGE_TRANSFER_IN"
ADJUSTMENT = "COUNT_ADJUSTMENT"
ZERO = (0, 0, 0, 0)
QUANTITY_NAMES = ("stock_balance", "reserved", "unavailable", "paid_pending_dispatch")


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


# --- 请求 ---


def execute(client: TestClient, caller: Caller, merge_id: str, role: str = STOCK_ADMIN, **headers):
    url = f"{MERGES}/{merge_id}/execute"
    return client.post(url, headers=write_headers(caller, role=role, **headers))


def counted_merge(
    client: TestClient,
    caller: Caller,
    source: str,
    canonical: str,
    shelf: int,
    unsellable: int = 0,
    reason: str | None = None,
) -> str:
    """建单并录入实数，返回 merge_id。"""
    merge_id = created(create(client, caller, source, canonical, reason))["merge_id"]
    data_of(count(client, caller, merge_id, shelf, unsellable), 200)
    return merge_id


def reserve_on(client: TestClient, caller: Caller, sku_id: str, quantity: int) -> dict:
    """一行的订单（line_ref 为 l1），返回预留的表示。"""
    body = order([line("l1", sku_id, quantity)])
    return data_of(reserve(client, caller, body, actor=ACTOR), 201)


def commit_order(client: TestClient, caller: Caller, reserved: dict) -> None:
    data_of(commit(client, caller, reserved["order_ref"], commit_body(reserved)), 200)


def refund_restock(client: TestClient, caller: Caller, reserved: dict, quantity: int) -> None:
    body = body_of(REFUND_RESTOCKS, [qline("l1", quantity)])
    data_of(post(client, caller, REFUND_RESTOCKS, reserved["order_ref"], body), 201)


def dispatch(client: TestClient, caller: Caller, reserved: dict, quantity: int) -> None:
    body = body_of(DISPATCHES, [qline("l1", quantity)])
    data_of(post(client, caller, DISPATCHES, reserved["order_ref"], body), 201)


# --- 观察 ---


def merge_entries(engine: Engine, caller: Caller, merge_id: str) -> dict[str, dict]:
    """执行写的流水，按流水类型；每种至多一条。"""
    written = [entry for entry in entries(engine, caller) if entry["source_ref"] == merge_id]
    by_type = {entry["entry_type"]: entry for entry in written}
    assert len(by_type) == len(written)
    return by_type


def execute_records(engine: Engine, caller: Caller) -> list[dict]:
    stored = rows(engine, IdempotencyRecord, caller.tenant_id)
    return [record for record in stored if record["operation"] == SKU_MERGES_EXECUTE]


def sku_row(engine: Engine, sku_id: str) -> dict:
    statement = select(*Sku.__table__.columns).where(Sku.sku_id == sku_id)
    with engine.connect() as db:
        return dict(db.execute(statement).mappings().one())


def channel_of(engine: Engine, caller: Caller) -> str:
    statement = select(Credential.channel_id).where(
        Credential.credential_id == caller.credential_id
    )
    with engine.connect() as db:
        return db.execute(statement).scalar_one()


def line_skus(engine: Engine, caller: Caller) -> dict[tuple[str, str], str]:
    """按 (reservation_id, line_ref) 的订单行 sku_id。"""
    lines = rows_of(engine, OrderLine, caller)
    return {(row["reservation_id"], row["line_ref"]): row["sku_id"] for row in lines}


def changed_at(engine: Engine, caller: Caller) -> dict[str, datetime]:
    reservations = rows_of(engine, Reservation, caller)
    return {row["reservation_id"]: row["last_changed_at"] for row in reservations}


def deltas_of(entry: dict) -> tuple:
    return tuple(entry[f"{name}_delta"] for name in QUANTITY_NAMES)


def same_tenant_caller(engine: Engine, caller: Caller, **credential: object) -> Caller:
    """同一租户的另一个凭据，带全部 scope；credential 覆盖凭据的列（例如不带销售入口）。"""
    plain = generate_token()
    with engine.begin() as db:
        credential_id = add_credential(db, caller.tenant_id, scopes=ALL_SCOPES, **credential)
        db.execute(token_insert(caller.tenant_id, credential_id, token_digest=token_digest(plain)))
    return Caller(caller.tenant_id, caller.pool_id, credential_id, plain)


def count_on_another_pool(engine: Engine, caller: Caller, merge_id: str, pool_id: str) -> None:
    """直接改库：合并单按同租户的另一个池录入了实数（V1 每个租户只有一个池，接口造不出）。"""
    set_merge(
        engine,
        merge_id,
        status="counted",
        pool_id=pool_id,
        shelf_count=1,
        unsellable_count=0,
        counted_credential_id=caller.credential_id,
        counted_actor_ref=ACTOR_REF,
        counted_actor_role=STOCK_OPERATOR,
        counted_request_id=request_id(),
        counted_at=EXECUTED_AT,
    )


def assert_settled(
    engine: Engine,
    caller: Caller,
    data: dict,
    canonical_after: tuple,
    deltas: tuple[int, int],
) -> dict[str, dict]:
    """执行成功：规范 SKU 为 canonical_after、来源归零，表示里的三条流水 ID 与库里的一致；
    deltas 为 (d_shelf, d_u)，都为 0 时不写定数流水。返回执行写的流水。
    """
    merge_id = data["merge_id"]
    source, canonical = data["source_sku_id"], data["canonical_sku_id"]
    assert data["status"] == "reconcile_pending"
    assert (data["count"]["d_shelf"], data["count"]["d_u"]) == deltas
    assert quantities_of(engine, caller, canonical) == canonical_after
    assert quantities_of(engine, caller, source) == ZERO
    written = merge_entries(engine, caller, merge_id)
    execution = data["execution"]
    assert set(execution) == EXECUTION_KEYS
    assert execution["transfer_out_entry_id"] == written[TRANSFER_OUT]["entry_id"]
    assert execution["transfer_in_entry_id"] == written[TRANSFER_IN]["entry_id"]
    assert written[TRANSFER_OUT]["sku_id"] == source
    assert written[TRANSFER_IN]["sku_id"] == canonical
    d_shelf, d_u = deltas
    if deltas == (0, 0):
        assert set(written) == {TRANSFER_OUT, TRANSFER_IN}
        assert execution["adjustment_entry_id"] is None
    else:
        adjustment = written[ADJUSTMENT]
        assert adjustment["sku_id"] == canonical
        assert deltas_of(adjustment) == (d_shelf + d_u, 0, d_u, 0)
        assert execution["adjustment_entry_id"] == adjustment["entry_id"]
    # 执行后两个余额行的判定列都等于各自当时的 balance_version（§2.4 第 1、2 步）。
    for sku_id in (source, canonical):
        row = balance_row(engine, caller, sku_id)
        version = row["balance_version"]
        assert row["last_book_only_version"] == row["last_reconciled_version"] == version
    assert stored_merge(engine, merge_id)["status"] == "reconcile_pending"
    return written


def assert_rejected_and_kept(
    engine: Engine, caller: Caller, merge_id: str, before: tuple, response, code: str
) -> dict:
    """被拒：合并单仍为 counted，流水、两个 SKU 的账面与来源 SKU 行都没变；返回错误。"""
    error = error_of(response, 409)
    assert error["code"] == code
    assert snapshot_of(engine, caller, merge_id) == before
    assert stored_merge(engine, merge_id)["status"] == "counted"
    return error


def snapshot_of(engine: Engine, caller: Caller, merge_id: str) -> tuple:
    merge = stored_merge(engine, merge_id)
    source, canonical = merge["source_sku_id"], merge["canonical_sku_id"]
    return (
        entries(engine, caller),
        quantities_of(engine, caller, source),
        quantities_of(engine, caller, canonical),
        sku_row(engine, source),
        line_skus(engine, caller),
    )


# --- §2.4 合并定数的例子 ---


def test_example_1_a_refund_restock_after_the_count_is_kept(client, engine, caller):
    source, canonical = add_skus(engine, caller.tenant_id, 2)
    stock(engine, caller, source, 7)
    stock(engine, caller, canonical, 10)
    paid = reserve_on(client, caller, source, 2)
    commit_order(client, caller, paid)
    assert quantities_of(engine, caller, source) == (5, 0, 0, 2)
    merge_id = counted_merge(client, caller, source, canonical, 17, 0)

    # 盘点之后、执行之前：1 件待发货退款回库。
    refund_restock(client, caller, paid, 1)
    assert quantities_of(engine, caller, source) == (6, 0, 0, 1)

    data = data_of(execute(client, caller, merge_id), 200)
    assert_settled(engine, caller, data, (16, 0, 0, 1), (0, 0))


def test_example_2_a_commit_after_the_count_is_kept_and_the_missing_unit_written_off(
    client, engine, caller
):
    source, canonical = add_skus(engine, caller.tenant_id, 2)
    stock(engine, caller, source, 5)
    stock(engine, caller, canonical, 10)
    reserved = reserve_on(client, caller, source, 2)
    assert quantities_of(engine, caller, source) == (5, 2, 0, 0)
    merge_id = counted_merge(client, caller, source, canonical, 14, 0)

    # 盘点之后、执行之前：预留付款确认。
    commit_order(client, caller, reserved)
    assert quantities_of(engine, caller, source) == (3, 0, 0, 2)

    data = data_of(execute(client, caller, merge_id), 200)
    assert_settled(engine, caller, data, (12, 0, 0, 2), (-1, 0))


def test_example_3_a_unit_marked_unavailable_after_the_count_is_kept(client, engine, caller):
    source, canonical = add_skus(engine, caller.tenant_id, 2)
    preset(engine, caller, source, (5, 0, 1, 0), 0)
    stock(engine, caller, canonical, 10)
    merge_id = counted_merge(client, caller, source, canonical, 14, 1)

    # 盘点之后、执行之前：又发现 1 件损坏，标不可售（不是只改账的流水）。
    adjust(client, caller, "mark_unavailable", source, 1)
    assert quantities_of(engine, caller, source) == (5, 0, 2, 0)

    data = data_of(execute(client, caller, merge_id), 200)
    assert_settled(engine, caller, data, (15, 0, 2, 0), (0, 0))


def test_example_4_a_count_below_the_reservations_is_rejected(client, engine, caller):
    source, canonical = add_skus(engine, caller.tenant_id, 2)
    stock(engine, caller, source, 2)
    stock(engine, caller, canonical, 1)
    reserve_on(client, caller, source, 2)
    assert quantities_of(engine, caller, source) == (2, 2, 0, 0)
    merge_id = counted_merge(client, caller, source, canonical, 0, 0)
    before = snapshot_of(engine, caller, merge_id)

    key = new_key()
    response = execute(client, caller, merge_id, key=key)
    error = assert_rejected_and_kept(
        engine, caller, merge_id, before, response, "COUNT_BELOW_COMMITMENTS"
    )
    # 迁移后 3/2/0/0，差额 −3 落账后 0/2/0/0：缺口 2，只有规范 SKU 一项。
    assert error["details"] == {
        "skus": [
            {
                "sku_id": canonical,
                "stock_balance": 0,
                "reserved": 2,
                "unavailable": 0,
                "paid_pending_dispatch": 0,
                "shortfall": 2,
            }
        ]
    }
    assert quantities_of(engine, caller, source) == (2, 2, 0, 0)
    assert quantities_of(engine, caller, canonical) == (1, 0, 0, 0)
    # 按数据库状态判出，作为确定结果保存。
    replay = execute(client, caller, merge_id, key=key)
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == response.json()


def test_example_5_a_book_only_entry_after_the_count_is_stale_until_recounted(
    client, engine, caller
):
    source, canonical = add_skus(engine, caller.tenant_id, 2)
    stock(engine, caller, source, 5)
    stock(engine, caller, canonical, 10)
    merge_id = counted_merge(client, caller, source, canonical, 13, 0)

    # 另一张 §6 盘点单只点来源 SKU，确认记 −2（只改账的流水）。
    count_id = new_count(client, caller)["count_id"]
    data_of(submit(client, caller, count_id, [counted(source, 3)]), 200)
    data_of(confirm(client, caller, count_id), 200)
    assert quantities_of(engine, caller, source) == (3, 0, 0, 0)
    before = snapshot_of(engine, caller, merge_id)

    response = execute(client, caller, merge_id)
    error = assert_rejected_and_kept(engine, caller, merge_id, before, response, "COUNT_STALE")
    assert error["details"] == {"sku_ids": [source]}

    # 重新点数、重新录入 13 / 0（新快照合计 13）后，用新的幂等键直接再执行。
    data_of(count(client, caller, merge_id, 13, 0), 200)
    data = data_of(execute(client, caller, merge_id), 200)
    assert_settled(engine, caller, data, (13, 0, 0, 0), (0, 0))


def test_example_6_after_handling_the_orders_the_original_count_executes(client, engine, caller):
    source, canonical = add_skus(engine, caller.tenant_id, 2)
    stock(engine, caller, source, 5)
    stock(engine, caller, canonical, 1)
    paid = reserve_on(client, caller, source, 2)
    commit_order(client, caller, paid)
    held = reserve_on(client, caller, source, 3)
    assert quantities_of(engine, caller, source) == (3, 3, 0, 2)
    merge_id = counted_merge(client, caller, source, canonical, 4, 0)
    before = snapshot_of(engine, caller, merge_id)

    # 第一次执行：迁移后 4/3/0/2，差额 −2 得 2/3/0/2，缺口 1。
    response = execute(client, caller, merge_id)
    error = assert_rejected_and_kept(
        engine, caller, merge_id, before, response, "COUNT_BELOW_COMMITMENTS"
    )
    assert error["details"] == {
        "skus": [
            {
                "sku_id": canonical,
                "stock_balance": 2,
                "reserved": 3,
                "unavailable": 0,
                "paid_pending_dispatch": 2,
                "shortfall": 1,
            }
        ]
    }

    # 释放预留、发出 2 件已付款的货；不重新录入，用新的幂等键直接再执行。
    data_of(release(client, caller, held["reservation_id"], {"reason": "cancelled"}), 200)
    dispatch(client, caller, paid, 2)
    assert quantities_of(engine, caller, source) == (3, 0, 0, 0)
    data = data_of(execute(client, caller, merge_id), 200)
    assert_settled(engine, caller, data, (2, 0, 0, 0), (-2, 0))


# --- 迁移与改挂 ---


def test_open_orders_are_repointed_and_later_order_commands_act_on_the_canonical_sku(
    client, engine, caller
):
    source, canonical, other = add_skus(engine, caller.tenant_id, 3)
    stock(engine, caller, source, 20)
    stock(engine, caller, canonical, 3)
    stock(engine, caller, other, 5)
    to_release = reserve_on(client, caller, source, 1)
    to_commit = reserve_on(client, caller, source, 1)
    expired = reserve_on(client, caller, source, 1)
    expire(engine, caller, expired["reservation_id"])
    review = reserve_on(client, caller, source, 1)
    expire(engine, caller, review["reservation_id"])
    set_status(
        engine,
        review["reservation_id"],
        "late_payment_review",
        **paid_columns(caller, f"pay-{new_id('ref')}"),
    )
    # 已确认的订单另有一行在别的 SKU 上：那一行不改挂。
    lines = [line("l1", source, 2), line("l2", other, 1)]
    paid = data_of(reserve(client, caller, order(lines), actor=ACTOR), 201)
    commit_order(client, caller, paid)
    released = reserve_on(client, caller, source, 1)
    data_of(release(client, caller, released["reservation_id"], {"reason": "cancelled"}), 200)
    refunded = reserve_on(client, caller, source, 1)
    expire(engine, caller, refunded["reservation_id"])
    set_status(
        engine,
        refunded["reservation_id"],
        "closed_refunded",
        **paid_columns(caller, f"pay-{new_id('ref')}"),
        review_closed_at=Reservation.created_at,
        review_closed_credential_id=caller.credential_id,
        review_closed_request_id=request_id(),
    )
    assert quantities_of(engine, caller, source) == (18, 2, 0, 2)
    merge_id = counted_merge(client, caller, source, canonical, 23, 0)
    before = changed_at(engine, caller)

    data = data_of(execute(client, caller, merge_id), 200)
    assert_settled(engine, caller, data, (21, 2, 0, 2), (0, 0))
    assert data["execution"]["repointed_line_count"] == 5

    # 四种状态的来源行改挂；released、closed_refunded 的不改挂。
    moved = [to_release, to_commit, expired, review, paid]
    stayed = [released, refunded]
    skus = line_skus(engine, caller)
    for reservation in moved:
        assert skus[(reservation["reservation_id"], "l1")] == canonical
    for reservation in stayed:
        assert skus[(reservation["reservation_id"], "l1")] == source
    assert skus[(paid["reservation_id"], "l2")] == other
    after = changed_at(engine, caller)
    executed_at = data["execution"]["executed_at"]
    for reservation in moved:
        reservation_id = reservation["reservation_id"]
        assert after[reservation_id] > before[reservation_id]
        assert format_timestamp(after[reservation_id]) == executed_at
    for reservation in stayed:
        reservation_id = reservation["reservation_id"]
        assert after[reservation_id] == before[reservation_id]

    # 之后的释放、确认、晚到确认、发货、退款回库与退货验收都作用到规范 SKU。
    written_before = {entry["entry_id"] for entry in entries(engine, caller)}
    data_of(release(client, caller, to_release["reservation_id"], {"reason": "cancelled"}), 200)
    commit_order(client, caller, to_commit)
    commit_order(client, caller, expired)
    dispatch(client, caller, paid, 1)
    refund_restock(client, caller, paid, 1)
    body = return_body([rline("l1", 1, 0)])
    data_of(receive_return(client, caller, paid["order_ref"], body), 201)

    later = [entry for entry in entries(engine, caller) if entry["entry_id"] not in written_before]
    assert sorted((entry["entry_type"], entry["sku_id"]) for entry in later) == sorted(
        [
            ("RELEASE", canonical),
            ("SALE_COMMIT", canonical),
            ("LATE_SALE_COMMIT", canonical),
            ("DISPATCH", canonical),
            ("REFUND_RESTOCK", canonical),
            ("RETURN_SELLABLE", canonical),
        ]
    )
    assert quantities_of(engine, caller, canonical) == (21, 0, 0, 2)
    assert quantities_of(engine, caller, source) == ZERO


def test_an_empty_source_still_writes_the_pair_of_transfers(client, engine, caller):
    source, canonical = add_skus(engine, caller.tenant_id, 2)
    receipt = receive_entry(client, caller, canonical, 3)
    merge_id = counted_merge(client, caller, source, canonical, 3, 0)
    key, rid = new_key(), request_id()

    data = data_of(execute(client, caller, merge_id, key=key, rid=rid), 200)
    written = assert_settled(engine, caller, data, (3, 0, 0, 0), (0, 0))
    channel_id = channel_of(engine, caller)
    for entry in written.values():
        assert deltas_of(entry) == ZERO
        assert (entry["source_type"], entry["source_ref"]) == ("sku_merge", merge_id)
        assert entry["channel_id"] == channel_id
        assert (entry["reason_code"], entry["note"], entry["line_ref"]) == (None, None, None)
        assert (entry["idempotency_key"], entry["request_id"]) == (key, rid)
        assert entry["credential_id"] == caller.credential_id
        assert (entry["actor_ref"], entry["actor_role"]) == (ACTOR_REF, STOCK_ADMIN)
        assert entry["corrects_entry_id"] is None
    assert written[TRANSFER_OUT]["command_id"] == written[TRANSFER_IN]["command_id"]

    # 差额为 0、没写定数流水，规范 SKU 上执行之前的收货也不能再更正。
    assert not_correctable(correct(client, caller, receipt))["reason"] == "superseded"


def test_receipts_before_the_execution_on_both_skus_can_no_longer_be_corrected(
    client, engine, caller
):
    source, canonical = add_skus(engine, caller.tenant_id, 2)
    on_source = receive_entry(client, caller, source, 2)
    on_canonical = receive_entry(client, caller, canonical, 3)
    merge_id = counted_merge(client, caller, source, canonical, 4, 0)
    key, rid = new_key(), request_id()

    data = data_of(execute(client, caller, merge_id, key=key, rid=rid), 200)
    written = assert_settled(engine, caller, data, (4, 0, 0, 0), (-1, 0))
    channel_id = channel_of(engine, caller)
    assert deltas_of(written[TRANSFER_OUT]) == (-2, 0, 0, 0)
    assert deltas_of(written[TRANSFER_IN]) == (2, 0, 0, 0)
    for entry in written.values():
        assert (entry["source_type"], entry["source_ref"]) == ("sku_merge", merge_id)
        assert entry["channel_id"] == channel_id
        assert (entry["reason_code"], entry["note"], entry["line_ref"]) == (None, None, None)
        assert (entry["idempotency_key"], entry["request_id"]) == (key, rid)

    for receipt in (on_source, on_canonical):
        assert not_correctable(correct(client, caller, receipt))["reason"] == "superseded"


def test_a_reservation_released_after_the_list_is_read_is_not_repointed(
    client, engine, caller, monkeypatch
):
    source, canonical = add_skus(engine, caller.tenant_id, 2)
    stock(engine, caller, source, 5)
    kept = reserve_on(client, caller, source, 1)
    released = reserve_on(client, caller, source, 2)
    merge_id = counted_merge(client, caller, source, canonical, 5, 0)
    before = changed_at(engine, caller)

    # 名单读出之后、锁住预留行之前，另一个连接释放其中一个预留并提交。
    other_client = TestClient(client.app)
    listed: list[list[str]] = []
    responses = []
    real_read = sku_merge_service.read_repoint_candidates

    def read_then_release(connection, tenant_id, pool_id, sku_id):
        found = real_read(connection, tenant_id, pool_id, sku_id)
        listed.append(found)
        if not responses:
            body = {"reason": "cancelled"}
            responses.append(release(other_client, caller, released["reservation_id"], body))
        return found

    monkeypatch.setattr(sku_merge_service, "read_repoint_candidates", read_then_release)
    data = data_of(execute(client, caller, merge_id), 200)

    [response] = responses
    assert data_of(response, 200)["status"] == "released"
    assert listed == [sorted([kept["reservation_id"], released["reservation_id"]])]
    assert data["execution"]["repointed_line_count"] == 1
    skus = line_skus(engine, caller)
    assert skus[(kept["reservation_id"], "l1")] == canonical
    assert skus[(released["reservation_id"], "l1")] == source
    after = changed_at(engine, caller)
    assert format_timestamp(after[kept["reservation_id"]]) == data["execution"]["executed_at"]
    assert after[released["reservation_id"]] > before[released["reservation_id"]]
    assert format_timestamp(after[released["reservation_id"]]) != data["execution"]["executed_at"]
    # 释放先落在来源 SKU 上，迁移的是释放之后的账面。
    assert_settled(engine, caller, data, (5, 1, 0, 0), (0, 0))


def test_each_execution_logs_the_repointed_lines_and_the_transaction_time(
    client, engine, caller, caplog
):
    caplog.set_level(logging.INFO, logger="app.api.sku_merges")
    source, canonical = add_skus(engine, caller.tenant_id, 2)
    stock(engine, caller, source, 3)
    reserve_on(client, caller, source, 1)
    merge_id = counted_merge(client, caller, source, canonical, 3, 0)

    data_of(execute(client, caller, merge_id), 200)

    [record] = [
        record
        for record in caplog.records
        if record.name == "app.api.sku_merges" and merge_id in record.getMessage()
    ]
    logged_id, outcome, repointed, seconds = record.args
    assert (logged_id, outcome, repointed) == (merge_id, "executed", 1)
    # 远短于 5 分钟（§5.9 窗口终点的余量）。
    assert 0 <= seconds < 60


# --- 来源 SKU ---


@pytest.mark.parametrize("source_active", [True, False])
def test_the_source_sku_is_merged_with_one_catalog_change(client, engine, caller, source_active):
    source, canonical = add_skus(engine, caller.tenant_id, 2)
    if not source_active:
        deactivate_sku(engine, source)
    stock(engine, caller, source, 2)
    merge_id = counted_merge(client, caller, source, canonical, 2, 0, reason="same tee")
    before = sku_row(engine, source)
    rid = request_id()

    data_of(execute(client, caller, merge_id, rid=rid), 200)

    after = sku_row(engine, source)
    assert (after["status"], after["merged_into_sku_id"]) == ("inactive", canonical)
    assert after["version"] == before["version"] + 1
    assert sku_row(engine, canonical)["merged_into_sku_id"] is None

    [change] = pull(client, caller)["changes"]
    assert (change["event_type"], change["object_type"]) == ("sku_merged", "sku")
    assert (change["object_id"], change["product_id"]) == (source, before["product_id"])
    assert change["version"] == after["version"]
    expected = {"merged_into_sku_id": {"before": None, "after": canonical}}
    if source_active:
        expected["status"] = {"before": "active", "after": "inactive"}
    assert change["changes"] == expected
    assert change["reason"] == "same tee"
    assert (change["actor_ref"], change["request_id"]) == (ACTOR_REF, rid)
    assert change["import_id"] is None

    # 带来源 SKU 的新预留：409 SKU_MERGED，附规范 ID。
    error = error_of(reserve(client, caller, order([line("l1", source, 1)]), actor=ACTOR), 409)
    assert error["code"] == "SKU_MERGED"
    assert error["details"] == {"sku_id": source, "merged_into_sku_id": canonical}


# --- 状态与重复 ---


def test_executing_an_executed_merge_returns_it_and_writes_nothing(client, engine, caller):
    source, canonical = add_skus(engine, caller.tenant_id, 2)
    stock(engine, caller, source, 2)
    stock(engine, caller, canonical, 3)
    merge_id = counted_merge(client, caller, source, canonical, 4, 0)
    key, rid = new_key(), request_id()
    first = execute(client, caller, merge_id, key=key, rid=rid)
    data = data_of(first, 200)
    assert set(data) == MERGE_KEYS
    assert data_of(get_merge(client, caller, merge_id), 200) == data
    row = stored_merge(engine, merge_id)
    assert row["executed_credential_id"] == caller.credential_id
    assert (row["executed_actor_ref"], row["executed_actor_role"]) == (ACTOR_REF, STOCK_ADMIN)
    assert row["executed_request_id"] == rid
    assert format_timestamp(row["executed_at"]) == data["execution"]["executed_at"]
    written = entries(engine, caller)

    # 同键：原样重放。
    replay = execute(client, caller, merge_id, key=key)
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == first.json()

    # 换键：200 与当前表示，不再执行。
    again = execute(client, caller, merge_id)
    assert "Idempotency-Replayed" not in again.headers
    assert data_of(again, 200) == data
    assert entries(engine, caller) == written
    assert stored_merge(engine, merge_id)["executed_request_id"] == rid

    # 完成之后同样。
    completed = data_of(complete(client, caller, merge_id), 200)
    assert data_of(execute(client, caller, merge_id), 200) == completed
    assert entries(engine, caller) == written


def test_a_merge_not_counted_yet_or_cancelled_is_a_state_conflict(client, engine, caller):
    source, canonical = add_skus(engine, caller.tenant_id, 2)
    paused = created(create(client, caller, source, canonical))["merge_id"]
    key = new_key()
    response = execute(client, caller, paused, key=key)
    # 还没录入的单只查销售入口，在事务里按状态判出、作为确定结果保存。
    assert_state_conflict(response, {"status": "selling_paused"})
    replay = execute(client, caller, paused, key=key)
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == response.json()
    data_of(count(client, caller, paused, 0), 200)
    data_of(cancel(client, caller, paused), 200)
    assert_state_conflict(execute(client, caller, paused), {"status": "cancelled"})

    assert entries(engine, caller) == []
    assert sku_row(engine, source)["merged_into_sku_id"] is None


def test_a_canonical_sku_deactivated_after_creating_is_409_and_nothing_is_written(
    client, engine, caller
):
    source, canonical = add_skus(engine, caller.tenant_id, 2)
    stock(engine, caller, source, 2)
    reserved = reserve_on(client, caller, source, 1)
    merge_id = counted_merge(client, caller, source, canonical, 2, 0)
    deactivate_sku(engine, canonical)
    before = snapshot_of(engine, caller, merge_id)
    changes_before = changed_at(engine, caller)

    response = execute(client, caller, merge_id)
    error = assert_rejected_and_kept(engine, caller, merge_id, before, response, "SKU_INACTIVE")
    assert error["details"] == {"sku_ids": [canonical]}
    assert changed_at(engine, caller) == changes_before
    assert line_skus(engine, caller)[(reserved["reservation_id"], "l1")] == source
    assert pull(client, caller)["changes"] == []


# --- 鉴权与池 ---


def test_executing_needs_stock_admin(client, engine, caller):
    source, canonical = add_skus(engine, caller.tenant_id, 2)
    merge_id = counted_merge(client, caller, source, canonical, 0, 0)
    rid = request_id()
    response = execute(client, caller, merge_id, role=STOCK_OPERATOR, rid=rid)
    assert error_of(response, 403)["code"] == "FORBIDDEN"
    [event] = events(engine, rid)
    assert event["details"] == {"category": "role", "role": STOCK_OPERATOR}
    assert execute_records(engine, caller) == []
    assert stored_merge(engine, merge_id)["status"] == "counted"


def test_a_credential_without_a_sales_channel_is_forbidden(client, engine, caller):
    source, canonical = add_skus(engine, caller.tenant_id, 2)
    counted_id = counted_merge(client, caller, source, canonical, 0, 0)
    other_source, other_canonical = add_skus(engine, caller.tenant_id, 2)
    paused_id = created(create(client, caller, other_source, other_canonical))["merge_id"]
    no_channel = same_tenant_caller(engine, caller)

    # 录入过与还没录入的单都先查销售入口。
    for merge_id in (counted_id, paused_id):
        rid = request_id()
        response = execute(client, no_channel, merge_id, rid=rid)
        assert error_of(response, 403)["code"] == "FORBIDDEN"
        [event] = events(engine, rid)
        assert event["event_type"] == "forbidden"
        assert event["details"]["category"] == "pool"
        assert execute_records(engine, no_channel) == []
    assert stored_merge(engine, counted_id)["status"] == "counted"
    assert stored_merge(engine, paused_id)["status"] == "selling_paused"


def test_a_missing_or_foreign_merge_is_404_and_not_saved(client, engine, caller):
    foreign = live_caller(engine, scopes=[*ALL_SCOPES, ORDERS_READ])
    foreign_source, foreign_canonical = add_skus(engine, foreign.tenant_id, 2)
    # 别的租户的单还没录入实数（没有池）。
    foreign_id = created(create(client, foreign, foreign_source, foreign_canonical))["merge_id"]
    missing_id = new_id("smg")

    missing_rid, foreign_rid = request_id(), request_id()
    missing = error_of(execute(client, caller, missing_id, rid=missing_rid), 404)
    found = error_of(execute(client, caller, foreign_id, rid=foreign_rid), 404)
    assert missing == found
    assert missing["code"] == "SKU_MERGE_NOT_FOUND"
    assert events(engine, missing_rid) == []
    [event] = events(engine, foreign_rid)
    assert event["event_type"] == "cross_tenant"
    assert event["details"] == {"object_type": "sku_merge", "id": foreign_id}
    # 池授权在取幂等锁之前：都不保存。
    assert execute_records(engine, caller) == []
    assert stored_merge(engine, foreign_id)["status"] == "selling_paused"


def test_a_merge_counted_on_another_pool_is_forbidden_before_the_lock(client, engine, caller):
    source, canonical = add_skus(engine, caller.tenant_id, 2)
    merge_id = created(create(client, caller, source, canonical))["merge_id"]
    other_pool = add_other_pool(engine, caller.tenant_id)
    count_on_another_pool(engine, caller, merge_id, other_pool)

    rid = request_id()
    response = execute(client, caller, merge_id, rid=rid)
    assert error_of(response, 403)["code"] == "FORBIDDEN"
    [event] = events(engine, rid)
    assert event["details"] == {
        "category": "pool",
        "pool_id": other_pool,
        "object_type": "sku_merge",
        "object_id": merge_id,
    }
    assert execute_records(engine, caller) == []
    assert stored_merge(engine, merge_id)["status"] == "counted"


def test_a_pool_recorded_after_the_pool_check_is_forbidden_inside_the_lock_and_saved(
    client, engine, caller, monkeypatch
):
    source, canonical = add_skus(engine, caller.tenant_id, 2)
    merge_id = created(create(client, caller, source, canonical))["merge_id"]
    other_pool = add_other_pool(engine, caller.tenant_id)

    # 池授权读到的单还没录入；之后、开事务之前它被录入到同租户的另一个池。
    real_run = sku_merge_api.run_idempotent

    def count_elsewhere_then_run(request, **kwargs):
        count_on_another_pool(engine, caller, merge_id, other_pool)
        return real_run(request, **kwargs)

    monkeypatch.setattr(sku_merge_api, "run_idempotent", count_elsewhere_then_run)
    key, rid = new_key(), request_id()
    response = execute(client, caller, merge_id, key=key, rid=rid)

    assert error_of(response, 403)["code"] == "FORBIDDEN"
    pool_event = {
        "category": "pool",
        "pool_id": other_pool,
        "object_type": "sku_merge",
        "object_id": merge_id,
    }
    [event] = events(engine, rid)
    assert (event["event_type"], event["details"]) == ("forbidden", pool_event)
    # 锁内按数据库状态判出的 403 作为确定结果保存。
    [record] = execute_records(engine, caller)
    assert (record["idempotency_key"], record["response_status"]) == (key, 403)
    assert record["response_body"] == response.json()
    assert stored_merge(engine, merge_id)["status"] == "counted"
    assert entries(engine, caller) == []

    # 同键重试：锁外的池授权已读到新池，再 403、不重放，多记一条 forbidden 事件。
    retry_rid = request_id()
    retry = execute(client, caller, merge_id, key=key, rid=retry_rid)
    assert error_of(retry, 403)["code"] == "FORBIDDEN"
    assert "Idempotency-Replayed" not in retry.headers
    [retry_event] = events(engine, retry_rid)
    assert (retry_event["event_type"], retry_event["details"]) == ("forbidden", pool_event)
    assert len(execute_records(engine, caller)) == 1
    assert rid_of(retry) == retry_rid
