"""批量导入不碰数据库的规则（契约 §2.3 的 0.7 细化）。

预览：逐行判定与汇总；确认：按重查结果给出要建的组与逐行结果。
编码与条码的比较键一律用 catalog.code_key；传入的现有集合也必须是比较键。
"""

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace

from app.domain.catalog import (
    InvalidField,
    code_key,
    normalize_barcode,
    normalize_name,
    normalize_reference_price,
    normalize_sku_code,
)
from app.domain.requests import is_reference

# 预览的逐行结果（§2.3），按判定顺序排列。
INVALID_FORMAT = "invalid_format"
DUPLICATE_SKU_CODE = "duplicate_sku_code"
DUPLICATE_BARCODE = "duplicate_barcode"
CONFLICTS_EXISTING = "conflicts_existing"
GROUP_BLOCKED = "group_blocked"
OK = "ok"
PREVIEW_RESULTS = (
    INVALID_FORMAT,
    DUPLICATE_SKU_CODE,
    DUPLICATE_BARCODE,
    CONFLICTS_EXISTING,
    GROUP_BLOCKED,
    OK,
)

# 确认的逐行结果。
CREATED = "created"
SKIPPED = "skipped"
FAILED = "failed"

EXTERNAL_REF = "external_ref"
PRODUCT_REF = "product_ref"
PRODUCT_NAME = "product_name"
SKU_CODE = "sku_code"
SKU_NAME = "sku_name"
BARCODE = "barcode"
REFERENCE_PRICE = "reference_price"
# 多出的字段统一记这个名字，不回显调用方的键名。
UNKNOWN_FIELD = "unknown_field"

REQUIRED_FIELDS = (EXTERNAL_REF, PRODUCT_REF, PRODUCT_NAME, SKU_CODE, SKU_NAME)
OPTIONAL_FIELDS = (BARCODE, REFERENCE_PRICE)
# invalid_fields 的排列顺序。
FIELD_ORDER = REQUIRED_FIELDS + OPTIONAL_FIELDS + (UNKNOWN_FIELD,)


def _normalize_reference(value, field: str) -> str:
    """第 1 节的引用格式，原样返回（不去空白、不改大小写）。"""
    if not is_reference(value):
        raise InvalidField(field)
    return value


_ROW_NORMALIZERS = {
    EXTERNAL_REF: _normalize_reference,
    PRODUCT_REF: _normalize_reference,
    PRODUCT_NAME: normalize_name,
    SKU_CODE: normalize_sku_code,
    SKU_NAME: normalize_name,
    BARCODE: normalize_barcode,
    REFERENCE_PRICE: normalize_reference_price,
}


@dataclass(frozen=True)
class ImportedSku:
    """合格行规范化后的字段值；barcode、reference_price 可为 None。"""

    external_ref: str
    product_ref: str
    product_name: str
    sku_code: str
    sku_name: str
    barcode: str | None
    reference_price: dict | None


@dataclass(frozen=True)
class RowPreview:
    """预览的一行。

    external_ref、product_ref 是行里的原值（没带为 None，不一定合规）；
    invalid_fields 只在 invalid_format 时非空；values 只在 ok 时有。
    """

    index: int
    external_ref: object
    product_ref: object
    result: str
    invalid_fields: tuple[str, ...] = ()
    values: ImportedSku | None = None


def _check_fields(row: Mapping) -> tuple[dict, set[str]]:
    values = {}
    errors = set()
    for field, normalize in _ROW_NORMALIZERS.items():
        if field not in row:
            if field in REQUIRED_FIELDS:
                errors.add(field)
            else:
                values[field] = None
            continue
        try:
            values[field] = normalize(row[field], field)
        except InvalidField:
            errors.add(field)
    if any(key not in _ROW_NORMALIZERS for key in row):
        errors.add(UNKNOWN_FIELD)
    return values, errors


def _ordered(fields: Iterable[str]) -> tuple[str, ...]:
    present = set(fields)
    return tuple(field for field in FIELD_ORDER if field in present)


def _repeated(keys: Mapping[int, str]) -> set[int]:
    """keys 是 行下标 → 比较键；返回比较键出现不止一次的行下标。"""
    counts = {}
    for key in keys.values():
        counts[key] = counts.get(key, 0) + 1
    return {index for index, key in keys.items() if counts[key] > 1}


def preview_import(
    rows: Sequence[Mapping],
    claimed_code_keys: Collection[str],
    barcode_keys: Collection[str],
) -> list[RowPreview]:
    """逐行判定（§2.3），按行下标顺序返回。

    rows 的每个元素已由接口确认是 JSON 对象；
    claimed_code_keys：sku_code_claims 里编码的比较键（任何 SKU 的当前与退役编码）；
    barcode_keys：现有 SKU 当前条码的比较键。
    """
    checked = [_check_fields(row) for row in rows]
    field_errors = [errors for _, errors in checked]
    errors = [set(found) for found in field_errors]
    values = [found for found, _ in checked]

    # external_ref 重复：只在 external_ref 本身合规的行之间、原样比较。
    external_refs = {
        i: values[i][EXTERNAL_REF] for i in range(len(rows)) if EXTERNAL_REF not in field_errors[i]
    }
    for index in _repeated(external_refs):
        errors[index].add(EXTERNAL_REF)

    # 分组：product_ref 本身不合规的行不归任何组；按首次出现的顺序。
    groups: dict[str, list[int]] = {}
    for i in range(len(rows)):
        if PRODUCT_REF not in field_errors[i]:
            groups.setdefault(values[i][PRODUCT_REF], []).append(i)

    # 同组名称一致：只在 product_name 本身合规的行之间比较。
    for members in groups.values():
        named = [i for i in members if PRODUCT_NAME not in field_errors[i]]
        if len({values[i][PRODUCT_NAME] for i in named}) > 1:
            for i in named:
                errors[i].add(PRODUCT_NAME)

    results: dict[int, str] = {i: INVALID_FORMAT for i in range(len(rows)) if errors[i]}
    eligible = [i for i in range(len(rows)) if not errors[i]]

    duplicate_codes = _repeated({i: code_key(values[i][SKU_CODE]) for i in eligible})
    duplicate_barcodes = _repeated(
        {i: code_key(values[i][BARCODE]) for i in eligible if values[i][BARCODE] is not None}
    )
    for i in eligible:
        if i in duplicate_codes:
            results[i] = DUPLICATE_SKU_CODE
        elif i in duplicate_barcodes:
            results[i] = DUPLICATE_BARCODE
        elif _conflicts(values[i][SKU_CODE], values[i][BARCODE], claimed_code_keys, barcode_keys):
            results[i] = CONFLICTS_EXISTING

    for members in groups.values():
        blocked = any(i in results for i in members)
        for i in members:
            if i not in results:
                results[i] = GROUP_BLOCKED if blocked else OK

    previews = []
    for i, row in enumerate(rows):
        result = results[i]
        previews.append(
            RowPreview(
                index=i,
                external_ref=row.get(EXTERNAL_REF),
                product_ref=row.get(PRODUCT_REF),
                result=result,
                invalid_fields=_ordered(errors[i]) if result == INVALID_FORMAT else (),
                values=ImportedSku(**values[i]) if result == OK else None,
            )
        )
    return previews


def _conflicts(
    sku_code: str,
    barcode: str | None,
    claimed_code_keys: Collection[str],
    barcode_keys: Collection[str],
) -> bool:
    if code_key(sku_code) in claimed_code_keys:
        return True
    return barcode is not None and code_key(barcode) in barcode_keys


# ---- 汇总 ----


@dataclass(frozen=True)
class ImportSummary:
    """counts 含六种结果各自的行数（六个键都有）；可执行的组是全组为 ok 的组。"""

    counts: dict
    executable_groups: int
    executable_rows: int


def _groups(previews: Iterable[RowPreview]) -> dict[str, list[RowPreview]]:
    groups: dict[str, list[RowPreview]] = {}
    for preview in previews:
        if is_reference(preview.product_ref):
            groups.setdefault(preview.product_ref, []).append(preview)
    return groups


def summarize_preview(previews: Sequence[RowPreview]) -> ImportSummary:
    counts = {result: 0 for result in PREVIEW_RESULTS}
    for preview in previews:
        counts[preview.result] += 1
    executable = [
        members
        for members in _groups(previews).values()
        if all(preview.result == OK for preview in members)
    ]
    return ImportSummary(
        counts=counts,
        executable_groups=len(executable),
        executable_rows=sum(len(members) for members in executable),
    )


# ---- 确认计划 ----


def recheck_conflicts(
    previews: Iterable[RowPreview],
    claimed_code_keys: Collection[str],
    barcode_keys: Collection[str],
) -> frozenset[int]:
    """按当前目录重查：编码或条码现已被占用的 ok 行的下标。"""
    return frozenset(
        preview.index
        for preview in previews
        if preview.result == OK
        and _conflicts(
            preview.values.sku_code, preview.values.barcode, claimed_code_keys, barcode_keys
        )
    )


@dataclass(frozen=True)
class PlannedSku:
    index: int
    external_ref: str
    sku_code: str
    barcode: str | None
    name: str
    reference_price: dict | None


@dataclass(frozen=True)
class PlannedProduct:
    """要建的一组：商品名称取 product_name，各 SKU 名称取 sku_name。"""

    product_ref: str
    name: str
    skus: tuple[PlannedSku, ...]


@dataclass(frozen=True)
class RowOutcome:
    """确认的一行。

    created：product_id、sku_id 由服务层建好后用 with_ids 填；
    skipped：result 为预览结果；failed：result 为 conflicts_existing 或 group_blocked。
    """

    index: int
    external_ref: object
    product_ref: object
    outcome: str
    result: str | None = None
    product_id: str | None = None
    sku_id: str | None = None

    def with_ids(self, product_id: str, sku_id: str) -> "RowOutcome":
        if self.outcome != CREATED:
            raise ValueError(f"row {self.index} is not created")
        return replace(self, product_id=product_id, sku_id=sku_id)


@dataclass(frozen=True)
class ConfirmPlan:
    """products 按组在 rows 里首次出现的顺序；rows 按行下标，覆盖每一行。"""

    products: tuple[PlannedProduct, ...]
    rows: tuple[RowOutcome, ...]


def plan_confirm(previews: Sequence[RowPreview], conflicting: Collection[int]) -> ConfirmPlan:
    """确认时整组都没有冲突的 ok 组照建，其余组整组不建。

    conflicting：重查得到的冲突行下标（recheck_conflicts），只能是 ok 行。
    """
    ok_indexes = {preview.index for preview in previews if preview.result == OK}
    stray = set(conflicting) - ok_indexes
    if stray:
        raise ValueError(f"conflicting rows are not ok: {sorted(stray)}")

    failed_groups = {
        product_ref
        for product_ref, members in _groups(previews).items()
        if any(preview.index in conflicting for preview in members)
    }

    products = []
    for product_ref, members in _groups(previews).items():
        if product_ref in failed_groups or any(preview.result != OK for preview in members):
            continue
        products.append(
            PlannedProduct(
                product_ref=product_ref,
                name=members[0].values.product_name,
                skus=tuple(
                    PlannedSku(
                        index=preview.index,
                        external_ref=preview.values.external_ref,
                        sku_code=preview.values.sku_code,
                        barcode=preview.values.barcode,
                        name=preview.values.sku_name,
                        reference_price=preview.values.reference_price,
                    )
                    for preview in members
                ),
            )
        )

    outcomes = []
    for preview in sorted(previews, key=lambda p: p.index):
        if preview.result != OK:
            outcome, result = SKIPPED, preview.result
        elif preview.index in conflicting:
            outcome, result = FAILED, CONFLICTS_EXISTING
        elif preview.product_ref in failed_groups:
            outcome, result = FAILED, GROUP_BLOCKED
        else:
            outcome, result = CREATED, None
        outcomes.append(
            RowOutcome(
                index=preview.index,
                external_ref=preview.external_ref,
                product_ref=preview.product_ref,
                outcome=outcome,
                result=result,
            )
        )
    return ConfirmPlan(products=tuple(products), rows=tuple(outcomes))
