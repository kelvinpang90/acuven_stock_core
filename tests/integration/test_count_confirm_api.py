"""盘点单的确认落账（契约第 6 节、§4.1、§6.6、§6.7、§2.4、§7.1、§10 第 3、4 条）。

在真 MySQL 上跑；每个测试建自己的租户、池与凭据，池先切到 live。
SKU、余额行与“别的命令”的流水（释放预留、合并迁移）由测试直接在库里造（自己提交的连接）；
收货与更正走接口。并发的测试在服务层的 lock_balances 之后放 Gate，
让先到的确认持有余额行锁停住，再发出后到的确认。
"""

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, update

from app.config import Settings
from app.domain.requests import STOCK_ADMIN, STOCK_OPERATOR
from app.domain.service_auth import STOCK_READ
from app.main import create_app
from app.models.inventory import StockBalance
from app.services import counts as count_service
from app.services.stock import format_timestamp
from tests.integration.conftest import TEST_DATABASE_URL, entry_insert, new_id
from tests.integration.test_api_auth import Caller, error_of, events
from tests.integration.test_catalog_create_api import created
from tests.integration.test_catalog_queries_api import data_of
from tests.integration.test_counts_api import (
    ACTOR_REF,
    COUNTS,
    balance_row,
    counted,
    get_count,
    live_caller,
    new_count,
    new_key,
    new_ref,
    preset_balance,
    request_id,
    stored_count,
    stored_lines,
    submit,
    write_headers,
)
from tests.integration.test_idempotency import add_skus
from tests.integration.test_stock_imports_api import merge
from tests.integration.test_stock_service import Gate, entries, judgement, quantities_of

RECEIPTS = "/v1/receipts"
CORRECTIONS = "/v1/corrections"
REASON = "cycle_count"


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
    """池已 live 的调用方。"""
    return live_caller(engine)


# --- 请求 ---


def confirmation(reason_code: object = REASON, **extra: object) -> dict:
    return {"reason_code": reason_code} | extra


def confirm(
    client: TestClient,
    caller: Caller,
    count_id: str,
    body: dict | None = None,
    role: str = STOCK_ADMIN,
    **headers,
):
    body = confirmation() if body is None else body
    url = f"{COUNTS}/{count_id}/confirm"
    return client.post(url, json=body, headers=write_headers(caller, role=role, **headers))


def receive(client: TestClient, caller: Caller, sku_id: str, quantity: int) -> str:
    """收货，返回这条 RECEIPT 的 entry_id。"""
    body = {
        "pool_id": caller.pool_id,
        "receipt_ref": new_ref(),
        "lines": [{"line_ref": "L", "sku_id": sku_id, "quantity": quantity}],
    }
    data = created(client.post(RECEIPTS, json=body, headers=write_headers(caller)))
    return data["lines"][0]["entry_id"]


def correct(client: TestClient, caller: Caller, entry_id: str):
    body = {"entry_id": entry_id, "reason_code": "wrong_receipt"}
    return client.post(CORRECTIONS, json=body, headers=write_headers(caller, role=STOCK_ADMIN))


def submitted_count(client: TestClient, caller: Caller, skus: list) -> tuple[str, dict]:
    """开单并提交实数，返回 count_id 与提交后的表示。"""
    count_id = new_count(client, caller)["count_id"]
    return count_id, data_of(submit(client, caller, count_id, skus))


# --- 造数据与观察 ---


def state_of(engine: Engine, caller: Caller, count_id: str, sku_ids: list[str]) -> dict:
    """被拒时什么也不写：流水、余额行、盘点单与盘点行。"""
    return {
        "entries": entries(engine, caller),
        "balances": [balance_row(engine, caller, sku_id) for sku_id in sku_ids],
        "count": stored_count(engine, count_id),
        "lines": stored_lines(engine, count_id),
    }


def write_entry(
    engine: Engine,
    caller: Caller,
    sku_id: str,
    entry_type: str,
    deltas: tuple,
    *,
    judged: bool = False,
) -> None:
    """照别的命令的样子在一个余额行上写一条流水并更新余额行。

    judged 为真时（只改账的流水，如合并迁移）两个判定列都设为这条流水的版本。
    """
    names = ("stock_balance", "reserved", "unavailable", "paid_pending_dispatch")
    row = balance_row(engine, caller, sku_id)
    version = row["balance_version"] + 1
    after = {name: row[name] + delta for name, delta in zip(names, deltas, strict=True)}
    values = {
        **{f"{name}_delta": delta for name, delta in zip(names, deltas, strict=True)},
        **{f"{name}_after": value for name, value in after.items()},
    }
    columns = after | {"balance_version": version}
    if judged:
        columns |= {"last_book_only_version": version, "last_reconciled_version": version}
    with engine.begin() as db:
        balance = (caller.tenant_id, caller.pool_id, sku_id)
        db.execute(entry_insert(balance, version, entry_type=entry_type, **values))
        db.execute(
            update(StockBalance)
            .where(
                StockBalance.tenant_id == caller.tenant_id,
                StockBalance.pool_id == caller.pool_id,
                StockBalance.sku_id == sku_id,
            )
            .values(columns)
        )


def adjustments(engine: Engine, caller: Caller) -> list[dict]:
    """COUNT_ADJUSTMENT 流水，按 sku_id 与版本排序。"""
    written = entries(engine, caller)
    return [entry for entry in written if entry["entry_type"] == "COUNT_ADJUSTMENT"]


# --- 差额法落账 ---


def test_confirming_settles_the_differences_on_the_latest_book(client, engine, caller):
    received, short, damaged = sorted(add_skus(engine, caller.tenant_id, 3))
    preset_balance(engine, caller, received, stock_balance=20, balance_version=1)
    preset_balance(engine, caller, short, stock_balance=10, balance_version=1)
    preset_balance(engine, caller, damaged, stock_balance=10, unavailable=2, balance_version=1)
    ref = new_ref()
    count_id = new_count(client, caller, count_ref=ref)["count_id"]
    skus = [
        # 点到 20，与账面相符。
        counted(received, 20),
        # 在架少 3 件。
        counted(short, 7),
        # 在架 8 件不变，不可售多找出 3 件：只改不可售件数。
        counted(damaged, 8, 5),
    ]
    submitted = data_of(submit(client, caller, count_id, skus))
    # 提交之后收货 10：差额法保留它。
    receive(client, caller, received, 10)

    key, rid = new_key(), request_id()
    body = confirmation(note="  north aisle  ")
    response = confirm(client, caller, count_id, body, key=key, rid=rid)
    data = data_of(response)

    assert quantities_of(engine, caller, received) == (30, 0, 0, 0)
    assert quantities_of(engine, caller, short) == (7, 0, 0, 0)
    assert quantities_of(engine, caller, damaged) == (13, 0, 5, 0)

    # 差额为 0 的不写流水；另两个各一条 COUNT_ADJUSTMENT，同一命令。
    written = adjustments(engine, caller)
    assert [entry["sku_id"] for entry in written] == [short, damaged]
    by_sku = {entry["sku_id"]: entry for entry in written}
    deltas = ("stock_balance", "reserved", "unavailable", "paid_pending_dispatch")
    assert tuple(by_sku[short][f"{name}_delta"] for name in deltas) == (-3, 0, 0, 0)
    assert tuple(by_sku[damaged][f"{name}_delta"] for name in deltas) == (3, 0, 3, 0)
    assert len({entry["command_id"] for entry in written}) == 1
    for entry in written:
        assert (entry["source_type"], entry["source_ref"]) == ("count", ref)
        assert (entry["reason_code"], entry["note"]) == (REASON, "north aisle")
        assert (entry["actor_ref"], entry["actor_role"]) == (ACTOR_REF, STOCK_ADMIN)
        assert (entry["idempotency_key"], entry["request_id"]) == (key, rid)
        assert entry["credential_id"] == caller.credential_id
        assert entry["line_ref"] is None and entry["corrects_entry_id"] is None
        assert format_timestamp(entry["occurred_at"]) == data["confirmed_at"]

    # 表示：提交时的盘点行加上 entry_id；盘点单记下确认人与原因。
    entry_ids = {sku_id: by_sku[sku_id]["entry_id"] for sku_id in (short, damaged)}
    entry_ids[received] = None
    assert data["lines"] == [
        line | {"entry_id": entry_ids[line["sku_id"]]} for line in submitted["lines"]
    ]
    assert data["status"] == "confirmed"
    assert (data["reason_code"], data["confirm_note"]) == (REASON, "north aisle")
    assert data["cancelled_at"] is None
    for name in ("count_id", "count_ref", "pool_id", "note", "created_at"):
        assert data[name] == submitted[name]
    row = stored_count(engine, count_id)
    assert row["status"] == "confirmed"
    assert format_timestamp(row["confirmed_at"]) == data["confirmed_at"]
    assert row["confirmed_credential_id"] == caller.credential_id
    assert (row["confirmed_actor_ref"], row["confirmed_request_id"]) == (ACTOR_REF, rid)
    assert (row["confirmed_reason_code"], row["confirm_note"]) == (REASON, "north aisle")
    assert [line["entry_id"] for line in stored_lines(engine, count_id)] == [
        entry_ids[sku_id] for sku_id in (received, short, damaged)
    ]
    assert data_of(get_count(client, caller, count_id)) == data

    # 同键重放首次结果。
    replay = confirm(client, caller, count_id, body, key=key)
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == response.json()


def test_judgement_columns_follow_the_snapshot(client, engine, caller):
    verified, adjusted = sorted(add_skus(engine, caller.tenant_id, 2))
    before = {sku_id: receive(client, caller, sku_id, 10) for sku_id in (verified, adjusted)}
    # 快照都在版本 1：verified 点到 10（差额 0），adjusted 点到 8。
    count_id, _ = submitted_count(client, caller, [counted(verified, 10), counted(adjusted, 8)])
    # 快照之后、确认之前的收货（版本 2）。
    after = {sku_id: receive(client, caller, sku_id, 5) for sku_id in (verified, adjusted)}

    data = data_of(confirm(client, caller, count_id))
    lines = {line["sku_id"]: line for line in data["lines"]}
    assert lines[verified]["entry_id"] is None
    [entry] = adjustments(engine, caller)
    assert entry["entry_id"] == lines[adjusted]["entry_id"]
    assert entry["balance_version_after"] == 3

    # last_book_only_version：写了流水的为它的版本，差额为 0 的为当时的 balance_version；
    # last_reconciled_version：两种都是快照版本 1。
    assert judgement(engine, caller, verified) == (2, 2, 1, None)
    assert judgement(engine, caller, adjusted) == (3, 3, 1, None)
    assert quantities_of(engine, caller, verified) == (15, 0, 0, 0)
    assert quantities_of(engine, caller, adjusted) == (13, 0, 0, 0)

    # 快照之后录的收货没有被核实，仍可更正；快照之前的已被吸收。
    for sku_id in (verified, adjusted):
        corrected = created(correct(client, caller, after[sku_id]))
        assert corrected["corrected_entry_id"] == after[sku_id]
        error = error_of(correct(client, caller, before[sku_id]), 409)
        assert error["code"] == "ENTRY_NOT_CORRECTABLE"
        assert error["details"] == {"reason": "superseded", "last_reconciled_version": 1}
    assert quantities_of(engine, caller, verified) == (10, 0, 0, 0)
    assert quantities_of(engine, caller, adjusted) == (8, 0, 0, 0)


# --- COUNT_STALE ---


def test_a_count_stale_after_another_counts_adjustment_is_409(client, engine, caller):
    shared, fresh = sorted(add_skus(engine, caller.tenant_id, 2))
    preset_balance(engine, caller, shared, stock_balance=10, balance_version=1)
    preset_balance(engine, caller, fresh, stock_balance=4, balance_version=1)
    first_id, _ = submitted_count(client, caller, [counted(shared, 8)])
    second_id, _ = submitted_count(client, caller, [counted(shared, 8), counted(fresh, 3)])

    data_of(confirm(client, caller, first_id))
    assert quantities_of(engine, caller, shared) == (8, 0, 0, 0)

    # 不拦会再记一次 −2。
    before = state_of(engine, caller, second_id, [shared, fresh])
    key = new_key()
    response = confirm(client, caller, second_id, key=key)
    error = error_of(response, 409)
    assert error["code"] == "COUNT_STALE"
    assert error["details"] == {"sku_ids": [shared]}
    assert state_of(engine, caller, second_id, [shared, fresh]) == before
    assert before["count"]["status"] == "open"
    replay = confirm(client, caller, second_id, key=key)
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == response.json()

    # 只为作废的 SKU 重新提交（新快照 8/版本 2），换键再确认。
    data_of(submit(client, caller, second_id, [counted(shared, 8)]))
    data = data_of(confirm(client, caller, second_id))
    lines = {line["sku_id"]: line for line in data["lines"]}
    assert lines[shared]["entry_id"] is None
    assert lines[fresh]["entry_id"] is not None
    assert quantities_of(engine, caller, shared) == (8, 0, 0, 0)
    assert quantities_of(engine, caller, fresh) == (3, 0, 0, 0)
    assert [entry["sku_id"] for entry in adjustments(engine, caller)] == sorted([shared, fresh])


def test_a_zero_difference_verification_at_the_snapshot_is_not_stale(client, engine, caller):
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    preset_balance(engine, caller, sku_id, stock_balance=10, balance_version=1)
    verifying_id, _ = submitted_count(client, caller, [counted(sku_id, 10)])
    adjusting_id, _ = submitted_count(client, caller, [counted(sku_id, 7)])

    # 差额为 0 的核实把 last_book_only_version 设为 1，等于另一张单的快照版本。
    data_of(confirm(client, caller, verifying_id))
    assert judgement(engine, caller, sku_id) == (1, 1, 1, None)
    data = data_of(confirm(client, caller, adjusting_id))
    assert data["lines"][0]["entry_id"] is not None
    assert quantities_of(engine, caller, sku_id) == (7, 0, 0, 0)


def test_a_receipt_before_a_zero_verification_makes_others_stale(client, engine, caller):
    """快照之后、别的盘点以差额 0 核实之前另有流水：偏保守，报 COUNT_STALE（§6）。"""
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    preset_balance(engine, caller, sku_id, stock_balance=10, balance_version=1)
    verifying_id, _ = submitted_count(client, caller, [counted(sku_id, 10)])
    adjusting_id, _ = submitted_count(client, caller, [counted(sku_id, 7)])
    receive(client, caller, sku_id, 5)

    data_of(confirm(client, caller, verifying_id))
    assert judgement(engine, caller, sku_id) == (2, 2, 1, None)
    error = error_of(confirm(client, caller, adjusting_id), 409)
    assert (error["code"], error["details"]) == ("COUNT_STALE", {"sku_ids": [sku_id]})
    assert quantities_of(engine, caller, sku_id) == (15, 0, 0, 0)


def test_a_sku_merged_after_the_submission_is_stale(client, engine, caller):
    source, canonical, other = add_skus(engine, caller.tenant_id, 3)
    preset_balance(engine, caller, source, stock_balance=5, balance_version=1)
    preset_balance(engine, caller, canonical, stock_balance=10, balance_version=1)
    preset_balance(engine, caller, other, stock_balance=2, balance_version=1)
    skus = [counted(source, 5), counted(canonical, 9), counted(other, 2)]
    count_id, _ = submitted_count(client, caller, skus)

    # §2.4 第 1 步：成对的迁移流水，两个余额行的判定列都设为各自迁移流水的版本。
    write_entry(engine, caller, source, "MERGE_TRANSFER_OUT", (-5, 0, 0, 0), judged=True)
    write_entry(engine, caller, canonical, "MERGE_TRANSFER_IN", (5, 0, 0, 0), judged=True)
    merge(engine, source, canonical)

    sku_ids = [source, canonical, other]
    before = state_of(engine, caller, count_id, sku_ids)
    error = error_of(confirm(client, caller, count_id), 409)
    assert error["code"] == "COUNT_STALE"
    assert error["details"] == {"sku_ids": sorted([source, canonical])}
    assert state_of(engine, caller, count_id, sku_ids) == before


# --- COUNT_BELOW_COMMITMENTS ---


def test_a_count_below_reservations_is_409_until_they_are_released(client, engine, caller):
    reserved, fine = sorted(add_skus(engine, caller.tenant_id, 2))
    preset_balance(engine, caller, reserved, stock_balance=10, reserved=4, balance_version=1)
    preset_balance(engine, caller, fine, stock_balance=6, balance_version=1)
    count_id, submitted = submitted_count(client, caller, [counted(reserved, 3), counted(fine, 5)])

    sku_ids = [reserved, fine]
    before = state_of(engine, caller, count_id, sku_ids)
    body = confirmation(note="short")
    error = error_of(confirm(client, caller, count_id, body), 409)
    assert error["code"] == "COUNT_BELOW_COMMITMENTS"
    # 落账后 3/4/0/0：缺口 max(0, 4 + 0 − 3, −3, −0) = 1；不违反的 SKU 不列。
    assert error["details"] == {
        "skus": [
            {
                "sku_id": reserved,
                "stock_balance": 3,
                "reserved": 4,
                "unavailable": 0,
                "paid_pending_dispatch": 0,
                "shortfall": 1,
            }
        ]
    }
    assert state_of(engine, caller, count_id, sku_ids) == before

    # 预留释放（RELEASE 流水不是只改账的流水，不动判定列）后，换键、正文照旧直接再确认。
    write_entry(engine, caller, reserved, "RELEASE", (0, -4, 0, 0))
    data = data_of(confirm(client, caller, count_id, body))
    # 沿用原快照（10/4/0/0，版本 1）：差额 −7 加到 10/0/0/0 上。
    original = [line | {"entry_id": None} for line in data["lines"]]
    assert original == submitted["lines"]
    assert quantities_of(engine, caller, reserved) == (3, 0, 0, 0)
    assert quantities_of(engine, caller, fine) == (5, 0, 0, 0)
    entry = next(entry for entry in adjustments(engine, caller) if entry["sku_id"] == reserved)
    assert (entry["stock_balance_delta"], entry["balance_version_after"]) == (-7, 3)
    assert judgement(engine, caller, reserved) == (3, 3, 1, None)


# --- 状态与请求 ---


def test_a_count_without_lines_is_422(client, engine, caller):
    count_id = new_count(client, caller)["count_id"]
    key = new_key()
    response = confirm(client, caller, count_id, key=key)
    error = error_of(response, 422)
    assert error["code"] == "INVALID_REQUEST"
    assert error["details"]["reason"] == "count_has_no_lines"
    assert stored_count(engine, count_id)["status"] == "open"
    # 按数据库状态判出，作为确定结果保存。
    replay = confirm(client, caller, count_id, key=key)
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == response.json()


def test_a_confirmed_count_cannot_change_again(client, engine, caller):
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    preset_balance(engine, caller, sku_id, stock_balance=5, balance_version=1)
    count_id, _ = submitted_count(client, caller, [counted(sku_id, 4)])
    key = new_key()
    first = confirm(client, caller, count_id, key=key)
    data = data_of(first)

    for response in (
        confirm(client, caller, count_id),
        submit(client, caller, count_id, [counted(sku_id, 1)]),
        client.post(f"{COUNTS}/{count_id}/cancel", headers=write_headers(caller)),
    ):
        error = error_of(response, 409)
        assert error["code"] == "STATE_CONFLICT"
        assert error["details"] == {"status": "confirmed"}

    replay = confirm(client, caller, count_id, key=key)
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == first.json()
    assert len(adjustments(engine, caller)) == 1
    assert quantities_of(engine, caller, sku_id) == (4, 0, 0, 0)
    assert data_of(get_count(client, caller, count_id)) == data


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"note": "x"},
        {"reason_code": None},
        {"reason_code": ""},
        {"reason_code": "Has-Caps"},
        {"reason_code": "x" * 33},
        {"reason_code": 7},
        {"reason_code": REASON, "note": "   "},
        {"reason_code": REASON, "note": "x" * 501},
        {"reason_code": REASON, "occurred_at": "2026-11-15T00:00:00Z"},
    ],
)
def test_a_bad_confirmation_body_is_422(client, engine, caller, body):
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    count_id, _ = submitted_count(client, caller, [counted(sku_id, 1)])
    assert error_of(confirm(client, caller, count_id, body), 422)["code"] == "INVALID_REQUEST"
    assert stored_count(engine, count_id)["status"] == "open"
    assert entries(engine, caller) == []


def test_a_null_note_is_the_same_as_no_note(client, engine, caller):
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    count_id, _ = submitted_count(client, caller, [counted(sku_id, 2)])
    data = data_of(confirm(client, caller, count_id, confirmation(note=None)))
    assert data["confirm_note"] is None
    [entry] = adjustments(engine, caller)
    assert (entry["reason_code"], entry["note"]) == (REASON, None)


# --- 授权 ---


def test_confirming_needs_stock_admin_and_stock_write(client, engine, caller):
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    count_id, _ = submitted_count(client, caller, [counted(sku_id, 2)])

    rid = request_id()
    response = confirm(client, caller, count_id, role=STOCK_OPERATOR, rid=rid)
    assert error_of(response, 403)["code"] == "FORBIDDEN"
    [event] = events(engine, rid)
    assert event["details"] == {"category": "role", "role": STOCK_OPERATOR}

    reader = live_caller(engine, scopes=[STOCK_READ])
    assert error_of(confirm(client, reader, count_id), 403)["code"] == "FORBIDDEN"
    assert stored_count(engine, count_id)["status"] == "open"
    assert entries(engine, caller) == []


def test_a_foreign_or_missing_count_is_404(client, engine, caller):
    foreign = live_caller(engine)
    [foreign_sku] = add_skus(engine, foreign.tenant_id, 1)
    foreign_id, _ = submitted_count(client, foreign, [counted(foreign_sku, 1)])

    missing_rid, foreign_rid = request_id(), request_id()
    missing = error_of(confirm(client, caller, new_id("cnt"), rid=missing_rid), 404)
    assert error_of(confirm(client, caller, foreign_id, rid=foreign_rid), 404) == missing
    assert missing["code"] == "COUNT_NOT_FOUND"
    assert events(engine, missing_rid) == []
    [event] = events(engine, foreign_rid)
    assert event["details"] == {"object_type": "count", "id": foreign_id}
    assert stored_count(engine, foreign_id)["status"] == "open"
    assert entries(engine, foreign) == []


# --- 并发 ---


def test_concurrent_confirmations_do_not_count_a_difference_twice(
    client, engine, caller, monkeypatch
):
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    preset_balance(engine, caller, sku_id, stock_balance=10, balance_version=1)
    first_id, _ = submitted_count(client, caller, [counted(sku_id, 8)])
    second_id, _ = submitted_count(client, caller, [counted(sku_id, 8)])

    # 先到者锁住余额行后停住；后到者在这把锁上等待，先到者提交后它看到新写的流水。
    holding = Gate()
    lock_balances = count_service.lock_balances

    def gated(execution, pool_id, sku_ids):
        balances = lock_balances(execution, pool_id, sku_ids)
        holding.pass_through()
        return balances

    monkeypatch.setattr(count_service, "lock_balances", gated)
    other_client = TestClient(client.app)
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            early = pool.submit(confirm, client, caller, first_id)
            assert holding.reached.wait(10)
            late = pool.submit(confirm, other_client, caller, second_id)
            time.sleep(0.5)
            assert not late.done()
        finally:
            holding.proceed.set()
        early_response, late_response = early.result(), late.result()

    assert data_of(early_response)["status"] == "confirmed"
    error = error_of(late_response, 409)
    assert (error["code"], error["details"]) == ("COUNT_STALE", {"sku_ids": [sku_id]})
    assert len(adjustments(engine, caller)) == 1
    assert quantities_of(engine, caller, sku_id) == (8, 0, 0, 0)
    assert stored_count(engine, second_id)["status"] == "open"
