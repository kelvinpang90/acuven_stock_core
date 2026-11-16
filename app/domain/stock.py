"""库存命令与查询不碰数据库的规则（契约 §6.1、§6.2 第 5、6 条、§6.8）。

字段取值不合规抛 InvalidField（带字段名，接口回 422）；
余额与流水查询的参数不合格抛 InvalidParameter（接口回 422 INVALID_REQUEST）。
"""

import hashlib
import re
from datetime import date, datetime, timedelta

from app.domain.catalog import (
    AFTER,
    CURSOR_MAX,
    InvalidField,
    InvalidParameter,
    normalize_reason,
    parse_limit,
)

REASON_CODE = "reason_code"
NOTE = "note"
OCCURRED_AT = "occurred_at"
QUANTITY = "quantity"
BUSINESS_DATE = "business_date"

SKU_IDS = "sku_ids"
FROM = "from"
TO = "to"

QUANTITY_MAX = 1000000
MAX_SKU_IDS = 100
# DATETIME(6) 存 1000–9999 年；两头各留一年，换算时区后不会越界。
MIN_YEAR = 1001
MAX_YEAR = 9998
# DATE 存 1000–9999 年；business_date 不换算时区，不留余量。
DATE_MIN_YEAR = 1000
DATE_MAX_YEAR = 9999

LEDGER_CURSOR_PREFIX = "v1"
LEDGER_DIGEST_LENGTH = 16

_REASON_CODE = re.compile(r"[a-z0-9_]{1,32}")
# 日期 T 时间（秒必须有）、0–6 位小数秒、Z 或 ±hh:mm；T 与 Z 只认大写，数字只认 ASCII。
_TIMESTAMP = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.([0-9]{1,6}))?"
    r"(?:Z|([+-])([0-9]{2}):([0-9]{2}))"
)
# 只认 ASCII 数字，月日两位。
_DATE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
_VERSION = re.compile(r"[1-9][0-9]{0,18}")
_DIGEST = re.compile(r"[0-9a-f]{16}")


# ---- 公共字段（§6.2 第 6 条） ----


def normalize_reason_code(value) -> str:
    """1–32 个小写字母、数字或下划线；不去空白，不接受 None（可选时由调用方判断带没带）。"""
    if not isinstance(value, str) or not _REASON_CODE.fullmatch(value):
        raise InvalidField(REASON_CODE)
    return value


def normalize_note(value) -> str | None:
    """规则同 §2.6 第 1 条的 reason：None，或去首尾空白后 1–500 个字符、不含控制字符。"""
    try:
        return normalize_reason(value)
    except InvalidField:
        raise InvalidField(NOTE) from None


def _parse_timestamp(value) -> datetime | None:
    """RFC 3339 时间换算成 UTC、不带时区的 datetime（微秒精度）；不合规为 None。"""
    if not isinstance(value, str):
        return None
    match = _TIMESTAMP.fullmatch(value)
    if match is None:
        return None
    year, month, day, hour, minute, second, fraction, sign, offset_h, offset_m = match.groups()
    if not MIN_YEAR <= int(year) <= MAX_YEAR:
        return None
    try:
        # 月、日、时、分、秒的范围由 datetime 把关，秒为 60（闰秒）也在这里被拒。
        local = datetime(
            int(year),
            int(month),
            int(day),
            int(hour),
            int(minute),
            int(second),
            int((fraction or "").ljust(6, "0")),
        )
    except ValueError:
        return None
    if sign is None:
        return local
    if int(offset_h) > 23 or int(offset_m) > 59:
        return None
    offset = timedelta(hours=int(offset_h), minutes=int(offset_m))
    return local - offset if sign == "+" else local + offset


def parse_occurred_at(value) -> datetime:
    """带时区的 RFC 3339 时间，返回 UTC、不带时区的 datetime。

    日期加大写 T 加时间，秒必须有，小数秒 0–6 位，时区为大写 Z 或 ±hh:mm；
    秒为 00–59，年份为 1001–9998。不接受 None（不带时由调用方取数据库时间）。
    """
    parsed = _parse_timestamp(value)
    if parsed is None:
        raise InvalidField(OCCURRED_AT)
    return parsed


# ---- Demo 重置的日期（§6.1） ----


def parse_business_date(value) -> date:
    """YYYY-MM-DD 形式的合法日历日期，年份 1000–9999（MySQL DATE 的范围）。

    不拿它与当前日期比较。不合规抛 InvalidField（ValueError 的子类）。
    """
    if not isinstance(value, str):
        raise InvalidField(BUSINESS_DATE)
    match = _DATE.fullmatch(value)
    if match is None:
        raise InvalidField(BUSINESS_DATE)
    year, month, day = (int(part) for part in match.groups())
    if not DATE_MIN_YEAR <= year <= DATE_MAX_YEAR:
        raise InvalidField(BUSINESS_DATE)
    try:
        return date(year, month, day)
    except ValueError:
        raise InvalidField(BUSINESS_DATE) from None


# ---- 件数（§6.2 第 5 条） ----


def _is_int(value) -> bool:
    # bool 是 int 的子类，JSON 的 true 不能当 1 件。
    return isinstance(value, int) and not isinstance(value, bool)


def is_positive_quantity(value) -> bool:
    """收货与不可售调整的件数：1–1,000,000 的整数。"""
    return _is_int(value) and 1 <= value <= QUANTITY_MAX


def is_non_negative_quantity(value) -> bool:
    """期初数量与盘点实数：0–1,000,000 的整数。"""
    return _is_int(value) and 0 <= value <= QUANTITY_MAX


def parse_positive_quantity(value, field: str = QUANTITY) -> int:
    if not is_positive_quantity(value):
        raise InvalidField(field)
    return value


def parse_non_negative_quantity(value, field: str = QUANTITY) -> int:
    if not is_non_negative_quantity(value):
        raise InvalidField(field)
    return value


# ---- 余额查询（§6.8） ----


def parse_sku_ids(value: str | None) -> list[str]:
    """sku_ids 参数：逗号分隔的 1–100 个，不能有空项、不能重复；不校验 ID 格式。

    必填，不带也不合格。返回按参数里的顺序。
    """
    if not isinstance(value, str):
        raise InvalidParameter(SKU_IDS)
    sku_ids = value.split(",")
    if len(sku_ids) > MAX_SKU_IDS or "" in sku_ids or len(set(sku_ids)) != len(sku_ids):
        raise InvalidParameter(SKU_IDS)
    return sku_ids


# ---- 流水查询（§6.8） ----


def _parse_bound(value: str | None, parameter: str) -> datetime | None:
    if value is None:
        return None
    parsed = _parse_timestamp(value)
    if parsed is None:
        raise InvalidParameter(parameter)
    return parsed


def parse_ledger_range(
    from_: str | None, to: str | None
) -> tuple[datetime | None, datetime | None]:
    """from、to 参数：可选，规则同 occurred_at，返回 UTC、不带时区的 datetime。

    两个都带而 from 不早于 to 时按 from 报错。
    """
    start = _parse_bound(from_, FROM)
    end = _parse_bound(to, TO)
    if start is not None and end is not None and start >= end:
        raise InvalidParameter(FROM)
    return start, end


def parse_ledger_limit(value: str | None) -> int:
    """limit 参数，规则同 §2.5。"""
    return parse_limit(value)


def _format_bound(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is not None:
        value = (value - value.utcoffset()).replace(tzinfo=None)
    return value.isoformat(timespec="microseconds") + "Z"


def ledger_filter_digest(
    pool_id: str, sku_id: str, from_: datetime | None, to: datetime | None
) -> str:
    """筛选条件摘要，16 个小写十六进制字符。拼法定下后不改。

    pool_id、sku_id 与写成 YYYY-MM-DDTHH:MM:SS.ffffffZ 的 from、to（没带为空串）以换行符连接，
    UTF-8 编码后取 SHA-256 小写十六进制的前 16 个字符。from、to 传 parse_ledger_range 的结果。
    """
    parts = (pool_id, sku_id, _format_bound(from_), _format_bound(to))
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    return digest[:LEDGER_DIGEST_LENGTH]


def format_ledger_cursor(
    version: int, pool_id: str, sku_id: str, from_: datetime | None, to: datetime | None
) -> str:
    """v1 加点加 balance_version_after 的十进制加点加筛选条件摘要；调用方当不透明值。"""
    if not _is_int(version) or not 1 <= version <= CURSOR_MAX:
        raise ValueError(f"invalid balance version: {version!r}")
    digest = ledger_filter_digest(pool_id, sku_id, from_, to)
    return f"{LEDGER_CURSOR_PREFIX}.{version}.{digest}"


def parse_ledger_cursor(
    value: str | None, pool_id: str, sku_id: str, from_: datetime | None, to: datetime | None
) -> int | None:
    """after 参数：不带为 None（从头开始）；带时返回游标里的 balance_version_after。

    格式不对（前缀、段数、版本不是 1–9223372036854775807 的无前导零十进制、
    摘要不是 16 个小写十六进制字符），或摘要与本次筛选条件不符，都按 after 报错。
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidParameter(AFTER)
    parts = value.split(".")
    if len(parts) != 3:
        raise InvalidParameter(AFTER)
    prefix, version, digest = parts
    if prefix != LEDGER_CURSOR_PREFIX or not _VERSION.fullmatch(version):
        raise InvalidParameter(AFTER)
    if not _DIGEST.fullmatch(digest) or int(version) > CURSOR_MAX:
        raise InvalidParameter(AFTER)
    if digest != ledger_filter_digest(pool_id, sku_id, from_, to):
        raise InvalidParameter(AFTER)
    return int(version)
