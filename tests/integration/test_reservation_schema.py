"""预留与订单行的表在真 MySQL 上生效（契约 5.1、5.6 的存放与第 10 节第 1 条）。

只验表结构与约束，写这些表的命令在各自的任务里。连接夹具 db 与各表的造数见 conftest。
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest
from sqlalchemy import Connection, insert, select
from sqlalchemy.sql.expression import Executable

from app.domain.reservations import (
    ACTIVE,
    CANCELLED,
    CHECKOUT_REPLACED,
    COMMITTED,
    EXPIRED,
    LATE_PAYMENT_REVIEW,
    RELEASED,
)
from app.models.orders import LINE_ACCUMULATORS, RELEASE_COLUMNS, OrderLine, Reservation
from tests.integration.conftest import (
    add_channel,
    add_credential,
    add_pool,
    add_sku,
    add_tenant,
    assert_rejected_by,
    assert_rejected_by_one_of,
    balance_insert,
    new_id,
)

CREATED_AT = datetime(2026, 11, 16, 8, 0)
TTL_SECONDS = 900
EXPIRES_AT = CREATED_AT + timedelta(seconds=TTL_SECONDS)
CLOSED_AT = datetime(2026, 11, 16, 8, 5)
REQUEST_HASH = "0" * 64


@dataclass
class Shop:
    """一个租户的池、绑定它的销售入口、该租户的一个凭据，以及池里一个有余额行的 SKU。"""

    tenant_id: str
    pool_id: str
    channel_id: str
    credential_id: str
    sku_id: str


def add_balance_sku(db: Connection, tenant_id: str, pool_id: str) -> str:
    sku_id = add_sku(db, tenant_id)
    db.execute(balance_insert(tenant_id, pool_id, sku_id))
    return sku_id


def add_shop(db: Connection, tenant_id: str | None = None) -> Shop:
    tenant_id = tenant_id or add_tenant(db)
    pool_id = add_pool(db, tenant_id)
    channel_id = add_channel(db, tenant_id, pool_id)
    credential_id = add_credential(db, tenant_id)
    sku_id = add_balance_sku(db, tenant_id, pool_id)
    return Shop(tenant_id, pool_id, channel_id, credential_id, sku_id)


def reservation_insert(shop: Shop, **overrides: object) -> Executable:
    """默认是一个 active 的预留，没有操作者与角色：预留不要求角色（契约第 10 节第 3 条）。"""
    values = {
        "reservation_id": new_id("rsv"),
        "tenant_id": shop.tenant_id,
        "channel_id": shop.channel_id,
        "pool_id": shop.pool_id,
        "order_ref": new_id("ord"),
        "status": ACTIVE,
        "ttl_seconds": TTL_SECONDS,
        "expires_at": EXPIRES_AT,
        "request_hash": REQUEST_HASH,
        "credential_id": shop.credential_id,
        "request_id": new_id("req"),
        "created_at": CREATED_AT,
    }
    return insert(Reservation).values(values | overrides)


def released(shop: Shop, **overrides: object) -> dict[str, object]:
    """释放之后的列：状态、三个必有的释放列与关单时间。"""
    values = {
        "status": RELEASED,
        "release_reason": CANCELLED,
        "released_credential_id": shop.credential_id,
        "released_request_id": new_id("req"),
        "closed_at": CLOSED_AT,
    }
    return values | overrides


def add_reservation(db: Connection, shop: Shop, **overrides: object) -> str:
    reservation_id = new_id("rsv")
    db.execute(reservation_insert(shop, reservation_id=reservation_id, **overrides))
    return reservation_id


# 预留（契约 5.6 的存放）


def test_status_and_release_reason_fit_the_longest_values(db: Connection):
    shop = add_shop(db)
    release = add_reservation(db, shop, **released(shop, release_reason=CHECKOUT_REPLACED))
    review = add_reservation(db, shop, status=LATE_PAYMENT_REVIEW, closed_at=CLOSED_AT)
    rows = db.execute(
        select(Reservation.reservation_id, Reservation.status, Reservation.release_reason).where(
            Reservation.reservation_id.in_([release, review])
        )
    )
    assert {tuple(row) for row in rows} == {
        (release, RELEASED, CHECKOUT_REPLACED),
        (review, LATE_PAYMENT_REVIEW, None),
    }


def test_unknown_status_and_release_reason_are_rejected(db: Connection):
    shop = add_shop(db)
    for status in ("pending", "Active", "rejected", ""):
        assert_rejected_by(db, reservation_insert(shop, status=status), "ck_reservations_status")
    for reason in ("expired", "Cancelled", "refunded", ""):
        assert_rejected_by(
            db,
            reservation_insert(shop, **released(shop, release_reason=reason)),
            "ck_reservations_release_reason",
        )


def test_ttl_seconds_is_between_60_and_3600(db: Connection):
    shop = add_shop(db)
    for ttl_seconds in (59, 3601):
        assert_rejected_by(
            db,
            reservation_insert(
                shop,
                ttl_seconds=ttl_seconds,
                expires_at=CREATED_AT + timedelta(seconds=ttl_seconds),
            ),
            "ck_reservations_ttl_seconds",
        )
    for ttl_seconds in (60, 3600):
        add_reservation(
            db,
            shop,
            ttl_seconds=ttl_seconds,
            expires_at=CREATED_AT + timedelta(seconds=ttl_seconds),
        )


def test_an_order_ref_has_one_reservation_per_channel(db: Connection):
    shop = add_shop(db)
    add_reservation(db, shop, order_ref="order_1")
    assert_rejected_by(
        db,
        reservation_insert(shop, order_ref="order_1"),
        "uq_reservations_tenant_id_channel_id_order_ref",
    )
    # 同一租户的另一个入口，或同一入口的另一个 order_ref，都可以。
    add_reservation(db, add_shop(db, shop.tenant_id), order_ref="order_1")
    add_reservation(db, shop, order_ref="order_2")


@pytest.mark.parametrize(
    "column",
    ["credential_id", "request_id", "created_at", "expires_at", "request_hash"],
)
def test_creation_columns_are_required(db: Connection, column: str):
    shop = add_shop(db)
    assert_rejected_by(
        db, reservation_insert(shop, **{column: None}), f"Column '{column}' cannot be null"
    )


def test_release_columns_are_set_exactly_when_released(db: Connection):
    shop = add_shop(db)
    for column in RELEASE_COLUMNS:
        assert_rejected_by(
            db,
            reservation_insert(shop, **released(shop, **{column: None})),
            "ck_reservations_released_columns",
        )
    release_values = released(shop)
    for column in RELEASE_COLUMNS:
        assert_rejected_by(
            db,
            reservation_insert(shop, **{column: release_values[column]}),
            "ck_reservations_released_columns",
        )
        # 到期关单不是释放，也不带释放列。
        assert_rejected_by(
            db,
            reservation_insert(
                shop, status=EXPIRED, closed_at=CLOSED_AT, **{column: release_values[column]}
            ),
            "ck_reservations_released_columns",
        )
    add_reservation(db, shop, **released(shop))
    add_reservation(db, shop)


def test_closed_at_is_empty_while_active_and_set_once_released_or_expired(db: Connection):
    shop = add_shop(db)
    assert_rejected_by(
        db, reservation_insert(shop, closed_at=CLOSED_AT), "ck_reservations_closed_at"
    )
    assert_rejected_by(
        db, reservation_insert(shop, **released(shop, closed_at=None)), "ck_reservations_closed_at"
    )
    assert_rejected_by(db, reservation_insert(shop, status=EXPIRED), "ck_reservations_closed_at")
    add_reservation(db, shop, status=EXPIRED, closed_at=CLOSED_AT)
    # 确认销售之后的状态由确认销售任务定，这里不约束。
    add_reservation(db, shop, status=COMMITTED)
    add_reservation(db, shop, status=COMMITTED, closed_at=CLOSED_AT)


def test_actor_columns_are_both_set_or_both_empty(db: Connection):
    shop = add_shop(db)
    for actor in ({"actor_ref": "admin_1"}, {"actor_role": "stock_admin"}):
        assert_rejected_by(db, reservation_insert(shop, **actor), "ck_reservations_actor")
    for actor in ({"released_actor_ref": "admin_1"}, {"released_actor_role": "stock_admin"}):
        assert_rejected_by(
            db,
            reservation_insert(shop, **released(shop, **actor)),
            "ck_reservations_released_actor",
        )
    add_reservation(db, shop, actor_ref="admin_1", actor_role="stock_admin")
    add_reservation(
        db,
        shop,
        **released(shop, released_actor_ref="admin_1", released_actor_role="stock_admin"),
    )


def test_expires_at_is_after_created_at(db: Connection):
    shop = add_shop(db)
    for expires_at in (CREATED_AT, CREATED_AT - timedelta(seconds=1)):
        assert_rejected_by(
            db, reservation_insert(shop, expires_at=expires_at), "ck_reservations_expires_at"
        )


def test_reservation_pool_channel_and_credentials_must_belong_to_the_same_tenant(db: Connection):
    shop, other = add_shop(db), add_shop(db)
    assert_rejected_by(
        db,
        reservation_insert(shop, pool_id=other.pool_id),
        "fk_reservations_tenant_id_stock_pools",
    )
    assert_rejected_by(
        db,
        reservation_insert(shop, channel_id=other.channel_id),
        "fk_reservations_tenant_id_sales_channels",
    )
    assert_rejected_by(
        db,
        reservation_insert(shop, credential_id=other.credential_id),
        "fk_reservations_tenant_id_credentials",
    )
    assert_rejected_by(
        db,
        reservation_insert(shop, **released(shop, released_credential_id=other.credential_id)),
        "fk_reservations_released_credential_id_credentials",
    )


# 订单行（契约 5.1、5.6 的存放）


def line_insert(
    shop: Shop, reservation_id: str, sku_id: str | None = None, **overrides: object
) -> Executable:
    """默认下单 5 件、各累计量取默认值 0。"""
    values = {
        "order_line_id": new_id("oln"),
        "tenant_id": shop.tenant_id,
        "reservation_id": reservation_id,
        "pool_id": shop.pool_id,
        "sku_id": sku_id or shop.sku_id,
        "line_ref": new_id("line"),
        "quantity": 5,
    }
    return insert(OrderLine).values(values | overrides)


def test_line_accumulators_default_to_zero(db: Connection):
    shop = add_shop(db)
    order_line_id = new_id("oln")
    db.execute(line_insert(shop, add_reservation(db, shop), order_line_id=order_line_id))
    row = db.execute(
        select(*[getattr(OrderLine, column) for column in LINE_ACCUMULATORS]).where(
            OrderLine.order_line_id == order_line_id
        )
    ).one()
    assert tuple(row) == (0, 0, 0, 0, 0)


def test_line_quantity_is_between_1_and_1000000(db: Connection):
    shop = add_shop(db)
    reservation_id = add_reservation(db, shop)
    for quantity in (0, 1000001):
        assert_rejected_by(
            db, line_insert(shop, reservation_id, quantity=quantity), "ck_order_lines_quantity"
        )
    for quantity in (1, 1000000):
        db.execute(line_insert(shop, reservation_id, quantity=quantity))


def test_line_accumulators_are_nonnegative(db: Connection):
    shop = add_shop(db)
    reservation_id = add_reservation(db, shop)
    committed = {"committed_qty": 5}
    for column in ("refund_restocked_qty", "returned_sellable_qty", "returned_unsellable_qty"):
        assert_rejected_by(
            db,
            line_insert(shop, reservation_id, **committed, **{column: -1}),
            f"ck_order_lines_{column}_nonnegative",
        )
    # 为负时另一条约束也必然不成立；不依赖 MySQL 先报哪一条。
    assert_rejected_by_one_of(
        db,
        line_insert(shop, reservation_id, committed_qty=-1),
        "ck_order_lines_committed_qty_nonnegative",
        "ck_order_lines_whole_commit",
        "ck_order_lines_dispatched_restocked_within_committed",
    )
    assert_rejected_by_one_of(
        db,
        line_insert(shop, reservation_id, **committed, dispatched_qty=-1),
        "ck_order_lines_dispatched_qty_nonnegative",
        "ck_order_lines_returned_within_dispatched",
    )


def test_committed_qty_is_zero_or_the_whole_quantity(db: Connection):
    shop = add_shop(db)
    reservation_id = add_reservation(db, shop)
    for committed_qty in (1, 4, 6):
        assert_rejected_by(
            db,
            line_insert(shop, reservation_id, committed_qty=committed_qty),
            "ck_order_lines_whole_commit",
        )
    for committed_qty in (0, 5):
        db.execute(line_insert(shop, reservation_id, committed_qty=committed_qty))


def test_dispatched_and_refund_restocked_stay_within_committed(db: Connection):
    shop = add_shop(db)
    reservation_id = add_reservation(db, shop)
    check = "ck_order_lines_dispatched_restocked_within_committed"
    over = {"committed_qty": 5, "dispatched_qty": 3, "refund_restocked_qty": 3}
    assert_rejected_by(db, line_insert(shop, reservation_id, **over), check)
    assert_rejected_by(db, line_insert(shop, reservation_id, dispatched_qty=1), check)
    assert_rejected_by(db, line_insert(shop, reservation_id, refund_restocked_qty=1), check)
    within = {"committed_qty": 5, "dispatched_qty": 3, "refund_restocked_qty": 2}
    db.execute(line_insert(shop, reservation_id, **within))


def test_returns_stay_within_dispatched(db: Connection):
    shop = add_shop(db)
    reservation_id = add_reservation(db, shop)
    dispatched = {"committed_qty": 5, "dispatched_qty": 3}
    assert_rejected_by(
        db,
        line_insert(
            shop, reservation_id, **dispatched, returned_sellable_qty=2, returned_unsellable_qty=2
        ),
        "ck_order_lines_returned_within_dispatched",
    )
    db.execute(
        line_insert(
            shop, reservation_id, **dispatched, returned_sellable_qty=2, returned_unsellable_qty=1
        )
    )


def test_a_line_ref_appears_once_per_reservation(db: Connection):
    shop = add_shop(db)
    reservation_id = add_reservation(db, shop)
    db.execute(line_insert(shop, reservation_id, line_ref="line_1"))
    assert_rejected_by(
        db,
        line_insert(shop, reservation_id, line_ref="line_1"),
        "uq_order_lines_tenant_id_reservation_id_line_ref",
    )
    # 另一个预留可以用同一个 line_ref。
    db.execute(line_insert(shop, add_reservation(db, shop), line_ref="line_1"))


def test_a_reservation_may_have_two_lines_on_the_same_sku(db: Connection):
    # SKU 合并改挂之后，同一订单可能有两行指向同一个规范 SKU（契约 5.6）。
    shop = add_shop(db)
    reservation_id = add_reservation(db, shop)
    db.execute(line_insert(shop, reservation_id, line_ref="line_1"))
    db.execute(line_insert(shop, reservation_id, line_ref="line_2"))


def test_line_reservation_and_balance_must_belong_to_the_same_tenant(db: Connection):
    shop, other = add_shop(db), add_shop(db)
    assert_rejected_by(
        db,
        line_insert(shop, add_reservation(db, other)),
        "fk_order_lines_tenant_id_reservations",
    )
    reservation_id = add_reservation(db, shop)
    assert_rejected_by(
        db,
        line_insert(shop, reservation_id, other.sku_id, pool_id=other.pool_id),
        "fk_order_lines_tenant_id_stock_balances",
    )
    # 同租户、但池里没有这个 SKU 的余额行，也被拒。
    assert_rejected_by(
        db,
        line_insert(shop, reservation_id, add_sku(db, shop.tenant_id)),
        "fk_order_lines_tenant_id_stock_balances",
    )
