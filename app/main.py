from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api.errors import install_error_handlers
from app.api.requests import RequestMiddleware
from app.config import Settings
from app.db import make_engine
from app.domain.environment import health_payload


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    engine = make_engine(settings.database_url) if settings.database_url else None
    app = FastAPI(title="Acuven Stock Core", docs_url=None, redoc_url=None, openapi_url=None)
    # 鉴权、授权与之后的命令按请求从这里取连接（app.api.requests.request_connection）。
    app.state.engine = engine
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
