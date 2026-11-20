"""SKU 合并单与快照两张表的约束在真 MySQL 上生效（契约 2.8 的存放）。

约束测试在 demo 库上跑（conftest 的 db 夹具，每个测试一个事务、结束时回滚）。
降到 0010 照 test_demo_reset_schema.py 的先例在 production 测试库上做，
无论成败都在 finally 里升回 head，不动共享的 demo 库。
"""

from dataclasses import dataclass
from datetime import datetime

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
    add_credential,
    add_entry,
    add_pool,
    add_sku,
    add_tenant,
    assert_rejected_by,
    assert_rejected_by_one_of,
    balance_insert,
    new_id,
)
from tests.integration.test_demo_reset_schema import (
    alembic_version,
    has_table,
    head_revision,
    migrate_production,
    rolled_back,
)

AT = datetime(2026, 11, 19, 9, 0)
STOCK_ADMIN = "stock_admin"
TABLES = ("sku_merges", "sku_merge_snapshots")


@dataclass
class Parties:
    """一个租户的池、两个在该池有余额行的 SKU、一个凭据与三条流水（各引用列的目标）。"""

    tenant_id: str
    pool_id: str
    source_sku_id: str
    canonical_sku_id: str
    credential_id: str
    transfer_out_entry_id: str
    transfer_in_entry_id: str
    adjustment_entry_id: str


def add_parties(db: Connection) -> Parties:
    tenant_id = add_tenant(db)
    pool_id = add_pool(db, tenant_id)
    source, canonical = add_sku(db, tenant_id), add_sku(db, tenant_id)
    for sku_id in (source, canonical):
        db.execute(balance_insert(tenant_id, pool_id, sku_id))
    source_balance = (tenant_id, pool_id, source)
    canonical_balance = (tenant_id, pool_id, canonical)
    return Parties(
        tenant_id=tenant_id,
        pool_id=pool_id,
        source_sku_id=source,
        canonical_sku_id=canonical,
        credential_id=add_credential(db, tenant_id),
        transfer_out_entry_id=add_entry(db, source_balance, 1),
        transfer_in_entry_id=add_entry(db, canonical_balance, 1),
        adjustment_entry_id=add_entry(db, canonical_balance, 2),
    )


def actor(group: str) -> dict[str, object]:
    return {f"{group}_actor_ref": "admin_1", f"{group}_actor_role": STOCK_ADMIN}


def count_values(parties: Parties) -> dict[str, object]:
    return {
        "pool_id": parties.pool_id,
        "shelf_count": 17,
        "unsellable_count": 0,
        "counted_credential_id": parties.credential_id,
        "counted_request_id": new_id("req"),
        "counted_at": AT,
    } | actor("counted")


def execution_values(parties: Parties) -> dict[str, object]:
    return {
        "executed_credential_id": parties.credential_id,
        "executed_request_id": new_id("req"),
        "executed_at": AT,
        "transfer_out_entry_id": parties.transfer_out_entry_id,
        "transfer_in_entry_id": parties.transfer_in_entry_id,
        "adjustment_entry_id": parties.adjustment_entry_id,
        "repointed_line_count": 3,
    } | actor("executed")


def completion_values(parties: Parties) -> dict[str, object]:
    return {
        "completed_credential_id": parties.credential_id,
        "completed_request_id": new_id("req"),
        "completed_at": AT,
    } | actor("completed")


def cancellation_values(parties: Parties) -> dict[str, object]:
    return {
        "cancelled_credential_id": parties.credential_id,
        "cancelled_request_id": new_id("req"),
        "cancelled_at": AT,
    } | actor("cancelled")


def state_values(parties: Parties, status: str) -> dict[str, object]:
    """该状态合法的一行要有的各组列；cancelled 默认没录入过实数。"""
    values: dict[str, object] = {"status": status}
    if status in (COUNTED, RECONCILE_PENDING, COMPLETED):
        values |= count_values(parties)
    if status in (RECONCILE_PENDING, COMPLETED):
        values |= execution_values(parties)
    if status == COMPLETED:
        values |= completion_values(parties)
    if status == CANCELLED:
        values |= cancellation_values(parties)
    return values


def merge_insert(parties: Parties, status: str = SELLING_PAUSED, **overrides: object) -> Executable:
    values = {
        "merge_id": new_id(MERGE_ID_PREFIX),
        "tenant_id": parties.tenant_id,
        "source_sku_id": parties.source_sku_id,
        "canonical_sku_id": parties.canonical_sku_id,
        "reason": "same tee entered twice",
        "credential_id": parties.credential_id,
        "actor_ref": "admin_1",
        "actor_role": STOCK_ADMIN,
        "request_id": new_id("req"),
    }
    return insert(SkuMerge).values(values | state_values(parties, status) | overrides)


def add_merge(db: Connection, parties: Parties, status: str = COUNTED, **overrides: object) -> str:
    merge_id = new_id(MERGE_ID_PREFIX)
    db.execute(merge_insert(parties, status, merge_id=merge_id, **overrides))
    return merge_id


# --- 合并单：合法的行 ---


@pytest.mark.parametrize("status", STATUSES)
def test_a_consistent_row_of_each_status_is_accepted(db: Connection, status: str):
    db.execute(merge_insert(add_parties(db), status))


def test_a_cancelled_merge_may_or_may_not_carry_the_count(db: Connection):
    parties = add_parties(db)
    db.execute(merge_insert(parties, CANCELLED))
    db.execute(merge_insert(parties, CANCELLED, **count_values(parties)))


def test_the_adjustment_entry_may_be_empty_after_execution(db: Connection):
    parties = add_parties(db)
    for status in (RECONCILE_PENDING, COMPLETED):
        db.execute(merge_insert(parties, status, adjustment_entry_id=None))


def test_actor_columns_of_a_group_may_both_be_empty(db: Connection):
    parties = add_parties(db)
    for group, status in (
        ("counted", COUNTED),
        ("executed", RECONCILE_PENDING),
        ("completed", COMPLETED),
        ("cancelled", CANCELLED),
    ):
        empty = {f"{group}_actor_ref": None, f"{group}_actor_role": None}
        db.execute(merge_insert(parties, status, **empty))


# --- 合并单：检查约束 ---


def test_source_must_differ_from_canonical(db: Connection):
    parties = add_parties(db)
    same = merge_insert(parties, canonical_sku_id=parties.source_sku_id)
    assert_rejected_by(db, same, "ck_sku_merges_source_not_canonical")


@pytest.mark.parametrize("status", ["draft", "migrating", "failed_paused", "Counted", ""])
def test_status_is_one_of_the_five(db: Connection, status: str):
    assert_rejected_by(db, merge_insert(add_parties(db), status), "ck_sku_merges_status")


def test_selling_paused_carries_no_count(db: Connection):
    parties = add_parties(db)
    with_count = merge_insert(parties, SELLING_PAUSED, **count_values(parties))
    assert_rejected_by(db, with_count, "ck_sku_merges_count_columns")


@pytest.mark.parametrize("status", [COUNTED, RECONCILE_PENDING, COMPLETED])
def test_counted_and_later_states_carry_the_whole_count(db: Connection, status: str):
    parties = add_parties(db)
    without_count = {column: None for column in COUNT_COLUMNS}
    assert_rejected_by(
        db, merge_insert(parties, status, **without_count), "ck_sku_merges_count_columns"
    )
    for column in COUNT_COLUMNS:
        missing = merge_insert(parties, status, **{column: None})
        assert_rejected_by(db, missing, "ck_sku_merges_count_columns")


def test_a_cancelled_count_is_all_or_nothing(db: Connection):
    parties = add_parties(db)
    for column in COUNT_COLUMNS:
        partial = count_values(parties) | {column: None}
        assert_rejected_by(
            db, merge_insert(parties, CANCELLED, **partial), "ck_sku_merges_count_columns"
        )


@pytest.mark.parametrize("status", [RECONCILE_PENDING, COMPLETED])
def test_executed_states_carry_the_execution_and_both_transfer_entries(db: Connection, status: str):
    parties = add_parties(db)
    for column in EXECUTION_COLUMNS:
        missing = merge_insert(parties, status, **{column: None})
        assert_rejected_by(db, missing, "ck_sku_merges_execution_columns")


@pytest.mark.parametrize("status", [SELLING_PAUSED, COUNTED, CANCELLED])
def test_states_before_execution_carry_no_execution(db: Connection, status: str):
    parties = add_parties(db)
    executed = merge_insert(parties, status, **execution_values(parties))
    assert_rejected_by(db, executed, "ck_sku_merges_execution_columns")
    for column in (*EXECUTION_COLUMNS, "adjustment_entry_id"):
        one = merge_insert(parties, status, **{column: execution_values(parties)[column]})
        assert_rejected_by(db, one, "ck_sku_merges_execution_columns")


def test_completed_carries_the_completion(db: Connection):
    parties = add_parties(db)
    for column in COMPLETION_COLUMNS:
        missing = merge_insert(parties, COMPLETED, **{column: None})
        assert_rejected_by(db, missing, "ck_sku_merges_completion_columns")


@pytest.mark.parametrize("status", [SELLING_PAUSED, COUNTED, RECONCILE_PENDING, CANCELLED])
def test_only_completed_carries_completion_columns(db: Connection, status: str):
    parties = add_parties(db)
    for column in COMPLETION_COLUMNS:
        one = merge_insert(parties, status, **{column: completion_values(parties)[column]})
        assert_rejected_by(db, one, "ck_sku_merges_completion_columns")


def test_cancelled_carries_the_cancellation(db: Connection):
    parties = add_parties(db)
    for column in CANCELLATION_COLUMNS:
        missing = merge_insert(parties, CANCELLED, **{column: None})
        assert_rejected_by(db, missing, "ck_sku_merges_cancellation_columns")


@pytest.mark.parametrize("status", [SELLING_PAUSED, COUNTED, RECONCILE_PENDING, COMPLETED])
def test_only_cancelled_carries_cancellation_columns(db: Connection, status: str):
    parties = add_parties(db)
    for column in CANCELLATION_COLUMNS:
        one = merge_insert(parties, status, **{column: cancellation_values(parties)[column]})
        assert_rejected_by(db, one, "ck_sku_merges_cancellation_columns")


@pytest.mark.parametrize(
    ("group", "status"),
    [
        ("counted", COUNTED),
        ("executed", RECONCILE_PENDING),
        ("completed", COMPLETED),
        ("cancelled", CANCELLED),
    ],
)
def test_actor_ref_and_actor_role_of_a_group_are_both_set_or_both_empty(
    db: Connection, group: str, status: str
):
    parties = add_parties(db)
    for column in (f"{group}_actor_ref", f"{group}_actor_role"):
        only_one = merge_insert(parties, status, **{column: None})
        assert_rejected_by(db, only_one, f"ck_sku_merges_{group}_actor")


def test_the_creating_actor_is_required(db: Connection):
    """新建要求 stock_admin 角色，两列都必有。"""
    parties = add_parties(db)
    for column in ("actor_ref", "actor_role"):
        assert_rejected_by(db, merge_insert(parties, **{column: None}), f"'{column}'")


# --- 合并单：外键 ---


def test_merge_references_must_belong_to_the_same_tenant(db: Connection):
    parties, other = add_parties(db), add_parties(db)
    cases = {
        "fk_sku_merges_source_sku_id_skus": {"source_sku_id": other.source_sku_id},
        "fk_sku_merges_canonical_sku_id_skus": {"canonical_sku_id": other.canonical_sku_id},
        "fk_sku_merges_pool_id_stock_pools": {"pool_id": other.pool_id},
    }
    for column in (
        "credential_id",
        "counted_credential_id",
        "executed_credential_id",
        "completed_credential_id",
    ):
        cases[f"fk_sku_merges_{column}_credentials"] = {column: other.credential_id}
    for column in ("transfer_out_entry_id", "transfer_in_entry_id", "adjustment_entry_id"):
        cases[f"fk_sku_merges_{column}_ledger_entries"] = {column: getattr(other, column)}
    for constraint, overrides in cases.items():
        assert_rejected_by(db, merge_insert(parties, COMPLETED, **overrides), constraint)
    cancelled_by_other = {"cancelled_credential_id": other.credential_id}
    assert_rejected_by(
        db,
        merge_insert(parties, CANCELLED, **cancelled_by_other),
        "fk_sku_merges_cancelled_credential_id_credentials",
    )


def test_merge_id_is_unique(db: Connection):
    parties = add_parties(db)
    merge_id = add_merge(db, parties)
    assert_rejected_by(db, merge_insert(parties, merge_id=merge_id), "PRIMARY")


# --- 快照 ---


def snapshot_insert(
    parties: Parties, merge_id: str, sku_id: str | None = None, **overrides: object
) -> Executable:
    """默认是来源 SKU 刚预建的余额行：四个量为 0、版本 0。"""
    values = {
        "merge_id": merge_id,
        "sku_id": sku_id or parties.source_sku_id,
        "tenant_id": parties.tenant_id,
        "pool_id": parties.pool_id,
        "stock_balance": 0,
        "reserved": 0,
        "unavailable": 0,
        "paid_pending_dispatch": 0,
        "balance_version": 0,
    }
    return insert(SkuMergeSnapshot).values(values | overrides)


def test_a_merge_has_one_snapshot_per_sku(db: Connection):
    parties = add_parties(db)
    merge_id = add_merge(db, parties)
    quantities = {"stock_balance": 5, "reserved": 2, "unavailable": 1, "paid_pending_dispatch": 2}
    db.execute(snapshot_insert(parties, merge_id, balance_version=4, **quantities))
    db.execute(snapshot_insert(parties, merge_id, parties.canonical_sku_id))
    again = snapshot_insert(parties, merge_id)
    assert_rejected_by(db, again, "PRIMARY")


def test_a_snapshot_points_at_an_existing_balance_row(db: Connection):
    parties = add_parties(db)
    merge_id = add_merge(db, parties)
    # 同租户的 SKU，但在该池还没有余额行。
    without_balance = add_sku(db, parties.tenant_id)
    assert_rejected_by(
        db,
        snapshot_insert(parties, merge_id, without_balance),
        "fk_sku_merge_snapshots_tenant_id_stock_balances",
    )
    other_pool = add_pool(db, parties.tenant_id)
    assert_rejected_by(
        db,
        snapshot_insert(parties, merge_id, pool_id=other_pool),
        "fk_sku_merge_snapshots_tenant_id_stock_balances",
    )


def test_a_snapshot_belongs_to_a_merge_of_the_same_tenant(db: Connection):
    parties = add_parties(db)
    other = add_parties(db)
    for merge_id in (add_merge(db, other), new_id(MERGE_ID_PREFIX)):
        assert_rejected_by(
            db,
            snapshot_insert(parties, merge_id),
            "fk_sku_merge_snapshots_tenant_id_sku_merges",
        )
    # 余额行属于别的租户时同样被拒。
    merge_id = add_merge(db, parties)
    assert_rejected_by(
        db,
        snapshot_insert(parties, merge_id, other.source_sku_id, pool_id=other.pool_id),
        "fk_sku_merge_snapshots_tenant_id_stock_balances",
    )


def test_snapshot_quantities_and_version_are_nonnegative(db: Connection):
    parties = add_parties(db)
    merge_id = add_merge(db, parties)
    for column in ("reserved", "unavailable", "paid_pending_dispatch", "balance_version"):
        negative = snapshot_insert(parties, merge_id, **{column: -1})
        assert_rejected_by(db, negative, f"ck_sku_merge_snapshots_{column}_nonnegative")
    assert_rejected_by_one_of(
        db,
        snapshot_insert(parties, merge_id, stock_balance=-1),
        "ck_sku_merge_snapshots_stock_balance_nonnegative",
        "ck_sku_merge_snapshots_reserved_unavailable_within_balance",
    )


def test_snapshot_reserved_and_unavailable_fit_within_the_balance(db: Connection):
    parties = add_parties(db)
    merge_id = add_merge(db, parties)
    over = snapshot_insert(parties, merge_id, stock_balance=3, reserved=2, unavailable=2)
    assert_rejected_by(db, over, "ck_sku_merge_snapshots_reserved_unavailable_within_balance")


# --- 迁移往返（production 测试库） ---


def test_downgrade_to_0010_drops_both_tables_and_upgrade_restores_them(production_engine: Engine):
    try:
        migrate_production(command.downgrade, "0010", PRODUCTION)
        assert alembic_version(production_engine) == "0010"
        for table in TABLES:
            assert not has_table(production_engine, table)

        migrate_production(command.upgrade, "head", PRODUCTION)
        assert alembic_version(production_engine) == head_revision()
        for table in TABLES:
            assert has_table(production_engine, table)
        # 升回之后照常可写，约束也回来了；写入的行回滚。
        with rolled_back(production_engine) as conn:
            parties = add_parties(conn)
            merge_id = add_merge(conn, parties)
            conn.execute(snapshot_insert(parties, merge_id))
            same = merge_insert(parties, canonical_sku_id=parties.source_sku_id)
            assert_rejected_by(conn, same, "ck_sku_merges_source_not_canonical")
    finally:
        migrate_production(command.upgrade, "head", PRODUCTION)
    assert alembic_version(production_engine) == head_revision()
