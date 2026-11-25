"""入口的种类、平台店铺 ID 与一个池的入口组合（契约第 3 节，0.22，Q-81）。

这里只有不碰数据库的判断：锁池行、读池里已有的入口由命令行做（app/cli/provisioning.py），
结果交给这里判定。
"""

import re
from collections.abc import Iterable

API = "api"
PLATFORM = "platform"
BACKOFFICE = "backoffice"
CHANNEL_KINDS = (API, PLATFORM, BACKOFFICE)

SHOPEE = "shopee"
TIKTOK_SHOP = "tiktok_shop"
PLATFORMS = (SHOPEE, TIKTOK_SHOP)

MAX_PLATFORM_SHOP_ID_LENGTH = 64
_PLATFORM_SHOP_ID = re.compile(r"[A-Za-z0-9._:-]{1,64}")

# 拒绝的原因会写到标准错误，所以是固定的英文，不带参数值。
POOL_HAS_API_CHANNEL = "pool already has an api channel; it cannot have other channels"
API_CHANNEL_NOT_ALONE = "an api channel must be the only channel of its pool"
POOL_HAS_BACKOFFICE_CHANNEL = "pool already has a backoffice channel"


def is_platform_shop_id(value: object) -> bool:
    """1 到 64 个 ASCII 字母、数字、点、下划线、冒号或连字符。"""
    return isinstance(value, str) and _PLATFORM_SHOP_ID.fullmatch(value) is not None


def combination_error(existing_kinds: Iterable[str], kind: str) -> str | None:
    """池里已有这些种类的入口（不论状态）时，能不能再开通一个 kind 的入口；能则返回 None。

    契约的两种组合（恰好一个 api；或一个以上 platform 加恰好一个 backoffice）
    是开通完成后的样子，入口只能逐个开通，所以按“开通之后仍能成为两种组合之一”判：
    已有 api 时不能加任何入口；
    已有任何入口时不能加 api；backoffice 最多一个；platform 可以多个。
    """
    existing = set(existing_kinds)
    if API in existing:
        return POOL_HAS_API_CHANNEL
    if kind == API and existing:
        return API_CHANNEL_NOT_ALONE
    if kind == BACKOFFICE and BACKOFFICE in existing:
        return POOL_HAS_BACKOFFICE_CHANNEL
    return None
