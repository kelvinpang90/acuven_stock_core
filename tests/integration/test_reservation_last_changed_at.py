"""预留的最后变更时间 last_changed_at（契约 §5.9 最后变更时间与存放）。

接口测试在 demo 库上跑；每个测试建自己的租户、池、销售入口与凭据，池先切到 live，预留经接口建立。
到期由测试把预留挪到过去；已到期未关的预留要留到确认销售的业务事务里关时，
把开事务前钩子里关单那一步换成空操作（同 test_sale_commit_late_api.py 的 unclosed）。

回填在 production 测试库上验证（同 test_demo_reset_schema.py）：降到 0009 造数、升到 head 后核对，
无论成败都在 finally 里升回 head，再删掉造的行；不动共享的 demo 库。
"""

from collections.abc import Iterator
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, delete, insert, select

from alembic import command
from app.domain.environment import PRODUCTION
from app.domain.sale_commits import sku_shortfalls
from app.models.catalog import Tenant
from app.models.inventory import Location, SalesChannel, StockPool
from app.models.orders import OrderDocument, Reservation
from app.models.service_auth import Credential
from app.services import sale_commits as sale_commit_service
from app.services.reservations import database_now, sweep_expired_reservations
from tests.integration.conftest import (
    DISPATCHES,
    REFUND_RESTOCKS,
    body_of,
    committed_order,
    documents,
    new_id,
    post,
    qline,
)
from tests.integration.test_api_auth import Caller, error_of
from tests.integration.test_demo_reset_schema import (
    Shop,
    add_shop,
    alembic_version,
    migrate_production,
)
from tests.integration.test_late_payment_review_api import close, order_in_review
from tests.integration.test_reservation_release_api import release, reserved_order
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
    receive,
    return_body,
    rline,
)
from tests.integration.test_sale_commit_fault_api import fault_body, stocked_skus
from tests.integration.test_sale_commit_late_api import expired_order, set_unavailable
from tests.integration.test_sale_commits_api import commit, commit_body, stored_reservation

LATE_PAYMENT_INSUFFICIENT_STOCK = "LATE_PAYMENT_INSUFFICIENT_STOCK"
# 契约 5.9 的最后变更时间只看这几列；expires_at 与 paid_at 不算。
WRITE_TIMES = ("created_at", "closed_at", "committed_at", "review_closed_at")
# 调用方给的付款时间放在远的将来：最后变更时间不取它。
FUTURE_PAID_AT = "2099-01-01T00:00:00Z"
FUTURE_PAID_AT_UTC = datetime(2099, 1, 1)


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
def unclosed(monkeypatch) -> None:
    """确认销售开事务前钩子里的关单换成空操作：已到期未关的预留留到业务事务里关。"""

    def skip(connection, tenant_id, pool_id, sku_ids) -> int:
        return 0

    monkeypatch.setattr(sale_commit_service, "close_due_before_reserving", skip)


@pytest.fixture
def short(monkeypatch) -> None:
    """晚到确认判可售看到的可售量换成 0：关单放回的正是这一单的数量，按库里的数一定够。"""

    def short_of_everything(lines, available_by_sku):
        return sku_shortfalls(lines, dict.fromkeys(available_by_sku, 0))

    monkeypatch.setattr(sale_commit_service, "sku_shortfalls", short_of_everything)


# --- 请求与观察 ---


def new_key(kind: str) -> str:
    return f"shop:{kind}:{uuid4().hex}"


def last_changed_at(engine: Engine, caller: Caller, reservation_id: str) -> datetime:
    return stored_reservation(engine, caller, reservation_id)["last_changed_at"]


def latest_write(row: dict) -> datetime:
    """预留行的 created_at、closed_at、committed_at、review_closed_at 中最晚的一个。"""
    return max(row[column] for column in WRITE_TIMES if row[column] is not None)


def now_of(engine: Engine) -> datetime:
    with engine.connect() as db:
        return database_now(db)


def assert_unchanged_representation(data: dict) -> None:
    """last_changed_at 不进预留的表示（§5.9）。"""
    assert set(data) == RESERVATION_KEYS
    assert "last_changed_at" not in data


# --- 新建 ---


def test_a_new_reservation_is_last_changed_at_its_creation(client, engine, caller):
    created, _ = reserved_order(client, engine, caller, [2, 1])
    assert_unchanged_representation(created)
    row = stored_reservation(engine, caller, created["reservation_id"])
    assert row["last_changed_at"] == row["created_at"]


def test_a_fault_commit_with_stock_is_last_changed_at_its_creation(client, engine, caller):
    (sku,) = stocked_skus(engine, caller, 5)
    response = commit(client, caller, new_ref(), fault_body([line("l1", sku, 2)]))
    data = data_of(response, 200)
    assert_unchanged_representation(data)
    row = stored_reservation(engine, caller, data["reservation_id"])
    assert row["status"] == "committed"
    assert row["last_changed_at"] == row["created_at"] == row["committed_at"]


def test_a_fault_commit_without_stock_is_last_changed_at_its_creation(client, engine, caller):
    (sku,) = stocked_skus(engine, caller, 1)
    body = fault_body([line("l1", sku, 2)], paid_at=FUTURE_PAID_AT)
    response = commit(client, caller, new_ref(), body)
    assert error_of(response, 409)["code"] == LATE_PAYMENT_INSUFFICIENT_STOCK
    [row] = rows_of(engine, Reservation, caller)
    assert row["status"] == "late_payment_review"
    assert row["last_changed_at"] == row["created_at"] == row["closed_at"]


# --- 释放与到期关单 ---


def test_releasing_sets_it_to_the_release_time(client, engine, caller):
    created, _ = reserved_order(client, engine, caller, [2])
    reservation_id = created["reservation_id"]
    before = last_changed_at(engine, caller, reservation_id)

    data = data_of(release(client, caller, reservation_id, {"reason": "cancelled"}), 200)
    assert_unchanged_representation(data)
    row = stored_reservation(engine, caller, reservation_id)
    assert row["status"] == "released"
    assert row["last_changed_at"] > before
    assert row["last_changed_at"] == row["closed_at"] == latest_write(row)


def test_the_periodic_sweep_sets_it_to_the_closing_time(client, engine, caller):
    created, _ = reserved_order(client, engine, caller, [2])
    reservation_id = created["reservation_id"]
    shift_into_the_past(engine, reservation_id)
    before = last_changed_at(engine, caller, reservation_id)

    assert sweep_expired_reservations(engine) >= 1
    row = stored_reservation(engine, caller, reservation_id)
    assert row["status"] == "expired"
    assert row["last_changed_at"] > before
    assert row["last_changed_at"] == row["closed_at"] == latest_write(row)


def test_releasing_a_due_reservation_sets_it_to_the_closing_time(client, engine, caller):
    created, _ = reserved_order(client, engine, caller, [2])
    reservation_id = created["reservation_id"]
    shift_into_the_past(engine, reservation_id)
    before = last_changed_at(engine, caller, reservation_id)

    data = data_of(release(client, caller, reservation_id, {"reason": "cancelled"}), 200)
    assert data["status"] == "expired"
    row = stored_reservation(engine, caller, reservation_id)
    assert row["status"] == "expired"
    assert row["last_changed_at"] > before
    assert row["last_changed_at"] == row["closed_at"] == latest_write(row)


# --- 确认销售 ---


def test_a_commit_sets_it_to_the_commit_time(client, engine, caller):
    created, _ = reserved_order(client, engine, caller, [2])
    reservation_id = created["reservation_id"]
    before = last_changed_at(engine, caller, reservation_id)

    body = commit_body(created, paid_at=FUTURE_PAID_AT)
    data = data_of(commit(client, caller, created["order_ref"], body), 200)
    assert_unchanged_representation(data)
    row = stored_reservation(engine, caller, reservation_id)
    assert row["status"] == "committed"
    assert row["last_changed_at"] > before
    assert row["last_changed_at"] == row["committed_at"] == latest_write(row)
    assert row["last_changed_at"] != row["paid_at"]


def test_a_late_commit_sets_it_to_the_commit_time(client, engine, caller):
    created, _ = expired_order(client, engine, caller, [2])
    reservation_id = created["reservation_id"]
    before = last_changed_at(engine, caller, reservation_id)

    body = commit_body(created, paid_at=FUTURE_PAID_AT)
    data = data_of(commit(client, caller, created["order_ref"], body), 200)
    assert data["late"] is True
    row = stored_reservation(engine, caller, reservation_id)
    assert row["last_changed_at"] > before
    assert row["last_changed_at"] == row["committed_at"] == latest_write(row)


def test_a_due_reservation_closed_before_the_transaction_takes_the_commit_time(
    client, engine, caller
):
    """照常调用：开事务前的先关到期在另一个事务里关单，确认只写 committed_at。"""
    created, _ = reserved_order(client, engine, caller, [2])
    reservation_id = created["reservation_id"]
    shift_into_the_past(engine, reservation_id)

    data = data_of(commit(client, caller, created["order_ref"], commit_body(created)), 200)
    assert data["late"] is True
    row = stored_reservation(engine, caller, reservation_id)
    assert row["committed_at"] > row["closed_at"]
    assert row["last_changed_at"] == row["committed_at"] == latest_write(row)


def test_going_to_review_takes_the_command_time_not_paid_at(client, engine, caller):
    """已关单的预留第一次转入待审不写时间列：取本命令的时间，不早于转入之前的值。"""
    created, (sku,) = expired_order(client, engine, caller, [2])
    reservation_id = created["reservation_id"]
    set_unavailable(engine, caller, sku, 4)
    before = stored_reservation(engine, caller, reservation_id)

    body = commit_body(created, paid_at=FUTURE_PAID_AT)
    response = commit(client, caller, created["order_ref"], body)
    after_command = now_of(engine)
    assert error_of(response, 409)["code"] == LATE_PAYMENT_INSUFFICIENT_STOCK

    row = stored_reservation(engine, caller, reservation_id)
    assert row["status"] == "late_payment_review"
    assert row["paid_at"] == FUTURE_PAID_AT_UTC
    # 时间列都没写。
    for column in WRITE_TIMES:
        assert row[column] == before[column]
    assert before["last_changed_at"] < row["last_changed_at"] <= after_command
    assert row["last_changed_at"] > latest_write(row)


def test_a_due_reservation_going_to_review_normally_takes_the_command_time(
    client, engine, caller, short
):
    """照常调用：先关到期在另一个事务里，转入待审的事务不写时间列，取本命令的时间。"""
    created, _ = reserved_order(client, engine, caller, [2])
    reservation_id = created["reservation_id"]
    shift_into_the_past(engine, reservation_id)

    body = commit_body(created, paid_at=FUTURE_PAID_AT)
    response = commit(client, caller, created["order_ref"], body)
    after_command = now_of(engine)
    assert error_of(response, 409)["code"] == LATE_PAYMENT_INSUFFICIENT_STOCK

    row = stored_reservation(engine, caller, reservation_id)
    assert row["status"] == "late_payment_review"
    assert row["closed_at"] < row["last_changed_at"] <= after_command
    assert row["last_changed_at"] != row["paid_at"]


# --- 已到期未关：确认销售的同一事务里先关单 ---


def test_closing_and_committing_late_in_one_transaction_takes_the_closing_time(
    client, engine, caller, unclosed
):
    created, _ = reserved_order(client, engine, caller, [2])
    reservation_id = created["reservation_id"]
    shift_into_the_past(engine, reservation_id)
    before = last_changed_at(engine, caller, reservation_id)

    body = commit_body(created, paid_at=FUTURE_PAID_AT)
    data = data_of(commit(client, caller, created["order_ref"], body), 200)
    assert data["late"] is True
    row = stored_reservation(engine, caller, reservation_id)
    assert row["status"] == "committed"
    # 关单的数据库时间在命令时间之后取。
    assert row["closed_at"] >= row["committed_at"]
    assert row["last_changed_at"] > before
    assert row["last_changed_at"] == row["closed_at"] == latest_write(row)


def test_closing_and_going_to_review_in_one_transaction_takes_the_closing_time(
    client, engine, caller, unclosed, short
):
    created, _ = reserved_order(client, engine, caller, [2])
    reservation_id = created["reservation_id"]
    shift_into_the_past(engine, reservation_id)
    before = last_changed_at(engine, caller, reservation_id)

    body = commit_body(created, paid_at=FUTURE_PAID_AT)
    response = commit(client, caller, created["order_ref"], body)
    assert error_of(response, 409)["code"] == LATE_PAYMENT_INSUFFICIENT_STOCK
    row = stored_reservation(engine, caller, reservation_id)
    assert row["status"] == "late_payment_review"
    assert row["last_changed_at"] > before
    assert row["last_changed_at"] == row["closed_at"] == latest_write(row)


# --- 待审结案 ---


def test_closing_a_review_sets_it_to_the_review_closing_time(client, engine, caller):
    created, _, _ = order_in_review(client, engine, caller)
    reservation_id = created["reservation_id"]
    before = last_changed_at(engine, caller, reservation_id)

    data = data_of(close(client, caller, created["order_ref"]), 200)
    assert_unchanged_representation(data)
    row = stored_reservation(engine, caller, reservation_id)
    assert row["status"] == "closed_refunded"
    assert row["last_changed_at"] > before
    assert row["last_changed_at"] == row["review_closed_at"] == latest_write(row)


# --- 付款后的命令 ---


def last_document_created_at(engine: Engine, caller: Caller) -> datetime:
    return documents(engine, caller)[-1]["created_at"]


@pytest.mark.parametrize("kind", [DISPATCHES, REFUND_RESTOCKS])
def test_a_dispatch_or_refund_restock_sets_it_to_the_document_time(client, engine, caller, kind):
    created, _ = committed_order(client, engine, caller, [2])
    reservation_id = created["reservation_id"]
    before = last_changed_at(engine, caller, reservation_id)

    body = body_of(kind, [qline("l1", 1)])
    data_of(post(client, caller, kind, created["order_ref"], body), 201)
    row = stored_reservation(engine, caller, reservation_id)
    assert row["last_changed_at"] > before
    assert row["last_changed_at"] == last_document_created_at(engine, caller)


def test_a_return_receipt_sets_it_to_the_document_time(client, engine, caller):
    created, _ = dispatched_order(client, engine, caller, [2])
    reservation_id = created["reservation_id"]
    before = last_changed_at(engine, caller, reservation_id)

    data_of(receive(client, caller, created["order_ref"], return_body([rline("l1", 1, 1)])), 201)
    row = stored_reservation(engine, caller, reservation_id)
    assert row["last_changed_at"] > before
    assert row["last_changed_at"] == last_document_created_at(engine, caller)


# --- 不改任何列的路径不设 ---


def test_replaying_or_reporting_a_reservation_again_leaves_it(client, engine, caller):
    (sku,) = stocked_skus(engine, caller, 5)
    body = order([line("l1", sku, 2)])
    key = new_key("reserve")
    created = data_of(reserve(client, caller, body, key), 201)
    before = last_changed_at(engine, caller, created["reservation_id"])

    replay = reserve(client, caller, body, key)
    assert replay.headers["Idempotency-Replayed"] == "true"
    data_of(reserve(client, caller, body), 200)
    assert last_changed_at(engine, caller, created["reservation_id"]) == before


def test_replaying_or_reporting_a_commit_again_leaves_it(client, engine, caller):
    created, _ = reserved_order(client, engine, caller, [2])
    body = commit_body(created)
    key = new_key("commit")
    data_of(commit(client, caller, created["order_ref"], body, key), 200)
    before = last_changed_at(engine, caller, created["reservation_id"])

    replay = commit(client, caller, created["order_ref"], body, key)
    assert replay.headers["Idempotency-Replayed"] == "true"
    data_of(commit(client, caller, created["order_ref"], body), 200)
    assert last_changed_at(engine, caller, created["reservation_id"]) == before


def test_replaying_or_reporting_a_document_again_leaves_it(client, engine, caller):
    created, _ = committed_order(client, engine, caller, [2])
    body = body_of(DISPATCHES, [qline("l1", 1)])
    key = new_key("fulfil")
    data_of(post(client, caller, DISPATCHES, created["order_ref"], body, key), 201)
    before = last_changed_at(engine, caller, created["reservation_id"])

    replay = post(client, caller, DISPATCHES, created["order_ref"], body, key)
    assert replay.headers["Idempotency-Replayed"] == "true"
    data_of(post(client, caller, DISPATCHES, created["order_ref"], body), 200)
    assert last_changed_at(engine, caller, created["reservation_id"]) == before


def test_releasing_a_released_or_expired_reservation_again_leaves_it(client, engine, caller):
    released, _ = reserved_order(client, engine, caller, [2])
    data_of(release(client, caller, released["reservation_id"], {"reason": "cancelled"}), 200)
    expired, _ = expired_order(client, engine, caller, [1])
    for reservation_id in (released["reservation_id"], expired["reservation_id"]):
        before = last_changed_at(engine, caller, reservation_id)
        data_of(release(client, caller, reservation_id, {"reason": "payment_failed"}), 200)
        assert last_changed_at(engine, caller, reservation_id) == before


def test_a_review_retry_still_short_leaves_it(client, engine, caller):
    created, _, paid = order_in_review(client, engine, caller)
    reservation_id = created["reservation_id"]
    before = last_changed_at(engine, caller, reservation_id)

    retry = commit_body(created, paid["payment_ref"], paid_at=FUTURE_PAID_AT)
    response = commit(client, caller, created["order_ref"], retry)
    assert error_of(response, 409)["code"] == LATE_PAYMENT_INSUFFICIENT_STOCK
    assert last_changed_at(engine, caller, reservation_id) == before


def test_closing_a_closed_review_again_leaves_it(client, engine, caller):
    created, _, _ = order_in_review(client, engine, caller)
    reservation_id = created["reservation_id"]
    data_of(close(client, caller, created["order_ref"]), 200)
    before = last_changed_at(engine, caller, reservation_id)

    data_of(close(client, caller, created["order_ref"]), 200)
    assert last_changed_at(engine, caller, reservation_id) == before


# --- 迁移回填（production 测试库） ---

T0 = datetime(2026, 11, 10, 8, 0)
REQUEST_HASH = "0" * 64


def minutes(count: int) -> datetime:
    return T0 + timedelta(minutes=count)


def reservation_values(shop: Shop, status: str, **values: object) -> dict:
    """0009 结构上的一个预留；默认 active，到期时间不算进最后变更时间。"""
    return {
        "reservation_id": new_id("rsv"),
        "tenant_id": shop.tenant_id,
        "channel_id": shop.channel_id,
        "pool_id": shop.pool_id,
        "order_ref": f"order-{uuid4().hex}",
        "status": status,
        "ttl_seconds": 900,
        "expires_at": minutes(15),
        "request_hash": REQUEST_HASH,
        "credential_id": shop.credential_id,
        "request_id": new_id("req"),
        "created_at": T0,
    } | values


def paid_values(shop: Shop, **values: object) -> dict:
    """付款之后的三种状态要有的列；paid_at 放在远的将来，回填不取它。"""
    return {
        "payment_ref": f"pay-{uuid4().hex}",
        "paid_at": FUTURE_PAID_AT_UTC,
        "commit_request_hash": REQUEST_HASH,
        "commit_credential_id": shop.credential_id,
        "commit_request_id": new_id("req"),
    } | values


def add_document(
    conn: Connection, shop: Shop, reservation_id: str, document_type: str, created_at: datetime
) -> None:
    conn.execute(
        insert(OrderDocument).values(
            document_id=new_id("doc"),
            tenant_id=shop.tenant_id,
            channel_id=shop.channel_id,
            pool_id=shop.pool_id,
            reservation_id=reservation_id,
            document_type=document_type,
            document_ref=f"doc-{uuid4().hex}",
            request_hash=REQUEST_HASH,
            result={"document": "backfill"},
            credential_id=shop.credential_id,
            request_id=new_id("req"),
            created_at=created_at,
        )
    )


def seed_reservations(conn: Connection, shop: Shop) -> dict[str, datetime]:
    """造几种预留，返回各自回填后应有的 last_changed_at。"""
    expected: dict[str, datetime] = {}

    def add(values: dict, last_changed_at: datetime) -> str:
        conn.execute(insert(Reservation).values(values))
        expected[values["reservation_id"]] = last_changed_at
        return values["reservation_id"]

    add(reservation_values(shop, "active"), T0)
    released = {
        "release_reason": "cancelled",
        "released_credential_id": shop.credential_id,
        "released_request_id": new_id("req"),
        "closed_at": minutes(5),
    }
    add(reservation_values(shop, "released", **released), minutes(5))
    # 经到期再晚到确认：committed_at 晚于 closed_at。
    late = paid_values(shop, closed_at=minutes(20), committed_at=minutes(30), late=True)
    add(reservation_values(shop, "committed", **late), minutes(30))
    # 带单据的取最晚一张单据的 created_at。
    committed = paid_values(shop, closed_at=minutes(10), committed_at=minutes(10))
    with_documents = add(reservation_values(shop, "committed", **committed), minutes(120))
    add_document(conn, shop, with_documents, "dispatch", minutes(60))
    add_document(conn, shop, with_documents, "refund_restock", minutes(120))
    # 待审：转入的时刻没有单独记，回填取关单时间。
    review = paid_values(shop, closed_at=minutes(20))
    add(reservation_values(shop, "late_payment_review", **review), minutes(20))
    refunded = paid_values(
        shop,
        closed_at=minutes(20),
        review_closed_at=minutes(180),
        review_closed_credential_id=shop.credential_id,
        review_closed_request_id=new_id("req"),
    )
    add(reservation_values(shop, "closed_refunded", **refunded), minutes(180))
    return expected


def remove_shop(engine: Engine, tenant_id: str) -> None:
    """删掉回填测试造的行，按外键的依赖倒序。"""
    with engine.begin() as conn:
        for model in (OrderDocument, Reservation, SalesChannel, StockPool, Location, Credential):
            conn.execute(delete(model).where(model.tenant_id == tenant_id))
        conn.execute(delete(Tenant).where(Tenant.tenant_id == tenant_id))


def test_the_migration_backfills_the_latest_time_column_or_document(production_engine: Engine):
    tenant_id: str | None = None
    try:
        migrate_production(command.downgrade, "0009", PRODUCTION)
        assert alembic_version(production_engine) == "0009"
        # 0009 的结构上造数并提交：升级时的回填要看到它们。
        with production_engine.begin() as conn:
            shop = add_shop(conn)
            tenant_id = shop.tenant_id
            expected = seed_reservations(conn, shop)

        migrate_production(command.upgrade, "head", PRODUCTION)
        statement = select(Reservation.reservation_id, Reservation.last_changed_at).where(
            Reservation.tenant_id == shop.tenant_id
        )
        with production_engine.connect() as conn:
            backfilled = dict(conn.execute(statement).tuples().all())
        assert backfilled == expected
    finally:
        migrate_production(command.upgrade, "head", PRODUCTION)
        if tenant_id is not None:
            remove_shop(production_engine, tenant_id)
