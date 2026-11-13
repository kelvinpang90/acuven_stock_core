"""所有接口共用、与鉴权无关的请求处理（契约 §7.1、§8.1 检查顺序、§8.3）。

检查顺序（一个请求只报最先遇到的错误）：
  1–3 步在 RequestMiddleware 里、进入路由之前做：
      路由 404/405、X-Request-ID 格式 400、请求体大小 413；
      请求体在这里整个读进内存（最多 128 KiB），之后的依赖从内存读。
  4–6 步（鉴权、scope、其他请求头）是依赖，挂在路由装饰器的 dependencies=[...] 里：
      FastAPI 先解析装饰器的依赖、再解析参数里的依赖，所以它们先于第 7 步。
  7 步是参数里的依赖：有请求体的接口用 json_body(模型)，得到 ParsedBody
      （校验后的模型与原始 JSON 解析结果，后者不补默认值，供幂等摘要用）；
      没有请求体的接口挂 no_body。
  8–9 步（角色、池授权）放在第 7 步的参数之后。

每个请求结束时在 app.api.access 写一行 JSON 日志；不记 Authorization 头、令牌与请求体。
"""

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import ClassVar

from fastapi import Request
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.datastructures import Headers, MutableHeaders
from starlette.routing import Match
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.errors import (
    INVALID_REQUEST,
    METHOD_NOT_ALLOWED,
    NOT_FOUND,
    REQUEST_TOO_LARGE,
    ApiError,
    error_response,
    internal_error,
    invalid_request,
)
from app.domain.ids import new_id
from app.domain.requests import REQUEST_ID, InvalidHeader, parse_request_id, truncate_user_agent

MAX_BODY_BYTES = 128 * 1024
MAX_BATCH_ITEMS = 100
# 不校验 X-Request-ID 的路径：不合格时改用生成的 ID（§8.1）。
UNCHECKED_REQUEST_ID_PATHS = frozenset({"/healthz", "/readyz"})

_log = logging.getLogger("app.api")
access_log = logging.getLogger("app.api.access")
# uvicorn 只配置它自己的日志器；这里给 app.api 挂一个输出，进程日志才真的写出来。
if not _log.handlers:
    _log.addHandler(logging.StreamHandler())
_log.setLevel(logging.INFO)

_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def _too_large() -> ApiError:
    return ApiError(413, REQUEST_TOO_LARGE, "request is too large")


def _match_route(scope: Scope) -> tuple[Match, set[str]]:
    """第 1 步：FULL 为找到；PARTIAL 为方法不对（附允许的方法）；NONE 为路径不存在。"""
    allowed: set[str] = set()
    for route in scope["app"].router.routes:
        match, _ = route.matches(scope)
        if match == Match.FULL:
            return Match.FULL, allowed
        if match == Match.PARTIAL:
            allowed.update(getattr(route, "methods", None) or ())
    if allowed:
        return Match.PARTIAL, allowed
    return Match.NONE, allowed


def _valid_request_id(value: str | None) -> bool:
    try:
        parse_request_id(value)
    except InvalidHeader:
        return False
    return True


async def _read_body(headers: Headers, receive: Receive) -> bytes | None:
    """第 3 步：先按 Content-Length，没有时按实际读到的字节数；超过上限返回 None。"""
    length = headers.get("content-length")
    if length is not None and length.isdigit() and int(length) > MAX_BODY_BYTES:
        return None
    chunks = []
    size = 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


class RequestMiddleware:
    """请求 ID、第 1–3 步、未处理异常的 500 与进程日志。"""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        headers = Headers(scope=scope)
        supplied = headers.get(REQUEST_ID)
        supplied_ok = _valid_request_id(supplied)
        request_id = supplied if supplied_ok and supplied is not None else new_id("req")
        scope.setdefault("state", {})["request_id"] = request_id
        status: int | None = None

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID] = request_id
            await send(message)

        async def reply(error: ApiError) -> None:
            await error_response(request_id, error)(scope, receive, send_with_id)

        try:
            match, allowed = _match_route(scope)
            if match == Match.NONE:
                await reply(ApiError(404, NOT_FOUND, "no such route"))
                return
            if match == Match.PARTIAL:
                allow = {"Allow": ", ".join(sorted(allowed))}
                await reply(ApiError(405, METHOD_NOT_ALLOWED, "method not allowed", headers=allow))
                return
            if not supplied_ok and scope["path"] not in UNCHECKED_REQUEST_ID_PATHS:
                await reply(ApiError(400, INVALID_REQUEST, f"invalid {REQUEST_ID} header"))
                return
            body = await _read_body(headers, receive)
            if body is None:
                await reply(_too_large())
                return

            delivered = False

            async def replay() -> Message:
                nonlocal delivered
                if delivered:
                    return await receive()
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}

            try:
                await self.app(scope, replay, send_with_id)
            except Exception:
                _log.exception("unhandled error (request_id=%s)", request_id)
                if status is not None:
                    raise
                await reply(internal_error())
        finally:
            line = {
                "method": scope["method"],
                "path": scope["path"],
                "status": status,
                "request_id": request_id,
                "user_agent": truncate_user_agent(headers.get("user-agent")),
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            }
            access_log.info(json.dumps(line))


class RequestBody(BaseModel):
    """请求体模型的基类：禁止未知字段，类型严格匹配。

    按 JSON 的类型匹配、不做转换：整数字段不收字符串与布尔。

    子类在 batch_fields 里列出批量数组字段名（如 ("lines",)），各最多 100 个元素，超过 413。
    """

    model_config = ConfigDict(extra="forbid", strict=True)
    batch_fields: ClassVar[tuple[str, ...]] = ()


@dataclass(frozen=True)
class ParsedBody[M: RequestBody]:
    model: M
    raw: dict  # 调用方发来的 JSON 解析结果，不补默认值（§7.1 摘要用）


def _check_content_type(value: str | None) -> None:
    """只比较媒体类型、不分大小写；带 charset 时必须是 utf-8。"""
    if value is None:
        raise invalid_request("Content-Type must be application/json")
    media_type, *params = value.split(";")
    if media_type.strip().lower() != "application/json":
        raise invalid_request("Content-Type must be application/json")
    for param in params:
        name, _, param_value = param.partition("=")
        if name.strip().lower() == "charset" and param_value.strip().strip('"').lower() != "utf-8":
            raise invalid_request("request body must be UTF-8")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate key")
    return result


def _reject_number(value: str):
    raise ValueError(f"non-integer number {value}")


def _has_lone_surrogate(value) -> bool:
    # 不用递归：嵌套深度只受 json 解析器的上限约束。
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if _LONE_SURROGATE.search(item):
                return True
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, dict):
            stack.extend(item)
            stack.extend(item.values())
    return False


def _parse_json(body: bytes) -> dict:
    """第 7 步第一小步：只接受能确定摘要的 JSON 对象（§7.1）。"""
    try:
        # 不是 UTF-8 时 decode 抛 UnicodeDecodeError；带 BOM 的文本 json.loads 自己会拒绝；
        # 整数位数超过 Python 的上限时 int() 抛 ValueError；嵌套过深抛 RecursionError。
        parsed = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_float=_reject_number,
            parse_constant=_reject_number,
        )
    except (ValueError, RecursionError):
        raise invalid_request("request body is not valid JSON") from None
    if not isinstance(parsed, dict):
        raise invalid_request("request body must be a JSON object")
    if _has_lone_surrogate(parsed):
        raise invalid_request("request body contains a lone surrogate")
    return parsed


def _check_batch_sizes(model: type[RequestBody], raw: dict) -> None:
    """第二小步：批量数组超长 413；字段不是数组时不计数，留给字段校验。"""
    for field in model.batch_fields:
        value = raw.get(field)
        if isinstance(value, list) and len(value) > MAX_BATCH_ITEMS:
            raise _too_large()


def _validate[M: RequestBody](model: type[M], body: bytes, raw: dict) -> M:
    """第三小步：字段校验（JSON 模式的严格校验才按 JSON 的类型匹配，如枚举收字符串）。"""
    try:
        validated = model.model_validate_json(body)
    except ValidationError as exc:
        errors = [{"loc": list(e["loc"]), "type": e["type"]} for e in exc.errors()]
        raise invalid_request("request body validation failed", {"errors": errors}) from None
    lines = raw.get("lines")
    if isinstance(lines, list):
        seen = set()
        for line in lines:
            ref = line.get("line_ref") if isinstance(line, dict) else None
            if isinstance(ref, str):
                if ref in seen:
                    raise invalid_request("duplicate line_ref", {"line_ref": ref})
                seen.add(ref)
    return validated


def json_body[M: RequestBody](model: type[M]):
    """有请求体的接口的第 7 步依赖。

    参数写 Annotated[ParsedBody[M], Depends(json_body(M))]。
    """

    async def dependency(request: Request) -> ParsedBody[M]:
        body = await request.body()
        if not body:
            raise invalid_request("request body is required")
        _check_content_type(request.headers.get("content-type"))
        raw = _parse_json(body)
        _check_batch_sizes(model, raw)
        return ParsedBody(_validate(model, body, raw), raw)

    return dependency


async def no_body(request: Request) -> None:
    """没有请求体的接口的第 7 步依赖：空请求体跳过（摘要的 body 为 null），非空 422。"""
    if await request.body():
        raise invalid_request("this endpoint takes no request body")
