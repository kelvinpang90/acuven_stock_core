"""不变量自查 GET /v1/ops/invariants（契约 §4.4，§10 第 4 条）。

要求 ops.read（运维凭据，不带销售入口）、不要求角色，不经幂等执行器，不做池授权：
查凭据所属租户的全部池。不收任何查询参数，带了 422。只读，不改、不修任何东西；
查出违反也是 200，响应为 checked_at、ok、violation_count 与至多 100 项 violations。
每次自查在进程日志写一行租户、用时与违反项数，不写违反项的内容。
读库在 app/services/invariants.py；谁定时调它、怎么告警属于 OPS-04 的监控，不在这里。
"""

import logging
import time
from collections.abc import Callable

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import Connection
from sqlalchemy.exc import SQLAlchemyError

from app.api.auth import authorize, get_access
from app.api.errors import ok
from app.api.requests import discard_connection, request_connection, unavailable
from app.api.stock_queries import _parameters
from app.domain.service_auth import OPS_READ
from app.services import invariants
from app.services.stock import format_timestamp

_log = logging.getLogger("app.api")

router = APIRouter()
_READ = [Depends(authorize(OPS_READ))]


def _read[T](request: Request, query: Callable[[Connection], T]) -> T:
    """用本请求的连接读，读完回滚结束事务；数据库出错 503。"""
    connection = request_connection(request)
    try:
        try:
            return query(connection)
        finally:
            connection.rollback()
    except SQLAlchemyError:
        _log.warning(
            "database error during an invariant check (request_id=%s)",
            request.state.request_id,
            exc_info=True,
        )
        discard_connection(request)
        raise unavailable() from None


@router.get("/v1/ops/invariants", dependencies=_READ)
def check_invariants(request: Request) -> JSONResponse:
    _parameters(request, ())
    tenant_id = get_access(request).principal.tenant_id
    started = time.perf_counter()
    report = _read(request, lambda connection: invariants.check_invariants(connection, tenant_id))
    _log.info(
        "invariant check: tenant_id=%s duration_ms=%.1f violation_count=%d",
        tenant_id,
        (time.perf_counter() - started) * 1000,
        report.violation_count,
    )
    return ok(
        request,
        {
            "checked_at": format_timestamp(report.checked_at),
            "ok": report.violation_count == 0,
            "violation_count": report.violation_count,
            "violations": report.violations,
        },
    )
