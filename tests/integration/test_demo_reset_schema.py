"""实例标记、Demo 重置表与生产库的流水类型约束在真 MySQL 上生效（契约 6.1）。

demo 库用 conftest 的 db 夹具（每个测试一个事务、结束时回滚）。production 库是第二个库
（production_engine）：对它升降迁移之前，测试对它开着的连接与事务都先结束，否则 DDL 等元数据锁；
在它上面插入的流水一律回滚，否则升回 head 时加检查约束会校验已有行而失败。
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine, delete, insert, inspect, select, text, update
from sqlalchemy.sql.expression import Executable

from alembic import command
from app.domain.environment import DEMO, PRODUCTION, InstanceMismatch
from app.models.instance import CoreInstance
from app.models.inventory import DemoReset
from tests.integration.conftest import (
    ROOT,
    TEST_PRODUCTION_DATABASE_URL,
    add_balance,
    add_channel,
    add_credential,
    add_entry,
    add_pool,
    add_tenant,
    assert_rejected_by,
    entry_insert,
    migrate,
    new_id,
)

BUSINESS_DATE = date(2026, 11, 16)
REQUEST_HASH = "0" * 64
ENTRY_TYPE_CHECK = "ck_ledger_entries_entry_type"


def marker_rows(conn: Connection) -> list[tuple]:
    rows = conn.execute(select(CoreInstance.singleton, CoreInstance.core_env))
    return [tuple(row) for row in rows]


# demo 库上的实例标记


def test_demo_database_is_marked_demo_in_exactly_one_row(db: Connection):
    assert marker_rows(db) == [(1, DEMO)]


def test_marker_is_a_single_row_of_a_known_environment(db: Connection):
    second = insert(CoreInstance).values(singleton=1, core_env=DEMO)
    assert_rejected_by(db, second, "PRIMARY")
    for singleton in (0, 2):
        assert_rejected_by(
            db,
            insert(CoreInstance).values(singleton=singleton, core_env=DEMO),
            "ck_core_instance_singleton",
        )
        assert_rejected_by(
            db, update(CoreInstance).values(singleton=singleton), "ck_core_instance_singleton"
        )
    for core_env in ("staging", "Demo", "prod", ""):
        assert_rejected_by(
            db, update(CoreInstance).values(core_env=core_env), "ck_core_instance_core_env"
        )
    assert marker_rows(db) == [(1, DEMO)]


def test_demo_database_accepts_demo_reset_entries(db: Connection):
    balance = add_balance(db)
    add_entry(db, balance, 1, entry_type="DEMO_RESET")
    add_entry(db, balance, 2)


# Demo 重置的存放（契约 6.1）


@dataclass
class Shop:
    """一个租户的池、绑定它的销售入口与该租户的一个凭据。"""

    tenant_id: str
    pool_id: str
    channel_id: str
    credential_id: str


def add_shop(db: Connection, tenant_id: str | None = None) -> Shop:
    tenant_id = tenant_id or add_tenant(db)
    pool_id = add_pool(db, tenant_id)
    channel_id = add_channel(db, tenant_id, pool_id)
    return Shop(tenant_id, pool_id, channel_id, add_credential(db, tenant_id))


def reset_insert(shop: Shop, **overrides: object) -> Executable:
    """默认没有操作者与角色：重置不要求角色（契约第 10 节第 3 条）。"""
    values = {
        "reset_id": new_id("drs"),
        "tenant_id": shop.tenant_id,
        "channel_id": shop.channel_id,
        "pool_id": shop.pool_id,
        "business_date": BUSINESS_DATE,
        "request_hash": REQUEST_HASH,
        "result": {"reset_id": "drs_01", "skus": []},
        "credential_id": shop.credential_id,
        "request_id": new_id("req"),
    }
    return insert(DemoReset).values(values | overrides)


def test_a_pool_is_reset_at_most_once_per_business_date(db: Connection):
    shop = add_shop(db)
    db.execute(reset_insert(shop))
    assert_rejected_by(db, reset_insert(shop), "uq_demo_resets_tenant_id_pool_id_business_date")
    # 换一天，或同一租户的另一个池，都可以。
    db.execute(reset_insert(shop, business_date=date(2026, 11, 17)))
    db.execute(reset_insert(add_shop(db, shop.tenant_id)))


def test_reset_result_must_be_an_object(db: Connection):
    shop = add_shop(db)
    db.execute(reset_insert(shop, result={}))
    for result in ([], "result", 1):
        assert_rejected_by(
            db,
            reset_insert(shop, business_date=date(2026, 11, 18), result=result),
            "ck_demo_resets_result",
        )


def test_actor_ref_and_actor_role_are_both_set_or_both_empty(db: Connection):
    shop = add_shop(db)
    for actor in ({"actor_ref": "admin_1"}, {"actor_role": "stock_admin"}):
        assert_rejected_by(db, reset_insert(shop, **actor), "ck_demo_resets_actor")
    db.execute(reset_insert(shop))
    both = {"actor_ref": "admin_1", "actor_role": "stock_admin"}
    db.execute(reset_insert(shop, business_date=date(2026, 11, 17), **both))


def test_reset_channel_pool_and_credential_must_belong_to_the_same_tenant(db: Connection):
    shop, other = add_shop(db), add_shop(db)
    assert_rejected_by(
        db,
        reset_insert(shop, channel_id=other.channel_id),
        "fk_demo_resets_tenant_id_sales_channels",
    )
    assert_rejected_by(
        db, reset_insert(shop, pool_id=other.pool_id), "fk_demo_resets_tenant_id_stock_pools"
    )
    assert_rejected_by(
        db,
        reset_insert(shop, credential_id=other.credential_id),
        "fk_demo_resets_tenant_id_credentials",
    )


# production 库（契约 6.1 生产实例的防护、迁移前核对）


@contextmanager
def rolled_back(engine: Engine) -> Iterator[Connection]:
    """一个结束时回滚并归还的连接：之后对这个库升降迁移不会等元数据锁。"""
    with engine.connect() as conn:
        conn.begin()
        try:
            yield conn
        finally:
            conn.rollback()


def migrate_production(
    step: Callable[[Config, str], object], revision: str, core_env: str | None
) -> None:
    migrate(step, revision, TEST_PRODUCTION_DATABASE_URL, core_env)


def alembic_version(engine: Engine) -> str:
    with engine.connect() as conn:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


def has_table(engine: Engine, table: str) -> bool:
    return inspect(engine).has_table(table)


def head_revision() -> str:
    """迁移脚本目录的当前 head：以后再加迁移不用改这里的断言。"""
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return ScriptDirectory.from_config(config).get_current_head()


def assert_production_head(engine: Engine) -> None:
    assert alembic_version(engine) == head_revision()
    with engine.connect() as conn:
        assert marker_rows(conn) == [(1, PRODUCTION)]
    assert has_table(engine, "demo_resets")
    with rolled_back(engine) as conn:
        balance = add_balance(conn)
        add_entry(conn, balance, 1)
        demo_reset = entry_insert(balance, 2, entry_type="DEMO_RESET")
        assert_rejected_by(conn, demo_reset, ENTRY_TYPE_CHECK)


def test_production_database_is_marked_production(production_engine: Engine):
    with production_engine.connect() as conn:
        assert marker_rows(conn) == [(1, PRODUCTION)]


def test_production_database_rejects_demo_reset_entries(production_engine: Engine):
    with rolled_back(production_engine) as conn:
        balance = add_balance(conn)
        add_entry(conn, balance, 1)
        assert_rejected_by(
            conn, entry_insert(balance, 2, entry_type="DEMO_RESET"), ENTRY_TYPE_CHECK
        )
        add_entry(conn, balance, 2, entry_type="RECEIPT")


@pytest.mark.parametrize("core_env", [DEMO, "staging", "Production", "", None])
def test_migrating_with_another_or_invalid_core_env_fails_and_leaves_the_database(
    production_engine: Engine, core_env: str | None
):
    expected = InstanceMismatch if core_env == DEMO else ValueError
    with pytest.raises(expected):
        migrate_production(command.upgrade, "head", core_env)
    # 降级会改库，核对在执行任何迁移之前就拦下它。
    with pytest.raises(expected):
        migrate_production(command.downgrade, "0005", core_env)
    assert_production_head(production_engine)


def test_migrating_fails_when_the_marker_table_has_no_row(production_engine: Engine):
    with production_engine.begin() as conn:
        conn.execute(delete(CoreInstance))
    try:
        with pytest.raises(InstanceMismatch):
            migrate_production(command.upgrade, "head", PRODUCTION)
        assert alembic_version(production_engine) == head_revision()
    finally:
        with production_engine.begin() as conn:
            conn.execute(insert(CoreInstance).values(singleton=1, core_env=PRODUCTION))
    assert_production_head(production_engine)


def test_downgrade_restores_demo_reset_and_upgrade_requires_core_env(production_engine: Engine):
    migrate_production(command.downgrade, "0005", PRODUCTION)
    try:
        assert alembic_version(production_engine) == "0005"
        assert not has_table(production_engine, "core_instance")
        assert not has_table(production_engine, "demo_resets")
        # 0002 的原约束回来了。插入的流水回滚，升回 head 时加约束才不会因已有行失败。
        with rolled_back(production_engine) as conn:
            add_entry(conn, add_balance(conn), 1, entry_type="DEMO_RESET")
        # 没有标记时跳过迁移前核对，由 0006 自己先校验 CORE_ENV，不改库。
        with pytest.raises(ValueError):
            migrate_production(command.upgrade, "head", None)
        assert alembic_version(production_engine) == "0005"
        assert not has_table(production_engine, "core_instance")
        assert not has_table(production_engine, "demo_resets")
    finally:
        migrate_production(command.upgrade, "head", PRODUCTION)
    assert_production_head(production_engine)
