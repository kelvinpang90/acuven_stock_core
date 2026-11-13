"""库存侧各表的约束在真 MySQL 上生效（契约第 3 节、4.1、4.3、7.1 与第 10 节第 1 条）。

这些规则由数据库兜底，服务层的 bug 也绕不过去。连接夹具 db 与 assert_rejected_by 见 conftest。
"""

from datetime import datetime

import pytest
from sqlalchemy import Connection, insert, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.sql.expression import Executable

from app.models.catalog import Product, Sku, Tenant
from app.models.inventory import (
    IdempotencyRecord,
    LedgerEntry,
    Location,
    SalesChannel,
    StockBalance,
    StockPool,
)
from tests.integration.conftest import add_credential, assert_rejected_by, new_id

# (tenant_id, pool_id, sku_id)：一个余额行，也是流水指向它的复合外键。
Balance = tuple[str, str, str]

OCCURRED_AT = datetime(2026, 11, 12, 8, 30)

# 只有 stock_balance 为负时，reserved + unavailable ≤ stock_balance 必然同时被违反；
# 不依赖 MySQL 先报哪一条，下面的测试对这种行接受两者之一。
NON_NEGATIVE_ALONE = ["reserved", "unavailable", "paid_pending_dispatch"]


def assert_rejected_by_one_of(db: Connection, statement: Executable, *constraints: str) -> None:
    with pytest.raises(DBAPIError) as caught, db.begin_nested():
        db.execute(statement)
    assert any(constraint in str(caught.value.orig) for constraint in constraints)


def add_tenant(db: Connection) -> str:
    tenant_id = new_id("ten")
    db.execute(insert(Tenant).values(tenant_id=tenant_id, name="Tenant", status="active"))
    return tenant_id


def add_sku(db: Connection, tenant_id: str) -> str:
    product_id, sku_id = new_id("prd"), new_id("sku")
    db.execute(
        insert(Product).values(
            product_id=product_id, tenant_id=tenant_id, name="Tee", status="active", version=1
        )
    )
    db.execute(
        insert(Sku).values(
            sku_id=sku_id,
            tenant_id=tenant_id,
            product_id=product_id,
            sku_code=new_id("code"),
            name="Tee",
            status="active",
            version=1,
        )
    )
    return sku_id


def add_location(db: Connection, tenant_id: str) -> str:
    location_id = new_id("loc")
    db.execute(
        insert(Location).values(
            location_id=location_id, tenant_id=tenant_id, name="Warehouse", status="active"
        )
    )
    return location_id


def pool_insert(tenant_id: str, location_id: str, **overrides: object) -> Executable:
    values = {
        "pool_id": new_id("pool"),
        "tenant_id": tenant_id,
        "location_id": location_id,
        "name": "Main",
        "status": "initializing",
    }
    return insert(StockPool).values(values | overrides)


def add_pool(db: Connection, tenant_id: str) -> str:
    pool_id = new_id("pool")
    db.execute(pool_insert(tenant_id, add_location(db, tenant_id), pool_id=pool_id))
    return pool_id


def channel_insert(tenant_id: str, pool_id: str, **overrides: object) -> Executable:
    values = {
        "channel_id": new_id("chn"),
        "tenant_id": tenant_id,
        "pool_id": pool_id,
        "name": "Shop",
        "status": "active",
    }
    return insert(SalesChannel).values(values | overrides)


def add_channel(db: Connection, tenant_id: str, pool_id: str) -> str:
    channel_id = new_id("chn")
    db.execute(channel_insert(tenant_id, pool_id, channel_id=channel_id))
    return channel_id


def balance_insert(tenant_id: str, pool_id: str, sku_id: str, **overrides: object) -> Executable:
    values = {
        "balance_id": new_id("bal"),
        "tenant_id": tenant_id,
        "pool_id": pool_id,
        "sku_id": sku_id,
    }
    return insert(StockBalance).values(values | overrides)


def add_balance(db: Connection) -> Balance:
    """建一个租户及其池、SKU 与余额行。"""
    tenant_id = add_tenant(db)
    pool_id, sku_id = add_pool(db, tenant_id), add_sku(db, tenant_id)
    db.execute(balance_insert(tenant_id, pool_id, sku_id))
    return tenant_id, pool_id, sku_id


def entry_insert(balance: Balance, version: int, **overrides: object) -> Executable:
    """默认是一条 RECEIPT：收货 5 件，余额行从 0/0/0/0 到 5/0/0/0。"""
    tenant_id, pool_id, sku_id = balance
    values = {
        "entry_id": new_id("led"),
        "tenant_id": tenant_id,
        "pool_id": pool_id,
        "sku_id": sku_id,
        "entry_type": "RECEIPT",
        "stock_balance_delta": 5,
        "reserved_delta": 0,
        "unavailable_delta": 0,
        "paid_pending_dispatch_delta": 0,
        "stock_balance_after": 5,
        "reserved_after": 0,
        "unavailable_after": 0,
        "paid_pending_dispatch_after": 0,
        "balance_version_after": version,
        "command_id": new_id("cmd"),
        "occurred_at": OCCURRED_AT,
    }
    return insert(LedgerEntry).values(values | overrides)


def add_entry(db: Connection, balance: Balance, version: int, **overrides: object) -> str:
    entry_id = new_id("led")
    db.execute(entry_insert(balance, version, entry_id=entry_id, **overrides))
    return entry_id


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


def test_a_pool_has_at_most_one_sales_channel(db: Connection):
    tenant = add_tenant(db)
    pool = add_pool(db, tenant)
    add_channel(db, tenant, pool)
    assert_rejected_by(db, channel_insert(tenant, pool), "uq_sales_channels_tenant_id_pool_id")
    # 另一个池可以有自己的入口。
    add_channel(db, tenant, add_pool(db, tenant))


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
    db.execute(pool_insert(tenant, location, status="live"))
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
