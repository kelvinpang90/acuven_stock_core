"""库存侧各表的约束在真 MySQL 上生效（契约第 3 节、4.1、4.3、7.1 与第 10 节第 1 条）。

这些规则由数据库兜底，服务层的 bug 也绕不过去。
连接夹具 db、assert_rejected_by 与各表的造数见 conftest。
0013 的升降在 production 库上做，见文件末尾。
"""

import pytest
from sqlalchemy import Connection, Engine, delete, insert, inspect, select
from sqlalchemy.sql.expression import Executable

from alembic import command
from app.domain.channels import API, BACKOFFICE, PLATFORM, PLATFORMS, SHOPEE, TIKTOK_SHOP
from app.domain.environment import PRODUCTION
from app.models.catalog import Tenant
from app.models.inventory import (
    IdempotencyRecord,
    Location,
    SalesChannel,
    StockBalance,
    StockPool,
)
from tests.integration.conftest import (
    add_balance,
    add_channel,
    add_credential,
    add_entry,
    add_location,
    add_pool,
    add_sku,
    add_tenant,
    assert_rejected_by,
    assert_rejected_by_one_of,
    balance_insert,
    channel_insert,
    entry_insert,
    new_id,
    pool_insert,
    went_live,
)
from tests.integration.test_demo_reset_schema import (
    alembic_version,
    head_revision,
    migrate_production,
    rolled_back,
)

# 只有 stock_balance 为负时，reserved + unavailable ≤ stock_balance 必然同时被违反；
# 不依赖 MySQL 先报哪一条，下面的测试对这种行接受两者之一。
NON_NEGATIVE_ALONE = ["reserved", "unavailable", "paid_pending_dispatch"]


def idempotency_insert(tenant_id: str, credential_id: str, **overrides: object) -> Executable:
    values = {
        "idempotency_record_id": new_id("idem"),
        "tenant_id": tenant_id,
        "credential_id": credential_id,
        "operation": "reservations.create",
        "idempotency_key": "shop:order:1001:reserve:1",
        "request_hash": "0" * 64,
        "response_status": 201,
        "response_body": {"data": {"reservation_id": "res_01"}},
    }
    return insert(IdempotencyRecord).values(values | overrides)


# 地点、池与销售入口（契约第 3 节）


def test_pool_cannot_sit_at_another_tenants_location(db: Connection):
    tenant = add_tenant(db)
    foreign_location = add_location(db, add_tenant(db))
    assert_rejected_by(
        db, pool_insert(tenant, foreign_location), "fk_stock_pools_tenant_id_locations"
    )


def test_a_pool_may_have_several_channels(db: Connection):
    # 入口组合由开通命令行检查（契约第 3 节，Q-81），数据库不再拒绝同一个池上的第二个入口。
    tenant = add_tenant(db)
    pool = add_pool(db, tenant)
    add_channel(db, tenant, pool)
    add_channel(db, tenant, pool)
    db.execute(channel_insert(tenant, pool, **platform_shop()))
    db.execute(channel_insert(tenant, pool, kind=BACKOFFICE))
    kinds = db.execute(select(SalesChannel.kind).where(SalesChannel.pool_id == pool)).scalars()
    assert sorted(kinds) == sorted([API, API, PLATFORM, BACKOFFICE])


# 入口的种类与平台店铺（契约第 3 节，0.22，Q-81）

KIND_CHECK = "ck_sales_channels_kind"
PAIR_CHECK = "ck_sales_channels_platform_pair"
PLATFORM_CHECK = "ck_sales_channels_platform_value"
SHOP_ID_CHECK = "ck_sales_channels_shop_id"
SHOP_UNIQUE = "uq_sales_channels_tenant_id_platform_platform_shop_id"


def platform_shop(platform: str = SHOPEE, shop_id: str | None = None) -> dict[str, object]:
    """一个 platform 入口的三列；店铺 ID 默认取一个新的。"""
    return {"kind": PLATFORM, "platform": platform, "platform_shop_id": shop_id or new_id("shop")}


def test_kind_defaults_to_api(db: Connection):
    tenant = add_tenant(db)
    channel = add_channel(db, tenant, add_pool(db, tenant))
    columns = (SalesChannel.kind, SalesChannel.platform, SalesChannel.platform_shop_id)
    row = db.execute(select(*columns).where(SalesChannel.channel_id == channel)).one()
    assert tuple(row) == (API, None, None)


def test_kind_must_be_an_exact_known_value(db: Connection):
    tenant = add_tenant(db)
    pool = add_pool(db, tenant)
    for kind in (API, BACKOFFICE):
        db.execute(channel_insert(tenant, pool, kind=kind))
    db.execute(channel_insert(tenant, pool, **platform_shop()))
    for kind in ["API", "Platform", "back_office", "web", ""]:
        assert_rejected_by(db, channel_insert(tenant, pool, kind=kind), KIND_CHECK)


def test_a_platform_channel_needs_both_platform_columns(db: Connection):
    tenant = add_tenant(db)
    pool = add_pool(db, tenant)
    for missing in ("platform", "platform_shop_id"):
        rejected = channel_insert(tenant, pool, **(platform_shop() | {missing: None}))
        assert_rejected_by(db, rejected, PAIR_CHECK)
    bare = channel_insert(tenant, pool, kind=PLATFORM)
    assert_rejected_by(db, bare, PAIR_CHECK)


def test_api_and_backoffice_channels_have_no_platform_columns(db: Connection):
    tenant = add_tenant(db)
    pool = add_pool(db, tenant)
    for kind in (API, BACKOFFICE):
        for present in ({"platform": SHOPEE}, {"platform_shop_id": "shop-1"}):
            rejected = channel_insert(tenant, pool, kind=kind, **present)
            assert_rejected_by(db, rejected, PAIR_CHECK)
        both = channel_insert(tenant, pool, **(platform_shop() | {"kind": kind}))
        assert_rejected_by(db, both, PAIR_CHECK)


def test_platform_must_be_an_exact_known_value(db: Connection):
    tenant = add_tenant(db)
    pool = add_pool(db, tenant)
    for platform in PLATFORMS:
        db.execute(channel_insert(tenant, pool, **platform_shop(platform)))
    for platform in ["Shopee", "SHOPEE", "tiktok", "tiktok-shop", "lazada", ""]:
        rejected = channel_insert(tenant, pool, **platform_shop(platform))
        assert_rejected_by(db, rejected, PLATFORM_CHECK)


def test_platform_shop_id_uses_the_allowed_characters(db: Connection):
    tenant = add_tenant(db)
    pool = add_pool(db, tenant)
    for shop_id in ["1", "a" * 64, "Shop.01_my:main-2"]:
        db.execute(channel_insert(tenant, pool, **platform_shop(shop_id=shop_id)))
    for shop_id in ["shop 1", "shop/1", "shop\n", "shöp", "shop@1"]:
        rejected = channel_insert(tenant, pool, **platform_shop(shop_id=shop_id))
        assert_rejected_by(db, rejected, SHOP_ID_CHECK)
    # 空串不是 NULL：pair 那条放过，由店铺 ID 那条拒绝。
    rejected = channel_insert(tenant, pool, **(platform_shop() | {"platform_shop_id": ""}))
    assert_rejected_by(db, rejected, SHOP_ID_CHECK)


def test_a_platform_shop_is_registered_once_per_tenant(db: Connection):
    tenant = add_tenant(db)
    shop = platform_shop(SHOPEE, "shop-1")
    db.execute(channel_insert(tenant, add_pool(db, tenant), **shop))
    # 同一个池或另一个池都不行。
    for pool in (add_pool(db, tenant), add_pool(db, tenant)):
        assert_rejected_by(db, channel_insert(tenant, pool, **shop), SHOP_UNIQUE)
    # 别的平台的同号店铺、别的租户的同一店铺照样可以。
    tiktok = platform_shop(TIKTOK_SHOP, "shop-1")
    db.execute(channel_insert(tenant, add_pool(db, tenant), **tiktok))
    other = add_tenant(db)
    db.execute(channel_insert(other, add_pool(db, other), **shop))


def test_channel_cannot_sell_another_tenants_pool(db: Connection):
    tenant = add_tenant(db)
    foreign_pool = add_pool(db, add_tenant(db))
    assert_rejected_by(
        db, channel_insert(tenant, foreign_pool), "fk_sales_channels_tenant_id_stock_pools"
    )


def test_pool_status_must_be_an_exact_known_value(db: Connection):
    tenant = add_tenant(db)
    location = add_location(db, tenant)
    db.execute(pool_insert(tenant, location, status="initializing"))
    # live 的池必有上线记录（契约 6.3）。
    live = went_live(add_credential(db, tenant))
    db.execute(pool_insert(tenant, location, status="live", **live))
    for status in ["LIVE", "Live", "Initializing", "active", "closed"]:
        rejected = pool_insert(tenant, location, status=status)
        assert_rejected_by(db, rejected, "ck_stock_pools_status")


def test_location_and_channel_status_must_be_an_exact_known_value(db: Connection):
    tenant = add_tenant(db)
    location = insert(Location).values(
        location_id=new_id("loc"), tenant_id=tenant, name="Warehouse", status="ACTIVE"
    )
    assert_rejected_by(db, location, "ck_locations_status")
    channel = channel_insert(tenant, add_pool(db, tenant), status="live")
    assert_rejected_by(db, channel, "ck_sales_channels_status")


# 余额（契约 4.1）


def test_one_balance_row_per_tenant_pool_and_sku(db: Connection):
    tenant, pool, sku = add_balance(db)
    assert_rejected_by(
        db, balance_insert(tenant, pool, sku), "uq_stock_balances_tenant_id_pool_id_sku_id"
    )
    # 同一 SKU 在另一个池是另一行。
    db.execute(balance_insert(tenant, add_pool(db, tenant), sku))


def test_new_balance_starts_at_zero(db: Connection):
    tenant, pool, sku = add_balance(db)
    row = db.execute(
        select(
            StockBalance.stock_balance,
            StockBalance.reserved,
            StockBalance.unavailable,
            StockBalance.paid_pending_dispatch,
            StockBalance.balance_version,
        ).where(
            StockBalance.tenant_id == tenant,
            StockBalance.pool_id == pool,
            StockBalance.sku_id == sku,
        )
    ).one()
    assert tuple(row) == (0, 0, 0, 0, 0)


def test_balance_quantities_are_non_negative(db: Connection):
    tenant = add_tenant(db)
    pool = add_pool(db, tenant)
    for column in NON_NEGATIVE_ALONE:
        negative = balance_insert(tenant, pool, add_sku(db, tenant), **{column: -1})
        assert_rejected_by(db, negative, f"ck_stock_balances_{column}_nonnegative")
    assert_rejected_by_one_of(
        db,
        balance_insert(tenant, pool, add_sku(db, tenant), stock_balance=-1),
        "ck_stock_balances_stock_balance_nonnegative",
        "ck_stock_balances_reserved_unavailable_within_balance",
    )
    negative_version = balance_insert(tenant, pool, add_sku(db, tenant), balance_version=-1)
    assert_rejected_by(db, negative_version, "ck_stock_balances_balance_version_nonnegative")


def test_reserved_plus_unavailable_cannot_exceed_stock_balance(db: Connection):
    tenant = add_tenant(db)
    pool = add_pool(db, tenant)
    quantities = {"stock_balance": 5, "reserved": 3}
    # 恰好相等是 available = 0，允许。
    db.execute(balance_insert(tenant, pool, add_sku(db, tenant), unavailable=2, **quantities))
    over = balance_insert(tenant, pool, add_sku(db, tenant), unavailable=3, **quantities)
    assert_rejected_by(db, over, "ck_stock_balances_reserved_unavailable_within_balance")


def test_balance_cannot_point_to_another_tenants_sku_or_pool(db: Connection):
    tenant = add_tenant(db)
    other_tenant = add_tenant(db)
    pool, sku = add_pool(db, tenant), add_sku(db, tenant)
    assert_rejected_by(
        db,
        balance_insert(tenant, pool, add_sku(db, other_tenant)),
        "fk_stock_balances_tenant_id_skus",
    )
    assert_rejected_by(
        db,
        balance_insert(tenant, add_pool(db, other_tenant), sku),
        "fk_stock_balances_tenant_id_stock_pools",
    )


# 流水（契约 4.3）


def test_entry_type_must_be_one_of_the_known_types(db: Connection):
    balance = add_balance(db)
    known = ["MERGE_TRANSFER_OUT", "MERGE_TRANSFER_IN", "DEMO_RESET", "CORRECTION"]
    original = add_entry(db, balance, 1)
    for version, entry_type in enumerate(known, start=2):
        corrects = original if entry_type == "CORRECTION" else None
        add_entry(db, balance, version, entry_type=entry_type, corrects_entry_id=corrects)
    for entry_type in ["receipt", "Receipt", "ADJUSTMENT", ""]:
        assert_rejected_by(
            db, entry_insert(balance, 99, entry_type=entry_type), "ck_ledger_entries_entry_type"
        )


def test_entry_after_values_obey_the_balance_invariants(db: Connection):
    balance = add_balance(db)
    for column in NON_NEGATIVE_ALONE:
        negative = entry_insert(balance, 1, **{f"{column}_after": -1})
        assert_rejected_by(db, negative, f"ck_ledger_entries_{column}_after_nonnegative")
    assert_rejected_by_one_of(
        db,
        entry_insert(balance, 1, stock_balance_after=-1),
        "ck_ledger_entries_stock_balance_after_nonnegative",
        "ck_ledger_entries_reserved_unavailable_after_within_balance",
    )
    over = entry_insert(balance, 1, stock_balance_after=5, reserved_after=4, unavailable_after=2)
    assert_rejected_by(db, over, "ck_ledger_entries_reserved_unavailable_after_within_balance")
    assert_rejected_by(
        db, entry_insert(balance, 0), "ck_ledger_entries_balance_version_after_positive"
    )


def test_deltas_may_be_negative(db: Connection):
    balance = add_balance(db)
    add_entry(db, balance, 1)
    # 晚到支付确认 1 件：5/0/0/0 → 4/0/0/1。
    late_sale = {
        "entry_type": "LATE_SALE_COMMIT",
        "stock_balance_delta": -1,
        "paid_pending_dispatch_delta": 1,
        "stock_balance_after": 4,
        "paid_pending_dispatch_after": 1,
    }
    add_entry(db, balance, 2, **late_sale)


def test_correction_must_reference_another_entry_and_only_correction_may(db: Connection):
    balance = add_balance(db)
    original = add_entry(db, balance, 1)
    reversal = {"entry_type": "CORRECTION", "stock_balance_delta": -5, "stock_balance_after": 0}
    assert_rejected_by(
        db, entry_insert(balance, 2, **reversal), "ck_ledger_entries_correction_target"
    )
    assert_rejected_by(
        db,
        entry_insert(balance, 2, corrects_entry_id=original),
        "ck_ledger_entries_correction_target",
    )
    self_id = new_id("led")
    assert_rejected_by(
        db,
        entry_insert(balance, 2, entry_id=self_id, corrects_entry_id=self_id, **reversal),
        "ck_ledger_entries_correction_not_self",
    )
    add_entry(db, balance, 2, corrects_entry_id=original, **reversal)


def test_correction_cannot_reference_another_tenants_entry(db: Connection):
    foreign_entry = add_entry(db, add_balance(db), 1)
    balance = add_balance(db)
    correction = entry_insert(balance, 1, entry_type="CORRECTION", corrects_entry_id=foreign_entry)
    assert_rejected_by(db, correction, "fk_ledger_entries_tenant_id_ledger_entries")


def test_two_entries_of_one_balance_row_cannot_share_a_version(db: Connection):
    balance = add_balance(db)
    command = new_id("cmd")
    # 同一命令对同一行写多条流水时版本逐条加。
    add_entry(db, balance, 1, command_id=command)
    add_entry(db, balance, 2, command_id=command)
    assert_rejected_by(
        db,
        entry_insert(balance, 2),
        "uq_ledger_entries_tenant_id_pool_id_sku_id_balance_version_after",
    )
    # 另一个余额行有自己的版本序列。
    tenant, pool, _ = balance
    other_sku = add_sku(db, tenant)
    db.execute(balance_insert(tenant, pool, other_sku))
    add_entry(db, (tenant, pool, other_sku), 1, command_id=command)


def test_entry_must_belong_to_an_existing_balance_row_of_its_tenant(db: Connection):
    tenant, pool, _ = add_balance(db)
    sku_without_balance = add_sku(db, tenant)
    assert_rejected_by(
        db,
        entry_insert((tenant, pool, sku_without_balance), 1),
        "fk_ledger_entries_tenant_id_stock_balances",
    )


def test_entry_channel_must_belong_to_the_same_tenant(db: Connection):
    balance = add_balance(db)
    other_tenant = add_tenant(db)
    foreign_channel = add_channel(db, other_tenant, add_pool(db, other_tenant))
    assert_rejected_by(
        db,
        entry_insert(balance, 1, channel_id=foreign_channel),
        "fk_ledger_entries_tenant_id_sales_channels",
    )
    tenant, pool, _ = balance
    add_entry(db, balance, 1, channel_id=add_channel(db, tenant, pool))


def test_entry_credential_is_optional_but_must_belong_to_the_same_tenant(db: Connection):
    balance = add_balance(db)
    for credential in [new_id("cred"), add_credential(db, add_tenant(db))]:
        assert_rejected_by(
            db,
            entry_insert(balance, 1, credential_id=credential),
            "fk_ledger_entries_tenant_id_credentials",
        )
    tenant, _, _ = balance
    add_entry(db, balance, 1, credential_id=add_credential(db, tenant))
    # Core 自己的到期关闭没有凭据。
    add_entry(db, balance, 2, credential_id=None)


def test_source_type_and_source_ref_come_together(db: Connection):
    balance = add_balance(db)
    add_entry(db, balance, 1, source_type="receipt", source_ref="rcv-2026-11-12-01")
    assert_rejected_by(
        db, entry_insert(balance, 2, source_type="receipt"), "ck_ledger_entries_source_pair"
    )
    assert_rejected_by(
        db,
        entry_insert(balance, 2, source_ref="rcv-2026-11-12-02"),
        "ck_ledger_entries_source_pair",
    )


# 幂等记录（契约 7.1）

# 约束名超过 64 字符，建表时截成前 56 个字符再加 "_" 与 4 位摘要；这里比对截断后不变的前缀。
IDEMPOTENCY_SCOPE_UNIQUE = "uq_idempotency_records_tenant_id_credential_id_operation_"


def test_idempotency_key_is_unique_per_tenant_credential_and_operation(db: Connection):
    tenant = add_tenant(db)
    credential = add_credential(db, tenant)
    db.execute(idempotency_insert(tenant, credential))
    assert_rejected_by(db, idempotency_insert(tenant, credential), IDEMPOTENCY_SCOPE_UNIQUE)
    db.execute(idempotency_insert(tenant, credential, operation="sale_commits.create"))
    db.execute(idempotency_insert(tenant, add_credential(db, tenant)))
    other_tenant = add_tenant(db)
    db.execute(idempotency_insert(other_tenant, add_credential(db, other_tenant)))


def test_only_settled_responses_are_stored(db: Connection):
    tenant = add_tenant(db)
    credential = add_credential(db, tenant)
    for status in [200, 409, 422, 499]:
        settled = idempotency_insert(
            tenant, credential, idempotency_key=f"k{status}", response_status=status
        )
        db.execute(settled)
    for status in [500, 503, 429, 199]:
        rejected = idempotency_insert(
            tenant, credential, idempotency_key=f"k{status}", response_status=status
        )
        assert_rejected_by(db, rejected, "ck_idempotency_records_response_status")


def test_idempotency_record_credential_must_exist_in_the_same_tenant(db: Connection):
    tenant = add_tenant(db)
    other_tenant = add_tenant(db)
    for credential in [new_id("cred"), add_credential(db, other_tenant)]:
        assert_rejected_by(
            db,
            idempotency_insert(tenant, credential),
            "fk_idempotency_records_tenant_id_credentials",
        )


# 0013 的升降（production 库）：照 test_demo_reset_schema 的先例，不动共享的 demo 库；
# 无论成败都在 finally 里升回 head，提交进去的行也在 finally 里删掉。

POOL_UNIQUE = "uq_sales_channels_tenant_id_pool_id"


def has_channel_kind(engine: Engine) -> bool:
    return "kind" in {column["name"] for column in inspect(engine).get_columns("sales_channels")}


def test_downgrade_to_0012_and_back_restores_one_channel_per_pool(production_engine: Engine):
    try:
        migrate_production(command.downgrade, "0012", PRODUCTION)
        assert alembic_version(production_engine) == "0012"
        assert not has_channel_kind(production_engine)
        # 0002 的唯一约束回来了：一个池只有一个入口。
        with rolled_back(production_engine) as conn:
            tenant = add_tenant(conn)
            pool = add_pool(conn, tenant)
            add_channel(conn, tenant, pool)
            assert_rejected_by(conn, channel_insert(tenant, pool), POOL_UNIQUE)

        migrate_production(command.upgrade, "head", PRODUCTION)
        assert alembic_version(production_engine) == head_revision()
        assert has_channel_kind(production_engine)
        # 升回之后同一个池可以有多个入口，新约束也回来了；写入的行回滚。
        with rolled_back(production_engine) as conn:
            tenant = add_tenant(conn)
            pool = add_pool(conn, tenant)
            conn.execute(channel_insert(tenant, pool, **platform_shop()))
            conn.execute(channel_insert(tenant, pool, kind=BACKOFFICE))
            assert_rejected_by(conn, channel_insert(tenant, pool, kind=PLATFORM), PAIR_CHECK)
    finally:
        migrate_production(command.upgrade, "head", PRODUCTION)
    assert alembic_version(production_engine) == head_revision()


def test_downgrade_fails_without_changes_while_a_pool_has_several_channels(
    production_engine: Engine,
):
    with production_engine.begin() as conn:
        tenant = add_tenant(conn)
        location = add_location(conn, tenant)
        pool = new_id("pool")
        conn.execute(pool_insert(tenant, location, pool_id=pool))
        channels = [new_id("chn"), new_id("chn")]
        conn.execute(channel_insert(tenant, pool, channel_id=channels[0], **platform_shop()))
        conn.execute(channel_insert(tenant, pool, channel_id=channels[1], kind=BACKOFFICE))
    try:
        with pytest.raises(RuntimeError):
            migrate_production(command.downgrade, "0012", PRODUCTION)
        assert alembic_version(production_engine) == head_revision()
        assert has_channel_kind(production_engine)
        with production_engine.connect() as conn:
            kinds = conn.execute(select(SalesChannel.kind).where(SalesChannel.pool_id == pool))
            assert sorted(kinds.scalars()) == sorted([PLATFORM, BACKOFFICE])
    finally:
        # 这些行没有别的行引用。
        with production_engine.begin() as conn:
            conn.execute(delete(SalesChannel).where(SalesChannel.channel_id.in_(channels)))
            conn.execute(delete(StockPool).where(StockPool.pool_id == pool))
            conn.execute(delete(Location).where(Location.location_id == location))
            conn.execute(delete(Tenant).where(Tenant.tenant_id == tenant))
        migrate_production(command.upgrade, "head", PRODUCTION)
