"""订单库存查询：预留的表示、每行累计量、单据分页与各种拒绝（契约 §5.8、§5.1、§2.5）。

在真 MySQL 上跑；每个测试建自己的租户、池、销售入口与凭据，池先切到 live，
预留、确认销售、发货、退款回库与退货验收经接口建立；到期由测试直接改库模拟。
"""

from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from app.domain.fulfilment import format_order_stock_cursor
from app.domain.service_auth import ORDERS_WRITE, generate_token, token_digest
from app.models.inventory import SalesChannel
from app.models.orders import Reservation
from tests.integration.conftest import (
    DISPATCHES,
    REFUND_RESTOCKS,
    add_channel,
    add_credential,
    add_pool,
    body_of,
    documents,
    post,
    qline,
)
from tests.integration.test_api_auth import Caller, error_of, events
from tests.integration.test_idempotency import add_skus, rid_of
from tests.integration.test_reservation_release_api import reserved_order
from tests.integration.test_reservations_api import (
    ORDER_SCOPES,
    data_of,
    line,
    live_caller,
    make_client,
    new_ref,
    order,
    reserve,
    rows_of,
    shift_into_the_past,
    stock,
)
from tests.integration.test_return_receipts_api import RETURN_RECEIPTS, return_body, rline
from tests.integration.test_sale_commits_api import commit, commit_body
from tests.integration.test_service_auth_schema import token_insert
from tests.integration.test_stock_imports_api import set_live

LINE_KEYS = {
    "line_ref",
    "sku_id",
    "quantity",
    "committed_qty",
    "dispatched_qty",
    "refund_restocked_qty",
    "returned_sellable_qty",
    "returned_unsellable_qty",
}


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


# --- 请求与观察 ---


def order_stock(client: TestClient, caller: Caller, order_ref: str, params=None):
    headers = caller.auth | {"X-Request-ID": f"order-stock-{uuid4().hex}"}
    return client.get(f"/v1/orders/{order_ref}/stock", params=params, headers=headers)


def totals(
    line_ref: str,
    sku_id: str,
    quantity: int,
    committed: int = 0,
    dispatched: int = 0,
    refund_restocked: int = 0,
    returned_sellable: int = 0,
    returned_unsellable: int = 0,
) -> dict:
    return {
        "line_ref": line_ref,
        "sku_id": sku_id,
        "quantity": quantity,
        "committed_qty": committed,
        "dispatched_qty": dispatched,
        "refund_restocked_qty": refund_restocked,
        "returned_sellable_qty": returned_sellable,
        "returned_unsellable_qty": returned_unsellable,
    }


def committed(client: TestClient, engine: Engine, caller: Caller, quantities: list[int]):
    """预留并确认；返回确认的 200 响应体的 data 与按行的 SKU。"""
    created, skus = reserved_order(client, engine, caller, quantities)
    confirmed = data_of(commit(client, caller, created["order_ref"], commit_body(created)), 200)
    return confirmed, skus


def dispatch(client: TestClient, caller: Caller, order_ref: str, lines: list[dict]) -> dict:
    return data_of(post(client, caller, DISPATCHES, order_ref, body_of(DISPATCHES, lines)), 201)


def credential_for(
    engine: Engine, tenant_id: str, pool_id: str, channel_id: str, scopes: list[str]
) -> Caller:
    """同一租户里绑定 channel_id 的另一个凭据与令牌。"""
    plain = generate_token()
    with engine.begin() as db:
        credential_id = add_credential(db, tenant_id, channel_id=channel_id, scopes=scopes)
        db.execute(token_insert(tenant_id, credential_id, token_digest=token_digest(plain)))
    return Caller(tenant_id, pool_id, credential_id, plain)


def channel_of(engine: Engine, caller: Caller) -> str:
    with engine.connect() as db:
        statement = select(SalesChannel.channel_id).where(SalesChannel.pool_id == caller.pool_id)
        return db.execute(statement).scalar_one()


def other_channel(engine: Engine, caller: Caller) -> Caller:
    """同一租户的另一个池（已 live）、绑定它的另一个销售入口与凭据。"""
    with engine.begin() as db:
        pool_id = add_pool(db, caller.tenant_id)
        channel_id = add_channel(db, caller.tenant_id, pool_id)
    other = credential_for(engine, caller.tenant_id, pool_id, channel_id, ORDER_SCOPES)
    set_live(engine, other)
    return other


# --- 查询 ---


def test_an_order_lists_its_documents_and_the_line_totals(client, engine, caller):
    confirmed, (first_sku, second_sku) = committed(client, engine, caller, [3, 2])
    order_ref = confirmed["order_ref"]
    reservation_id = confirmed["reservation_id"]
    shipped = dispatch(client, caller, order_ref, [qline("l1", 2), qline("l2", 2)])
    refund = body_of(REFUND_RESTOCKS, [qline("l1", 1)])
    refunded = data_of(post(client, caller, REFUND_RESTOCKS, order_ref, refund), 201)
    receipt = return_body([rline("l2", 1, 1)])
    returned = data_of(post(client, caller, RETURN_RECEIPTS, order_ref, receipt), 201)

    response = order_stock(client, caller, order_ref)
    data = data_of(response, 200)
    assert set(data) == {"reservation", "lines", "documents", "next_cursor"}
    assert data["reservation"] == confirmed
    assert data["reservation"]["status"] == "committed"
    assert all(set(row) == LINE_KEYS for row in data["lines"])
    assert data["lines"] == [
        totals("l1", first_sku, 3, committed=3, dispatched=2, refund_restocked=1),
        totals(
            "l2",
            second_sku,
            2,
            committed=2,
            dispatched=2,
            returned_sellable=1,
            returned_unsellable=1,
        ),
    ]
    # 按生效先后，每项与该命令 201 响应体的 data 相同。
    assert data["documents"] == [shipped, refunded, returned]
    last = documents(engine, caller)[-1]
    assert last["document_id"] == returned["document_id"]
    expected = format_order_stock_cursor(last["document_seq"], reservation_id)
    assert data["next_cursor"] == expected
    assert events(engine, rid_of(response)) == []

    # 用最后一张的游标再查：没有新单据，next_cursor 原样返回。
    again = data_of(order_stock(client, caller, order_ref, {"after": expected}), 200)
    assert again == data | {"documents": []}


def test_an_unconfirmed_order_has_zero_totals_and_no_documents(client, engine, caller):
    created, (first_sku, second_sku) = reserved_order(client, engine, caller, [2, 1])
    order_ref = created["order_ref"]
    data = data_of(order_stock(client, caller, order_ref), 200)
    assert data == {
        "reservation": created,
        "lines": [totals("l1", first_sku, 2), totals("l2", second_sku, 1)],
        "documents": [],
        "next_cursor": None,
    }
    assert data["reservation"]["status"] == "active"

    # 已到期还没关掉的 active 显示为 expired；查询不写库。
    shift_into_the_past(engine, created["reservation_id"])
    due = data_of(order_stock(client, caller, order_ref), 200)
    assert due["reservation"]["status"] == "expired"
    assert due["reservation"]["released_at"] is None
    assert due["lines"] == data["lines"]
    assert (due["documents"], due["next_cursor"]) == ([], None)
    [stored] = rows_of(engine, Reservation, caller)
    assert stored["status"] == "active"


def test_documents_page_by_limit_without_gaps_or_repeats(client, engine, caller):
    confirmed, (sku,) = committed(client, engine, caller, [5])
    order_ref = confirmed["order_ref"]
    reservation_id = confirmed["reservation_id"]
    issued = [dispatch(client, caller, order_ref, [qline("l1", 1)]) for _ in range(5)]
    seqs = [document["document_seq"] for document in documents(engine, caller)]
    assert len(seqs) == 5
    lines = [totals("l1", sku, 5, committed=5, dispatched=5)]

    pages = []
    after = None
    for _ in range(3):
        params = {"limit": "2"} | ({} if after is None else {"after": after})
        page = data_of(order_stock(client, caller, order_ref, params), 200)
        # 每页都带 reservation 与 lines。
        assert page["reservation"] == confirmed
        assert page["lines"] == lines
        pages.append(page["documents"])
        after = page["next_cursor"]
    assert [len(page) for page in pages] == [2, 2, 1]
    assert [document for page in pages for document in page] == issued
    assert after == format_order_stock_cursor(seqs[-1], reservation_id)

    # 最后一页之后再翻：documents 为空，next_cursor 等于请求的 after。
    beyond = data_of(order_stock(client, caller, order_ref, {"limit": "2", "after": after}), 200)
    assert beyond["documents"] == []
    assert beyond["next_cursor"] == after
    assert beyond["reservation"] == confirmed
    assert beyond["lines"] == lines

    # 不带 limit 为 100：一页取完。
    whole = data_of(order_stock(client, caller, order_ref), 200)
    assert whole["documents"] == issued
    assert whole["next_cursor"] == after


# --- 拒绝 ---


def test_a_missing_order_is_not_found(client, engine, caller):
    response = order_stock(client, caller, new_ref())
    error = error_of(response, 404)
    assert error["code"] == "RESERVATION_NOT_FOUND"
    assert events(engine, rid_of(response)) == []


@pytest.mark.parametrize("channel_status", [None, "inactive"])
def test_a_credential_without_a_sales_channel_is_not_found(client, engine, caller, channel_status):
    created, _ = reserved_order(client, engine, caller, [1])
    without_channel = live_caller(engine, channel_status=channel_status)
    response = order_stock(client, without_channel, created["order_ref"])
    assert error_of(response, 404)["code"] == "RESERVATION_NOT_FOUND"
    assert events(engine, rid_of(response)) == []


def test_the_same_order_ref_on_another_channel_is_not_visible(client, engine, caller):
    created, _ = reserved_order(client, engine, caller, [1])
    order_ref = created["order_ref"]
    other = other_channel(engine, caller)

    response = order_stock(client, other, order_ref)
    assert error_of(response, 404)["code"] == "RESERVATION_NOT_FOUND"
    assert events(engine, rid_of(response)) == []

    # 别的入口用同一个 order_ref 预留之后，各自只看到自己的。
    [sku] = add_skus(engine, caller.tenant_id, 1)
    stock(engine, other, sku, 5)
    theirs = data_of(reserve(client, other, order([line("l1", sku, 1)], order_ref)), 201)
    assert theirs["reservation_id"] != created["reservation_id"]
    mine = data_of(order_stock(client, caller, order_ref), 200)
    seen = data_of(order_stock(client, other, order_ref), 200)
    assert mine["reservation"] == created
    assert seen["reservation"] == theirs


@pytest.mark.parametrize(
    "after",
    [
        "",
        "abc",
        "v1.1",
        "v1.1.0123456789abcdef.x",
        "v2.1.0123456789abcdef",
        "v1.0.0123456789abcdef",
        "v1.01.0123456789abcdef",
        "v1.-1.0123456789abcdef",
        "v1.9223372036854775808.0123456789abcdef",
        "v1.1.0123456789ABCDEF",
        "v1.1.0123456789abcde",
    ],
)
def test_a_malformed_cursor_is_rejected_even_for_a_missing_order(client, engine, caller, after):
    created, _ = reserved_order(client, engine, caller, [1])
    for order_ref in (created["order_ref"], new_ref()):
        response = order_stock(client, caller, order_ref, {"after": after})
        error = error_of(response, 422)
        assert error["code"] == "INVALID_REQUEST"
        assert error["details"] == {"parameter": "after"}


def test_a_cursor_of_another_order_is_rejected(client, engine, caller):
    mine, _ = committed(client, engine, caller, [2])
    theirs, _ = committed(client, engine, caller, [2])
    dispatch(client, caller, theirs["order_ref"], [qline("l1", 1)])
    foreign = data_of(order_stock(client, caller, theirs["order_ref"]), 200)["next_cursor"]
    assert foreign is not None

    response = order_stock(client, caller, mine["order_ref"], {"after": foreign})
    error = error_of(response, 422)
    assert error["code"] == "INVALID_REQUEST"
    assert error["details"] == {"parameter": "after"}
    assert events(engine, rid_of(response)) == []

    # 订单不存在时先报 404：游标与订单是否相符最后才判。
    missing = order_stock(client, caller, new_ref(), {"after": foreign})
    assert error_of(missing, 404)["code"] == "RESERVATION_NOT_FOUND"


@pytest.mark.parametrize("limit", ["0", "101", "abc", "", "-1", "01", "1.5"])
def test_an_invalid_limit_is_rejected(client, engine, caller, limit):
    created, _ = reserved_order(client, engine, caller, [1])
    response = order_stock(client, caller, created["order_ref"], {"limit": limit})
    error = error_of(response, 422)
    assert error["code"] == "INVALID_REQUEST"
    assert error["details"] == {"parameter": "limit"}


@pytest.mark.parametrize(
    "params",
    [
        [("order_ref", "x")],
        [("limit", "2"), ("cursor", "x")],
        [("after", "v1.1.0123456789abcdef"), ("after", "v1.2.0123456789abcdef")],
        [("limit", "2"), ("limit", "2")],
    ],
)
def test_unknown_or_repeated_query_parameters_are_rejected(client, engine, caller, params):
    created, _ = reserved_order(client, engine, caller, [1])
    response = order_stock(client, caller, created["order_ref"], params)
    assert error_of(response, 422)["code"] == "INVALID_REQUEST"


def test_a_malformed_order_ref_is_rejected(client, caller):
    response = order_stock(client, caller, "o" * 129, {"after": "v1.1.0123456789abcdef"})
    error = error_of(response, 422)
    assert error["code"] == "INVALID_REQUEST"
    assert error["details"] == {"parameter": "order_ref"}


def test_the_query_needs_orders_read(client, engine, caller):
    created, _ = reserved_order(client, engine, caller, [1])
    writer = credential_for(
        engine, caller.tenant_id, caller.pool_id, channel_of(engine, caller), [ORDERS_WRITE]
    )
    response = order_stock(client, writer, created["order_ref"])
    assert error_of(response, 403)["code"] == "FORBIDDEN"
    [event] = events(engine, rid_of(response))
    assert event["event_type"] == "forbidden"
    assert event["details"] == {"category": "scope", "missing_scopes": ["orders.read"]}
