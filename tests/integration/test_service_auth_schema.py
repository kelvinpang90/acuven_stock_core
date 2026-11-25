"""服务凭据、令牌与安全事件各表的约束在真 MySQL 上生效（契约 7.2、第 10 节第 1 条与第 4 条）。

幂等记录与流水指向凭据的外键见 test_inventory_schema。
连接夹具 db 与 assert_rejected_by 见 conftest。
0012 的升降照 test_demo_reset_schema 的先例在 production 库上做，不动共享的 demo 库
（降到 0011 时先经过 0013 的降级）；
无论成败都在 finally 里升回 head，提交进去的行也在 finally 里删掉。
"""

from datetime import datetime

import pytest
from sqlalchemy import Connection, Engine, delete, insert, inspect, select
from sqlalchemy.sql.expression import Executable

from alembic import command
from app.domain.environment import PRODUCTION
from app.domain.service_auth import ADMIN, SERVICE, SYSTEM, generate_token, token_digest
from app.models.catalog import Tenant
from app.models.service_auth import EVENT_TYPES, Credential, CredentialToken, SecurityEvent
from tests.integration.conftest import add_credential, assert_rejected_by, credential_insert, new_id
from tests.integration.test_demo_reset_schema import (
    alembic_version,
    head_revision,
    migrate_production,
    rolled_back,
)
from tests.integration.test_inventory_schema import add_channel, add_pool, add_tenant

REVOKED_AT = datetime(2026, 11, 13, 9, 0)
SCOPES_CHECK = "ck_credentials_scopes"
KIND_CHECK = "ck_credentials_kind"
CHANNEL_CHECK = "ck_credentials_channel"


def token_insert(tenant_id: str, credential_id: str, **overrides: object) -> Executable:
    values = {
        "token_id": new_id("ctk"),
        "tenant_id": tenant_id,
        "credential_id": credential_id,
        "token_digest": token_digest(generate_token()),
    }
    return insert(CredentialToken).values(values | overrides)


def event_insert(**overrides: object) -> Executable:
    values = {"event_id": new_id("sev"), "event_type": "invalid_credential"}
    return insert(SecurityEvent).values(values | overrides)


# 凭据（契约第 10 节第 4 条）


def test_credential_channel_must_belong_to_the_same_tenant(db: Connection):
    tenant = add_tenant(db)
    other_tenant = add_tenant(db)
    foreign_channel = add_channel(db, other_tenant, add_pool(db, other_tenant))
    assert_rejected_by(
        db,
        credential_insert(tenant, channel_id=foreign_channel),
        "fk_credentials_tenant_id_sales_channels",
    )
    add_credential(db, tenant, channel_id=add_channel(db, tenant, add_pool(db, tenant)))
    # 运维凭据不带销售入口。
    add_credential(db, tenant, channel_id=None, scopes=["ops.read"])


def test_scopes_must_be_a_non_empty_array(db: Connection):
    tenant = add_tenant(db)
    add_credential(db, tenant, scopes=["catalog.read"])
    for scopes in [[], {"scope": "catalog.read"}, "catalog.read", 1]:
        assert_rejected_by(db, credential_insert(tenant, scopes=scopes), "ck_credentials_scopes")


def test_revoked_at_is_set_exactly_when_revoked(db: Connection):
    tenant = add_tenant(db)
    add_credential(db, tenant, status="revoked", revoked_at=REVOKED_AT)
    assert_rejected_by(db, credential_insert(tenant, status="revoked"), "ck_credentials_revoked_at")
    assert_rejected_by(
        db,
        credential_insert(tenant, status="active", revoked_at=REVOKED_AT),
        "ck_credentials_revoked_at",
    )


def test_credential_status_must_be_an_exact_known_value(db: Connection):
    tenant = add_tenant(db)
    for status in ["Active", "ACTIVE", "Revoked", "REVOKED", "inactive"]:
        assert_rejected_by(db, credential_insert(tenant, status=status), "ck_credentials_status")


# 操作主体的种类（契约第 10 节第 4 条）


def kind_of(conn: Connection, credential_id: str) -> str:
    statement = select(Credential.kind).where(Credential.credential_id == credential_id)
    return conn.execute(statement).scalar_one()


def test_kind_defaults_to_service(db: Connection):
    tenant = add_tenant(db)
    assert kind_of(db, add_credential(db, tenant)) == SERVICE


def test_kind_must_be_an_exact_known_value(db: Connection):
    tenant = add_tenant(db)
    for kind in ["Service", "ADMIN", "System", "operator", ""]:
        assert_rejected_by(db, credential_insert(tenant, kind=kind), KIND_CHECK)
        assert_rejected_by(db, credential_insert(tenant, kind=kind, scopes=[]), KIND_CHECK)


def test_service_scopes_must_be_a_non_empty_array(db: Connection):
    tenant = add_tenant(db)
    assert_rejected_by(db, credential_insert(tenant, kind=SERVICE, scopes=[]), SCOPES_CHECK)
    assert kind_of(db, add_credential(db, tenant, kind=SERVICE)) == SERVICE


@pytest.mark.parametrize("kind", [ADMIN, SYSTEM])
def test_admin_and_system_have_no_scopes_and_no_channel(db: Connection, kind: str):
    tenant = add_tenant(db)
    channel = add_channel(db, tenant, add_pool(db, tenant))
    assert kind_of(db, add_credential(db, tenant, kind=kind, scopes=[])) == kind
    for scopes in [["catalog.read"], ["ops.read"], {}, "catalog.read"]:
        assert_rejected_by(db, credential_insert(tenant, kind=kind, scopes=scopes), SCOPES_CHECK)
    assert_rejected_by(
        db, credential_insert(tenant, kind=kind, scopes=[], channel_id=channel), CHANNEL_CHECK
    )


# 令牌（契约第 10 节第 4 条）


def test_token_digest_is_unique_across_tenants(db: Connection):
    tenant = add_tenant(db)
    other_tenant = add_tenant(db)
    digest = token_digest(generate_token())
    db.execute(token_insert(tenant, add_credential(db, tenant), token_digest=digest))
    for owner in [tenant, other_tenant]:
        duplicate = token_insert(owner, add_credential(db, owner), token_digest=digest)
        assert_rejected_by(db, duplicate, "uq_credential_tokens_token_digest")


def test_token_digest_must_be_64_lowercase_hex_characters(db: Connection):
    tenant = add_tenant(db)
    credential = add_credential(db, tenant)
    lower = token_digest(generate_token())
    db.execute(token_insert(tenant, credential, token_digest="0" * 64))
    for digest in [lower.upper(), "A" * 64, "g" * 64, "0" * 63, "", "-" * 64]:
        assert_rejected_by(
            db,
            token_insert(tenant, credential, token_digest=digest),
            "ck_credential_tokens_token_digest",
        )


def test_token_cannot_belong_to_another_tenants_credential(db: Connection):
    tenant = add_tenant(db)
    foreign_credential = add_credential(db, add_tenant(db))
    assert_rejected_by(
        db,
        token_insert(tenant, foreign_credential),
        "fk_credential_tokens_tenant_id_credentials",
    )
    assert_rejected_by(
        db, token_insert(tenant, new_id("cred")), "fk_credential_tokens_tenant_id_credentials"
    )


# 安全事件（契约 7.2）


def test_event_type_must_be_one_of_the_known_types(db: Connection):
    for event_type in EVENT_TYPES:
        db.execute(event_insert(event_type=event_type))
    for event_type in ["INVALID_CREDENTIAL", "Forbidden", "token_rotated", ""]:
        assert_rejected_by(db, event_insert(event_type=event_type), "ck_security_events_event_type")


def test_event_with_a_credential_must_name_its_tenant(db: Connection):
    tenant = add_tenant(db)
    credential = add_credential(db, tenant)
    assert_rejected_by(
        db,
        event_insert(event_type="forbidden", credential_id=credential),
        "ck_security_events_credential_tenant",
    )
    db.execute(event_insert(event_type="forbidden", tenant_id=tenant, credential_id=credential))
    # 凭据无效时认不出租户与凭据。
    db.execute(event_insert())


def test_event_credential_must_belong_to_the_event_tenant(db: Connection):
    tenant = add_tenant(db)
    foreign_credential = add_credential(db, add_tenant(db))
    assert_rejected_by(
        db,
        event_insert(event_type="forbidden", tenant_id=tenant, credential_id=foreign_credential),
        "fk_security_events_tenant_id_credentials",
    )
    assert_rejected_by(
        db, event_insert(tenant_id=new_id("ten")), "fk_security_events_tenant_id_tenants"
    )


def test_event_occurred_at_defaults_to_database_time(db: Connection):
    event_id = new_id("sev")
    db.execute(event_insert(event_id=event_id))
    occurred_at = db.execute(
        select(SecurityEvent.occurred_at).where(SecurityEvent.event_id == event_id)
    ).scalar_one()
    assert occurred_at is not None


# 0012 的升降（production 库）


def has_kind(engine: Engine) -> bool:
    return "kind" in {column["name"] for column in inspect(engine).get_columns("credentials")}


def remove_committed(engine: Engine, rows: list[tuple[str, str]]) -> None:
    """删掉测试提交的 (租户, 凭据)：它们没有令牌，也没有别的行引用。"""
    with engine.begin() as conn:
        for tenant_id, credential_id in rows:
            conn.execute(delete(Credential).where(Credential.credential_id == credential_id))
            conn.execute(delete(Tenant).where(Tenant.tenant_id == tenant_id))


def test_downgrade_to_0011_and_back_keeps_existing_credentials_as_service(
    production_engine: Engine,
):
    committed: list[tuple[str, str]] = []
    try:
        migrate_production(command.downgrade, "0011", PRODUCTION)
        try:
            assert alembic_version(production_engine) == "0011"
            assert not has_kind(production_engine)
            # 0003 的原约束回来了：scopes 一律非空。
            with rolled_back(production_engine) as conn:
                tenant = add_tenant(conn)
                assert_rejected_by(conn, credential_insert(tenant, scopes=[]), SCOPES_CHECK)
            # 0011 上已有的行，升级后是 service。
            with production_engine.begin() as conn:
                tenant = add_tenant(conn)
                committed.append((tenant, add_credential(conn, tenant)))
        finally:
            migrate_production(command.upgrade, "head", PRODUCTION)
        assert alembic_version(production_engine) == head_revision()
        assert has_kind(production_engine)
        with production_engine.connect() as conn:
            assert kind_of(conn, committed[0][1]) == SERVICE
        with rolled_back(production_engine) as conn:
            tenant = add_tenant(conn)
            assert kind_of(conn, add_credential(conn, tenant, kind=ADMIN, scopes=[])) == ADMIN
            assert_rejected_by(conn, credential_insert(tenant, scopes=[]), SCOPES_CHECK)
    finally:
        remove_committed(production_engine, committed)


def test_downgrade_fails_without_changes_while_admin_or_system_principals_exist(
    production_engine: Engine,
):
    with production_engine.begin() as conn:
        tenant = add_tenant(conn)
        committed = [(tenant, add_credential(conn, tenant, kind=SYSTEM, scopes=[]))]
    try:
        # MySQL 的 DDL 不在事务里：从 head 降到 0011 时，0013 的降级先成功并提交版本 0012，
        # 0012 的降级再因 system 主体失败，所以版本停在 0012。
        with pytest.raises(RuntimeError):
            migrate_production(command.downgrade, "0011", PRODUCTION)
        assert alembic_version(production_engine) == "0012"
        assert has_kind(production_engine)
        with production_engine.connect() as conn:
            assert kind_of(conn, committed[0][1]) == SYSTEM
    finally:
        remove_committed(production_engine, committed)
        migrate_production(command.upgrade, "head", PRODUCTION)
