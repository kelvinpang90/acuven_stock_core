"""平台事件执行器（契约 §5.10，§3.1 下单扣配额、取消加回与恢复条件）。

§5.10 的存放、处理顺序第 1–8 步（含第 4 步落后、越过阶段的三种情形）与
“每个动作调哪个命令体”的全部各行：created、ready_to_ship、扣数之前与已确认之后的 cancelled、
shipped、return_requested、return_received。
调 Core 命令体的动作（建 Execution、派生正文、保存点）在 app/services/platform_commands.py，
由这里分派。只有函数，不挂作业、不挂接口（由以后的任务接）：
- record_auto_event：webhook 与补拉把一个事件写进收件箱；
- orders_with_pending_events：找有未处理事件的订单，供作业用；
- process_order：处理一个订单这一轮的事件。
规则用 app/domain/platform_events.py 与 app/domain/allocations.py
的纯函数；这里取锁、开事务、读写库。

process_order 的先后：
1. 在没有打开事务的连接上 GET_LOCK(订单锁名, 5)；
   取不到或出错什么也不做，返回 LOCK_BUSY。
2. 取这个订单全部未处理的事件，按 sort_key 排序，
   逐个处理，每个事件一笔事务。轮到 return_received 时这一轮还没处理的事件里有 shipped，
   先依次处理这些 shipped（同乱序的先处理前序，每个事件最多被提前一次），否则退货会因
   尚未记发货被 Core 拒绝。
3. 开事务之前：解析涉及的订单行的 SKU（已有平台订单行取
   已定下的、取这个订单的全部订单行，created 的各行按当前
   active 的平台商品，普通读）；
   用短事务预建余额行、pool_allocations 行与这个池全部
   platform 入口在这些 SKU 上的 channel_allocations 行；
   ready_to_ship 另照确认销售接口的 CommitSkus 算出这些 SKU 的合并去向，
   一并预建余额行（命令体的 prepared 即这个集合）；
   会话设为 READ COMMITTED，再关这些 SKU（ready_to_ship 连同合并去向）上已到期的预留
   （close_due_before_reserving，同 HTTP 执行器的先后）。
   之后调模块级钩子 _between_prepare_and_lock（默认为空）。
4. 开事务，照 §5.10 第 4 步的锁序加锁，用不到的层跳过：
   收件箱行 FOR UPDATE（已处理就提交空事务）
   → 平台订单行 FOR UPDATE
   → created 再按 sku_id 升序 FOR UPDATE 锁这个入口在
     这些 SKU 上的 channel_allocations 行
   → 平台商品 FOR SHARE 重新解析 created 各行的 SKU：
     与第 3 步不同就回滚、从第 3 步重做一次，
     再不同就回滚、这一轮停下（LISTING_CHANGED）。
   然后按阶段判定：ignored 记 ignored、不改任何量（return_requested 总是这样）；
   created 的 apply 建平台订单并扣配额；ready_to_ship 的 apply 见下面的进入待发货；
   unpaid、held 时 cancelled 的 apply 见下面的扣数之前的取消；
   committed 时 shipped、cancelled、return_received 的 apply 见下面的已确认之后；
   attention 写 stage_conflict（见下面的待处理）；
   missing_prior 与 blocked 见下面的乱序。
5、6. 命令体在一个保存点里（app/services/platform_commands.py）。命令抛 RedoCommand：
   回滚整笔事务，从第 3 步重做一次；再抛就回滚、这一轮停下（REDO_AGAIN）。
7. 写收件箱行的 applied_at（数据库时间）与 result，
   与改动一起提交；提交之后写命令登记的安全事件（app/services/security_events.py，
   同 HTTP 执行器；回滚了的不写）。一轮全部处理完、这个订单没有未处理的事件了，
   而 next_pull_at 非空或 pull_attempts 大于 0：用一笔短事务清为空与 0。
   一轮结束时在同一连接上设回隔离级别、
   RELEASE_ALL_LOCKS()，出错就作废这个连接
   （同 app/services/idempotency.py 的 _release_locks）。
8. 数据库错误或程序异常：整笔回滚，收件箱行仍未处理，
   这一轮停下（FAILED）；异常不向外抛，记一行进程日志
   （不含 payload）。

created（§3.1、0.24）：插入平台订单（stage unpaid）与各行；
按 platform_line_id 升序逐行，有 SKU 的行用 take_quota
从这个入口这个 SKU 的 Q 扣走、记 quota_taken，pending 加
这一行的件数；manual_confirmed_quota 非空时同时减去
min(件数, manual_confirmed_quota)，不论入口模式，
不写 platform_ops_log。有行找不到生效平台商品时订单为 held、
attention 为 unmapped，那些行不计 pending、不扣配额，
result 记 held、result_code 记 unmapped。

扣数之前的 cancelled（§3.1，订单 unpaid 或 held）：payload 带 lines 时
每行这次取消 cancelled_total − cancelled_qty 件（不大于 0 即没有新取消），
不带时取消各行全部剩余件数；全部行都没有新取消记 ignored、不改任何量。
有这个订单没有的行（unknown_line），或 cancelled_total 超过 quantity
（cancel_exceeds_remaining）时不改任何量、写待处理、result 记 attention；
两者都有记 unknown_line。锁序：平台订单之后锁订单行，
unpaid 时再 池行 FOR SHARE → 余额行 → pool_allocations 行
→ 这个池全部 platform 入口的 channel_allocations 行（按 sku_id、再按 channel_id）
→ 这个入口在这些 SKU 上的平台商品 FOR SHARE；held 时只锁这个入口的配额行。
每行加 cancelled_qty，有 SKU 的行从 pending 减取消的件数；unpaid 时按 SKU
汇总可加回的件数（add_back），restore_allowed 判过就整份加到 Q、各行
quota_returned 随之增加，不过就整份不加（进 F）；held 时一件也不加回。
各行剩余件数都为 0 时阶段改为 cancelled（attention 不清）。

进入待发货（§5.10、§3.1，订单 unpaid，或 held 而 ready_at 未记过）：
先在保存点之外记 ready_at（payload 的平台时间，没有则用 received_at，截到整秒；
已记过不改），再 FOR UPDATE 锁订单行。有剩余件数大于 0 的行 sku_id 为空：held、
attention unmapped，不调 Core。否则用 assign_line_refs 按 SKU 合并、派生两个命令的正文；
校验不过：held、attention invalid_request、attention_code INVALID_REQUEST，不调 Core。
然后照第 4 步的锁序先锁 池行 FOR SHARE → 这些 SKU 的余额行 FOR UPDATE → 这个入口在
这些 SKU 上的配额行 FOR UPDATE → 平台商品 FOR SHARE（SKU 都按升序），命令体之后再锁的
余额行已持有，SKU 行与商品行排在后面。保存点里预留并确认销售：
- 成功：写各平台行的 line_ref，pending 减各行剩余件数，阶段 committed，result applied，
  attention 不动（unpaid 订单上可能有别的原因，留给后台清除）；
- 业务拒绝：held，attention core_rejected、attention_code 为错误码，pending 不动，result held；
- 带缺陷标记（见 platform_commands）的拒绝：同上，attention 为 defect；
  带缺陷标记的成功（rejudge 成功、按业务引用返回原结果）：照成功，result_code 记 defect，
  attention 写 defect（让人去查的原因，覆盖已有的）、attention_code 为空。

已确认之后（§5.10、§3.1，订单 committed）：先 FOR UPDATE 锁订单行，按这个先后判，
判出原因的不调 Core、result 记 attention：
- cancelled 带 lines、各行 cancelled_total 都不大于这一行的 cancelled_qty（付款前那次取消
  从另一个来源晚到）：ignored；不带 refund_id：refund_id_missing；带 refund_id 不带 lines
  （订单级，件数里可能含已交运的）：cancel_lines_missing；
- shipped 缺 package_id、return_received 缺 return_id：missing_platform_id（平台给不出稳定的
  ID 时按订单、动作、平台行、件数、时间判断同一动作，V1 先一律进待处理）；
- payload 里有平台行不属于这个订单或没有 line_ref：line_not_committed；
- 引用由 document_ref 派生，正文由 dispatch_body、refund_restock_body、return_receipt_body
  派生：整理后一行不剩记 ignored；校验不过 invalid_request、attention_code INVALID_REQUEST。
然后一个保存点里调发货、退款回库或退货验收（执行身份：自动来源为 system 主体，manual 来源为
收件箱行的 credential_id；操作者由调用方传入，process_order 为空）：
- 新生效：shipped、return_received 不再改别的（退货增量进 F），result applied；cancelled
  在命令之后锁 pool_allocations 行 → 这个池全部 platform 入口在这些 SKU 上的
  channel_allocations 行 → 这个入口在这些 SKU 上的平台商品 FOR SHARE（池行与余额行跳过：
  命令体已锁过余额行，available 从它读；配额行在命令体之后锁才不颠倒“预留行、订单行在
  余额行之前”的锁序），照扣数之前的加回规则把这次退款的件数按平台行 quota_taken −
  quota_returned 封顶、按 SKU 判恢复条件、整份加或不加；已确认之后的取消不记 cancelled_qty、
  不改 pending 与阶段。平台行的 SKU 已被合并时它没有生效平台商品，恢复条件第 2 项不满足，
  不加回，也不读它的余额行；
- 按业务引用返回原结果：ignored，不改 Q 与 pending；
- 业务拒绝：阶段不变、pending 不动，attention core_rejected（带缺陷标记的为 defect）、
  attention_code 为错误码，result attention；
- rejudge 成功：result applied、result_code defect，attention 写 defect；rejudge 按引用
  返回原结果，不改 Q 与 pending、不加回配额。

待处理（本任务与之后的平台事件任务都照此）：收件箱行的 result_code 写
这个事件的 Core 错误码，没有错误码时写原因。让订单进 held 的原因与缺陷总是覆盖
订单上的 attention 与 attention_code（后台照它重试）；其余原因在订单已有
attention 时不覆盖、没有时写上。

乱序（§5.10 第 4 步）：missing_prior 时，这一轮还没处理的事件里有缺的
那个动作就回滚当前事件，先处理排序最前的那一个（不看平台时间），再回到
当前事件；同一轮里每个事件最多被提前一次。没有就回滚，在一笔短事务里把
平台订单的 next_pull_at 设为数据库当前时间（已非空不动，pull_attempts 不动；
补拉、退避与计数由补拉作业做），这一轮停下（MISSING_PRIOR）；平台订单还不存在时
什么也不写，只停下。blocked 时回滚，这一轮停下，不标补拉（BLOCKED）。
"""

import hashlib
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import (
    Connection,
    Engine,
    Transaction,
    and_,
    func,
    insert,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.sql.expression import Executable

from app.api.errors import INVALID_REQUEST
from app.domain.allocations import (
    MANUAL,
    add_back,
    manual_check_overdue,
    restore_allowed,
    take_quota,
    unallocated,
)
from app.domain.catalog import ACTIVE
from app.domain.channels import PLATFORM
from app.domain.ids import new_id
from app.domain.platform_events import (
    CANCELLED,
    COMMITTED,
    CREATED,
    HASH_HEX_LENGTH,
    HELD,
    ID_MAX_LENGTH,
    PACKAGE_ID,
    READY_TO_SHIP,
    REFUND_ID,
    RETURN_ID,
    RETURN_RECEIVED,
    SHIPPED,
    UNPAID,
    CancelledPayload,
    CreatedLine,
    CreatedPayload,
    LineAssignment,
    Outcome,
    Payload,
    assign_line_refs,
    classify,
    order_lock_name,
    order_ref,
    parse_payload,
    sort_key,
)
from app.domain.requests import Actor
from app.domain.service_auth import SYSTEM
from app.models.catalog import NOW
from app.models.inventory import SalesChannel, StockBalance, StockPool
from app.models.platform import (
    ChannelAllocation,
    PlatformEvent,
    PlatformOrder,
    PlatformOrderLine,
    PoolAllocation,
    SkuListing,
)
from app.models.platform_ops import ManualCheck, PlatformConnection
from app.models.service_auth import Credential
from app.services.idempotency import RedoCommand
from app.services.platform_commands import (
    DocumentCommand,
    InvalidDerivedBody,
    command_context,
    create_order_document,
    dispatch_command,
    ready_commands,
    refund_restock_command,
    reserve_and_commit,
    return_receipt_command,
)
from app.services.reservations import close_due_before_reserving
from app.services.sale_commits import CommitSkus
from app.services.security_events import SecurityEventRecord, record_security_event
from app.services.stock import lock_balance_rows

# 自动来源（§5.10 存放）；manual 由后台登记，不经 record_auto_event。
WEBHOOK = "webhook"
PULL = "pull"
MANUAL_SOURCE = "manual"
AUTO_SOURCES = (WEBHOOK, PULL)

# 收件箱的 result 与待处理原因。
APPLIED = "applied"
IGNORED = "ignored"
ATTENTION = "attention"
UNMAPPED = "unmapped"
UNKNOWN_LINE = "unknown_line"
CANCEL_EXCEEDS_REMAINING = "cancel_exceeds_remaining"
STAGE_CONFLICT = "stage_conflict"
CORE_REJECTED = "core_rejected"
INVALID_REQUEST_REASON = "invalid_request"
# 已确认之后的动作不调 Core 的原因。
REFUND_ID_MISSING = "refund_id_missing"
CANCEL_LINES_MISSING = "cancel_lines_missing"
MISSING_PLATFORM_ID = "missing_platform_id"
LINE_NOT_COMMITTED = "line_not_committed"
# 引用派生或状态记录有缺陷（§5.10 第 6 步、命令体共用）；成功时也记在 result_code。
DEFECT = "defect"
# stage_conflict 的 Core 错误码。
STATE_CONFLICT = "STATE_CONFLICT"

EVENT_ID_PREFIX = "pev"
ORDER_ID_PREFIX = "pfo"
# 存进 event_key 的单据 ID：补拉用状态时间作 event_key，同一行同一时刻的两个包裹或两次取消
# 否则会撞去重键、后一个被静默丢掉。
DOCUMENT_ID_FIELDS = (PACKAGE_ID, REFUND_ID, RETURN_ID)

# 取订单锁最多等待的秒数（§5.10 第 1 步）；测试可以换成更短的值。
ORDER_LOCK_WAIT_SECONDS = 5
# 第 4 步重新解析的 SKU 与第 3 步不同时最多做几遍第 3、4 步。
RESOLVE_ATTEMPTS = 2
# 命令抛 RedoCommand 时最多做几遍第 3 步起的处理（§5.10 命令体共用）。
REDO_ATTEMPTS = 2

# 这一轮停下的原因。
LOCK_BUSY = "lock_busy"
LISTING_CHANGED = "listing_changed"
MISSING_PRIOR = "missing_prior"  # 缺前序动作、收件箱里也没有：标了补拉（平台订单存在时）
BLOCKED = "blocked"  # 不标补拉
REDO_AGAIN = "redo_again"  # 命令两次要求整个重做
FAILED = "failed"

_NO_REFERENCED_ROW = 1452  # MySQL ER_NO_REFERENCED_ROW_2：外键指向的行不存在
_READ_COMMITTED = "SET SESSION transaction_isolation = 'READ-COMMITTED'"
_DEFAULT_ISOLATION = "SET SESSION transaction_isolation = DEFAULT"

# 第 3 步之后、开事务之前在同一连接上调用（默认为空）；测试用 monkeypatch 设它。
_between_prepare_and_lock: Callable[[Connection], None] | None = None

# 写到 app.api 的子日志器：app/api/requests.py 给 app.api 挂了输出，生产上看得到。
_log = logging.getLogger("app.api.platform_events")


# --- 收件箱写入（§5.10 存放、引用格式第 1 条） ---


def _check_platform_key(value, field: str, *, allow_empty: bool) -> None:
    """平台给的订单号与行号：字符串，不超过 128 个字符；allow_empty 为假时不能是空串。"""
    if not isinstance(value, str) or len(value) > ID_MAX_LENGTH:
        raise ValueError(f"invalid {field}: {value!r}")
    if not allow_empty and not value:
        raise ValueError(f"invalid {field}: {value!r}")


def _hash_key(value: str) -> str:
    return "h" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:HASH_HEX_LENGTH]


def _stored_event_key(event_key: str, payload: Payload) -> str:
    """传入的 event_key，payload 带包裹、退款或退货 ID 时加竖线再加这个 ID；
    整串超过 128 个字符时换成 h 加整串 UTF-8 的 SHA-256 前 32 位。
    """
    stored = event_key
    for field in DOCUMENT_ID_FIELDS:
        document_id = getattr(payload, field, None)
        if document_id is not None:
            stored = f"{event_key}|{document_id}"
            break
    if len(stored) > ID_MAX_LENGTH:
        return _hash_key(stored)
    return stored


def _system_credential(connection: Connection, tenant_id: str, channel_id: str) -> str | None:
    """入口是这个租户 active 的 platform 入口、连接不是人工模式时，
    返回这个租户的 system 主体，否则 None。
    可写而租户没有 system 主体是开通的缺陷，抛 RuntimeError。
    """
    statement = (
        select(SalesChannel.kind, SalesChannel.status, PlatformConnection.mode)
        .select_from(SalesChannel)
        .outerjoin(
            PlatformConnection,
            and_(
                PlatformConnection.tenant_id == SalesChannel.tenant_id,
                PlatformConnection.channel_id == SalesChannel.channel_id,
            ),
        )
        .where(SalesChannel.tenant_id == tenant_id, SalesChannel.channel_id == channel_id)
    )
    channel = connection.execute(statement).first()
    if (
        channel is None
        or channel.kind != PLATFORM
        or channel.status != ACTIVE
        or channel.mode is None
        or channel.mode == MANUAL
    ):
        return None
    return _system_principal(connection, tenant_id)


def _system_principal(connection: Connection, tenant_id: str) -> str:
    """这个租户的 system 主体；没有是开通的缺陷，抛 RuntimeError。"""
    credential_id = connection.execute(
        select(Credential.credential_id)
        .where(Credential.tenant_id == tenant_id, Credential.kind == SYSTEM)
        .order_by(Credential.credential_id)
        .limit(1)
    ).scalar_one_or_none()
    if credential_id is None:
        raise RuntimeError(f"tenant {tenant_id} has no system principal")
    return credential_id


def record_auto_event(
    engine: Engine,
    tenant_id: str,
    channel_id: str,
    platform_order_id: str,
    platform_line_id: str,
    action: str,
    event_key: str,
    source: str,
    payload,
) -> bool:
    """把 webhook 或补拉的一个事件写进收件箱；写了（含重复送达、什么也不改）返回真。

    参数不合规（source 不是 webhook、pull，action 为 return_received，payload 不合格，
    created 带了平台行，订单号为空或超长）是调用方的缺陷，抛 ValueError、不写。
    入口不是这个租户 active 的 platform 入口、或它的连接为人工模式时不写，返回假
    （§5.10 引用格式第 1 条）。数据库错误照常抛出（webhook 据此回 5xx）。
    """
    if source not in AUTO_SOURCES:
        raise ValueError(f"not an automatic event source: {source!r}")
    if action == RETURN_RECEIVED:
        raise ValueError("return_received is only registered from the backoffice")
    parsed = parse_payload(action, payload)
    _check_platform_key(platform_order_id, "platform_order_id", allow_empty=False)
    # 含换行符的订单号取不了订单锁（order_lock_name 拒绝），写进来就永远处理不了。
    if "\n" in platform_order_id:
        raise ValueError(f"invalid platform_order_id: {platform_order_id!r}")
    _check_platform_key(platform_line_id, "platform_line_id", allow_empty=True)
    if action == CREATED and platform_line_id != "":
        raise ValueError("created is an order-level action; platform_line_id must be empty")
    if not isinstance(event_key, str) or not event_key:
        raise ValueError(f"invalid event_key: {event_key!r}")
    stored_key = _stored_event_key(event_key, parsed)

    with engine.connect() as connection:
        credential_id = _system_credential(connection, tenant_id, channel_id)
        # 结束读隐式开的事务：写入在自己的短事务里提交。
        connection.rollback()
        if credential_id is None:
            return False
        table = PlatformEvent.__table__
        statement = mysql_insert(table).values(
            event_row_id=new_id(EVENT_ID_PREFIX),
            tenant_id=tenant_id,
            channel_id=channel_id,
            platform_order_id=platform_order_id,
            platform_line_id=platform_line_id,
            action=action,
            event_key=stored_key,
            source=source,
            payload=payload,
            received_at=NOW,
            credential_id=credential_id,
        )
        # 重复送达撞上去重键时只把 event_row_id 设为自身，即什么也不改（§5.10 存放）。
        statement = statement.on_duplicate_key_update(event_row_id=table.c.event_row_id)
        with connection.begin():
            connection.execute(statement)
    return True


# --- 找有未处理事件的订单 ---


@dataclass(frozen=True)
class PendingOrder:
    tenant_id: str
    channel_id: str
    platform_order_id: str


def orders_with_pending_events(engine: Engine, limit: int) -> list[PendingOrder]:
    """有未处理事件的订单：按各订单最早未处理事件的 received_at 升序、
    再按三列升序取前 limit 个。

    一直处理不掉的订单会排在前面；作业怎样不被它们占满由作业定。
    """
    if type(limit) is not int or limit < 1:
        raise ValueError(f"limit must be a positive integer, got {limit!r}")
    key = (PlatformEvent.tenant_id, PlatformEvent.channel_id, PlatformEvent.platform_order_id)
    statement = (
        select(*key)
        .where(PlatformEvent.applied_at.is_(None))
        .group_by(*key)
        .order_by(func.min(PlatformEvent.received_at), *key)
        .limit(limit)
    )
    with engine.connect() as connection:
        return [PendingOrder(*row) for row in connection.execute(statement)]


# --- 处理一个订单 ---


@dataclass(frozen=True)
class RoundResult:
    """这一轮的结果：写了 applied_at 的事件个数，停在哪个事件（event_row_id）与原因。

    处理完时 stopped_at 与 reason 都为 None；锁忙时 stopped_at 为 None。
    """

    processed: int
    stopped_at: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class _Order:
    tenant_id: str
    channel_id: str
    platform_order_id: str


@dataclass(frozen=True)
class _Channel:
    pool_id: str
    platform: str | None
    platform_shop_id: str | None


@dataclass(frozen=True)
class _Event:
    event_row_id: str
    action: str
    payload: Payload
    received_at: datetime
    source: str
    credential_id: str  # 自动来源为 system 主体，manual 为登记它的管理员


@dataclass(frozen=True)
class _Prepared:
    """第 3 步的结果：created 各行解析出的 SKU（找不到为 None；
    别的动作为空）与涉及的 SKU；ready_to_ship 另有确认销售要的 CommitSkus。
    """

    skus: Mapping[str, str | None]
    sku_ids: tuple[str, ...]  # 升序
    commit_skus: CommitSkus | None = None


@dataclass
class _Allocation:
    """锁住的 channel_allocations 行，扣配额、取消时就地改。"""

    quota: int
    pending: int
    manual_confirmed_quota: int | None
    review_hold: bool


@dataclass(frozen=True)
class _Line:
    """锁住的平台订单行。"""

    platform_line_id: str
    sku_id: str | None
    quantity: int
    cancelled_qty: int
    quota_taken: int
    quota_returned: int
    line_ref: str | None


@dataclass(frozen=True)
class _RestoreRows:
    """取消加回要用的锁住的行（按 sku_id）。"""

    available: Mapping[str, int]  # 至少有 listed 里的 SKU
    safety_stock: Mapping[str, int]
    # (channel_id, sku_id) → 这个池全部 platform 入口的配额行
    allocations: Mapping[tuple[str, str], _Allocation]
    listed: frozenset[str]  # 这个入口上有 active 平台商品的 SKU


@dataclass(frozen=True)
class _Receiving:
    """恢复条件第 1 项要用的：这个入口的连接与人工核对。"""

    mode: str
    status: str
    manual_overdue: bool


@dataclass(frozen=True)
class _Missing:
    """第 4 步越过了阶段，缺 action 这个前序动作（已回滚）。"""

    action: str


# 第 4 步的结果（不向外）；另有 _Missing 与这一轮停下的原因。
_DONE = "done"  # 写了 applied_at
_ALREADY_APPLIED = "already_applied"  # 收件箱行已处理过，提交了空事务
_RESOLVE_AGAIN = "resolve_again"  # 重新解析的 SKU 与第 3 步不同
_REDO = "redo"  # 命令抛了 RedoCommand，整笔事务已回滚


def _check_ttl(reservation_ttl_seconds) -> None:
    if type(reservation_ttl_seconds) is not int or reservation_ttl_seconds < 1:
        raise ValueError(
            f"reservation_ttl_seconds must be a positive integer, got {reservation_ttl_seconds!r}"
        )


def process_order(
    engine: Engine,
    tenant_id: str,
    channel_id: str,
    platform_order_id: str,
    *,
    reservation_ttl_seconds: int,
) -> RoundResult:
    """处理一个订单这一轮的事件（§5.10 处理顺序）；异常不向外抛。

    预留有效期由调用方传入（以后作业从 Settings 取），执行器不读配置。
    """
    _check_ttl(reservation_ttl_seconds)
    order = _Order(tenant_id, channel_id, platform_order_id)
    name = order_lock_name(engine.url.database or "", tenant_id, channel_id, platform_order_id)
    try:
        connection = engine.connect()
    except SQLAlchemyError:
        _log.warning("connecting for a platform order failed", exc_info=True)
        return RoundResult(0, None, LOCK_BUSY)
    try:
        if not _acquire(connection, name):
            return RoundResult(0, None, LOCK_BUSY)
        return _run_round(connection, order, reservation_ttl_seconds)
    finally:
        _finish(connection)


def _acquire(connection: Connection, name: str) -> bool:
    """GET_LOCK(订单锁名, 等待秒数)：1 取到；0（超时）、NULL 或出错
    都算取不到，什么也不写。
    """
    try:
        acquired = connection.execute(
            text("SELECT GET_LOCK(:name, :timeout)"),
            {"name": name, "timeout": ORDER_LOCK_WAIT_SECONDS},
        ).scalar()
        # 结束这条语句隐式开的事务：之后的读在取到锁之后才建立快照。
        connection.rollback()
    except SQLAlchemyError:
        _log.warning("GET_LOCK on a platform order failed", exc_info=True)
        return False
    return acquired == 1


def _finish(connection: Connection) -> None:
    """设回默认隔离级别、释放命名锁，再放回连接池；连接已作废或出错时作废它、不放回。"""
    if not connection.invalidated:
        try:
            connection.rollback()
            connection.execute(text(_DEFAULT_ISOLATION))
            connection.execute(text("SELECT RELEASE_ALL_LOCKS()"))
            connection.rollback()
        except SQLAlchemyError:
            _log.warning(
                "releasing a platform order failed, discarding the connection", exc_info=True
            )
            try:
                connection.invalidate()
            except SQLAlchemyError:
                _log.warning("invalidating a connection failed", exc_info=True)
    try:
        connection.close()
    except SQLAlchemyError:
        _log.warning("closing a platform order connection failed", exc_info=True)


def _log_failure(order: _Order, event_row_id: str | None, error: BaseException) -> None:
    """一行进程日志：订单、事件、异常类型与 MySQL 错误码，不含 payload 与语句参数。"""
    code = getattr(getattr(error, "orig", None), "args", ())[:1]
    _log.warning(
        "platform event processing failed (tenant_id=%s, channel_id=%s, platform_order_id=%s,"
        " event_row_id=%s): %s%s",
        order.tenant_id,
        order.channel_id,
        order.platform_order_id,
        event_row_id,
        type(error).__name__,
        f" {code[0]}" if code else "",
    )


def _rollback_quietly(connection: Connection) -> None:
    if connection.invalidated:
        return
    try:
        connection.rollback()
    except SQLAlchemyError:
        _log.warning("rolling back failed", exc_info=True)


def _run_round(connection: Connection, order: _Order, ttl_seconds: int) -> RoundResult:
    try:
        channel = _read_channel(connection, order)
        events = _pending_events(connection, order)
        connection.rollback()
    except Exception as error:
        _rollback_quietly(connection)
        _log_failure(order, None, error)
        return RoundResult(0, None, FAILED)

    processed = 0
    queue = list(events)
    promoted: set[str] = set()
    while queue:
        event = queue[0]
        if event.action == RETURN_RECEIVED:
            shipped = [
                other
                for other in queue[1:]
                if other.action == SHIPPED and other.event_row_id not in promoted
            ]
            if shipped:
                # 退货要先记发货：这一轮还没处理的 shipped 依次排到它前面。
                for other in shipped:
                    promoted.add(other.event_row_id)
                    queue.remove(other)
                queue[0:0] = shipped
                continue
        outcome = _process_event(connection, order, channel, event, ttl_seconds)
        if isinstance(outcome, _Missing):
            prior = next(
                (
                    other
                    for other in queue[1:]
                    if other.action == outcome.action and other.event_row_id not in promoted
                ),
                None,
            )
            if prior is not None:
                # 先处理缺的那个动作（不看平台时间），再回到这个事件。
                promoted.add(prior.event_row_id)
                queue.remove(prior)
                queue.insert(0, prior)
                continue
            reason = _request_pull(connection, order, event, outcome.action)
            return RoundResult(processed, event.event_row_id, reason)
        if outcome == _DONE:
            processed += 1
        elif outcome != _ALREADY_APPLIED:
            return RoundResult(processed, event.event_row_id, outcome)
        queue.pop(0)
    try:
        _clear_pull(connection, order)
    except Exception as error:
        _rollback_quietly(connection)
        _log_failure(order, None, error)
        return RoundResult(processed, None, FAILED)
    return RoundResult(processed)


def _request_pull(connection: Connection, order: _Order, event: _Event, missing: str) -> str:
    """缺的前序动作不在收件箱里：短事务把 next_pull_at 设为数据库当前时间
    （已非空就不动，pull_attempts 不动）；平台订单还不存在（缺 created）时什么也不写。
    返回 MISSING_PRIOR，出错为 FAILED。
    """
    if missing == CREATED:
        return MISSING_PRIOR
    try:
        with connection.begin():
            connection.execute(
                update(PlatformOrder)
                .where(*_order_where(order), PlatformOrder.next_pull_at.is_(None))
                .values(next_pull_at=NOW, updated_at=NOW)
            )
    except Exception as error:
        _rollback_quietly(connection)
        _log_failure(order, event.event_row_id, error)
        return FAILED
    return MISSING_PRIOR


def _clear_pull(connection: Connection, order: _Order) -> None:
    """这个订单没有未处理的事件了，而 next_pull_at 非空或 pull_attempts 大于 0：
    短事务清为空与 0（缺的动作补齐、全部处理完）。
    """
    unprocessed = (
        select(PlatformEvent.event_row_id)
        .where(
            PlatformEvent.tenant_id == order.tenant_id,
            PlatformEvent.channel_id == order.channel_id,
            PlatformEvent.platform_order_id == order.platform_order_id,
            PlatformEvent.applied_at.is_(None),
        )
        .exists()
    )
    with connection.begin():
        connection.execute(
            update(PlatformOrder)
            .where(
                *_order_where(order),
                or_(PlatformOrder.next_pull_at.is_not(None), PlatformOrder.pull_attempts > 0),
                ~unprocessed,
            )
            .values(next_pull_at=None, pull_attempts=0, updated_at=NOW)
        )


def _order_where(order: _Order) -> tuple:
    return (
        PlatformOrder.tenant_id == order.tenant_id,
        PlatformOrder.channel_id == order.channel_id,
        PlatformOrder.platform_order_id == order.platform_order_id,
    )


def _read_channel(connection: Connection, order: _Order) -> _Channel:
    """订单的入口；收件箱行的外键保证它在这个租户里，读不到是程序错误。"""
    columns = (SalesChannel.pool_id, SalesChannel.platform, SalesChannel.platform_shop_id)
    statement = select(*columns).where(
        SalesChannel.tenant_id == order.tenant_id,
        SalesChannel.channel_id == order.channel_id,
    )
    row = connection.execute(statement).first()
    if row is None:
        raise RuntimeError(f"channel {order.channel_id} is not in the tenant")
    return _Channel(row.pool_id, row.platform, row.platform_shop_id)


def _pending_events(connection: Connection, order: _Order) -> list[_Event]:
    """第 2 步：这个订单全部未处理的事件，按 sort_key 排好。"""
    statement = select(
        PlatformEvent.event_row_id,
        PlatformEvent.action,
        PlatformEvent.payload,
        PlatformEvent.received_at,
        PlatformEvent.source,
        PlatformEvent.credential_id,
    ).where(
        PlatformEvent.tenant_id == order.tenant_id,
        PlatformEvent.channel_id == order.channel_id,
        PlatformEvent.platform_order_id == order.platform_order_id,
        PlatformEvent.applied_at.is_(None),
    )
    events = [
        _Event(
            event_row_id=row.event_row_id,
            action=row.action,
            payload=parse_payload(row.action, row.payload),
            received_at=row.received_at,
            source=row.source,
            credential_id=row.credential_id,
        )
        for row in connection.execute(statement)
    ]
    return sorted(
        events,
        key=lambda event: sort_key(
            event.payload.platform_time, event.action, event.received_at, event.event_row_id
        ),
    )


def _process_event(
    connection: Connection, order: _Order, channel: _Channel, event: _Event, ttl_seconds: int
) -> str | _Missing:
    """第 3–8 步；返回 _DONE、_ALREADY_APPLIED、_Missing 或这一轮停下的原因。

    重新解析的 SKU 不同与命令要求重做各最多从第 3 步重做一次；
    提交了才写命令登记的安全事件。
    """
    resolved = redone = 0
    while True:
        events: list[SecurityEventRecord] = []
        try:
            prepared = _prepare(connection, order, channel, event)
            hook = _between_prepare_and_lock
            if hook is not None:
                hook(connection)
                # 结束钩子读库时隐式开的事务：第 4 步的事务在这之后才开。
                connection.rollback()
            outcome = _locked(connection, order, channel, event, prepared, ttl_seconds, events)
        except Exception as error:
            _rollback_quietly(connection)
            _log_failure(order, event.event_row_id, error)
            return FAILED
        if outcome == _RESOLVE_AGAIN:
            resolved += 1
            if resolved >= RESOLVE_ATTEMPTS:
                return LISTING_CHANGED
            continue
        if outcome == _REDO:
            redone += 1
            if redone >= REDO_ATTEMPTS:
                return REDO_AGAIN
            continue
        if outcome == _DONE:
            _write_security_events(connection, events)
        return outcome


def _write_security_events(connection: Connection, events: Iterable[SecurityEventRecord]) -> None:
    """事务提交之后在同一连接上写（§7.2，同 HTTP 执行器）；写入失败只记进程日志。"""
    for event in events:
        record_security_event(event, engine=connection.engine, connection=connection)


# --- 第 3 步：开事务之前 ---


def _resolve_listings(
    connection: Connection, order: _Order, lines: Iterable[CreatedLine], *, share: bool
) -> dict[str, str | None]:
    """按当前 active 的平台商品解析各行的 SKU：platform_line_id → sku_id，找不到为 None。

    share 为真时 FOR SHARE 读（第 4 步）；按 (平台商品 ID, 规格 ID) 升序逐个读。
    """
    lines = list(lines)
    keys = sorted({(line.platform_item_id, line.platform_model_id) for line in lines})
    found: dict[tuple[str, str], str | None] = {}
    for item_id, model_id in keys:
        statement = select(SkuListing.sku_id, SkuListing.status).where(
            SkuListing.tenant_id == order.tenant_id,
            SkuListing.channel_id == order.channel_id,
            SkuListing.platform_item_id == item_id,
            SkuListing.platform_model_id == model_id,
        )
        if share:
            statement = statement.with_for_update(read=True)
        row = connection.execute(statement).first()
        # 平台规格在这个入口上唯一（uq_sku_listings_platform_ids）；
        # inactive 的行算找不到。
        active = row is not None and row.status == ACTIVE
        found[(item_id, model_id)] = row.sku_id if active else None
    return {
        line.platform_line_id: found[(line.platform_item_id, line.platform_model_id)]
        for line in lines
    }


def _settled_skus(connection: Connection, order: _Order) -> list[str]:
    """已有平台订单行上已定下的 SKU（升序）。

    取这个订单的全部订单行，不按事件挑（多建配额行无害）。
    """
    statement = (
        select(PlatformOrderLine.sku_id)
        .join(
            PlatformOrder,
            and_(
                PlatformOrder.tenant_id == PlatformOrderLine.tenant_id,
                PlatformOrder.platform_order_row_id == PlatformOrderLine.platform_order_row_id,
            ),
        )
        .where(
            PlatformOrder.tenant_id == order.tenant_id,
            PlatformOrder.channel_id == order.channel_id,
            PlatformOrder.platform_order_id == order.platform_order_id,
            PlatformOrderLine.sku_id.is_not(None),
        )
        .distinct()
    )
    return sorted(connection.execute(statement).scalars())


def _platform_channels(connection: Connection, tenant_id: str, pool_id: str) -> list[str]:
    """这个池全部 platform 入口（不论状态），按 channel_id 升序。"""
    statement = (
        select(SalesChannel.channel_id)
        .where(
            SalesChannel.tenant_id == tenant_id,
            SalesChannel.pool_id == pool_id,
            SalesChannel.kind == PLATFORM,
        )
        .order_by(SalesChannel.channel_id)
    )
    return list(connection.execute(statement).scalars())


def _balance_upsert(tenant_id: str, pool_id: str, sku_id: str) -> Executable:
    """同 app/services/idempotency.py 的余额行预建：只给四列，已有的行不改。"""
    table = StockBalance.__table__
    statement = mysql_insert(table).values(
        balance_id=new_id("bal"), tenant_id=tenant_id, pool_id=pool_id, sku_id=sku_id
    )
    return statement.on_duplicate_key_update(balance_id=table.c.balance_id)


def _pool_allocation_upsert(tenant_id: str, pool_id: str, sku_id: str) -> Executable:
    table = PoolAllocation.__table__
    statement = mysql_insert(table).values(tenant_id=tenant_id, pool_id=pool_id, sku_id=sku_id)
    return statement.on_duplicate_key_update(sku_id=table.c.sku_id)


def _channel_allocation_upsert(
    tenant_id: str, channel_id: str, pool_id: str, sku_id: str
) -> Executable:
    table = ChannelAllocation.__table__
    statement = mysql_insert(table).values(
        tenant_id=tenant_id, channel_id=channel_id, pool_id=pool_id, sku_id=sku_id
    )
    return statement.on_duplicate_key_update(sku_id=table.c.sku_id)


def _upsert(connection: Connection, statement: Executable) -> None:
    """外键失败（1452）只回滚这一条语句、跳过；别的错误照抛。"""
    try:
        connection.execute(statement)
    except IntegrityError as exc:
        if getattr(exc.orig, "args", ())[:1] != (_NO_REFERENCED_ROW,):
            raise


def _prepare(connection: Connection, order: _Order, channel: _Channel, event: _Event) -> _Prepared:
    """第 3 步：解析 SKU，预建余额行与两级配额行，设 READ COMMITTED，关已到期的预留。

    返回时连接上没有打开的事务。
    """
    if event.action == CREATED:
        payload: CreatedPayload = event.payload
        skus = _resolve_listings(connection, order, payload.lines, share=False)
        sku_ids = sorted({sku_id for sku_id in skus.values() if sku_id is not None})
    else:
        skus = {}
        sku_ids = _settled_skus(connection, order)
    commit_skus = None
    balance_ids = sku_ids
    if event.action == READY_TO_SHIP:
        # 确认销售要的 prepared：这些 SKU 及其合并去向（同 HTTP 接口，§5.7）。
        commit_skus = CommitSkus(order.tenant_id, channel.pool_id, sku_ids)
        balance_ids = sorted({sku_id for _pool_id, sku_id in commit_skus.balance_rows(connection)})
    channel_ids = _platform_channels(connection, order.tenant_id, channel.pool_id)
    # 结束读隐式开的事务：预建在自己的短事务里提交。
    connection.rollback()
    if balance_ids:
        tenant_id, pool_id = order.tenant_id, channel.pool_id
        with connection.begin():
            for sku_id in balance_ids:
                _upsert(connection, _balance_upsert(tenant_id, pool_id, sku_id))
            for sku_id in sku_ids:
                _upsert(connection, _pool_allocation_upsert(tenant_id, pool_id, sku_id))
            for sku_id in sku_ids:
                for channel_id in channel_ids:
                    statement = _channel_allocation_upsert(tenant_id, channel_id, pool_id, sku_id)
                    _upsert(connection, statement)
    connection.execute(text(_READ_COMMITTED))
    connection.rollback()
    if balance_ids:
        close_due_before_reserving(connection, order.tenant_id, channel.pool_id, balance_ids)
        connection.rollback()
    return _Prepared(skus, tuple(sku_ids), commit_skus)


# --- 第 4、7 步：事务里 ---


def _locked(
    connection: Connection,
    order: _Order,
    channel: _Channel,
    event: _Event,
    prepared: _Prepared,
    ttl_seconds: int,
    events: list[SecurityEventRecord],
) -> str | _Missing:
    """开事务加锁、判定、改量并写收件箱行；返回 _DONE、_ALREADY_APPLIED、_RESOLVE_AGAIN、
    _REDO、_Missing 或 BLOCKED。只有 _DONE 与 _ALREADY_APPLIED 提交，其余回滚；
    异常时回滚再抛出。命令登记的安全事件放进 events，由调用方在提交之后写。
    """
    transaction = connection.begin()
    try:
        outcome = _decide(connection, order, channel, event, prepared, ttl_seconds, events)
        if outcome in (_DONE, _ALREADY_APPLIED):
            transaction.commit()
        else:
            transaction.rollback()
        return outcome
    except RedoCommand:
        # 回滚整笔事务（含被拒仍保留的写入），这次登记的事件不写；由调用方从第 3 步重做。
        _rollback_transaction(transaction)
        events.clear()
        return _REDO
    except BaseException:
        _rollback_transaction(transaction)
        raise


def _rollback_transaction(transaction: Transaction) -> None:
    if not transaction.is_active:
        return
    try:
        transaction.rollback()
    except SQLAlchemyError:
        _log.warning("rolling back failed", exc_info=True)


def _decide(
    connection: Connection,
    order: _Order,
    channel: _Channel,
    event: _Event,
    prepared: _Prepared,
    ttl_seconds: int,
    events: list[SecurityEventRecord],
) -> str | _Missing:
    applied_at = connection.execute(
        select(PlatformEvent.applied_at)
        .where(
            PlatformEvent.tenant_id == order.tenant_id,
            PlatformEvent.event_row_id == event.event_row_id,
        )
        .with_for_update()
    ).scalar_one()
    if applied_at is not None:
        # 处理过了（去重）：提交空事务。
        return _ALREADY_APPLIED

    platform_order = connection.execute(
        select(
            PlatformOrder.platform_order_row_id,
            PlatformOrder.order_ref,
            PlatformOrder.stage,
            PlatformOrder.ready_at,
            PlatformOrder.attention,
        )
        .where(*_order_where(order))
        .with_for_update()
    ).first()
    stage = None if platform_order is None else platform_order.stage
    ready_at_set = platform_order is not None and platform_order.ready_at is not None

    allocations: dict[str, _Allocation] = {}
    if event.action == CREATED and platform_order is None:
        allocations = _lock_allocations(connection, order, prepared.sku_ids)
        skus = _resolve_listings(connection, order, event.payload.lines, share=True)
        if skus != prepared.skus:
            return _RESOLVE_AGAIN

    classification = classify(stage, event.action, ready_at_set)
    if classification.outcome is Outcome.IGNORED:
        _mark_applied(connection, order, event, IGNORED, None)
        return _DONE
    if classification.outcome is Outcome.MISSING_PRIOR:
        return _Missing(classification.missing)
    if classification.outcome is Outcome.BLOCKED:
        return BLOCKED
    if classification.outcome is Outcome.ATTENTION:
        # 与阶段矛盾：阶段不变。
        current = platform_order.attention
        _write_attention(connection, order, current, STAGE_CONFLICT, STATE_CONFLICT, holds=False)
        _mark_applied(connection, order, event, ATTENTION, STATE_CONFLICT)
        return _DONE
    if event.action == CREATED:
        result, result_code = _apply_created(
            connection, order, channel, event.payload, prepared.skus, allocations
        )
        _mark_applied(connection, order, event, result, result_code)
        return _DONE
    if event.action == CANCELLED and stage in (UNPAID, HELD):
        result, result_code = _apply_cancelled(
            connection, order, channel, event.payload, platform_order
        )
        _mark_applied(connection, order, event, result, result_code)
        return _DONE
    if event.action == READY_TO_SHIP:
        result, result_code = _apply_ready(
            connection, order, channel, event, platform_order, prepared, ttl_seconds, events
        )
        _mark_applied(connection, order, event, result, result_code)
        return _DONE
    if stage == COMMITTED and event.action in (SHIPPED, CANCELLED, RETURN_RECEIVED):
        # process_order 处理 manual 行时操作者为空（后台登记时由后台传入管理员）。
        result, result_code = _apply_after_commit(
            connection, order, channel, event, platform_order, ttl_seconds, None, events
        )
        _mark_applied(connection, order, event, result, result_code)
        return _DONE
    raise RuntimeError(f"{event.action} on a {stage} order was classified as apply")


def _hold(connection: Connection, order: _Order, reason: str, code: str | None) -> None:
    """订单设为（或保持）held：让订单进 held 的原因总是覆盖 attention 与 attention_code。"""
    connection.execute(
        update(PlatformOrder)
        .where(*_order_where(order))
        .values(stage=HELD, attention=reason, attention_code=code, updated_at=NOW)
    )


def _write_attention(
    connection: Connection,
    order: _Order,
    current: str | None,
    reason: str,
    code: str | None,
    *,
    holds: bool,
) -> None:
    """写订单的 attention 与 attention_code。

    holds 为真（让订单进 held 的原因：后台要照它重试）时总是覆盖；
    否则订单已有 attention 时不覆盖，原因只留在这个事件的 result_code 里。
    """
    if not holds and current is not None:
        return
    connection.execute(
        update(PlatformOrder)
        .where(*_order_where(order))
        .values(attention=reason, attention_code=code, updated_at=NOW)
    )


def _lock_channel_allocations(
    connection: Connection, tenant_id: str, channel_ids: Iterable[str], sku_ids: Iterable[str]
) -> dict[tuple[str, str], _Allocation]:
    """按 sku_id、再按 channel_id 升序 FOR UPDATE 锁 channel_allocations 行，
    返回 (channel_id, sku_id) → 行。

    行在第 3 步预建过；读不到是程序错误。
    """
    allocations: dict[tuple[str, str], _Allocation] = {}
    channel_ids = sorted(set(channel_ids))
    for sku_id in sorted(set(sku_ids)):
        for channel_id in channel_ids:
            row = connection.execute(
                select(
                    ChannelAllocation.quota,
                    ChannelAllocation.pending,
                    ChannelAllocation.manual_confirmed_quota,
                    ChannelAllocation.review_hold,
                )
                .where(
                    ChannelAllocation.tenant_id == tenant_id,
                    ChannelAllocation.channel_id == channel_id,
                    ChannelAllocation.sku_id == sku_id,
                )
                .with_for_update()
            ).first()
            if row is None:
                raise RuntimeError(
                    f"channel allocation for channel {channel_id} and sku {sku_id}"
                    " was not pre-created"
                )
            allocations[(channel_id, sku_id)] = _Allocation(
                row.quota, row.pending, row.manual_confirmed_quota, bool(row.review_hold)
            )
    return allocations


def _lock_allocations(
    connection: Connection, order: _Order, sku_ids: Iterable[str]
) -> dict[str, _Allocation]:
    """按 sku_id 升序 FOR UPDATE 锁这个入口在这些 SKU 上的 channel_allocations 行。"""
    locked = _lock_channel_allocations(connection, order.tenant_id, (order.channel_id,), sku_ids)
    return {sku_id: allocation for (_channel_id, sku_id), allocation in locked.items()}


def _insert_order(
    connection: Connection,
    order: _Order,
    platform_order_row_id: str,
    ref: str,
    stage: str,
    attention: str | None,
) -> None:
    """写平台订单一行；其余列取数据库默认值。"""
    connection.execute(
        insert(PlatformOrder).values(
            platform_order_row_id=platform_order_row_id,
            tenant_id=order.tenant_id,
            channel_id=order.channel_id,
            platform_order_id=order.platform_order_id,
            order_ref=ref,
            stage=stage,
            attention=attention,
            attention_code=None,
        )
    )


def _apply_created(
    connection: Connection,
    order: _Order,
    channel: _Channel,
    payload: CreatedPayload,
    skus: Mapping[str, str | None],
    allocations: dict[str, _Allocation],
) -> tuple[str, str | None]:
    """建平台订单与各行，按 §3.1 扣配额、加 pending；
    返回收件箱行的 result 与 result_code。
    """
    unmapped = any(skus[line.platform_line_id] is None for line in payload.lines)
    platform_order_row_id = new_id(ORDER_ID_PREFIX)
    _insert_order(
        connection,
        order,
        platform_order_row_id,
        order_ref(channel.platform, channel.platform_shop_id, order.platform_order_id),
        HELD if unmapped else UNPAID,
        UNMAPPED if unmapped else None,
    )

    rows = []
    changed: set[str] = set()
    for line in sorted(payload.lines, key=lambda line: line.platform_line_id):
        sku_id = skus[line.platform_line_id]
        taken = 0
        if sku_id is not None:
            allocation = allocations[sku_id]
            taken, allocation.quota = take_quota(line.quantity, allocation.quota)
            allocation.pending += line.quantity
            # 0.24：卖家后台的显示量随平台卖出自己减，最多减到 0（§3.2）。
            if allocation.manual_confirmed_quota is not None:
                allocation.manual_confirmed_quota -= min(
                    line.quantity, allocation.manual_confirmed_quota
                )
            changed.add(sku_id)
        rows.append(
            {
                "tenant_id": order.tenant_id,
                "platform_order_row_id": platform_order_row_id,
                "platform_line_id": line.platform_line_id,
                "platform_item_id": line.platform_item_id,
                "platform_model_id": line.platform_model_id,
                "sku_id": sku_id,
                "quantity": line.quantity,
                "quota_taken": taken,
            }
        )
    connection.execute(insert(PlatformOrderLine), rows)

    for sku_id in sorted(changed):
        allocation = allocations[sku_id]
        connection.execute(
            update(ChannelAllocation)
            .where(
                ChannelAllocation.tenant_id == order.tenant_id,
                ChannelAllocation.channel_id == order.channel_id,
                ChannelAllocation.sku_id == sku_id,
            )
            .values(
                quota=allocation.quota,
                pending=allocation.pending,
                manual_confirmed_quota=allocation.manual_confirmed_quota,
                updated_at=NOW,
            )
        )
    if unmapped:
        return HELD, UNMAPPED
    return APPLIED, None


def _lock_order_lines(
    connection: Connection, tenant_id: str, platform_order_row_id: str
) -> dict[str, _Line]:
    """FOR UPDATE 锁这个平台订单的全部订单行，按 platform_line_id 升序。"""
    statement = (
        select(
            PlatformOrderLine.platform_line_id,
            PlatformOrderLine.sku_id,
            PlatformOrderLine.quantity,
            PlatformOrderLine.cancelled_qty,
            PlatformOrderLine.quota_taken,
            PlatformOrderLine.quota_returned,
            PlatformOrderLine.line_ref,
        )
        .where(
            PlatformOrderLine.tenant_id == tenant_id,
            PlatformOrderLine.platform_order_row_id == platform_order_row_id,
        )
        .order_by(PlatformOrderLine.platform_line_id)
        .with_for_update()
    )
    return {row.platform_line_id: _Line(*row) for row in connection.execute(statement)}


def _cancel_problem(payload: CancelledPayload, lines: Mapping[str, _Line]) -> str | None:
    """行级取消里有这个订单没有的行（unknown_line，优先）或累计取消超过下单件数
    （cancel_exceeds_remaining）时返回原因，否则 None。
    """
    if payload.lines is None:
        return None
    if any(line.platform_line_id not in lines for line in payload.lines):
        return UNKNOWN_LINE
    if any(line.cancelled_total > lines[line.platform_line_id].quantity for line in payload.lines):
        return CANCEL_EXCEEDS_REMAINING
    return None


def _new_cancellations(payload: CancelledPayload, lines: Mapping[str, _Line]) -> dict[str, int]:
    """各行这次新取消的件数（大于 0 的才列），按 platform_line_id 升序。

    不带 lines 时取消各行全部剩余件数；带 lines 时为 cancelled_total − cancelled_qty。
    """
    wanted: dict[str, int] = {}
    if payload.lines is None:
        wanted = {line_id: line.quantity - line.cancelled_qty for line_id, line in lines.items()}
    else:
        for line in payload.lines:
            already = lines[line.platform_line_id].cancelled_qty
            wanted[line.platform_line_id] = line.cancelled_total - already
    return {line_id: wanted[line_id] for line_id in sorted(wanted) if wanted[line_id] > 0}


def _apply_cancelled(
    connection: Connection,
    order: _Order,
    channel: _Channel,
    payload: CancelledPayload,
    platform_order,
) -> tuple[str, str | None]:
    """扣数之前的取消（§3.1，订单 unpaid 或 held）；返回收件箱行的 result 与 result_code。"""
    lines = _lock_order_lines(connection, order.tenant_id, platform_order.platform_order_row_id)
    problem = _cancel_problem(payload, lines)
    if problem is not None:
        _write_attention(connection, order, platform_order.attention, problem, None, holds=False)
        return ATTENTION, problem
    cancelled = _new_cancellations(payload, lines)
    if not cancelled:
        # 同一次取消从另一个来源又到一次。
        return IGNORED, None

    sku_ids = sorted({lines[line_id].sku_id for line_id in cancelled} - {None})
    restore: _RestoreRows | None = None
    allocations: dict[str, _Allocation] = {}
    if sku_ids and platform_order.stage == UNPAID:
        restore = _lock_restore_rows(connection, order, channel, sku_ids)
        allocations = {
            sku_id: restore.allocations[(order.channel_id, sku_id)] for sku_id in sku_ids
        }
    elif sku_ids:
        # 待处理中的订单（Core 从未扣数）被取消一件也不加回，不用锁判恢复条件的行。
        allocations = _lock_allocations(connection, order, sku_ids)
    for line_id, quantity in cancelled.items():
        sku_id = lines[line_id].sku_id
        if sku_id is not None:
            allocations[sku_id].pending -= quantity
    returned: dict[str, int] = {}
    if restore is not None:
        returned = _quota_returns(connection, order, lines, cancelled, restore)

    for line_id, quantity in cancelled.items():
        line = lines[line_id]
        connection.execute(
            update(PlatformOrderLine)
            .where(
                PlatformOrderLine.tenant_id == order.tenant_id,
                PlatformOrderLine.platform_order_row_id == platform_order.platform_order_row_id,
                PlatformOrderLine.platform_line_id == line_id,
            )
            .values(
                cancelled_qty=line.cancelled_qty + quantity,
                quota_returned=line.quota_returned + returned.get(line_id, 0),
            )
        )
    for sku_id in sku_ids:
        allocation = allocations[sku_id]
        connection.execute(
            update(ChannelAllocation)
            .where(
                ChannelAllocation.tenant_id == order.tenant_id,
                ChannelAllocation.channel_id == order.channel_id,
                ChannelAllocation.sku_id == sku_id,
            )
            .values(quota=allocation.quota, pending=allocation.pending, updated_at=NOW)
        )
    remaining = sum(
        line.quantity - line.cancelled_qty - cancelled.get(line_id, 0)
        for line_id, line in lines.items()
    )
    if remaining == 0:
        # attention 不清：后台照它看得出这单曾卡在哪里。
        statement = update(PlatformOrder).where(*_order_where(order))
        connection.execute(statement.values(stage=CANCELLED, updated_at=NOW))
    return APPLIED, None


def _lock_restore_rows(
    connection: Connection, order: _Order, channel: _Channel, sku_ids: list[str]
) -> _RestoreRows:
    """取消加回的锁序（§3.1、§5.10 第 4 步）：池行 FOR SHARE → 余额行 FOR UPDATE
    → pool_allocations 行 FOR UPDATE → 这个池全部 platform 入口的 channel_allocations 行
    FOR UPDATE → 这个入口在这些 SKU 上的平台商品 FOR SHARE；SKU 都按升序。
    """
    _lock_pool(connection, order, channel)
    balances = lock_balance_rows(connection, order.tenant_id, channel.pool_id, sku_ids)
    safety_stock = _lock_pool_allocations(connection, order, channel, sku_ids)
    channel_ids = _platform_channels(connection, order.tenant_id, channel.pool_id)
    allocations = _lock_channel_allocations(connection, order.tenant_id, channel_ids, sku_ids)
    return _RestoreRows(
        available={sku_id: balances[sku_id].quantities.available for sku_id in sku_ids},
        safety_stock=safety_stock,
        allocations=allocations,
        listed=_lock_listings(connection, order, sku_ids),
    )


def _lock_restore_rows_after_commands(
    connection: Connection, order: _Order, channel: _Channel, sku_ids: list[str]
) -> _RestoreRows:
    """已确认之后取消加回的锁序，在命令体之后：pool_allocations 行 FOR UPDATE → 这个池全部
    platform 入口的 channel_allocations 行 FOR UPDATE → 这个入口在这些 SKU 上的平台商品
    FOR SHARE；SKU 都按升序。

    池行与余额行跳过：命令体已锁过余额行。available 只读有生效平台商品的 SKU 的余额行
    （它们没被合并，正是命令体锁过的订单行 SKU，读的是已持有的行）；平台行的 SKU 已被合并时
    它没有生效平台商品，恢复条件判不过，不读它的余额行。
    """
    safety_stock = _lock_pool_allocations(connection, order, channel, sku_ids)
    channel_ids = _platform_channels(connection, order.tenant_id, channel.pool_id)
    allocations = _lock_channel_allocations(connection, order.tenant_id, channel_ids, sku_ids)
    listed = _lock_listings(connection, order, sku_ids)
    balances = lock_balance_rows(connection, order.tenant_id, channel.pool_id, sorted(listed))
    return _RestoreRows(
        available={sku_id: balance.quantities.available for sku_id, balance in balances.items()},
        safety_stock=safety_stock,
        allocations=allocations,
        listed=listed,
    )


def _lock_pool_allocations(
    connection: Connection, order: _Order, channel: _Channel, sku_ids: list[str]
) -> dict[str, int]:
    """按 sku_id 升序 FOR UPDATE 锁 pool_allocations 行，返回各 SKU 的安全库存。"""
    safety_stock: dict[str, int] = {}
    for sku_id in sorted(set(sku_ids)):
        value = connection.execute(
            select(PoolAllocation.safety_stock)
            .where(
                PoolAllocation.tenant_id == order.tenant_id,
                PoolAllocation.pool_id == channel.pool_id,
                PoolAllocation.sku_id == sku_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if value is None:
            raise RuntimeError(f"pool allocation for sku {sku_id} was not pre-created")
        safety_stock[sku_id] = value
    return safety_stock


def _lock_pool(connection: Connection, order: _Order, channel: _Channel) -> None:
    """池行 FOR SHARE；池由入口的外键保证在这个租户里，读不到是程序错误。"""
    pool = connection.execute(
        select(StockPool.pool_id)
        .where(StockPool.tenant_id == order.tenant_id, StockPool.pool_id == channel.pool_id)
        .with_for_update(read=True)
    ).scalar_one_or_none()
    if pool is None:
        raise RuntimeError(f"pool {channel.pool_id} is not in the tenant")


def _lock_listings(connection: Connection, order: _Order, sku_ids: Iterable[str]) -> frozenset[str]:
    """按 sku_id 升序 FOR SHARE 读这个入口在这些 SKU 上的平台商品；
    返回有 active 平台商品的 SKU。
    """
    listed = set()
    for sku_id in sorted(set(sku_ids)):
        statuses = connection.execute(
            select(SkuListing.status)
            .where(
                SkuListing.tenant_id == order.tenant_id,
                SkuListing.channel_id == order.channel_id,
                SkuListing.sku_id == sku_id,
            )
            .order_by(SkuListing.listing_id)
            .with_for_update(read=True)
        ).scalars()
        if ACTIVE in set(statuses):
            listed.add(sku_id)
    return frozenset(listed)


def _receiving(connection: Connection, order: _Order) -> _Receiving | None:
    """这个入口的连接行与人工核对逾期（now 为数据库时间）；没有连接行为 None。"""
    row = connection.execute(
        select(
            PlatformConnection.mode,
            PlatformConnection.status,
            PlatformConnection.mode_changed_at,
        ).where(
            PlatformConnection.tenant_id == order.tenant_id,
            PlatformConnection.channel_id == order.channel_id,
        )
    ).first()
    if row is None:
        return None
    last_checked_until = connection.execute(
        select(func.max(ManualCheck.checked_until)).where(
            ManualCheck.tenant_id == order.tenant_id,
            ManualCheck.channel_id == order.channel_id,
        )
    ).scalar()
    now = connection.execute(text("SELECT CURRENT_TIMESTAMP(6)")).scalar_one()
    overdue = manual_check_overdue(row.mode, last_checked_until, row.mode_changed_at, now)
    return _Receiving(row.mode, row.status, overdue)


def _quota_returns(
    connection: Connection,
    order: _Order,
    lines: Mapping[str, _Line],
    cancelled: Mapping[str, int],
    restore: _RestoreRows,
) -> dict[str, int]:
    """取消（unpaid 的，或已确认之后的退款）按 SKU 判恢复条件、把能加回的就地加到这个入口的 Q；
    返回各行这次加回的件数（platform_line_id → 件数，0 不列）。

    调用前这个入口的 pending 已减去取消的件数（已确认之后的取消不改 pending）。
    一个 SKU 判不过就整份不加（进 F）；没有生效平台商品的 SKU 先判掉（不读它的 available）。
    """
    per_line: dict[str, int] = {}
    by_sku: dict[str, int] = {}
    for line_id, quantity in cancelled.items():
        line = lines[line_id]
        if line.sku_id is None:
            continue
        amount = add_back(quantity, line.quota_taken, line.quota_returned, True)
        if amount:
            per_line[line_id] = amount
            by_sku[line.sku_id] = by_sku.get(line.sku_id, 0) + amount

    receiving: _Receiving | None = None
    returned: dict[str, int] = {}
    for sku_id in sorted(by_sku):
        total = by_sku[sku_id]
        if sku_id not in restore.listed:
            # 恢复条件第 2 项不满足。
            continue
        if receiving is None:
            receiving = _receiving(connection, order)
            if receiving is None:
                # 连接行缺了（开通的缺陷）：判不了第 1 项，不加回。
                return {}
        rows = [
            allocation
            for (_channel_id, row_sku_id), allocation in restore.allocations.items()
            if row_sku_id == sku_id
        ]
        before = unallocated(
            restore.available[sku_id],
            restore.safety_stock[sku_id],
            [allocation.quota for allocation in rows],
            [allocation.pending for allocation in rows],
        )
        # 按加回之后算：这个入口的 Q 加上 total。
        unallocated_after = before - total
        own = restore.allocations[(order.channel_id, sku_id)]
        allowed = restore_allowed(
            receiving.mode,
            receiving.status,
            receiving.manual_overdue,
            True,  # 有生效平台商品，上面已判过
            own.review_hold,
            unallocated_after,
        )
        if not allowed:
            continue
        own.quota += total
        for line_id, amount in per_line.items():
            if lines[line_id].sku_id == sku_id:
                returned[line_id] = amount
    return returned


def _ready_at(event: _Event) -> datetime:
    """payload 的平台时间，没有则用这个事件的 received_at，截到整秒（§5.10）。"""
    value = event.payload.platform_time or event.received_at
    return value.replace(microsecond=0)


def _apply_ready(
    connection: Connection,
    order: _Order,
    channel: _Channel,
    event: _Event,
    platform_order,
    prepared: _Prepared,
    ttl_seconds: int,
    events: list[SecurityEventRecord],
) -> tuple[str, str | None]:
    """进入待发货（§5.10、§3.1，订单 unpaid，或 held 而 ready_at 未记过）；
    返回收件箱行的 result 与 result_code。
    """
    # 写在保存点之外：预留被拒、订单进 held 时也保留，之后重试的确认销售照样用它。
    ready_at = platform_order.ready_at
    if ready_at is None:
        ready_at = _ready_at(event)
        connection.execute(
            update(PlatformOrder)
            .where(*_order_where(order))
            .values(ready_at=ready_at, updated_at=NOW)
        )
    lines = _lock_order_lines(connection, order.tenant_id, platform_order.platform_order_row_id)
    remaining = [
        (line_id, line.sku_id, line.quantity - line.cancelled_qty)
        for line_id, line in lines.items()
    ]
    if any(sku_id is None and quantity > 0 for _line_id, sku_id, quantity in remaining):
        _hold(connection, order, UNMAPPED, None)
        return HELD, UNMAPPED
    assignment = assign_line_refs(remaining)
    try:
        commands = ready_commands(
            platform_order.order_ref, assignment.lines, ready_at.replace(microsecond=0)
        )
    except InvalidDerivedBody:
        _hold(connection, order, INVALID_REQUEST_REASON, INVALID_REQUEST)
        return HELD, INVALID_REQUEST
    if commands is None:
        # 整理之后一行都不剩：不调 Core（§5.10 派生的命令正文）。
        return IGNORED, None
    if prepared.commit_skus is None:
        raise RuntimeError("ready_to_ship was prepared without its commit skus")

    sku_ids = [line.sku_id for line in assignment.lines]
    allocations = _lock_for_commands(connection, order, channel, sku_ids)
    context = command_context(
        tenant_id=order.tenant_id,
        channel_id=order.channel_id,
        pool_id=channel.pool_id,
        credential_id=_system_principal(connection, order.tenant_id),
        event_row_id=event.event_row_id,
        reservation_ttl_seconds=ttl_seconds,
    )
    outcome = reserve_and_commit(connection, context, prepared.commit_skus, commands)
    events.extend(outcome.events)
    if outcome.rejection is not None:
        # 扣数之前被拒（第 6 步）：件数留在 pending。
        code = outcome.rejection.code
        _hold(connection, order, DEFECT if outcome.defect else CORE_REJECTED, code)
        return HELD, code

    _commit_ready(connection, order, platform_order, assignment, allocations)
    if outcome.defect:
        # 缺陷是让人去查的原因，同进 held 的原因一样覆盖已有的。
        _write_attention(connection, order, platform_order.attention, DEFECT, None, holds=True)
        return APPLIED, DEFECT
    return APPLIED, None


def _lock_for_commands(
    connection: Connection, order: _Order, channel: _Channel, sku_ids: list[str]
) -> dict[str, _Allocation]:
    """调命令体之前照 §5.10 第 4 步的锁序先锁：池行 FOR SHARE → 这些 SKU 的余额行
    FOR UPDATE → 这个入口在这些 SKU 上的配额行 FOR UPDATE → 平台商品 FOR SHARE
    （SKU 都按升序）。命令体之后再锁的余额行已持有，SKU 行与商品行排在平台商品之后。
    """
    _lock_pool(connection, order, channel)
    lock_balance_rows(connection, order.tenant_id, channel.pool_id, sku_ids)
    allocations = _lock_allocations(connection, order, sku_ids)
    _lock_listings(connection, order, sku_ids)
    return allocations


def _commit_ready(
    connection: Connection,
    order: _Order,
    platform_order,
    assignment: LineAssignment,
    allocations: dict[str, _Allocation],
) -> None:
    """预留与确认销售都成功：写各平台行的 line_ref，pending 减各行剩余件数，阶段 committed。"""
    for line_id, line_ref in sorted(assignment.line_refs.items()):
        connection.execute(
            update(PlatformOrderLine)
            .where(
                PlatformOrderLine.tenant_id == order.tenant_id,
                PlatformOrderLine.platform_order_row_id == platform_order.platform_order_row_id,
                PlatformOrderLine.platform_line_id == line_id,
            )
            .values(line_ref=line_ref)
        )
    # 每个 Core 行即一个 SKU，件数是合并到它的各平台行剩余件数之和。
    for line in assignment.lines:
        allocation = allocations[line.sku_id]
        allocation.pending -= line.quantity
        connection.execute(
            update(ChannelAllocation)
            .where(
                ChannelAllocation.tenant_id == order.tenant_id,
                ChannelAllocation.channel_id == order.channel_id,
                ChannelAllocation.sku_id == line.sku_id,
            )
            .values(pending=allocation.pending, updated_at=NOW)
        )
    connection.execute(
        update(PlatformOrder).where(*_order_where(order)).values(stage=COMMITTED, updated_at=NOW)
    )


def _after_commit_problem(event: _Event, lines: Mapping[str, _Line]) -> str | None:
    """已确认之后不调 Core 的原因（按 refund_id_missing、cancel_lines_missing、
    missing_platform_id、line_not_committed 的先后），没有为 None。
    """
    payload = event.payload
    if event.action == CANCELLED:
        if payload.refund_id is None:
            return REFUND_ID_MISSING
        if payload.lines is None:
            # 订单级：件数里可能含已交运的，契约要求取平台的件数。
            return CANCEL_LINES_MISSING
    elif event.action == SHIPPED and payload.package_id is None:
        return MISSING_PLATFORM_ID
    elif event.action == RETURN_RECEIVED and payload.return_id is None:
        return MISSING_PLATFORM_ID
    for line in payload.lines:
        platform_line = lines.get(line.platform_line_id)
        if platform_line is None or platform_line.line_ref is None:
            return LINE_NOT_COMMITTED
    return None


def _document_command(
    event: _Event, ref: str, line_refs: Mapping[str, str]
) -> DocumentCommand | None:
    if event.action == SHIPPED:
        return dispatch_command(ref, event.payload, line_refs)
    if event.action == CANCELLED:
        return refund_restock_command(ref, event.payload, line_refs)
    return return_receipt_command(ref, event.payload, line_refs)


def _event_principal(connection: Connection, order: _Order, event: _Event) -> str:
    """执行 Core 命令的身份：自动来源为 system 主体，manual 为收件箱行上的 credential_id。"""
    if event.source == MANUAL_SOURCE:
        return event.credential_id
    return _system_principal(connection, order.tenant_id)


def _apply_after_commit(
    connection: Connection,
    order: _Order,
    channel: _Channel,
    event: _Event,
    platform_order,
    ttl_seconds: int,
    actor: Actor | None,
    events: list[SecurityEventRecord],
) -> tuple[str, str | None]:
    """已确认之后的 shipped、cancelled 与 return_received（§5.10、§3.1，订单 committed）；
    返回收件箱行的 result 与 result_code。actor 为执行命令的操作者（process_order 为空）。
    """
    lines = _lock_order_lines(connection, order.tenant_id, platform_order.platform_order_row_id)
    current = platform_order.attention
    payload = event.payload
    if event.action == CANCELLED and payload.lines is not None:
        known = all(line.platform_line_id in lines for line in payload.lines)
        if known and all(
            line.cancelled_total <= lines[line.platform_line_id].cancelled_qty
            for line in payload.lines
        ):
            # 付款前那次取消从另一个来源晚到（已确认之后的取消不记 cancelled_qty）。
            return IGNORED, None
    problem = _after_commit_problem(event, lines)
    if problem is not None:
        _write_attention(connection, order, current, problem, None, holds=False)
        return ATTENTION, problem

    line_refs = {line_id: line.line_ref for line_id, line in lines.items() if line.line_ref}
    try:
        command = _document_command(event, platform_order.order_ref, line_refs)
    except InvalidDerivedBody:
        _write_attention(
            connection, order, current, INVALID_REQUEST_REASON, INVALID_REQUEST, holds=False
        )
        return ATTENTION, INVALID_REQUEST
    if command is None:
        # 整理之后一行都不剩：不调 Core（§5.10 派生的命令正文）。
        return IGNORED, None

    context = command_context(
        tenant_id=order.tenant_id,
        channel_id=order.channel_id,
        pool_id=channel.pool_id,
        credential_id=_event_principal(connection, order, event),
        event_row_id=event.event_row_id,
        reservation_ttl_seconds=ttl_seconds,
        actor=actor,
    )
    outcome = create_order_document(connection, context, command)
    events.extend(outcome.events)
    if outcome.rejection is not None:
        # 已确认之后被拒（第 6 步）：阶段不变、pending 不动。
        code = outcome.rejection.code
        if outcome.defect:
            _write_attention(connection, order, current, DEFECT, code, holds=True)
        else:
            _write_attention(connection, order, current, CORE_REJECTED, code, holds=False)
        return ATTENTION, code
    if outcome.defect:
        # rejudge 按业务引用返回原结果：不改 Q 与 pending、不加回配额。
        _write_attention(connection, order, current, DEFECT, None, holds=True)
        return APPLIED, DEFECT
    if outcome.answered_by_reference:
        # 同一单据换了 event_key 再到：不改 Q 与 pending。
        return IGNORED, None
    if event.action == CANCELLED:
        _restore_after_refund(connection, order, channel, platform_order, lines, payload)
    return APPLIED, None


def _restore_after_refund(
    connection: Connection,
    order: _Order,
    channel: _Channel,
    platform_order,
    lines: Mapping[str, _Line],
    payload: CancelledPayload,
) -> None:
    """退款回库新生效之后按恢复条件加回 quota 与 quota_returned（§3.1）。

    件数取平台这次退款取消的件数；cancelled_qty、pending 与阶段不改。
    """
    refunded: dict[str, int] = {}
    for line in sorted(payload.lines, key=lambda line: line.platform_line_id):
        refunded[line.platform_line_id] = line.quantity
    sku_ids = sorted({lines[line_id].sku_id for line_id in refunded} - {None})
    if not sku_ids:
        return
    restore = _lock_restore_rows_after_commands(connection, order, channel, sku_ids)
    returned = _quota_returns(connection, order, lines, refunded, restore)
    for line_id, amount in sorted(returned.items()):
        connection.execute(
            update(PlatformOrderLine)
            .where(
                PlatformOrderLine.tenant_id == order.tenant_id,
                PlatformOrderLine.platform_order_row_id == platform_order.platform_order_row_id,
                PlatformOrderLine.platform_line_id == line_id,
            )
            .values(quota_returned=lines[line_id].quota_returned + amount)
        )
    for sku_id in sorted({lines[line_id].sku_id for line_id in returned}):
        allocation = restore.allocations[(order.channel_id, sku_id)]
        connection.execute(
            update(ChannelAllocation)
            .where(
                ChannelAllocation.tenant_id == order.tenant_id,
                ChannelAllocation.channel_id == order.channel_id,
                ChannelAllocation.sku_id == sku_id,
            )
            .values(quota=allocation.quota, updated_at=NOW)
        )


def _mark_applied(
    connection: Connection, order: _Order, event: _Event, result: str, result_code: str | None
) -> None:
    """第 7 步：applied_at 取数据库时间，与改动同一事务提交。"""
    connection.execute(
        update(PlatformEvent)
        .where(
            PlatformEvent.tenant_id == order.tenant_id,
            PlatformEvent.event_row_id == event.event_row_id,
        )
        .values(applied_at=NOW, result=result, result_code=result_code)
    )
