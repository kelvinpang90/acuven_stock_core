"""共用请求处理（契约 §8.1 检查顺序第 1、2、3、7 步，§8.3 错误封装）。

不需要数据库：测试给 create_app 返回的应用挂测试专用路由。
"""

import json
import logging
import re
from typing import Annotated

import pytest
from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.api.errors import ApiError, ok
from app.api.requests import ParsedBody, RequestBody, json_body, no_body
from app.config import Settings
from app.main import create_app

GENERATED_ID = re.compile(r"req_[0-9A-HJKMNP-TV-Z]{26}")
JSON = {"Content-Type": "application/json"}
SECRET = "secret-internal-detail"
OVER_LIMIT = 128 * 1024 + 1


class Line(RequestBody):
    line_ref: str
    quantity: int


class Command(RequestBody):
    batch_fields = ("lines",)

    lines: list[Line]
    note: str | None = None


CommandBody = Annotated[ParsedBody[Command], Depends(json_body(Command))]


@pytest.fixture
def client() -> TestClient:
    app = create_app(Settings(env="demo", git_sha="b" * 40, database_url=""))

    @app.get("/test/echo")
    def echo(request: Request) -> JSONResponse:
        return ok(request, {"echo": True})

    @app.post("/test/commands")
    def command(request: Request, body: CommandBody) -> JSONResponse:
        return ok(request, {"raw": body.raw, "note": body.model.note})

    @app.post("/test/commands/{command_id}/cancel", dependencies=[Depends(no_body)])
    def cancel(request: Request, command_id: str) -> JSONResponse:
        return ok(request, {"cancelled": command_id})

    @app.get("/test/boom")
    def boom() -> JSONResponse:
        raise RuntimeError(SECRET)

    @app.get("/test/busy")
    def busy() -> JSONResponse:
        headers = {"Retry-After": "1"}
        raise ApiError(409, "REQUEST_IN_PROGRESS", "in progress", retryable=True, headers=headers)

    return TestClient(app)


def lines(count: int) -> list[dict]:
    return [{"line_ref": f"line-{i}", "quantity": 1} for i in range(count)]


def assert_error(response, status: int, code: str) -> dict:
    """错误体形状固定；响应体的 request_id 与响应头相同。"""
    assert response.status_code == status
    body = response.json()
    assert set(body) == {"error", "request_id"}
    assert set(body["error"]) == {"code", "message", "retryable", "details"}
    assert body["error"]["code"] == code
    assert body["request_id"] == response.headers["X-Request-ID"]
    return body["error"]


# --- 请求 ID ---


def test_a_valid_request_id_is_echoed_in_header_and_body(client):
    response = client.get("/test/echo", headers={"X-Request-ID": "shop:order-1.retry_2"})
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "shop:order-1.retry_2"
    assert response.json() == {"data": {"echo": True}, "request_id": "shop:order-1.retry_2"}


def test_a_missing_request_id_is_generated_as_req_ulid(client):
    response = client.get("/test/echo")
    assert GENERATED_ID.fullmatch(response.headers["X-Request-ID"])
    assert response.json()["request_id"] == response.headers["X-Request-ID"]


@pytest.mark.parametrize("bad", ["has space", "", "a" * 129, "slash/inside"])
def test_an_invalid_request_id_is_400_with_a_generated_id(client, bad):
    response = client.get("/test/echo", headers={"X-Request-ID": bad})
    assert_error(response, 400, "INVALID_REQUEST")
    assert GENERATED_ID.fullmatch(response.headers["X-Request-ID"])


def test_healthz_ignores_an_invalid_request_id_and_keeps_its_body(client):
    response = client.get("/healthz", headers={"X-Request-ID": "has space"})
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "commit": "b" * 40, "env": "demo"}
    assert GENERATED_ID.fullmatch(response.headers["X-Request-ID"])


def test_readyz_ignores_an_invalid_request_id_and_keeps_its_body(client):
    response = client.get("/readyz", headers={"X-Request-ID": "has space"})
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "reason": "database_not_configured"}
    assert GENERATED_ID.fullmatch(response.headers["X-Request-ID"])


def test_healthz_echoes_a_valid_request_id(client):
    response = client.get("/healthz", headers={"X-Request-ID": "probe-1"})
    assert response.headers["X-Request-ID"] == "probe-1"


@pytest.mark.parametrize(
    ("method", "path", "status", "code"),
    [
        ("GET", "/nope", 404, "NOT_FOUND"),
        ("GET", "/test/commands", 405, "METHOD_NOT_ALLOWED"),
    ],
)
def test_routing_errors_echo_a_valid_request_id_and_generate_otherwise(
    client, method, path, status, code
):
    echoed = client.request(method, path, headers={"X-Request-ID": "caller-7"})
    assert_error(echoed, status, code)
    assert echoed.headers["X-Request-ID"] == "caller-7"

    # 第 1 步先于第 2 步：路由错误照常报，不报 400。
    generated = client.request(method, path, headers={"X-Request-ID": "has space"})
    assert_error(generated, status, code)
    assert GENERATED_ID.fullmatch(generated.headers["X-Request-ID"])


def test_an_unknown_path_is_404_before_the_size_check(client):
    response = client.post("/nope", content=b"x" * OVER_LIMIT, headers=JSON)
    assert_error(response, 404, "NOT_FOUND")


# --- 错误封装 ---


def test_success_body_is_data_plus_request_id(client):
    response = client.get("/test/echo")
    assert set(response.json()) == {"data", "request_id"}


def test_unknown_route_and_wrong_method(client):
    assert assert_error(client.get("/v1/nothing"), 404, "NOT_FOUND")["retryable"] is False
    response = client.get("/test/commands")
    assert assert_error(response, 405, "METHOD_NOT_ALLOWED")["retryable"] is False
    assert response.headers["Allow"] == "POST"


def test_an_unhandled_exception_is_500_without_the_exception_text(client):
    response = client.get("/test/boom")
    error = assert_error(response, 500, "INTERNAL_ERROR")
    assert error["retryable"] is True
    assert SECRET not in response.text
    assert "RuntimeError" not in response.text


def test_an_api_error_carries_its_headers_and_retryable_flag(client):
    response = client.get("/test/busy")
    error = assert_error(response, 409, "REQUEST_IN_PROGRESS")
    assert error["retryable"] is True
    assert response.headers["Retry-After"] == "1"


# --- 请求体 ---


def test_the_route_gets_the_raw_json_without_defaults(client):
    sent = {"lines": [{"line_ref": "b", "quantity": 2}]}
    response = client.post("/test/commands", json=sent)
    assert response.status_code == 200
    # 模型补了 note 的默认值，原始解析结果里没有：幂等摘要用的是后者。
    assert response.json()["data"] == {"raw": sent, "note": None}


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b'{"lines": [', id="not-json"),
        pytest.param(b"[]", id="top-level-array"),
        pytest.param(b'{"lines": [], "lines": []}', id="duplicate-key"),
        pytest.param(b'{"lines": [{"line_ref": "a", "quantity": 1.0}]}', id="decimal-point"),
        pytest.param(b'{"lines": [{"line_ref": "a", "quantity": 1e2}]}', id="exponent"),
        pytest.param(b'{"lines": [{"line_ref": "a", "quantity": NaN}]}', id="nan"),
        pytest.param(b'{"lines": [{"line_ref": "a", "quantity": Infinity}]}', id="infinity"),
        pytest.param(b'{"lines": [{"line_ref": "\\ud800", "quantity": 1}]}', id="lone-surrogate"),
        pytest.param(b'{"lines": [], "extra": 1}', id="unknown-field"),
        pytest.param(
            b'{"lines": [{"line_ref": "a", "quantity": 1}, {"line_ref": "a", "quantity": 2}]}',
            id="duplicate-line-ref",
        ),
        pytest.param(b'{"lines": [{"line_ref": "a", "quantity": "5"}]}', id="int-as-string"),
        pytest.param(b'{"lines": [{"line_ref": "a", "quantity": true}]}', id="int-as-bool"),
        pytest.param(b'{"lines": "x"}', id="lines-not-array"),
        pytest.param(b'\xff{"lines": []}', id="not-utf8"),
        pytest.param(b'\xef\xbb\xbf{"lines": []}', id="utf8-bom"),
        pytest.param(
            b'{"lines": [{"line_ref": "a", "quantity": ' + b"9" * 5000 + b"}]}",
            id="too-many-digits",
        ),
    ],
)
def test_invalid_bodies_are_422(client, content):
    response = client.post("/test/commands", content=content, headers=JSON)
    assert_error(response, 422, "INVALID_REQUEST")


def test_a_body_endpoint_rejects_an_empty_body(client):
    response = client.post("/test/commands", content=b"", headers=JSON)
    assert_error(response, 422, "INVALID_REQUEST")


@pytest.mark.parametrize("content_type", [None, "text/plain", "application/json; charset=latin-1"])
def test_a_body_endpoint_rejects_a_non_json_content_type(client, content_type):
    headers = {} if content_type is None else {"Content-Type": content_type}
    response = client.post("/test/commands", content=b'{"lines": []}', headers=headers)
    assert_error(response, 422, "INVALID_REQUEST")


@pytest.mark.parametrize("content_type", ["Application/JSON", "application/json; charset=UTF-8"])
def test_the_media_type_is_case_insensitive_and_may_carry_utf8_charset(client, content_type):
    headers = {"Content-Type": content_type}
    response = client.post("/test/commands", content=b'{"lines": []}', headers=headers)
    assert response.status_code == 200


def test_a_no_body_endpoint_accepts_an_empty_body(client):
    response = client.post("/test/commands/cmd-1/cancel")
    assert response.status_code == 200
    assert response.json()["data"] == {"cancelled": "cmd-1"}


def test_a_no_body_endpoint_rejects_a_non_empty_body(client):
    response = client.post("/test/commands/cmd-1/cancel", content=b"{}", headers=JSON)
    assert_error(response, 422, "INVALID_REQUEST")


def test_an_oversized_body_with_content_length_is_413(client):
    content = b'{"lines": [], "note": "' + b"x" * OVER_LIMIT + b'"}'
    response = client.post("/test/commands", content=content, headers=JSON)
    assert "content-length" in response.request.headers
    assert_error(response, 413, "REQUEST_TOO_LARGE")


def test_an_oversized_body_without_content_length_is_413(client):
    content = b'{"lines": [], "note": "' + b"x" * OVER_LIMIT + b'"}'
    response = client.post("/test/commands", content=iter([content]), headers=JSON)
    assert "content-length" not in response.request.headers
    assert_error(response, 413, "REQUEST_TOO_LARGE")


def test_a_batch_of_100_is_accepted(client):
    response = client.post("/test/commands", json={"lines": lines(100)})
    assert response.status_code == 200


def test_a_batch_of_101_is_413(client):
    response = client.post("/test/commands", json={"lines": lines(101)})
    assert_error(response, 413, "REQUEST_TOO_LARGE")


def test_batch_size_is_checked_before_unknown_fields(client):
    response = client.post("/test/commands", json={"lines": lines(101), "extra": 1})
    assert_error(response, 413, "REQUEST_TOO_LARGE")


# --- 进程日志 ---


def access_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "app.api.access"]


def test_each_request_writes_one_log_line_without_credentials_or_body(client, caplog):
    caplog.set_level(logging.INFO, logger="app.api.access")
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer csk_token-marker",
        "User-Agent": "u" * 300,
        "X-Request-ID": "log-test-1",
    }
    content = b'{"lines": [], "note": "body-marker"}'
    response = client.post("/test/commands?trace=query-marker", content=content, headers=headers)
    assert response.status_code == 200

    [line] = access_lines(caplog)
    entry = json.loads(line)
    assert entry.pop("duration_ms") >= 0
    assert entry == {
        "method": "POST",
        "path": "/test/commands",
        "status": 200,
        "request_id": "log-test-1",
        "user_agent": "u" * 200,
    }
    for marker in ("token-marker", "Bearer", "body-marker", "query-marker"):
        assert marker not in line


def test_rejected_requests_are_logged_too(client, caplog):
    caplog.set_level(logging.INFO, logger="app.api.access")
    response = client.get("/nope")
    [line] = access_lines(caplog)
    entry = json.loads(line)
    assert entry["status"] == 404
    assert entry["request_id"] == response.headers["X-Request-ID"]
