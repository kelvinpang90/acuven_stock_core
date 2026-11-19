import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, insert, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.sql.expression import Executable

from alembic import command
from app.db import make_engine
from app.domain.environment import DEMO, PRODUCTION
from app.models.catalog import Product, Sku, Tenant
from app.models.instance import CoreInstance
from app.models.inventory import LedgerEntry, Location, SalesChannel, StockBalance, StockPool
from app.models.orders import OrderDocument, OrderLine
from app.models.service_auth import Credential

if TYPE_CHECKING:
    from tests.integration.test_api_auth import Caller

# 只指向 CI 的一次性 MySQL 服务容器；本地没设时，用到下面夹具的测试跳过。
TEST_DATABASE_URL = os.environ.get("CORE_TEST_DATABASE_URL", "")
# 同一容器里的第二个库，标记为 production（契约 6.1）。
TEST_PRODUCTION_DATABASE_URL = os.environ.get("CORE_TEST_PRODUCTION_DATABASE_URL", "")
ROOT = Path(__file__).resolve().parents[2]


def migrate(
    step: Callable[[Config, str], object], revision: str, database_url: str, core_env: str | None
) -> None:
    """以给定的 CORE_ENV（None 表示没有设）对库执行 command.upgrade 或 command.downgrade。

    0006 起迁移要求 CORE_ENV，并在库里已有实例标记时先核对它（契约 6.1）。
    调用之前，测试对这个库开着的连接与事务都要先结束，否则 DDL 等元数据锁直到超时。
    """
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    with pytest.MonkeyPatch.context() as env:
        # alembic/env.py 只从 CORE_DATABASE_URL 读连接串。
        env.setenv("CORE_DATABASE_URL", database_url)
        if core_env is None:
            env.delenv("CORE_ENV", raising=False)
        else:
            env.setenv("CORE_ENV", core_env)
        step(config, revision)


@pytest.fixture(scope="session")
def migrated_engine() -> Iterator[Engine]:
    """需要表结构的测试共用：会话开始时以 demo 升级到 head，结束时降到 base。

    结束时降到底，CI 之后的迁移往返才能照常从空库开始。
    """
    if not TEST_DATABASE_URL:
        pytest.skip("CORE_TEST_DATABASE_URL not set (CI only)")
    with pytest.MonkeyPatch.context() as env:
        # 会话期间照旧留着 CORE_DATABASE_URL；CORE_ENV 只在升降迁移时设。
        env.setenv("CORE_DATABASE_URL", TEST_DATABASE_URL)
        migrate(command.upgrade, "head", TEST_DATABASE_URL, DEMO)
        engine = make_engine(TEST_DATABASE_URL)
        try:
            yield engine
        finally:
            engine.dispose()
            migrate(command.downgrade, "base", TEST_DATABASE_URL, DEMO)


@pytest.fixture(scope="session")
def production_engine() -> Iterator[Engine]:
    """标记为 production 的第二个库：会话开始时以 production 升级到 head，结束时降到 base。

    CI 设了 CORE_TEST_PRODUCTION_DATABASE_URL；本地没设时，用到它的测试跳过。
    """
    if not TEST_PRODUCTION_DATABASE_URL:
        pytest.skip("CORE_TEST_PRODUCTION_DATABASE_URL not set (CI only)")
    migrate(command.upgrade, "head", TEST_PRODUCTION_DATABASE_URL, PRODUCTION)
    engine = make_engine(TEST_PRODUCTION_DATABASE_URL)
    try:
        yield engine
    finally:
        engine.dispose()
        migrate(command.downgrade, "base", TEST_PRODUCTION_DATABASE_URL, PRODUCTION)


@contextmanager
def instance_marked(engine: Engine, core_env: str) -> Iterator[None]:
    """临时把测试库 core_instance 的 core_env 改成给定值并提交，退出时改回 demo。

    启动核对（契约 6.1）读的是已提交的标记，所以要提交；用完立刻退出，
    同一测试里的 demo 进程（命令行、应用）才能照常通过核对。
    """
    with engine.begin() as conn:
        conn.execute(update(CoreInstance).values(core_env=core_env))
    try:
        yield
    finally:
        with engine.begin() as conn:
            conn.execute(update(CoreInstance).values(core_env=DEMO))


# 下面是 schema 测试共用的：每个测试在一个事务里跑、结束时回滚；
# 被拒的语句包在保存点里，并断言拒绝它的正是预期的那条约束，而不是碰巧别的错误。


@pytest.fixture
def db(migrated_engine: Engine) -> Iterator[Connection]:
    with migrated_engine.connect() as conn:
        conn.begin()
        yield conn
        conn.rollback()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:26].upper()}"


def assert_rejected_by(db: Connection, statement: Executable, constraint: str) -> None:
    with pytest.raises(DBAPIError) as caught, db.begin_nested():
        db.execute(statement)
    assert constraint in str(caught.value.orig)


def credential_insert(tenant_id: str, **overrides: object) -> Executable:
    values = {
        "credential_id": new_id("cred"),
        "tenant_id": tenant_id,
        "name": "Shop",
        "scopes": ["stock.read", "stock.write"],
        "status": "active",
    }
    return insert(Credential).values(values | overrides)


def add_credential(db: Connection, tenant_id: str, **overrides: object) -> str:
    """幂等记录与流水的 credential_id 要指向同租户的凭据；同一租户可以建多个。"""
    credential_id = new_id("cred")
    db.execute(credential_insert(tenant_id, credential_id=credential_id, **overrides))
    return credential_id


def assert_rejected_by_one_of(db: Connection, statement: Executable, *constraints: str) -> None:
    with pytest.raises(DBAPIError) as caught, db.begin_nested():
        db.execute(statement)
    assert any(constraint in str(caught.value.orig) for constraint in constraints)


# 库存侧各表的造数（库存表结构与库存管理表结构两组测试共用）。

# (tenant_id, pool_id, sku_id)：一个余额行，也是流水指向它的复合外键。
Balance = tuple[str, str, str]

OCCURRED_AT = datetime(2026, 11, 12, 8, 30)
WENT_LIVE_AT = datetime(2026, 11, 15, 8, 0)


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


def went_live(credential_id: str) -> dict[str, object]:
    """池切 live 时一并记下的四列（契约 6.3）；status 由调用方另给 live。"""
    return {
        "went_live_at": WENT_LIVE_AT,
        "went_live_credential_id": credential_id,
        "went_live_actor_ref": "admin_1",
        "went_live_request_id": new_id("req"),
    }


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


# 订单单据命令（发货、退款回库、退货验收）的接口测试共用的请求与观察。
# 要用的其他测试模块的辅助函数在函数里导入：那些模块在顶层就导入本文件，顶层互相导入会循环。

DISPATCHES_CREATE = "dispatches.create"
REFUND_RESTOCKS_CREATE = "refund_restocks.create"
DISPATCHES = "dispatches"
REFUND_RESTOCKS = "refund-restocks"
# 路径、操作类型、引用字段、details.ref_type、单据类型、流水类型、document_id 前缀。
KINDS = {
    DISPATCHES: (DISPATCHES_CREATE, "shipment_ref", "shipment", "dispatch", "DISPATCH", "shp_"),
    REFUND_RESTOCKS: (
        REFUND_RESTOCKS_CREATE,
        "refund_ref",
        "refund",
        "refund_restock",
        "REFUND_RESTOCK",
        "rfr_",
    ),
}
# 三种单据的表示里，除本类型的引用字段之外都有的键。
COMMON_KEYS = {
    "document_id",
    "document_type",
    "order_ref",
    "reservation_id",
    "pool_id",
    "reason_code",
    "note",
    "occurred_at",
    "created_at",
    "lines",
}


def new_key() -> str:
    return f"shop:fulfil:{uuid4().hex}"


def new_document_ref() -> str:
    return f"doc-{uuid4().hex}"


def qline(line_ref: str, quantity: object) -> dict:
    return {"line_ref": line_ref, "quantity": quantity}


def body_of(kind: str, lines: list[dict], ref: str | None = None, **extra: object) -> dict:
    """本类型的正文：引用字段默认取一个新引用。"""
    ref_field = KINDS[kind][1]
    return {ref_field: ref or new_document_ref(), "lines": lines} | extra


def post(
    client: TestClient,
    caller: "Caller",
    kind: str,
    order_ref: str,
    body: object,
    key: str | None = None,
    actor: dict | None = None,
):
    headers = caller.auth | {
        "Idempotency-Key": key or new_key(),
        "X-Request-ID": f"fulfil-test-{uuid4().hex}",
    }
    path = f"/v1/orders/{order_ref}/{kind}"
    return client.post(path, json=body, headers=headers | (actor or {}))


def committed_order(client: TestClient, engine: Engine, caller: "Caller", quantities: list[int]):
    """每行一个新 SKU、各备 5 件，预留并确认；返回预留的表示与按行的 SKU。"""
    from tests.integration.test_reservation_release_api import reserved_order
    from tests.integration.test_reservations_api import data_of
    from tests.integration.test_sale_commits_api import commit, commit_body

    created, skus = reserved_order(client, engine, caller, quantities)
    data_of(commit(client, caller, created["order_ref"], commit_body(created)), 200)
    return created, skus


def documents(engine: Engine, caller: "Caller") -> list[dict]:
    from tests.integration.test_reservations_api import rows_of

    return sorted(
        rows_of(engine, OrderDocument, caller), key=lambda document: document["document_seq"]
    )


def order_lines(engine: Engine, caller: "Caller") -> dict[str, dict]:
    from tests.integration.test_reservations_api import rows_of

    return {row["line_ref"]: row for row in rows_of(engine, OrderLine, caller)}


def snapshot(engine: Engine, caller: "Caller") -> tuple:
    """拒绝前后要相同的：流水、单据与订单行。"""
    from tests.integration.test_stock_service import entries

    return entries(engine, caller), documents(engine, caller), order_lines(engine, caller)
