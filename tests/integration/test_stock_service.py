"""库存写命令共用的函数（app/services/stock.py；契约 §4.1、§4.3、§6.2、§6.8、§8.1）。

在真 MySQL 上跑；测试给应用挂一个测试专用的写路由，经幂等执行器运行命令。
请求体的 marker 选出测试登记在 COMMANDS 里的命令；执行器按 pool_id 与 sku_ids 预建余额行
（prebuild 为假时不预建，测试自己先建好）；ref 与 content 供单据命令用，content 只用来改变请求摘要。
并发的测试用 Gate 让先到的请求停在命令里，后到的请求在它持有的锁上等待。
造数据与观察都用测试自己的连接；应用用自己的连接池。
"""

import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated
from uuid import uuid4

import pytest
from fastapi import Depends, Request
from fastapi.responses import Response
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, select, update

from app.api.auth import authorize
from app.api.errors import ApiError
from app.api.requests import ParsedBody, RequestBody, json_body
from app.config import Settings
from app.domain.ledger import EntryType
from app.domain.service_auth import STOCK_WRITE
from app.main import create_app
from app.models.catalog import Product, Sku
from app.models.inventory import LedgerEntry, StockDocument, StockPool
from app.services.idempotency import CommandResult, Execution, run_idempotent
from app.services.security_events import CROSS_TENANT
from app.services.stock import (
    CORRECTION,
    RECEIPT,
    UNAVAILABLE_ADJUSTMENT,
    Document,
    OnConflict,
    append_entry,
    command_now,
    entry_representation,
    format_timestamp,
    insert_document,
    lock_balances,
    lock_pool,
    mark_verified,
    new_command_id,
    new_document_id,
    require_initializing,
    require_live,
    require_not_merged,
    require_skus_exist,
    with_document,
)
from tests.integration.conftest import (
    OCCURRED_AT,
    TEST_DATABASE_URL,
    add_entry,
    balance_insert,
    new_id,
    went_live,
)
from tests.integration.test_api_auth import Caller, error_of, events, issue
from tests.integration.test_idempotency import add_skus, balance_of, rid_of

STOCK_RUN = "test-stock.run"
STOCK_URL = "/test/stock"
ACTOR = {"X-Actor-Ref": "admin-7", "X-Actor-Role": "stock_admin"}


# --- 测试专用路由 ---


class StockBody(RequestBody):
    marker: str
    pool_id: str
    sku_ids: list[str] = []
    prebuild: bool = True
    ref: str = ""
    content: str = ""


StockCommand = Callable[[Execution, StockBody], CommandResult]
# 按 marker 登记的命令。每个测试结束时清空。
COMMANDS: dict[str, StockCommand] = {}

stock_body = json_body(StockBody)
WRITE = Depends(authorize(STOCK_WRITE, write=True))


def add_test_routes(app) -> None:
    @app.post(STOCK_URL, dependencies=[WRITE])
    def run_stock(
        request: Request, body: Annotated[ParsedBody[StockBody], Depends(stock_body)]
    ) -> Response:
        model = body.model

        def balance_rows(connection) -> list[tuple[str, str]]:
            return [(model.pool_id, sku_id) for sku_id in model.sku_ids]

        return run_idempotent(
            request,
            operation=STOCK_RUN,
            body=body,
            command=lambda execution: COMMANDS[model.marker](execution, model),
            balance_rows=balance_rows if model.prebuild else None,
        )


@pytest.fixture
def client(migrated_engine: Engine) -> Iterator[TestClient]:
    app = create_app(Settings(env="demo", git_sha="c" * 40, database_url=TEST_DATABASE_URL))
    add_test_routes(app)
    try:
        yield TestClient(app)
    finally:
        app.state.engine.dispose()


@pytest.fixture
def engine(migrated_engine: Engine) -> Engine:
    return migrated_engine


@pytest.fixture(autouse=True)
def clear_commands() -> Iterator[None]:
    yield
    COMMANDS.clear()


# --- 请求、造数据与观察 ---


def body_of(caller: Caller, marker: str, sku_ids=(), **extra: object) -> dict:
    return {"marker": marker, "pool_id": caller.pool_id, "sku_ids": list(sku_ids)} | extra


def send(client: TestClient, caller: Caller, body: dict, key: str | None = None):
    headers = {
        "Authorization": f"Bearer {caller.token}",
        "Idempotency-Key": key or f"shop:stock:{uuid4().hex}",
        "X-Request-ID": f"stock-test-{uuid4().hex}",
    } | ACTOR
    return client.post(STOCK_URL, headers=headers, json=body)


def data_of(response, status: int):
    assert response.status_code == status, response.text
    return response.json()["data"]


def preset_balance(engine: Engine, caller: Caller, sku_id: str, **values: object) -> None:
    with engine.begin() as db:
        db.execute(balance_insert(caller.tenant_id, caller.pool_id, sku_id, **values))


def entries(engine: Engine, caller: Caller) -> list[dict]:
    statement = (
        select(*LedgerEntry.__table__.columns)
        .where(LedgerEntry.tenant_id == caller.tenant_id)
        .order_by(LedgerEntry.sku_id, LedgerEntry.balance_version_after)
    )
    with engine.connect() as db:
        return [dict(row) for row in db.execute(statement).mappings()]


def documents(engine: Engine, caller: Caller) -> list[dict]:
    statement = select(*StockDocument.__table__.columns).where(
        StockDocument.tenant_id == caller.tenant_id
    )
    with engine.connect() as db:
        return [dict(row) for row in db.execute(statement).mappings()]


def judgement(engine: Engine, caller: Caller, sku_id: str) -> tuple:
    """余额行的版本、两个判定列与 opening_entry_id。"""
    row = balance_of(engine, caller, caller.pool_id, sku_id)
    return (
        row["balance_version"],
        row["last_book_only_version"],
        row["last_reconciled_version"],
        row["opening_entry_id"],
    )


def quantities_of(engine: Engine, caller: Caller, sku_id: str) -> tuple:
    row = balance_of(engine, caller, caller.pool_id, sku_id)
    names = ("stock_balance", "reserved", "unavailable", "paid_pending_dispatch")
    return tuple(row[name] for name in names)


class Gate:
    """第一次经过时停住，直到测试放行；之后经过的不停。"""

    def __init__(self) -> None:
        self.reached = threading.Event()
        self.proceed = threading.Event()

    def pass_through(self) -> None:
        if not self.reached.is_set():
            self.reached.set()
            assert self.proceed.wait(10)


def race(client: TestClient, caller: Caller, gate: Gate, first_body: dict, second_body: dict):
    """先到者停在 gate 上时发出后到者，给它时间走到锁上等待，再放行。"""
    other_client = TestClient(client.app)
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            first = pool.submit(send, client, caller, first_body, "shop:stock:first")
            assert gate.reached.wait(10)
            second = pool.submit(send, other_client, caller, second_body, "shop:stock:second")
            time.sleep(0.5)
        finally:
            gate.proceed.set()
        return first.result(), second.result()


def record_test_event(execution: Execution, body: StockBody) -> None:
    """命令登记的一条安全事件，用来观察它有没有被丢弃。"""
    execution.record_security_event(CROSS_TENANT, {"object_type": "test", "id": body.marker})


# --- 池状态 ---


def test_pool_status_is_checked_under_a_lock(client, engine):
    caller = issue(engine)

    def needs_live(execution: Execution, body: StockBody) -> CommandResult:
        require_live(execution, body.pool_id)
        return CommandResult({"status": lock_pool(execution, body.pool_id)})

    def needs_initializing(execution: Execution, body: StockBody) -> CommandResult:
        require_initializing(execution, body.pool_id)
        return CommandResult({"status": lock_pool(execution, body.pool_id, for_update=True)})

    COMMANDS["live"] = needs_live
    COMMANDS["initializing"] = needs_initializing

    error = error_of(send(client, caller, body_of(caller, "live")), 409)
    assert error["code"] == "POOL_NOT_LIVE"
    assert error["details"] == {"pool_status": "initializing"}
    assert data_of(send(client, caller, body_of(caller, "initializing")), 200) == {
        "status": "initializing"
    }

    with engine.begin() as db:
        statement = update(StockPool).where(StockPool.pool_id == caller.pool_id)
        db.execute(statement.values(status="live", **went_live(caller.credential_id)))

    assert data_of(send(client, caller, body_of(caller, "live")), 200) == {"status": "live"}
    error = error_of(send(client, caller, body_of(caller, "initializing")), 409)
    assert error["code"] == "POOL_ALREADY_LIVE"
    assert error["details"] == {"pool_status": "live"}


# --- SKU 检查 ---


def check_skus(execution: Execution, body: StockBody) -> CommandResult:
    require_skus_exist(execution, body.sku_ids)
    balances = lock_balances(execution, body.pool_id, body.sku_ids)
    require_not_merged(execution, body.sku_ids)
    return CommandResult({"sku_ids": list(balances)})


def test_missing_and_foreign_skus_are_listed_in_request_order(client, engine):
    caller = issue(engine)
    other = issue(engine)
    [own] = add_skus(engine, caller.tenant_id, 1)
    foreign_a, foreign_b = add_skus(engine, other.tenant_id, 2)
    missing = new_id("sku")
    COMMANDS["check"] = check_skus

    sku_ids = [foreign_a, own, missing, foreign_b]
    response = send(client, caller, body_of(caller, "check", sku_ids))
    error = error_of(response, 404)
    assert error["code"] == "SKU_NOT_FOUND"
    assert error["details"] == {"sku_ids": [foreign_a, missing, foreign_b]}

    recorded = events(engine, rid_of(response))
    assert [row["event_type"] for row in recorded] == [CROSS_TENANT, CROSS_TENANT]
    assert sorted(row["details"]["id"] for row in recorded) == sorted([foreign_a, foreign_b])
    for row in recorded:
        assert row["details"]["object_type"] == "sku"
        assert row["tenant_id"] == caller.tenant_id
    assert entries(engine, caller) == []


def test_merged_skus_are_rejected_and_inactive_ones_pass(client, engine):
    caller = issue(engine)
    merged, canonical, inactive, under_inactive_product = add_skus(engine, caller.tenant_id, 4)
    with engine.begin() as db:
        db.execute(
            update(Sku)
            .where(Sku.sku_id == merged)
            .values(status="inactive", merged_into_sku_id=canonical)
        )
        db.execute(update(Sku).where(Sku.sku_id == inactive).values(status="inactive"))
        product_id = db.execute(
            select(Sku.product_id).where(Sku.sku_id == under_inactive_product)
        ).scalar_one()
        db.execute(
            update(Product).where(Product.product_id == product_id).values(status="inactive")
        )
    COMMANDS["check"] = check_skus

    passing = [inactive, under_inactive_product, canonical]
    data = data_of(send(client, caller, body_of(caller, "check", passing)), 200)
    assert data["sku_ids"] == sorted(passing)

    response = send(client, caller, body_of(caller, "check", [canonical, merged]))
    error = error_of(response, 409)
    assert error["code"] == "SKU_MERGED"
    assert error["details"] == {"sku_id": merged, "merged_into_sku_id": canonical}


def test_a_merge_committed_after_the_command_started_is_still_seen(client, engine):
    caller = issue(engine)
    sku, canonical = add_skus(engine, caller.tenant_id, 2)
    seen: list[str | None] = []

    def merge_meanwhile(execution: Execution, body: StockBody) -> CommandResult:
        require_skus_exist(execution, body.sku_ids)
        # 命令已经开始：另一个连接提交了这个 SKU 的合并。
        with engine.begin() as other:
            other.execute(
                update(Sku)
                .where(Sku.sku_id == sku)
                .values(status="inactive", merged_into_sku_id=canonical)
            )
        # 普通读仍读快照，看不到它。
        statement = select(Sku.merged_into_sku_id).where(Sku.sku_id == sku)
        seen.append(execution.connection.execute(statement).scalar_one())
        lock_balances(execution, body.pool_id, body.sku_ids)
        require_not_merged(execution, body.sku_ids)
        return CommandResult({})

    COMMANDS["merge"] = merge_meanwhile
    response = send(client, caller, body_of(caller, "merge", [sku]))
    assert seen == [None]
    error = error_of(response, 409)
    assert error["code"] == "SKU_MERGED"
    assert error["details"] == {"sku_id": sku, "merged_into_sku_id": canonical}


# --- 流水 ---


def test_entries_of_one_command_share_the_command_time_and_id(client, engine):
    caller = issue(engine)
    skus = add_skus(engine, caller.tenant_id, 2)
    seen = {}

    def receive(execution: Execution, body: StockBody) -> CommandResult:
        now = command_now(execution)
        seen["now"] = now
        balances = lock_balances(execution, body.pool_id, body.sku_ids)
        command_id = new_command_id()
        written = [
            append_entry(
                execution, balances[sku_id], EntryType.RECEIPT, (2, 0, 0, 0), command_id, now=now
            )
            for sku_id in body.sku_ids
        ]
        given = append_entry(
            execution,
            balances[skus[0]],
            EntryType.RECEIPT,
            (1, 0, 0, 0),
            command_id,
            now=now,
            occurred_at=OCCURRED_AT,
        )
        return CommandResult(written + [given], 201)

    COMMANDS["receive"] = receive
    data = data_of(send(client, caller, body_of(caller, "receive", skus)), 201)
    first, second, given = data

    now = format_timestamp(seen["now"])
    assert first["occurred_at"] == second["occurred_at"] == now
    assert first["recorded_at"] == second["recorded_at"] == given["recorded_at"] == now
    assert given["occurred_at"] == format_timestamp(OCCURRED_AT) == "2026-11-12T08:30:00.000000Z"
    assert {entry["command_id"] for entry in data} == {first["command_id"]}
    assert first["command_id"].startswith("cmd_")

    rows = entries(engine, caller)
    assert len(rows) == 3
    assert {row["command_id"] for row in rows} == {first["command_id"]}
    assert {row["recorded_at"] for row in rows} == {seen["now"]}


def test_two_entries_on_one_row_build_on_each_other(client, engine):
    caller = issue(engine)
    [sku] = add_skus(engine, caller.tenant_id, 1)
    preset_balance(
        engine,
        caller,
        sku,
        stock_balance=10,
        balance_version=3,
        last_book_only_version=2,
        last_reconciled_version=1,
    )

    def receive_and_mark(execution: Execution, body: StockBody) -> CommandResult:
        now = command_now(execution)
        balance = lock_balances(execution, body.pool_id, body.sku_ids)[sku]
        command_id = new_command_id()
        received = append_entry(
            execution,
            balance,
            EntryType.RECEIPT,
            (5, 0, 0, 0),
            command_id,
            now=now,
            source_type="receipt",
            source_ref="rcv-1",
            line_ref="l1",
            reason_code="supplier_short",
            note="first",
        )
        marked = append_entry(
            execution, balance, EntryType.MARK_UNAVAILABLE, (0, 0, 4, 0), command_id, now=now
        )
        return CommandResult([received, marked], 201)

    COMMANDS["two"] = receive_and_mark
    key = "shop:stock:two"
    response = send(client, caller, body_of(caller, "two", [sku]), key)
    received, marked = data_of(response, 201)

    assert quantities_of(engine, caller, sku) == (15, 0, 4, 0)
    # 收货与标不可售都不动两个判定列。
    assert judgement(engine, caller, sku) == (5, 2, 1, None)
    assert received["after"] == {
        "stock_balance": 15,
        "reserved": 0,
        "unavailable": 0,
        "paid_pending_dispatch": 0,
    }
    assert received["deltas"] == {
        "stock_balance": 5,
        "reserved": 0,
        "unavailable": 0,
        "paid_pending_dispatch": 0,
    }
    assert received["balance_version_after"] == 4
    assert marked["after"] == {
        "stock_balance": 15,
        "reserved": 0,
        "unavailable": 4,
        "paid_pending_dispatch": 0,
    }
    assert marked["balance_version_after"] == 5
    assert received["command_id"] == marked["command_id"]
    assert received["entry_id"].startswith("ent_")

    # 表示与库里的行一致；不含幂等键与凭据，它们在库里。
    rows = entries(engine, caller)
    assert [entry_representation(row) for row in rows] == [received, marked]
    assert "idempotency_key" not in received and "credential_id" not in received
    for row in rows:
        assert row["idempotency_key"] == key
        assert row["credential_id"] == caller.credential_id
        assert row["request_id"] == rid_of(response)
        assert row["actor_ref"] == ACTOR["X-Actor-Ref"]
        assert row["actor_role"] == ACTOR["X-Actor-Role"]
        assert row["pool_id"] == caller.pool_id
    assert received["channel_id"] is not None
    assert (received["source_type"], received["source_ref"], received["line_ref"]) == (
        "receipt",
        "rcv-1",
        "l1",
    )
    assert (received["reason_code"], received["note"]) == ("supplier_short", "first")


def test_judgement_columns_and_the_opening_entry(client, engine):
    caller = issue(engine)
    [sku] = add_skus(engine, caller.tenant_id, 1)
    state: dict[str, str] = {}

    def writer(entry_type: EntryType, deltas, **kwargs) -> StockCommand:
        def write(execution: Execution, body: StockBody) -> CommandResult:
            now = command_now(execution)
            balance = lock_balances(execution, body.pool_id, body.sku_ids)[sku]
            extra = {key: value() for key, value in kwargs.items()}
            entry = append_entry(
                execution, balance, entry_type, deltas, new_command_id(), now=now, **extra
            )
            return CommandResult(entry, 201)

        return write

    COMMANDS["opening"] = writer(EntryType.OPENING_BALANCE, (10, 0, 0, 0))
    COMMANDS["correction"] = writer(
        EntryType.CORRECTION, (-10, 0, 0, 0), corrects_entry_id=lambda: state["opening"]
    )
    COMMANDS["receipt"] = writer(EntryType.RECEIPT, (3, 0, 0, 0))
    COMMANDS["count"] = writer(EntryType.COUNT_ADJUSTMENT, (1, 0, 0, 0))

    opening = data_of(send(client, caller, body_of(caller, "opening", [sku])), 201)
    state["opening"] = opening["entry_id"]
    # 定数类流水：两个判定列都设为它的版本；期初记在 opening_entry_id。
    assert judgement(engine, caller, sku) == (1, 1, 1, opening["entry_id"])

    correction = data_of(send(client, caller, body_of(caller, "correction", [sku])), 201)
    assert correction["corrects_entry_id"] == opening["entry_id"]
    # 更正只更新 last_book_only_version；更正的是期初，清空 opening_entry_id。
    assert judgement(engine, caller, sku) == (2, 2, 1, None)

    data_of(send(client, caller, body_of(caller, "receipt", [sku])), 201)
    assert judgement(engine, caller, sku) == (3, 2, 1, None)

    data_of(send(client, caller, body_of(caller, "count", [sku])), 201)
    assert judgement(engine, caller, sku) == (4, 4, 4, None)
    assert quantities_of(engine, caller, sku) == (4, 0, 0, 0)


def test_a_given_reconciled_version_and_verification_without_an_entry(client, engine):
    caller = issue(engine)
    [sku] = add_skus(engine, caller.tenant_id, 1)
    preset_balance(
        engine,
        caller,
        sku,
        stock_balance=5,
        balance_version=3,
        last_book_only_version=2,
        last_reconciled_version=1,
    )

    def count(execution: Execution, body: StockBody) -> CommandResult:
        now = command_now(execution)
        balance = lock_balances(execution, body.pool_id, body.sku_ids)[sku]
        # 盘点确认的例外：last_reconciled_version 取快照版本 2。
        append_entry(
            execution,
            balance,
            EntryType.COUNT_ADJUSTMENT,
            (1, 0, 0, 0),
            new_command_id(),
            now=now,
            reconciled_version=2,
        )
        before = (balance.balance_version, balance.last_book_only_version)
        mark_verified(execution, balance, reconciled_version=3)
        return CommandResult({"before": list(before)}, 201)

    def shrink(execution: Execution, body: StockBody) -> CommandResult:
        now = command_now(execution)
        balance = lock_balances(execution, body.pool_id, body.sku_ids)[sku]
        append_entry(
            execution,
            balance,
            EntryType.RECEIPT,
            (1, 0, 0, 0),
            new_command_id(),
            now=now,
            reconciled_version=0,
        )
        return CommandResult({}, 201)

    def verify(execution: Execution, body: StockBody) -> CommandResult:
        balance = lock_balances(execution, body.pool_id, body.sku_ids)[sku]
        mark_verified(execution, balance)
        return CommandResult({})

    COMMANDS["count"] = count
    COMMANDS["shrink"] = shrink
    COMMANDS["verify"] = verify

    assert data_of(send(client, caller, body_of(caller, "count", [sku])), 201) == {"before": [4, 4]}
    assert judgement(engine, caller, sku) == (4, 4, 3, None)

    # 给定的值比当前小：程序错误，500，什么也没写。
    error = error_of(send(client, caller, body_of(caller, "shrink", [sku])), 500)
    assert error["code"] == "INTERNAL_ERROR"
    assert judgement(engine, caller, sku) == (4, 4, 3, None)
    assert len(entries(engine, caller)) == 1

    data_of(send(client, caller, body_of(caller, "verify", [sku])), 200)
    assert judgement(engine, caller, sku) == (4, 4, 4, None)


def test_a_broken_invariant_rolls_back_the_whole_command(client, engine):
    caller = issue(engine)
    first, second = add_skus(engine, caller.tenant_id, 2)

    def overdraw(execution: Execution, body: StockBody) -> CommandResult:
        now = command_now(execution)
        balances = lock_balances(execution, body.pool_id, body.sku_ids)
        command_id = new_command_id()
        append_entry(
            execution, balances[first], EntryType.RECEIPT, (5, 0, 0, 0), command_id, now=now
        )
        # 第二个 SKU 没有库存：unavailable 超过 stock_balance。
        append_entry(
            execution,
            balances[second],
            EntryType.MARK_UNAVAILABLE,
            (0, 0, 1, 0),
            command_id,
            now=now,
        )
        return CommandResult({}, 201)

    COMMANDS["overdraw"] = overdraw
    error = error_of(send(client, caller, body_of(caller, "overdraw", [first, second])), 500)
    assert error["code"] == "INTERNAL_ERROR"
    assert entries(engine, caller) == []
    assert quantities_of(engine, caller, first) == (0, 0, 0, 0)
    assert judgement(engine, caller, first) == (0, 0, 0, None)


# --- 加锁顺序 ---


def test_balance_rows_are_locked_in_ascending_order(client, engine):
    caller = issue(engine)
    low, high = sorted(add_skus(engine, caller.tenant_id, 2))
    for sku in (low, high):
        preset_balance(engine, caller, sku)
    locked: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:
        if "FROM stock_balances" in statement and statement.rstrip().endswith("FOR UPDATE"):
            locked.extend(value for key, value in parameters.items() if key.startswith("sku_id"))

    gate = Gate()

    def receive(execution: Execution, body: StockBody) -> CommandResult:
        now = command_now(execution)
        balances = lock_balances(execution, body.pool_id, body.sku_ids)
        gate.pass_through()
        command_id = new_command_id()
        for sku_id in body.sku_ids:
            append_entry(
                execution, balances[sku_id], EntryType.RECEIPT, (1, 0, 0, 0), command_id, now=now
            )
        return CommandResult({}, 201)

    COMMANDS["receive"] = receive
    app_engine = client.app.state.engine
    event.listen(app_engine, "before_cursor_execute", record)
    try:
        # 不预建：后到者直接走到锁余额行那一步，在先到者持有的第一行上等待。
        first, second = race(
            client,
            caller,
            gate,
            body_of(caller, "receive", [high, low], prebuild=False),
            body_of(caller, "receive", [low, high], prebuild=False),
        )
    finally:
        event.remove(app_engine, "before_cursor_execute", record)

    assert first.status_code == second.status_code == 201
    assert locked == [low, high, low, high]
    for sku in (low, high):
        assert quantities_of(engine, caller, sku) == (2, 0, 0, 0)
        assert judgement(engine, caller, sku)[0] == 2


# --- 单据 ---


def receipt_command(
    execution: Execution, body: StockBody, on_conflict: OnConflict | None = None
) -> CommandResult:
    """收一件，写一张收货单据；content 进结果。"""

    def command(execution: Execution) -> CommandResult:
        now = command_now(execution)
        balances = lock_balances(execution, body.pool_id, body.sku_ids)
        command_id = new_command_id()
        written = [
            append_entry(
                execution,
                balances[sku_id],
                EntryType.RECEIPT,
                (1, 0, 0, 0),
                command_id,
                now=now,
                source_type="receipt",
                source_ref=body.ref,
            )
            for sku_id in body.sku_ids
        ]
        document_id = new_document_id(RECEIPT)
        result = {
            "document_id": document_id,
            "receipt_ref": body.ref,
            "content": body.content,
            "created_at": format_timestamp(now),
            "entries": written,
        }
        insert_document(execution, RECEIPT, body.ref, document_id, now, result)
        return CommandResult(result, 201)

    return with_document(execution, RECEIPT, body.ref, command, on_conflict)


def test_a_document_is_answered_by_its_digest_on_a_new_key(client, engine):
    caller = issue(engine)
    [sku] = add_skus(engine, caller.tenant_id, 1)
    COMMANDS["receipt"] = receipt_command

    def already_corrected(document: Document):
        details = {"reason": "already_corrected", "document_id": document.document_id}
        raise ApiError(409, "ENTRY_NOT_CORRECTABLE", "already corrected", details=details)

    def custom(execution: Execution, body: StockBody) -> CommandResult:
        return receipt_command(execution, body, already_corrected)

    COMMANDS["custom"] = custom

    ref = "rcv-1"
    body = body_of(caller, "receipt", [sku], ref=ref, content="a")
    first = data_of(send(client, caller, body), 201)
    assert first["document_id"].startswith("rcp_")

    again = send(client, caller, body)
    assert data_of(again, 200) == first
    assert "Idempotency-Replayed" not in again.headers
    assert events(engine, rid_of(again)) == []
    assert quantities_of(engine, caller, sku) == (1, 0, 0, 0)

    [document] = documents(engine, caller)
    assert document["document_id"] == first["document_id"]
    assert document["document_type"] == RECEIPT
    assert document["document_ref"] == ref
    assert document["result"] == first
    assert document["pool_id"] == caller.pool_id
    assert document["credential_id"] == caller.credential_id
    assert (document["actor_ref"], document["actor_role"]) == ("admin-7", "stock_admin")
    assert format_timestamp(document["created_at"]) == first["created_at"]
    assert first["created_at"] == first["entries"][0]["recorded_at"]

    conflict = send(client, caller, body | {"content": "b"})
    error = error_of(conflict, 409)
    assert error["code"] == "BUSINESS_REF_CONFLICT"
    assert error["details"] == {"ref_type": RECEIPT, "ref": ref}
    [conflict_event] = events(engine, rid_of(conflict))
    assert conflict_event["event_type"] == "business_ref_conflict"
    assert conflict_event["details"] == {"ref_type": RECEIPT, "ref": ref}

    # 调用方给的回答替换默认的冲突，不记事件。
    replaced = send(client, caller, body | {"marker": "custom"})
    error = error_of(replaced, 409)
    assert error["code"] == "ENTRY_NOT_CORRECTABLE"
    assert error["details"] == {
        "reason": "already_corrected",
        "document_id": first["document_id"],
    }
    assert events(engine, rid_of(replaced)) == []

    assert len(documents(engine, caller)) == 1
    assert quantities_of(engine, caller, sku) == (1, 0, 0, 0)


@pytest.mark.parametrize("same_content", [True, False])
def test_a_document_inserted_concurrently_is_rejudged(client, engine, same_content):
    caller = issue(engine)
    gate = Gate()

    def claim(execution: Execution, body: StockBody) -> CommandResult:
        def command(execution: Execution) -> CommandResult:
            now = command_now(execution)
            document_id = new_document_id(UNAVAILABLE_ADJUSTMENT)
            result = {"document_id": document_id, "content": body.content}
            insert_document(execution, UNAVAILABLE_ADJUSTMENT, body.ref, document_id, now, result)
            # 先到者插完单据、提交之前停在这里；后到者在单据的唯一键上等它。
            gate.pass_through()
            return CommandResult(result, 201)

        return with_document(execution, UNAVAILABLE_ADJUSTMENT, body.ref, command)

    COMMANDS["claim"] = claim
    first_body = body_of(caller, "claim", ref="adj-1", content="a")
    second_body = first_body if same_content else first_body | {"content": "b"}
    first, second = race(client, caller, gate, first_body, second_body)

    first_data = data_of(first, 201)
    if same_content:
        assert data_of(second, 200) == first_data
        assert events(engine, rid_of(second)) == []
    else:
        assert error_of(second, 409)["code"] == "BUSINESS_REF_CONFLICT"
        [conflict_event] = events(engine, rid_of(second))
        assert conflict_event["event_type"] == "business_ref_conflict"
    [document] = documents(engine, caller)
    assert document["document_id"] == first_data["document_id"]

    # 重新判定的结果是后到者那个键的确定结果。
    replay = send(client, caller, second_body, "shop:stock:second")
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == second.json()


def test_a_rejection_caused_by_the_concurrent_winner_turns_into_its_result(client, engine):
    caller = issue(engine)
    [sku] = add_skus(engine, caller.tenant_id, 1)
    preset_balance(engine, caller, sku, stock_balance=5)
    gate = Gate()
    available: list[int] = []

    def mark(execution: Execution, body: StockBody) -> CommandResult:
        def command(execution: Execution) -> CommandResult:
            now = command_now(execution)
            balance = lock_balances(execution, body.pool_id, body.sku_ids)[sku]
            available.append(balance.quantities.available)
            record_test_event(execution, body)
            # 按账面拒绝：可售量不够标 5 件不可售。
            if balance.quantities.available < 5:
                raise ApiError(409, "INSUFFICIENT_STOCK", "not enough available stock")
            entry = append_entry(
                execution,
                balance,
                EntryType.MARK_UNAVAILABLE,
                (0, 0, 5, 0),
                new_command_id(),
                now=now,
                source_type="unavailable_adjustment",
                source_ref=body.ref,
                reason_code="damaged",
            )
            document_id = new_document_id(UNAVAILABLE_ADJUSTMENT)
            result = {"document_id": document_id, "entry_id": entry["entry_id"]}
            insert_document(execution, UNAVAILABLE_ADJUSTMENT, body.ref, document_id, now, result)
            # 先到者写完、提交之前停在这里；后到者已经查过单据（没有），在余额行上等它。
            gate.pass_through()
            return CommandResult(result, 201)

        return with_document(execution, UNAVAILABLE_ADJUSTMENT, body.ref, command)

    COMMANDS["mark"] = mark
    # 不预建（余额行已建好）：预建会在先到者锁住的行上等它提交，后到者的读快照就看得到单据了。
    body = body_of(caller, "mark", [sku], ref="adj-1", prebuild=False)
    first, second = race(client, caller, gate, body, body)

    first_data = data_of(first, 201)
    # 后到者第一次运行时没查到单据，锁到行时可售量已被用掉而被拒；
    # 重新判定读到先到者的单据，回答它的结果，不再运行命令。
    assert available == [5, 0]
    assert data_of(second, 200) == first_data
    assert len(events(engine, rid_of(first))) == 1
    # 被拒的第一次运行登记的事件丢弃。
    assert events(engine, rid_of(second)) == []
    assert quantities_of(engine, caller, sku) == (5, 0, 5, 0)
    assert len(entries(engine, caller)) == 1
    assert len(documents(engine, caller)) == 1


def test_a_rejection_on_the_rerun_is_the_final_result(client, engine):
    caller = issue(engine)
    runs: list[str] = []

    def refuse(execution: Execution, body: StockBody) -> CommandResult:
        def command(execution: Execution) -> CommandResult:
            runs.append(body.ref)
            record_test_event(execution, body)
            raise ApiError(409, "INSUFFICIENT_STOCK", "not enough available stock")

        return with_document(execution, UNAVAILABLE_ADJUSTMENT, body.ref, command)

    COMMANDS["refuse"] = refuse
    body = body_of(caller, "refuse", ref="adj-1")
    response = send(client, caller, body, "shop:stock:refuse")
    assert error_of(response, 409)["code"] == "INSUFFICIENT_STOCK"
    # 第一次运行的拒绝改为重新判定，在新事务里再运行一次；第一次登记的事件丢弃。
    assert runs == ["adj-1", "adj-1"]
    assert len(events(engine, rid_of(response))) == 1
    assert documents(engine, caller) == []

    replay = send(client, caller, body, "shop:stock:refuse")
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert runs == ["adj-1", "adj-1"]


def test_a_correction_taken_concurrently_is_rejudged(client, engine):
    caller = issue(engine)
    [sku] = add_skus(engine, caller.tenant_id, 1)
    preset_balance(engine, caller, sku, stock_balance=10, balance_version=1)
    with engine.begin() as db:
        target = add_entry(db, (caller.tenant_id, caller.pool_id, sku), 1)
    gate = Gate()
    versions: list[int] = []

    def correct(execution: Execution, body: StockBody) -> CommandResult:
        def command(execution: Execution) -> CommandResult:
            now = command_now(execution)
            balance = lock_balances(execution, body.pool_id, body.sku_ids)[sku]
            versions.append(balance.balance_version)
            entry = append_entry(
                execution,
                balance,
                EntryType.CORRECTION,
                (-5, 0, 0, 0),
                new_command_id(),
                now=now,
                source_type="correction",
                source_ref=body.ref,
                reason_code="wrong",
                corrects_entry_id=body.ref,
            )
            document_id = new_document_id(CORRECTION)
            result = {"document_id": document_id, "corrected_entry_id": body.ref, "entry": entry}
            insert_document(execution, CORRECTION, body.ref, document_id, now, result)
            gate.pass_through()
            return CommandResult(result, 201)

        return with_document(execution, CORRECTION, body.ref, command)

    COMMANDS["correct"] = correct
    body = body_of(caller, "correct", [sku], ref=target, prebuild=False)
    # 后到者锁到余额行时先到者已提交；它的 CORRECTION 撞上 (tenant_id, corrects_entry_id)。
    first, second = race(client, caller, gate, body, body)

    first_data = data_of(first, 201)
    # 后到者运行过一次（锁到的是先到者写后的行），重新判定读到单据，不再运行。
    assert versions == [1, 2]
    assert first_data["document_id"].startswith("cor_")
    assert data_of(second, 200) == first_data
    corrections = [row for row in entries(engine, caller) if row["entry_type"] == "CORRECTION"]
    assert [row["entry_id"] for row in corrections] == [first_data["entry"]["entry_id"]]
    assert quantities_of(engine, caller, sku) == (5, 0, 0, 0)
    assert judgement(engine, caller, sku)[:3] == (2, 2, 0)
    assert len(documents(engine, caller)) == 1
