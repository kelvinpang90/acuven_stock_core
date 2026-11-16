import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import NoReturn

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.api import (
    catalog,
    catalog_changes,
    catalog_imports,
    counts,
    stock_documents,
    stock_imports,
    stock_queries,
)
from app.api.errors import install_error_handlers
from app.api.requests import RequestMiddleware
from app.config import Settings
from app.db import make_engine
from app.domain.environment import health_payload
from app.services.instance import instance_check_error
from app.services.periodic import PeriodicTask
from app.services.security_events import purge_expired_security_events

SECURITY_EVENT_PURGE = "security-event-purge"


def _purge_security_events(engine: Engine) -> None:
    """定时任务的一次运行：从连接池取一个连接，清理满 1 年的安全事件（契约 7.2）。"""
    with engine.connect() as connection:
        purge_expired_security_events(connection)


def _refuse_to_start(reason: str) -> NoReturn:
    """启动核对没过（契约 6.1）：标准错误写一行原因，进程以非 0 退出。

    uvicorn 以工厂方式加载时，SystemExit 从 create_app 传出去，进程以 1 退出、不打印堆栈。
    """
    sys.stderr.write(f"error: refusing to start: {reason}\n")
    raise SystemExit(1)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    # 连接串必填（契约 0.11）；建好引擎之后、构造应用之前做启动核对。
    if not settings.database_url:
        _refuse_to_start(f"CORE_DATABASE_URL is empty; CORE_ENV is {settings.env}")
    try:
        engine = make_engine(settings.database_url)
    except Exception:
        _refuse_to_start(f"database configuration is invalid; CORE_ENV is {settings.env}")
    error = instance_check_error(engine, settings.env)
    if error is not None:
        engine.dispose()
        _refuse_to_start(error)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # uvicorn 单进程运行：进入时启动定时清理，应用关闭时停止。
        task = PeriodicTask(SECURITY_EVENT_PURGE, lambda: _purge_security_events(engine))
        task.start()
        app.state.security_event_purge = task
        try:
            yield
        finally:
            task.stop()

    app = FastAPI(
        title="Acuven Stock Core",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    # 鉴权、授权与之后的命令按请求从这里取连接（app.api.requests.request_connection）。
    app.state.engine = engine
    # 进入 lifespan 时设置。
    app.state.security_event_purge = None
    app.add_middleware(RequestMiddleware)
    install_error_handlers(app)
    app.include_router(catalog.router)
    app.include_router(catalog_imports.router)
    app.include_router(catalog_changes.router)
    app.include_router(stock_queries.router)
    app.include_router(stock_imports.router)
    app.include_router(stock_documents.router)
    app.include_router(counts.router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """存活检查：不碰数据库，返回线上 commit 与环境标记。"""
        return health_payload(settings.git_sha, settings.env)

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        """就绪检查：数据库能执行一条查询才算就绪。"""
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except SQLAlchemyError:
            return JSONResponse({"status": "unavailable", "reason": "database_unreachable"}, 503)
        return JSONResponse({"status": "ok"})

    return app
