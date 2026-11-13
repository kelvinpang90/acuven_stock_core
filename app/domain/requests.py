"""请求头的格式规则（契约 §7.1、§8.1、§10 第 3 条）。

不合格一律抛 InvalidHeader（带头名，上层回 400 INVALID_REQUEST）；不去空格、不改大小写、不猜。
空字符串是“带了但不合格”，不当作没带；没带由调用方传 None。
"""

import re
from dataclasses import dataclass

IDEMPOTENCY_KEY = "Idempotency-Key"
REQUEST_ID = "X-Request-ID"
ACTOR_REF = "X-Actor-Ref"
ACTOR_ROLE = "X-Actor-Role"

STOCK_OPERATOR = "stock_operator"
STOCK_ADMIN = "stock_admin"
CATALOG_ADMIN = "catalog_admin"
ACTOR_ROLES = frozenset({STOCK_OPERATOR, STOCK_ADMIN, CATALOG_ADMIN})

USER_AGENT_MAX_LENGTH = 200

# §7.1：1–128 个可见 ASCII 字符（0x21–0x7E，不含空格）。
_IDEMPOTENCY_KEY = re.compile(r"[\x21-\x7e]{1,128}")
# 第 1 节的引用格式：1–128 个 [A-Za-z0-9._:-]。
_REF = re.compile(r"[A-Za-z0-9._:-]{1,128}")


class InvalidHeader(ValueError):
    """请求头缺少（必带时）或格式不合格；header 是头名。"""

    def __init__(self, header: str):
        super().__init__(f"invalid {header} header")
        self.header = header


@dataclass(frozen=True)
class Actor:
    ref: str
    role: str


def _matches(pattern: re.Pattern, value) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def parse_idempotency_key(value: str | None) -> str:
    """写请求必带 Idempotency-Key：没带与不合格同样抛出。"""
    if not _matches(_IDEMPOTENCY_KEY, value):
        raise InvalidHeader(IDEMPOTENCY_KEY)
    return value


def parse_request_id(value: str | None) -> str | None:
    """X-Request-ID 可选：没带返回 None（由上层生成 req_ ID），带了就必须合格。"""
    if value is None:
        return None
    if not _matches(_REF, value):
        raise InvalidHeader(REQUEST_ID)
    return value


def parse_actor(ref: str | None, role: str | None) -> Actor | None:
    """两个操作者头要么都带、要么都不带；都不带返回 None。

    角色区分大小写，只接受三个值之一。
    """
    if ref is None and role is None:
        return None
    if not _matches(_REF, ref):
        raise InvalidHeader(ACTOR_REF)
    if role not in ACTOR_ROLES:
        raise InvalidHeader(ACTOR_ROLE)
    return Actor(ref, role)


def truncate_user_agent(value: str | None) -> str | None:
    """记入安全事件与进程日志前截断到 200 个字符（§7.2、§8.1）；不校验格式。"""
    if value is None:
        return None
    return value[:USER_AGENT_MAX_LENGTH]
