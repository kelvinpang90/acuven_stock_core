"""预留表上确认销售与待审结案的列与约束在真 MySQL 上生效（契约 5.7 的存放）。

只验表结构与约束，写这些列的命令在各自的任务里。造数沿用预留表结构测试的夹具。
"""

from datetime import datetime

import pytest
from sqlalchemy import Connection, select, update

from app.domain.reservations import (
    ACTIVE,
    CLOSED_REFUNDED,
    COMMITTED,
    EXPIRED,
    LATE_PAYMENT_REVIEW,
    RELEASED,
)
from app.models.orders import COMMIT_COLUMNS, Reservation
from tests.integration.conftest import assert_rejected_by, new_id
from tests.integration.test_reservation_schema import (
    CLOSED_AT,
    CREATED_AT,
    Shop,
    add_reservation,
    add_shop,
    paid,
    released,
    reservation_insert,
)

COMMITTED_AT = datetime(2026, 11, 16, 8, 10)
REVIEW_CLOSED_AT = datetime(2026, 11, 16, 9, 0)


def committed(shop: Shop, **overrides: object) -> dict[str, object]:
    """确认之后的列：状态、付款与确认的各列、确认时间与关单时间。"""
    values = {"status": COMMITTED, "committed_at": COMMITTED_AT, "closed_at": CLOSED_AT}
    return values | paid(shop) | overrides


def in_review(shop: Shop, **overrides: object) -> dict[str, object]:
    """转入待审之后的列：状态、付款与确认的各列与关单时间，没有确认时间。"""
    values = {"status": LATE_PAYMENT_REVIEW, "closed_at": CLOSED_AT}
    return values | paid(shop) | overrides


def review_closed(shop: Shop) -> dict[str, object]:
    """待审结案记下的三列。"""
    return {
        "review_closed_at": REVIEW_CLOSED_AT,
        "review_closed_credential_id": shop.credential_id,
        "review_closed_request_id": new_id("req"),
    }


def refunded(shop: Shop, **overrides: object) -> dict[str, object]:
    """待审结案之后的列。"""
    return in_review(shop, status=CLOSED_REFUNDED) | review_closed(shop) | overrides


def before_payment(shop: Shop, status: str) -> dict[str, object]:
    """active、released、expired 各自在 0007 里合格的列。"""
    if status == RELEASED:
        return released(shop)
    if status == EXPIRED:
        return {"status": EXPIRED, "closed_at": CLOSED_AT}
    return {"status": ACTIVE}


def stored(db: Connection, reservation_id: str) -> dict:
    """布尔列读回来是 0 或 1，断言按真假比。"""
    table = Reservation.__table__
    statement = select(table).where(table.c.reservation_id == reservation_id)
    return dict(db.execute(statement).mappings().one())


# committed


@pytest.mark.parametrize("column", COMMIT_COLUMNS)
def test_committed_requires_every_commit_column(db: Connection, column: str):
    shop = add_shop(db)
    assert_rejected_by(
        db,
        reservation_insert(shop, **committed(shop, **{column: None})),
        "ck_reservations_commit_columns",
    )


def test_committed_requires_committed_at_and_closed_at(db: Connection):
    shop = add_shop(db)
    assert_rejected_by(
        db,
        reservation_insert(shop, **committed(shop, committed_at=None)),
        "ck_reservations_committed_at",
    )
    assert_rejected_by(
        db,
        reservation_insert(shop, **committed(shop, closed_at=None)),
        "ck_reservations_paid_closed_at",
    )


def test_committed_with_every_column_is_accepted_late_or_not(db: Connection):
    shop = add_shop(db)
    on_time = add_reservation(db, shop, **committed(shop))
    late = add_reservation(db, shop, **committed(shop, late=True))
    assert not stored(db, on_time)["late"]
    assert stored(db, late)["late"]
    # 新加的两个布尔默认为 false。
    assert not stored(db, on_time)["fault_mode"]


def test_committed_must_not_carry_review_closed_columns(db: Connection):
    shop = add_shop(db)
    for column, value in review_closed(shop).items():
        assert_rejected_by(
            db,
            reservation_insert(shop, **committed(shop, **{column: value})),
            "ck_reservations_review_closed_columns",
        )


# late_payment_review 与 closed_refunded


@pytest.mark.parametrize("column", COMMIT_COLUMNS)
def test_review_and_refunded_require_every_commit_column(db: Connection, column: str):
    shop = add_shop(db)
    for values in (in_review(shop, **{column: None}), refunded(shop, **{column: None})):
        assert_rejected_by(db, reservation_insert(shop, **values), "ck_reservations_commit_columns")


def test_review_and_refunded_have_no_committed_at_and_are_never_late(db: Connection):
    shop = add_shop(db)
    for paid_columns in (in_review, refunded):
        assert_rejected_by(
            db,
            reservation_insert(shop, **paid_columns(shop, committed_at=COMMITTED_AT)),
            "ck_reservations_committed_at",
        )
        assert_rejected_by(
            db,
            reservation_insert(shop, **paid_columns(shop, late=True)),
            "ck_reservations_late",
        )
        assert_rejected_by(
            db,
            reservation_insert(shop, **paid_columns(shop, closed_at=None)),
            "ck_reservations_paid_closed_at",
        )


def test_review_closed_columns_are_set_exactly_when_closed_refunded(db: Connection):
    shop = add_shop(db)
    for column, value in review_closed(shop).items():
        assert_rejected_by(
            db,
            reservation_insert(shop, **refunded(shop, **{column: None})),
            "ck_reservations_review_closed_columns",
        )
        assert_rejected_by(
            db,
            reservation_insert(shop, **in_review(shop, **{column: value})),
            "ck_reservations_review_closed_columns",
        )
    add_reservation(db, shop, **in_review(shop))
    add_reservation(db, shop, **refunded(shop))


# active、released、expired


@pytest.mark.parametrize("status", [ACTIVE, RELEASED, EXPIRED])
def test_unpaid_states_carry_no_commit_or_review_closed_columns(db: Connection, status: str):
    shop = add_shop(db)
    base = before_payment(shop, status)
    for column, value in paid(shop).items():
        assert_rejected_by(
            db,
            reservation_insert(shop, **base, **{column: value}),
            "ck_reservations_commit_columns",
        )
    assert_rejected_by(
        db,
        reservation_insert(shop, **base, committed_at=COMMITTED_AT),
        "ck_reservations_committed_at",
    )
    for column, value in review_closed(shop).items():
        assert_rejected_by(
            db,
            reservation_insert(shop, **base, **{column: value}),
            "ck_reservations_review_closed_columns",
        )
    assert_rejected_by(db, reservation_insert(shop, **base, late=True), "ck_reservations_late")
    # 不带新列时与 0007 相同；两组新的操作者列不按状态约束。
    add_reservation(db, shop, **base)
    add_reservation(
        db,
        shop,
        **base,
        commit_actor_ref="admin_1",
        commit_actor_role="stock_admin",
        review_closed_actor_ref="admin_1",
        review_closed_actor_role="stock_admin",
    )


# 唯一与外键


def test_a_payment_ref_confirms_one_order_per_channel(db: Connection):
    shop = add_shop(db)
    add_reservation(db, shop, **committed(shop, payment_ref="pay_1"))
    assert_rejected_by(
        db,
        reservation_insert(shop, **in_review(shop, payment_ref="pay_1")),
        "uq_reservations_tenant_id_channel_id_payment_ref",
    )
    # 同一租户的另一个入口可以用同一个 payment_ref；没有付款的预留都为空，不相撞。
    add_reservation(db, add_shop(db, shop.tenant_id), **committed(shop, payment_ref="pay_1"))
    for _ in range(2):
        add_reservation(db, shop)
        add_reservation(db, shop, **released(shop))


def test_commit_and_review_closed_credentials_must_belong_to_the_same_tenant(db: Connection):
    shop, other = add_shop(db), add_shop(db)
    assert_rejected_by(
        db,
        reservation_insert(shop, **committed(shop, commit_credential_id=other.credential_id)),
        "fk_reservations_commit_credential_id_credentials",
    )
    assert_rejected_by(
        db,
        reservation_insert(shop, **refunded(shop, review_closed_credential_id=other.credential_id)),
        "fk_reservations_review_closed_credential_id_credentials",
    )


def test_new_actor_columns_are_both_set_or_both_empty(db: Connection):
    shop = add_shop(db)
    for actor in ({"commit_actor_ref": "admin_1"}, {"commit_actor_role": "stock_admin"}):
        assert_rejected_by(
            db,
            reservation_insert(shop, **committed(shop, **actor)),
            "ck_reservations_commit_actor",
        )
    for actor in (
        {"review_closed_actor_ref": "admin_1"},
        {"review_closed_actor_role": "stock_admin"},
    ):
        assert_rejected_by(
            db,
            reservation_insert(shop, **refunded(shop, **actor)),
            "ck_reservations_review_closed_actor",
        )
    add_reservation(
        db, shop, **committed(shop, commit_actor_ref="admin_1", commit_actor_role="stock_admin")
    )
    add_reservation(
        db,
        shop,
        **refunded(
            shop,
            commit_actor_ref="admin_1",
            commit_actor_role="stock_admin",
            review_closed_actor_ref="admin_1",
            review_closed_actor_role="stock_admin",
        ),
    )


# 状态之间的转变与故障补交


def test_an_expired_reservation_committed_late_keeps_its_closed_at(db: Connection):
    shop = add_shop(db)
    reservation_id = add_reservation(db, shop, status=EXPIRED, closed_at=CLOSED_AT)
    late_commit = committed(shop, late=True)
    del late_commit["closed_at"]
    statement = update(Reservation).where(Reservation.reservation_id == reservation_id)
    db.execute(statement.values(**late_commit))
    row = stored(db, reservation_id)
    assert (row["status"], row["closed_at"]) == (COMMITTED, CLOSED_AT)
    assert row["late"]


def test_a_review_moves_on_to_committed_or_closed_refunded(db: Connection):
    shop = add_shop(db)
    moves = {
        COMMITTED: {"committed_at": COMMITTED_AT, "late": True},
        CLOSED_REFUNDED: review_closed(shop),
    }
    for status, values in moves.items():
        reservation_id = add_reservation(db, shop, **in_review(shop))
        statement = update(Reservation).where(Reservation.reservation_id == reservation_id)
        db.execute(statement.values(status=status, **values))
        assert stored(db, reservation_id)["status"] == status


def test_a_fault_mode_commit_creates_a_closed_reservation_directly(db: Connection):
    # 故障补交新建：从未 active，closed_at 为创建时间（契约 5.7 第 5 步）。
    shop = add_shop(db)
    new_rows = (
        committed(shop, late=True, fault_mode=True, closed_at=CREATED_AT),
        in_review(shop, fault_mode=True, closed_at=CREATED_AT),
    )
    for values in new_rows:
        row = stored(db, add_reservation(db, shop, **values))
        assert row["fault_mode"]
        assert row["closed_at"] == CREATED_AT
