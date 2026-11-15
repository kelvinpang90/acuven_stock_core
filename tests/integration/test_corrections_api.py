"""更正（契约 §4.3、§6.2、§6.7、§7.2、§10 第 3、4 条）。

在真 MySQL 上跑；每个测试建自己的租户、池与凭据。原流水多数经收货、不可售调整与期初的接口造；
接口造不出的（预留、盘点差异、合并迁移、差额为 0 的核实）经 app/services/stock.py 的函数造：
测试给应用挂 test_stock_service 的测试专用写路由，命令登记在它的 COMMANDS 里。
个别流水（一条预留、另一个池与别的租户的）直接在库里造。
并发的测试在服务层的 lock_balances 之前与之后放 Gate，让两笔更正在同一余额行的锁上交错。
"""

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.config import Settings
from app.domain.ledger import EntryType
from app.domain.requests import STOCK_ADMIN, STOCK_OPERATOR
from app.main import create_app
from app.models.inventory import QUANTITIES
from app.services import stock_documents as stock_document_service
from app.services.idempotency import CommandResult, Execution
from app.services.stock import (
    append_entry,
    command_now,
    entry_representation,
    format_timestamp,
    lock_balances,
    mark_verified,
    new_command_id,
)
from tests.integration.conftest import TEST_DATABASE_URL, add_entry, new_id
from tests.integration.test_api_auth import Caller, add_other_pool, error_of, events, issue
from tests.integration.test_idempotency import add_skus
from tests.integration.test_stock_documents_api import (
    ADJUSTMENTS,
    RECEIPTS,
    adjustment_body,
    idempotency_records,
    line,
    new_key,
    post,
    preset_balance,
    receipt_body,
    request_id,
    snapshot,
)
from tests.integration.test_stock_imports_api import confirm, merge, preview, set_live
from tests.integration.test_stock_service import (
    COMMANDS,
    Gate,
    StockBody,
    add_test_routes,
    body_of,
    data_of,
    documents,
    entries,
    judgement,
    quantities_of,
    send,
)

CORRECTIONS = "/v1/corrections"
CORRECTIONS_CREATE = "corrections.create"
RESULT_KEYS = {"document_id", "corrected_entry_id", "entry"}


@pytest.fixture
def client(migrated_engine: Engine) -> Iterator[TestClient]:
    app = create_app(Settings(env="demo", git_sha="c" * 40, database_url=TEST_DATABASE_URL))
    add_test_routes(app)
    try:
        yield TestClient(app)
    finally:
        app.state.engine.dispose()
        COMMANDS.clear()


@pytest.fixture
def engine(migrated_engine: Engine) -> Engine:
    return migrated_engine


@pytest.fixture
def caller(engine: Engine) -> Caller:
    """池已 live 的调用方。"""
    issued = issue(engine)
    set_live(engine, issued)
    return issued


# --- 请求 ---


def correction_body(target: str, **extra: object) -> dict:
    return {"entry_id": target, "reason_code": "wrong_entry"} | extra


def correct(client: TestClient, caller: Caller, target: str, role: str = STOCK_ADMIN, **extra):
    """extra 里的 key、rid 是请求头，其余进正文（可以覆盖 entry_id、reason_code）。"""
    headers = {name: extra.pop(name) for name in ("key", "rid") if name in extra}
    body = correction_body(target, **extra)
    return post(client, caller, CORRECTIONS, body, role=role, **headers)


def not_correctable(response) -> dict:
    error = error_of(response, 409)
    assert error["code"] == "ENTRY_NOT_CORRECTABLE"
    return error["details"]


def corrections_of(engine: Engine, caller: Caller) -> tuple[list[dict], list[dict]]:
    """CORRECTION 流水与更正单据。"""
    written = [row for row in entries(engine, caller) if row["entry_type"] == "CORRECTION"]
    kept = [row for row in documents(engine, caller) if row["document_type"] == "correction"]
    return written, kept


def correction_records(engine: Engine, caller: Caller) -> list[dict]:
    records = idempotency_records(engine, caller)
    return [row for row in records if row["operation"] == CORRECTIONS_CREATE]


# --- 造原流水 ---


def receive(client: TestClient, caller: Caller, sku_id: str, quantity: int) -> str:
    """经收货接口收一行，返回它的流水 ID。"""
    body = receipt_body(caller, [line("A", sku_id, quantity)])
    return data_of(post(client, caller, RECEIPTS, body), 201)["lines"][0]["entry_id"]


def adjust(client: TestClient, caller: Caller, action: str, sku_id: str, quantity: int) -> str:
    body = adjustment_body(caller, action, [line("A", sku_id, quantity)])
    response = post(client, caller, ADJUSTMENTS, body, role=STOCK_ADMIN)
    return data_of(response, 201)["lines"][0]["entry_id"]


def open_with(client: TestClient, caller: Caller, sku_id: str, quantity: int) -> dict:
    """经期初导入的预览与确认导一行期初，返回这一行的确认结果。"""
    import_id = preview(client, caller, [{"sku_id": sku_id, "quantity": quantity}])["import_id"]
    [row] = confirm(client, caller, import_id)["result"]
    assert row["outcome"] == "created"
    return row


def run_service(client: TestClient, caller: Caller, command, sku_ids: list[str], status: int):
    """经测试专用写路由运行一个服务层命令（预建 sku_ids 的余额行）。"""
    marker = new_id("cmd")
    COMMANDS[marker] = command
    return data_of(send(client, caller, body_of(caller, marker, sku_ids)), status)


def write_with_service(
    client: TestClient, caller: Caller, sku_id: str, entry_type: EntryType, deltas: tuple
) -> str:
    """经 append_entry 写一条流水（判定列照默认规则更新），返回它的流水 ID。"""

    def write(execution: Execution, body: StockBody) -> CommandResult:
        now = command_now(execution)
        balance = lock_balances(execution, body.pool_id, [sku_id])[sku_id]
        entry = append_entry(execution, balance, entry_type, deltas, new_command_id(), now=now)
        return CommandResult(entry, 201)

    return run_service(client, caller, write, [sku_id], 201)["entry_id"]


def verify_with_service(client: TestClient, caller: Caller, sku_id: str) -> None:
    """差额为 0、不写流水的核实：两个判定列设为当前 balance_version（§4.1）。"""

    def verify(execution: Execution, body: StockBody) -> CommandResult:
        mark_verified(execution, lock_balances(execution, body.pool_id, [sku_id])[sku_id])
        return CommandResult({})

    run_service(client, caller, verify, [sku_id], 200)


def merge_with_service(
    client: TestClient, engine: Engine, caller: Caller, source: str, into: str
) -> None:
    """照 §2.4 第 1 步写一对迁移流水（来源归零），再标记合并。"""

    def transfer(execution: Execution, body: StockBody) -> CommandResult:
        now = command_now(execution)
        balances = lock_balances(execution, body.pool_id, [source, into])
        moved = tuple(getattr(balances[source], name) for name in QUANTITIES)
        command_id = new_command_id()
        append_entry(
            execution,
            balances[source],
            EntryType.MERGE_TRANSFER_OUT,
            tuple(-value for value in moved),
            command_id,
            now=now,
        )
        append_entry(
            execution, balances[into], EntryType.MERGE_TRANSFER_IN, moved, command_id, now=now
        )
        return CommandResult({}, 201)

    run_service(client, caller, transfer, [source, into], 201)
    merge(engine, source, into)


# --- 成功 ---


def test_a_receipt_is_reversed_and_the_document_returned(client, engine, caller):
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    preset_balance(engine, caller, sku_id, stock_balance=4, balance_version=1)
    received = receive(client, caller, sku_id, 10)
    assert quantities_of(engine, caller, sku_id) == (14, 0, 0, 0)

    key, rid = new_key(), request_id()
    optional = {"note": "  wrong pallet  ", "occurred_at": "2026-11-15T10:00:00+08:00"}
    response = correct(client, caller, received, key=key, rid=rid, **optional)
    data = data_of(response, 201)

    assert set(data) == RESULT_KEYS
    assert data["document_id"].startswith("cor_")
    assert data["corrected_entry_id"] == received

    # 余额回到收货前；CORRECTION 只更新 last_book_only_version。
    assert quantities_of(engine, caller, sku_id) == (4, 0, 0, 0)
    assert judgement(engine, caller, sku_id) == (3, 3, 0, None)

    by_id = {row["entry_id"]: row for row in entries(engine, caller)}
    original, row = by_id[received], by_id[data["entry"]["entry_id"]]
    assert data["entry"] == entry_representation(row)
    assert row["entry_type"] == "CORRECTION"
    for name in QUANTITIES:
        assert row[f"{name}_delta"] == -original[f"{name}_delta"]
    assert data["entry"]["deltas"] == {
        "stock_balance": -10,
        "reserved": 0,
        "unavailable": 0,
        "paid_pending_dispatch": 0,
    }
    assert data["entry"]["after"] == {
        "stock_balance": 4,
        "reserved": 0,
        "unavailable": 0,
        "paid_pending_dispatch": 0,
    }
    assert row["balance_version_after"] == 3
    assert row["corrects_entry_id"] == received
    assert (row["source_type"], row["source_ref"]) == ("correction", received)
    assert row["line_ref"] is None
    assert (row["reason_code"], row["note"]) == ("wrong_entry", "wrong pallet")
    assert row["occurred_at"] == datetime(2026, 11, 15, 2, 0)
    assert (row["actor_ref"], row["actor_role"]) == ("clerk-3", STOCK_ADMIN)
    assert (row["idempotency_key"], row["request_id"]) == (key, rid)
    assert row["credential_id"] == caller.credential_id

    # 单据一行，document_ref 是原 entry_id，result 就是响应的 data。
    _, [document] = corrections_of(engine, caller)
    assert document["document_id"] == data["document_id"]
    assert document["document_ref"] == received
    assert document["result"] == data
    assert document["pool_id"] == caller.pool_id
    assert document["request_id"] == rid
    assert format_timestamp(document["created_at"]) == data["entry"]["recorded_at"]
    [record] = correction_records(engine, caller)
    assert record["request_hash"] == document["request_hash"]
    assert events(engine, rid) == []

    # 同键：原样重放。
    replay = correct(client, caller, received, key=key, **optional)
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == response.json()


def test_a_correction_without_optional_fields_uses_the_database_time(client, engine, caller):
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    data = data_of(correct(client, caller, receive(client, caller, sku_id, 2)), 201)
    assert data["entry"]["note"] is None
    assert data["entry"]["occurred_at"] == data["entry"]["recorded_at"]


def test_adjustments_recorded_the_wrong_way_round_are_both_corrected(client, engine, caller):
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    preset_balance(engine, caller, sku_id, stock_balance=10)
    marked = adjust(client, caller, "mark_unavailable", sku_id, 2)
    restored = adjust(client, caller, "restore_available", sku_id, 2)
    assert quantities_of(engine, caller, sku_id) == (10, 0, 0, 0)

    first = data_of(correct(client, caller, restored), 201)
    assert first["entry"]["deltas"]["unavailable"] == 2
    assert quantities_of(engine, caller, sku_id) == (10, 0, 2, 0)
    second = data_of(correct(client, caller, marked), 201)
    assert second["entry"]["deltas"]["unavailable"] == -2
    assert quantities_of(engine, caller, sku_id) == (10, 0, 0, 0)

    # 报损的更正把两个量都加回来。
    adjust(client, caller, "mark_unavailable", sku_id, 3)
    written_off = adjust(client, caller, "write_off", sku_id, 3)
    assert quantities_of(engine, caller, sku_id) == (7, 0, 0, 0)
    deltas = data_of(correct(client, caller, written_off), 201)["entry"]["deltas"]
    assert (deltas["stock_balance"], deltas["unavailable"]) == (3, 3)
    assert quantities_of(engine, caller, sku_id) == (10, 0, 3, 0)


def test_a_wrong_opening_is_corrected_and_can_be_imported_again(client, engine):
    caller = issue(engine)  # 池在 initializing
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    opening = open_with(client, caller, sku_id, 9)["entry_id"]
    assert judgement(engine, caller, sku_id) == (1, 1, 1, opening)

    data = data_of(correct(client, caller, opening), 201)
    assert data["entry"]["deltas"]["stock_balance"] == -9
    assert data["entry"]["corrects_entry_id"] == opening
    # 有效期初被冲掉：opening_entry_id 清空，last_reconciled_version 不变。
    assert quantities_of(engine, caller, sku_id) == (0, 0, 0, 0)
    assert judgement(engine, caller, sku_id) == (2, 2, 1, None)

    # 用新的导入按正确的数重导。
    reopened = open_with(client, caller, sku_id, 7)
    assert reopened["preview_result"] == "ok"
    assert quantities_of(engine, caller, sku_id) == (7, 0, 0, 0)
    assert judgement(engine, caller, sku_id) == (3, 3, 3, reopened["entry_id"])

    # 重导的期初的判定列是它自己设的，不算被吸收，照样能更正。
    data_of(correct(client, caller, reopened["entry_id"]), 201)
    assert judgement(engine, caller, sku_id) == (4, 4, 3, None)


# --- 类型与池状态 ---


def test_entries_of_other_types_are_not_correctable(client, engine, caller):
    receipt_sku, count_sku, reserve_sku = add_skus(engine, caller.tenant_id, 3)
    sku_ids = [receipt_sku, count_sku, reserve_sku]

    # 更正本身。
    correction = data_of(correct(client, caller, receive(client, caller, receipt_sku, 3)), 201)
    # 盘点差异（经服务层写）。
    receive(client, caller, count_sku, 5)
    counted = write_with_service(
        client, caller, count_sku, EntryType.COUNT_ADJUSTMENT, (-1, 0, 0, 0)
    )
    # 预留（直接造流水）。
    preset_balance(engine, caller, reserve_sku, stock_balance=5, reserved=1, balance_version=1)
    with engine.begin() as db:
        reserved = add_entry(
            db,
            (caller.tenant_id, caller.pool_id, reserve_sku),
            1,
            entry_type="RESERVE",
            stock_balance_delta=0,
            reserved_delta=1,
            reserved_after=1,
        )

    before = snapshot(engine, caller, sku_ids)
    for target in (correction["entry"]["entry_id"], counted, reserved):
        assert not_correctable(correct(client, caller, target)) == {"reason": "entry_type"}
    assert snapshot(engine, caller, sku_ids) == before


def test_a_correction_cannot_be_corrected_on_an_initializing_pool_either(client, engine):
    """池状态不限：initializing 的池上照样按类型判定。"""
    caller = issue(engine)
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    opening = open_with(client, caller, sku_id, 4)["entry_id"]
    data = data_of(correct(client, caller, opening), 201)
    details = not_correctable(correct(client, caller, data["entry"]["entry_id"]))
    assert details == {"reason": "entry_type"}


def test_an_opening_cannot_be_corrected_once_the_pool_is_live(client, engine):
    caller = issue(engine)
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    opening = open_with(client, caller, sku_id, 4)["entry_id"]
    set_live(engine, caller)
    assert not_correctable(correct(client, caller, opening)) == {"reason": "pool_live"}
    assert quantities_of(engine, caller, sku_id) == (4, 0, 0, 0)
    assert judgement(engine, caller, sku_id) == (1, 1, 1, opening)


def test_an_earlier_receipt_on_a_merged_sku_is_superseded(client, engine, caller):
    source, canonical = add_skus(engine, caller.tenant_id, 2)
    received = receive(client, caller, source, 6)  # 来源 SKU 的版本 1
    receive(client, caller, canonical, 2)
    merge_with_service(client, engine, caller, source, canonical)
    assert quantities_of(engine, caller, source) == (0, 0, 0, 0)
    assert quantities_of(engine, caller, canonical) == (8, 0, 0, 0)
    assert judgement(engine, caller, source) == (2, 2, 2, None)
    before = snapshot(engine, caller, [source, canonical])

    details = not_correctable(correct(client, caller, received))
    assert details == {"reason": "superseded", "last_reconciled_version": 2}
    assert snapshot(engine, caller, [source, canonical]) == before


# --- 已被吸收 ---


def test_a_receipt_before_a_count_adjustment_is_superseded(client, engine, caller):
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    received = receive(client, caller, sku_id, 10)
    write_with_service(client, caller, sku_id, EntryType.COUNT_ADJUSTMENT, (-2, 0, 0, 0))
    before = snapshot(engine, caller, [sku_id])

    details = not_correctable(correct(client, caller, received))
    assert details == {"reason": "superseded", "last_reconciled_version": 2}
    assert snapshot(engine, caller, [sku_id]) == before


def test_two_wrong_receipts_on_one_row_are_each_corrected(client, engine, caller):
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    first = receive(client, caller, sku_id, 10)
    second = receive(client, caller, sku_id, 10)

    data_of(correct(client, caller, second), 201)
    # 更正不核实账面：last_reconciled_version 不变，前一笔照样能更正（0.10）。
    assert judgement(engine, caller, sku_id) == (3, 3, 0, None)
    data_of(correct(client, caller, first), 201)
    assert quantities_of(engine, caller, sku_id) == (0, 0, 0, 0)
    assert judgement(engine, caller, sku_id) == (4, 4, 0, None)


def test_receipts_verified_by_a_zero_difference_are_superseded(client, engine, caller):
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    first = receive(client, caller, sku_id, 10)
    second = receive(client, caller, sku_id, 10)
    verify_with_service(client, caller, sku_id)
    assert judgement(engine, caller, sku_id) == (2, 2, 2, None)
    before = snapshot(engine, caller, [sku_id])

    for target in (second, first):
        details = not_correctable(correct(client, caller, target))
        assert details == {"reason": "superseded", "last_reconciled_version": 2}
    assert snapshot(engine, caller, [sku_id]) == before

    # 核实之后才录的收货不受影响。
    later = receive(client, caller, sku_id, 3)
    data_of(correct(client, caller, later), 201)
    assert quantities_of(engine, caller, sku_id) == (20, 0, 0, 0)


# --- 不变量 ---


def test_a_receipt_already_reserved_breaks_the_invariants(client, engine, caller):
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    preset_balance(engine, caller, sku_id, stock_balance=3, unavailable=1, balance_version=1)
    received = receive(client, caller, sku_id, 10)  # 13/0/1/0
    write_with_service(client, caller, sku_id, EntryType.RESERVE, (0, 8, 0, 0))  # 13/8/1/0
    before = snapshot(engine, caller, [sku_id])
    judged = judgement(engine, caller, sku_id)

    error = error_of(correct(client, caller, received), 409)
    assert error["code"] == "CORRECTION_BREAKS_INVARIANTS"
    # 落账后 3/8/1/0，缺口 = 8 + 1 − 3。
    assert error["details"] == {
        "sku_id": sku_id,
        "stock_balance": 3,
        "reserved": 8,
        "unavailable": 1,
        "paid_pending_dispatch": 0,
        "shortfall": 6,
    }
    # 什么也没写。
    assert snapshot(engine, caller, [sku_id]) == before
    assert judgement(engine, caller, sku_id) == judged


# --- 业务引用与并发 ---


def test_a_correction_is_applied_once_whatever_key_reports_it(client, engine, caller):
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    received = receive(client, caller, sku_id, 5)
    data = data_of(correct(client, caller, received, note="first"), 201)
    applied = snapshot(engine, caller, [sku_id])

    # 换键同内容：200 与原结果（账面已不够再反向一次，也不是 409）。
    rid = request_id()
    again = correct(client, caller, received, note="first", rid=rid)
    assert "Idempotency-Replayed" not in again.headers
    assert data_of(again, 200) == data
    assert events(engine, rid) == []

    # 换键不同内容：already_corrected，带已有的单据，不记 business_ref_conflict。
    rid = request_id()
    details = not_correctable(correct(client, caller, received, note="second", rid=rid))
    assert details == {"reason": "already_corrected", "document_id": data["document_id"]}
    assert events(engine, rid) == []

    assert snapshot(engine, caller, [sku_id]) == applied
    assert quantities_of(engine, caller, sku_id) == (0, 0, 0, 0)


@pytest.mark.parametrize("same_content", [True, False])
@pytest.mark.parametrize("headroom", [True, False])
def test_concurrent_corrections_of_one_entry_apply_once(
    client, engine, caller, monkeypatch, same_content, headroom
):
    """headroom 为真时反向两次账面仍合法，后到者撞上流水表的唯一约束；
    为假时后到者锁行后反向不合法而被拒。两种都在新事务里重新判定，不得 superseded。
    """
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    if headroom:
        preset_balance(engine, caller, sku_id, stock_balance=20, balance_version=1)
    received = receive(client, caller, sku_id, 4)
    late_rid = request_id()
    late_note = "early" if same_content else "late"

    # late 在锁余额行之前停住（读快照已建立，里面还没有单据）；early 锁住余额行后停住。
    # 放行 late，它在 early 持有的行锁上等待；再放行 early 提交。
    before_lock, holding_lock = Gate(), Gate()
    locked = stock_document_service.lock_balances

    def gated(execution, pool_id, sku_ids):
        late = execution.request.state.request_id == late_rid
        if late:
            before_lock.pass_through()
        balances = locked(execution, pool_id, sku_ids)
        if not late:
            holding_lock.pass_through()
        return balances

    monkeypatch.setattr(stock_document_service, "lock_balances", gated)
    other_client = TestClient(client.app)
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            late = pool.submit(correct, client, caller, received, note=late_note, rid=late_rid)
            assert before_lock.reached.wait(10)
            early = pool.submit(correct, other_client, caller, received, note="early")
            assert holding_lock.reached.wait(10)
            before_lock.proceed.set()
            time.sleep(0.5)
        finally:
            before_lock.proceed.set()
            holding_lock.proceed.set()
        early_data = data_of(early.result(), 201)
        late_response = late.result()

    if same_content:
        assert data_of(late_response, 200) == early_data
    else:
        details = not_correctable(late_response)
        assert details == {"reason": "already_corrected", "document_id": early_data["document_id"]}
    assert events(engine, late_rid) == []

    # 只有一条 CORRECTION 生效；late 的结果也作为它那个键的确定结果保存。
    written, kept = corrections_of(engine, caller)
    assert [row["entry_id"] for row in written] == [early_data["entry"]["entry_id"]]
    assert [row["document_id"] for row in kept] == [early_data["document_id"]]
    expected = (20, 0, 0, 0) if headroom else (0, 0, 0, 0)
    assert quantities_of(engine, caller, sku_id) == expected
    assert len(correction_records(engine, caller)) == 2


# --- 正文与授权 ---


@pytest.mark.parametrize(
    "extra",
    [
        {"unknown": "x"},
        {"reason_code": None},
        {"reason_code": "Wrong"},
        {"note": "   "},
        {"occurred_at": "2026-11-15T10:00:00"},
        {"entry_id": 5},
    ],
)
def test_a_bad_correction_body_is_422(client, engine, caller, extra):
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    received = receive(client, caller, sku_id, 5)
    response = correct(client, caller, received, **extra)
    assert error_of(response, 422)["code"] == "INVALID_REQUEST"
    # 第 7 步的拒绝不进幂等、不保存。
    assert correction_records(engine, caller) == []
    assert quantities_of(engine, caller, sku_id) == (5, 0, 0, 0)


def test_a_correction_without_a_reason_code_is_422(client, engine, caller):
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    body = {"entry_id": receive(client, caller, sku_id, 5)}
    response = post(client, caller, CORRECTIONS, body, role=STOCK_ADMIN)
    assert error_of(response, 422)["code"] == "INVALID_REQUEST"
    assert correction_records(engine, caller) == []


def test_corrections_need_stock_admin(client, engine, caller):
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    received = receive(client, caller, sku_id, 5)
    rid = request_id()
    response = correct(client, caller, received, role=STOCK_OPERATOR, rid=rid)
    assert error_of(response, 403)["code"] == "FORBIDDEN"
    [event] = events(engine, rid)
    assert event["details"] == {"category": "role", "role": STOCK_OPERATOR}
    assert quantities_of(engine, caller, sku_id) == (5, 0, 0, 0)
    assert corrections_of(engine, caller) == ([], [])
    assert correction_records(engine, caller) == []


def test_a_missing_and_a_foreign_entry_get_the_same_404(client, engine, caller):
    foreign = issue(engine)
    [foreign_sku] = add_skus(engine, foreign.tenant_id, 1)
    preset_balance(engine, foreign, foreign_sku, stock_balance=5, balance_version=1)
    with engine.begin() as db:
        foreign_entry = add_entry(db, (foreign.tenant_id, foreign.pool_id, foreign_sku), 1)

    missing_rid, foreign_rid = request_id(), request_id()
    missing = error_of(correct(client, caller, new_id("ent"), rid=missing_rid), 404)
    elsewhere = error_of(correct(client, caller, foreign_entry, rid=foreign_rid), 404)
    assert missing["code"] == "ENTRY_NOT_FOUND"
    assert elsewhere == missing

    assert events(engine, missing_rid) == []
    [event] = events(engine, foreign_rid)
    assert event["event_type"] == "cross_tenant"
    assert event["details"] == {"object_type": "entry", "id": foreign_entry}
    assert event["tenant_id"] == caller.tenant_id

    # 池授权的 404 在取锁之前判出：不保存，什么也不写。
    assert idempotency_records(engine, caller) == []
    assert documents(engine, caller) == [] and documents(engine, foreign) == []
    assert quantities_of(engine, foreign, foreign_sku) == (5, 0, 0, 0)
    assert len(entries(engine, foreign)) == 1


def test_an_entry_in_another_pool_of_the_tenant_is_403(client, engine, caller):
    other_pool = add_other_pool(engine, caller.tenant_id)
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    other = Caller(caller.tenant_id, other_pool, caller.credential_id, caller.token)
    preset_balance(engine, other, sku_id, stock_balance=5, balance_version=1)
    with engine.begin() as db:
        target = add_entry(db, (caller.tenant_id, other_pool, sku_id), 1)

    rid = request_id()
    assert error_of(correct(client, caller, target, rid=rid), 403)["code"] == "FORBIDDEN"
    [event] = events(engine, rid)
    assert event["details"] == {
        "category": "pool",
        "pool_id": other_pool,
        "object_type": "entry",
        "object_id": target,
    }
    assert idempotency_records(engine, caller) == []
    assert corrections_of(engine, caller) == ([], [])
