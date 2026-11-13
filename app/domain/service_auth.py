"""服务凭据的令牌、scope 与池授权规则（契约 §8.1、§10 第 4 条）。

这里只有不碰数据库的判断：按摘要查令牌、读凭据与租户状态由上层做，结果交给这里判定。
运维命令行的 --reason、--overlap-hours 与轮换时选哪些旧令牌也在这里（app/cli/service_auth.py）。
"""

import base64
import hashlib
import os
import re
import unicodedata
from collections.abc import Collection, Iterable, Sequence
from datetime import datetime, timedelta
from enum import StrEnum

TOKEN_PREFIX = "csk_"
TOKEN_RANDOM_BYTES = 32
# 32 字节的 base64url（不补 =）是 43 个字符。
_TOKEN = re.compile(r"csk_[A-Za-z0-9_-]{43}")

ACTIVE = "active"

CATALOG_READ = "catalog.read"
CATALOG_WRITE = "catalog.write"
STOCK_READ = "stock.read"
STOCK_WRITE = "stock.write"
ORDERS_READ = "orders.read"
ORDERS_WRITE = "orders.write"
DEMO_RESET = "demo.reset"
OPS_READ = "ops.read"
SCOPES = frozenset(
    {
        CATALOG_READ,
        CATALOG_WRITE,
        STOCK_READ,
        STOCK_WRITE,
        ORDERS_READ,
        ORDERS_WRITE,
        DEMO_RESET,
        OPS_READ,
    }
)


MAX_REASON_LENGTH = 500
# 轮换的重叠期：旧令牌的截止时间不晚于轮换时刻后 7 天（契约第 10 节第 4 条）。
MAX_OVERLAP_HOURS = 7 * 24
# 只收 ASCII 数字：int() 还认全角数字、正负号与空格。三位以内，避开超长数字串。
_OVERLAP_HOURS = re.compile(r"[0-9]{1,3}")
_OVERLAP_HOURS_MESSAGE = f"overlap hours must be a whole number from 0 to {MAX_OVERLAP_HOURS}"


class InvalidScopes(ValueError):
    """签发凭据时给的 scope 不合格（命令行拒绝签发）。"""


class InvalidOption(ValueError):
    """命令行的参数不合格；消息会写到标准错误，所以不带参数值。"""


class PoolDecision(StrEnum):
    ALLOWED = "allowed"
    FORBIDDEN = "forbidden"  # 403 FORBIDDEN
    NOT_FOUND = "not_found"  # 404，与对象不存在同一错误码与 message


def generate_token(random_bytes: bytes | None = None) -> str:
    """csk_ 加 32 字节随机数的 base64url（不补 =）；random_bytes 可注入以便测试。"""
    if random_bytes is None:
        random_bytes = os.urandom(TOKEN_RANDOM_BYTES)
    if not isinstance(random_bytes, bytes) or len(random_bytes) != TOKEN_RANDOM_BYTES:
        raise ValueError(f"token needs exactly {TOKEN_RANDOM_BYTES} random bytes")
    return TOKEN_PREFIX + base64.urlsafe_b64encode(random_bytes).rstrip(b"=").decode("ascii")


def is_token_format(value) -> bool:
    """是不是 32 字节的规范 base64url 编码（末位多出的两位须为 0）加 csk_ 前缀。"""
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        return False
    encoded = value[len(TOKEN_PREFIX) :]
    raw = base64.urlsafe_b64decode(encoded + "=")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") == encoded


def token_digest(token: str) -> str:
    """库里只存这个：令牌全文 UTF-8 的 SHA-256 小写十六进制。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def parse_authorization(header: str | None) -> str | None:
    """从 Authorization 头取出令牌；认不出时返回 None，不抛出。

    认不出包括：头缺少、方案不对、空格不对、令牌格式不对。
    方案名 Bearer 不区分大小写，与令牌之间正好一个空格；上层对 None 一律 401。
    """
    if not isinstance(header, str):
        return None
    scheme, separator, token = header.partition(" ")
    # 只比 ASCII：避免非 ASCII 字符经 lower() 变成拉丁字母混过去。
    if not separator or not scheme.isascii() or scheme.lower() != "bearer":
        return None
    if not is_token_format(token):
        return None
    return token


def is_token_usable(
    *,
    token_revoked_at: datetime | None,
    expires_at: datetime | None,
    credential_status: str,
    tenant_status: str,
    now: datetime,
) -> bool:
    """令牌可用：未吊销、凭据 active、租户 active、没有截止时间或 now 早于截止时间。

    now 是调用方传入的数据库时间；到点即失效（now == expires_at 时不可用）。
    """
    if token_revoked_at is not None:
        return False
    if credential_status != ACTIVE or tenant_status != ACTIVE:
        return False
    return expires_at is None or now < expires_at


def scopes_cover(granted: Collection[str], required: Collection[str]) -> bool:
    """授予的 scope 是否覆盖接口要求的全部 scope（SKU 合并要求两个，缺一不可）。

    要求为空说明接口没配 scope，按不覆盖处理，不放行。
    """
    if not required:
        return False
    return set(required) <= set(granted)


def validate_issued_scopes(scopes: Sequence[str], *, has_channel: bool) -> frozenset[str]:
    """签发凭据时的 scope 校验：非空、无重复、都是已知值。

    ops.read 只能单独出现，且凭据不能带销售入口（运维接口用独立凭据）。
    """
    if not scopes:
        raise InvalidScopes("a credential needs at least one scope")
    unknown = sorted(set(scopes) - SCOPES)
    if unknown:
        raise InvalidScopes(f"unknown scopes: {unknown}")
    if len(set(scopes)) != len(scopes):
        raise InvalidScopes("duplicate scopes")
    if OPS_READ in scopes:
        if len(scopes) != 1:
            raise InvalidScopes(f"{OPS_READ} cannot be combined with other scopes")
        if has_channel:
            raise InvalidScopes(f"a credential with {OPS_READ} cannot have a sales channel")
    return frozenset(scopes)


def parse_reason(value: str) -> str:
    """命令行的 --reason：去首尾空格后 1 到 500 个字符、单行、不含控制字符。"""
    reason = value.strip()
    if not reason:
        raise InvalidOption("reason must not be empty")
    if len(reason) > MAX_REASON_LENGTH:
        raise InvalidOption(f"reason must be at most {MAX_REASON_LENGTH} characters")
    # 与命令行的名称同一规则：Cc 含换行与制表符，Zl、Zp 是行与段分隔符，Cs 是孤立代理项。
    if any(unicodedata.category(char) in ("Cc", "Zl", "Zp", "Cs") for char in reason):
        raise InvalidOption("reason must be a single line without control characters")
    return reason


def parse_overlap_hours(value: str) -> int:
    """命令行的 --overlap-hours：0 到 168 的整数（0 表示旧令牌即刻失效）。"""
    if not isinstance(value, str) or not _OVERLAP_HOURS.fullmatch(value):
        raise InvalidOption(_OVERLAP_HOURS_MESSAGE)
    hours = int(value)
    if hours > MAX_OVERLAP_HOURS:
        raise InvalidOption(_OVERLAP_HOURS_MESSAGE)
    return hours


def rotation_deadline(rotated_at: datetime, overlap_hours: int) -> datetime:
    """旧令牌的截止时间：数据库时间的轮换时刻加重叠小时数。"""
    return rotated_at + timedelta(hours=overlap_hours)


def tokens_to_expire(
    others: Iterable[tuple[str, datetime | None]], deadline: datetime
) -> list[str]:
    """轮换时要把截止时间改成 deadline 的旧令牌 ID。

    others 是该凭据其余未吊销的令牌 (token_id, expires_at)，不含这次签发的新令牌。
    没有截止时间或截止时间晚于 deadline 的改；已更早到期的不动（不把截止时间往后推）。
    """
    return [
        token_id for token_id, expires_at in others if expires_at is None or expires_at > deadline
    ]


def authorize_sales_command(granted_pool_id: str | None) -> PoolDecision:
    """销售类命令不带 pool_id，池由销售入口决定：只看凭据有没有销售入口。

    granted_pool_id 是凭据销售入口绑定的池；没有销售入口或入口已停用时传 None。
    """
    return PoolDecision.FORBIDDEN if granted_pool_id is None else PoolDecision.ALLOWED


def authorize_pool(
    granted_pool_id: str | None, requested_pool_id: str, *, pool_in_tenant: bool
) -> PoolDecision:
    """带 pool_id 的命令按 §10 第 4 条的顺序判定。

    没有销售入口 403（先于租户判断，不暴露别的租户的池是否存在）；池不属于本租户 404；
    属于本租户但不是获准的池 403；否则允许。
    按 ID 指向池内对象的命令也用这个：requested_pool_id 是对象所属的池，
    pool_in_tenant 是对象是否属于本租户。
    """
    if granted_pool_id is None:
        return PoolDecision.FORBIDDEN
    if not pool_in_tenant:
        return PoolDecision.NOT_FOUND
    if requested_pool_id != granted_pool_id:
        return PoolDecision.FORBIDDEN
    return PoolDecision.ALLOWED
