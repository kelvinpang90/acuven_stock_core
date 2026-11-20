"""SKU 合并单与快照两张表的约束在真 MySQL 上生效（契约 2.8 的存放）。

demo 库用 conftest 的 db 夹具（每个测试一个事务、结束时回滚）。
降到 0010 在 production 测试库上做（同 test_demo_reset_schema.py）：
无论成败都在 finally 里升回 head，不动共享的 demo 库。
"""

from dataclasses import dataclass

import pytest
from sqlalchemy import Connection, Engine, insert
from sqlalchemy.sql.expression import Executable

from alembic import command
from app.domain.environment import PRODUCTION
from app.domain.sku_merges import (
    CANCELLED,
    COMPLETED,
    COUNTED,
    MERGE_ID_PREFIX,
    RECONCILE_PENDING,
    SELLING_PAUSED,
    STATUSES,
)
from app.models.merges import (
    CANCELLATION_COLUMNS,
    COMPLETION_COLUMNS,
    COUNT_COLUMNS,
    EXECUTION_COLUMNS,
    SkuMerge,
    SkuMergeSnapshot,
)
from tests.integration.conftest import (
    OCCURRED_AT,
    add_credential,
    add_entry,
    add_pool,
    add_sku,
    add_tenant,
    assert_rejected_by,
    balance_insert,
    new_id,
)
from tests.integration.test_demo_reset_schema import (
    alembic_version,
    has_table,
    head_revision,
    migrate_production,
)

TABLES = ("sku_merges", "sku_merge_snapshots")
CHECK = "ck_sku_merges_"


@dataclass
class Pair:
    """一个租户的池、两个都有余额行的 SKU、一个凭据与三条流水（两条迁移、一条定数）。"""

    tenant_id: str
    pool_id: str
    source_sku_id: str
    canonical_sku_id: str
    credential_id: str
    transfer_out_entry_id: str
    transfer_in_entry_id: str
    adjustment_entry_id: str


def add_pair(db: Connection) -> Pair:
    tenant_id = add_tenant(db)
    pool_id = add_pool(db, tenant_id)
    source, canonical = add_sku(db, tenant_id), add_sku(db, tenant_id)
    for sku_id in (source, canonical):
        db.execute(balance_insert(tenant_id, pool_id, sku_id))
    transfer_out = add_entry(db, (tenant_id, pool_id, source), 1, entry_type="MERGE_TRANSFER_OUT")
    transfer_in = add_entry(db, (tenant_id, pool_id, canonical), 1, entry_type="MERGE_TRANSFER_IN")
    adjustment = add_entry(db, (tenant_id, pool_id, canonical), 2, entry_type="COUNT_ADJUSTMENT")
    return Pair(
        tenant_id,
        pool_id,
        source,
        canonical,
        add_credential(db, tenant_id),
        transfer_out,
        transfer_in,
        adjustment,
    )


def actor(prefix: str = "") -> dict[str, object]:
    return {f"{prefix}actor_ref": "admin_1", f"{prefix}actor_role": "stock_admin"}


def created(pair: Pair) -> dict[str, object]:
    return {
        "merge_id": new_id(MERGE_ID_PREFIX),
        "tenant_id": pair.tenant_id,
        "source_sku_id": pair.source_sku_id,
        "canonical_sku_id": pair.canonical_sku_id,
        "credential_id": pair.credential_id,
        "request_id": new_id("req"),
    } | actor()


def counted(pair: Pair) -> dict[str, object]:
    return {
        "pool_id": pair.pool_id,
        "shelf_count": 17,
        "unsellable_count": 0,
        "counted_credential_id": pair.credential_id,
        "counted_request_id": new_id("req"),
        "counted_at": OCCURRED_AT,
    } | actor("counted_")


def executed(pair: Pair) -> dict[str, object]:
    return {
        "executed_credential_id": pair.credential_id,
        "executed_request_id": new_id("req"),
        "executed_at": OCCURRED_AT,
        "transfer_out_entry_id": pair.transfer_out_entry_id,
        "transfer_in_entry_id": pair.transfer_in_entry_id,
        "adjustment_entry_id": pair.adjustment_entry_id,
        "repointed_line_count": 3,
    } | actor("executed_")


def closed(prefix: str, pair: Pair) -> dict[str, object]:
    """完成或取消的一组：prefix 为 completed_ 或 cancelled_。"""
    return {
        f"{prefix}credential_id": pair.credential_id,
        f"{prefix}request_id": new_id("req"),
        f"{prefix}at": OCCURRED_AT,
    } | actor(prefix)


def consistent(pair: Pair, status: str) -> dict[str, object]:
    """该状态下一条合法的行；cancelled 默认不带录入实数。"""
    values = created(pair) | {"status": status}
    if status in (COUNTED, RECONCILE_PENDING, COMPLETED):
        values |= counted(pair)
    if status in (RECONCILE_PENDING, COMPLETED):
        values |= executed(pair)
    if status == COMPLETED:
        values |= closed("completed_", pair)
    if status == CANCELLED:
        values |= closed("cancelled_", pair)
    return values


def merge_insert(pair: Pair, status: str, **overrides: object) -> Executable:
    return insert(SkuMerge).values(consistent(pair, status) | overrides)


def without(columns) -> dict[str, None]:
    return dict.fromkeys(columns)


# 合法的行


def test_each_status_accepts_a_consistent_row(db: Connection):
    pair = add_pair(db)
    for status in STATUSES:
        db.execute(merge_insert(pair, status))


def test_a_cancelled_merge_may_or_may_not_keep_its_count(db: Connection):
    pair = add_pair(db)
    db.execute(merge_insert(pair, CANCELLED))
    db.execute(merge_insert(pair, CANCELLED, **counted(pair)))


def test_an_execution_without_an_adjustment_entry_is_accepted(db: Connection):
    pair = add_pair(db)
    for status in (RECONCILE_PENDING, COMPLETED):
        db.execute(merge_insert(pair, status, adjustment_entry_id=None))


def test_actor_columns_may_both_be_empty(db: Connection):
    pair = add_pair(db)
    db.execute(merge_insert(pair, SELLING_PAUSED, **without(actor())))
    db.execute(merge_insert(pair, COUNTED, **without(actor("counted_"))))
    db.execute(merge_insert(pair, RECONCILE_PENDING, **without(actor("executed_"))))
    db.execute(merge_insert(pair, COMPLETED, **without(actor("completed_"))))
    db.execute(merge_insert(pair, CANCELLED, **without(actor("cancelled_"))))


# 检查约束


def test_source_must_differ_from_canonical(db: Connection):
    pair = add_pair(db)
    same = merge_insert(pair, SELLING_PAUSED, canonical_sku_id=pair.source_sku_id)
    assert_rejected_by(db, same, f"{CHECK}source_not_canonical")


@pytest.mark.parametrize("status", ["draft", "migrating", "failed_paused", "Counted", "paused", ""])
def test_status_outside_the_five_values_is_rejected(db: Connection, status: str):
    pair = add_pair(db)
    assert_rejected_by(db, merge_insert(pair, SELLING_PAUSED, status=status), f"{CHECK}status")


def test_selling_paused_has_no_count(db: Connection):
    pair = add_pair(db)
    statement = merge_insert(pair, SELLING_PAUSED, **counted(pair))
    assert_rejected_by(db, statement, f"{CHECK}count_columns")


@pytest.mark.parametrize("status", [COUNTED, RECONCILE_PENDING, COMPLETED])
def test_counted_and_later_states_need_the_count(db: Connection, status: str):
    pair = add_pair(db)
    statement = merge_insert(pair, status, **without(COUNT_COLUMNS))
    assert_rejected_by(db, statement, f"{CHECK}count_columns")


@pytest.mark.parametrize("status", [COUNTED, CANCELLED])
@pytest.mark.parametrize("column", COUNT_COLUMNS)
def test_count_columns_are_all_set_or_all_empty(db: Connection, status: str, column: str):
    pair = add_pair(db)
    statement = merge_insert(pair, status, **(counted(pair) | {column: None}))
    assert_rejected_by(db, statement, f"{CHECK}count_columns")


@pytest.mark.parametrize("column", EXECUTION_COLUMNS)
@pytest.mark.parametrize("status", [RECONCILE_PENDING, COMPLETED])
def test_executed_states_need_every_execution_column(db: Connection, status: str, column: str):
    """含缺任一条迁移流水 ID。"""
    pair = add_pair(db)
    statement = merge_insert(pair, status, **{column: None})
    assert_rejected_by(db, statement, f"{CHECK}execution_columns")


@pytest.mark.parametrize("status", [SELLING_PAUSED, COUNTED, CANCELLED])
def test_other_states_have_no_execution(db: Connection, status: str):
    pair = add_pair(db)
    statement = merge_insert(pair, status, **executed(pair))
    assert_rejected_by(db, statement, f"{CHECK}execution_columns")
    adjustment_only = merge_insert(pair, status, adjustment_entry_id=pair.adjustment_entry_id)
    assert_rejected_by(db, adjustment_only, f"{CHECK}execution_columns")


@pytest.mark.parametrize("column", COMPLETION_COLUMNS)
def test_completed_needs_every_completion_column(db: Connection, column: str):
    pair = add_pair(db)
    statement = merge_insert(pair, COMPLETED, **{column: None})
    assert_rejected_by(db, statement, f"{CHECK}completion_columns")


@pytest.mark.parametrize("status", [SELLING_PAUSED, COUNTED, RECONCILE_PENDING, CANCELLED])
def test_only_completed_has_completion_columns(db: Connection, status: str):
    pair = add_pair(db)
    statement = merge_insert(pair, status, **closed("completed_", pair))
    assert_rejected_by(db, statement, f"{CHECK}completion_columns")


@pytest.mark.parametrize("status", [SELLING_PAUSED, COUNTED, RECONCILE_PENDING, COMPLETED])
def test_only_cancelled_has_cancellation_columns(db: Connection, status: str):
    pair = add_pair(db)
    statement = merge_insert(pair, status, **closed("cancelled_", pair))
    assert_rejected_by(db, statement, f"{CHECK}cancellation_columns")


@pytest.mark.parametrize("column", CANCELLATION_COLUMNS)
def test_cancelled_needs_every_cancellation_column(db: Connection, column: str):
    pair = add_pair(db)
    statement = merge_insert(pair, CANCELLED, **{column: None})
    assert_rejected_by(db, statement, f"{CHECK}cancellation_columns")


# 各组操作者所在的状态：这组列在该状态下有值。
ACTOR_GROUPS = [
    ("", SELLING_PAUSED),
    ("counted_", COUNTED),
    ("executed_", RECONCILE_PENDING),
    ("completed_", COMPLETED),
    ("cancelled_", CANCELLED),
]


@pytest.mark.parametrize(("prefix", "status"), ACTOR_GROUPS)
def test_actor_ref_and_actor_role_are_both_set_or_both_empty(
    db: Connection, prefix: str, status: str
):
    pair = add_pair(db)
    for column in (f"{prefix}actor_ref", f"{prefix}actor_role"):
        statement = merge_insert(pair, status, **{column: None})
        assert_rejected_by(db, statement, f"{CHECK}{prefix}actor")


# 外键：指向的对象须属于同一租户


def test_skus_must_belong_to_the_same_tenant(db: Connection):
    pair, other = add_pair(db), add_pair(db)
    assert_rejected_by(
        db,
        merge_insert(pair, SELLING_PAUSED, source_sku_id=other.source_sku_id),
        "fk_sku_merges_source_sku_id_skus",
    )
    assert_rejected_by(
        db,
        merge_insert(pair, SELLING_PAUSED, canonical_sku_id=other.canonical_sku_id),
        "fk_sku_merges_canonical_sku_id_skus",
    )


def test_pool_must_belong_to_the_same_tenant(db: Connection):
    pair, other = add_pair(db), add_pair(db)
    assert_rejected_by(
        db,
        merge_insert(pair, COUNTED, pool_id=other.pool_id),
        "fk_sku_merges_pool_id_stock_pools",
    )


@pytest.mark.parametrize(
    ("column", "status"),
    [
        ("credential_id", SELLING_PAUSED),
        ("counted_credential_id", COUNTED),
        ("executed_credential_id", RECONCILE_PENDING),
        ("completed_credential_id", COMPLETED),
        ("cancelled_credential_id", CANCELLED),
    ],
)
def test_credentials_must_belong_to_the_same_tenant(db: Connection, column: str, status: str):
    pair, other = add_pair(db), add_pair(db)
    statement = merge_insert(pair, status, **{column: other.credential_id})
    assert_rejected_by(db, statement, f"fk_sku_merges_{column}_credentials")


@pytest.mark.parametrize(
    "column", ["transfer_out_entry_id", "transfer_in_entry_id", "adjustment_entry_id"]
)
def test_ledger_entries_must_belong_to_the_same_tenant(db: Connection, column: str):
    pair, other = add_pair(db), add_pair(db)
    statement = merge_insert(pair, RECONCILE_PENDING, **{column: getattr(other, column)})
    assert_rejected_by(db, statement, f"fk_sku_merges_{column}_ledger_entries")


# 快照


def add_counted_merge(db: Connection, pair: Pair) -> str:
    values = consistent(pair, COUNTED)
    db.execute(insert(SkuMerge).values(values))
    return values["merge_id"]


def snapshot_insert(pair: Pair, merge_id: str, sku_id: str, **overrides: object) -> Executable:
    values = {
        "merge_id": merge_id,
        "sku_id": sku_id,
        "tenant_id": pair.tenant_id,
        "pool_id": pair.pool_id,
        "stock_balance": 5,
        "reserved": 2,
        "unavailable": 1,
        "paid_pending_dispatch": 1,
        "balance_version": 4,
    }
    return insert(SkuMergeSnapshot).values(values | overrides)


def test_a_merge_has_one_snapshot_per_sku(db: Connection):
    pair = add_pair(db)
    merge_id = add_counted_merge(db, pair)
    for sku_id in (pair.source_sku_id, pair.canonical_sku_id):
        db.execute(snapshot_insert(pair, merge_id, sku_id))
    again = snapshot_insert(pair, merge_id, pair.source_sku_id)
    assert_rejected_by(db, again, "PRIMARY")


def test_a_snapshot_needs_an_existing_balance_row(db: Connection):
    pair = add_pair(db)
    merge_id = add_counted_merge(db, pair)
    # 同租户的 SKU，但在这个池里没有余额行。
    unbalanced = add_sku(db, pair.tenant_id)
    assert_rejected_by(
        db,
        snapshot_insert(pair, merge_id, unbalanced),
        "fk_sku_merge_snapshots_tenant_id_stock_balances",
    )
    # 同租户的另一个池里没有这个 SKU 的余额行。
    other_pool = add_pool(db, pair.tenant_id)
    assert_rejected_by(
        db,
        snapshot_insert(pair, merge_id, pair.source_sku_id, pool_id=other_pool),
        "fk_sku_merge_snapshots_tenant_id_stock_balances",
    )


def test_a_snapshot_belongs_to_a_merge_of_the_same_tenant(db: Connection):
    pair, other = add_pair(db), add_pair(db)
    other_merge = add_counted_merge(db, other)
    assert_rejected_by(
        db,
        snapshot_insert(pair, other_merge, pair.source_sku_id),
        "fk_sku_merge_snapshots_tenant_id_sku_merges",
    )


@pytest.mark.parametrize(
    "column",
    ["stock_balance", "reserved", "unavailable", "paid_pending_dispatch", "balance_version"],
)
def test_snapshot_quantities_are_not_negative(db: Connection, column: str):
    pair = add_pair(db)
    merge_id = add_counted_merge(db, pair)
    db.execute(snapshot_insert(pair, merge_id, pair.canonical_sku_id, **{column: 0}))
    assert_rejected_by(
        db,
        snapshot_insert(pair, merge_id, pair.source_sku_id, **{column: -1}),
        f"ck_sku_merge_snapshots_{column}_nonnegative",
    )


# 迁移（production 测试库）


def test_downgrade_drops_both_tables_and_upgrade_restores_them(production_engine: Engine):
    try:
        migrate_production(command.downgrade, "0010", PRODUCTION)
        assert alembic_version(production_engine) == "0010"
        for table in TABLES:
            assert not has_table(production_engine, table)
    finally:
        migrate_production(command.upgrade, "head", PRODUCTION)
    assert alembic_version(production_engine) == head_revision()
    for table in TABLES:
        assert has_table(production_engine, table)
