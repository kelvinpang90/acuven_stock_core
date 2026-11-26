"""只读排查：流水、预留与订单库存（契约 8.6 第 1 条、4.4 的 0.22 说明）。

生产实例的 /v1 只挂不变量自查，运维在容器里用这三个子命令查同样的内容：
inspect ledger 对应 GET /v1/ledger，inspect reservation 对应 GET /v1/reservations/{id}，
inspect order-stock 对应 GET /v1/orders/{order_ref}/stock。输出的 JSON 与对应读接口响应里的
data 相同；读库、表示与参数解析都复用那三个接口用的函数，参数规则与之相同。
--tenant 限定租户；订单引用按入口区分，所以 order-stock 另要 --channel。

只读：不写库、不写安全事件、不用令牌，读完回滚。找不到池、SKU、预留或订单（不存在或属于
别的租户，不区分）以退出码 2 结束，说明不带参数值。
"""

from argparse import Namespace
from typing import Any

from sqlalchemy import Connection

from app.api.errors import ApiError
from app.cli import CliError
from app.domain.catalog import AFTER, InvalidParameter
from app.domain.fulfilment import parse_order_stock_cursor
from app.domain.requests import is_reference
from app.domain.stock import (
    format_ledger_cursor,
    parse_ledger_cursor,
    parse_ledger_limit,
    parse_ledger_range,
)
from app.services import catalog, fulfilment, reservations, stock_queries
from app.services.stock import entry_representation

NOT_FOUND = 404


def _invalid(error: InvalidParameter) -> CliError:
    return CliError(f"invalid {error.parameter} parameter")


def inspect_ledger(conn: Connection, args: Namespace) -> dict[str, Any]:
    """同 GET /v1/ledger：先校验参数，再查池与 SKU。"""
    tenant_id, pool_id, sku_id, after = args.tenant, args.pool, args.sku, args.after
    try:
        start, end = parse_ledger_range(args.from_, args.to)
        after_version = parse_ledger_cursor(after, pool_id, sku_id, start, end)
        limit = parse_ledger_limit(args.limit)
    except InvalidParameter as error:
        raise _invalid(error) from None
    if stock_queries.pool_owner(conn, pool_id) != tenant_id:
        raise CliError("pool not found for this tenant")
    if catalog.sku_owner(conn, sku_id) != tenant_id:
        raise CliError("sku not found for this tenant")
    rows = stock_queries.read_ledger(
        conn,
        tenant_id,
        pool_id,
        sku_id,
        start=start,
        end=end,
        after_version=after_version,
        limit=limit,
    )
    conn.rollback()
    entries = [entry_representation(row) for row in rows]
    next_cursor = after
    if rows:
        last = rows[-1]["balance_version_after"]
        next_cursor = format_ledger_cursor(last, pool_id, sku_id, start, end)
    return {"entries": entries, "next_cursor": next_cursor}


def inspect_reservation(conn: Connection, args: Namespace) -> dict[str, Any]:
    """同 GET /v1/reservations/{reservation_id}。"""
    found = reservations.read_reservation(conn, args.tenant, args.reservation_id)
    if found is None:
        raise CliError("reservation not found for this tenant")
    data = reservations.current_representation(conn, args.tenant, found)
    conn.rollback()
    return data


def inspect_order_stock(conn: Connection, args: Namespace) -> dict[str, Any]:
    """同 GET /v1/orders/{order_ref}/stock，销售入口取 --channel。

    判定顺序同接口：order_ref、limit、after 的格式，再查订单，游标不属于本订单最后判。
    read_order_stock 先设下一个事务的隔离级别，所以它是本事务里的第一条语句。
    """
    if not is_reference(args.order_ref):
        raise CliError("invalid order_ref parameter")
    after = args.after
    try:
        limit = parse_ledger_limit(args.limit)
        cursor = None if after is None else parse_order_stock_cursor(after)
    except InvalidParameter as error:
        raise _invalid(error) from None
    try:
        data = fulfilment.read_order_stock(
            conn, args.tenant, args.channel, args.order_ref, after, cursor, limit
        )
    except ApiError as error:
        if error.status == NOT_FOUND:
            raise CliError("order not found for this tenant and channel") from None
        raise CliError(f"invalid {error.details.get('parameter', AFTER)} parameter") from None
    conn.rollback()
    return data


def register(subparsers: Any) -> None:
    inspect = subparsers.add_parser(
        "inspect", help="read-only lookups (same data as the /v1 read endpoints)"
    )
    inspect_actions = inspect.add_subparsers(dest="action", required=True)

    ledger = inspect_actions.add_parser(
        "ledger", help="ledger entries of one SKU in one pool (as GET /v1/ledger)"
    )
    ledger.add_argument("--tenant", required=True, help="tenant ID")
    ledger.add_argument("--pool", required=True, help="pool ID")
    ledger.add_argument("--sku", required=True, help="SKU ID")
    ledger.add_argument("--from", dest="from_", help="recorded at or after (RFC 3339)")
    ledger.add_argument("--to", help="recorded before (RFC 3339)")
    ledger.add_argument("--after", help="next_cursor of the previous page")
    ledger.add_argument("--limit", help="1 to 100, default 100")
    ledger.set_defaults(handler=inspect_ledger)

    reservation = inspect_actions.add_parser(
        "reservation", help="one reservation (as GET /v1/reservations/{id})"
    )
    reservation.add_argument("--tenant", required=True, help="tenant ID")
    reservation.add_argument("--reservation-id", required=True, help="reservation ID")
    reservation.set_defaults(handler=inspect_reservation)

    order_stock = inspect_actions.add_parser(
        "order-stock", help="an order's stock and documents (as GET /v1/orders/{ref}/stock)"
    )
    order_stock.add_argument("--tenant", required=True, help="tenant ID")
    order_stock.add_argument("--channel", required=True, help="sales channel ID of the order")
    order_stock.add_argument("--order-ref", required=True, help="order reference")
    order_stock.add_argument("--after", help="next_cursor of the previous page")
    order_stock.add_argument("--limit", help="1 to 100, default 100")
    order_stock.set_defaults(handler=inspect_order_stock)
