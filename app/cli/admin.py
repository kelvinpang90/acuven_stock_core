"""后台管理员账号：建号、重设密码、停用与恢复（docs/ADMIN_AUTH.md 4.6，契约 7.2）。

只在 CORE_ENV=production 且 CORE_ADMIN_ENABLED=true 时可用，由 app/cli/__main__.py 在连库之前判定。
不经 run_command：4.5 的服务函数各自开事务并提交，所以这里用 run_admin 取一个不在事务里的连接
交给子命令；连库之前与之后的检查、退出码与异常的输出照 run_command。
只读查询在这个连接上自动开的事务，在提示输入密码与调改数据的服务函数之前都先回滚：
提示输入时不持有事务与锁。密码只经 getpass 不回显地输入两次，不接受命令行参数或环境变量；
输出只有规范用户名、账号 ID 与结果，不含密码与哈希。
成功（「已是……状态」除外）后在提交之后另开短事务写一条安全事件。
"""

import getpass
from argparse import Namespace
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TextIO

from sqlalchemy import Connection
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from app.cli import EXIT_FAILURE, EXIT_OK, EXIT_USAGE, CliError, fail, write_audit_event, write_json
from app.config import Settings
from app.db import make_engine
from app.domain.pw_hash import is_valid_username, normalize_username
from app.domain.requests import is_reference
from app.domain.service_auth import InvalidOption, parse_reason
from app.services.admin_auth import (
    ALREADY_EXISTS,
    INVALID_TENANT,
    INVALID_USERNAME,
    NOT_FOUND,
    WEAK_PASSWORD,
    AccountError,
    account_exists,
    create_account,
    disable_account,
    enable_account,
    get_account,
    reset_password,
    utc_now,
)
from app.services.instance import instance_check_error
from app.services.security_events import SecurityEventRecord

COMMAND = "admin"

ACCOUNT_CREATED = "admin_account_created"
PASSWORD_RESET = "admin_password_reset"
ACCOUNT_DISABLED = "admin_account_disabled"
ACCOUNT_ENABLED = "admin_account_enabled"

# 输出里的 result
CREATED = "created"
PASSWORD_WAS_RESET = "password_reset"
DISABLED = "disabled"
ENABLED = "enabled"
ALREADY_DISABLED = "already_disabled"
ALREADY_ENABLED = "already_enabled"

# AccountError 的 code 对应的一行说明；不带用户名与密码。
ACCOUNT_ERRORS = {
    INVALID_USERNAME: (
        "invalid username: 3 to 32 of a-z 0-9 . _ - (case-insensitive), "
        "starting with a letter or digit"
    ),
    INVALID_TENANT: "tenant not found or not active",
    ALREADY_EXISTS: "account already exists",
    NOT_FOUND: "account not found",
    WEAK_PASSWORD: "password must be 12 to 128 characters and must not equal the username",
}


@dataclass(frozen=True)
class Change:
    """子命令的结果：event_type 为 None 表示「已是……状态」，没有改、不记安全事件。"""

    username: str
    result: str
    event_type: str | None


Handler = Callable[[Connection, Namespace], Change]


def _operator(args: Namespace) -> tuple[str, str]:
    """--actor 与 --reason 的格式与校验同 credential issue。"""
    if not is_reference(args.actor):
        raise CliError("actor must be 1 to 128 letters, digits or . _ : - (no names or emails)")
    try:
        reason = parse_reason(args.reason)
    except InvalidOption as error:
        raise CliError(str(error)) from None
    return args.actor, reason


def _username(args: Namespace) -> str:
    """规范用户名（4.1）；不合法在连库之前报错。"""
    name = normalize_username(args.username)
    if not is_valid_username(name):
        raise CliError(ACCOUNT_ERRORS[INVALID_USERNAME])
    return name


def _read_password() -> str:
    """不回显地输入两次；EOF 或 Ctrl+C 算取消。调用时连接上不能有事务。"""
    try:
        first = getpass.getpass("New password: ")
        second = getpass.getpass("Repeat password: ")
    except (EOFError, KeyboardInterrupt):
        raise CliError("cancelled; nothing was written") from None
    if first != second:
        raise CliError("passwords do not match; nothing was written")
    return first


def create_account_command(conn: Connection, args: Namespace) -> Change:
    exists = account_exists(conn, username=args.username)
    conn.rollback()
    if exists:
        raise AccountError(ALREADY_EXISTS)
    password = _read_password()
    create_account(
        conn, tenant_id=args.tenant, username=args.username, password=password, now=utc_now()
    )
    return Change(args.canonical_username, CREATED, ACCOUNT_CREATED)


def reset_password_command(conn: Connection, args: Namespace) -> Change:
    exists = account_exists(conn, username=args.username)
    conn.rollback()
    if not exists:
        raise AccountError(NOT_FOUND)
    password = _read_password()
    reset_password(conn, username=args.username, password=password, now=utc_now())
    return Change(args.canonical_username, PASSWORD_WAS_RESET, PASSWORD_RESET)


def disable_account_command(conn: Connection, args: Namespace) -> Change:
    if disable_account(conn, username=args.username, now=utc_now()):
        return Change(args.canonical_username, DISABLED, ACCOUNT_DISABLED)
    return Change(args.canonical_username, ALREADY_DISABLED, None)


def enable_account_command(conn: Connection, args: Namespace) -> Change:
    if enable_account(conn, username=args.username):
        return Change(args.canonical_username, ENABLED, ACCOUNT_ENABLED)
    return Change(args.canonical_username, ALREADY_ENABLED, None)


def _outcome(
    conn: Connection, change: Change, actor: str, reason: str
) -> tuple[dict[str, Any], SecurityEventRecord | None]:
    """提交之后再读账号，得到输出与安全事件；读完回滚那个只读事务。"""
    account = get_account(conn, username=change.username)
    conn.rollback()
    if account is None:
        raise LookupError("account not found after the change")
    payload = {
        "username": account.username,
        "admin_account_id": account.account_id,
        "result": change.result,
    }
    if change.event_type is None:
        return payload, None
    event = SecurityEventRecord(
        change.event_type,
        tenant_id=account.tenant_id,
        credential_id=account.credential_id,
        actor_ref=actor,
        details={
            "reason": reason,
            "admin_account_id": account.account_id,
            "credential_id": account.credential_id,
            "username": account.username,
        },
    )
    return payload, event


def run_admin(
    settings: Settings, handler: Handler, args: Namespace, stdout: TextIO, stderr: TextIO
) -> int:
    """实例与开关已由调用方判过。之后：用户名与操作者 → 连库与实例标记 → 子命令 → 安全事件。"""
    try:
        args.canonical_username = _username(args)
        actor, reason = _operator(args)
    except CliError as error:
        fail(stderr, error.message)
        return error.exit_code
    if not settings.database_url:
        fail(stderr, "database not configured (CORE_DATABASE_URL is empty)")
        return EXIT_FAILURE
    try:
        engine = make_engine(settings.database_url)
    except Exception:
        fail(stderr, "database configuration is invalid")
        return EXIT_FAILURE
    try:
        # 启动核对（契约 6.1）：不过就不执行命令，也不写安全事件。
        # instance_check_error 自己把读库失败折成一行原因；这里再兜一层，异常不带出连接串。
        try:
            mismatch = instance_check_error(engine, settings.env)
        except OperationalError:
            fail(stderr, "database unavailable (OperationalError)")
            return EXIT_FAILURE
        except SQLAlchemyError as error:
            fail(stderr, f"database error ({type(error).__name__}); nothing was written")
            return EXIT_FAILURE
        except Exception as error:
            fail(stderr, f"unexpected failure ({type(error).__name__}); nothing was written")
            return EXIT_FAILURE
        if mismatch is not None:
            fail(stderr, mismatch)
            return EXIT_FAILURE
        try:
            with engine.connect() as conn:
                try:
                    change = handler(conn, args)
                except CliError as error:
                    fail(stderr, error.message)
                    return error.exit_code
                except AccountError as error:
                    fail(stderr, ACCOUNT_ERRORS.get(error.code, "invalid account operation"))
                    return EXIT_USAGE
                except OperationalError:
                    # 提交途中断开时不确定有没有写入，所以不说「什么也没写」。
                    fail(stderr, "database unavailable (OperationalError)")
                    return EXIT_FAILURE
                except SQLAlchemyError as error:
                    fail(stderr, f"database error ({type(error).__name__}); nothing was written")
                    return EXIT_FAILURE
                except Exception as error:
                    # 只给异常类名：异常文本可能带连接串或参数值。
                    fail(
                        stderr,
                        f"unexpected failure ({type(error).__name__}); nothing was written",
                    )
                    return EXIT_FAILURE
                try:
                    payload, event = _outcome(conn, change, actor, reason)
                except Exception as error:
                    _report_after_commit(stderr, change, error)
                    return EXIT_FAILURE
        except SQLAlchemyError as error:
            # 取连接或归还连接时失败（子命令里的错误已在上面处理）。
            fail(stderr, f"database error ({type(error).__name__})")
            return EXIT_FAILURE
        if event is not None:
            write_audit_event(engine, event, stderr)
    finally:
        engine.dispose()
    write_json(stdout, payload)
    return EXIT_OK


def _report_after_commit(stderr: TextIO, change: Change, error: Exception) -> None:
    """服务函数已提交（或无需改动）之后读账号失败：结果已定，只是没能输出与记事件。"""
    name = type(error).__name__
    if change.event_type is None:
        fail(stderr, f"no change needed ({change.result}), but reading the account failed ({name})")
        return
    fail(
        stderr,
        f"the change took effect ({change.result}), but reading the account failed ({name}); "
        f"security event {change.event_type} was not recorded, record it manually",
    )


def _add_options(parser: Any) -> None:
    parser.add_argument("--username", required=True, help="3 to 32 of a-z 0-9 . _ -")
    parser.add_argument("--actor", required=True, help="operator reference (no names or emails)")
    parser.add_argument("--reason", required=True, help="single line, up to 500 characters")


def register(subparsers: Any) -> None:
    admin = subparsers.add_parser(
        COMMAND, help="admin accounts (CORE_ENV=production and CORE_ADMIN_ENABLED=true only)"
    )
    actions = admin.add_subparsers(dest="action", required=True)

    create = actions.add_parser(
        "create-account", help="create an account; the password is prompted for twice"
    )
    create.add_argument("--tenant", required=True, help="tenant ID")
    _add_options(create)
    create.set_defaults(handler=create_account_command)

    reset = actions.add_parser(
        "reset-password", help="set a new password (prompted twice) and end all its sessions"
    )
    _add_options(reset)
    reset.set_defaults(handler=reset_password_command)

    disable = actions.add_parser("disable-account", help="disable an account and end its sessions")
    _add_options(disable)
    disable.set_defaults(handler=disable_account_command)

    enable = actions.add_parser("enable-account", help="enable a disabled account")
    _add_options(enable)
    enable.set_defaults(handler=enable_account_command)
