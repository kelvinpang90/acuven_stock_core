"""对账查询接口 GET /v1/reconciliation/orders（契约 §5.9，§2.5、§10 第 4 条）。

要求 orders.read、不要求角色，不经幂等执行器，只做普通读。查询参数只收 changed_from、changed_to、
after、limit，各最多一次（规则同流水查询）。判定顺序：两个时间、limit、游标的格式不对
（含时间早于 2000 年）422；changed_from 不早于 changed_to 422；changed_to 晚于数据库时间减
300 秒 422（details 为 latest_changed_to）；游标与本次窗口不符 422。凭据没有销售入口或入口已停用时，
在这些 422 之后返回空数组，next_cursor 原样为 after。
判窗口用的数据库时间与本页的预留、订单行出自同一个 REPEATABLE READ、READ ONLY 的读快照；
读库在 app/services/reconciliation.py。
"""

import logging
from collections.abc import Callable

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import Connection
from sqlalchemy.exc import SQLAlchemyError

from app.api.auth import authorize, get_access
from app.api.errors import invalid_request, ok
from app.api.requests import discard_connection, request_connection, unavailable
from app.api.stock_queries import _invalid_parameter, _parameters
from app.domain.catalog import AFTER, LIMIT, InvalidParameter
from app.domain.reconciliation import (
    CHANGED_FROM,
    CHANGED_TO,
    ChangedToTooLate,
    check_window,
    format_reconciliation_cursor,
    match_reconciliation_cursor,
    parse_page_limit,
    parse_reconciliation_cursor,
    parse_window,
)
from app.domain.service_auth import ORDERS_READ
from app.services import reconciliation
from app.services.reconciliation import ReconciliationOrder

RECONCILIATION_PARAMETERS = (CHANGED_FROM, CHANGED_TO, AFTER, LIMIT)

_log = logging.getLogger("app.api")

router = APIRouter()
_READ = [Depends(authorize(ORDERS_READ))]


def _read[T](request: Request, query: Callable[[Connection], T]) -> T:
    """用本请求的连接读，读完（也包括查询报 422）回滚结束事务；数据库出错 503。"""
    connection = request_connection(request)
    try:
        try:
            return query(connection)
        finally:
            connection.rollback()
    except SQLAlchemyError:
        _log.warning(
            "database error during a reconciliation read (request_id=%s)",
            request.state.request_id,
            exc_info=True,
        )
        discard_connection(request)
        raise unavailable() from None


@router.get("/v1/reconciliation/orders", dependencies=_READ)
def list_reconciliation_orders(request: Request) -> JSONResponse:
    """按 (last_changed_at, reservation_id) 升序；返回 orders 与 next_cursor。

    next_cursor 为本页最后一项的游标；没有新记录时原样返回请求里的 after，没带则为 null。
    """
    values = _parameters(request, (), RECONCILIATION_PARAMETERS)
    after = values.get(AFTER)
    try:
        changed_from, changed_to = parse_window(values.get(CHANGED_FROM), values.get(CHANGED_TO))
        limit = parse_page_limit(values.get(LIMIT))
        cursor = parse_reconciliation_cursor(after)
    except InvalidParameter as error:
        raise _invalid_parameter(error) from None
    principal = get_access(request).principal

    def query(connection: Connection) -> list[ReconciliationOrder]:
        now = reconciliation.begin_snapshot(connection)
        try:
            check_window(changed_from, changed_to, now)
            match_reconciliation_cursor(cursor, changed_from, changed_to)
        except InvalidParameter as error:
            raise _invalid_parameter(error) from None
        except ChangedToTooLate as error:
            raise invalid_request(str(error), error.details) from None
        if principal.channel_id is None:
            # 查询限定在本入口：没有入口就没有自己的订单。
            return []
        return reconciliation.read_reconciliation_orders(
            connection,
            principal.tenant_id,
            principal.channel_id,
            changed_from,
            changed_to,
            cursor,
            limit,
            now,
        )

    orders = _read(request, query)
    next_cursor = after
    if orders:
        last = orders[-1]
        next_cursor = format_reconciliation_cursor(
            last.last_changed_at, last.reservation_id, changed_from, changed_to
        )
    return ok(
        request,
        {"orders": [order.representation for order in orders], "next_cursor": next_cursor},
    )
