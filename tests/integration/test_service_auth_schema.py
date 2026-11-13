"""服务凭据、令牌与安全事件各表的约束在真 MySQL 上生效（契约 7.2、第 10 节第 1 条与第 4 条）。

幂等记录与流水指向凭据的外键见 test_inventory_schema。
连接夹具 db 与 assert_rejected_by 见 conftest。
"""

from datetime import datetime

from sqlalchemy import Connection, insert, select
from sqlalchemy.sql.expression import Executable

from app.domain.service_auth import generate_token, token_digest
from app.models.service_auth import EVENT_TYPES, CredentialToken, SecurityEvent
from tests.integration.conftest import add_credential, assert_rejected_by, credential_insert, new_id
from tests.integration.test_inventory_schema import add_channel, add_pool, add_tenant

REVOKED_AT = datetime(2026, 11, 13, 9, 0)


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
