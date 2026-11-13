"""运维命令行的共用部分（契约第 10 节第 4 条：Core 没有 HTTP 管理接口，开通与凭据只走这里）。

只在 Core 容器里由运维执行：python -m app.cli <子命令>，入口在 app/cli/__main__.py。

子命令模块只 import 本模块、不 import __main__，并提供 register(subparsers)：给每个子命令
set_defaults(handler=...)。handler 收到同一事务里的连接与解析好的参数，返回要输出的 JSON 对象；
校验不过就抛 CliError，事务回滚、什么也不写。

退出码与输出约定：
- 0：成功，向标准输出写一行 JSON；
- 2：参数或校验错误，向标准错误写一行说明；
- 1：数据库未配置、连不上等其他失败，向标准错误写一行说明。
所有说明都不含连接串、密码、堆栈与运维传入的参数值（误传的明文令牌不会被打出来）。
"""

import json
import unicodedata
from argparse import Namespace
from collections.abc import Callable
from typing import Any, TextIO

from sqlalchemy import Connection, Row, select
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from app.config import Settings
from app.db import make_engine
from app.models.catalog import Tenant
from app.models.inventory import SalesChannel

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2

ACTIVE = "active"
MAX_NAME_LENGTH = 200

Handler = Callable[[Connection, Namespace], dict[str, Any]]


class CliError(Exception):
    """参数或校验错误：消息原样写到标准错误，所以不得带参数值。"""

    def __init__(self, message: str, exit_code: int = EXIT_USAGE) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code


def fail(stderr: TextIO, message: str) -> None:
    stderr.write(f"error: {message}\n")


def write_json(stdout: TextIO, payload: dict[str, Any]) -> None:
    stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")


def clean_name(value: str) -> str:
    """去首尾空格后 1 到 200 个字符、单行、不含控制字符。"""
    name = value.strip()
    if not name:
        raise CliError("name must not be empty")
    if len(name) > MAX_NAME_LENGTH:
        raise CliError(f"name must be at most {MAX_NAME_LENGTH} characters")
    # Cc 含换行与制表符；Zl、Zp 是 Unicode 的行与段分隔符；
    # Cs 是命令行里解不开的字节留下的孤立代理项。
    if any(unicodedata.category(char) in ("Cc", "Zl", "Zp", "Cs") for char in name):
        raise CliError("name must be a single line without control characters")
    return name


def require_tenant(conn: Connection, tenant_id: str, *, active: bool) -> Row:
    """按 ID 查租户并加共享锁（同一事务里引用它建对象时，状态不会被并发改掉）。"""
    tenant = conn.execute(
        select(Tenant.tenant_id, Tenant.name, Tenant.status)
        .where(Tenant.tenant_id == tenant_id)
        .with_for_update(read=True)
    ).one_or_none()
    if tenant is None:
        raise CliError("tenant not found")
    if active and tenant.status != ACTIVE:
        raise CliError("tenant is not active")
    return tenant


def require_channel(conn: Connection, tenant_id: str, channel_id: str, *, active: bool) -> Row:
    """按 ID 查本租户的销售入口并加共享锁；属于别的租户与不存在报同一句。"""
    channel = conn.execute(
        select(SalesChannel.channel_id, SalesChannel.pool_id, SalesChannel.status)
        .where(SalesChannel.tenant_id == tenant_id, SalesChannel.channel_id == channel_id)
        .with_for_update(read=True)
    ).one_or_none()
    if channel is None:
        raise CliError("sales channel not found for this tenant")
    if active and channel.status != ACTIVE:
        raise CliError("sales channel is not active")
    return channel


def run_command(
    settings: Settings, handler: Handler, args: Namespace, stdout: TextIO, stderr: TextIO
) -> int:
    """一个命令一个数据库事务：handler 抛错时回滚，提交成功后才输出结果。"""
    if not settings.database_url:
        fail(stderr, "database not configured (CORE_DATABASE_URL is empty)")
        return EXIT_FAILURE
    try:
        engine = make_engine(settings.database_url)
    except Exception:
        fail(stderr, "database configuration is invalid")
        return EXIT_FAILURE
    try:
        with engine.begin() as conn:
            payload = handler(conn, args)
    except CliError as error:
        fail(stderr, error.message)
        return error.exit_code
    except OperationalError:
        # 提交途中断开时不确定有没有写入，所以不说「什么也没写」。
        fail(stderr, "database unavailable")
        return EXIT_FAILURE
    except SQLAlchemyError:
        fail(stderr, "database error; nothing was written")
        return EXIT_FAILURE
    except Exception as error:
        # 只给异常类名：异常文本可能带连接串或参数值。
        fail(stderr, f"unexpected failure ({type(error).__name__}); nothing was written")
        return EXIT_FAILURE
    finally:
        engine.dispose()
    write_json(stdout, payload)
    return EXIT_OK
