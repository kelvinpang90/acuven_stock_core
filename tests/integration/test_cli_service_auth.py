"""运维命令行的凭据与令牌子命令（契约 7.2、第 10 节第 4 条）。

在真 MySQL 上跑：直接调用入口函数 main（同 test_cli_provisioning）。
租户到销售入口用命令行本身开通；已停用的租户与入口、已过去的截止时间由测试用自己的连接直接写库。
签发的令牌用一个挂了鉴权的测试路由验证；安全事件按目标凭据查。
"""

import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest
from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import DateTime, Engine, func, insert, literal_column, select, update
from sqlalchemy.exc import OperationalError

from app.api.auth import authorize, get_access
from app.api.errors import ok
from app.config import Settings
from app.domain.service_auth import (
    CATALOG_READ,
    DEMO_RESET,
    OPS_READ,
    ORDERS_WRITE,
    STOCK_READ,
    generate_token,
    is_token_format,
    token_digest,
)
from app.main import create_app
from app.models.catalog import Tenant
from app.models.inventory import SalesChannel
from app.models.service_auth import Credential, CredentialToken, SecurityEvent
from app.services import security_events
from app.services.security_events import SecurityEventRecord, record_security_event
from tests.integration.conftest import TEST_DATABASE_URL, new_id
from tests.integration.test_cli_provisioning import Provisioned, Result, create, provision, run_cli

ACTOR = "ops-7"
REASON = "demo shop launch"
OPERATOR = ("--actor", ACTOR, "--reason", REASON)
SCOPES = (CATALOG_READ, STOCK_READ)
LOGGER = "app.services.security_events"
DB_NOW = literal_column("UTC_TIMESTAMP(6)", DateTime())
A_SECOND_AGO = literal_column("UTC_TIMESTAMP(6) - INTERVAL 1 SECOND")


# --- 夹具 ---


@pytest.fixture
def engine(migrated_engine: Engine) -> Engine:
    return migrated_engine


def cli_for(env: str) -> Callable[..., Result]:
    settings = Settings(env=env, database_url=TEST_DATABASE_URL)
    return lambda *argv: run_cli(settings, *argv)


@pytest.fixture
def cli(migrated_engine: Engine) -> Callable[..., Result]:
    return cli_for("demo")


@pytest.fixture
def production_cli(migrated_engine: Engine) -> Callable[..., Result]:
    return cli_for("production")


@pytest.fixture
def shop(cli) -> Provisioned:
    return provision(cli)


@pytest.fixture
def client(migrated_engine: Engine) -> Iterator[TestClient]:
    """挂一个要求鉴权（catalog.read）的测试路由的应用。"""
    app = create_app(Settings(env="demo", git_sha="c" * 40, database_url=TEST_DATABASE_URL))

    @app.get("/test/whoami", dependencies=[Depends(authorize(CATALOG_READ))])
    def whoami(request: Request) -> JSONResponse:
        return ok(request, {"credential_id": get_access(request).principal.credential_id})

    try:
        yield TestClient(app)
    finally:
        if app.state.engine is not None:
            app.state.engine.dispose()


def authenticates(client: TestClient, token: str) -> bool:
    response = client.get("/test/whoami", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code in (200, 401), response.text
    return response.status_code == 200


# --- 命令行参数 ---


def issue_argv(
    tenant_id: str,
    *,
    scopes: tuple[str, ...] = SCOPES,
    channel: str | None = None,
    name: str = "Demo shop",
    operator: tuple[str, ...] = OPERATOR,
) -> list[str]:
    argv = ["credential", "issue", "--tenant", tenant_id, "--name", name, *operator]
    for scope in scopes:
        argv += ["--scope", scope]
    if channel is not None:
        argv += ["--channel", channel]
    return argv


def rotate_argv(credential_id: str, hours: str, operator: tuple[str, ...] = OPERATOR) -> list[str]:
    # 写成 --overlap-hours=-1 的形式：负数不会被 argparse 当成选项。
    argv = ["credential", "rotate", "--credential", credential_id, f"--overlap-hours={hours}"]
    return argv + list(operator)


def revoke_token_argv(token_id: str) -> list[str]:
    return ["token", "revoke", "--token", token_id, *OPERATOR]


def revoke_credential_argv(credential_id: str) -> list[str]:
    return ["credential", "revoke", "--credential", credential_id, *OPERATOR]


def issue(cli, shop: Provisioned, **kwargs) -> dict:
    return cli(*issue_argv(shop.tenant_id, channel=shop.channel_id, **kwargs)).json()


# --- 读库 ---


def rows(engine: Engine, model, *where) -> list[dict]:
    with engine.connect() as conn:
        statement = select(*model.__table__.c).where(*where)
        return [dict(row) for row in conn.execute(statement).mappings()]


def credential_row(engine: Engine, credential_id: str) -> dict:
    [row] = rows(engine, Credential, Credential.credential_id == credential_id)
    return row


def token_rows(engine: Engine, credential_id: str) -> dict[str, dict]:
    tokens = rows(engine, CredentialToken, CredentialToken.credential_id == credential_id)
    return {token["token_id"]: token for token in tokens}


def events_of(engine: Engine, credential_id: str) -> list[dict]:
    with engine.connect() as conn:
        statement = (
            select(*SecurityEvent.__table__.c)
            .where(SecurityEvent.credential_id == credential_id)
            .order_by(SecurityEvent.occurred_at, SecurityEvent.event_id)
        )
        return [dict(row) for row in conn.execute(statement).mappings()]


def counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as conn:
        return {
            model.__tablename__: conn.execute(select(func.count()).select_from(model)).scalar_one()
            for model in (Credential, CredentialToken, SecurityEvent)
        }


def db_now(engine: Engine) -> datetime:
    with engine.connect() as conn:
        return conn.execute(select(DB_NOW)).scalar_one()


def utc(value: datetime) -> str:
    return value.isoformat(timespec="microseconds") + "Z"


def utc_or_none(value: datetime | None) -> str | None:
    return None if value is None else utc(value)


def assert_operator_event(event: dict, event_type: str, tenant_id: str, details: dict) -> None:
    """命令行的事件：操作者记 --actor，没有请求，所以请求 ID、方法、路径与 User-Agent 为空。"""
    assert event["event_type"] == event_type
    assert event["tenant_id"] == tenant_id
    assert event["actor_ref"] == ACTOR
    assert event["details"] == details
    for column in ("request_id", "http_method", "path", "user_agent"):
        assert event[column] is None


# --- 签发 ---


def test_issue_prints_the_token_once_and_stores_only_its_digest(cli, engine, client, shop):
    output = issue(cli, shop, name="  Demo shop ")
    token, credential_id, token_id = output["token"], output["credential_id"], output["token_id"]
    assert is_token_format(token)
    assert credential_id.startswith("cred_")
    assert token_id.startswith("ctk_")
    assert output == {
        "credential_id": credential_id,
        "tenant_id": shop.tenant_id,
        "channel_id": shop.channel_id,
        "name": "Demo shop",
        "scopes": list(SCOPES),
        "status": "active",
        "token_id": token_id,
        "token": token,
    }

    credential = credential_row(engine, credential_id)
    assert credential["channel_id"] == shop.channel_id
    assert credential["scopes"] == list(SCOPES)
    assert credential["status"] == "active"
    assert credential["revoked_at"] is None
    [stored] = token_rows(engine, credential_id).values()
    assert stored["token_id"] == token_id
    assert stored["token_digest"] == token_digest(token)
    assert stored["expires_at"] is None
    assert stored["revoked_at"] is None
    stored_text = json.dumps([credential, stored], default=str)
    assert token not in stored_text
    assert token[4:] not in stored_text

    assert authenticates(client, token)

    [event] = events_of(engine, credential_id)
    details = {"reason": REASON, "token_id": token_id}
    assert_operator_event(event, "credential_issued", shop.tenant_id, details)
    recorded = json.dumps(event, default=str)
    assert token not in recorded
    assert token_digest(token) not in recorded


def test_an_ops_credential_has_no_channel(cli, engine, shop):
    output = cli(*issue_argv(shop.tenant_id, scopes=(OPS_READ,))).json()
    assert output["channel_id"] is None
    assert credential_row(engine, output["credential_id"])["scopes"] == [OPS_READ]


@dataclass
class World:
    mine: Provisioned
    other: Provisioned
    inactive_tenant_id: str
    inactive_channel_id: str


@pytest.fixture
def world(cli, engine) -> World:
    mine, other = provision(cli), provision(cli)
    spare = cli(*create("pool", "Spare", tenant=mine.tenant_id, location=mine.location_id))
    inactive_tenant_id, inactive_channel_id = new_id("ten"), new_id("chan")
    with engine.begin() as db:
        db.execute(
            insert(Tenant).values(tenant_id=inactive_tenant_id, name="Gone", status="inactive")
        )
        db.execute(
            insert(SalesChannel).values(
                channel_id=inactive_channel_id,
                tenant_id=mine.tenant_id,
                pool_id=spare.json()["pool_id"],
                name="Closed",
                status="inactive",
            )
        )
    return World(mine, other, inactive_tenant_id, inactive_channel_id)


def rejected_issues(w: World) -> dict[str, list[str]]:
    tenant, channel = w.mine.tenant_id, w.mine.channel_id

    def by(actor: str = ACTOR, reason: str = REASON) -> list[str]:
        return issue_argv(tenant, operator=("--actor", actor, "--reason", reason))

    return {
        "unknown scope": issue_argv(tenant, scopes=(CATALOG_READ, "stock.admin"), channel=channel),
        "scope in the wrong case": issue_argv(tenant, scopes=("Catalog.read",), channel=channel),
        "scope twice": issue_argv(tenant, scopes=(STOCK_READ, STOCK_READ), channel=channel),
        "no scope": issue_argv(tenant, scopes=(), channel=channel),
        "ops.read with another scope": issue_argv(tenant, scopes=(OPS_READ, STOCK_READ)),
        "ops.read with a channel": issue_argv(tenant, scopes=(OPS_READ,), channel=channel),
        "channel of another tenant": issue_argv(tenant, channel=w.other.channel_id),
        "inactive channel": issue_argv(tenant, channel=w.inactive_channel_id),
        "unknown channel": issue_argv(tenant, channel=new_id("chan")),
        "unknown tenant": issue_argv(new_id("ten")),
        "inactive tenant": issue_argv(w.inactive_tenant_id),
        "missing actor": issue_argv(tenant, operator=("--reason", REASON)),
        "missing reason": issue_argv(tenant, operator=("--actor", ACTOR)),
        "actor with a space": by(actor="ops 7"),
        "actor with an at sign": by(actor="ops@7"),
        "actor over 128": by(actor="a" * 129),
        "blank reason": by(reason="   "),
        "reason over 500": by(reason="r" * 501),
        "reason with newline": by(reason="a\nb"),
        "empty name": issue_argv(tenant, name=" "),
        "name with newline": issue_argv(tenant, name="Demo\nshop"),
    }


REJECTED_ISSUES = [
    "unknown scope",
    "scope in the wrong case",
    "scope twice",
    "no scope",
    "ops.read with another scope",
    "ops.read with a channel",
    "channel of another tenant",
    "inactive channel",
    "unknown channel",
    "unknown tenant",
    "inactive tenant",
    "missing actor",
    "missing reason",
    "actor with a space",
    "actor with an at sign",
    "actor over 128",
    "blank reason",
    "reason over 500",
    "reason with newline",
    "empty name",
    "name with newline",
]


@pytest.mark.parametrize("case", REJECTED_ISSUES)
def test_rejected_issues_exit_2_and_write_no_credential_token_or_event(case, cli, world, engine):
    commands = rejected_issues(world)
    assert set(commands) == set(REJECTED_ISSUES)
    before = counts(engine)
    result = cli(*commands[case])
    assert result.code == 2
    result.one_error_line()
    assert counts(engine) == before


def test_issue_reasons_of_exactly_500_characters_are_accepted(cli, engine, shop):
    operator = ("--actor", "a" * 128, "--reason", " " + "r" * 500 + " ")
    output = issue(cli, shop, operator=operator)
    [event] = events_of(engine, output["credential_id"])
    assert event["actor_ref"] == "a" * 128
    assert event["details"]["reason"] == "r" * 500


def test_demo_reset_is_refused_on_production_only(cli, production_cli, engine, shop):
    scopes = (DEMO_RESET, ORDERS_WRITE)
    before = counts(engine)
    refused = production_cli(*issue_argv(shop.tenant_id, scopes=scopes, channel=shop.channel_id))
    assert refused.code == 2
    refused.one_error_line()
    assert counts(engine) == before

    # 生产实例上不带 demo.reset 照常签发；Demo 实例上可以带。
    argv = issue_argv(shop.tenant_id, scopes=(ORDERS_WRITE,), channel=shop.channel_id)
    assert production_cli(*argv).json()["scopes"] == [ORDERS_WRITE]
    assert issue(cli, shop, scopes=scopes)["scopes"] == list(scopes)


# --- 轮换 ---


def test_rotation_follows_the_contract_example(cli, engine, client, shop):
    first = issue(cli, shop)
    credential_id, token_a, a_id = first["credential_id"], first["token"], first["token_id"]
    issued_a = token_rows(engine, credential_id)[a_id]

    # 在 T 以 24 小时轮换得 B：A 的截止时间为 T + 24h，B 没有截止时间。
    before = db_now(engine)
    second = cli(*rotate_argv(credential_id, "24")).json()
    after = db_now(engine)
    token_b, b_id = second["token"], second["token_id"]
    assert is_token_format(token_b)
    assert b_id.startswith("ctk_")
    tokens = token_rows(engine, credential_id)
    first_deadline = tokens[a_id]["expires_at"]
    rotated_at = first_deadline - timedelta(hours=24)
    assert before <= rotated_at <= after
    assert tokens[a_id]["updated_at"] > issued_a["updated_at"]
    assert tokens[b_id]["expires_at"] is None
    assert tokens[b_id]["token_digest"] == token_digest(token_b)
    expired = [{"token_id": a_id, "expires_at": utc(first_deadline)}]
    assert second == {
        "credential_id": credential_id,
        "token_id": b_id,
        "token": token_b,
        "expired_tokens": expired,
    }
    # 重叠期内新旧令牌都能用。
    assert authenticates(client, token_a)
    assert authenticates(client, token_b)
    event = events_of(engine, credential_id)[-1]
    details = {"reason": REASON, "token_id": b_id, "expired_tokens": expired}
    assert_operator_event(event, "token_issued", shop.tenant_id, details)
    assert token_b not in json.dumps(event, default=str)

    # 稍后以 0 小时轮换得 C：A 与 B 的截止时间都改为这次轮换时刻，即刻失效。
    previous = token_rows(engine, credential_id)
    third = cli(*rotate_argv(credential_id, "0")).json()
    token_c, c_id = third["token"], third["token_id"]
    tokens = token_rows(engine, credential_id)
    second_deadline = tokens[b_id]["expires_at"]
    assert tokens[a_id]["expires_at"] == second_deadline
    assert rotated_at < second_deadline < first_deadline
    for old in (a_id, b_id):
        assert tokens[old]["updated_at"] > previous[old]["updated_at"]
    assert tokens[c_id]["expires_at"] is None
    expired = [
        {"token_id": a_id, "expires_at": utc(second_deadline)},
        {"token_id": b_id, "expires_at": utc(second_deadline)},
    ]
    assert third["expired_tokens"] == expired
    assert not authenticates(client, token_a)
    assert not authenticates(client, token_b)
    assert authenticates(client, token_c)
    assert events_of(engine, credential_id)[-1]["details"] == {
        "reason": REASON,
        "token_id": c_id,
        "expired_tokens": expired,
    }


def test_an_old_token_stops_at_its_deadline_and_a_rotation_never_extends_it(
    cli, engine, client, shop
):
    first = issue(cli, shop)
    credential_id, token_a, a_id = first["credential_id"], first["token"], first["token_id"]
    second = cli(*rotate_argv(credential_id, "24")).json()
    token_b, b_id = second["token"], second["token_id"]

    # 截止时间到了：旧令牌 401，新令牌照常。
    with engine.begin() as db:
        statement = update(CredentialToken).where(CredentialToken.token_id == a_id)
        db.execute(statement.values(expires_at=A_SECOND_AGO))
    assert not authenticates(client, token_a)
    assert authenticates(client, token_b)

    # 以上限 168 小时再轮换：已到期的 A 不动，B 截止于轮换时刻后 7 天。
    passed = token_rows(engine, credential_id)[a_id]["expires_at"]
    before = db_now(engine)
    third = cli(*rotate_argv(credential_id, "168")).json()
    after = db_now(engine)
    tokens = token_rows(engine, credential_id)
    assert tokens[a_id]["expires_at"] == passed
    assert before + timedelta(days=7) <= tokens[b_id]["expires_at"] <= after + timedelta(days=7)
    expired = [{"token_id": b_id, "expires_at": utc(tokens[b_id]["expires_at"])}]
    assert third["expired_tokens"] == expired
    assert not authenticates(client, token_a)
    assert authenticates(client, token_b)
    assert authenticates(client, third["token"])


@pytest.mark.parametrize("hours", ["-1", "169", "1.5", "24.0", "abc", "", " 24", "２４"])
def test_rotation_rejects_bad_overlap_hours(cli, engine, shop, hours):
    credential_id = issue(cli, shop)["credential_id"]
    before = counts(engine), token_rows(engine, credential_id)
    result = cli(*rotate_argv(credential_id, hours))
    assert result.code == 2
    result.one_error_line()
    assert (counts(engine), token_rows(engine, credential_id)) == before


def rotation_target(case: str, cli, engine, shop: Provisioned) -> str:
    credential_id = issue(cli, shop)["credential_id"]
    if case == "revoked credential":
        cli(*revoke_credential_argv(credential_id)).json()
    elif case == "inactive tenant":
        with engine.begin() as db:
            statement = update(Tenant).where(Tenant.tenant_id == shop.tenant_id)
            db.execute(statement.values(status="inactive"))
    elif case == "unknown credential":
        return new_id("cred")
    return credential_id


@pytest.mark.parametrize(
    ("case", "operator"),
    [
        ("revoked credential", OPERATOR),
        ("inactive tenant", OPERATOR),
        ("unknown credential", OPERATOR),
        ("missing actor", ("--reason", REASON)),
        ("missing reason", ("--actor", ACTOR)),
        ("bad actor", ("--actor", "ops 7", "--reason", REASON)),
        ("bad reason", ("--actor", ACTOR, "--reason", "a\nb")),
    ],
)
def test_rejected_rotations_exit_2_and_change_nothing(case, operator, cli, engine, shop):
    credential_id = rotation_target(case, cli, engine, shop)
    before = counts(engine), token_rows(engine, credential_id)
    result = cli(*rotate_argv(credential_id, "24", operator))
    assert result.code == 2
    result.one_error_line()
    assert (counts(engine), token_rows(engine, credential_id)) == before


def test_production_refuses_to_rotate_a_demo_reset_credential(cli, production_cli, engine, shop):
    credential_id = issue(cli, shop, scopes=(DEMO_RESET, ORDERS_WRITE))["credential_id"]
    before = counts(engine), token_rows(engine, credential_id)
    refused = production_cli(*rotate_argv(credential_id, "24"))
    assert refused.code == 2
    refused.one_error_line()
    assert (counts(engine), token_rows(engine, credential_id)) == before
    # Demo 实例上同一个凭据可以轮换。
    assert cli(*rotate_argv(credential_id, "24")).json()["credential_id"] == credential_id


# --- 吊销 ---


def test_revoking_a_token_takes_effect_at_once_and_only_for_that_token(cli, engine, client, shop):
    first = issue(cli, shop)
    credential_id, token_a, a_id = first["credential_id"], first["token"], first["token_id"]
    second = cli(*rotate_argv(credential_id, "24")).json()
    token_b, b_id = second["token"], second["token_id"]
    before = token_rows(engine, credential_id)

    output = cli(*revoke_token_argv(a_id)).json()
    tokens = token_rows(engine, credential_id)
    assert tokens[a_id]["revoked_at"] is not None
    assert tokens[a_id]["updated_at"] > before[a_id]["updated_at"]
    assert tokens[b_id] == before[b_id]
    assert output == {
        "token_id": a_id,
        "credential_id": credential_id,
        "revoked_at": utc(tokens[a_id]["revoked_at"]),
    }
    assert not authenticates(client, token_a)
    assert authenticates(client, token_b)
    event = events_of(engine, credential_id)[-1]
    details = {"reason": REASON, "token_id": a_id}
    assert_operator_event(event, "token_revoked", shop.tenant_id, details)

    # 不可撤销：再吊销是错误，不多写事件。
    written = len(events_of(engine, credential_id))
    again = cli(*revoke_token_argv(a_id))
    assert again.code == 2
    again.one_error_line()
    assert len(events_of(engine, credential_id)) == written
    assert token_rows(engine, credential_id) == tokens

    # 已吊销的令牌不参与之后的轮换。
    third = cli(*rotate_argv(credential_id, "0")).json()
    assert [old["token_id"] for old in third["expired_tokens"]] == [b_id]
    assert token_rows(engine, credential_id)[a_id] == tokens[a_id]


def test_revoking_a_credential_stops_all_its_tokens(cli, engine, client, shop):
    first = issue(cli, shop)
    credential_id, token_a = first["credential_id"], first["token"]
    token_b = cli(*rotate_argv(credential_id, "24")).json()["token"]
    before_credential = credential_row(engine, credential_id)
    before_tokens = token_rows(engine, credential_id)

    output = cli(*revoke_credential_argv(credential_id)).json()
    credential = credential_row(engine, credential_id)
    assert credential["status"] == "revoked"
    assert credential["revoked_at"] is not None
    assert credential["updated_at"] > before_credential["updated_at"]
    assert output == {
        "credential_id": credential_id,
        "status": "revoked",
        "revoked_at": utc(credential["revoked_at"]),
    }
    # 令牌行不动：凭据已吊销时鉴权一律 401。
    assert token_rows(engine, credential_id) == before_tokens
    assert not authenticates(client, token_a)
    assert not authenticates(client, token_b)
    event = events_of(engine, credential_id)[-1]
    assert_operator_event(event, "credential_revoked", shop.tenant_id, {"reason": REASON})

    written = len(events_of(engine, credential_id))
    again = cli(*revoke_credential_argv(credential_id))
    assert again.code == 2
    again.one_error_line()
    assert len(events_of(engine, credential_id)) == written
    assert credential_row(engine, credential_id) == credential


def test_revoking_unknown_objects_exits_2_without_echoing_the_id(cli, engine):
    before = counts(engine)
    token_id, credential_id = new_id("ctk"), new_id("cred")
    for argv, unknown in (
        (revoke_token_argv(token_id), token_id),
        (revoke_credential_argv(credential_id), credential_id),
        (rotate_argv(credential_id, "24"), credential_id),
    ):
        result = cli(*argv)
        assert result.code == 2
        assert unknown not in result.one_error_line()
    assert counts(engine) == before


def test_token_revoke_with_the_token_itself_is_refused_without_echoing_it(
    cli, engine, client, shop
):
    token = issue(cli, shop)["token"]
    before = counts(engine)
    result = cli("token", "revoke", "--token", token, *OPERATOR)
    assert result.code == 2
    line = result.one_error_line()
    assert token not in line
    assert token[4:] not in line
    assert "ctk_" in line
    # 与令牌 ID 不存在是不同的报错。
    missing = cli(*revoke_token_argv(new_id("ctk")))
    assert missing.code == 2
    assert missing.one_error_line() != line
    assert counts(engine) == before
    assert authenticates(client, token)


# --- 列出 ---


def test_list_shows_the_tenants_credentials_and_tokens_without_digests(cli, engine, shop):
    other = provision(cli)
    shop_issue = issue(cli, shop)
    rotated = cli(*rotate_argv(shop_issue["credential_id"], "24")).json()
    ops = cli(*issue_argv(shop.tenant_id, scopes=(OPS_READ,))).json()
    retired = issue(cli, shop, scopes=(STOCK_READ,))
    cli(*revoke_credential_argv(retired["credential_id"])).json()
    foreign = issue(cli, other)
    before = counts(engine)

    result = cli("credential", "list", "--tenant", shop.tenant_id)
    listed = result.json()
    mine = [shop_issue["credential_id"], ops["credential_id"], retired["credential_id"]]
    expected = []
    for credential_id in sorted(mine):
        credential = credential_row(engine, credential_id)
        tokens = sorted(
            token_rows(engine, credential_id).values(),
            key=lambda token: (token["created_at"], token["token_id"]),
        )
        listed_tokens = [
            {
                "token_id": token["token_id"],
                "created_at": utc(token["created_at"]),
                "expires_at": utc_or_none(token["expires_at"]),
                "revoked_at": utc_or_none(token["revoked_at"]),
            }
            for token in tokens
        ]
        expected.append(
            {
                "credential_id": credential_id,
                "name": credential["name"],
                "channel_id": credential["channel_id"],
                "scopes": credential["scopes"],
                "status": credential["status"],
                "revoked_at": utc_or_none(credential["revoked_at"]),
                "tokens": listed_tokens,
            }
        )
    assert listed == {"tenant_id": shop.tenant_id, "credentials": expected}
    by_id = {credential["credential_id"]: credential for credential in listed["credentials"]}
    rotated_tokens = by_id[rotated["credential_id"]]["tokens"]
    assert [token["token_id"] for token in rotated_tokens] == [
        shop_issue["token_id"],
        rotated["token_id"],
    ]
    assert rotated_tokens[0]["expires_at"] == rotated["expired_tokens"][0]["expires_at"]
    assert by_id[retired["credential_id"]]["status"] == "revoked"
    assert by_id[ops["credential_id"]]["channel_id"] is None

    # 不含摘要与明文令牌，也不含别的租户的凭据；只读，不写事件。
    for output in (shop_issue, rotated, ops, retired):
        assert output["token"] not in result.out
        assert token_digest(output["token"]) not in result.out
    assert foreign["credential_id"] not in result.out
    assert foreign["token_id"] not in result.out
    assert counts(engine) == before


def test_list_for_an_unknown_tenant_exits_2(cli):
    result = cli("credential", "list", "--tenant", new_id("ten"))
    assert result.code == 2
    result.one_error_line()


# --- 保密与事件写入失败 ---


def test_no_subcommand_writes_a_token_to_stderr_or_the_log(cli, shop, caplog):
    caplog.set_level(logging.DEBUG)
    first = cli(*issue_argv(shop.tenant_id, channel=shop.channel_id))
    output = first.json()
    credential_id, token_id = output["credential_id"], output["token_id"]
    rotated = cli(*rotate_argv(credential_id, "24"))
    tokens = [output["token"], rotated.json()["token"]]
    results = [
        first,
        rotated,
        cli("credential", "list", "--tenant", shop.tenant_id),
        cli("token", "revoke", "--token", tokens[0], *OPERATOR),
        cli(*revoke_token_argv(token_id)),
        cli(*revoke_token_argv(token_id)),
        cli(*revoke_credential_argv(credential_id)),
        cli(*rotate_argv(credential_id, "24")),
        cli("credential", "issue", tokens[1]),
        cli("token", "revoke", tokens[1]),
    ]
    assert [result.code for result in results] == [0, 0, 0, 2, 0, 2, 0, 2, 2, 2]
    for token in tokens:
        for text in [result.err for result in results] + [caplog.text]:
            assert token not in text
            assert token[4:] not in text
        # 只在签发它的那一次输出里出现一次。
        assert sum(result.out.count(token) for result in results) == 1


def test_argument_errors_do_not_echo_the_values_passed(cli, shop):
    secret = generate_token()
    credential_id = issue(cli, shop)["credential_id"]
    for argv in (
        ["token", "revoke", secret],
        ["credential", "issue", "--tenant", shop.tenant_id, secret],
        ["credential", "list", "--tenant", shop.tenant_id, "--token", secret],
        rotate_argv(credential_id, secret),
        issue_argv(shop.tenant_id, scopes=(secret,)),
        issue_argv(shop.tenant_id, operator=("--actor", secret + "!", "--reason", REASON)),
        issue_argv(shop.tenant_id, operator=("--actor", ACTOR, "--reason", "x\n" + secret)),
    ):
        result = cli(*argv)
        assert result.code == 2
        line = result.one_error_line()
        assert secret not in line
        assert secret[4:] not in line


def broken_insert(connection, values):
    raise OperationalError("INSERT INTO security_events", {}, Exception("disk full"))


OPERATIONS = ["credential_issued", "token_issued", "token_revoked", "credential_revoked"]


@pytest.mark.parametrize("operation", OPERATIONS)
def test_a_failed_event_write_keeps_the_operation_and_warns_in_one_line(
    operation, cli, engine, shop, monkeypatch, caplog, capsys
):
    caplog.set_level(logging.ERROR, logger=LOGGER)
    logger = logging.getLogger(LOGGER)
    disabled_before = logger.disabled
    first = issue(cli, shop)
    credential_id, token_id = first["credential_id"], first["token_id"]
    argv = {
        "credential_issued": issue_argv(shop.tenant_id, channel=shop.channel_id),
        "token_issued": rotate_argv(credential_id, "24"),
        "token_revoked": revoke_token_argv(token_id),
        "credential_revoked": revoke_credential_argv(credential_id),
    }[operation]
    capsys.readouterr()
    caplog.clear()

    with monkeypatch.context() as patch:
        patch.setattr(security_events, "_insert", broken_insert)
        result = cli(*argv)

    # 两项都要：没配置时记录进 caplog；只关 propagate 时 caplog 收不到，但堆栈写进 sys.stderr。
    assert [r for r in caplog.records if r.name == LOGGER] == []
    assert capsys.readouterr().err == ""
    # 入口函数返回后日志配置已恢复：同一进程里的写入失败照常记日志。
    assert logger.disabled == disabled_before
    assert not record_security_event(SecurityEventRecord("token_revoked"), engine=None)
    assert any(r.name == LOGGER for r in caplog.records)

    # 操作照样生效、退出码 0，标准错误只有一行警告：带操作类型与目标 ID，没有堆栈与 SQL。
    assert result.code == 0
    payload = json.loads(result.out)
    assert result.err.count("\n") == 1
    assert result.err.startswith("warning: ")
    for marker in ("Traceback", "INSERT", "disk full", TEST_DATABASE_URL):
        assert marker not in result.err
    assert operation in result.err
    target_credential = payload["credential_id"]
    assert target_credential in result.err
    if operation in ("credential_issued", "token_issued", "token_revoked"):
        assert payload["token_id"] in result.err
    if "token" in payload:
        assert payload["token"] not in result.err

    if operation == "credential_issued":
        assert credential_row(engine, target_credential)["status"] == "active"
        assert events_of(engine, target_credential) == []
        return
    if operation == "token_issued":
        assert payload["token_id"] in token_rows(engine, credential_id)
    elif operation == "token_revoked":
        assert token_rows(engine, credential_id)[token_id]["revoked_at"] is not None
    else:
        assert credential_row(engine, credential_id)["status"] == "revoked"
    types = [event["event_type"] for event in events_of(engine, credential_id)]
    assert types == ["credential_issued"]


def test_help_lists_the_credential_and_token_commands(cli):
    result = cli("--help")
    assert result.code == 0
    assert "credential" in result.out
    assert "token" in result.out
    for argv, option in (
        (["credential", "rotate", "-h"], "--overlap-hours"),
        (["token", "revoke", "-h"], "--token"),
    ):
        assert option in cli(*argv).out
