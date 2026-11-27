"""平台连接、写入暂停、人工核对、告警、对账与运营日志六张表在真 MySQL 上生效（契约 3.2 的存放，
Q-99、Q-101；第 10 节第 1 条）。

只验表结构与约束，写这几张表的服务与作业在各自的任务里。demo 库用 conftest 的 db 夹具；
迁移往返照 test_demo_reset_schema 的先例在 production 库上做。
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import Connection, Engine, delete, insert, inspect, select
from sqlalchemy.sql.expression import Executable

from alembic import command
from app.domain.channels import PLATFORM, SHOPEE
from app.domain.environment import PRODUCTION
from app.models.catalog import Tenant
from app.models.inventory import Location, SalesChannel, StockPool
from app.models.platform_ops import (
    APP_PROFILES,
    CONNECTION_STATUSES,
    MODES,
    OPS_ACTIONS,
    TRIGGERS,
    ManualCheck,
    PlatformAlert,
    PlatformConnection,
    PlatformOpsLog,
    PlatformWritePause,
    ReconciliationRun,
)
from tests.integration.conftest import (
    add_credential,
    add_pool,
    add_sku,
    add_tenant,
    assert_rejected_by,
    channel_insert,
    new_id,
)
from tests.integration.test_demo_reset_schema import (
    Shop,
    alembic_version,
    has_table,
    head_revision,
    migrate_production,
)
from tests.integration.test_platform_schema import (
    add_order,
    add_platform_channel,
    add_platform_shop,
)

AT = datetime(2026, 11, 24, 8, 0)
LATER = AT + timedelta(hours=12)
OPS_TABLES = (
    "platform_connections",
    "platform_write_pause",
    "manual_checks",
    "platform_alerts",
    "reconciliation_runs",
    "platform_ops_log",
)
ACTOR = {"actor_ref": "admin_1", "actor_role": "stock_admin"}
HALF_ACTORS = [{"actor_ref": "admin_1"}, {"actor_role": "stock_admin"}]


def indexes(db: Connection, table: str) -> dict[str, list[str]]:
    return {index["name"]: index["column_names"] for index in inspect(db).get_indexes(table)}


def other_credential(db: Connection) -> str:
    """另一个租户的凭据：复合外键要指向同租户的行。"""
    return add_credential(db, add_tenant(db))


# platform_connections

CONNECTION_CHANNEL_FK = "fk_platform_connections_tenant_id_sales_channels"


def connection_insert(shop: Shop, **overrides: object) -> Executable:
    values = {"tenant_id": shop.tenant_id, "channel_id": shop.channel_id}
    return insert(PlatformConnection).values(values | overrides)


def connection_row(db: Connection, shop: Shop, channel_id: str | None = None):
    return db.execute(
        select(PlatformConnection)
        .where(PlatformConnection.tenant_id == shop.tenant_id)
        .where(PlatformConnection.channel_id == (channel_id or shop.channel_id))
    ).one()


def test_connection_defaults(db: Connection):
    shop = add_platform_shop(db)
    db.execute(connection_insert(shop))
    row = connection_row(db, shop)
    assert (row.mode, row.status, row.app_profile) == ("manual", "not_connected", "self_seller")
    assert row.capabilities == {}
    assert row.mode_changed_at is not None
    assert row.created_at is not None and row.updated_at is not None
    for column in (
        "access_token_ciphertext",
        "access_token_expires_at",
        "refresh_token_ciphertext",
        "refresh_token_expires_at",
        "pull_cursor",
        "next_pull_at",
        "last_refreshed_at",
        "last_synced_at",
    ):
        assert getattr(row, column) is None, column


def test_a_channel_has_one_connection(db: Connection):
    shop = add_platform_shop(db)
    db.execute(connection_insert(shop))
    assert_rejected_by(db, connection_insert(shop, mode="auto"), "PRIMARY")
    db.execute(connection_insert(shop, channel_id=add_platform_channel(db, shop)))


def test_connection_channel_must_exist_in_the_same_tenant(db: Connection):
    shop, other = add_platform_shop(db), add_platform_shop(db)
    for channel_id in (new_id("chn"), other.channel_id):
        foreign = connection_insert(shop, channel_id=channel_id)
        assert_rejected_by(db, foreign, CONNECTION_CHANNEL_FK)


@pytest.mark.parametrize(
    "column, accepted, rejected",
    [
        ("mode", MODES, ("MANUAL", "Auto", "semi", "")),
        ("status", CONNECTION_STATUSES, ("ACTIVE", "Revoked", "expired", "")),
        ("app_profile", APP_PROFILES, ("SELF_SELLER", "partner", "")),
    ],
)
def test_connection_values_must_be_exact_known_values(
    db: Connection, column: str, accepted: tuple[str, ...], rejected: tuple[str, ...]
):
    shop = add_platform_shop(db)
    for value in accepted:
        channel_id = add_platform_channel(db, shop)
        db.execute(connection_insert(shop, channel_id=channel_id, **{column: value}))
    for value in rejected:
        assert_rejected_by(
            db, connection_insert(shop, **{column: value}), f"ck_platform_connections_{column}"
        )


def test_capabilities_must_be_an_object(db: Connection):
    shop = add_platform_shop(db)
    capabilities = {"inventory_write": True, "order_webhook": False}
    db.execute(connection_insert(shop, capabilities=capabilities))
    assert connection_row(db, shop).capabilities == capabilities
    # None 存成 JSON 的 null，同样不是对象。
    for value in ([], ["inventory_write"], "capabilities", 1, None):
        assert_rejected_by(
            db,
            connection_insert(shop, channel_id=add_platform_channel(db, shop), capabilities=value),
            "ck_platform_connections_capabilities",
        )


def test_connection_stores_ciphertexts_and_times(db: Connection):
    # 令牌与状态的搭配由连接与授权的任务管，不是数据库约束：未连接也可以有密文。
    shop = add_platform_shop(db)
    values = {
        "access_token_ciphertext": b"\x00\xff" * 2048,
        "access_token_expires_at": LATER,
        "refresh_token_ciphertext": b"\x01",
        "refresh_token_expires_at": LATER,
        "pull_cursor": AT,
        "next_pull_at": LATER,
        "last_refreshed_at": AT,
        "last_synced_at": AT,
    }
    db.execute(connection_insert(shop, **values))
    row = connection_row(db, shop)
    assert {column: getattr(row, column) for column in values} == values
    assert row.status == "not_connected"


# platform_write_pause

SET_CHECK = "ck_platform_write_pause_set_columns"
CLEARED_CHECK = "ck_platform_write_pause_cleared_columns"


def pause_insert(tenant_id: str, **overrides: object) -> Executable:
    return insert(PlatformWritePause).values({"tenant_id": tenant_id} | overrides)


def set_columns(credential_id: str) -> dict[str, object]:
    return {"set_credential_id": credential_id, "set_at": AT, "set_reason": "platform outage"}


def cleared_columns(credential_id: str) -> dict[str, object]:
    return {
        "cleared_credential_id": credential_id,
        "cleared_at": LATER,
        "cleared_reason": "reconciled",
    }


def test_write_pause_defaults(db: Connection):
    tenant_id = add_tenant(db)
    db.execute(pause_insert(tenant_id))
    row = db.execute(
        select(PlatformWritePause).where(PlatformWritePause.tenant_id == tenant_id)
    ).one()
    assert row.paused is False
    for column in (*set_columns(""), *cleared_columns("")):
        assert getattr(row, column) is None, column
    assert row.updated_at is not None


def test_a_tenant_has_one_write_pause_row(db: Connection):
    tenant_id = add_tenant(db)
    db.execute(pause_insert(tenant_id))
    assert_rejected_by(db, pause_insert(tenant_id, paused=True), "PRIMARY")


def test_write_pause_tenant_must_exist(db: Connection):
    unknown = pause_insert(new_id("ten"))
    assert_rejected_by(db, unknown, "fk_platform_write_pause_tenant_id_tenants")


@pytest.mark.parametrize("group", ["set", "cleared"])
def test_each_group_is_all_set_or_all_empty(db: Connection, group: str):
    tenant_id = add_tenant(db)
    credential_id = add_credential(db, tenant_id)
    full = (set_columns if group == "set" else cleared_columns)(credential_id)
    check = SET_CHECK if group == "set" else CLEARED_CHECK
    for missing in full:
        partial = {column: value for column, value in full.items() if column != missing}
        assert_rejected_by(db, pause_insert(tenant_id, **partial), check)
    for column, value in full.items():
        assert_rejected_by(db, pause_insert(tenant_id, **{column: value}), check)
    db.execute(pause_insert(tenant_id, paused=True, **full))


def test_set_and_cleared_groups_together_are_accepted(db: Connection):
    tenant_id = add_tenant(db)
    credential_id = add_credential(db, tenant_id)
    both = set_columns(credential_id) | cleared_columns(credential_id)
    db.execute(pause_insert(tenant_id, **both))


@pytest.mark.parametrize(
    "group, constraint",
    [
        ("set", "fk_platform_write_pause_set_credential"),
        ("cleared", "fk_platform_write_pause_cleared_credential"),
    ],
)
def test_write_pause_credentials_must_exist_in_the_same_tenant(
    db: Connection, group: str, constraint: str
):
    tenant_id = add_tenant(db)
    columns = set_columns if group == "set" else cleared_columns
    for credential_id in (new_id("cred"), other_credential(db)):
        assert_rejected_by(db, pause_insert(tenant_id, **columns(credential_id)), constraint)


# manual_checks

CHECK_CHANNEL_FK = "fk_manual_checks_tenant_id_sales_channels"
CHECK_CREDENTIAL_FK = "fk_manual_checks_tenant_id_credentials"


def manual_check_insert(shop: Shop, **overrides: object) -> Executable:
    """默认是 12 小时的一次核对，没有发现差异，没有说明与操作者。"""
    values = {
        "manual_check_id": new_id("mck"),
        "tenant_id": shop.tenant_id,
        "channel_id": shop.channel_id,
        "checked_from": AT,
        "checked_until": LATER,
        "found_discrepancy": False,
        "credential_id": shop.credential_id,
        "request_id": new_id("req"),
    }
    return insert(ManualCheck).values(values | overrides)


def test_manual_check_defaults(db: Connection):
    shop = add_platform_shop(db)
    check_id = new_id("mck")
    db.execute(manual_check_insert(shop, manual_check_id=check_id))
    row = db.execute(select(ManualCheck).where(ManualCheck.manual_check_id == check_id)).one()
    assert (row.note, row.actor_ref, row.actor_role) == (None, None, None)
    assert row.found_discrepancy is False
    assert row.created_at is not None


def test_manual_check_id_is_the_primary_key(db: Connection):
    shop = add_platform_shop(db)
    check_id = new_id("mck")
    db.execute(manual_check_insert(shop, manual_check_id=check_id))
    assert_rejected_by(db, manual_check_insert(shop, manual_check_id=check_id), "PRIMARY")


def test_checked_from_is_before_checked_until(db: Connection):
    shop = add_platform_shop(db)
    for checked_from in (LATER, LATER + timedelta(microseconds=1)):
        assert_rejected_by(
            db,
            manual_check_insert(shop, checked_from=checked_from),
            "ck_manual_checks_checked_range",
        )
    db.execute(manual_check_insert(shop, checked_from=LATER - timedelta(microseconds=1)))


def test_manual_check_note_and_discrepancy(db: Connection):
    shop = add_platform_shop(db)
    db.execute(manual_check_insert(shop, found_discrepancy=True, note="n" * 500))
    assert_rejected_by(
        db,
        manual_check_insert(shop, found_discrepancy=None),
        "Column 'found_discrepancy' cannot be null",
    )


def test_manual_check_actor_ref_and_actor_role_are_both_set_or_both_empty(db: Connection):
    shop = add_platform_shop(db)
    for actor in HALF_ACTORS:
        assert_rejected_by(db, manual_check_insert(shop, **actor), "ck_manual_checks_actor")
    db.execute(manual_check_insert(shop, **ACTOR))


def test_manual_check_channel_and_credential_must_exist_in_the_same_tenant(db: Connection):
    shop, other = add_platform_shop(db), add_platform_shop(db)
    for channel_id in (new_id("chn"), other.channel_id):
        assert_rejected_by(db, manual_check_insert(shop, channel_id=channel_id), CHECK_CHANNEL_FK)
    for credential_id in (new_id("cred"), other.credential_id):
        assert_rejected_by(
            db, manual_check_insert(shop, credential_id=credential_id), CHECK_CREDENTIAL_FK
        )


def test_manual_checks_are_indexed_by_channel_and_checked_until(db: Connection):
    assert indexes(db, "manual_checks")["ix_manual_checks_tenant_id_channel_id_checked_until"] == [
        "tenant_id",
        "channel_id",
        "checked_until",
    ]


# platform_alerts


def alert_insert(tenant_id: str, **overrides: object) -> Executable:
    """默认是一条没有对象、还没发出、没确认的告警。"""
    values = {"alert_id": new_id("alr"), "tenant_id": tenant_id, "kind": "readback_mismatch"}
    return insert(PlatformAlert).values(values | overrides)


def test_alert_defaults(db: Connection):
    tenant_id = add_tenant(db)
    alert_id = new_id("alr")
    db.execute(alert_insert(tenant_id, alert_id=alert_id))
    row = db.execute(select(PlatformAlert).where(PlatformAlert.alert_id == alert_id)).one()
    assert row.notify_count == 0
    assert row.created_at is not None
    for column in (
        "channel_id",
        "sku_id",
        "platform_order_row_id",
        "detail",
        "first_sent_at",
        "last_notified_at",
        "acked_credential_id",
        "acked_at",
        "escalated_at",
    ):
        assert getattr(row, column) is None, column


def test_alert_id_is_the_primary_key(db: Connection):
    tenant_id = add_tenant(db)
    alert_id = new_id("alr")
    db.execute(alert_insert(tenant_id, alert_id=alert_id))
    assert_rejected_by(db, alert_insert(tenant_id, alert_id=alert_id), "PRIMARY")


def test_alert_kind_is_checked_by_the_application_not_the_database(db: Connection):
    tenant_id = add_tenant(db)
    for kind in ("readback_mismatch", "auth_failed", "Anything_Else", "k" * 32):
        db.execute(alert_insert(tenant_id, kind=kind))


def test_alert_with_all_objects_sent_and_acknowledged_is_accepted(db: Connection):
    shop = add_platform_shop(db)
    db.execute(
        alert_insert(
            shop.tenant_id,
            channel_id=shop.channel_id,
            sku_id=add_sku(db, shop.tenant_id),
            platform_order_row_id=add_order(db, shop),
            detail={"expected": 3, "actual": 5},
            first_sent_at=AT,
            last_notified_at=LATER,
            notify_count=2,
            acked_credential_id=shop.credential_id,
            acked_at=LATER,
            escalated_at=LATER,
        )
    )


def test_alert_tenant_must_exist(db: Connection):
    assert_rejected_by(db, alert_insert(new_id("ten")), "fk_platform_alerts_tenant_id_tenants")


def test_alert_objects_must_exist_in_the_same_tenant(db: Connection):
    shop, other = add_platform_shop(db), add_platform_shop(db)
    cases = {
        "channel_id": ((new_id("chn"), other.channel_id), "sales_channels"),
        "sku_id": ((new_id("sku"), add_sku(db, other.tenant_id)), "skus"),
        "platform_order_row_id": ((new_id("pfo"), add_order(db, other)), "platform_orders"),
    }
    for column, (values, referred) in cases.items():
        constraint = f"fk_platform_alerts_tenant_id_{referred}"
        for value in values:
            assert_rejected_by(db, alert_insert(shop.tenant_id, **{column: value}), constraint)


def test_alert_acknowledger_must_exist_in_the_same_tenant(db: Connection):
    shop, other = add_platform_shop(db), add_platform_shop(db)
    for credential_id in (new_id("cred"), other.credential_id):
        assert_rejected_by(
            db,
            alert_insert(shop.tenant_id, acked_credential_id=credential_id, acked_at=AT),
            "fk_platform_alerts_tenant_id_credentials",
        )


def test_alert_detail_must_be_an_object_when_present(db: Connection):
    tenant_id = add_tenant(db)
    db.execute(alert_insert(tenant_id, detail={}))
    db.execute(alert_insert(tenant_id, detail=None))
    for detail in ([], ["sku_1"], "detail", 1):
        assert_rejected_by(db, alert_insert(tenant_id, detail=detail), "ck_platform_alerts_detail")


def test_notify_count_is_nonnegative(db: Connection):
    tenant_id = add_tenant(db)
    assert_rejected_by(
        db, alert_insert(tenant_id, notify_count=-1), "ck_platform_alerts_notify_count"
    )
    db.execute(alert_insert(tenant_id, notify_count=0))


@pytest.mark.parametrize("column", ["acked_credential_id", "acked_at"])
def test_acked_credential_and_acked_at_are_both_set_or_both_empty(db: Connection, column: str):
    shop = add_platform_shop(db)
    value = shop.credential_id if column == "acked_credential_id" else AT
    assert_rejected_by(
        db, alert_insert(shop.tenant_id, **{column: value}), "ck_platform_alerts_acked"
    )


def test_alerts_are_indexed_by_acknowledgement_and_creation(db: Connection):
    assert indexes(db, "platform_alerts")["ix_platform_alerts_tenant_id_acked_at_created_at"] == [
        "tenant_id",
        "acked_at",
        "created_at",
    ]


# reconciliation_runs


def run_insert(tenant_id: str, **overrides: object) -> Executable:
    """默认是一次还没跑完的定时对账，没有发起的主体。"""
    values = {"run_id": new_id("rcn"), "tenant_id": tenant_id, "trigger": "scheduled"}
    return insert(ReconciliationRun).values(values | overrides)


def test_run_defaults(db: Connection):
    tenant_id = add_tenant(db)
    run_id = new_id("rcn")
    db.execute(run_insert(tenant_id, run_id=run_id))
    row = db.execute(select(ReconciliationRun).where(ReconciliationRun.run_id == run_id)).one()
    assert row.started_at is not None
    assert (row.credential_id, row.finished_at, row.discrepancy_count) == (None, None, None)


def test_run_id_is_the_primary_key(db: Connection):
    tenant_id = add_tenant(db)
    run_id = new_id("rcn")
    db.execute(run_insert(tenant_id, run_id=run_id))
    assert_rejected_by(db, run_insert(tenant_id, run_id=run_id), "PRIMARY")


def test_trigger_must_be_an_exact_known_value(db: Connection):
    tenant_id = add_tenant(db)
    credential_id = add_credential(db, tenant_id)
    for trigger in TRIGGERS:
        db.execute(run_insert(tenant_id, trigger=trigger, credential_id=credential_id))
    for trigger in ("Scheduled", "MANUAL", "auto", ""):
        assert_rejected_by(
            db, run_insert(tenant_id, trigger=trigger), "ck_reconciliation_runs_trigger"
        )


def test_a_manual_run_has_an_initiator(db: Connection):
    tenant_id = add_tenant(db)
    assert_rejected_by(
        db, run_insert(tenant_id, trigger="manual"), "ck_reconciliation_runs_manual_credential"
    )
    credential_id = add_credential(db, tenant_id)
    db.execute(run_insert(tenant_id, trigger="manual", credential_id=credential_id))
    # 定时对账有没有主体都可以。
    db.execute(run_insert(tenant_id, credential_id=credential_id))


@pytest.mark.parametrize("columns", [{"finished_at": LATER}, {"discrepancy_count": 0}])
def test_finished_at_and_discrepancy_count_are_both_set_or_both_empty(
    db: Connection, columns: dict
):
    tenant_id = add_tenant(db)
    assert_rejected_by(db, run_insert(tenant_id, **columns), "ck_reconciliation_runs_finished")


def test_discrepancy_count_is_nonnegative(db: Connection):
    tenant_id = add_tenant(db)
    assert_rejected_by(
        db,
        run_insert(tenant_id, finished_at=LATER, discrepancy_count=-1),
        "ck_reconciliation_runs_discrepancy_count",
    )
    for count in (0, 3):
        db.execute(run_insert(tenant_id, finished_at=LATER, discrepancy_count=count))


def test_run_tenant_and_initiator_must_exist_in_the_same_tenant(db: Connection):
    assert_rejected_by(db, run_insert(new_id("ten")), "fk_reconciliation_runs_tenant_id_tenants")
    tenant_id = add_tenant(db)
    for credential_id in (new_id("cred"), other_credential(db)):
        assert_rejected_by(
            db,
            run_insert(tenant_id, trigger="manual", credential_id=credential_id),
            "fk_reconciliation_runs_tenant_id_credentials",
        )


# platform_ops_log


def log_insert(shop: Shop, **overrides: object) -> Executable:
    """默认是一次设上写入暂停，没有操作者与请求。"""
    values = {
        "tenant_id": shop.tenant_id,
        "action": "write_pause_set",
        "object_type": "tenant",
        "object_id": shop.tenant_id,
        "before": {"paused": False},
        "after": {"paused": True},
        "reason": "platform outage",
        "credential_id": shop.credential_id,
    }
    return insert(PlatformOpsLog).values(values | overrides)


def test_log_ids_increase_and_created_at_defaults(db: Connection):
    shop = add_platform_shop(db)
    first = db.execute(log_insert(shop)).inserted_primary_key[0]
    second = db.execute(log_insert(shop)).inserted_primary_key[0]
    assert isinstance(first, int) and second > first
    row = db.execute(select(PlatformOpsLog).where(PlatformOpsLog.id == first)).one()
    assert row.created_at is not None
    assert (row.actor_ref, row.actor_role, row.request_id) == (None, None, None)


def test_every_action_of_the_contract_table_is_accepted(db: Connection):
    assert len(OPS_ACTIONS) == len(set(OPS_ACTIONS)) == 16
    shop = add_platform_shop(db)
    for action in OPS_ACTIONS:
        db.execute(log_insert(shop, action=action))


def test_actions_outside_the_contract_table_are_rejected(db: Connection):
    shop = add_platform_shop(db)
    for action in ("WRITE_PAUSE_SET", "Write_pause_set", "write_pause", "token_issued", ""):
        assert_rejected_by(db, log_insert(shop, action=action), "ck_platform_ops_log_action")


@pytest.mark.parametrize("column", ["before", "after"])
def test_before_and_after_must_be_objects_when_present(db: Connection, column: str):
    shop = add_platform_shop(db)
    for value in ({}, None):
        db.execute(log_insert(shop, **{column: value}))
    for value in ([], ["paused"], "paused", 1):
        assert_rejected_by(db, log_insert(shop, **{column: value}), f"ck_platform_ops_log_{column}")


def test_optional_log_columns_may_be_empty(db: Connection):
    shop = add_platform_shop(db)
    empty = dict.fromkeys(("object_type", "object_id", "before", "after", "reason", "request_id"))
    db.execute(log_insert(shop, **empty))
    db.execute(log_insert(shop, reason="r" * 500, object_id="o" * 64, request_id=new_id("req")))


def test_log_actor_ref_and_actor_role_are_both_set_or_both_empty(db: Connection):
    shop = add_platform_shop(db)
    for actor in HALF_ACTORS:
        assert_rejected_by(db, log_insert(shop, **actor), "ck_platform_ops_log_actor")
    db.execute(log_insert(shop, request_id=new_id("req"), **ACTOR))


def test_log_credential_is_required_and_must_exist_in_the_same_tenant(db: Connection):
    shop, other = add_platform_shop(db), add_platform_shop(db)
    assert_rejected_by(
        db, log_insert(shop, credential_id=None), "Column 'credential_id' cannot be null"
    )
    for credential_id in (new_id("cred"), other.credential_id):
        assert_rejected_by(
            db,
            log_insert(shop, credential_id=credential_id),
            "fk_platform_ops_log_tenant_id_credentials",
        )


def test_log_is_indexed_by_tenant_and_id(db: Connection):
    assert indexes(db, "platform_ops_log")["ix_platform_ops_log_tenant_id_id"] == [
        "tenant_id",
        "id",
    ]


# production 库上的迁移往返

PLATFORM_CHANNEL = {"kind": PLATFORM, "platform": SHOPEE}


def remove_tenants(engine: Engine, tenant_ids: list[str]) -> None:
    """删掉这些租户与它们的入口、池、地点、连接与写入暂停行（按外键先后）。"""
    if not tenant_ids:
        return
    with engine.begin() as conn:
        for model in (
            PlatformConnection,
            PlatformWritePause,
            SalesChannel,
            StockPool,
            Location,
        ):
            conn.execute(delete(model).where(model.tenant_id.in_(tenant_ids)))
        conn.execute(delete(Tenant).where(Tenant.tenant_id.in_(tenant_ids)))


def test_downgrade_to_0016_drops_the_ops_tables_and_upgrade_backfills_connections(
    production_engine: Engine,
):
    migrate_production(command.downgrade, "0016", PRODUCTION)
    tenant_ids: list[str] = []
    try:
        try:
            assert alembic_version(production_engine) == "0016"
            for table in OPS_TABLES:
                assert not has_table(production_engine, table)
            assert has_table(production_engine, "platform_events")
            # 0016 上已有的 platform 入口与 api 入口，各在自己的租户里。
            with production_engine.begin() as conn:
                platform_tenant, api_tenant = add_tenant(conn), add_tenant(conn)
                tenant_ids += [platform_tenant, api_tenant]
                channel_id = new_id("chn")
                conn.execute(
                    channel_insert(
                        platform_tenant,
                        add_pool(conn, platform_tenant),
                        channel_id=channel_id,
                        platform_shop_id=new_id("shop"),
                        **PLATFORM_CHANNEL,
                    )
                )
                conn.execute(channel_insert(api_tenant, add_pool(conn, api_tenant)))
        finally:
            migrate_production(command.upgrade, "head", PRODUCTION)
        assert alembic_version(production_engine) == head_revision()
        for table in OPS_TABLES:
            assert has_table(production_engine, table)

        # 升回时补建：platform 入口有默认的连接行，它的租户有一行没设上的写入暂停。
        with production_engine.connect() as conn:
            connections = conn.execute(
                select(PlatformConnection).where(PlatformConnection.tenant_id.in_(tenant_ids))
            ).all()
            pauses = conn.execute(
                select(PlatformWritePause).where(PlatformWritePause.tenant_id.in_(tenant_ids))
            ).all()
        [connection] = connections
        assert (connection.tenant_id, connection.channel_id) == (platform_tenant, channel_id)
        assert (connection.mode, connection.status) == ("manual", "not_connected")
        assert connection.app_profile == "self_seller"
        assert connection.capabilities == {}
        assert connection.mode_changed_at is not None
        [pause] = pauses
        assert (pause.tenant_id, pause.paused, pause.set_at, pause.cleared_at) == (
            platform_tenant,
            False,
            None,
            None,
        )
    finally:
        remove_tenants(production_engine, tenant_ids)
