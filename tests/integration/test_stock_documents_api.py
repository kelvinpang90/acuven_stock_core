"""收货与不可售调整（契约 §6.2、§6.4、§6.5、§7.1、§7.2、§10 第 3、4 条）。

在真 MySQL 上跑；每个测试建自己的租户、池与凭据，SKU 与余额行由测试直接在库里造（自己提交的连接）。
池默认先切到 live。并发的测试在服务层的 lock_balances 之后放 Gate，
让先到的请求持有余额行锁时后到的请求发出、在锁上等待。
"""

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, update

from app.config import Settings
from app.domain.requests import CATALOG_ADMIN, STOCK_ADMIN, STOCK_OPERATOR
from app.main import create_app
from app.models.catalog import Sku
from app.models.inventory import IdempotencyRecord
from app.services import stock_documents as stock_document_service
from app.services.stock import format_timestamp
from tests.integration.conftest import TEST_DATABASE_URL, balance_insert, new_id
from tests.integration.test_api_auth import Caller, error_of, events, issue
from tests.integration.test_catalog_create_api import created
from tests.integration.test_catalog_queries_api import data_of
from tests.integration.test_idempotency import add_skus
from tests.integration.test_stock_imports_api import merge, set_live
from tests.integration.test_stock_service import Gate, documents, entries, quantities_of

RECEIPTS = "/v1/receipts"
ADJUSTMENTS = "/v1/unavailable-adjustments"
ACTOR_REF = "clerk-3"
HEADER_KEYS = {
    "document_id",
    "pool_id",
    "reason_code",
    "note",
    "occurred_at",
    "created_at",
    "lines",
}
RECEIPT_KEYS = HEADER_KEYS | {"receipt_ref"}
ADJUSTMENT_KEYS = HEADER_KEYS | {"adjustment_ref", "action"}
LINE_KEYS = {
    "line_ref",
    "sku_id",
    "quantity",
    "entry_id",
    "stock_balance",
    "reserved",
    "unavailable",
    "paid_pending_dispatch",
    "available",
    "balance_version",
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
    """池已 live 的调用方。"""
    issued = issue(engine)
    set_live(engine, issued)
    return issued


# --- 请求 ---


def new_key() -> str:
    return f"shop:stock-document:{uuid4().hex}"


def request_id() -> str:
    return f"stock-document-test-{uuid4().hex}"


def new_ref() -> str:
    return f"ref-{uuid4().hex}"


def write_headers(
    caller: Caller, key: str | None = None, rid: str | None = None, role: str = STOCK_OPERATOR
) -> dict:
    extra = {"Idempotency-Key": key or new_key(), "X-Request-ID": rid or request_id()}
    return caller.auth | {"X-Actor-Ref": ACTOR_REF, "X-Actor-Role": role} | extra


def line(line_ref: str, sku_id: str, quantity: object) -> dict:
    return {"line_ref": line_ref, "sku_id": sku_id, "quantity": quantity}


def receipt_body(caller: Caller, lines: list, ref: str | None = None, **extra: object) -> dict:
    return {"pool_id": caller.pool_id, "receipt_ref": ref or new_ref(), "lines": lines} | extra


def adjustment_body(
    caller: Caller, action: str, lines: list, ref: str | None = None, **extra: object
) -> dict:
    body = {
        "pool_id": caller.pool_id,
        "adjustment_ref": ref or new_ref(),
        "action": action,
        "reason_code": "damaged",
        "lines": lines,
    }
    return body | extra


def post(client: TestClient, caller: Caller, url: str, body: dict, **headers):
    return client.post(url, json=body, headers=write_headers(caller, **headers))


# --- 造数据与观察 ---


def preset_balance(engine: Engine, caller: Caller, sku_id: str, **values: object) -> None:
    with engine.begin() as db:
        db.execute(balance_insert(caller.tenant_id, caller.pool_id, sku_id, **values))


def deactivate(engine: Engine, sku_id: str) -> None:
    with engine.begin() as db:
        db.execute(update(Sku).where(Sku.sku_id == sku_id).values(status="inactive"))


def idempotency_records(engine: Engine, caller: Caller) -> list[dict]:
    columns = IdempotencyRecord.__table__.columns
    statement = select(*columns).where(IdempotencyRecord.tenant_id == caller.tenant_id)
    with engine.connect() as db:
        return [dict(row) for row in db.execute(statement).mappings()]


def snapshot(engine: Engine, caller: Caller, sku_ids: list[str]) -> tuple:
    """流水、单据与各余额行的四个量。"""
    balances = [quantities_of(engine, caller, sku_id) for sku_id in sku_ids]
    return entries(engine, caller), documents(engine, caller), balances


# --- 收货 ---


def test_a_receipt_books_each_line_and_returns_the_document(client, engine, caller):
    stocked, inactive = add_skus(engine, caller.tenant_id, 2)
    deactivate(engine, inactive)  # 停用的 SKU 照收
    preset_balance(
        engine, caller, stocked, stock_balance=4, reserved=1, unavailable=1, balance_version=2
    )
    ref, key, rid = new_ref(), new_key(), request_id()
    lines = [line("L-2", inactive, 7), line("L-1", stocked, 5)]
    body = receipt_body(
        caller,
        lines,
        ref,
        reason_code="supplier_delivery",
        note="  pallet 3  ",
        occurred_at="2026-11-15T10:00:00+08:00",
    )
    data = created(post(client, caller, RECEIPTS, body, key=key, rid=rid))

    assert set(data) == RECEIPT_KEYS
    assert data["document_id"].startswith("rcp_")
    assert (data["receipt_ref"], data["pool_id"]) == (ref, caller.pool_id)
    assert (data["reason_code"], data["note"]) == ("supplier_delivery", "pallet 3")
    assert data["occurred_at"] == "2026-11-15T02:00:00.000000Z"

    by_sku = {entry["sku_id"]: entry for entry in entries(engine, caller)}
    assert len(by_sku) == 2
    # 各行按请求顺序，带写后的四个量、available 与版本。
    assert [set(row) for row in data["lines"]] == [LINE_KEYS, LINE_KEYS]
    assert data["lines"] == [
        {
            "line_ref": "L-2",
            "sku_id": inactive,
            "quantity": 7,
            "entry_id": by_sku[inactive]["entry_id"],
            "stock_balance": 7,
            "reserved": 0,
            "unavailable": 0,
            "paid_pending_dispatch": 0,
            "available": 7,
            "balance_version": 1,
        },
        {
            "line_ref": "L-1",
            "sku_id": stocked,
            "quantity": 5,
            "entry_id": by_sku[stocked]["entry_id"],
            "stock_balance": 9,
            "reserved": 1,
            "unavailable": 1,
            "paid_pending_dispatch": 0,
            "available": 7,
            "balance_version": 3,
        },
    ]
    assert quantities_of(engine, caller, inactive) == (7, 0, 0, 0)
    assert quantities_of(engine, caller, stocked) == (9, 1, 1, 0)

    # 每行一条 RECEIPT，同一命令的流水共用 command_id。
    assert len({entry["command_id"] for entry in by_sku.values()}) == 1
    for sent in lines:
        entry = by_sku[sent["sku_id"]]
        assert entry["entry_type"] == "RECEIPT"
        assert entry["stock_balance_delta"] == sent["quantity"]
        deltas = (entry["reserved_delta"], entry["unavailable_delta"])
        assert deltas == (0, 0) and entry["paid_pending_dispatch_delta"] == 0
        assert (entry["source_type"], entry["source_ref"]) == ("receipt", ref)
        assert entry["line_ref"] == sent["line_ref"]
        assert (entry["reason_code"], entry["note"]) == ("supplier_delivery", "pallet 3")
        assert entry["occurred_at"] == datetime(2026, 11, 15, 2, 0)
        assert format_timestamp(entry["recorded_at"]) == data["created_at"]
        assert (entry["actor_ref"], entry["actor_role"]) == (ACTOR_REF, STOCK_OPERATOR)
        assert (entry["idempotency_key"], entry["request_id"]) == (key, rid)
        assert entry["credential_id"] == caller.credential_id

    # 单据一行，result 就是响应的 data。
    [document] = documents(engine, caller)
    assert document["document_id"] == data["document_id"]
    assert (document["document_type"], document["document_ref"]) == ("receipt", ref)
    assert document["pool_id"] == caller.pool_id
    assert document["result"] == data
    assert document["credential_id"] == caller.credential_id
    assert (document["actor_ref"], document["actor_role"]) == (ACTOR_REF, STOCK_OPERATOR)
    assert document["request_id"] == rid
    assert format_timestamp(document["created_at"]) == data["created_at"]
    [record] = idempotency_records(engine, caller)
    assert record["request_hash"] == document["request_hash"]


def test_a_receipt_without_optional_fields_uses_the_database_time(client, engine, caller):
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    data = created(post(client, caller, RECEIPTS, receipt_body(caller, [line("L", sku_id, 1)])))
    assert (data["reason_code"], data["note"]) == (None, None)
    assert data["occurred_at"] == data["created_at"]
    [entry] = entries(engine, caller)
    assert (entry["reason_code"], entry["note"]) == (None, None)
    assert entry["occurred_at"] == entry["recorded_at"]


def test_a_receipt_is_booked_once_whatever_key_reports_it(client, engine, caller):
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    ref, key = new_ref(), new_key()
    lines = [line("A", sku_id, 3)]
    body = receipt_body(caller, lines, ref, note="first")
    first = post(client, caller, RECEIPTS, body, key=key)
    data = created(first)
    booked = snapshot(engine, caller, [sku_id])

    # 同键：原样重放。
    replay = post(client, caller, RECEIPTS, body, key=key)
    assert replay.status_code == 201
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == first.json()

    # 换键同内容：200 与原结果，不再入库。
    again = post(client, caller, RECEIPTS, body)
    assert "Idempotency-Replayed" not in again.headers
    assert data_of(again) == data

    # 换键不同内容：409 BUSINESS_REF_CONFLICT，记事件。
    rid = request_id()
    changed = receipt_body(caller, lines, ref, note="second")
    error = error_of(post(client, caller, RECEIPTS, changed, rid=rid), 409)
    assert error["code"] == "BUSINESS_REF_CONFLICT"
    [event] = events(engine, rid)
    assert event["event_type"] == "business_ref_conflict"
    assert event["details"] == {"ref_type": "receipt", "ref": ref}

    assert snapshot(engine, caller, [sku_id]) == booked
    assert quantities_of(engine, caller, sku_id) == (3, 0, 0, 0)


def test_lines_in_another_order_are_the_same_content(client, engine, caller):
    first_sku, second_sku = add_skus(engine, caller.tenant_id, 2)
    ref = new_ref()
    lines = [line("A", first_sku, 1), line("B", second_sku, 2)]
    data = created(post(client, caller, RECEIPTS, receipt_body(caller, lines, ref)))
    reordered = receipt_body(caller, lines[::-1], ref)
    assert data_of(post(client, caller, RECEIPTS, reordered)) == data
    assert len(entries(engine, caller)) == 2


def test_a_receipt_on_an_initializing_pool_is_409(client, engine):
    caller = issue(engine)
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    response = post(client, caller, RECEIPTS, receipt_body(caller, [line("L", sku_id, 1)]))
    error = error_of(response, 409)
    assert error["code"] == "POOL_NOT_LIVE"
    assert error["details"] == {"pool_status": "initializing"}
    assert entries(engine, caller) == [] and documents(engine, caller) == []


# --- SKU 与正文校验 ---


def test_missing_and_foreign_skus_are_404_together(client, engine, caller):
    foreign = issue(engine)
    [known] = add_skus(engine, caller.tenant_id, 1)
    [foreign_sku] = add_skus(engine, foreign.tenant_id, 1)
    missing = new_id("sku")
    lines = [line("A", foreign_sku, 1), line("B", known, 1), line("C", missing, 1)]
    rid = request_id()
    response = post(client, caller, RECEIPTS, receipt_body(caller, lines), rid=rid)
    error = error_of(response, 404)
    assert error["code"] == "SKU_NOT_FOUND"
    assert error["details"] == {"sku_ids": [foreign_sku, missing]}
    [event] = events(engine, rid)
    assert event["event_type"] == "cross_tenant"
    assert event["details"] == {"object_type": "sku", "id": foreign_sku}
    assert entries(engine, caller) == [] and documents(engine, caller) == []
    assert entries(engine, foreign) == []


def test_a_merged_sku_is_409(client, engine, caller):
    merged, canonical = add_skus(engine, caller.tenant_id, 2)
    merge(engine, merged, canonical)
    lines = [line("A", canonical, 1), line("B", merged, 1)]
    error = error_of(post(client, caller, RECEIPTS, receipt_body(caller, lines)), 409)
    assert error["code"] == "SKU_MERGED"
    assert error["details"] == {"sku_id": merged, "merged_into_sku_id": canonical}
    assert entries(engine, caller) == [] and documents(engine, caller) == []


def bad_receipt(case: str, caller: Caller, sku_id: str, other: str) -> dict:
    good = [line("A", sku_id, 1)]
    return {
        "duplicate_line_ref": receipt_body(caller, [line("A", sku_id, 1), line("A", other, 1)]),
        "duplicate_sku_id": receipt_body(caller, [line("A", sku_id, 1), line("B", sku_id, 1)]),
        "zero_quantity": receipt_body(caller, [line("A", sku_id, 0)]),
        "too_many": receipt_body(caller, [line("A", sku_id, 1_000_001)]),
        "string_quantity": receipt_body(caller, [line("A", sku_id, "5")]),
        "bool_quantity": receipt_body(caller, [line("A", sku_id, True)]),
        "unknown_field": receipt_body(caller, good, extra="x"),
        "unknown_line_field": receipt_body(caller, [line("A", sku_id, 1) | {"note": "x"}]),
        "no_lines": receipt_body(caller, []),
        "bad_receipt_ref": receipt_body(caller, good, "has space"),
        "bad_line_ref": receipt_body(caller, [line("a/b", sku_id, 1)]),
        "bad_reason_code": receipt_body(caller, good, reason_code="Damaged"),
        "null_reason_code": receipt_body(caller, good, reason_code=None),
        "blank_note": receipt_body(caller, good, note="   "),
        "bad_occurred_at": receipt_body(caller, good, occurred_at="2026-11-15T10:00:00"),
    }[case]


@pytest.mark.parametrize(
    "case",
    [
        "duplicate_line_ref",
        "duplicate_sku_id",
        "zero_quantity",
        "too_many",
        "string_quantity",
        "bool_quantity",
        "unknown_field",
        "unknown_line_field",
        "no_lines",
        "bad_receipt_ref",
        "bad_line_ref",
        "bad_reason_code",
        "null_reason_code",
        "blank_note",
        "bad_occurred_at",
    ],
)
def test_a_bad_receipt_body_is_422(client, engine, caller, case):
    sku_id, other = add_skus(engine, caller.tenant_id, 2)
    response = post(client, caller, RECEIPTS, bad_receipt(case, caller, sku_id, other))
    assert error_of(response, 422)["code"] == "INVALID_REQUEST"
    # 第 7 步的拒绝不进幂等、不保存。
    assert idempotency_records(engine, caller) == []
    assert entries(engine, caller) == [] and documents(engine, caller) == []


def test_a_million_is_accepted_and_101_lines_are_413(client, engine, caller):
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    body = receipt_body(caller, [line("A", sku_id, 1_000_000)])
    data = created(post(client, caller, RECEIPTS, body))
    assert data["lines"][0]["stock_balance"] == 1_000_000

    lines = [line(f"L{index}", new_id("sku"), 1) for index in range(101)]
    response = post(client, caller, RECEIPTS, receipt_body(caller, lines))
    assert error_of(response, 413)["code"] == "REQUEST_TOO_LARGE"
    lines = [line(f"L{index}", new_id("sku"), 1) for index in range(101)]
    body = adjustment_body(caller, "mark_unavailable", lines)
    response = post(client, caller, ADJUSTMENTS, body)
    assert error_of(response, 413)["code"] == "REQUEST_TOO_LARGE"
    assert len(entries(engine, caller)) == 1


# --- 不可售调整 ---


def test_mark_unavailable_up_to_available_and_reject_every_line_beyond(client, engine, caller):
    exact, over, far_over = add_skus(engine, caller.tenant_id, 3)
    # available 分别是 10 − 3 − 2 = 5、4、0。
    preset_balance(
        engine, caller, exact, stock_balance=10, reserved=3, unavailable=2, balance_version=1
    )
    preset_balance(engine, caller, over, stock_balance=4, balance_version=1)
    preset_balance(engine, caller, far_over, stock_balance=2, unavailable=2, balance_version=1)
    sku_ids = [exact, over, far_over]
    before = snapshot(engine, caller, sku_ids)

    lines = [line("A", exact, 5), line("B", over, 5), line("C", far_over, 1)]
    response = post(client, caller, ADJUSTMENTS, adjustment_body(caller, "mark_unavailable", lines))
    error = error_of(response, 409)
    assert error["code"] == "INSUFFICIENT_STOCK"
    assert error["details"] == {
        "lines": [
            {"line_ref": "B", "sku_id": over, "quantity": 5, "limit": 4},
            {"line_ref": "C", "sku_id": far_over, "quantity": 1, "limit": 0},
        ]
    }
    # 被拒时什么也没写。
    assert snapshot(engine, caller, sku_ids) == before

    # 恰好等于上限通过。
    ref = new_ref()
    lines = [line("A", exact, 5), line("B", over, 4)]
    body = adjustment_body(caller, "mark_unavailable", lines, ref, note="water")
    data = created(post(client, caller, ADJUSTMENTS, body))
    assert set(data) == ADJUSTMENT_KEYS
    assert data["document_id"].startswith("adj_")
    assert (data["adjustment_ref"], data["action"]) == (ref, "mark_unavailable")
    assert (data["reason_code"], data["note"]) == ("damaged", "water")
    assert [(row["line_ref"], row["unavailable"], row["available"]) for row in data["lines"]] == [
        ("A", 7, 0),
        ("B", 4, 0),
    ]
    assert quantities_of(engine, caller, exact) == (10, 3, 7, 0)
    assert quantities_of(engine, caller, over) == (4, 0, 4, 0)

    written = entries(engine, caller)
    assert {entry["entry_type"] for entry in written} == {"MARK_UNAVAILABLE"}
    assert len({entry["command_id"] for entry in written}) == 1
    for entry in written:
        assert (entry["source_type"], entry["source_ref"]) == ("unavailable_adjustment", ref)
        assert entry["stock_balance_delta"] == 0
        assert entry["reason_code"] == "damaged"
    [document] = documents(engine, caller)
    assert (document["document_type"], document["document_ref"]) == ("unavailable_adjustment", ref)
    assert document["result"] == data


@pytest.mark.parametrize("action", ["restore_available", "write_off"])
def test_restore_and_write_off_are_limited_by_unavailable(client, engine, caller, action):
    exact, over = add_skus(engine, caller.tenant_id, 2)
    preset_balance(
        engine, caller, exact, stock_balance=10, reserved=2, unavailable=3, balance_version=1
    )
    preset_balance(engine, caller, over, stock_balance=9, unavailable=1, balance_version=1)
    sku_ids = [exact, over]
    before = snapshot(engine, caller, sku_ids)

    lines = [line("A", exact, 4), line("B", over, 2)]
    body = adjustment_body(caller, action, lines)
    error = error_of(post(client, caller, ADJUSTMENTS, body, role=STOCK_ADMIN), 409)
    assert error["code"] == "QUANTITY_EXCEEDS_UNAVAILABLE"
    assert error["details"] == {
        "lines": [
            {"line_ref": "A", "sku_id": exact, "quantity": 4, "limit": 3},
            {"line_ref": "B", "sku_id": over, "quantity": 2, "limit": 1},
        ]
    }
    assert snapshot(engine, caller, sku_ids) == before

    # 恰好等于上限通过。
    lines = [line("A", exact, 3), line("B", over, 1)]
    body = adjustment_body(caller, action, lines)
    data = created(post(client, caller, ADJUSTMENTS, body, role=STOCK_ADMIN))
    assert data["action"] == action
    entry_type = {"restore_available": "RESTORE_AVAILABLE", "write_off": "WRITE_OFF"}[action]
    assert {entry["entry_type"] for entry in entries(engine, caller)} == {entry_type}
    if action == "restore_available":
        assert quantities_of(engine, caller, exact) == (10, 2, 0, 0)
        assert quantities_of(engine, caller, over) == (9, 0, 0, 0)
        assert [row["available"] for row in data["lines"]] == [8, 9]
    else:
        # 报损同时减 stock_balance 与 unavailable，available 不变。
        assert quantities_of(engine, caller, exact) == (7, 2, 0, 0)
        assert quantities_of(engine, caller, over) == (8, 0, 0, 0)
        assert [row["available"] for row in data["lines"]] == [5, 8]


def test_write_off_needs_stock_admin_and_the_others_accept_stock_operator(client, engine, caller):
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    preset_balance(engine, caller, sku_id, stock_balance=10, balance_version=1)

    rid = request_id()
    body = adjustment_body(caller, "write_off", [line("A", sku_id, 1)])
    response = post(client, caller, ADJUSTMENTS, body, rid=rid, role=STOCK_OPERATOR)
    assert error_of(response, 403)["code"] == "FORBIDDEN"
    [event] = events(engine, rid)
    assert event["details"] == {"category": "role", "role": STOCK_OPERATOR}
    assert entries(engine, caller) == []

    for action in ("mark_unavailable", "restore_available"):
        body = adjustment_body(caller, action, [line("A", sku_id, 2)])
        created(post(client, caller, ADJUSTMENTS, body, role=STOCK_OPERATOR))
        error = error_of(post(client, caller, ADJUSTMENTS, body, role=CATALOG_ADMIN), 403)
        assert error["code"] == "FORBIDDEN"
    body = adjustment_body(caller, "mark_unavailable", [line("A", sku_id, 1)])
    created(post(client, caller, ADJUSTMENTS, body, role=STOCK_ADMIN))
    body = adjustment_body(caller, "write_off", [line("A", sku_id, 1)])
    created(post(client, caller, ADJUSTMENTS, body, role=STOCK_ADMIN))
    assert quantities_of(engine, caller, sku_id) == (9, 0, 0, 0)

    # 没带操作者头 400。
    headers = write_headers(caller)
    del headers["X-Actor-Ref"], headers["X-Actor-Role"]
    response = client.post(ADJUSTMENTS, json=body, headers=headers)
    assert error_of(response, 400)["code"] == "INVALID_REQUEST"


def test_receipts_accept_stock_operator_and_stock_admin_only(client, engine, caller):
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    for role in (STOCK_OPERATOR, STOCK_ADMIN):
        body = receipt_body(caller, [line("A", sku_id, 1)])
        created(post(client, caller, RECEIPTS, body, role=role))
    rid = request_id()
    body = receipt_body(caller, [line("A", sku_id, 1)])
    response = post(client, caller, RECEIPTS, body, rid=rid, role=CATALOG_ADMIN)
    assert error_of(response, 403)["code"] == "FORBIDDEN"
    [event] = events(engine, rid)
    assert event["details"] == {"category": "role", "role": CATALOG_ADMIN}
    assert quantities_of(engine, caller, sku_id) == (2, 0, 0, 0)


def bad_adjustment(case: str, caller: Caller, sku_id: str) -> dict:
    body = adjustment_body(caller, "mark_unavailable", [line("A", sku_id, 1)])
    if case == "no_reason_code":
        del body["reason_code"]
    elif case == "null_reason_code":
        body["reason_code"] = None
    elif case == "unknown_action":
        body["action"] = "destroy"
    elif case == "no_action":
        del body["action"]
    elif case == "bad_adjustment_ref":
        body["adjustment_ref"] = ""
    elif case == "unknown_field":
        body["receipt_ref"] = "R-1"
    return body


@pytest.mark.parametrize(
    "case",
    [
        "no_reason_code",
        "null_reason_code",
        "unknown_action",
        "no_action",
        "bad_adjustment_ref",
        "unknown_field",
    ],
)
def test_a_bad_adjustment_body_is_422(client, engine, caller, case):
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    preset_balance(engine, caller, sku_id, stock_balance=10, balance_version=1)
    response = post(client, caller, ADJUSTMENTS, bad_adjustment(case, caller, sku_id))
    assert error_of(response, 422)["code"] == "INVALID_REQUEST"
    assert idempotency_records(engine, caller) == []
    assert entries(engine, caller) == []


def test_an_adjustment_is_applied_once_whatever_key_reports_it(client, engine, caller):
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    preset_balance(engine, caller, sku_id, stock_balance=5, balance_version=1)
    ref = new_ref()
    body = adjustment_body(caller, "mark_unavailable", [line("A", sku_id, 5)], ref)
    data = created(post(client, caller, ADJUSTMENTS, body))
    applied = snapshot(engine, caller, [sku_id])

    # 换键同内容：先到者已用掉可售量，仍得到原结果而不是 409 INSUFFICIENT_STOCK。
    again = post(client, caller, ADJUSTMENTS, body)
    assert data_of(again) == data

    rid = request_id()
    changed = adjustment_body(caller, "mark_unavailable", [line("A", sku_id, 4)], ref)
    error = error_of(post(client, caller, ADJUSTMENTS, changed, rid=rid), 409)
    assert error["code"] == "BUSINESS_REF_CONFLICT"
    [event] = events(engine, rid)
    assert event["event_type"] == "business_ref_conflict"
    assert event["details"] == {"ref_type": "unavailable_adjustment", "ref": ref}

    assert snapshot(engine, caller, [sku_id]) == applied
    assert quantities_of(engine, caller, sku_id) == (5, 0, 5, 0)


def test_adjustment_and_receipt_refs_are_separate(client, engine, caller):
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    ref = new_ref()
    created(post(client, caller, RECEIPTS, receipt_body(caller, [line("A", sku_id, 3)], ref)))
    body = adjustment_body(caller, "mark_unavailable", [line("A", sku_id, 1)], ref)
    created(post(client, caller, ADJUSTMENTS, body))
    assert quantities_of(engine, caller, sku_id) == (3, 0, 1, 0)


def test_an_adjustment_on_an_initializing_pool_is_409(client, engine):
    caller = issue(engine)
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    body = adjustment_body(caller, "mark_unavailable", [line("A", sku_id, 1)])
    error = error_of(post(client, caller, ADJUSTMENTS, body), 409)
    assert error["code"] == "POOL_NOT_LIVE"
    assert entries(engine, caller) == [] and documents(engine, caller) == []


# --- 授权 ---


def test_another_tenants_pool_looks_missing(client, engine, caller):
    foreign = issue(engine)
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    for url, body in (
        (RECEIPTS, receipt_body(foreign, [line("A", sku_id, 1)])),
        (ADJUSTMENTS, adjustment_body(foreign, "mark_unavailable", [line("A", sku_id, 1)])),
    ):
        rid = request_id()
        error = error_of(post(client, caller, url, body, rid=rid), 404)
        assert error["code"] == "POOL_NOT_FOUND"
        [event] = events(engine, rid)
        assert event["details"] == {"object_type": "pool", "id": foreign.pool_id}
    assert entries(engine, caller) == [] and entries(engine, foreign) == []


# --- 并发 ---


@pytest.mark.parametrize("same_content", [True, False])
def test_concurrent_receipts_with_one_ref_book_once(
    client, engine, caller, monkeypatch, same_content
):
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    ref = new_ref()
    early_body = receipt_body(caller, [line("A", sku_id, 4)], ref)
    late_body = early_body if same_content else receipt_body(caller, [line("A", sku_id, 6)], ref)
    late_rid = request_id()

    # late 在锁余额行之前停住（读快照已建立，里面还没有单据）；early 锁住余额行后停住。
    # 放行 late，它在 early 持有的行锁上等待；再放行 early 提交。
    # late 拿到锁后写单据撞上唯一约束，按 early 的单据重新判定。
    before_lock, holding_lock = Gate(), Gate()
    lock_balances = stock_document_service.lock_balances

    def gated(execution, pool_id, sku_ids):
        late = execution.request.state.request_id == late_rid
        if late:
            before_lock.pass_through()
        balances = lock_balances(execution, pool_id, sku_ids)
        if not late:
            holding_lock.pass_through()
        return balances

    monkeypatch.setattr(stock_document_service, "lock_balances", gated)
    other_client = TestClient(client.app)
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            late = pool.submit(post, client, caller, RECEIPTS, late_body, rid=late_rid)
            assert before_lock.reached.wait(10)
            early = pool.submit(post, other_client, caller, RECEIPTS, early_body)
            assert holding_lock.reached.wait(10)
            before_lock.proceed.set()
            time.sleep(0.5)
        finally:
            before_lock.proceed.set()
            holding_lock.proceed.set()
        early_data = created(early.result())
        late_response = late.result()

    if same_content:
        assert data_of(late_response) == early_data
        assert events(engine, late_rid) == []
    else:
        assert error_of(late_response, 409)["code"] == "BUSINESS_REF_CONFLICT"
        [event] = events(engine, late_rid)
        assert event["event_type"] == "business_ref_conflict"

    # 只入库一次；late 的结果也作为它那个键的确定结果保存。
    [entry] = entries(engine, caller)
    assert entry["entry_id"] == early_data["lines"][0]["entry_id"]
    assert quantities_of(engine, caller, sku_id) == (4, 0, 0, 0)
    assert len(documents(engine, caller)) == 1
    assert len(idempotency_records(engine, caller)) == 2
