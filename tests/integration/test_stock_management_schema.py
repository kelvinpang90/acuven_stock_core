"""库存管理各表与判定列的约束在真 MySQL 上生效
（契约 4.1、6.2 第 7 条、6.3、6.6、6.7 与第 10 节第 1 条）。

这些规则由数据库兜底，服务层的 bug 也绕不过去。连接夹具 db 与 assert_rejected_by 见 conftest。
"""

from datetime import datetime
from typing import NamedTuple

from sqlalchemy import Connection, insert, select
from sqlalchemy.sql.expression import Executable

from app.models.inventory import (
    StockBalance,
    StockCount,
    StockCountLine,
    StockDocument,
    StockImport,
)
from tests.integration.conftest import add_credential, assert_rejected_by, new_id
from tests.integration.test_inventory_schema import (
    add_balance,
    add_channel,
    add_entry,
    add_location,
    add_pool,
    add_sku,
    add_tenant,
    assert_rejected_by_one_of,
    balance_insert,
    entry_insert,
    pool_insert,
    went_live,
)

EXPIRES_AT = datetime(2026, 11, 16, 9, 0)
CONFIRMED_AT = datetime(2026, 11, 15, 10, 0)
CANCELLED_AT = datetime(2026, 11, 15, 11, 0)
SUBMITTED_AT = datetime(2026, 11, 15, 9, 30)
REQUEST_HASH = "0" * 64
IMPORT_ROWS = [{"index": 0, "sku_id": "sku_1", "quantity": 5, "result": "ok"}]


class Shop(NamedTuple):
    """一个租户的池、它的销售入口与一个凭据。"""

    tenant: str
    pool: str
    channel: str
    credential: str


def add_shop(db: Connection) -> Shop:
    tenant = add_tenant(db)
    pool = add_pool(db, tenant)
    return Shop(tenant, pool, add_channel(db, tenant, pool), add_credential(db, tenant))


# 余额行的判定列（契约 4.1）


def test_decision_columns_default_to_zero_zero_and_null(db: Connection):
    tenant, pool, sku = add_balance(db)
    row = db.execute(
        select(
            StockBalance.last_book_only_version,
            StockBalance.last_reconciled_version,
            StockBalance.opening_entry_id,
        ).where(
            StockBalance.tenant_id == tenant,
            StockBalance.pool_id == pool,
            StockBalance.sku_id == sku,
        )
    ).one()
    assert tuple(row) == (0, 0, None)


def test_last_book_only_version_lies_between_zero_and_balance_version(db: Connection):
    tenant = add_tenant(db)
    pool = add_pool(db, tenant)
    # 恰好等于 balance_version 允许；opening_entry_id 不加外键，任意值都能存。
    db.execute(
        balance_insert(
            tenant,
            pool,
            add_sku(db, tenant),
            balance_version=3,
            last_book_only_version=3,
            opening_entry_id=new_id("led"),
        )
    )
    over = balance_insert(
        tenant, pool, add_sku(db, tenant), balance_version=2, last_book_only_version=3
    )
    assert_rejected_by(db, over, "ck_stock_balances_last_book_only_version_range")
    # 为负时 last_reconciled_version（不小于 0）也必然大于它，接受两条之一。
    assert_rejected_by_one_of(
        db,
        balance_insert(tenant, pool, add_sku(db, tenant), last_book_only_version=-1),
        "ck_stock_balances_last_book_only_version_range",
        "ck_stock_balances_last_reconciled_version_range",
    )


def test_last_reconciled_version_lies_between_zero_and_last_book_only_version(db: Connection):
    tenant = add_tenant(db)
    pool = add_pool(db, tenant)
    versions = {"balance_version": 3, "last_book_only_version": 2}
    db.execute(
        balance_insert(tenant, pool, add_sku(db, tenant), last_reconciled_version=2, **versions)
    )
    for version in [-1, 3]:
        rejected = balance_insert(
            tenant, pool, add_sku(db, tenant), last_reconciled_version=version, **versions
        )
        assert_rejected_by(db, rejected, "ck_stock_balances_last_reconciled_version_range")


# 池切 live（契约 6.3）


def test_live_pool_has_went_live_columns_and_initializing_pool_has_none(db: Connection):
    tenant = add_tenant(db)
    location = add_location(db, tenant)
    db.execute(pool_insert(tenant, location, status="live", **went_live(db, tenant)))
    assert_rejected_by(
        db, pool_insert(tenant, location, status="live"), "ck_stock_pools_went_live_status"
    )
    assert_rejected_by(
        db,
        pool_insert(tenant, location, status="initializing", **went_live(db, tenant)),
        "ck_stock_pools_went_live_status",
    )


def test_went_live_columns_are_all_set_or_all_empty(db: Connection):
    tenant = add_tenant(db)
    location = add_location(db, tenant)
    for column in went_live(db, tenant):
        partial = went_live(db, tenant) | {column: None}
        # 缺 went_live_at 时按 initializing 插，只让四列一致性这一条不满足。
        status = "initializing" if column == "went_live_at" else "live"
        assert_rejected_by(
            db,
            pool_insert(tenant, location, status=status, **partial),
            "ck_stock_pools_went_live_columns",
        )


def test_went_live_credential_must_belong_to_the_same_tenant(db: Connection):
    tenant = add_tenant(db)
    location = add_location(db, tenant)
    for credential in [new_id("cred"), add_credential(db, add_tenant(db))]:
        columns = went_live(db, tenant) | {"went_live_credential_id": credential}
        assert_rejected_by(
            db,
            pool_insert(tenant, location, status="live", **columns),
            "fk_stock_pools_tenant_id_credentials",
        )


# 流水：一条流水最多被更正一次（契约 6.7）


def test_an_entry_can_be_corrected_at_most_once(db: Connection):
    balance = add_balance(db)
    original = add_entry(db, balance, 1)
    reversal = {
        "entry_type": "CORRECTION",
        "corrects_entry_id": original,
        "stock_balance_delta": -5,
        "stock_balance_after": 0,
    }
    add_entry(db, balance, 2, **reversal)
    assert_rejected_by(
        db, entry_insert(balance, 3, **reversal), "uq_ledger_entries_tenant_id_corrects_entry_id"
    )
    # 另一条流水可以有自己的更正。
    other = add_entry(db, balance, 3)
    add_entry(db, balance, 4, **(reversal | {"corrects_entry_id": other}))


def test_entries_without_a_correction_target_are_not_limited(db: Connection):
    balance = add_balance(db)
    for version in range(1, 4):
        add_entry(db, balance, version, corrects_entry_id=None)


# 期初导入（契约 6.3）


def import_insert(shop: Shop, **overrides: object) -> Executable:
    values = {
        "import_id": new_id("simp"),
        "tenant_id": shop.tenant,
        "pool_id": shop.pool,
        "status": "previewed",
        "expires_at": EXPIRES_AT,
        "rows": IMPORT_ROWS,
        "credential_id": shop.credential,
        "actor_ref": "admin_1",
        "request_id": new_id("req"),
    }
    return insert(StockImport).values(values | overrides)


def import_confirmation(credential_id: str) -> dict[str, object]:
    """确认后才有的五列，status 一并改为 confirmed。"""
    return {
        "status": "confirmed",
        "result": {"rows": [{"index": 0, "result": "created", "entry_id": "led_01"}]},
        "confirmed_credential_id": credential_id,
        "confirmed_actor_ref": "admin_2",
        "confirmed_request_id": new_id("req"),
        "confirmed_at": CONFIRMED_AT,
    }


def test_stock_import_status_must_be_an_exact_known_value(db: Connection):
    shop = add_shop(db)
    db.execute(import_insert(shop))
    db.execute(import_insert(shop, **import_confirmation(shop.credential)))
    # expired 由 expires_at 推出，不存。
    for status in ["PREVIEWED", "Confirmed", "expired"]:
        assert_rejected_by(db, import_insert(shop, status=status), "ck_stock_imports_status")


def test_previewed_stock_import_has_no_confirmation_columns(db: Connection):
    shop = add_shop(db)
    for column, value in import_confirmation(shop.credential).items():
        if column == "status":
            continue
        assert_rejected_by(
            db, import_insert(shop, **{column: value}), "ck_stock_imports_confirmation"
        )


def test_confirmed_stock_import_has_all_confirmation_columns(db: Connection):
    shop = add_shop(db)
    for column in import_confirmation(shop.credential):
        if column == "status":
            continue
        incomplete = import_confirmation(shop.credential) | {column: None}
        assert_rejected_by(db, import_insert(shop, **incomplete), "ck_stock_imports_confirmation")


def test_stock_import_rows_must_be_an_array(db: Connection):
    shop = add_shop(db)
    db.execute(import_insert(shop, rows=[]))
    for rows in [{"rows": IMPORT_ROWS}, "rows", 1]:
        assert_rejected_by(db, import_insert(shop, rows=rows), "ck_stock_imports_rows")


def test_stock_import_pool_and_credentials_must_belong_to_the_same_tenant(db: Connection):
    shop = add_shop(db)
    other = add_shop(db)
    assert_rejected_by(
        db, import_insert(shop, pool_id=other.pool), "fk_stock_imports_tenant_id_stock_pools"
    )
    assert_rejected_by(
        db,
        import_insert(shop, credential_id=other.credential),
        "fk_stock_imports_tenant_id_credentials",
    )
    assert_rejected_by(
        db,
        import_insert(shop, **import_confirmation(other.credential)),
        "fk_stock_imports_confirmed_credential_id_credentials",
    )
    # 确认人可以用同租户的另一个凭据。
    db.execute(import_insert(shop, **import_confirmation(add_credential(db, shop.tenant))))


# 库存单据（契约 6.2 第 7 条）

# 约束名超过 64 字符，建表时截成前 56 个字符再加 "_" 与 4 位摘要；这里比对截断后不变的前缀。
DOCUMENT_REF_UNIQUE = "uq_stock_documents_tenant_id_channel_id_document_type_"


def document_insert(shop: Shop, **overrides: object) -> Executable:
    values = {
        "document_id": new_id("rcp"),
        "tenant_id": shop.tenant,
        "channel_id": shop.channel,
        "pool_id": shop.pool,
        "document_type": "receipt",
        "document_ref": "rcv-2026-11-15-01",
        "request_hash": REQUEST_HASH,
        "result": {"document_id": "rcp_01", "lines": []},
        "credential_id": shop.credential,
        "actor_ref": "clerk_1",
        "actor_role": "stock_operator",
        "request_id": new_id("req"),
    }
    return insert(StockDocument).values(values | overrides)


def test_document_type_must_be_an_exact_known_value(db: Connection):
    shop = add_shop(db)
    for index, document_type in enumerate(["receipt", "unavailable_adjustment", "correction"]):
        db.execute(document_insert(shop, document_type=document_type, document_ref=f"r{index}"))
    for document_type in ["Receipt", "RECEIPT", "count", ""]:
        assert_rejected_by(
            db,
            document_insert(shop, document_type=document_type),
            "ck_stock_documents_document_type",
        )


def test_document_ref_is_unique_per_tenant_channel_and_type(db: Connection):
    shop = add_shop(db)
    db.execute(document_insert(shop))
    assert_rejected_by(db, document_insert(shop), DOCUMENT_REF_UNIQUE)
    # 同一引用换一种单据类型、或在另一个销售入口，是另一个业务引用。
    db.execute(document_insert(shop, document_type="unavailable_adjustment"))
    other_pool = add_pool(db, shop.tenant)
    other_channel = add_channel(db, shop.tenant, other_pool)
    db.execute(document_insert(shop, channel_id=other_channel, pool_id=other_pool))


def test_document_result_must_be_an_object(db: Connection):
    shop = add_shop(db)
    db.execute(document_insert(shop, result={}))
    for index, result in enumerate([[], "rcp_01", 1]):
        assert_rejected_by(
            db,
            document_insert(shop, document_ref=f"r{index}", result=result),
            "ck_stock_documents_result",
        )


def test_document_channel_pool_and_credential_must_belong_to_the_same_tenant(db: Connection):
    shop = add_shop(db)
    other = add_shop(db)
    assert_rejected_by(
        db,
        document_insert(shop, channel_id=other.channel),
        "fk_stock_documents_tenant_id_sales_channels",
    )
    assert_rejected_by(
        db, document_insert(shop, pool_id=other.pool), "fk_stock_documents_tenant_id_stock_pools"
    )
    assert_rejected_by(
        db,
        document_insert(shop, credential_id=other.credential),
        "fk_stock_documents_tenant_id_credentials",
    )


# 盘点单（契约 6.6）


def count_insert(shop: Shop, **overrides: object) -> Executable:
    values = {
        "count_id": new_id("cnt"),
        "tenant_id": shop.tenant,
        "channel_id": shop.channel,
        "pool_id": shop.pool,
        "count_ref": new_id("count"),
        "status": "open",
        "note": "Weekly count",
        "request_hash": REQUEST_HASH,
        "credential_id": shop.credential,
        "actor_ref": "clerk_1",
        "request_id": new_id("req"),
    }
    return insert(StockCount).values(values | overrides)


def add_count(db: Connection, shop: Shop, **overrides: object) -> str:
    count_id = new_id("cnt")
    db.execute(count_insert(shop, count_id=count_id, **overrides))
    return count_id


def count_confirmation(credential_id: str) -> dict[str, object]:
    """确认后必有的五列；confirm_note 可空，不在其中。"""
    return {
        "confirmed_credential_id": credential_id,
        "confirmed_actor_ref": "admin_1",
        "confirmed_request_id": new_id("req"),
        "confirmed_at": CONFIRMED_AT,
        "confirmed_reason_code": "cycle_count",
    }


def count_cancellation(credential_id: str) -> dict[str, object]:
    return {
        "cancelled_credential_id": credential_id,
        "cancelled_actor_ref": "clerk_1",
        "cancelled_request_id": new_id("req"),
        "cancelled_at": CANCELLED_AT,
    }


def test_count_status_must_be_an_exact_known_value(db: Connection):
    shop = add_shop(db)
    add_count(db, shop)
    add_count(db, shop, status="confirmed", **count_confirmation(shop.credential))
    add_count(db, shop, status="cancelled", **count_cancellation(shop.credential))
    for status in ["OPEN", "Confirmed", "counted", "expired"]:
        assert_rejected_by(db, count_insert(shop, status=status), "ck_stock_counts_status")


def test_open_count_has_no_confirmation_or_cancellation_columns(db: Connection):
    shop = add_shop(db)
    confirmation = count_confirmation(shop.credential) | {"confirm_note": "ok"}
    for column, value in confirmation.items():
        rejected = count_insert(shop, **{column: value})
        assert_rejected_by(db, rejected, "ck_stock_counts_confirmation")
    for column, value in count_cancellation(shop.credential).items():
        rejected = count_insert(shop, **{column: value})
        assert_rejected_by(db, rejected, "ck_stock_counts_cancellation")


def test_confirmed_count_has_all_confirmation_columns_and_no_cancellation_columns(db: Connection):
    shop = add_shop(db)
    confirmed = {"status": "confirmed"} | count_confirmation(shop.credential)
    # confirm_note 可有可无。
    add_count(db, shop, **confirmed)
    add_count(db, shop, confirm_note="Two found in the back room", **confirmed)
    for column in count_confirmation(shop.credential):
        incomplete = confirmed | {column: None}
        assert_rejected_by(db, count_insert(shop, **incomplete), "ck_stock_counts_confirmation")
    for column, value in count_cancellation(shop.credential).items():
        assert_rejected_by(
            db, count_insert(shop, **confirmed, **{column: value}), "ck_stock_counts_cancellation"
        )


def test_cancelled_count_has_all_cancellation_columns_and_no_confirmation_columns(
    db: Connection,
):
    shop = add_shop(db)
    cancelled = {"status": "cancelled"} | count_cancellation(shop.credential)
    add_count(db, shop, **cancelled)
    for column in count_cancellation(shop.credential):
        incomplete = cancelled | {column: None}
        assert_rejected_by(db, count_insert(shop, **incomplete), "ck_stock_counts_cancellation")
    confirmation = count_confirmation(shop.credential) | {"confirm_note": "ok"}
    for column, value in confirmation.items():
        assert_rejected_by(
            db, count_insert(shop, **cancelled, **{column: value}), "ck_stock_counts_confirmation"
        )


def test_count_ref_is_unique_per_tenant_and_channel(db: Connection):
    shop = add_shop(db)
    add_count(db, shop, count_ref="count-2026-11")
    assert_rejected_by(
        db,
        count_insert(shop, count_ref="count-2026-11"),
        "uq_stock_counts_tenant_id_channel_id_count_ref",
    )
    # 另一个销售入口可以用同一个引用。
    other_pool = add_pool(db, shop.tenant)
    other_channel = add_channel(db, shop.tenant, other_pool)
    add_count(db, shop, count_ref="count-2026-11", channel_id=other_channel, pool_id=other_pool)


def test_count_channel_pool_and_credentials_must_belong_to_the_same_tenant(db: Connection):
    shop = add_shop(db)
    other = add_shop(db)
    assert_rejected_by(
        db,
        count_insert(shop, channel_id=other.channel),
        "fk_stock_counts_tenant_id_sales_channels",
    )
    assert_rejected_by(
        db, count_insert(shop, pool_id=other.pool), "fk_stock_counts_tenant_id_stock_pools"
    )
    assert_rejected_by(
        db,
        count_insert(shop, credential_id=other.credential),
        "fk_stock_counts_tenant_id_credentials",
    )
    assert_rejected_by(
        db,
        count_insert(shop, status="confirmed", **count_confirmation(other.credential)),
        "fk_stock_counts_confirmed_credential_id_credentials",
    )
    assert_rejected_by(
        db,
        count_insert(shop, status="cancelled", **count_cancellation(other.credential)),
        "fk_stock_counts_cancelled_credential_id_credentials",
    )


# 盘点行（契约 6.6；差额公式见契约第 6 节）

# 约束名超过 64 字符，建表时截成前 56 个字符再加 "_" 与 4 位摘要；这里比对截断后不变的前缀。
SNAPSHOT_WITHIN_BALANCE = "ck_stock_count_lines_snapshot_reserved_unavailable_"


def add_counted_sku(db: Connection, shop: Shop) -> str:
    """提交前预建的余额行。"""
    sku = add_sku(db, shop.tenant)
    db.execute(balance_insert(shop.tenant, shop.pool, sku))
    return sku


def line_insert(shop: Shop, count_id: str, sku_id: str, **overrides: object) -> Executable:
    """默认快照 10/2/1/1、版本 4，点到在架 10、不可售 1，两个差额都为 0。

    没有显式给出 d_shelf、d_u 时按快照与实数算出，只让被测的那条约束不满足。
    """
    values = {
        "count_id": count_id,
        "sku_id": sku_id,
        "tenant_id": shop.tenant,
        "pool_id": shop.pool,
        "shelf_count": 10,
        "unsellable_count": 1,
        "snapshot_stock_balance": 10,
        "snapshot_reserved": 2,
        "snapshot_unavailable": 1,
        "snapshot_paid_pending_dispatch": 1,
        "snapshot_balance_version": 4,
        "actor_ref": "clerk_1",
        "request_id": new_id("req"),
        "submitted_at": SUBMITTED_AT,
    } | overrides
    on_shelf = (
        values["snapshot_stock_balance"]
        - values["snapshot_unavailable"]
        + values["snapshot_paid_pending_dispatch"]
    )
    differences = {
        "d_shelf": values["shelf_count"] - on_shelf,
        "d_u": values["unsellable_count"] - values["snapshot_unavailable"],
    }
    return insert(StockCountLine).values(differences | values)


def test_counted_quantities_lie_between_zero_and_one_million(db: Connection):
    shop = add_shop(db)
    count = add_count(db, shop)
    for column in ["shelf_count", "unsellable_count"]:
        for value in [0, 1_000_000]:
            db.execute(line_insert(shop, count, add_counted_sku(db, shop), **{column: value}))
        for value in [-1, 1_000_001]:
            assert_rejected_by(
                db,
                line_insert(shop, count, add_counted_sku(db, shop), **{column: value}),
                f"ck_stock_count_lines_{column}_range",
            )


def test_snapshot_obeys_the_balance_invariants(db: Connection):
    shop = add_shop(db)
    count = add_count(db, shop)
    for column in ["reserved", "unavailable", "paid_pending_dispatch", "balance_version"]:
        assert_rejected_by(
            db,
            line_insert(shop, count, add_counted_sku(db, shop), **{f"snapshot_{column}": -1}),
            f"ck_stock_count_lines_snapshot_{column}_nonnegative",
        )
    assert_rejected_by_one_of(
        db,
        line_insert(shop, count, add_counted_sku(db, shop), snapshot_stock_balance=-1),
        "ck_stock_count_lines_snapshot_stock_balance_nonnegative",
        SNAPSHOT_WITHIN_BALANCE,
    )
    over = line_insert(
        shop, count, add_counted_sku(db, shop), snapshot_reserved=6, snapshot_unavailable=5
    )
    assert_rejected_by(db, over, SNAPSHOT_WITHIN_BALANCE)
    # 恰好相等是 available = 0，允许。
    db.execute(
        line_insert(
            shop, count, add_counted_sku(db, shop), snapshot_reserved=6, snapshot_unavailable=4
        )
    )


def test_differences_follow_the_count_formula(db: Connection):
    shop = add_shop(db)
    count = add_count(db, shop)
    # 快照 10/2/1/1：在架应有 10 − 1 + 1 = 10 件；点到在架 7、不可售 3。
    db.execute(
        line_insert(
            shop,
            count,
            add_counted_sku(db, shop),
            shelf_count=7,
            unsellable_count=3,
            d_shelf=-3,
            d_u=2,
        )
    )
    for d_shelf in [-2, 3, 0]:
        assert_rejected_by(
            db,
            line_insert(shop, count, add_counted_sku(db, shop), shelf_count=7, d_shelf=d_shelf),
            "ck_stock_count_lines_d_shelf",
        )
    for d_u in [-2, 1, 3]:
        assert_rejected_by(
            db,
            line_insert(shop, count, add_counted_sku(db, shop), unsellable_count=3, d_u=d_u),
            "ck_stock_count_lines_d_u",
        )


def test_one_line_per_count_and_sku(db: Connection):
    shop = add_shop(db)
    count = add_count(db, shop)
    sku = add_counted_sku(db, shop)
    db.execute(line_insert(shop, count, sku))
    assert_rejected_by(db, line_insert(shop, count, sku), "PRIMARY")
    # 同一 SKU 可以在另一张盘点单上。
    db.execute(line_insert(shop, add_count(db, shop), sku))


def test_line_count_balance_and_entry_must_belong_to_the_same_tenant(db: Connection):
    shop = add_shop(db)
    other = add_shop(db)
    count = add_count(db, shop)
    assert_rejected_by(
        db,
        line_insert(shop, add_count(db, other), add_counted_sku(db, shop)),
        "fk_stock_count_lines_tenant_id_stock_counts",
    )
    for pool, sku in [
        (other.pool, add_counted_sku(db, other)),
        (shop.pool, add_counted_sku(db, other)),
        # 本租户的 SKU，但余额行还没建。
        (shop.pool, add_sku(db, shop.tenant)),
    ]:
        assert_rejected_by(
            db,
            line_insert(shop, count, sku, pool_id=pool),
            "fk_stock_count_lines_tenant_id_stock_balances",
        )
    other_sku = add_counted_sku(db, other)
    foreign_entry = add_entry(db, (other.tenant, other.pool, other_sku), 1)
    assert_rejected_by(
        db,
        line_insert(shop, count, add_counted_sku(db, shop), entry_id=foreign_entry),
        "fk_stock_count_lines_tenant_id_ledger_entries",
    )
    sku = add_counted_sku(db, shop)
    entry = add_entry(db, (shop.tenant, shop.pool, sku), 1, entry_type="COUNT_ADJUSTMENT")
    db.execute(line_insert(shop, count, sku, entry_id=entry))
