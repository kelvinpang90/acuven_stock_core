"""发货、退款回库、退货验收与订单库存查询不碰数据库的规则（契约 §5.8，§5.1）。

锁行、读写订单行与单据、落账由上层在数据库里做；这里只放常量与判定。
流水类型沿用 ledger，件数校验沿用 stock，游标上限沿用 catalog。
"""

import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

from app.domain.catalog import AFTER, CURSOR_MAX, InvalidParameter
from app.domain.ledger import EntryType
from app.domain.stock import QUANTITY_MAX, is_non_negative_quantity, is_positive_quantity

# ---- 单据类型（§5.8 存放、流水） ----

DISPATCH = "dispatch"
REFUND_RESTOCK = "refund_restock"
RETURN_RECEIPT = "return_receipt"
DOCUMENT_TYPES = (DISPATCH, REFUND_RESTOCK, RETURN_RECEIPT)

# 单据类型也是流水的 source_type（§5.8 流水）。
SOURCE_TYPES = DOCUMENT_TYPES

# document_id 的前缀，配 ids.new_id 用（得到 shp_…、rfr_…、rtn_…）。
DOCUMENT_ID_PREFIXES = {
    DISPATCH: "shp",
    REFUND_RESTOCK: "rfr",
    RETURN_RECEIPT: "rtn",
}

# 请求正文与单据表示里的业务引用字段。
REF_FIELDS = {
    DISPATCH: "shipment_ref",
    REFUND_RESTOCK: "refund_ref",
    RETURN_RECEIPT: "return_ref",
}

# 409 BUSINESS_REF_CONFLICT 的 details.ref_type。
REF_TYPES = {
    DISPATCH: "shipment",
    REFUND_RESTOCK: "refund",
    RETURN_RECEIPT: "return",
}


def _require_document_type(document_type):
    if document_type not in DOCUMENT_TYPES:
        raise ValueError(f"unknown document type: {document_type!r}")


# ---- 订单行与请求行 ----


@dataclass(frozen=True)
class OrderLineTotals:
    """一行订单行的下单数量与 §5.1 的五个累计量。"""

    line_ref: str
    sku_id: str
    quantity: int
    committed_qty: int
    dispatched_qty: int
    refund_restocked_qty: int
    returned_sellable_qty: int
    returned_unsellable_qty: int


@dataclass(frozen=True)
class QuantityLine:
    """发货与退款回库请求的一行。"""

    line_ref: str
    quantity: int


@dataclass(frozen=True)
class ReturnLine:
    """退货验收请求的一行。"""

    line_ref: str
    sellable_quantity: int
    unsellable_quantity: int


def line_limit(document_type: str, totals: OrderLineTotals) -> int:
    """这一行当前的余量（§5.8 各命令的“余量”）；未知类型抛 ValueError。"""
    _require_document_type(document_type)
    if document_type == RETURN_RECEIPT:
        returned = totals.returned_sellable_qty + totals.returned_unsellable_qty
        return totals.dispatched_qty - returned
    return totals.committed_qty - totals.dispatched_qty - totals.refund_restocked_qty


def parse_return_quantities(sellable, unsellable) -> tuple[int, int]:
    """退货验收一行的两个数：都是 0–1,000,000 的整数（布尔不算），和至少 1。

    不合规抛 ValueError（接口回 422）；合规返回 (sellable, unsellable)。
    """
    if not is_non_negative_quantity(sellable) or not is_non_negative_quantity(unsellable):
        raise ValueError(
            f"return quantities must be integers 0..{QUANTITY_MAX}: {sellable!r}, {unsellable!r}"
        )
    if sellable + unsellable < 1:
        raise ValueError("return quantities must add up to at least 1")
    return sellable, unsellable


def requested_quantity(document_type: str, line: QuantityLine | ReturnLine) -> int:
    """本行件数：发货与退款回库为 quantity，退货验收为两个数之和。

    行的形状与类型不符、件数不合规或类型未知时抛 ValueError。
    """
    _require_document_type(document_type)
    if document_type == RETURN_RECEIPT:
        if not isinstance(line, ReturnLine):
            raise ValueError(f"{document_type} needs a ReturnLine, got {line!r}")
        sellable, unsellable = parse_return_quantities(
            line.sellable_quantity, line.unsellable_quantity
        )
        return sellable + unsellable
    if not isinstance(line, QuantityLine):
        raise ValueError(f"{document_type} needs a QuantityLine, got {line!r}")
    if not is_positive_quantity(line.quantity):
        raise ValueError(f"quantity must be an integer 1..{QUANTITY_MAX}, got {line.quantity!r}")
    return line.quantity


# ---- 判定（§5.8 事务里的第 3、4 步） ----


def unknown_line_refs(
    request_line_refs: Iterable[str], order_line_refs: Iterable[str]
) -> list[str]:
    """请求里有、订单里没有的 line_ref，升序；即 RESERVATION_MISMATCH 的 details.line_refs。"""
    return sorted(set(request_line_refs) - set(order_line_refs))


def exceeding_lines(
    document_type: str,
    request_lines: Sequence[QuantityLine | ReturnLine],
    order_lines: Iterable[OrderLineTotals],
) -> list[dict]:
    """件数超过余量的行，按请求顺序；即 QUANTITY_EXCEEDS_OUTSTANDING 的 details.lines。

    每项 line_ref、sku_id（订单行的）、quantity（退货验收为两个数之和）与 limit。
    请求里有订单没有的 line_ref 时抛 ValueError（调用方先查 unknown_line_refs）。
    """
    by_ref = {totals.line_ref: totals for totals in order_lines}
    exceeding = []
    for line in request_lines:
        totals = by_ref.get(line.line_ref)
        if totals is None:
            raise ValueError(f"line_ref {line.line_ref!r} is not on the order")
        quantity = requested_quantity(document_type, line)
        limit = line_limit(document_type, totals)
        if quantity > limit:
            exceeding.append(
                {
                    "line_ref": line.line_ref,
                    "sku_id": totals.sku_id,
                    "quantity": quantity,
                    "limit": limit,
                }
            )
    return exceeding


# ---- 执行（§5.8 事务里的第 5 步与各命令） ----


def line_entries(
    document_type: str, line: QuantityLine | ReturnLine
) -> list[tuple[EntryType, int]]:
    """本行要写的流水，按写入顺序给出 (流水类型, 件数)。

    发货一条 DISPATCH，退款回库一条 REFUND_RESTOCK；
    退货验收可售大于 0 时一条 RETURN_SELLABLE，不可售大于 0 时再一条 RETURN_UNSELLABLE。
    """
    requested_quantity(document_type, line)
    if document_type == DISPATCH:
        return [(EntryType.DISPATCH, line.quantity)]
    if document_type == REFUND_RESTOCK:
        return [(EntryType.REFUND_RESTOCK, line.quantity)]
    entries = []
    if line.sellable_quantity > 0:
        entries.append((EntryType.RETURN_SELLABLE, line.sellable_quantity))
    if line.unsellable_quantity > 0:
        entries.append((EntryType.RETURN_UNSELLABLE, line.unsellable_quantity))
    return entries


def apply_line(
    document_type: str, totals: OrderLineTotals, line: QuantityLine | ReturnLine
) -> OrderLineTotals:
    """返回加上本行件数之后的累计量，不改原值。

    line_ref 不符、件数超过余量（§5.1 的约束会被打破）时抛 ValueError。
    """
    if line.line_ref != totals.line_ref:
        raise ValueError(f"line_ref {line.line_ref!r} does not match {totals.line_ref!r}")
    quantity = requested_quantity(document_type, line)
    limit = line_limit(document_type, totals)
    if quantity > limit:
        raise ValueError(f"quantity {quantity} exceeds outstanding {limit} on {line.line_ref!r}")
    if document_type == DISPATCH:
        return replace(totals, dispatched_qty=totals.dispatched_qty + line.quantity)
    if document_type == REFUND_RESTOCK:
        return replace(totals, refund_restocked_qty=totals.refund_restocked_qty + line.quantity)
    return replace(
        totals,
        returned_sellable_qty=totals.returned_sellable_qty + line.sellable_quantity,
        returned_unsellable_qty=totals.returned_unsellable_qty + line.unsellable_quantity,
    )


# ---- 订单库存查询的游标（§5.8 单据分页） ----

ORDER_STOCK_CURSOR_PREFIX = "v1"
ORDER_STOCK_DIGEST_LENGTH = 16

_DOCUMENT_SEQ = re.compile(r"[1-9][0-9]{0,18}")
_DIGEST = re.compile(r"[0-9a-f]{16}")


def order_stock_cursor_digest(reservation_id: str) -> str:
    """reservation_id 按 UTF-8 编码后 SHA-256 小写十六进制的前 16 个字符。"""
    digest = hashlib.sha256(reservation_id.encode("utf-8")).hexdigest()
    return digest[:ORDER_STOCK_DIGEST_LENGTH]


def format_order_stock_cursor(document_seq: int, reservation_id: str) -> str:
    """v1 加点加 document_seq 的十进制加点加订单摘要；调用方当不透明值。"""
    is_int = isinstance(document_seq, int) and not isinstance(document_seq, bool)
    if not is_int or not 1 <= document_seq <= CURSOR_MAX:
        raise ValueError(f"invalid document_seq: {document_seq!r}")
    digest = order_stock_cursor_digest(reservation_id)
    return f"{ORDER_STOCK_CURSOR_PREFIX}.{document_seq}.{digest}"


def parse_order_stock_cursor(value) -> tuple[int, str]:
    """after 参数只校验格式，返回 (document_seq, 摘要)；是否属于本订单另用 cursor_matches 判。

    前缀、段数、document_seq 不是 1–9223372036854775807 的无前导零十进制、
    摘要不是 16 个小写十六进制字符时抛 InvalidParameter（ValueError 的子类，按 after 报错）。
    契约的判定顺序是格式先 422、查不到订单 404、绑定不符最后 422，所以两步分开。
    """
    if not isinstance(value, str):
        raise InvalidParameter(AFTER)
    parts = value.split(".")
    if len(parts) != 3:
        raise InvalidParameter(AFTER)
    prefix, document_seq, digest = parts
    if prefix != ORDER_STOCK_CURSOR_PREFIX or not _DOCUMENT_SEQ.fullmatch(document_seq):
        raise InvalidParameter(AFTER)
    if not _DIGEST.fullmatch(digest) or int(document_seq) > CURSOR_MAX:
        raise InvalidParameter(AFTER)
    return int(document_seq), digest


def cursor_matches(digest: str, reservation_id: str) -> bool:
    """游标里的摘要是否属于这个订单（预留）。"""
    return digest == order_stock_cursor_digest(reservation_id)
