"""预留的最后变更时间：迁移回填、各条命令在同一事务里设它、不改列的路径不设（契约 §5.9）。

在真 MySQL 上跑；每个测试建自己的租户、池、销售入口与凭据，池先切到 live，
预留、确认销售、待审与付款后的单据经接口建立；到期由测试把预留挪到过去模拟（自己提交的连接）。
回填照 test_demo_reset_schema.py 的先例，在 production 测试库上降到 0009 再升回，
不动共享的 demo 库；对它升降迁移之前，测试对它开着的连接与事务都先结束。
"""

from collections.abc import Iterator
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, inspect, select

from alembic import command
from app.db import make_engine
from app.domain.environment import PRODUCTION
from app.domain.sale_commits import sku_shortfalls
from app.models.orders import OrderDocument, Reservation
from app.services import sale_commits as sale_commit_service
from app.services.reservations import database_now, sweep_expired_reservations
from tests.integration.conftest import (
    DISPATCHES,
    REFUND_RESTOCKS,
    TEST_DATABASE_URL,
    body_of,
    committed_order,
    documents,
    new_key,
    post,
    qline,
)
from tests.integration.test_api_auth import Caller, error_of
from tests.integration.test_demo_reset_schema import alembic_version, migrate_production
from tests.integration.test_late_payment_review_api import close, order_in_review
from tests.integration.test_order_document_schema import add_document
from tests.integration.test_reservation_release_api import release, reserved_order
from tests.integration.test_reservation_schema import add_reservation, add_shop, released
from tests.integration.test_reservations_api import (
    RESERVATION_KEYS,
    data_of,
    line,
    live_caller,
    make_client,
    new_ref,
    order,
    reserve,
    rows_of,
    shift_into_the_past,
)
from tests.integration.test_return_receipts_api import (
    dispatched_order,
    receipts,
    receive,
    return_body,
    rline,
)
from tests.integration.test_sale_commit_fault_api import fault_body, stocked_skus
from tests.integration.test_sale_commit_late_api import (
    LATE_PAYMENT_INSUFFICIENT_STOCK,
    expired_order,
    set_unavailable,
)
from tests.integration.test_sale_commit_schema import committed, refunded
from tests.integration.test_sale_commits_api import commit, commit_body, stored_reservation

# 调用方给的付款时间远在将来：最后变更时间任何情况下都不取它。
FUTURE_PAID_AT = "2099-01-01T00:00:00Z"
FUTURE_PAID_AT_UTC = datetime(2099, 1, 1)
CANCELLED = {"reason": "cancelled"}


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


@pytest.fixture
def own_engine(migrated_engine: Engine) -> Iterator[Engine]:
    """定时任务用的单独引擎：它设的会话隔离级别不会带进共用的连接池。"""
    own = make_engine(TEST_DATABASE_URL)
    try:
        yield own
    finally:
        own.dispose()


@pytest.fixture
def unclosed(monkeypatch) -> None:
    """确认销售开事务前钩子里的关单换成空操作（同 test_sale_commit_late_api.py）：
    已到期未关的 active 预留留到业务事务里关。
    """

    def skip(connection, tenant_id, pool_id, sku_ids) -> int:
        return 0

    monkeypatch.setattr(sale_commit_service, "close_due_before_reserving", skip)


# --- 观察 ---


def stored(engine: Engine, caller: Caller, reservation_id: str) -> dict:
    return stored_reservation(engine, caller, reservation_id)


def changed_at(engine: Engine, caller: Caller, reservation_id: str) -> datetime:
    return stored(engine, caller, reservation_id)["last_changed_at"]


def now(engine: Engine) -> datetime:
    with engine.connect() as db:
        return database_now(db)


def latest_written(row: dict) -> datetime:
    """事务写进预留的四个时间列里最晚的一个（不看 expires_at 与 paid_at）。"""
    written = (row["created_at"], row["closed_at"], row["committed_at"], row["review_closed_at"])
    return max(value for value in written if value is not None)


# --- 迁移回填（production 测试库） ---


def test_upgrading_backfills_the_latest_time_column_or_document(production_engine: Engine):
    shop = None
    try:
        migrate_production(command.downgrade, "0009", PRODUCTION)
        assert alembic_version(production_engine) == "0009"
        columns = inspect(production_engine).get_columns("reservations")
        assert "last_changed_at" not in {column["name"] for column in columns}

        with production_engine.begin() as db:
            shop = add_shop(db)
            # 付款时间都在将来：回填不取它。
            future = {"paid_at": FUTURE_PAID_AT_UTC}
            active = add_reservation(db, shop)
            released_id = add_reservation(db, shop, **released(shop))
            committed_id = add_reservation(db, shop, **committed(shop, **future))
            # 经到期再晚到确认：关单时间晚于确认时间。
            late_id = add_reservation(
                db, shop, **committed(shop, closed_at=datetime(2026, 11, 16, 8, 20), **future)
            )
            documented = add_reservation(db, shop, **committed(shop, **future))
            for created_at in (datetime(2026, 11, 16, 11, 0), datetime(2026, 11, 16, 10, 0)):
                add_document(db, shop, documented, created_at=created_at)
            refunded_id = add_reservation(db, shop, **refunded(shop, **future))
            before = {
                row.reservation_id: dict(row._mapping)
                for row in db.execute(
                    select(
                        Reservation.reservation_id,
                        Reservation.created_at,
                        Reservation.closed_at,
                        Reservation.committed_at,
                        Reservation.review_closed_at,
                    ).where(Reservation.tenant_id == shop.tenant_id)
                )
            }

        migrate_production(command.upgrade, "0010", PRODUCTION)
        assert alembic_version(production_engine) == "0010"
        with production_engine.connect() as db:
            statement = select(Reservation.reservation_id, Reservation.last_changed_at).where(
                Reservation.tenant_id == shop.tenant_id
            )
            backfilled = dict(db.execute(statement).all())

        assert backfilled[active] == datetime(2026, 11, 16, 8, 0)
        assert backfilled[released_id] == datetime(2026, 11, 16, 8, 5)
        assert backfilled[committed_id] == datetime(2026, 11, 16, 8, 10)
        assert backfilled[late_id] == datetime(2026, 11, 16, 8, 20)
        assert backfilled[documented] == datetime(2026, 11, 16, 11, 0)
        assert backfilled[refunded_id] == datetime(2026, 11, 16, 9, 0)
        # 没有单据的各行等于四个时间列里最大的一个。
        for reservation_id, row in before.items():
            if reservation_id != documented:
                assert backfilled[reservation_id] == latest_written(row)
    finally:
        migrate_production(command.upgrade, "head", PRODUCTION)
        if shop is not None:
            with production_engine.begin() as db:
                db.execute(delete(OrderDocument).where(OrderDocument.tenant_id == shop.tenant_id))
                db.execute(delete(Reservation).where(Reservation.tenant_id == shop.tenant_id))


# --- 新建 ---


def test_a_new_reservation_sets_it_to_created_at(client, engine, caller):
    created, _ = reserved_order(client, engine, caller, [2, 1])
    row = stored(engine, caller, created["reservation_id"])
    assert row["last_changed_at"] == row["created_at"]
    # 不进预留的表示。
    assert set(created) == RESERVATION_KEYS


def test_a_fault_commit_sets_it_on_the_created_reservation(client, engine, caller):
    first_sku, second_sku = stocked_skus(engine, caller, 5, 1)

    body = fault_body([line("l1", first_sku, 2)], paid_at=FUTURE_PAID_AT)
    data = data_of(commit(client, caller, new_ref(), body), 200)
    assert data["status"] == "committed"
    assert set(data) == RESERVATION_KEYS
    row = stored(engine, caller, data["reservation_id"])
    assert row["last_changed_at"] == row["created_at"] == row["committed_at"] == row["closed_at"]

    # 不够：转入待审的预留同样设上。
    order_ref = new_ref()
    short = fault_body([line("l1", second_sku, 2)], paid_at=FUTURE_PAID_AT)
    response = commit(client, caller, order_ref, short)
    assert error_of(response, 409)["code"] == LATE_PAYMENT_INSUFFICIENT_STOCK
    rows = rows_of(engine, Reservation, caller)
    [review] = [row for row in rows if row["order_ref"] == order_ref]
    assert review["status"] == "late_payment_review"
    assert review["last_changed_at"] == review["created_at"] == review["closed_at"]


# --- 释放与到期关单 ---


def test_releasing_sets_it_to_closed_at(client, engine, caller):
    created, _ = reserved_order(client, engine, caller, [1])
    reservation_id = created["reservation_id"]
    before = changed_at(engine, caller, reservation_id)

    data = data_of(release(client, caller, reservation_id, CANCELLED), 200)
    assert set(data) == RESERVATION_KEYS
    row = stored(engine, caller, reservation_id)
    assert row["status"] == "released"
    assert row["last_changed_at"] > before
    assert row["last_changed_at"] == row["closed_at"] == latest_written(row)


def test_the_sweep_sets_it_to_closed_at(client, engine, caller, own_engine):
    created, _ = reserved_order(client, engine, caller, [1])
    reservation_id = created["reservation_id"]
    shift_into_the_past(engine, reservation_id)
    before = changed_at(engine, caller, reservation_id)

    assert sweep_expired_reservations(own_engine) >= 1
    row = stored(engine, caller, reservation_id)
    assert row["status"] == "expired"
    assert row["last_changed_at"] > before
    assert row["last_changed_at"] == row["closed_at"] == latest_written(row)


def test_releasing_a_due_reservation_sets_it_to_the_closing_time(client, engine, caller):
    created, _ = reserved_order(client, engine, caller, [1])
    reservation_id = created["reservation_id"]
    shift_into_the_past(engine, reservation_id)
    before = changed_at(engine, caller, reservation_id)

    data = data_of(release(client, caller, reservation_id, CANCELLED), 200)
    assert data["status"] == "expired"
    row = stored(engine, caller, reservation_id)
    assert row["status"] == "expired"
    assert row["last_changed_at"] > before
    assert row["last_changed_at"] == row["closed_at"] == latest_written(row)


def test_a_new_reservation_closing_a_due_one_first_sets_it_on_the_closed_one(
    client, engine, caller
):
    created, (sku,) = reserved_order(client, engine, caller, [5])
    reservation_id = created["reservation_id"]
    shift_into_the_past(engine, reservation_id)
    before = changed_at(engine, caller, reservation_id)

    data_of(reserve(client, caller, order([line("l1", sku, 5)])), 201)
    row = stored(engine, caller, reservation_id)
    assert row["status"] == "expired"
    assert row["last_changed_at"] > before
    assert row["last_changed_at"] == row["closed_at"] == latest_written(row)


# --- 确认销售与待审 ---


def test_committing_sets_it_to_committed_at(client, engine, caller):
    created, _ = reserved_order(client, engine, caller, [2])
    reservation_id = created["reservation_id"]
    before = changed_at(engine, caller, reservation_id)

    body = commit_body(created, paid_at=FUTURE_PAID_AT)
    data = data_of(commit(client, caller, created["order_ref"], body), 200)
    assert set(data) == RESERVATION_KEYS
    row = stored(engine, caller, reservation_id)
    assert row["status"] == "committed"
    assert row["last_changed_at"] > before
    assert row["last_changed_at"] == row["committed_at"] == latest_written(row)
    assert row["last_changed_at"] != row["paid_at"]


def test_a_late_commit_sets_it_to_committed_at(client, engine, caller):
    created, _ = expired_order(client, engine, caller, [2])
    reservation_id = created["reservation_id"]
    before = changed_at(engine, caller, reservation_id)

    body = commit_body(created, paid_at=FUTURE_PAID_AT)
    data = data_of(commit(client, caller, created["order_ref"], body), 200)
    assert data["late"] is True
    row = stored(engine, caller, reservation_id)
    assert row["last_changed_at"] > before
    # 关单在之前的事务里：本事务只写 committed_at。
    assert row["closed_at"] < row["committed_at"]
    assert row["last_changed_at"] == row["committed_at"] == latest_written(row)


def test_going_to_review_after_closing_sets_it_to_the_command_time(client, engine, caller):
    """已关单的预留第一次转入待审不写四个时间列：取本命令的时间，不取 paid_at。"""
    created, (sku,) = expired_order(client, engine, caller, [2])
    reservation_id = created["reservation_id"]
    set_unavailable(engine, caller, sku, 4)
    before = stored(engine, caller, reservation_id)
    started = now(engine)

    body = commit_body(created, paid_at=FUTURE_PAID_AT)
    response = commit(client, caller, created["order_ref"], body)
    assert error_of(response, 409)["code"] == LATE_PAYMENT_INSUFFICIENT_STOCK
    finished = now(engine)

    row = stored(engine, caller, reservation_id)
    assert row["status"] == "late_payment_review"
    assert row["paid_at"] == FUTURE_PAID_AT_UTC
    assert latest_written(row) == latest_written(before)
    assert row["last_changed_at"] > before["last_changed_at"]
    assert started < row["last_changed_at"] < finished
    assert row["last_changed_at"] != row["paid_at"]


def test_closing_a_review_sets_it_to_review_closed_at(client, engine, caller):
    created, _, _ = order_in_review(client, engine, caller)
    reservation_id = created["reservation_id"]
    before = changed_at(engine, caller, reservation_id)

    data = data_of(close(client, caller, created["order_ref"]), 200)
    assert set(data) == RESERVATION_KEYS
    row = stored(engine, caller, reservation_id)
    assert row["status"] == "closed_refunded"
    assert row["last_changed_at"] > before
    assert row["last_changed_at"] == row["review_closed_at"] == latest_written(row)


# --- 到期未关的预留在确认销售的同一事务里先关单 ---


def test_closing_then_committing_late_in_one_transaction_takes_the_closing_time(
    client, engine, caller, unclosed
):
    created, _ = reserved_order(client, engine, caller, [2])
    reservation_id = created["reservation_id"]
    shift_into_the_past(engine, reservation_id)
    before = changed_at(engine, caller, reservation_id)

    body = commit_body(created, paid_at=FUTURE_PAID_AT)
    data = data_of(commit(client, caller, created["order_ref"], body), 200)
    assert data["late"] is True
    row = stored(engine, caller, reservation_id)
    assert row["last_changed_at"] > before
    # 关单取的时间晚于命令开头取的、写进 committed_at 的时间。
    assert row["closed_at"] >= row["committed_at"]
    assert row["last_changed_at"] == row["closed_at"] == latest_written(row)


def test_closing_then_going_to_review_in_one_transaction_takes_the_closing_time(
    client, engine, caller, unclosed, monkeypatch
):
    """照常调用时关单之后一定够；这里把判可售看到的可售量换成 0（同晚到确认的测试）。"""

    def short_of_everything(lines, available_by_sku):
        return sku_shortfalls(lines, dict.fromkeys(available_by_sku, 0))

    monkeypatch.setattr(sale_commit_service, "sku_shortfalls", short_of_everything)
    created, _ = reserved_order(client, engine, caller, [2])
    reservation_id = created["reservation_id"]
    shift_into_the_past(engine, reservation_id)
    before = changed_at(engine, caller, reservation_id)

    body = commit_body(created, paid_at=FUTURE_PAID_AT)
    response = commit(client, caller, created["order_ref"], body)
    assert error_of(response, 409)["code"] == LATE_PAYMENT_INSUFFICIENT_STOCK
    row = stored(engine, caller, reservation_id)
    assert row["status"] == "late_payment_review"
    assert row["last_changed_at"] > before
    assert row["last_changed_at"] == row["closed_at"] == latest_written(row)


# --- 付款后的单据 ---


@pytest.mark.parametrize("kind", [DISPATCHES, REFUND_RESTOCKS])
def test_a_dispatch_or_refund_restock_sets_it_to_the_document_time(client, engine, caller, kind):
    created, _ = committed_order(client, engine, caller, [2])
    reservation_id = created["reservation_id"]
    before = stored(engine, caller, reservation_id)

    body = body_of(kind, [qline("l1", 1)])
    data = data_of(post(client, caller, kind, created["order_ref"], body), 201)
    [document] = documents(engine, caller)
    assert document["document_id"] == data["document_id"]
    row = stored(engine, caller, reservation_id)
    assert row["last_changed_at"] > before["last_changed_at"]
    assert row["last_changed_at"] == document["created_at"]
    # 不写预留的时间列。
    assert latest_written(row) == latest_written(before)


def test_a_return_receipt_sets_it_to_the_document_time(client, engine, caller):
    created, _ = dispatched_order(client, engine, caller, [2])
    reservation_id = created["reservation_id"]
    before = changed_at(engine, caller, reservation_id)

    body = return_body([rline("l1", 1, 1)])
    data = data_of(receive(client, caller, created["order_ref"], body), 201)
    [receipt] = receipts(engine, caller)
    assert receipt["document_id"] == data["document_id"]
    row = stored(engine, caller, reservation_id)
    assert row["last_changed_at"] > before
    assert row["last_changed_at"] == receipt["created_at"]


# --- 不改列的路径不设 ---


def test_replays_and_the_same_order_on_a_new_key_leave_it(client, engine, caller):
    created, (sku,) = reserved_order(client, engine, caller, [1])
    reservation_id = created["reservation_id"]
    body = order([line("l1", sku, 1)], created["order_ref"])
    before = changed_at(engine, caller, reservation_id)
    # 同内容换键重报预留：200 与已有的预留。
    assert data_of(reserve(client, caller, body), 200)["reservation_id"] == reservation_id
    assert changed_at(engine, caller, reservation_id) == before

    paid = commit_body(created)
    key = new_key()
    data_of(commit(client, caller, created["order_ref"], paid, key), 200)
    before = changed_at(engine, caller, reservation_id)
    # 同键重放与换键同内容重报确认。
    replay = commit(client, caller, created["order_ref"], paid, key)
    assert replay.headers["Idempotency-Replayed"] == "true"
    data_of(commit(client, caller, created["order_ref"], paid), 200)
    assert changed_at(engine, caller, reservation_id) == before

    shipment = body_of(DISPATCHES, [qline("l1", 1)])
    key = new_key()
    data_of(post(client, caller, DISPATCHES, created["order_ref"], shipment, key), 201)
    before = changed_at(engine, caller, reservation_id)
    # 同键重放与换键同内容重报发货。
    replay = post(client, caller, DISPATCHES, created["order_ref"], shipment, key)
    assert replay.headers["Idempotency-Replayed"] == "true"
    data_of(post(client, caller, DISPATCHES, created["order_ref"], shipment), 200)
    assert changed_at(engine, caller, reservation_id) == before


def test_releasing_a_released_or_expired_reservation_again_leaves_it(client, engine, caller):
    released_order, _ = reserved_order(client, engine, caller, [1])
    expired, _ = expired_order(client, engine, caller, [1])
    data_of(release(client, caller, released_order["reservation_id"], CANCELLED), 200)
    for created in (released_order, expired):
        reservation_id = created["reservation_id"]
        before = changed_at(engine, caller, reservation_id)
        data_of(release(client, caller, reservation_id, CANCELLED), 200)
        assert changed_at(engine, caller, reservation_id) == before


def test_a_review_retry_still_short_leaves_it(client, engine, caller):
    created, _, paid = order_in_review(client, engine, caller)
    reservation_id = created["reservation_id"]
    before = stored(engine, caller, reservation_id)

    retry = commit_body(created, paid["payment_ref"], paid_at=FUTURE_PAID_AT)
    response = commit(client, caller, created["order_ref"], retry)
    assert error_of(response, 409)["code"] == LATE_PAYMENT_INSUFFICIENT_STOCK
    assert stored(engine, caller, reservation_id) == before


def test_closing_a_closed_review_again_leaves_it(client, engine, caller):
    created, _, _ = order_in_review(client, engine, caller)
    reservation_id = created["reservation_id"]
    data_of(close(client, caller, created["order_ref"]), 200)
    before = changed_at(engine, caller, reservation_id)

    data_of(close(client, caller, created["order_ref"]), 200)
    assert changed_at(engine, caller, reservation_id) == before
