import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
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
    demo_resets,
    fulfilment,
    ops,
    reconciliation,
    reservations,
    sale_commits,
    sku_merges,
    stock_documents,
    stock_imports,
    stock_queries,
)
from app.api.catalog import format_timestamp
from app.api.errors import install_error_handlers
from app.api.requests import RequestMiddleware
from app.config import Settings
from app.db import make_engine
from app.domain.environment import DEMO, PRODUCTION, admin_enabled_error, health_payload
from app.domain.periodic import periodic_status
from app.services.instance import instance_check_error
from app.services.periodic import PeriodicTask, stop_all
from app.services.reservations import sweep_expired_reservations
from app.services.security_events import purge_expired_security_events

SECURITY_EVENT_PURGE = "security-event-purge"
RESERVATION_EXPIRY = "reservation-expiry"
# 到期扫描每 30 秒一次，启动后首次等 30 秒（契约 5.6）。
RESERVATION_EXPIRY_INTERVAL = 30.0

_log = logging.getLogger("app.api")


def _purge_security_events(engine: Engine) -> None:
    """定时任务的一次运行：从连接池取一个连接，清理满 1 年的安全事件（契约 7.2）。"""
    with engine.connect() as connection:
        purge_expired_security_events(connection)


def _close_expired_reservations(engine: Engine) -> None:
    """定时任务的一次运行：关掉已到期的 active 预留（契约 5.2 第 4 条、5.6）。

    连接的取用、隔离级别的设置与复原都在 sweep_expired_reservations 里。
    """
    sweep_expired_reservations(engine)


def _optional_timestamp(value: datetime | None) -> str | None:
    return None if value is None else format_timestamp(value)


def _refuse_to_start(reason: str) -> NoReturn:
    """启动核对没过（契约 6.1）：标准错误写一行原因，进程以非 0 退出。

    uvicorn 以工厂方式加载时，SystemExit 从 create_app 传出去，进程以 1 退出、不打印堆栈。
    """
    sys.stderr.write(f"error: refusing to start: {reason}\n")
    raise SystemExit(1)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    # Demo 实例不开后台（契约 6.1，Q-87）：连库之前判出。
    admin_error = admin_enabled_error(settings.env, settings.admin_enabled)
    if admin_error is not None:
        _refuse_to_start(admin_error)
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
    # 有效期只在启动时读，进程日志记下当前值（契约 5.6）。
    _log.info("reservation ttl: %d seconds", settings.reservation_ttl_seconds)

    def make_periodic_tasks() -> dict[str, PeriodicTask]:
        return {
            SECURITY_EVENT_PURGE: PeriodicTask(
                SECURITY_EVENT_PURGE, lambda: _purge_security_events(engine)
            ),
            RESERVATION_EXPIRY: PeriodicTask(
                RESERVATION_EXPIRY,
                lambda: _close_expired_reservations(engine),
                interval=RESERVATION_EXPIRY_INTERVAL,
                first_delay=RESERVATION_EXPIRY_INTERVAL,
            ),
        }

    # 任务在这里就建好：没进入 lifespan 时就绪检查照样列出它们（alive 为 false，契约 8.5）。
    periodic_tasks = make_periodic_tasks()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # uvicorn 单进程运行：进入时启动定时清理与到期扫描，应用关闭时一并停止。
        purge = periodic_tasks[SECURITY_EVENT_PURGE]
        expiry = periodic_tasks[RESERVATION_EXPIRY]
        try:
            purge.start()
            app.state.security_event_purge = purge
            expiry.start()
            app.state.reservation_expiry = expiry
            yield
        finally:
            try:
                # 一次停掉全部任务，共用一个截止时间（契约 8.6 第 5 条）；没启动的任务直接算停下。
                stop_all([expiry, purge])
            finally:
                # 线程只能启动一次：换上没启动的新任务，再次进入 lifespan 时启动它们。
                periodic_tasks.update(make_periodic_tasks())

    def periodic_payload() -> dict[str, object]:
        """就绪检查正文的 periodic（契约 8.5）：判定只用单调时钟，不碰数据库。"""
        states = sorted((task.state() for task in periodic_tasks.values()), key=lambda s: s.name)
        return {
            "status": periodic_status(state.stale for state in states),
            "tasks": [
                {
                    "name": state.name,
                    "alive": state.alive,
                    "last_success_at": _optional_timestamp(state.last_success_at),
                    "last_failure_at": _optional_timestamp(state.last_failure_at),
                    "stale": state.stale,
                }
                for state in states
            ],
        }

    app = FastAPI(
        title="Acuven Stock Core",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    # 鉴权、授权与之后的命令按请求从这里取连接（app.api.requests.request_connection）。
    app.state.engine = engine
    # 预留命令从这里取有效期（app.api.reservations）。
    app.state.reservation_ttl_seconds = settings.reservation_ttl_seconds
    # 后台开关：之后的后台登录、后台接口与平台任务按它挂路由与作业；/v1 仍只看 CORE_ENV。
    app.state.admin_enabled = settings.admin_enabled
    # 进入 lifespan 时设置。
    app.state.security_event_purge = None
    app.state.reservation_expiry = None
    app.add_middleware(RequestMiddleware)
    install_error_handlers(app)
    if settings.env == PRODUCTION:
        # 生产实例的 /v1 只挂不变量自查（契约 8.6 第 1 条、4.4）；别的 /v1 路径同不存在的
        # 路径一样 404、不鉴权。只看 CORE_ENV。
        app.include_router(ops.router)
    else:
        app.include_router(catalog.router)
        app.include_router(catalog_imports.router)
        app.include_router(catalog_changes.router)
        app.include_router(stock_queries.router)
        app.include_router(stock_imports.router)
        app.include_router(stock_documents.router)
        app.include_router(counts.router)
        app.include_router(sku_merges.router)
        app.include_router(reservations.router)
        app.include_router(sale_commits.router)
        app.include_router(fulfilment.router)
        app.include_router(reconciliation.router)
        app.include_router(ops.router)
    # Demo 重置只在 Demo 实例上存在；生产实例不挂，该路径同不存在的路径 404（契约 6.1）。
    if settings.env == DEMO:
        app.include_router(demo_resets.router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """存活检查：不碰数据库，返回线上 commit 与环境标记。"""
        return health_payload(settings.git_sha, settings.env)

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        """就绪检查：数据库能执行一条查询才算就绪；正文另带定时任务的状态，不影响状态码。"""
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except SQLAlchemyError:
            body = {"status": "unavailable", "reason": "database_unreachable"}
            return JSONResponse(body | {"periodic": periodic_payload()}, 503)
        return JSONResponse({"status": "ok", "periodic": periodic_payload()})

    return app
