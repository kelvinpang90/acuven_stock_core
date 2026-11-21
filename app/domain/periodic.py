"""进程内定时任务的停转判定（契约 8.5）。

纯函数：时刻都是调用方读好的单调时钟秒数，这里不读时钟、不碰数据库。
"""

from collections.abc import Iterable

# 一次运行结束的结果。
SUCCEEDED = "succeeded"
FAILED = "failed"

# periodic.status 的取值。
OK = "ok"
STALE = "stale"

# 超过 2 个运行间隔加这么多秒没有正常结束即 stale。
GRACE_SECONDS = 300.0


def is_stale(
    *,
    alive: bool,
    interval: float,
    first_delay: float,
    started_at: float | None,
    last_success_at: float | None,
    last_outcome: str | None,
    now: float,
) -> bool:
    """按契约 8.5 的三条判定一个任务是否 stale，满足任一条即是。

    1. 线程不在跑（还没启动的任务也算）；
    2. 最近一次结束的运行抛出了异常（last_outcome 为 FAILED）；
    3. 距最近一次正常结束——本进程里还没有过时，距启动后第一次应运行的时刻
       （started_at + first_delay）——超过 2 * interval + GRACE_SECONDS。
    """
    if not alive or started_at is None:
        return True
    if last_outcome == FAILED:
        return True
    since = last_success_at if last_success_at is not None else started_at + first_delay
    return now - since > 2 * interval + GRACE_SECONDS


def periodic_status(stale: Iterable[bool]) -> str:
    """没有 stale 的任务为 ok，否则为 stale。"""
    return STALE if any(stale) else OK
