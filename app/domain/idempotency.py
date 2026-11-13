"""幂等的纯规则（契约 §7.1）：规范化请求摘要、命名锁锁名、重放判定与保存范围。

取锁、查记录、写记录由上层在数据库里做；这里只算字符串与做判定。
"""

import hashlib
import json
from enum import StrEnum

REQUEST_IN_PROGRESS = "REQUEST_IN_PROGRESS"
IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"


class UndigestableInput(ValueError):
    """输入里有摘要无法确定的值（浮点数、NaN、Infinity、孤立代理项等）。

    这些在请求体校验时就该 422；走到这里说明上层漏了校验，是程序错误。
    """


class Decision(StrEnum):
    EXECUTE = "execute"  # 没有记录：执行命令
    REPLAY = "replay"  # 同键同内容：原样重放
    CONFLICT = "conflict"  # 同键不同内容：409 IDEMPOTENCY_CONFLICT


def _check(value) -> None:
    """只放行 JSON 解析结果里能确定摘要的类型：对象、数组、字符串、整数、布尔与 null。"""
    if value is None or isinstance(value, bool | int):
        return
    if isinstance(value, str):
        # Python 字符串里成对的代理项已合成一个码点，剩下的代理码点都是孤立的。
        if any("\ud800" <= ch <= "\udfff" for ch in value):
            raise UndigestableInput("string contains a lone surrogate")
        return
    if isinstance(value, list):
        for item in value:
            _check(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise UndigestableInput(f"object key must be a string, got {key!r}")
            _check(key)
            _check(item)
        return
    raise UndigestableInput(f"cannot digest {type(value).__name__} value {value!r}")


def _dumps(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _is_sortable_lines(lines) -> bool:
    return isinstance(lines, list) and all(
        isinstance(line, dict) and isinstance(line.get("line_ref"), str) for line in lines
    )


def canonical_request(path: dict[str, str], body) -> str:
    """§7.1 的规范化 JSON：{"path": 路径参数, "body": 请求体或 null}。

    只有请求体顶层的 lines、且元素都是带字符串 line_ref 的对象时，按 line_ref 排序；
    line_ref 相同时再按元素的规范化 JSON 排序，保证结果确定。更深层与其他数组保持原序。
    """
    _check(path)
    _check(body)
    if isinstance(body, dict) and _is_sortable_lines(body.get("lines")):
        lines = sorted(body["lines"], key=lambda line: (line["line_ref"], _dumps(line)))
        body = {**body, "lines": lines}
    return _dumps({"path": path, "body": body})


def request_digest(path: dict[str, str], body) -> str:
    """规范化请求摘要：canonical_request 的 UTF-8 SHA-256 小写十六进制。

    没有请求体时 body 传 None；请求体是调用方发来的 JSON 解析结果，不补默认值。
    """
    return hashlib.sha256(canonical_request(path, body).encode("utf-8")).hexdigest()


def lock_name(
    database: str, tenant_id: str, credential_id: str, operation: str, idempotency_key: str
) -> str:
    """MySQL 命名锁的锁名：五项以换行符连接后 UTF-8 的 SHA-256 小写十六进制，64 个字符。

    拼法定下后不改，否则新旧版本同时运行时互斥失效。任一项为空或含换行符时拒绝，
    以免两个不同的作用域拼出同一个锁名。
    """
    parts = (database, tenant_id, credential_id, operation, idempotency_key)
    for part in parts:
        if not isinstance(part, str) or not part or "\n" in part:
            raise ValueError(f"invalid lock name part: {part!r}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def decide(stored_digest: str | None, digest: str) -> Decision:
    """取锁之后查到的已有记录摘要（没有记录为 None）与本次摘要比较。"""
    if stored_digest is None:
        return Decision.EXECUTE
    if stored_digest == digest:
        return Decision.REPLAY
    return Decision.CONFLICT


def is_final_result(status: int, error_code: str | None = None) -> bool:
    """取锁之后命令返回的结果是否作为确定结果保存。

    2xx 与 4xx 都保存，包括按数据库状态判出的 404、409、422；不保存的是 429、5xx，
    以及 409 REQUEST_IN_PROGRESS 与 IDEMPOTENCY_CONFLICT。
    """
    if not 200 <= status <= 499 or status == 429:
        return False
    return not (status == 409 and error_code in (REQUEST_IN_PROGRESS, IDEMPOTENCY_CONFLICT))
