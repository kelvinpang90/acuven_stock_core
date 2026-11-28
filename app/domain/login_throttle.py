"""后台登录失败锁定的规则（docs/ADMIN_AUTH.md 4.2）。

纯函数，只用标准库：规范来源、两个摘要、窗口与两道上限的判定。按摘要查库数条数在
app/services/admin_auth.py。throttle_key 与 source_key 不进日志、异常消息与接口响应。
"""

import hashlib
import ipaddress
from collections.abc import Iterable
from datetime import datetime, timedelta

WINDOW = timedelta(minutes=60)
# 同一来源在窗口内的失败满这么多条即锁定（不论用户名）；先于按来源与用户名的一道判。
SOURCE_LIMIT = 30
# 同一来源与用户名在窗口内的失败满这么多条即锁定。
THROTTLE_LIMIT = 10

SOURCE = "source"
THROTTLE = "throttle"

# locked 不算失败（被锁时继续尝试不延长锁定），success 不清零。
FAILURE_OUTCOMES = frozenset({"bad_password", "unknown_user", "disabled"})


def require_whole_second(now: datetime) -> None:
    """规则与服务收的 now：不带时区的 UTC、整秒；否则 ValueError。"""
    if not isinstance(now, datetime):
        raise ValueError("now must be a datetime")
    if now.tzinfo is not None:
        raise ValueError("now must be a naive UTC datetime")
    if now.microsecond:
        raise ValueError("now must be a whole second")


def normalize_source(text: str) -> str:
    """IPv4 取规范文本；IPv4 映射的 IPv6 取其中的 IPv4；其他 IPv6 取所在 /64；其余原样。"""
    if not isinstance(text, str):
        raise TypeError("source must be a string")
    try:
        return str(ipaddress.IPv4Address(text))
    except ValueError:
        pass
    try:
        address = ipaddress.IPv6Address(text)
    except ValueError:
        return text
    if address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    # 不经 ip_network：带作用域（%eth0）的地址按 int 取网段，不带作用域。
    prefix = int(address) >> 64 << 64
    return str(ipaddress.IPv6Network((prefix, 64)))


def _sha256(text: str) -> str:
    # 无法按 UTF-8 编码的字符（孤立代理项）换成 ?，只用于计数。
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def throttle_key(source: str, username: str) -> str:
    """规范来源加换行加规范用户名的 SHA-256 十六进制；两者都由调用方先规范化。"""
    return _sha256(source + "\n" + username)


def source_key(source: str) -> str:
    """规范来源的 SHA-256 十六进制。"""
    return _sha256(source)


def window_start(now: datetime) -> datetime:
    """窗口的下界（不含）：now − 60 分钟。上界是 now（含）。"""
    require_whole_second(now)
    return now - WINDOW


def in_window(occurred_at: datetime, now: datetime) -> bool:
    """now − 60 分钟 < occurred_at <= now；晚于 now 的不算。"""
    return window_start(now) < occurred_at <= now


def is_failure(outcome: str) -> bool:
    return outcome in FAILURE_OUTCOMES


def count_failures(attempts: Iterable[tuple[datetime, str]], now: datetime) -> int:
    """attempts 是 (occurred_at, outcome)；数窗口内的失败。服务里用同样条件在库里数。"""
    start = window_start(now)
    return sum(
        1 for occurred_at, outcome in attempts if start < occurred_at <= now and is_failure(outcome)
    )


def lock_reason(*, source_failures: int, throttle_failures: int) -> str | None:
    """按顺序判两道上限：先按来源（满 30），再按来源与用户名（满 10）；都没满为 None。"""
    if source_failures >= SOURCE_LIMIT:
        return SOURCE
    if throttle_failures >= THROTTLE_LIMIT:
        return THROTTLE
    return None
