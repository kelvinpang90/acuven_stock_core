"""库存管理命令用到的表与列在真 MySQL 上生效
（契约 4.1、6.2 第 7 条、6.3、6.6、6.7 与第 10 节第 1 条）。

只验表结构与约束，写这些表的命令在各自的任务里。连接夹具 db 与各表的造数见 conftest。
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Connection, insert, select, update
from sqlalchemy.sql.expression import Executable

from app.models.inventory import (
    WENT_LIVE_COLUMNS,
    StockBalance,
    StockCount,
    StockCountLine,
    StockDocument,
    StockImport,
)
from tests.integration.conftest import (
    Balance,
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
    entry_insert,
    new_id,
    pool_insert,
    went_live,
)

EXPIRES_AT = datetime(2026, 11, 16, 9, 0)
CONFIRMED_AT = datetime(2026, 11, 15, 10, 0)
CANCELLED_AT = datetime(2026, 11, 15, 11, 0)
SUBMITTED_AT = datetime(2026, 11, 15, 9, 30)
REQUEST_HASH = "0" * 64

# 约束名超过 64 字符，建表时截成前 56 个字符再加 "_" 与 4 位摘要；这里比对截断后不变的前缀。
DOCUMENT_REF_UNIQUE = "uq_stock_documents_tenant_id_channel_id_document_type_document_ref"[:56]
SNAPSHOT_WITHIN_BALANCE = "ck_stock_count_lines_snapshot_reserved_unavailable_within_balance"[:56]


@dataclass
class Shop:
    """一个租户的池、绑定它的销售入口与该租户的一个凭据。"""

    tenant_id: str
    pool_id: str
    channel_id: str
    credential_id: str


def add_shop(db: Connection) -> Shop:
    tenant_id = add_tenant(db)
    pool_id = add_pool(db, tenant_id)
    channel_id = add_channel(db, tenant_id, pool_id)
    return Shop(tenant_id, pool_id, channel_id, add_credential(db, tenant_id))


# 余额行的判定列（契约 4.1）


def is_balance(balance: Balance) -> tuple:
    tenant, pool, sku = balance
    return (
        StockBalance.tenant_id == tenant,
        StockBalance.pool_id == pool,
        StockBalance.sku_id == sku,
    )


def test_balance_inserted_without_decision_columns_takes_zero_zero_and_null(db: Connection):
    balance = add_balance(db)
    row = db.execute(
        select(
            StockBalance.last_book_only_version,
            StockBalance.last_reconciled_version,
            StockBalance.opening_entry_id,
        ).where(*is_balance(balance))
    ).one()
    assert tuple(row) == (0, 0, None)


def test_opening_entry_id_has_no_foreign_key(db: Connection):
    # 反向外键会与流水指向余额行的外键成环（契约 4.1），一致性由服务层保证。
    balance = add_balance(db)
    unknown_entry = new_id("led")
    db.execute(
        update(StockBalance).where(*is_balance(balance)).values(opening_entry_id=unknown_entry)
    )


def test_last_book_only_version_is_between_zero_and_balance_version(db: Connection):
    tenant = add_tenant(db)
    pool = add_pool(db, tenant)
    # 恰好等于 balance_version 允许。
    db.execute(
        balance_insert(
            tenant, pool, add_sku(db, tenant), balance_version=3, last_book_only_version=3
        )
    )
    over = balance_insert(
        tenant, pool, add_sku(db, tenant), balance_version=3, last_book_only_version=4
    )
    assert_rejected_by(db, over, "ck_stock_balances_last_book_only_version_range")
    # 为负时默认的 last_reconciled_version（0）必然也超过它；不依赖 MySQL 先报哪一条。
    assert_rejected_by_one_of(
        db,
        balance_insert(tenant, pool, add_sku(db, tenant), last_book_only_version=-1),
        "ck_stock_balances_last_book_only_version_range",
        "ck_stock_balances_last_reconciled_version_range",
    )


def test_last_reconciled_version_is_between_zero_and_last_book_only_version(db: Connection):
    tenant = add_tenant(db)
    pool = add_pool(db, tenant)
    versions = {"balance_version": 5, "last_book_only_version": 4}
    db.execute(
        balance_insert(tenant, pool, add_sku(db, tenant), last_reconciled_version=4, **versions)
    )
    for reconciled in (-1, 5):
        rejected = balance_insert(
            tenant, pool, add_sku(db, tenant), last_reconciled_version=reconciled, **versions
        )
        assert_rejected_by(db, rejected, "ck_stock_balances_last_reconciled_version_range")


# 池切 live（契约 6.3）


def test_pool_is_live_exactly_when_it_records_going_live(db: Connection):
    tenant = add_tenant(db)
    location = add_location(db, tenant)
    credential = add_credential(db, tenant)
    db.execute(pool_insert(tenant, location))
    db.execute(pool_insert(tenant, location, status="live", **went_live(credential)))
    not_recorded = pool_insert(tenant, location, status="live")
    assert_rejected_by(db, not_recorded, "ck_stock_pools_live_status")
    assert_rejected_by(
        db,
        pool_insert(tenant, location, status="initializing", **went_live(credential)),
        "ck_stock_pools_live_status",
    )


def test_went_live_columns_are_all_set_or_all_empty(db: Connection):
    tenant = add_tenant(db)
    location = add_location(db, tenant)
    credential = add_credential(db, tenant)
    for column in WENT_LIVE_COLUMNS:
        # 缺 went_live_at 的池只能是 initializing，缺别的列的是 live，都只违反这一条。
        status = "initializing" if column == "went_live_at" else "live"
        partial = went_live(credential) | {column: None}
        assert_rejected_by(
            db,
            pool_insert(tenant, location, status=status, **partial),
            "ck_stock_pools_went_live_columns",
        )


def test_went_live_credential_must_belong_to_the_same_tenant(db: Connection):
    tenant = add_tenant(db)
    location = add_location(db, tenant)
    for credential in [new_id("cred"), add_credential(db, add_tenant(db))]:
        assert_rejected_by(
            db,
            pool_insert(tenant, location, status="live", **went_live(credential)),
            "fk_stock_pools_tenant_id_credentials",
        )


# 流水只被更正一次（契约 6.7）


def test_an_entry_is_corrected_at_most_once(db: Connection):
    balance = add_balance(db)
    original = add_entry(db, balance, 1)
    reversal = {"entry_type": "CORRECTION", "stock_balance_delta": -5, "stock_balance_after": 0}
    add_entry(db, balance, 2, corrects_entry_id=original, **reversal)
    assert_rejected_by(
        db,
        entry_insert(balance, 3, corrects_entry_id=original, **reversal),
        "uq_ledger_entries_tenant_id_corrects_entry_id",
    )
    # corrects_entry_id 为空的流水照常插入：MySQL 唯一索引允许多个 NULL。
    second = add_entry(db, balance, 3)
    add_entry(db, balance, 4, entry_type="MARK_UNAVAILABLE", stock_balance_delta=0)
    # 另一条流水可以有自己的一次更正。
    add_entry(db, balance, 5, corrects_entry_id=second, **reversal)


# 期初导入（契约 6.3）

IMPORT_ROWS = [{"index": 0, "row": '{"sku_id": "sku_1", "quantity": 5}', "result": "ok"}]


def import_insert(shop: Shop, **overrides: object) -> Executable:
    values = {
        "import_id": new_id("simp"),
        "tenant_id": shop.tenant_id,
        "pool_id": shop.pool_id,
        "status": "previewed",
        "expires_at": EXPIRES_AT,
        "rows": IMPORT_ROWS,
        "credential_id": shop.credential_id,
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


def test_import_status_must_be_previewed_or_confirmed(db: Connection):
    shop = add_shop(db)
    db.execute(import_insert(shop))
    db.execute(import_insert(shop, **import_confirmation(shop.credential_id)))
    # expired 由 expires_at 推出，不存（契约 6.3）。
    for status in ["PREVIEWED", "Confirmed", "expired"]:
        assert_rejected_by(db, import_insert(shop, status=status), "ck_stock_imports_status")


def test_previewed_import_has_no_confirmation_columns(db: Connection):
    shop = add_shop(db)
    for column, value in import_confirmation(shop.credential_id).items():
        if column == "status":
            continue
        assert_rejected_by(
            db, import_insert(shop, **{column: value}), "ck_stock_imports_confirmation"
        )


def test_confirmed_import_has_all_confirmation_columns(db: Connection):
    shop = add_shop(db)
    for column in import_confirmation(shop.credential_id):
        if column == "status":
            continue
        incomplete = import_confirmation(shop.credential_id) | {column: None}
        assert_rejected_by(db, import_insert(shop, **incomplete), "ck_stock_imports_confirmation")


def test_import_rows_must_be_an_array(db: Connection):
    shop = add_shop(db)
    db.execute(import_insert(shop, rows=[]))
    for rows in [{"rows": IMPORT_ROWS}, "rows", 1]:
        assert_rejected_by(db, import_insert(shop, rows=rows), "ck_stock_imports_rows")


def test_import_pool_and_credentials_must_belong_to_the_same_tenant(db: Connection):
    shop, other = add_shop(db), add_shop(db)
    assert_rejected_by(
        db, import_insert(shop, pool_id=other.pool_id), "fk_stock_imports_tenant_id_stock_pools"
    )
    assert_rejected_by(
        db,
        import_insert(shop, credential_id=other.credential_id),
        "fk_stock_imports_tenant_id_credentials",
    )
    assert_rejected_by(
        db,
        import_insert(shop, **import_confirmation(other.credential_id)),
        "fk_stock_imports_confirmed_credential_id_credentials",
    )
    # 确认人可以用同租户的另一个凭据。
    db.execute(import_insert(shop, **import_confirmation(add_credential(db, shop.tenant_id))))


# 库存单据（契约 6.2 第 7 条）


def document_insert(shop: Shop, **overrides: object) -> Executable:
    values = {
        "document_id": new_id("rcp"),
        "tenant_id": shop.tenant_id,
        "channel_id": shop.channel_id,
        "pool_id": shop.pool_id,
        "document_type": "receipt",
        "document_ref": "rcv-2026-11-15-01",
        "request_hash": REQUEST_HASH,
        "result": {"document_id": "rcp_01", "lines": []},
        "credential_id": shop.credential_id,
        "actor_ref": "operator_1",
        "actor_role": "stock_operator",
        "request_id": new_id("req"),
    }
    return insert(StockDocument).values(values | overrides)


def test_document_type_must_be_a_known_type(db: Connection):
    shop = add_shop(db)
    for document_type in ["receipt", "unavailable_adjustment", "correction"]:
        db.execute(document_insert(shop, document_type=document_type))
    for document_type in ["RECEIPT", "Receipt", "count", "adjustment", ""]:
        assert_rejected_by(
            db,
            document_insert(shop, document_type=document_type),
            "ck_stock_documents_document_type",
        )


def test_document_ref_is_unique_per_tenant_channel_and_type(db: Connection):
    shop = add_shop(db)
    db.execute(document_insert(shop))
    assert_rejected_by(db, document_insert(shop), DOCUMENT_REF_UNIQUE)
    # 同一个引用换一种单据类型是另一个业务引用。
    db.execute(document_insert(shop, document_type="unavailable_adjustment"))
    # 同一租户另一个销售入口有自己的引用空间。
    other_pool = add_pool(db, shop.tenant_id)
    other_channel = add_channel(db, shop.tenant_id, other_pool)
    db.execute(document_insert(shop, channel_id=other_channel, pool_id=other_pool))


def test_document_result_must_be_an_object(db: Connection):
    shop = add_shop(db)
    for result in [[], ["rcp_01"], "ok", 1]:
        assert_rejected_by(db, document_insert(shop, result=result), "ck_stock_documents_result")


def test_document_channel_pool_and_credential_must_belong_to_the_same_tenant(db: Connection):
    shop, other = add_shop(db), add_shop(db)
    cases = {
        "channel_id": (other.channel_id, "fk_stock_documents_tenant_id_sales_channels"),
        "pool_id": (other.pool_id, "fk_stock_documents_tenant_id_stock_pools"),
        "credential_id": (other.credential_id, "fk_stock_documents_tenant_id_credentials"),
    }
    for column, (foreign, constraint) in cases.items():
        assert_rejected_by(db, document_insert(shop, **{column: foreign}), constraint)


# 盘点单（契约第 6 节、6.6）


def count_insert(shop: Shop, **overrides: object) -> Executable:
    values = {
        "count_id": new_id("cnt"),
        "tenant_id": shop.tenant_id,
        "channel_id": shop.channel_id,
        "pool_id": shop.pool_id,
        "count_ref": new_id("count"),
        "status": "open",
        "request_hash": REQUEST_HASH,
        "credential_id": shop.credential_id,
        "actor_ref": "operator_1",
        "request_id": new_id("req"),
    }
    return insert(StockCount).values(values | overrides)


def add_count(db: Connection, shop: Shop, **overrides: object) -> str:
    count_id = new_id("cnt")
    db.execute(count_insert(shop, count_id=count_id, **overrides))
    return count_id


def confirmed_columns(credential_id: str) -> dict[str, object]:
    """确认后必有的五列；confirm_note 可选，不在其中。"""
    return {
        "confirmed_credential_id": credential_id,
        "confirmed_actor_ref": "admin_1",
        "confirmed_request_id": new_id("req"),
        "confirmed_at": CONFIRMED_AT,
        "confirmed_reason_code": "cycle_count",
    }


def cancelled_columns(credential_id: str) -> dict[str, object]:
    return {
        "cancelled_credential_id": credential_id,
        "cancelled_actor_ref": "admin_1",
        "cancelled_request_id": new_id("req"),
        "cancelled_at": CANCELLED_AT,
    }


CONFIRM_NOTE = {"confirm_note": "Recounted shelf B"}


def test_count_status_must_be_an_exact_known_value(db: Connection):
    shop = add_shop(db)
    credential = shop.credential_id
    db.execute(count_insert(shop))
    db.execute(count_insert(shop, status="confirmed", **confirmed_columns(credential)))
    db.execute(
        count_insert(shop, status="confirmed", **confirmed_columns(credential), **CONFIRM_NOTE)
    )
    db.execute(count_insert(shop, status="cancelled", **cancelled_columns(credential)))
    for status in ["OPEN", "Confirmed", "submitted", "closed"]:
        assert_rejected_by(db, count_insert(shop, status=status), "ck_stock_counts_status")


def test_open_count_has_no_confirmation_or_cancellation_columns(db: Connection):
    shop = add_shop(db)
    for column, value in (confirmed_columns(shop.credential_id) | CONFIRM_NOTE).items():
        assert_rejected_by(
            db, count_insert(shop, **{column: value}), "ck_stock_counts_confirmation"
        )
    for column, value in cancelled_columns(shop.credential_id).items():
        assert_rejected_by(
            db, count_insert(shop, **{column: value}), "ck_stock_counts_cancellation"
        )


def test_confirmed_count_has_all_confirmation_and_no_cancellation_columns(db: Connection):
    shop = add_shop(db)
    confirmed = confirmed_columns(shop.credential_id)
    for column in confirmed:
        incomplete = confirmed | {column: None}
        assert_rejected_by(
            db,
            count_insert(shop, status="confirmed", **incomplete),
            "ck_stock_counts_confirmation",
        )
    for column, value in cancelled_columns(shop.credential_id).items():
        assert_rejected_by(
            db,
            count_insert(shop, status="confirmed", **confirmed, **{column: value}),
            "ck_stock_counts_cancellation",
        )


def test_cancelled_count_has_all_cancellation_and_no_confirmation_columns(db: Connection):
    shop = add_shop(db)
    cancelled = cancelled_columns(shop.credential_id)
    for column in cancelled:
        incomplete = cancelled | {column: None}
        assert_rejected_by(
            db,
            count_insert(shop, status="cancelled", **incomplete),
            "ck_stock_counts_cancellation",
        )
    for column, value in (confirmed_columns(shop.credential_id) | CONFIRM_NOTE).items():
        assert_rejected_by(
            db,
            count_insert(shop, status="cancelled", **cancelled, **{column: value}),
            "ck_stock_counts_confirmation",
        )


def test_count_ref_is_unique_per_tenant_and_channel(db: Connection):
    shop = add_shop(db)
    db.execute(count_insert(shop, count_ref="count-2026-11-15"))
    # 取消后 count_ref 仍被占用（契约第 6 节），所以不看状态。
    assert_rejected_by(
        db,
        count_insert(
            shop,
            count_ref="count-2026-11-15",
            status="cancelled",
            **cancelled_columns(shop.credential_id),
        ),
        "uq_stock_counts_tenant_id_channel_id_count_ref",
    )
    other_pool = add_pool(db, shop.tenant_id)
    other_channel = add_channel(db, shop.tenant_id, other_pool)
    same_ref_elsewhere = {"channel_id": other_channel, "pool_id": other_pool}
    db.execute(count_insert(shop, count_ref="count-2026-11-15", **same_ref_elsewhere))


def test_count_channel_pool_and_credentials_must_belong_to_the_same_tenant(db: Connection):
    shop, other = add_shop(db), add_shop(db)
    foreign = other.credential_id
    cases = [
        ({"channel_id": other.channel_id}, "fk_stock_counts_tenant_id_sales_channels"),
        ({"pool_id": other.pool_id}, "fk_stock_counts_tenant_id_stock_pools"),
        ({"credential_id": foreign}, "fk_stock_counts_tenant_id_credentials"),
        (
            {"status": "confirmed", **confirmed_columns(foreign)},
            "fk_stock_counts_confirmed_credential_id_credentials",
        ),
        (
            {"status": "cancelled", **cancelled_columns(foreign)},
            "fk_stock_counts_cancelled_credential_id_credentials",
        ),
    ]
    for overrides, constraint in cases:
        assert_rejected_by(db, count_insert(shop, **overrides), constraint)


# 盘点行（契约第 6 节、6.6）

# 快照 20/3/2/1、版本 4：预期在架 = 20 − 2 + 1 = 19。
SNAPSHOT = {
    "snapshot_stock_balance": 20,
    "snapshot_reserved": 3,
    "snapshot_unavailable": 2,
    "snapshot_paid_pending_dispatch": 1,
    "snapshot_balance_version": 4,
}


def add_counted_sku(db: Connection, shop: Shop) -> str:
    """盘点行指向的余额行在提交时已预建（契约 4.1）。"""
    sku_id = add_sku(db, shop.tenant_id)
    db.execute(balance_insert(shop.tenant_id, shop.pool_id, sku_id))
    return sku_id


def line_insert(shop: Shop, count_id: str, sku_id: str, **overrides: object) -> Executable:
    """差额默认照契约第 6 节的公式由实数与快照算出；要造不符的差额时直接给 d_shelf、d_u。"""
    values = {
        "count_id": count_id,
        "sku_id": sku_id,
        "tenant_id": shop.tenant_id,
        "pool_id": shop.pool_id,
        "shelf_count": 18,
        "unsellable_count": 1,
        **SNAPSHOT,
        "actor_ref": "operator_1",
        "request_id": new_id("req"),
        "submitted_at": SUBMITTED_AT,
    } | overrides
    expected_shelf = (
        values["snapshot_stock_balance"]
        - values["snapshot_unavailable"]
        + values["snapshot_paid_pending_dispatch"]
    )
    values.setdefault("d_shelf", values["shelf_count"] - expected_shelf)
    values.setdefault("d_u", values["unsellable_count"] - values["snapshot_unavailable"])
    return insert(StockCountLine).values(values)


def test_line_differences_follow_the_count_formula(db: Connection):
    shop = add_shop(db)
    count = add_count(db, shop)
    # 在架 18、不可售 1：d_shelf = 18 − 19 = −1，d_u = 1 − 2 = −1。
    db.execute(line_insert(shop, count, add_counted_sku(db, shop), d_shelf=-1, d_u=-1))
    # 契约第 6 节的例子：快照 5、点到 3。
    example = {
        "snapshot_stock_balance": 5,
        "snapshot_reserved": 0,
        "snapshot_unavailable": 0,
        "snapshot_paid_pending_dispatch": 0,
    }
    sku = add_counted_sku(db, shop)
    db.execute(
        line_insert(shop, count, sku, shelf_count=3, unsellable_count=0, d_shelf=-2, **example)
    )
    sku = add_counted_sku(db, shop)
    for overrides, constraint in [
        ({"d_shelf": 0}, "ck_stock_count_lines_d_shelf"),
        ({"d_shelf": -2}, "ck_stock_count_lines_d_shelf"),
        ({"d_u": 0}, "ck_stock_count_lines_d_u"),
        ({"d_u": 3}, "ck_stock_count_lines_d_u"),
    ]:
        assert_rejected_by(db, line_insert(shop, count, sku, **overrides), constraint)


def test_line_counts_are_between_zero_and_one_million(db: Connection):
    shop = add_shop(db)
    count = add_count(db, shop)
    for value in (0, 1_000_000):
        sku = add_counted_sku(db, shop)
        db.execute(line_insert(shop, count, sku, shelf_count=value, unsellable_count=value))
    sku = add_counted_sku(db, shop)
    for column in ("shelf_count", "unsellable_count"):
        for value in (-1, 1_000_001):
            assert_rejected_by(
                db,
                line_insert(shop, count, sku, **{column: value}),
                f"ck_stock_count_lines_{column}",
            )


def test_line_snapshot_obeys_the_balance_invariants(db: Connection):
    shop = add_shop(db)
    count = add_count(db, shop)
    sku = add_counted_sku(db, shop)
    for column in ("reserved", "unavailable", "paid_pending_dispatch", "balance_version"):
        assert_rejected_by(
            db,
            line_insert(shop, count, sku, **{f"snapshot_{column}": -1}),
            f"ck_stock_count_lines_snapshot_{column}_nonnegative",
        )
    # stock_balance 为负时不变量那条必然同时被违反，接受两者之一。
    assert_rejected_by_one_of(
        db,
        line_insert(shop, count, sku, snapshot_stock_balance=-1),
        "ck_stock_count_lines_snapshot_stock_balance_nonnegative",
        SNAPSHOT_WITHIN_BALANCE,
    )
    over = line_insert(shop, count, sku, snapshot_reserved=10, snapshot_unavailable=11)
    assert_rejected_by(db, over, SNAPSHOT_WITHIN_BALANCE)
    # 恰好相等是 available = 0，允许。
    db.execute(line_insert(shop, count, sku, snapshot_reserved=10, snapshot_unavailable=10))


def test_line_count_balance_and_entry_must_belong_to_the_same_tenant(db: Connection):
    shop, other = add_shop(db), add_shop(db)
    count = add_count(db, shop)
    sku = add_counted_sku(db, shop)
    foreign_count = add_count(db, other)
    assert_rejected_by(
        db,
        line_insert(shop, foreign_count, sku),
        "fk_stock_count_lines_tenant_id_stock_counts",
    )
    foreign_sku = add_counted_sku(db, other)
    assert_rejected_by(
        db,
        line_insert(shop, count, foreign_sku, pool_id=other.pool_id),
        "fk_stock_count_lines_tenant_id_stock_balances",
    )
    # 本租户的 SKU 在这个池还没有余额行，同样被拒。
    assert_rejected_by(
        db,
        line_insert(shop, count, add_sku(db, shop.tenant_id)),
        "fk_stock_count_lines_tenant_id_stock_balances",
    )
    foreign_entry = add_entry(db, (other.tenant_id, other.pool_id, foreign_sku), 1)
    assert_rejected_by(
        db,
        line_insert(shop, count, sku, entry_id=foreign_entry),
        "fk_stock_count_lines_tenant_id_ledger_entries",
    )
    # 确认后记下本租户的 COUNT_ADJUSTMENT 流水；两个差额都为 0 时为空。
    entry = add_entry(db, (shop.tenant_id, shop.pool_id, sku), 1, entry_type="COUNT_ADJUSTMENT")
    db.execute(line_insert(shop, count, sku, entry_id=entry))
    db.execute(line_insert(shop, count, add_counted_sku(db, shop), entry_id=None))
