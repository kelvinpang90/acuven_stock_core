"""服务凭据与令牌：签发、轮换、吊销与列出（契约 7.2、第 10 节第 4 条）。

改变权限的四个子命令都必带 --actor 与 --reason，成功后返回 Audited，由 run_command 在业务事务
提交之后另开短事务写一条安全事件。明文令牌只出现在成功输出的 JSON 里：库里只存摘要，
不进安全事件、报错与日志；输出丢了就吊销那个令牌再轮换。
不做：修改凭据的 scope 或入口（要改就签发新凭据、吊销旧的）、恢复已吊销的对象、到期清理。
"""

from argparse import Namespace
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, DateTime, Row, insert, literal_column, select, update

from app.cli import ACTIVE, Audited, CliError, clean_name, require_channel, require_tenant
from app.domain.environment import PRODUCTION
from app.domain.ids import new_id
from app.domain.requests import is_reference
from app.domain.service_auth import (
    DEMO_RESET,
    TOKEN_PREFIX,
    InvalidOption,
    InvalidScopes,
    generate_token,
    parse_overlap_hours,
    parse_reason,
    rotation_deadline,
    token_digest,
    tokens_to_expire,
    validate_issued_scopes,
)
from app.models.service_auth import Credential, CredentialToken
from app.services.security_events import SecurityEventRecord

REVOKED = "revoked"
CREDENTIAL_ISSUED = "credential_issued"
TOKEN_ISSUED = "token_issued"
TOKEN_REVOKED = "token_revoked"
CREDENTIAL_REVOKED = "credential_revoked"

# 截止时间、吊销时间与 updated_at 都按数据库时间写（鉴权也按数据库时间比较）。
DB_NOW = literal_column("UTC_TIMESTAMP(6)", DateTime())


def _utc(value: datetime | None) -> str | None:
    """库里是 UTC 的 naive 时间；输出与安全事件里写成 RFC 3339（契约 8.1）。"""
    return None if value is None else value.isoformat(timespec="microseconds") + "Z"


def _db_now(conn: Connection) -> datetime:
    return conn.execute(select(DB_NOW)).scalar_one()


def _operator(args: Namespace) -> tuple[str, str]:
    """--actor 用第 1 节的引用格式（不含姓名与邮箱）；--reason 去首尾空格后单行。"""
    if not is_reference(args.actor):
        raise CliError("actor must be 1 to 128 letters, digits or . _ : - (no names or emails)")
    try:
        reason = parse_reason(args.reason)
    except InvalidOption as error:
        raise CliError(str(error)) from None
    return args.actor, reason


def _reject_demo_reset_in_production(args: Namespace, scopes: list[str]) -> None:
    # 生产实例不签发、也不轮换带 demo.reset 的凭据（契约 6.1、第 10 节第 4 条）。
    if args.core_env == PRODUCTION and DEMO_RESET in scopes:
        raise CliError(f"credentials with {DEMO_RESET} are not allowed when CORE_ENV=production")


def _lock_active_credential(conn: Connection, credential_id: str) -> Row:
    """按 ID 查凭据并加排他锁：并发的轮换与吊销排队，后到的看到的是前一个提交后的状态。"""
    credential = conn.execute(
        select(Credential.credential_id, Credential.tenant_id, Credential.scopes, Credential.status)
        .where(Credential.credential_id == credential_id)
        .with_for_update()
    ).one_or_none()
    if credential is None:
        raise CliError("credential not found")
    if credential.status != ACTIVE:
        raise CliError("credential is already revoked")
    return credential


def _add_token(conn: Connection, tenant_id: str, credential_id: str) -> tuple[str, str]:
    """签发一个无截止时间的令牌，返回 (令牌 ID, 明文令牌)；库里只存摘要。"""
    token = generate_token()
    token_id = new_id("ctk")
    conn.execute(
        insert(CredentialToken).values(
            token_id=token_id,
            tenant_id=tenant_id,
            credential_id=credential_id,
            token_digest=token_digest(token),
        )
    )
    return token_id, token


def issue_credential(conn: Connection, args: Namespace) -> Audited:
    actor, reason = _operator(args)
    name = clean_name(args.name)
    scopes = args.scope or []
    try:
        validate_issued_scopes(scopes, has_channel=args.channel is not None)
    except InvalidScopes:
        # 原消息带未知的 scope 值，不能原样输出。
        raise CliError(
            "invalid scopes: need at least one, all known and distinct; "
            "ops.read must stand alone and without a sales channel"
        ) from None
    _reject_demo_reset_in_production(args, scopes)
    require_tenant(conn, args.tenant, active=True)
    if args.channel is not None:
        require_channel(conn, args.tenant, args.channel, active=True)
    credential = {
        "credential_id": new_id("cred"),
        "tenant_id": args.tenant,
        "channel_id": args.channel,
        "name": name,
        "scopes": list(scopes),
        "status": ACTIVE,
    }
    conn.execute(insert(Credential).values(credential))
    token_id, token = _add_token(conn, args.tenant, credential["credential_id"])
    event = SecurityEventRecord(
        CREDENTIAL_ISSUED,
        tenant_id=args.tenant,
        credential_id=credential["credential_id"],
        actor_ref=actor,
        details={"reason": reason, "token_id": token_id},
    )
    return Audited(credential | {"token_id": token_id, "token": token}, event)


def rotate_token(conn: Connection, args: Namespace) -> Audited:
    """签发新令牌，其余未吊销的旧令牌截止时间不晚于轮换时刻加重叠小时数（契约第 10 节第 4 条）。"""
    actor, reason = _operator(args)
    try:
        overlap_hours = parse_overlap_hours(args.overlap_hours)
    except InvalidOption as error:
        raise CliError(str(error)) from None
    credential = _lock_active_credential(conn, args.credential)
    require_tenant(conn, credential.tenant_id, active=True)
    _reject_demo_reset_in_production(args, credential.scopes)
    rotated_at = _db_now(conn)
    deadline = rotation_deadline(rotated_at, overlap_hours)
    # 在签发新令牌之前查，新令牌自然不在其中。
    others = conn.execute(
        select(CredentialToken.token_id, CredentialToken.expires_at)
        .where(
            CredentialToken.credential_id == credential.credential_id,
            CredentialToken.revoked_at.is_(None),
        )
        .order_by(CredentialToken.created_at, CredentialToken.token_id)
        .with_for_update()
    ).all()
    expiring = tokens_to_expire(others, deadline)
    if expiring:
        conn.execute(
            update(CredentialToken)
            .where(CredentialToken.token_id.in_(expiring))
            .values(expires_at=deadline, updated_at=rotated_at)
        )
    token_id, token = _add_token(conn, credential.tenant_id, credential.credential_id)
    expired_tokens = [{"token_id": old, "expires_at": _utc(deadline)} for old in expiring]
    event = SecurityEventRecord(
        TOKEN_ISSUED,
        tenant_id=credential.tenant_id,
        credential_id=credential.credential_id,
        actor_ref=actor,
        details={"reason": reason, "token_id": token_id, "expired_tokens": expired_tokens},
    )
    payload = {
        "credential_id": credential.credential_id,
        "token_id": token_id,
        "token": token,
        "expired_tokens": expired_tokens,
    }
    return Audited(payload, event)


def revoke_token(conn: Connection, args: Namespace) -> Audited:
    # 误把明文令牌当令牌 ID：单独提示，且不回显它。
    if args.token.startswith(TOKEN_PREFIX):
        raise CliError("--token takes the token ID (ctk_...), not the token itself")
    actor, reason = _operator(args)
    token = conn.execute(
        select(
            CredentialToken.token_id,
            CredentialToken.tenant_id,
            CredentialToken.credential_id,
            CredentialToken.revoked_at,
        )
        .where(CredentialToken.token_id == args.token)
        .with_for_update()
    ).one_or_none()
    if token is None:
        raise CliError("token not found")
    if token.revoked_at is not None:
        raise CliError("token is already revoked")
    revoked_at = _db_now(conn)
    conn.execute(
        update(CredentialToken)
        .where(CredentialToken.token_id == token.token_id)
        .values(revoked_at=revoked_at, updated_at=revoked_at)
    )
    event = SecurityEventRecord(
        TOKEN_REVOKED,
        tenant_id=token.tenant_id,
        credential_id=token.credential_id,
        actor_ref=actor,
        details={"reason": reason, "token_id": token.token_id},
    )
    payload = {
        "token_id": token.token_id,
        "credential_id": token.credential_id,
        "revoked_at": _utc(revoked_at),
    }
    return Audited(payload, event)


def revoke_credential(conn: Connection, args: Namespace) -> Audited:
    """凭据改为 revoked，令牌行不动：凭据已吊销时它的令牌鉴权一律 401。"""
    actor, reason = _operator(args)
    credential = _lock_active_credential(conn, args.credential)
    revoked_at = _db_now(conn)
    conn.execute(
        update(Credential)
        .where(Credential.credential_id == credential.credential_id)
        .values(status=REVOKED, revoked_at=revoked_at, updated_at=revoked_at)
    )
    event = SecurityEventRecord(
        CREDENTIAL_REVOKED,
        tenant_id=credential.tenant_id,
        credential_id=credential.credential_id,
        actor_ref=actor,
        details={"reason": reason},
    )
    payload = {
        "credential_id": credential.credential_id,
        "status": REVOKED,
        "revoked_at": _utc(revoked_at),
    }
    return Audited(payload, event)


def list_credentials(conn: Connection, args: Namespace) -> dict[str, Any]:
    """只读，不写安全事件；不输出令牌摘要。"""
    require_tenant(conn, args.tenant, active=False)
    rows = conn.execute(
        select(
            CredentialToken.credential_id,
            CredentialToken.token_id,
            CredentialToken.created_at,
            CredentialToken.expires_at,
            CredentialToken.revoked_at,
        )
        .where(CredentialToken.tenant_id == args.tenant)
        .order_by(CredentialToken.created_at, CredentialToken.token_id)
    )
    tokens: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        token = {
            "token_id": row.token_id,
            "created_at": _utc(row.created_at),
            "expires_at": _utc(row.expires_at),
            "revoked_at": _utc(row.revoked_at),
        }
        tokens.setdefault(row.credential_id, []).append(token)
    credentials = conn.execute(
        select(
            Credential.credential_id,
            Credential.name,
            Credential.channel_id,
            Credential.scopes,
            Credential.status,
            Credential.revoked_at,
        )
        .where(Credential.tenant_id == args.tenant)
        .order_by(Credential.credential_id)
    )
    return {
        "tenant_id": args.tenant,
        "credentials": [
            {
                "credential_id": credential.credential_id,
                "name": credential.name,
                "channel_id": credential.channel_id,
                "scopes": credential.scopes,
                "status": credential.status,
                "revoked_at": _utc(credential.revoked_at),
                "tokens": tokens.get(credential.credential_id, []),
            }
            for credential in credentials
        ],
    }


def _add_operator_options(parser: Any) -> None:
    parser.add_argument("--actor", required=True, help="operator reference (no names or emails)")
    parser.add_argument("--reason", required=True, help="single line, up to 500 characters")


def register(subparsers: Any) -> None:
    credential = subparsers.add_parser("credential", help="service credentials")
    credential_actions = credential.add_subparsers(dest="action", required=True)

    issue = credential_actions.add_parser(
        "issue", help="issue a credential and its first token (the token is shown once)"
    )
    issue.add_argument("--tenant", required=True, help="tenant ID")
    issue.add_argument("--name", required=True)
    issue.add_argument("--scope", action="append", help="repeat for each scope")
    issue.add_argument("--channel", help="sales channel ID of the same tenant")
    _add_operator_options(issue)
    issue.set_defaults(handler=issue_credential)

    rotate = credential_actions.add_parser(
        "rotate", help="issue a new token; the other tokens expire after the overlap"
    )
    rotate.add_argument("--credential", required=True, help="credential ID")
    rotate.add_argument("--overlap-hours", required=True, help="whole hours from 0 to 168")
    _add_operator_options(rotate)
    rotate.set_defaults(handler=rotate_token)

    revoke = credential_actions.add_parser(
        "revoke", help="revoke a credential and with it all its tokens (cannot be undone)"
    )
    revoke.add_argument("--credential", required=True, help="credential ID")
    _add_operator_options(revoke)
    revoke.set_defaults(handler=revoke_credential)

    list_parser = credential_actions.add_parser(
        "list", help="list a tenant's credentials and tokens (without digests)"
    )
    list_parser.add_argument("--tenant", required=True, help="tenant ID")
    list_parser.set_defaults(handler=list_credentials)

    token = subparsers.add_parser("token", help="credential tokens")
    token_actions = token.add_subparsers(dest="action", required=True)
    token_revoke = token_actions.add_parser("revoke", help="revoke one token (cannot be undone)")
    token_revoke.add_argument("--token", required=True, help="token ID (ctk_…), not the token")
    _add_operator_options(token_revoke)
    token_revoke.set_defaults(handler=revoke_token)
