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
    fail,
    provisioning,
    quiet_security_event_log,
    run_command,
    service_auth,
)
from app.config import Settings


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
    return parser


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
        return run_command(settings, args.handler, args, stdout, stderr)


def run_process() -> int:
    try:
        settings = Settings()
    except Exception:
        # 校验异常的文本带配置值，也不打印堆栈。
        fail(sys.stderr, "invalid configuration (check CORE_ENV and CORE_DATABASE_URL)")
        return EXIT_FAILURE
    return main(sys.argv[1:], settings, sys.stdout, sys.stderr)


if __name__ == "__main__":
    sys.exit(run_process())
