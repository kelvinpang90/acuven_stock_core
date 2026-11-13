"""响应封装与统一的业务异常（契约 §8.1、§8.3）。

成功：{"data": ..., "request_id": ...}；
错误：{"error": {"code", "message", "retryable", "details"}, "request_id"}。
request_id 由 requests.RequestMiddleware 放进 request.state，并与响应头 X-Request-ID 相同。
"""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

INVALID_REQUEST = "INVALID_REQUEST"
NOT_FOUND = "NOT_FOUND"
METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
REQUEST_TOO_LARGE = "REQUEST_TOO_LARGE"
INTERNAL_ERROR = "INTERNAL_ERROR"


class ApiError(Exception):
    """接口层统一的业务异常：抛出后按错误体返回，headers（如 Retry-After）原样进响应。"""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict | None = None,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}
        self.headers = headers


def invalid_request(message: str, details: dict | None = None) -> ApiError:
    """422 INVALID_REQUEST（请求体的各种不合格）。"""
    return ApiError(422, INVALID_REQUEST, message, details=details)


def ok(
    request: Request, data, status: int = 200, headers: dict[str, str] | None = None
) -> JSONResponse:
    """成功响应。"""
    body = {"data": data, "request_id": request.state.request_id}
    return JSONResponse(body, status, headers)


def error_response(request_id: str, error: ApiError) -> JSONResponse:
    body = {
        "error": {
            "code": error.code,
            "message": error.message,
            "retryable": error.retryable,
            "details": error.details,
        },
        "request_id": request_id,
    }
    return JSONResponse(body, error.status, error.headers)


def internal_error() -> ApiError:
    """未处理的异常：结果未知，可重试；不带异常文本与任何内部信息。"""
    return ApiError(500, INTERNAL_ERROR, "internal error", retryable=True)


async def _handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
    return error_response(request.state.request_id, exc)


async def _handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    # 路径参数、查询参数等由 FastAPI 校验的部分；只给位置与类型，不回显输入值。
    errors = [{"loc": list(e["loc"]), "type": e["type"]} for e in exc.errors()]
    error = invalid_request("request validation failed", {"errors": errors})
    return error_response(request.state.request_id, error)


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _handle_api_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
