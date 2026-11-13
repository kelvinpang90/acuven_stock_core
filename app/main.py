from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.api.errors import install_error_handlers
from app.api.requests import RequestMiddleware
from app.config import Settings
from app.db import make_engine
from app.domain.environment import health_payload
from app.services.periodic import PeriodicTask
from app.services.security_events import purge_expired_security_events

SECURITY_EVENT_PURGE = "security-event-purge"


def _purge_security_events(engine: Engine) -> None:
    """定时任务的一次运行：从连接池取一个连接，清理满 1 年的安全事件（契约 7.2）。"""
    with engine.connect() as connection:
        purge_expired_security_events(connection)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    engine = make_engine(settings.database_url) if settings.database_url else None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # 数据库已配置时才启动定时清理；uvicorn 单进程运行，应用关闭时停止。
        task = None
        if engine is not None:
            task = PeriodicTask(SECURITY_EVENT_PURGE, lambda: _purge_security_events(engine))
            task.start()
        app.state.security_event_purge = task
        try:
            yield
        finally:
            if task is not None:
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
    # 进入 lifespan 时设置；数据库未配置时为 None。
    app.state.security_event_purge = None
    app.add_middleware(RequestMiddleware)
    install_error_handlers(app)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """存活检查：不碰数据库，返回线上 commit 与环境标记。"""
        return health_payload(settings.git_sha, settings.env)

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        """就绪检查：数据库能执行一条查询才算就绪。"""
        if engine is None:
            return JSONResponse({"status": "unavailable", "reason": "database_not_configured"}, 503)
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except SQLAlchemyError:
            return JSONResponse({"status": "unavailable", "reason": "database_unreachable"}, 503)
        return JSONResponse({"status": "ok"})

    return app
