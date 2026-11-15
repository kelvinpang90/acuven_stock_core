"""期初导入不碰数据库的规则（契约 §6.3）。

预览：逐行判定与汇总；确认：按重查结果给出逐行结果。每行一个 SKU，行与行互不牵连。
逐行结果都是键固定的 dict，可以原样存进导入记录、写进响应。
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.domain.stock import is_non_negative_quantity

# 预览的逐行结果（§6.3），按判定顺序排列。
INVALID_FORMAT = "invalid_format"
DUPLICATE_SKU = "duplicate_sku"
SKU_NOT_FOUND = "sku_not_found"
SKU_MERGED = "sku_merged"
ALREADY_OPENED = "already_opened"
OK = "ok"
PREVIEW_RESULTS = (INVALID_FORMAT, DUPLICATE_SKU, SKU_NOT_FOUND, SKU_MERGED, ALREADY_OPENED, OK)

# 确认的逐行结果。
CREATED = "created"
SKIPPED = "skipped"
FAILED = "failed"

SKU_ID = "sku_id"
QUANTITY = "quantity"
ROW_FIELDS = frozenset({SKU_ID, QUANTITY})


@dataclass(frozen=True)
class SkuState:
    """本租户里一个存在的 SKU 的现状；不存在的 SKU 不出现在 lookup 里。

    merged：merged_into_sku_id 非空；opened：在该池的余额行上 opening_entry_id 非空。
    """

    merged: bool = False
    opened: bool = False


def _is_int(value) -> bool:
    # bool 是 int 的子类，JSON 的 true 不能当 1 件。
    return isinstance(value, int) and not isinstance(value, bool)


def _well_formed(row) -> bool:
    return (
        isinstance(row, Mapping)
        and set(row) == ROW_FIELDS
        and isinstance(row[SKU_ID], str)
        and is_non_negative_quantity(row[QUANTITY])
    )


def _sku_result(sku_id: str, lookup: Mapping[str, SkuState]) -> str:
    """按当前 lookup 判 sku_not_found、sku_merged、already_opened 或 ok。"""
    state = lookup.get(sku_id)
    if state is None:
        return SKU_NOT_FOUND
    if state.merged:
        return SKU_MERGED
    if state.opened:
        return ALREADY_OPENED
    return OK


def judge_rows(rows: Sequence, lookup: Mapping[str, SkuState]) -> list[dict]:
    """逐行判定（§6.3），按行下标顺序返回。

    rows 是预览正文的 rows 列表，元素不一定是对象；lookup 是 sku_id → SkuState，
    只含本租户里存在的 SKU。每项的键固定为 index、sku_id、quantity、result：
    sku_id 只在是字符串时回显，quantity 只在是整数时回显，否则为 None。
    """
    well_formed = [_well_formed(row) for row in rows]

    # duplicate_sku：只在格式合格的行之间、按字符串原样比较，相关各行都算。
    counts: dict[str, int] = {}
    for row, ok in zip(rows, well_formed, strict=True):
        if ok:
            counts[row[SKU_ID]] = counts.get(row[SKU_ID], 0) + 1

    previews = []
    for index, (row, ok) in enumerate(zip(rows, well_formed, strict=True)):
        if not ok:
            result = INVALID_FORMAT
        elif counts[row[SKU_ID]] > 1:
            result = DUPLICATE_SKU
        else:
            result = _sku_result(row[SKU_ID], lookup)
        sku_id = row.get(SKU_ID) if isinstance(row, Mapping) else None
        quantity = row.get(QUANTITY) if isinstance(row, Mapping) else None
        previews.append(
            {
                "index": index,
                "sku_id": sku_id if isinstance(sku_id, str) else None,
                "quantity": quantity if _is_int(quantity) else None,
                "result": result,
            }
        )
    return previews


def summarize(previews: Sequence[Mapping]) -> dict:
    """counts 含六种结果各自的行数（六个键都有）；可执行的是 ok 行，件数合计只算 ok 行。"""
    counts = {result: 0 for result in PREVIEW_RESULTS}
    for preview in previews:
        counts[preview["result"]] += 1
    executable = [preview for preview in previews if preview["result"] == OK]
    return {
        "counts": counts,
        "executable_rows": len(executable),
        "executable_quantity": sum(preview["quantity"] for preview in executable),
    }


def plan_confirm(previews: Sequence[Mapping], lookup: Mapping[str, SkuState]) -> list[dict]:
    """确认计划：预览为 ok 的行按当前 lookup 重查，其余行跳过；按行下标顺序返回。

    previews 是 judge_rows 的结果（可以是从导入记录读回的）；lookup 同 judge_rows，
    由服务层在锁住余额行之后按当前状态给出。每项的键固定为 index、sku_id、quantity、
    outcome、entry_id、preview_result、failure：
    - created：重查通过，entry_id 先为 None，由服务层落账后填（数量为 0 时仍为 None）；
    - failed：重查不合格，failure 为 sku_not_found、sku_merged 或 already_opened；
    - skipped：预览时就不合格，preview_result 为预览结果。
    created 与 failed 的 preview_result 都是 ok；failure 只有 failed 的行有，其余为 None。
    """
    plan = []
    for preview in sorted(previews, key=lambda p: p["index"]):
        failure = None
        if preview["result"] != OK:
            outcome = SKIPPED
        else:
            recheck = _sku_result(preview["sku_id"], lookup)
            outcome = CREATED if recheck == OK else FAILED
            failure = None if recheck == OK else recheck
        plan.append(
            {
                "index": preview["index"],
                "sku_id": preview["sku_id"],
                "quantity": preview["quantity"],
                "outcome": outcome,
                "entry_id": None,
                "preview_result": preview["result"],
                "failure": failure,
            }
        )
    return plan
