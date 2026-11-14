"""目录命令不碰数据库的规则（契约 §2.2、§2.5、§2.6、§2.7）。

字段取值不合规抛 InvalidField（带字段名：导入记 invalid_format，接口回 422）；
变更流的查询参数不合格抛 InvalidParameter（接口回 422 INVALID_REQUEST）。
"""

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

ACTIVE = "active"
INACTIVE = "inactive"

NAME_MAX_LENGTH = 200
CODE_MAX_LENGTH = 64
REASON_MAX_LENGTH = 500
AMOUNT_MINOR_MAX = 9007199254740991

NAME = "name"
SKU_CODE = "sku_code"
BARCODE = "barcode"
REFERENCE_PRICE = "reference_price"
REASON = "reason"
STATUS = "status"
EXPECTED_VERSION = "expected_version"

PRODUCT = "product"
SKU = "sku"

# §2.6 第 4 条：PATCH 正文里可改的字段；expected_version、reason 不算“改动字段”。
UPDATABLE_FIELDS = {
    PRODUCT: (NAME,),
    SKU: (SKU_CODE, BARCODE, NAME, REFERENCE_PRICE),
}
# 给 null 是清空的字段；name、sku_code 新建与修改都不接受 null。
CLEARABLE_FIELDS = frozenset({BARCODE, REFERENCE_PRICE})
_UPDATE_META_FIELDS = frozenset({EXPECTED_VERSION, REASON})

# §2.2 冲突码，顶层错误码按此顺序取第一个出现的（§2.6 第 2 条）。
SKU_CODE_TAKEN = "SKU_CODE_TAKEN"
SKU_CODE_RETIRED = "SKU_CODE_RETIRED"
BARCODE_TAKEN = "BARCODE_TAKEN"
CONFLICT_CODES = (SKU_CODE_TAKEN, SKU_CODE_RETIRED, BARCODE_TAKEN)

# §2.2 编码归属判定的另外两种结果。
CLAIM_INSERT = "insert"
CLAIM_OWNED = "owned"

# §2.5 事件类型。
PRODUCT_CREATED = "product_created"
PRODUCT_UPDATED = "product_updated"
PRODUCT_DEACTIVATED = "product_deactivated"
PRODUCT_REACTIVATED = "product_reactivated"
SKU_CREATED = "sku_created"
SKU_UPDATED = "sku_updated"
SKU_DEACTIVATED = "sku_deactivated"
SKU_REACTIVATED = "sku_reactivated"
EVENT_TYPES = frozenset(
    {
        PRODUCT_CREATED,
        PRODUCT_UPDATED,
        PRODUCT_DEACTIVATED,
        PRODUCT_REACTIVATED,
        SKU_CREATED,
        SKU_UPDATED,
        SKU_DEACTIVATED,
        SKU_REACTIVATED,
    }
)

AFTER = "after"
LIMIT = "limit"
DEFAULT_LIMIT = 100
MAX_LIMIT = 100
CURSOR_MAX = 9223372036854775807

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_PRINTABLE_ASCII = re.compile(r"[\x20-\x7e]+")
_CURRENCY = re.compile(r"[A-Z]{3}")
_CURSOR = re.compile(r"[1-9][0-9]{0,18}")
_LIMIT = re.compile(r"[1-9][0-9]{0,2}")
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


class InvalidField(ValueError):
    """字段取值不合规；field 是字段名。"""

    def __init__(self, field: str):
        super().__init__(f"invalid {field}")
        self.field = field


class NothingToUpdate(ValueError):
    """修改请求除 expected_version、reason 外一个字段都没带（422）。"""

    def __init__(self):
        super().__init__("no field to update")


class InvalidParameter(ValueError):
    """变更流的查询参数不合格；parameter 是参数名。"""

    def __init__(self, parameter: str):
        super().__init__(f"invalid {parameter} parameter")
        self.parameter = parameter


# ---- 字段取值（§2.6 第 1 条） ----


def _text(field: str, value, max_length: int) -> str:
    if not isinstance(value, str):
        raise InvalidField(field)
    value = value.strip()
    if not 1 <= len(value) <= max_length or _CONTROL.search(value):
        raise InvalidField(field)
    return value


def normalize_name(value, field: str = NAME) -> str:
    """商品与 SKU 的名称：去首尾空白后 1–200 个字符、不含控制字符；不接受 None。

    导入的 product_name、sku_name 也按它校验，field 传导入的字段名。
    """
    return _text(field, value, NAME_MAX_LENGTH)


def normalize_sku_code(value, field: str = SKU_CODE) -> str:
    """去首尾空白后 1–64 个可打印 ASCII 字符（空格只会在中间）；不接受 None。"""
    if not isinstance(value, str):
        raise InvalidField(field)
    value = value.strip()
    if len(value) > CODE_MAX_LENGTH or not _PRINTABLE_ASCII.fullmatch(value):
        raise InvalidField(field)
    return value


def normalize_barcode(value, field: str = BARCODE) -> str | None:
    """None 或去首尾空白后为空串得 None（§2.2 空条码存 NULL），否则同 sku_code。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return normalize_sku_code(value, field)


def _is_int(value) -> bool:
    # bool 是 int 的子类，JSON 的 true 不能当 1。
    return isinstance(value, int) and not isinstance(value, bool)


def normalize_reference_price(value, field: str = REFERENCE_PRICE) -> dict | None:
    """None，或恰好含 amount_minor 与 currency 的对象；返回新的 dict。"""
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {"amount_minor", "currency"}:
        raise InvalidField(field)
    amount = value["amount_minor"]
    currency = value["currency"]
    if not _is_int(amount) or not 0 <= amount <= AMOUNT_MINOR_MAX:
        raise InvalidField(field)
    if not isinstance(currency, str) or not _CURRENCY.fullmatch(currency):
        raise InvalidField(field)
    return {"amount_minor": amount, "currency": currency}


def normalize_reason(value) -> str | None:
    """可选：None，或去首尾空白后 1–500 个字符、不含控制字符的字符串。"""
    if value is None:
        return None
    return _text(REASON, value, REASON_MAX_LENGTH)


_NORMALIZERS = {
    NAME: normalize_name,
    SKU_CODE: normalize_sku_code,
    BARCODE: normalize_barcode,
    REFERENCE_PRICE: normalize_reference_price,
}


def normalize_update(object_type: str, body: Mapping) -> dict:
    """修改请求（§2.6 第 4 条）里可改的字段，规范化后返回。

    返回值不含 expected_version 与 reason：前者由调用方另行校验，后者用 normalize_reason。
    未知字段按该字段名抛 InvalidField；
    除 expected_version、reason 外一个都没带抛 NothingToUpdate。
    name、sku_code 给 None 被拒，barcode、reference_price 给 None 是清空。
    """
    allowed = UPDATABLE_FIELDS[object_type]
    fields = {}
    for field, value in body.items():
        if field in _UPDATE_META_FIELDS:
            continue
        if field not in allowed:
            raise InvalidField(field)
        fields[field] = _NORMALIZERS[field](value)
    if not fields:
        raise NothingToUpdate()
    return fields


def merged_sku_update_allowed(fields: Mapping) -> bool:
    """已合并的 SKU 只接受清空条码。

    即除 expected_version、reason 外只带 barcode，且规范化后为 None；
    不满足时接口回 409 SKU_MERGED。fields 可以是原始正文，也可以是 normalize_update 的结果。
    """
    keys = set(fields) - _UPDATE_META_FIELDS
    return keys == {BARCODE} and normalize_barcode(fields[BARCODE]) is None


# ---- 编码比较与归属（§2.2、§2.6 第 1 条） ----


def code_key(code: str) -> str:
    """编码与条码判重的比较键：规范化后的值按 ASCII 转小写。

    “改没改”不用它，按规范化后的字符串原样比较（只改大小写也算改）。
    """
    return code.translate(_ASCII_LOWER)


def decide_code_claim(
    owner_sku_id: str | None, owner_current_code_matches: bool, requesting_sku_id: str | None
) -> str:
    """新编码在 sku_code_claims 里的归属判定（§2.2）。

    owner_sku_id：归属表里登记的 SKU，没登记为 None；
    owner_current_code_matches：那个 SKU 的当前编码与新编码的比较键是否相同（没登记时不看）；
    requesting_sku_id：发起修改的 SKU，新建时为 None。
    返回 CLAIM_INSERT（插入归属行）、CLAIM_OWNED（已归属自己，不再插入）、
    SKU_CODE_TAKEN 或 SKU_CODE_RETIRED。
    """
    if owner_sku_id is None:
        return CLAIM_INSERT
    if requesting_sku_id is not None and owner_sku_id == requesting_sku_id:
        return CLAIM_OWNED
    return SKU_CODE_TAKEN if owner_current_code_matches else SKU_CODE_RETIRED


# ---- 冲突（§2.6 第 2 条） ----

_CONFLICT_FIELD_ORDER = {SKU_CODE: 0, BARCODE: 1}


@dataclass(frozen=True)
class Conflict:
    """details.conflicts 的一项；加规格时不带 index。"""

    field: str
    value: str
    code: str
    index: int | None = None

    def __post_init__(self):
        if self.field not in _CONFLICT_FIELD_ORDER or self.code not in CONFLICT_CODES:
            raise ValueError(f"invalid conflict: {self!r}")

    def as_dict(self) -> dict:
        item = {} if self.index is None else {"index": self.index}
        item.update(field=self.field, value=self.value, code=self.code)
        return item


def conflict_code(conflicts: Iterable[Conflict]) -> str | None:
    """顶层错误码：按 CONFLICT_CODES 的顺序取第一个出现的；没有冲突为 None。"""
    present = {conflict.code for conflict in conflicts}
    for code in CONFLICT_CODES:
        if code in present:
            return code
    return None


def sort_conflicts(conflicts: Iterable[Conflict]) -> list[Conflict]:
    """全部保留，按 index 升序，同一 SKU 先 sku_code 后 barcode。"""
    return sorted(
        conflicts,
        key=lambda c: (-1 if c.index is None else c.index, _CONFLICT_FIELD_ORDER[c.field]),
    )


# ---- 改动字段（§2.5、§2.6 第 4 条、§2.7） ----


def _change(before, after) -> dict:
    return {"before": before, "after": after}


def diff_fields(current: Mapping, requested: Mapping) -> dict:
    """请求里带的已规范化字段与对象当前值比较，返回 {字段名: {"before", "after"}}。

    与当前值相同的字段不算；reference_price 整个对象算一个字段；全都相同时为空 dict。
    字符串原样比较，只改大小写也算改。
    """
    return {
        field: _change(current[field], after)
        for field, after in requested.items()
        if current[field] != after
    }


def product_created_changes(name: str) -> dict:
    """新建商品的改动字段：name 与 status，before 全为 None。"""
    return {NAME: _change(None, name), STATUS: _change(None, ACTIVE)}


def sku_created_changes(
    sku_code: str, barcode: str | None, name: str, reference_price: dict | None
) -> dict:
    """新建 SKU 的改动字段，before 全为 None。

    字段为 sku_code、barcode、name、reference_price 与 status；
    barcode、reference_price 为 None 时照样列出。
    """
    return {
        SKU_CODE: _change(None, sku_code),
        BARCODE: _change(None, barcode),
        NAME: _change(None, name),
        REFERENCE_PRICE: _change(None, reference_price),
        STATUS: _change(None, ACTIVE),
    }


def status_changes(current: str, target: str) -> dict:
    """停用与重新启用的改动字段：只有 status。

    已是目标状态时为空 dict（不加版本、不写记录）。
    """
    if current not in (ACTIVE, INACTIVE) or target not in (ACTIVE, INACTIVE):
        raise ValueError(f"invalid status: {current!r} -> {target!r}")
    return diff_fields({STATUS: current}, {STATUS: target})


# ---- 目录写锁（§2.7） ----


def catalog_lock_name(database: str, tenant_id: str) -> str:
    """目录写锁的锁名，64 个字符。

    数据库名、字面量 catalog、tenant_id 以换行符连接，UTF-8 编码后取 SHA-256 小写十六进制。
    拼法定下后不改。任一项为空或含换行符时拒绝，以免两个不同的作用域拼出同一个锁名。
    """
    parts = (database, "catalog", tenant_id)
    for part in parts:
        if not isinstance(part, str) or not part or "\n" in part:
            raise ValueError(f"invalid lock name part: {part!r}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


# ---- 变更流（§2.5） ----


def format_cursor(change_id: int) -> str:
    """变更记录 id 的十进制字符串；调用方当不透明值。"""
    return str(change_id)


def parse_cursor(value: str | None) -> int | None:
    """after 参数：不带为 None（从头开始）；带时只接受 1–9223372036854775807 的十进制表示。

    不收前导零、正负号与空白，0 不合格。
    """
    if value is None:
        return None
    if not isinstance(value, str) or not _CURSOR.fullmatch(value):
        raise InvalidParameter(AFTER)
    cursor = int(value)
    if cursor > CURSOR_MAX:
        raise InvalidParameter(AFTER)
    return cursor


def parse_limit(value: str | None) -> int:
    """limit 参数：不带为 100；带时只接受无符号、无前导零、1–100 的十进制数字串。"""
    if value is None:
        return DEFAULT_LIMIT
    if not isinstance(value, str) or not _LIMIT.fullmatch(value):
        raise InvalidParameter(LIMIT)
    limit = int(value)
    if limit > MAX_LIMIT:
        raise InvalidParameter(LIMIT)
    return limit
