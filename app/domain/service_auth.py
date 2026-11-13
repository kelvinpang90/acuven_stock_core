"""服务凭据的令牌、scope 与池授权规则（契约 §8.1、§10 第 4 条）。

这里只有不碰数据库的判断：按摘要查令牌、读凭据与租户状态由上层做，结果交给这里判定。
"""

import base64
import hashlib
import os
import re
from collections.abc import Collection, Sequence
from datetime import datetime
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


class InvalidScopes(ValueError):
    """签发凭据时给的 scope 不合格（命令行拒绝签发）。"""


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
