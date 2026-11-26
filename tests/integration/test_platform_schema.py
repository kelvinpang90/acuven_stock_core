"""池与入口两级配额表、配额变更表与平台商品表在真 MySQL 上生效（契约 3.1 的存放、
3.2 落在 channel_allocations 上的两组列、2.9 的存放与第 10 节第 1 条）。

只验表结构与约束，读写这几张表的服务在各自的任务里。demo 库用 conftest 的 db 夹具；
迁移往返照 test_demo_reset_schema 的先例在 production 库上做。
"""

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime

import pytest
from sqlalchemy import Connection, Engine, insert, select
from sqlalchemy.sql.expression import Executable

from alembic import command
from app.domain.channels import PLATFORM, SHOPEE
from app.domain.environment import PRODUCTION
from app.models.catalog import CatalogChange
from app.models.platform import AllocationChange, ChannelAllocation, PoolAllocation, SkuListing
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
