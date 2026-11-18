"""订单单据表在真 MySQL 上生效（契约 5.8 的存放与第 10 节第 1 条）。

只验表结构与约束，写这张表的命令在各自的任务里。造数沿用预留与确认销售表结构测试的夹具。
"""

import pytest
from sqlalchemy import Connection, insert, select
from sqlalchemy.sql.expression import Executable

from app.domain.fulfilment import DISPATCH, DOCUMENT_TYPES, REFUND_RESTOCK, RETURN_RECEIPT
from app.models.orders import OrderDocument
from tests.integration.conftest import assert_rejected_by, new_id
from tests.integration.test_reservation_schema import Shop, add_reservation, add_shop
from tests.integration.test_sale_commit_schema import committed

REQUEST_HASH = "0" * 64

# 约束名超过 64 字符，建表时截成前 56 个字符再加 "_" 与 4 位摘要；这里比对截断后不变的前缀。
DOCUMENT_REF_UNIQUE = "uq_order_documents_tenant_id_channel_id_document_type_document_ref"[:56]


def add_order(db: Connection, tenant_id: str | None = None) -> tuple[Shop, str]:
    """一个租户的池、入口、凭据，以及该入口上一个已确认的预留。"""
    shop = add_shop(db, tenant_id)
    return shop, add_reservation(db, shop, **committed(shop))


def document_insert(shop: Shop, reservation_id: str, **overrides: object) -> Executable:
    """默认是一张发货单，没有操作者与角色：三个命令都不要求角色（契约第 10 节第 3 条）。

    不给 document_seq，由自增生成。
    """
    values = {
        "document_id": new_id("shp"),
        "tenant_id": shop.tenant_id,
        "channel_id": shop.channel_id,
        "pool_id": shop.pool_id,
        "reservation_id": reservation_id,
        "document_type": DISPATCH,
        "document_ref": new_id("shipment"),
        "request_hash": REQUEST_HASH,
        "result": {"document_id": "shp_01", "lines": []},
        "credential_id": shop.credential_id,
        "request_id": new_id("req"),
    }
    return insert(OrderDocument).values(values | overrides)


def add_document(db: Connection, shop: Shop, reservation_id: str, **overrides: object) -> int:
    """插入一行，返回自增得到的 document_seq。"""
    result = db.execute(document_insert(shop, reservation_id, **overrides))
    return result.inserted_primary_key[0]


# document_seq 与 document_id


def test_document_seq_is_generated_increasing_and_returned_on_insert(db: Connection):
    shop, reservation_id = add_order(db)
    first_id, second_id = new_id("shp"), new_id("shp")
    first = add_document(db, shop, reservation_id, document_id=first_id)
    second = add_document(db, shop, reservation_id, document_id=second_id)
    assert isinstance(first, int) and isinstance(second, int)
    assert second > first
    rows = db.execute(
        select(OrderDocument.document_seq, OrderDocument.document_id).where(
            OrderDocument.reservation_id == reservation_id
        )
    )
    assert {tuple(row) for row in rows} == {(first, first_id), (second, second_id)}


def test_created_at_defaults_to_the_database_time(db: Connection):
    shop, reservation_id = add_order(db)
    document_seq = add_document(db, shop, reservation_id)
    created_at = db.execute(
        select(OrderDocument.created_at).where(OrderDocument.document_seq == document_seq)
    ).scalar_one()
    assert created_at is not None


def test_document_id_is_unique(db: Connection):
    shop, reservation_id = add_order(db)
    add_document(db, shop, reservation_id, document_id="shp_1")
    assert_rejected_by(
        db,
        document_insert(shop, reservation_id, document_id="shp_1"),
        "uq_order_documents_document_id",
    )


@pytest.mark.parametrize(
    "column", ["document_id", "document_ref", "request_hash", "credential_id", "request_id"]
)
def test_creation_columns_are_required(db: Connection, column: str):
    shop, reservation_id = add_order(db)
    assert_rejected_by(
        db,
        document_insert(shop, reservation_id, **{column: None}),
        f"Column '{column}' cannot be null",
    )


# document_type 与 result


def test_document_type_accepts_only_the_three_order_document_types(db: Connection):
    shop, reservation_id = add_order(db)
    assert DOCUMENT_TYPES == (DISPATCH, REFUND_RESTOCK, RETURN_RECEIPT)
    for document_type in DOCUMENT_TYPES:
        add_document(db, shop, reservation_id, document_type=document_type)
    for document_type in ("receipt", "Dispatch", "shipment", "return", "refund", ""):
        assert_rejected_by(
            db,
            document_insert(shop, reservation_id, document_type=document_type),
            "ck_order_documents_document_type",
        )


def test_result_must_be_an_object(db: Connection):
    shop, reservation_id = add_order(db)
    add_document(db, shop, reservation_id, result={})
    for result in ([], ["shp_01"], "result", 1):
        assert_rejected_by(
            db,
            document_insert(shop, reservation_id, result=result),
            "ck_order_documents_result",
        )


# 业务引用


def test_a_document_ref_takes_effect_once_per_channel_and_type(db: Connection):
    shop, reservation_id = add_order(db)
    add_document(db, shop, reservation_id, document_ref="ref_1")
    assert_rejected_by(
        db, document_insert(shop, reservation_id, document_ref="ref_1"), DOCUMENT_REF_UNIQUE
    )
    # 同一引用报到同一入口的另一个订单上，同样被拒。
    other_order = add_reservation(db, shop, **committed(shop))
    assert_rejected_by(
        db, document_insert(shop, other_order, document_ref="ref_1"), DOCUMENT_REF_UNIQUE
    )
    # 另一种类型的同一引用，或同一租户另一个入口的同一引用，都可以。
    for document_type in (REFUND_RESTOCK, RETURN_RECEIPT):
        add_document(db, shop, reservation_id, document_type=document_type, document_ref="ref_1")
    other_shop, other_reservation = add_order(db, shop.tenant_id)
    add_document(db, other_shop, other_reservation, document_ref="ref_1")


# 操作者


def test_actor_ref_and_actor_role_are_both_set_or_both_empty(db: Connection):
    shop, reservation_id = add_order(db)
    for actor in ({"actor_ref": "admin_1"}, {"actor_role": "stock_admin"}):
        assert_rejected_by(
            db, document_insert(shop, reservation_id, **actor), "ck_order_documents_actor"
        )
    add_document(db, shop, reservation_id)
    add_document(db, shop, reservation_id, actor_ref="admin_1", actor_role="stock_admin")


# 外键（第 10 节第 1 条）


def test_reservation_must_exist_in_the_same_tenant(db: Connection):
    (shop, reservation_id), (_, other_reservation) = add_order(db), add_order(db)
    for missing in (new_id("rsv"), other_reservation):
        assert_rejected_by(
            db,
            document_insert(shop, missing),
            "fk_order_documents_reservation_id_reservations",
        )
    add_document(db, shop, reservation_id)


def test_pool_must_exist_in_the_same_tenant(db: Connection):
    (shop, reservation_id), (other, _) = add_order(db), add_order(db)
    for pool_id in (new_id("pool"), other.pool_id):
        assert_rejected_by(
            db,
            document_insert(shop, reservation_id, pool_id=pool_id),
            "fk_order_documents_pool_id_stock_pools",
        )


def test_channel_must_exist_in_the_same_tenant(db: Connection):
    (shop, reservation_id), (other, _) = add_order(db), add_order(db)
    for channel_id in (new_id("chn"), other.channel_id):
        assert_rejected_by(
            db,
            document_insert(shop, reservation_id, channel_id=channel_id),
            "fk_order_documents_channel_id_sales_channels",
        )


def test_credential_must_exist_in_the_same_tenant(db: Connection):
    (shop, reservation_id), (other, _) = add_order(db), add_order(db)
    for credential_id in (new_id("cred"), other.credential_id):
        assert_rejected_by(
            db,
            document_insert(shop, reservation_id, credential_id=credential_id),
            "fk_order_documents_credential_id_credentials",
        )
