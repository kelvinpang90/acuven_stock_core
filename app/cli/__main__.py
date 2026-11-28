"""运维命令行的入口：python -m app.cli <子命令>。

测试直接调用 main，传入参数列表、Settings 与两个输出流；进程入口从环境变量构造 Settings。
退出码与输出约定见 app/cli/__init__.py。
"""

import argparse
import sys
from collections.abc import Sequence
from typing import NoReturn, TextIO

from app.cli import (
    EXIT_FAILURE,
    EXIT_OK,
    EXIT_USAGE,
    CliError,
    admin,
    fail,
    inspect,
    provisioning,
    quiet_security_event_log,
    run_command,
    service_auth,
)
from app.config import Settings
from app.domain.environment import PRODUCTION


class _HelpRequested(Exception):
    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class _Parser(argparse.ArgumentParser):
    """不打印、不退出进程：帮助与参数错误都交回 main 处理。

    子命令的解析器由 add_subparsers 按同一个类建，同样适用。
    """

    def print_help(self, file: TextIO | None = None) -> None:
        raise _HelpRequested(self.format_help())

    def error(self, message: str) -> NoReturn:
        # argparse 的报错文本带传入的值，不能原样输出。
        raise CliError("invalid arguments; run with --help for usage")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="python -m app.cli", description="Acuven Stock Core operator commands")
    subparsers = parser.add_subparsers(dest="command", required=True)
    provisioning.register(subparsers)
    service_auth.register(subparsers)
    inspect.register(subparsers)
    admin.register(subparsers)
    return parser


def admin_unavailable_error(settings: Settings) -> str | None:
    """账号命令只在生产实例且开了后台时可用（docs/ADMIN_AUTH.md 4.6）；在连库之前判定。"""
    if settings.env == PRODUCTION and settings.admin_enabled:
        return None
    enabled = "true" if settings.admin_enabled else "false"
    return (
        f"admin commands need CORE_ENV={PRODUCTION} and CORE_ADMIN_ENABLED=true;"
        f" CORE_ENV is {settings.env}, CORE_ADMIN_ENABLED is {enabled}"
    )


def main(argv: Sequence[str], settings: Settings, stdout: TextIO, stderr: TextIO) -> int:
    # 日志配置只在这一次调用期间生效，返回前恢复，不影响同一进程里的其他代码。
    with quiet_security_event_log():
        try:
            args = build_parser().parse_args(list(argv))
        except _HelpRequested as help_requested:
            stdout.write(help_requested.text)
            return EXIT_OK
        except CliError as error:
            fail(stderr, error.message)
            return EXIT_USAGE
        if args.command == admin.COMMAND:
            # 不经 run_command：账号的服务函数自己提交；实例与开关不对是退出码 2、不碰库。
            unavailable = admin_unavailable_error(settings)
            if unavailable is not None:
                fail(stderr, unavailable)
                return EXIT_USAGE
            return admin.run_admin(settings, args.handler, args, stdout, stderr)
        return run_command(settings, args.handler, args, stdout, stderr)


def run_process() -> int:
    try:
        settings = Settings()
    except Exception:
        # 校验异常的文本带配置值，也不打印堆栈。
        fail(
            sys.stderr,
            "invalid configuration (check CORE_ENV, CORE_DATABASE_URL,"
            " CORE_RESERVATION_TTL_SECONDS and CORE_ADMIN_ENABLED)",
        )
        return EXIT_FAILURE
    return main(sys.argv[1:], settings, sys.stdout, sys.stderr)


if __name__ == "__main__":
    sys.exit(run_process())
