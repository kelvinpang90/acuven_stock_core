"""池与入口两级配额表、配额变更表、平台商品表，以及平台订单、平台订单行与收件箱在真 MySQL 上
生效（契约 3.1 的存放、3.2 落在 channel_allocations 与 platform_order_lines 上的列、2.9 与 5.10
的存放、第 10 节第 1 条）。

只验表结构与约束，读写这几张表的服务在各自的任务里。demo 库用 conftest 的 db 夹具；
迁移往返照 test_demo_reset_schema 的先例在 production 库上做。
"""

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime

import pytest
from sqlalchemy import Connection, Engine, insert, inspect, select
from sqlalchemy.sql.expression import Executable

from alembic import command
from app.domain.channels import PLATFORM, SHOPEE
from app.domain.environment import PRODUCTION
from app.models.catalog import CatalogChange
from app.models.platform import (
    EVENT_ACTIONS,
    EVENT_SOURCES,
    STAGES,
    AllocationChange,
    ChannelAllocation,
    PlatformEvent,
    PlatformOrder,
    PlatformOrderLine,
    PoolAllocation,
    SkuListing,
)
from tests.integration.conftest import (
    add_channel,
    add_credential,
    add_sku,
    assert_rejected_by,
    balance_insert,
    channel_insert,
    new_id,
)
from tests.integration.test_catalog_schema import add_tenant_with_product
from tests.integration.test_demo_reset_schema import (
    Shop,
    add_shop,
    alembic_version,
    has_table,
    head_revision,
    migrate_production,
    rolled_back,
)

SINCE = datetime(2026, 11, 24, 8, 0)
TABLES = ("pool_allocations", "channel_allocations", "allocation_changes")

POOL_BALANCE_FK = "fk_pool_allocations_tenant_id_stock_balances"
CHANNEL_BALANCE_FK = "fk_channel_allocations_tenant_id_stock_balances"
REVIEW_CHECK = "ck_channel_allocations_review_columns"
MANUAL_CHECK = "ck_channel_allocations_manual_columns"

LISTING_UNIQUE = "uq_sku_listings_platform_ids"
# 平台商品没有规格时规格 ID 存 "-"（契约 2.9）。
NO_MODEL = "-"


def add_stocked_sku(db: Connection, shop: Shop) -> str:
    """该租户的一个新 SKU，以及它在这个池里的余额行。"""
    sku_id = add_sku(db, shop.tenant_id)
    db.execute(balance_insert(shop.tenant_id, shop.pool_id, sku_id))
    return sku_id


def pool_allocation_insert(shop: Shop, sku_id: str, **overrides: object) -> Executable:
    values = {"tenant_id": shop.tenant_id, "pool_id": shop.pool_id, "sku_id": sku_id}
    return insert(PoolAllocation).values(values | overrides)


def channel_allocation_insert(shop: Shop, sku_id: str, **overrides: object) -> Executable:
    values = {
        "tenant_id": shop.tenant_id,
        "channel_id": shop.channel_id,
        "pool_id": shop.pool_id,
        "sku_id": sku_id,
    }
    return insert(ChannelAllocation).values(values | overrides)


def change_insert(shop: Shop, sku_id: str, **overrides: object) -> Executable:
    """默认没有操作者与角色。"""
    values = {
        "tenant_id": shop.tenant_id,
        "pool_id": shop.pool_id,
        "sku_id": sku_id,
        "changes": {"safety_stock": {"before": 2, "after": 3}},
        "reason": "restock buffer",
        "credential_id": shop.credential_id,
        "request_id": new_id("req"),
    }
    return insert(AllocationChange).values(values | overrides)


def allocated(db: Connection, shop: Shop) -> str:
    """一个新 SKU，带余额行与池配额行。"""
    sku_id = add_stocked_sku(db, shop)
    db.execute(pool_allocation_insert(shop, sku_id))
    return sku_id


def assert_range_check(
    db: Connection, insert_with: Callable[..., Executable], column: str, constraint: str
) -> None:
    """-1 与 1,000,001 被这条约束拒绝。"""
    for value in (-1, 1000001):
        assert_rejected_by(db, insert_with(**{column: value}), constraint)


# pool_allocations


def test_pool_allocation_defaults(db: Connection):
    shop = add_shop(db)
    sku_id = add_stocked_sku(db, shop)
    db.execute(pool_allocation_insert(shop, sku_id))
    row = db.execute(
        select(PoolAllocation.safety_stock, PoolAllocation.version, PoolAllocation.updated_at)
        .where(PoolAllocation.tenant_id == shop.tenant_id)
        .where(PoolAllocation.sku_id == sku_id)
    ).one()
    assert (row.safety_stock, row.version) == (2, 0)
    assert row.updated_at is not None


def test_pool_allocation_is_one_row_per_pool_and_sku(db: Connection):
    shop = add_shop(db)
    sku_id = allocated(db, shop)
    assert_rejected_by(db, pool_allocation_insert(shop, sku_id), "PRIMARY")


def test_safety_stock_is_between_0_and_1000000(db: Connection):
    shop = add_shop(db)
    sku_id = add_stocked_sku(db, shop)
    assert_range_check(
        db,
        lambda **values: pool_allocation_insert(shop, sku_id, **values),
        "safety_stock",
        "ck_pool_allocations_safety_stock",
    )
    db.execute(pool_allocation_insert(shop, sku_id, safety_stock=0))
    db.execute(pool_allocation_insert(shop, add_stocked_sku(db, shop), safety_stock=1000000))


def test_version_is_nonnegative(db: Connection):
    shop = add_shop(db)
    sku_id = add_stocked_sku(db, shop)
    assert_rejected_by(
        db, pool_allocation_insert(shop, sku_id, version=-1), "ck_pool_allocations_version"
    )
    db.execute(pool_allocation_insert(shop, sku_id, version=0))


def test_pool_allocation_needs_a_balance_row_of_the_same_tenant(db: Connection):
    shop, other = add_shop(db), add_shop(db)
    # SKU 在这个租户里，但这个池没有它的余额行。
    without_balance = add_sku(db, shop.tenant_id)
    other_sku = add_stocked_sku(db, other)
    for sku_id in (without_balance, other_sku, new_id("sku")):
        assert_rejected_by(db, pool_allocation_insert(shop, sku_id), POOL_BALANCE_FK)
    # 另一个租户的余额行。
    assert_rejected_by(
        db, pool_allocation_insert(shop, other_sku, pool_id=other.pool_id), POOL_BALANCE_FK
    )


# channel_allocations


def test_channel_allocation_defaults(db: Connection):
    shop = add_shop(db)
    sku_id = add_stocked_sku(db, shop)
    db.execute(channel_allocation_insert(shop, sku_id))
    row = db.execute(
        select(ChannelAllocation)
        .where(ChannelAllocation.tenant_id == shop.tenant_id)
        .where(ChannelAllocation.sku_id == sku_id)
    ).one()
    assert (row.quota, row.pending, row.review_hold) == (0, 0, False)
    assert (row.review_reason, row.review_since) == (None, None)
    assert (row.manual_confirmed_quota, row.manual_confirmed_at) == (None, None)
    assert row.updated_at is not None


def test_channel_allocation_is_one_row_per_channel_and_sku(db: Connection):
    shop = add_shop(db)
    sku_id = add_stocked_sku(db, shop)
    db.execute(channel_allocation_insert(shop, sku_id))
    assert_rejected_by(db, channel_allocation_insert(shop, sku_id, quota=3), "PRIMARY")
    # 同一池的另一个入口、同一 SKU 可以。
    other_channel = add_channel(db, shop.tenant_id, shop.pool_id)
    db.execute(channel_allocation_insert(shop, sku_id, channel_id=other_channel))


@pytest.mark.parametrize(
    "column, constraint",
    [
        ("quota", "ck_channel_allocations_quota_range"),
        ("pending", "ck_channel_allocations_pending_range"),
    ],
)
def test_quota_and_pending_are_between_0_and_1000000(db: Connection, column: str, constraint: str):
    shop = add_shop(db)
    sku_id = add_stocked_sku(db, shop)
    assert_range_check(
        db, lambda **values: channel_allocation_insert(shop, sku_id, **values), column, constraint
    )
    db.execute(channel_allocation_insert(shop, sku_id, **{column: 0}))
    db.execute(channel_allocation_insert(shop, add_stocked_sku(db, shop), **{column: 1000000}))


def test_manual_confirmed_quota_is_between_0_and_1000000(db: Connection):
    shop = add_shop(db)
    sku_id = add_stocked_sku(db, shop)
    assert_range_check(
        db,
        lambda **values: channel_allocation_insert(
            shop, sku_id, manual_confirmed_at=SINCE, **values
        ),
        "manual_confirmed_quota",
        "ck_channel_allocations_manual_quota_range",
    )
    for quota in (0, 1000000):
        db.execute(
            channel_allocation_insert(
                shop,
                add_stocked_sku(db, shop),
                manual_confirmed_quota=quota,
                manual_confirmed_at=SINCE,
            )
        )


@pytest.mark.parametrize(
    "columns",
    [
        {"review_hold": True},
        {"review_hold": True, "review_reason": "readback mismatch"},
        {"review_hold": True, "review_since": SINCE},
        {"review_reason": "readback mismatch"},
        {"review_since": SINCE},
        {"review_reason": "readback mismatch", "review_since": SINCE},
    ],
)
def test_review_hold_is_set_exactly_when_reason_and_since_are(db: Connection, columns: dict):
    shop = add_shop(db)
    sku_id = add_stocked_sku(db, shop)
    assert_rejected_by(db, channel_allocation_insert(shop, sku_id, **columns), REVIEW_CHECK)


def test_review_hold_with_reason_and_since_is_accepted(db: Connection):
    shop = add_shop(db)
    held = {"review_hold": True, "review_reason": "readback mismatch", "review_since": SINCE}
    db.execute(channel_allocation_insert(shop, add_stocked_sku(db, shop), **held))
    db.execute(channel_allocation_insert(shop, add_stocked_sku(db, shop), review_hold=False))


@pytest.mark.parametrize("columns", [{"manual_confirmed_quota": 3}, {"manual_confirmed_at": SINCE}])
def test_manual_confirmed_quota_and_at_are_both_set_or_both_empty(db: Connection, columns: dict):
    shop = add_shop(db)
    sku_id = add_stocked_sku(db, shop)
    assert_rejected_by(db, channel_allocation_insert(shop, sku_id, **columns), MANUAL_CHECK)
    both = {"manual_confirmed_quota": 3, "manual_confirmed_at": SINCE}
    db.execute(channel_allocation_insert(shop, sku_id, **both))


def test_channel_must_exist_in_the_same_tenant(db: Connection):
    shop, other = add_shop(db), add_shop(db)
    sku_id = add_stocked_sku(db, shop)
    for channel_id in (new_id("chn"), other.channel_id):
        assert_rejected_by(
            db,
            channel_allocation_insert(shop, sku_id, channel_id=channel_id),
            "fk_channel_allocations_tenant_id_sales_channels",
        )


def test_channel_allocation_needs_a_balance_row_of_the_same_tenant(db: Connection):
    shop, other = add_shop(db), add_shop(db)
    without_balance = add_sku(db, shop.tenant_id)
    for sku_id in (without_balance, add_stocked_sku(db, other), new_id("sku")):
        assert_rejected_by(db, channel_allocation_insert(shop, sku_id), CHANNEL_BALANCE_FK)


# allocation_changes


def test_allocation_change_ids_increase_and_created_at_defaults(db: Connection):
    shop = add_shop(db)
    sku_id = allocated(db, shop)
    first = db.execute(change_insert(shop, sku_id)).inserted_primary_key[0]
    second = db.execute(change_insert(shop, sku_id)).inserted_primary_key[0]
    assert isinstance(first, int) and second > first
    created_at = db.execute(
        select(AllocationChange.created_at).where(AllocationChange.id == first)
    ).scalar_one()
    assert created_at is not None


def test_changes_must_be_an_object(db: Connection):
    shop = add_shop(db)
    sku_id = allocated(db, shop)
    db.execute(change_insert(shop, sku_id, changes={}))
    for changes in ([], ["safety_stock"], "changes", 1):
        assert_rejected_by(
            db, change_insert(shop, sku_id, changes=changes), "ck_allocation_changes_changes"
        )


def test_reason_is_required_and_not_empty(db: Connection):
    shop = add_shop(db)
    sku_id = allocated(db, shop)
    assert_rejected_by(db, change_insert(shop, sku_id, reason=""), "ck_allocation_changes_reason")
    assert_rejected_by(
        db, change_insert(shop, sku_id, reason=None), "Column 'reason' cannot be null"
    )


def test_request_id_is_required(db: Connection):
    shop = add_shop(db)
    sku_id = allocated(db, shop)
    assert_rejected_by(
        db, change_insert(shop, sku_id, request_id=None), "Column 'request_id' cannot be null"
    )


def test_actor_ref_and_actor_role_are_both_set_or_both_empty(db: Connection):
    shop = add_shop(db)
    sku_id = allocated(db, shop)
    for actor in ({"actor_ref": "admin_1"}, {"actor_role": "stock_admin"}):
        assert_rejected_by(db, change_insert(shop, sku_id, **actor), "ck_allocation_changes_actor")
    db.execute(change_insert(shop, sku_id))
    db.execute(change_insert(shop, sku_id, actor_ref="admin_1", actor_role="stock_admin"))


def test_allocation_change_needs_a_pool_allocation_row_of_the_same_tenant(db: Connection):
    shop, other = add_shop(db), add_shop(db)
    # 有余额行、没有池配额行。
    for sku_id in (add_stocked_sku(db, shop), allocated(db, other), new_id("sku")):
        assert_rejected_by(
            db, change_insert(shop, sku_id), "fk_allocation_changes_tenant_id_pool_allocations"
        )


def test_credential_must_exist_in_the_same_tenant(db: Connection):
    shop, other = add_shop(db), add_shop(db)
    sku_id = allocated(db, shop)
    for credential_id in (new_id("cred"), other.credential_id):
        assert_rejected_by(
            db,
            change_insert(shop, sku_id, credential_id=credential_id),
            "fk_allocation_changes_tenant_id_credentials",
        )
    db.execute(change_insert(shop, sku_id, credential_id=add_credential(db, shop.tenant_id)))


# sku_listings


def add_platform_channel(db: Connection, shop: Shop) -> str:
    """同一池上的一个 platform 入口。"""
    channel_id = new_id("chn")
    platform = {"kind": PLATFORM, "platform": SHOPEE, "platform_shop_id": new_id("shop")}
    db.execute(channel_insert(shop.tenant_id, shop.pool_id, channel_id=channel_id, **platform))
    return channel_id


def add_platform_shop(db: Connection) -> Shop:
    """入口换成 platform 入口的 Shop：平台商品挂在 platform 入口上。"""
    shop = add_shop(db)
    return replace(shop, channel_id=add_platform_channel(db, shop))


def listing_insert(shop: Shop, sku_id: str, **overrides: object) -> Executable:
    """默认是一个新的平台商品、没有规格，没有操作者与角色。"""
    values = {
        "listing_id": new_id("lst"),
        "tenant_id": shop.tenant_id,
        "channel_id": shop.channel_id,
        "platform_item_id": new_id("item"),
        "platform_model_id": NO_MODEL,
        "sku_id": sku_id,
        "status": "active",
        "version": 1,
        "credential_id": shop.credential_id,
        "request_id": new_id("req"),
    }
    return insert(SkuListing).values(values | overrides)


def test_listing_timestamps_default_and_actor_is_optional(db: Connection):
    shop = add_platform_shop(db)
    listing_id = new_id("lst")
    db.execute(listing_insert(shop, add_sku(db, shop.tenant_id), listing_id=listing_id))
    row = db.execute(select(SkuListing).where(SkuListing.listing_id == listing_id)).one()
    assert (row.actor_ref, row.actor_role) == (None, None)
    assert row.created_at is not None and row.updated_at is not None


def test_listing_id_is_the_primary_key(db: Connection):
    shop = add_platform_shop(db)
    sku_id = add_sku(db, shop.tenant_id)
    listing_id = new_id("lst")
    db.execute(listing_insert(shop, sku_id, listing_id=listing_id))
    assert_rejected_by(db, listing_insert(shop, sku_id, listing_id=listing_id), "PRIMARY")


def test_a_platform_model_has_one_row_per_channel(db: Connection):
    shop = add_platform_shop(db)
    sku_id = add_sku(db, shop.tenant_id)
    ids = {"platform_item_id": "shopee:2201.9", "platform_model_id": "m-1"}
    db.execute(listing_insert(shop, sku_id, **ids))
    assert_rejected_by(db, listing_insert(shop, sku_id, **ids), LISTING_UNIQUE)
    # 换一个 SKU、改成 inactive 也不行：改挂与解除都改原来那一行。
    other_sku = add_sku(db, shop.tenant_id)
    relinked = listing_insert(shop, other_sku, status="inactive", **ids)
    assert_rejected_by(db, relinked, LISTING_UNIQUE)
    # 同一租户的另一个 platform 入口可以有同样的两个 ID。
    other_channel = add_platform_channel(db, shop)
    db.execute(listing_insert(shop, sku_id, channel_id=other_channel, **ids))


def test_listings_differing_only_in_platform_model_id_are_accepted(db: Connection):
    shop = add_platform_shop(db)
    item = {"platform_item_id": "shopee:2201.9"}
    for model_id in (NO_MODEL, "m-1", "m-2"):
        db.execute(
            listing_insert(shop, add_sku(db, shop.tenant_id), platform_model_id=model_id, **item)
        )


def test_platform_ids_differing_only_in_case_are_different_ids(db: Connection):
    # 编码类列不区分大小写；平台 ID 区分（utf8mb4_bin）。
    shop = add_platform_shop(db)
    for item_id, model_id in (("abc", "m-x"), ("ABC", "m-x"), ("abc", "M-X"), ("Abc", "m-X")):
        ids = {"platform_item_id": item_id, "platform_model_id": model_id}
        db.execute(listing_insert(shop, add_sku(db, shop.tenant_id), **ids))
    assert_rejected_by(
        db,
        listing_insert(
            shop, add_sku(db, shop.tenant_id), platform_item_id="ABC", platform_model_id="m-x"
        ),
        LISTING_UNIQUE,
    )


@pytest.mark.parametrize(
    "column, constraint",
    [
        ("platform_item_id", "ck_sku_listings_item_id"),
        ("platform_model_id", "ck_sku_listings_model_id"),
    ],
)
def test_platform_ids_use_the_allowed_characters(db: Connection, column: str, constraint: str):
    shop = add_platform_shop(db)
    # 两个 ID 里只有被测的那一个变化，另一个取固定值，唯一约束不拦。
    fixed = {"platform_item_id": "item-1", "platform_model_id": NO_MODEL}
    for value in ("1", "-", "a" * 64, "Item.01_my:main-2"):
        values = fixed | {column: value}
        db.execute(listing_insert(shop, add_sku(db, shop.tenant_id), **values))
    for value in ("", "item 1", "item/1", "item\n", "itém", "item@1"):
        values = fixed | {column: value}
        assert_rejected_by(
            db, listing_insert(shop, add_sku(db, shop.tenant_id), **values), constraint
        )


def test_listing_status_must_be_an_exact_known_value(db: Connection):
    shop = add_platform_shop(db)
    sku_id = add_sku(db, shop.tenant_id)
    for status in ("active", "inactive"):
        db.execute(listing_insert(shop, sku_id, status=status))
    for status in ("ACTIVE", "Inactive", "unlinked", ""):
        assert_rejected_by(
            db, listing_insert(shop, sku_id, status=status), "ck_sku_listings_status"
        )


def test_listing_version_starts_at_one(db: Connection):
    shop = add_platform_shop(db)
    sku_id = add_sku(db, shop.tenant_id)
    for version in (0, -1):
        assert_rejected_by(
            db, listing_insert(shop, sku_id, version=version), "ck_sku_listings_version"
        )
    db.execute(listing_insert(shop, sku_id, version=1))
    db.execute(listing_insert(shop, sku_id, version=2))


def test_a_sku_may_have_two_active_listings_on_one_channel_in_the_database(db: Connection):
    # 同入口同 SKU 最多一条 active 由命令在目录写锁内保证，不是数据库约束（契约 2.9）。
    shop = add_platform_shop(db)
    sku_id = add_sku(db, shop.tenant_id)
    db.execute(listing_insert(shop, sku_id))
    db.execute(listing_insert(shop, sku_id))
    active = db.execute(
        select(SkuListing.listing_id)
        .where(SkuListing.tenant_id == shop.tenant_id)
        .where(SkuListing.channel_id == shop.channel_id)
        .where(SkuListing.sku_id == sku_id)
        .where(SkuListing.status == "active")
    ).all()
    assert len(active) == 2


def test_listing_actor_ref_and_actor_role_are_both_set_or_both_empty(db: Connection):
    shop = add_platform_shop(db)
    sku_id = add_sku(db, shop.tenant_id)
    for actor in ({"actor_ref": "admin_1"}, {"actor_role": "catalog_admin"}):
        assert_rejected_by(db, listing_insert(shop, sku_id, **actor), "ck_sku_listings_actor")
    db.execute(listing_insert(shop, sku_id, actor_ref="admin_1", actor_role="catalog_admin"))


@pytest.mark.parametrize("column", ["credential_id", "request_id"])
def test_listing_credential_and_request_id_are_required(db: Connection, column: str):
    shop = add_platform_shop(db)
    sku_id = add_sku(db, shop.tenant_id)
    assert_rejected_by(
        db, listing_insert(shop, sku_id, **{column: None}), f"Column '{column}' cannot be null"
    )


def test_listing_channel_must_exist_in_the_same_tenant(db: Connection):
    shop, other = add_platform_shop(db), add_platform_shop(db)
    sku_id = add_sku(db, shop.tenant_id)
    for channel_id in (new_id("chn"), other.channel_id):
        assert_rejected_by(
            db,
            listing_insert(shop, sku_id, channel_id=channel_id),
            "fk_sku_listings_tenant_id_sales_channels",
        )


def test_listing_sku_must_exist_in_the_same_tenant(db: Connection):
    shop, other = add_platform_shop(db), add_platform_shop(db)
    for sku_id in (new_id("sku"), add_sku(db, other.tenant_id)):
        assert_rejected_by(db, listing_insert(shop, sku_id), "fk_sku_listings_tenant_id_skus")


def test_listing_credential_must_exist_in_the_same_tenant(db: Connection):
    shop, other = add_platform_shop(db), add_platform_shop(db)
    sku_id = add_sku(db, shop.tenant_id)
    for credential_id in (new_id("cred"), other.credential_id):
        assert_rejected_by(
            db,
            listing_insert(shop, sku_id, credential_id=credential_id),
            "fk_sku_listings_tenant_id_credentials",
        )
    db.execute(listing_insert(shop, sku_id, credential_id=add_credential(db, shop.tenant_id)))


# platform_orders

PLATFORM_ORDER_TABLES = ("platform_orders", "platform_order_lines", "platform_events")
ORDER_ID_UNIQUE = "uq_platform_orders_tenant_id_channel_id_platform_order_id"
ORDER_REF_UNIQUE = "uq_platform_orders_tenant_id_channel_id_order_ref"
ORDER_REF_PREFIX = "shp:123456:"
HELD = {"stage": "held", "attention": "unmapped"}


def order_insert(shop: Shop, **overrides: object) -> Executable:
    """默认是一个新的未付款平台订单，order_ref 由平台订单号拼出。"""
    platform_order_id = overrides.get("platform_order_id", new_id("ord"))
    values = {
        "platform_order_row_id": new_id("pfo"),
        "tenant_id": shop.tenant_id,
        "channel_id": shop.channel_id,
        "platform_order_id": platform_order_id,
        "order_ref": f"{ORDER_REF_PREFIX}{platform_order_id}",
    }
    return insert(PlatformOrder).values(values | overrides)


def add_order(db: Connection, shop: Shop, **overrides: object) -> str:
    row_id = new_id("pfo")
    db.execute(order_insert(shop, platform_order_row_id=row_id, **overrides))
    return row_id


def test_platform_order_defaults(db: Connection):
    shop = add_platform_shop(db)
    row_id = add_order(db, shop)
    row = db.execute(
        select(PlatformOrder).where(PlatformOrder.platform_order_row_id == row_id)
    ).one()
    assert (row.stage, row.pull_attempts) == ("unpaid", 0)
    assert (row.attention, row.attention_code) == (None, None)
    assert (row.ready_at, row.next_pull_at) == (None, None)
    assert row.created_at is not None and row.updated_at is not None


def test_platform_order_row_id_is_the_primary_key(db: Connection):
    shop = add_platform_shop(db)
    row_id = add_order(db, shop)
    assert_rejected_by(db, order_insert(shop, platform_order_row_id=row_id), "PRIMARY")


def test_platform_order_row_id_is_unique_per_tenant_for_composite_foreign_keys(db: Connection):
    # 主键已保证唯一，违反这条必先违反主键；它只作订单行与告警的复合外键的引用目标，
    # 引用它的外键见 test_line_must_belong_to_a_platform_order_of_the_same_tenant。
    uniques = {
        unique["name"]: unique["column_names"]
        for unique in inspect(db).get_unique_constraints("platform_orders")
    }
    assert uniques["uq_platform_orders_tenant_id_platform_order_row_id"] == [
        "tenant_id",
        "platform_order_row_id",
    ]


def test_a_platform_order_id_has_one_row_per_channel(db: Connection):
    shop = add_platform_shop(db)
    add_order(db, shop, platform_order_id="2611210ABCD")
    duplicate = order_insert(shop, platform_order_id="2611210ABCD", order_ref=new_id("ref"))
    assert_rejected_by(db, duplicate, ORDER_ID_UNIQUE)
    # 同一租户的另一个 platform 入口可以有同样的平台订单号。
    other_channel = add_platform_channel(db, shop)
    db.execute(order_insert(shop, channel_id=other_channel, platform_order_id="2611210ABCD"))


def test_an_order_ref_has_one_row_per_channel(db: Connection):
    shop = add_platform_shop(db)
    order_ref = f"{ORDER_REF_PREFIX}2611210ABCD"
    add_order(db, shop, order_ref=order_ref)
    assert_rejected_by(db, order_insert(shop, order_ref=order_ref), ORDER_REF_UNIQUE)
    other_channel = add_platform_channel(db, shop)
    db.execute(order_insert(shop, channel_id=other_channel, order_ref=order_ref))


def test_platform_order_ids_and_order_refs_differing_only_in_case_or_accent_are_different(
    db: Connection,
):
    # 表的默认排序规则会把它们当成同一个；这几列用 utf8mb4_bin。
    shop = add_platform_shop(db)
    for platform_order_id in ("abc", "ABC", "Abc", "abé", "abe"):
        db.execute(order_insert(shop, platform_order_id=platform_order_id))
    assert_rejected_by(
        db, order_insert(shop, platform_order_id="ABC", order_ref=new_id("ref")), ORDER_ID_UNIQUE
    )
    same_ref = order_insert(shop, order_ref=f"{ORDER_REF_PREFIX}abé")
    assert_rejected_by(db, same_ref, ORDER_REF_UNIQUE)


def test_platform_order_id_is_required_and_not_empty(db: Connection):
    shop = add_platform_shop(db)
    assert_rejected_by(
        db,
        order_insert(shop, platform_order_id="", order_ref=new_id("ref")),
        "ck_platform_orders_platform_order_id_not_empty",
    )
    assert_rejected_by(
        db,
        order_insert(shop, platform_order_id=None, order_ref=new_id("ref")),
        "Column 'platform_order_id' cannot be null",
    )


def test_order_ref_is_1_to_112_characters(db: Connection):
    shop = add_platform_shop(db)
    for order_ref in ("", "a" * 113, ORDER_REF_PREFIX + "A" * 102):
        assert_rejected_by(
            db, order_insert(shop, order_ref=order_ref), "ck_platform_orders_order_ref_length"
        )
    assert_rejected_by(db, order_insert(shop, order_ref=None), "Column 'order_ref' cannot be null")
    for order_ref in ("a", ORDER_REF_PREFIX + "A" * 101):
        db.execute(order_insert(shop, order_ref=order_ref))


def test_stage_must_be_an_exact_known_value(db: Connection):
    shop = add_platform_shop(db)
    for stage in STAGES:
        db.execute(order_insert(shop, stage=stage, attention="unmapped"))
    for stage in ("UNPAID", "Held", "paid", ""):
        assert_rejected_by(db, order_insert(shop, stage=stage), "ck_platform_orders_stage")


@pytest.mark.parametrize("column", ["attention", "attention_code"])
def test_attention_and_attention_code_are_not_empty_strings(db: Connection, column: str):
    shop = add_platform_shop(db)
    values = {"attention": "unmapped", "attention_code": "SKU_NOT_FOUND"} | {column: ""}
    assert_rejected_by(db, order_insert(shop, **values), f"ck_platform_orders_{column}_not_empty")


def test_a_held_order_has_an_attention(db: Connection):
    shop = add_platform_shop(db)
    assert_rejected_by(db, order_insert(shop, stage="held"), "ck_platform_orders_held_attention")
    db.execute(order_insert(shop, **HELD))
    db.execute(order_insert(shop, **HELD, attention_code="INSUFFICIENT_STOCK"))
    # 已确认之后的拒绝只写 attention，阶段不变（契约 5.10 第 6 步）。
    db.execute(order_insert(shop, stage="committed", attention="core_rejected"))


def test_attention_code_needs_an_attention(db: Connection):
    shop = add_platform_shop(db)
    assert_rejected_by(
        db,
        order_insert(shop, attention_code="INSUFFICIENT_STOCK"),
        "ck_platform_orders_attention_code_needs_attention",
    )
    db.execute(order_insert(shop, attention="core_rejected", attention_code="STATE_CONFLICT"))


def test_pull_attempts_is_nonnegative(db: Connection):
    shop = add_platform_shop(db)
    negative = order_insert(shop, pull_attempts=-1)
    assert_rejected_by(db, negative, "ck_platform_orders_pull_attempts")
    for attempts in (0, 3):
        db.execute(order_insert(shop, pull_attempts=attempts, next_pull_at=SINCE))


def test_platform_order_channel_must_exist_in_the_same_tenant(db: Connection):
    shop, other = add_platform_shop(db), add_platform_shop(db)
    for channel_id in (new_id("chn"), other.channel_id):
        assert_rejected_by(
            db,
            order_insert(shop, channel_id=channel_id),
            "fk_platform_orders_tenant_id_sales_channels",
        )


# platform_order_lines

LINE_PK = "PRIMARY"
ORDER_FK = "fk_platform_order_lines_tenant_id_platform_orders"
QUOTA_CHECK = "ck_platform_order_lines_quota_taken_returned"
DISCREPANCY_CHECK = "ck_platform_order_lines_known_discrepancy"


def line_insert(shop: Shop, order_row_id: str, **overrides: object) -> Executable:
    """默认是一行 2 件、SKU 还没定下、平台商品没有规格。"""
    values = {
        "tenant_id": shop.tenant_id,
        "platform_order_row_id": order_row_id,
        "platform_line_id": new_id("line"),
        "platform_item_id": new_id("item"),
        "platform_model_id": NO_MODEL,
        "quantity": 2,
    }
    return insert(PlatformOrderLine).values(values | overrides)


def ordered(db: Connection) -> tuple[Shop, str]:
    """一个 platform 入口与它上面的一个平台订单。"""
    shop = add_platform_shop(db)
    return shop, add_order(db, shop)


def test_platform_order_line_defaults(db: Connection):
    shop, order = ordered(db)
    db.execute(line_insert(shop, order, platform_line_id="1"))
    row = db.execute(
        select(PlatformOrderLine)
        .where(PlatformOrderLine.tenant_id == shop.tenant_id)
        .where(PlatformOrderLine.platform_order_row_id == order)
    ).one()
    assert (row.cancelled_qty, row.quota_taken, row.quota_returned) == (0, 0, 0)
    assert (row.sku_id, row.line_ref) == (None, None)
    assert (row.known_discrepancy, row.known_discrepancy_note) == (False, None)


def test_a_platform_line_has_one_row_per_order(db: Connection):
    shop, order = ordered(db)
    db.execute(line_insert(shop, order, platform_line_id="line-1"))
    assert_rejected_by(db, line_insert(shop, order, platform_line_id="line-1"), LINE_PK)
    # 只差大小写的是另一行；另一个平台订单可以有同样的平台行 ID。
    db.execute(line_insert(shop, order, platform_line_id="LINE-1"))
    db.execute(line_insert(shop, add_order(db, shop), platform_line_id="line-1"))


def test_line_must_belong_to_a_platform_order_of_the_same_tenant(db: Connection):
    shop, order = ordered(db)
    other, other_order = ordered(db)
    for order_row_id in (new_id("pfo"), other_order):
        assert_rejected_by(db, line_insert(shop, order_row_id), ORDER_FK)
    db.execute(line_insert(shop, order))
    db.execute(line_insert(other, other_order))


def test_line_sku_is_optional_and_must_exist_in_the_same_tenant(db: Connection):
    shop, order = ordered(db)
    other = add_platform_shop(db)
    for sku_id in (new_id("sku"), add_sku(db, other.tenant_id)):
        assert_rejected_by(
            db, line_insert(shop, order, sku_id=sku_id), "fk_platform_order_lines_tenant_id_skus"
        )
    db.execute(line_insert(shop, order, sku_id=add_sku(db, shop.tenant_id)))
    db.execute(line_insert(shop, order, sku_id=None))


@pytest.mark.parametrize("column", ["platform_line_id", "platform_item_id", "platform_model_id"])
def test_line_platform_ids_are_required_and_not_empty(db: Connection, column: str):
    shop, order = ordered(db)
    assert_rejected_by(
        db,
        line_insert(shop, order, **{column: ""}),
        f"ck_platform_order_lines_{column}_not_empty",
    )
    assert_rejected_by(
        db, line_insert(shop, order, **{column: None}), f"Column '{column}' cannot be null"
    )


@pytest.mark.parametrize("column", ["platform_item_id", "platform_model_id"])
def test_line_platform_item_ids_are_stored_as_given(db: Connection, column: str):
    # 字符不合格的找不到平台商品、按 unmapped 进待处理，不让新订单事件写不进来。
    shop, order = ordered(db)
    for value in ("item 1", "item/1", "itém", "item\n", "a" * 128, "-"):
        db.execute(line_insert(shop, order, **{column: value}))


def test_line_quantity_is_between_1_and_1000000(db: Connection):
    shop, order = ordered(db)
    # 小于 0 的件数还会违反 cancelled_qty 与配额两条，不在这里试。
    for quantity in (0, 1000001):
        assert_rejected_by(
            db, line_insert(shop, order, quantity=quantity), "ck_platform_order_lines_quantity"
        )
    for quantity in (1, 1000000):
        db.execute(line_insert(shop, order, quantity=quantity))


def test_cancelled_qty_is_between_0_and_quantity(db: Connection):
    shop, order = ordered(db)
    for cancelled in (-1, 3):
        assert_rejected_by(
            db,
            line_insert(shop, order, cancelled_qty=cancelled),
            "ck_platform_order_lines_cancelled_qty",
        )
    for cancelled in (0, 2):
        db.execute(line_insert(shop, order, cancelled_qty=cancelled))


@pytest.mark.parametrize(
    "taken, returned",
    [(0, -1), (-1, -1), (1, 2), (3, 0), (3, 3)],
)
def test_quota_returned_is_within_quota_taken_within_quantity_rejected(
    db: Connection, taken: int, returned: int
):
    shop, order = ordered(db)
    quota = {"quota_taken": taken, "quota_returned": returned}
    assert_rejected_by(db, line_insert(shop, order, **quota), QUOTA_CHECK)


def test_quota_returned_is_within_quota_taken_within_quantity_accepted(db: Connection):
    shop, order = ordered(db)
    for taken, returned in ((0, 0), (1, 0), (2, 1), (2, 2)):
        db.execute(line_insert(shop, order, quota_taken=taken, quota_returned=returned))


@pytest.mark.parametrize(
    "columns",
    [
        {"known_discrepancy": True},
        {"known_discrepancy": True, "known_discrepancy_note": ""},
        {"known_discrepancy": False, "known_discrepancy_note": "recounted after dispatch"},
        {"known_discrepancy_note": "recounted after dispatch"},
    ],
)
def test_known_discrepancy_is_set_exactly_when_it_has_a_note(db: Connection, columns: dict):
    shop, order = ordered(db)
    assert_rejected_by(db, line_insert(shop, order, **columns), DISCREPANCY_CHECK)


def test_known_discrepancy_with_a_note_is_accepted(db: Connection):
    shop, order = ordered(db)
    marked = {"known_discrepancy": True, "known_discrepancy_note": "recounted after dispatch"}
    db.execute(line_insert(shop, order, **marked))
    db.execute(line_insert(shop, order, known_discrepancy=False, line_ref="L1", sku_id=None))


# platform_events

DEDUPE_UNIQUE = "uq_platform_events_dedupe_key"
MANUAL_UNIQUE = "uq_platform_events_tenant_id_credential_id_manual_key"
MANUAL_CHECK_NAME = "ck_platform_events_manual_columns"
RESULT_CODE_CHECK = "ck_platform_events_result_code_by_result"
REQUEST_HASH = "0" * 64
RECEIVED_AT = datetime(2026, 11, 24, 9, 0)
APPLIED_AT = datetime(2026, 11, 24, 9, 1)


def event_insert(shop: Shop, **overrides: object) -> Executable:
    """默认是一条还没处理的 webhook 订单级新订单事件。"""
    values = {
        "event_row_id": new_id("pev"),
        "tenant_id": shop.tenant_id,
        "channel_id": shop.channel_id,
        "platform_order_id": new_id("ord"),
        "platform_line_id": "",
        "action": "created",
        "event_key": new_id("evt"),
        "source": "webhook",
        "payload": {"lines": [{"platform_line_id": "1", "quantity": 2}]},
        "received_at": RECEIVED_AT,
        "credential_id": shop.credential_id,
    }
    return insert(PlatformEvent).values(values | overrides)


def manual(**overrides: object) -> dict[str, object]:
    """后台登记的事件：有提交的幂等键与正文摘要。"""
    values = {"source": "manual", "manual_key": new_id("idem"), "request_hash": REQUEST_HASH}
    return values | overrides


def processed(result: str, result_code: str | None = None) -> dict[str, object]:
    return {"applied_at": APPLIED_AT, "result": result, "result_code": result_code}


def test_unprocessed_auto_event_defaults_and_order_level_line_id_is_empty(db: Connection):
    shop = add_platform_shop(db)
    event_row_id = new_id("pev")
    db.execute(event_insert(shop, event_row_id=event_row_id))
    row = db.execute(select(PlatformEvent).where(PlatformEvent.event_row_id == event_row_id)).one()
    assert row.platform_line_id == ""
    assert (row.manual_key, row.request_hash) == (None, None)
    assert (row.applied_at, row.result, row.result_code) == (None, None, None)
    assert row.received_at == RECEIVED_AT


def test_event_row_id_is_the_primary_key(db: Connection):
    shop = add_platform_shop(db)
    event_row_id = new_id("pev")
    db.execute(event_insert(shop, event_row_id=event_row_id))
    assert_rejected_by(db, event_insert(shop, event_row_id=event_row_id), "PRIMARY")


def test_dedupe_key_rejects_a_second_event_with_the_same_key(db: Connection):
    shop = add_platform_shop(db)
    key = {
        "platform_order_id": "2611210ABCD",
        "platform_line_id": "line-1",
        "action": "shipped",
        "event_key": "evt-1",
    }
    db.execute(event_insert(shop, **key))
    assert_rejected_by(db, event_insert(shop, **key), DEDUPE_UNIQUE)
    # 换一个来源也是同一个事件。
    assert_rejected_by(db, event_insert(shop, source="pull", **key), DEDUPE_UNIQUE)
    # 只差 platform_line_id、只差 event_key（含只差大小写）的都能写入。
    for changed in (
        {"platform_line_id": "line-2"},
        {"platform_line_id": ""},
        {"event_key": "evt-2"},
        {"event_key": "EVT-1"},
    ):
        db.execute(event_insert(shop, **(key | changed)))
    # 同一租户的另一个入口可以有同样的键。
    db.execute(event_insert(shop, channel_id=add_platform_channel(db, shop), **key))


def test_auto_events_do_not_collide_on_the_manual_key(db: Connection):
    shop = add_platform_shop(db)
    for source in ("webhook", "pull", "webhook"):
        db.execute(event_insert(shop, source=source))
    rows = db.execute(
        select(PlatformEvent.event_row_id)
        .where(PlatformEvent.tenant_id == shop.tenant_id)
        .where(PlatformEvent.credential_id == shop.credential_id)
        .where(PlatformEvent.manual_key.is_(None))
    ).all()
    assert len(rows) == 3


def test_a_manual_key_is_used_once_per_credential(db: Connection):
    shop = add_platform_shop(db)
    key = new_id("idem")
    db.execute(event_insert(shop, **manual(manual_key=key)))
    # 同一个提交键换了内容再交（另一个动作、另一个摘要）也拒绝。
    again = event_insert(shop, action="cancelled", **manual(manual_key=key, request_hash="1" * 64))
    assert_rejected_by(db, again, MANUAL_UNIQUE)
    # 另一个管理员主体可以用同样的键。
    other_credential = add_credential(db, shop.tenant_id)
    db.execute(event_insert(shop, credential_id=other_credential, **manual(manual_key=key)))


@pytest.mark.parametrize("column", ["platform_order_id", "event_key"])
def test_event_platform_order_id_and_event_key_are_required_and_not_empty(
    db: Connection, column: str
):
    shop = add_platform_shop(db)
    assert_rejected_by(
        db, event_insert(shop, **{column: ""}), f"ck_platform_events_{column}_not_empty"
    )
    assert_rejected_by(
        db, event_insert(shop, **{column: None}), f"Column '{column}' cannot be null"
    )


# payload 传 None 存成 JSON 的 null，由 test_payload_must_be_an_object 那条约束管。
@pytest.mark.parametrize("column", ["platform_line_id", "received_at"])
def test_event_line_id_and_received_at_are_required(db: Connection, column: str):
    shop = add_platform_shop(db)
    assert_rejected_by(
        db, event_insert(shop, **{column: None}), f"Column '{column}' cannot be null"
    )


def test_event_action_must_be_an_exact_known_value(db: Connection):
    shop = add_platform_shop(db)
    for action in EVENT_ACTIONS:
        db.execute(event_insert(shop, action=action))
    for action in ("CREATED", "Shipped", "refunded", ""):
        assert_rejected_by(db, event_insert(shop, action=action), "ck_platform_events_action")


def test_event_source_must_be_an_exact_known_value(db: Connection):
    shop = add_platform_shop(db)
    for source in EVENT_SOURCES:
        extra = manual() if source == "manual" else {}
        db.execute(event_insert(shop, **({"source": source} | extra)))
    for source in ("Webhook", "PULL", "api", ""):
        assert_rejected_by(db, event_insert(shop, source=source), "ck_platform_events_source")


def test_payload_must_be_an_object(db: Connection):
    shop = add_platform_shop(db)
    db.execute(event_insert(shop, payload={}))
    for payload in ([], [{"quantity": 2}], "payload", 1):
        assert_rejected_by(db, event_insert(shop, payload=payload), "ck_platform_events_payload")


@pytest.mark.parametrize(
    "columns",
    [
        {"source": "manual"},
        {"source": "manual", "manual_key": "idem-1"},
        {"source": "manual", "request_hash": REQUEST_HASH},
        {"source": "webhook", "manual_key": "idem-1"},
        {"source": "pull", "request_hash": REQUEST_HASH},
        {"source": "webhook", "manual_key": "idem-1", "request_hash": REQUEST_HASH},
    ],
)
def test_manual_key_and_request_hash_are_set_exactly_for_manual_events(
    db: Connection, columns: dict
):
    shop = add_platform_shop(db)
    assert_rejected_by(db, event_insert(shop, **columns), MANUAL_CHECK_NAME)


@pytest.mark.parametrize("column", ["manual_key", "request_hash"])
def test_manual_key_and_request_hash_are_not_empty(db: Connection, column: str):
    shop = add_platform_shop(db)
    assert_rejected_by(
        db, event_insert(shop, **manual(**{column: ""})), f"ck_platform_events_{column}_not_empty"
    )


def test_event_result_must_be_an_exact_known_value(db: Connection):
    shop = add_platform_shop(db)
    for result, code in (
        ("applied", None),
        ("ignored", None),
        ("held", "unmapped"),
        ("attention", "STATE_CONFLICT"),
    ):
        db.execute(event_insert(shop, **processed(result, code)))
    for result in ("APPLIED", "Ignored", "rejected", ""):
        assert_rejected_by(
            db, event_insert(shop, **processed(result)), "ck_platform_events_result_value"
        )


@pytest.mark.parametrize(
    "columns", [{"applied_at": APPLIED_AT}, {"result": "applied"}, {"result": "ignored"}]
)
def test_applied_at_and_result_are_both_set_or_both_empty(db: Connection, columns: dict):
    shop = add_platform_shop(db)
    assert_rejected_by(db, event_insert(shop, **columns), "ck_platform_events_applied_result")


def test_result_code_is_not_an_empty_string(db: Connection):
    shop = add_platform_shop(db)
    empty = event_insert(shop, **processed("held", ""))
    assert_rejected_by(db, empty, "ck_platform_events_result_code_not_empty")


@pytest.mark.parametrize(
    "columns",
    [
        processed("held"),
        processed("attention"),
        processed("ignored", "STATE_CONFLICT"),
        # 还没处理的事件没有结果，也没有原因。
        {"result_code": "unmapped"},
    ],
)
def test_result_code_follows_the_result_rejected(db: Connection, columns: dict):
    shop = add_platform_shop(db)
    assert_rejected_by(db, event_insert(shop, **columns), RESULT_CODE_CHECK)


def test_result_code_follows_the_result_accepted(db: Connection):
    shop = add_platform_shop(db)
    for result, code in (
        ("applied", None),
        # 成功但另标缺陷。
        ("applied", "defect"),
        ("held", "INSUFFICIENT_STOCK"),
        ("attention", "QUANTITY_EXCEEDS_OUTSTANDING"),
        ("ignored", None),
    ):
        db.execute(event_insert(shop, **processed(result, code)))


def test_event_channel_must_exist_in_the_same_tenant(db: Connection):
    shop, other = add_platform_shop(db), add_platform_shop(db)
    for channel_id in (new_id("chn"), other.channel_id):
        assert_rejected_by(
            db,
            event_insert(shop, channel_id=channel_id),
            "fk_platform_events_tenant_id_sales_channels",
        )


def test_event_credential_must_exist_in_the_same_tenant(db: Connection):
    shop, other = add_platform_shop(db), add_platform_shop(db)
    for credential_id in (new_id("cred"), other.credential_id):
        assert_rejected_by(
            db,
            event_insert(shop, credential_id=credential_id),
            "fk_platform_events_tenant_id_credentials",
        )
    db.execute(event_insert(shop, credential_id=add_credential(db, shop.tenant_id)))


# production 库上的迁移往返

OBJECT_TYPE_CHECK = "ck_catalog_changes_object_type"


def listing_change_insert(conn: Connection) -> Executable:
    """一个新租户里的一条平台商品目录变更记录。"""
    tenant_id, product_id = add_tenant_with_product(conn)
    return insert(CatalogChange).values(
        tenant_id=tenant_id,
        event_type="listing_linked",
        object_type="listing",
        object_id=new_id("lst"),
        product_id=product_id,
        version=1,
        changes={"status": {"before": None, "after": "active"}},
        credential_id=add_credential(conn, tenant_id),
        actor_ref="admin_1",
        actor_role="catalog_admin",
        request_id=new_id("req"),
        idempotency_key=new_id("idem"),
    )


def test_downgrade_to_0013_drops_the_allocation_tables_and_upgrade_restores_them(
    production_engine: Engine,
):
    migrate_production(command.downgrade, "0013", PRODUCTION)
    try:
        assert alembic_version(production_engine) == "0013"
        for table in TABLES:
            assert not has_table(production_engine, table)
    finally:
        migrate_production(command.upgrade, "head", PRODUCTION)
    assert alembic_version(production_engine) == head_revision()
    for table in TABLES:
        assert has_table(production_engine, table)


def test_downgrade_to_0014_drops_sku_listings_and_upgrade_restores_it(
    production_engine: Engine,
):
    migrate_production(command.downgrade, "0014", PRODUCTION)
    try:
        assert alembic_version(production_engine) == "0014"
        assert not has_table(production_engine, "sku_listings")
        for table in TABLES:
            assert has_table(production_engine, table)
        # 原 object_type 约束回来了；插入的行回滚，升回 head 时不留 listing 记录。
        with rolled_back(production_engine) as conn:
            assert_rejected_by(conn, listing_change_insert(conn), OBJECT_TYPE_CHECK)
    finally:
        migrate_production(command.upgrade, "head", PRODUCTION)
    assert alembic_version(production_engine) == head_revision()
    assert has_table(production_engine, "sku_listings")
    with rolled_back(production_engine) as conn:
        conn.execute(listing_change_insert(conn))


def test_downgrade_to_0015_drops_the_platform_order_tables_and_upgrade_restores_them(
    production_engine: Engine,
):
    migrate_production(command.downgrade, "0015", PRODUCTION)
    try:
        assert alembic_version(production_engine) == "0015"
        for table in PLATFORM_ORDER_TABLES:
            assert not has_table(production_engine, table)
        assert has_table(production_engine, "sku_listings")
    finally:
        migrate_production(command.upgrade, "head", PRODUCTION)
    assert alembic_version(production_engine) == head_revision()
    for table in PLATFORM_ORDER_TABLES:
        assert has_table(production_engine, table)
