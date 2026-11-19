"""对账查询与待审列表不碰数据库的规则（契约 §5.9）。

查询参数不合格抛 InvalidParameter（带参数名，接口回 422 INVALID_REQUEST）；
窗口终点太晚抛 ChangedToTooLate（带这一刻允许的最晚终点，接口回 422 并放进 details）。
判定顺序（§5.9）由调用方分步调用：
先 parse_window、parse_page_limit 与两种游标的 parse，再 check_window，
最后 match_reconciliation_cursor / match_review_cursor。
时间一律是 UTC、不带时区的 datetime（同 app.domain.stock 的流水查询）。
"""

import hashlib
import re
from datetime import datetime, timedelta
from typing import NamedTuple

from app.domain.catalog import AFTER, InvalidField, InvalidParameter
from app.domain.ids import CROCKFORD_ALPHABET, ULID_LENGTH
from app.domain.reservations import LATE_PAYMENT_REVIEW, RESERVATION_ID_PREFIX
from app.domain.stock import parse_ledger_limit, parse_occurred_at

CHANGED_FROM = "changed_from"
CHANGED_TO = "changed_to"
STATUS = "status"
LATEST_CHANGED_TO = "latest_changed_to"

# 待审列表的 status 只接受这一个值（以后要列别的状态时按 §8.4 只做加法）。
REVIEW_STATUS = LATE_PAYMENT_REVIEW

# 窗口时间的下界：DATETIME 只保证 1000 年以后的值，契约取 2000 年。
WINDOW_MIN = datetime(2000, 1, 1)
# 窗口终点至少早于数据库时间这么多秒。
CHANGED_TO_DELAY_SECONDS = 300

CURSOR_PREFIX = "v1"
DIGEST_LENGTH = 16

_EPOCH = datetime(1970, 1, 1)
_MICROSECOND = timedelta(microseconds=1)
# 正整数，无前导零；datetime 到 9999 年的微秒时间戳不超过 18 位。
_TIMESTAMP = re.compile(r"[1-9][0-9]{0,17}")
_DIGEST = re.compile(r"[0-9a-f]{16}")
_RESERVATION_ID = re.compile(
    rf"{RESERVATION_ID_PREFIX}_[{re.escape(CROCKFORD_ALPHABET)}]{{{ULID_LENGTH}}}"
)


class ChangedToTooLate(ValueError):
    """changed_to 晚于数据库时间减 300 秒；latest_changed_to 是这一刻允许的最晚终点。"""

    def __init__(self, latest_changed_to: datetime):
        super().__init__(f"changed_to is later than {format_utc(latest_changed_to)}")
        self.latest_changed_to = latest_changed_to

    @property
    def details(self) -> dict:
        return {LATEST_CHANGED_TO: format_utc(self.latest_changed_to)}


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is not None:
        value = (value - value.utcoffset()).replace(tzinfo=None)
    return value


def format_utc(value: datetime) -> str:
    """写成 YYYY-MM-DDTHH:MM:SS.ffffffZ（同 ledger_filter_digest 里时间的写法）。"""
    return _to_utc(value).isoformat(timespec="microseconds") + "Z"


def is_reservation_id(value) -> bool:
    """rsv 加下划线加 26 个 Crockford Base32 大写字符。"""
    return isinstance(value, str) and _RESERVATION_ID.fullmatch(value) is not None


# ---- 对账窗口 ----


def parse_changed_at(value: str | None, parameter: str) -> datetime:
    """changed_from、changed_to：必填，规则同 occurred_at，且不早于 2000-01-01T00:00:00Z。

    返回 UTC、不带时区的 datetime；不合格按 parameter 报错。
    """
    if value is None:
        raise InvalidParameter(parameter)
    try:
        parsed = parse_occurred_at(value)
    except InvalidField:
        raise InvalidParameter(parameter) from None
    if parsed < WINDOW_MIN:
        raise InvalidParameter(parameter)
    return parsed


def parse_window(changed_from: str | None, changed_to: str | None) -> tuple[datetime, datetime]:
    """两个时间的格式；都不对时先报 changed_from。不比较先后（见 check_window）。"""
    return parse_changed_at(changed_from, CHANGED_FROM), parse_changed_at(changed_to, CHANGED_TO)


def latest_changed_to(now: datetime) -> datetime:
    """这一刻允许的最晚终点：数据库时间减 300 秒，UTC、不带时区。"""
    return _to_utc(now) - timedelta(seconds=CHANGED_TO_DELAY_SECONDS)


def check_window(changed_from: datetime, changed_to: datetime, now: datetime) -> None:
    """parse_window 之后判窗口；now 是数据库时间。

    changed_from 不早于 changed_to 按 changed_from 报 InvalidParameter（先判）；
    changed_to 晚于 now 减 300 秒抛 ChangedToTooLate。
    """
    if changed_from >= changed_to:
        raise InvalidParameter(CHANGED_FROM)
    latest = latest_changed_to(now)
    if changed_to > latest:
        raise ChangedToTooLate(latest)


def parse_page_limit(value: str | None) -> int:
    """两个接口的 limit 参数，规则同流水查询（§2.5）。"""
    return parse_ledger_limit(value)


def window_digest(changed_from: datetime, changed_to: datetime) -> str:
    """窗口摘要，16 个小写十六进制字符。拼法定下后不改。

    规范化后的 changed_from、changed_to 写成 YYYY-MM-DDTHH:MM:SS.ffffffZ 以换行符连接，
    UTF-8 编码后取 SHA-256 小写十六进制的前 16 个字符。
    """
    parts = (format_utc(changed_from), format_utc(changed_to))
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    return digest[:DIGEST_LENGTH]


# ---- 对账游标 ----


class ReconciliationCursor(NamedTuple):
    last_changed_at: datetime
    reservation_id: str
    digest: str


def _timestamp_us(value: datetime) -> int:
    return (_to_utc(value) - _EPOCH) // _MICROSECOND


def format_reconciliation_cursor(
    last_changed_at: datetime, reservation_id: str, changed_from: datetime, changed_to: datetime
) -> str:
    """v1、last_changed_at 的 UTC 微秒时间戳十进制、reservation_id、窗口摘要，以点连接。

    调用方当不透明值。
    """
    timestamp = _timestamp_us(last_changed_at)
    if timestamp < 1:
        raise ValueError(f"invalid last_changed_at: {last_changed_at!r}")
    if not is_reservation_id(reservation_id):
        raise ValueError(f"invalid reservation id: {reservation_id!r}")
    digest = window_digest(changed_from, changed_to)
    return f"{CURSOR_PREFIX}.{timestamp}.{reservation_id}.{digest}"


def parse_reconciliation_cursor(value: str | None) -> ReconciliationCursor | None:
    """after 参数的格式：不带为 None（从头开始）。

    前缀不是 v1、段数不是 4、时间戳不是无前导零的正十进制、
    reservation_id 不是预留 ID 的形状、摘要不是 16 个小写十六进制字符，都按 after 报错。
    不比对窗口（见 match_reconciliation_cursor）。
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidParameter(AFTER)
    parts = value.split(".")
    if len(parts) != 4:
        raise InvalidParameter(AFTER)
    prefix, timestamp, reservation_id, digest = parts
    if prefix != CURSOR_PREFIX or not _TIMESTAMP.fullmatch(timestamp):
        raise InvalidParameter(AFTER)
    if not is_reservation_id(reservation_id) or not _DIGEST.fullmatch(digest):
        raise InvalidParameter(AFTER)
    try:
        last_changed_at = _EPOCH + int(timestamp) * _MICROSECOND
    except OverflowError:
        raise InvalidParameter(AFTER) from None
    return ReconciliationCursor(last_changed_at, reservation_id, digest)


def match_reconciliation_cursor(
    cursor: ReconciliationCursor | None, changed_from: datetime, changed_to: datetime
) -> ReconciliationCursor | None:
    """摘要与本次窗口不符按 after 报错；符合时原样返回，None 也原样返回。"""
    if cursor is not None and cursor.digest != window_digest(changed_from, changed_to):
        raise InvalidParameter(AFTER)
    return cursor


# ---- 待审列表 ----


def parse_review_status(value: str | None) -> str:
    """status 参数：只接受 late_payment_review。"""
    if value != REVIEW_STATUS:
        raise InvalidParameter(STATUS)
    return value


def status_digest(status: str) -> str:
    """status 的 UTF-8 编码取 SHA-256 小写十六进制的前 16 个字符。拼法定下后不改。"""
    return hashlib.sha256(status.encode("utf-8")).hexdigest()[:DIGEST_LENGTH]


class ReviewCursor(NamedTuple):
    reservation_id: str
    digest: str


def format_review_cursor(reservation_id: str, status: str) -> str:
    """v1、reservation_id、status 摘要，以点连接；调用方当不透明值。"""
    if not is_reservation_id(reservation_id):
        raise ValueError(f"invalid reservation id: {reservation_id!r}")
    return f"{CURSOR_PREFIX}.{reservation_id}.{status_digest(status)}"


def parse_review_cursor(value: str | None) -> ReviewCursor | None:
    """after 参数的格式：不带为 None（从头开始）。

    前缀不是 v1、段数不是 3、reservation_id 不是预留 ID 的形状、
    摘要不是 16 个小写十六进制字符，都按 after 报错。不比对 status（见 match_review_cursor）。
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidParameter(AFTER)
    parts = value.split(".")
    if len(parts) != 3:
        raise InvalidParameter(AFTER)
    prefix, reservation_id, digest = parts
    if prefix != CURSOR_PREFIX or not is_reservation_id(reservation_id):
        raise InvalidParameter(AFTER)
    if not _DIGEST.fullmatch(digest):
        raise InvalidParameter(AFTER)
    return ReviewCursor(reservation_id, digest)


def match_review_cursor(cursor: ReviewCursor | None, status: str) -> ReviewCursor | None:
    """摘要与 status 不符按 after 报错；符合时原样返回，None 也原样返回。"""
    if cursor is not None and cursor.digest != status_digest(status):
        raise InvalidParameter(AFTER)
    return cursor
