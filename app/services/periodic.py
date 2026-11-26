"""进程内的定时任务：一个守护线程按固定间隔反复执行一个函数。

第一项是安全事件满 1 年的清理（契约 7.2，接入见 app/main.py）；
之后的预留到期扫描（契约 5.2 第 4 条）复用同一机制。
间隔与首次等待由调用代码传入，不设配置项。
每次运行结束记下结果，供就绪检查判断是否停转（契约 8.5，判定在 app/domain/periodic.py）。
"""

import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from app.domain.periodic import FAILED, SUCCEEDED, is_stale

DEFAULT_FIRST_DELAY = 60.0
DEFAULT_INTERVAL = 24 * 60 * 60.0
DEFAULT_STOP_TIMEOUT = 30.0

# 写到 app.api 的子日志器：app/api/requests.py 给 app.api 挂了输出、级别 INFO，生产上看得到。
_log = logging.getLogger("app.api.periodic")


@dataclass(frozen=True)
class TaskState:
    """某一时刻读出的任务状态。

    started_at、last_success_monotonic 是单调时钟的秒数，只用于判定；
    last_success_at、last_failure_at 是 UTC 墙钟，只作展示。
    """

    name: str
    alive: bool
    started_at: float | None
    last_success_monotonic: float | None
    last_outcome: str | None
    last_success_at: datetime | None
    last_failure_at: datetime | None
    stale: bool


class PeriodicTask:
    """启动后先等 first_delay 秒跑第一次，之后每隔 interval 秒跑一次。

    函数抛出异常时记进程日志，线程不退出，下个周期照常。
    """

    def __init__(
        self,
        name: str,
        function: Callable[[], object],
        *,
        interval: float = DEFAULT_INTERVAL,
        first_delay: float = DEFAULT_FIRST_DELAY,
    ):
        self.name = name
        self.function = function
        self.interval = interval
        self.first_delay = first_delay
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        # 以下记录由 _lock 保护：运行线程写，就绪检查的请求线程读。
        self._lock = threading.Lock()
        self._started_at: float | None = None
        self._last_success_monotonic: float | None = None
        self._last_failure_monotonic: float | None = None
        self._last_success_at: datetime | None = None
        self._last_failure_at: datetime | None = None
        self._last_outcome: str | None = None

    def start(self) -> None:
        with self._lock:
            self._started_at = time.monotonic()
        self._thread.start()

    def stop(self, timeout: float = DEFAULT_STOP_TIMEOUT) -> None:
        """通知线程停止并等它结束；正在跑的那一次跑完才结束，最多等 timeout 秒。"""
        stop_all([self], timeout)

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def run_once(self) -> None:
        """跑一次函数并记下结果；抛出的异常记进程日志，不往外抛。"""
        try:
            self.function()
        except Exception:
            _log.exception("periodic task %s failed", self.name)
            self._record(FAILED)
        else:
            self._record(SUCCEEDED)

    def state(self, now: float | None = None) -> TaskState:
        """读出当前状态；now 为单调时钟的秒数，不给时取当前值。"""
        alive = self._thread.is_alive()
        with self._lock:
            started_at = self._started_at
            last_success_monotonic = self._last_success_monotonic
            last_outcome = self._last_outcome
            last_success_at = self._last_success_at
            last_failure_at = self._last_failure_at
        if now is None:
            now = time.monotonic()
        stale = is_stale(
            alive=alive,
            interval=self.interval,
            first_delay=self.first_delay,
            started_at=started_at,
            last_success_at=last_success_monotonic,
            last_outcome=last_outcome,
            now=now,
        )
        return TaskState(
            name=self.name,
            alive=alive,
            started_at=started_at,
            last_success_monotonic=last_success_monotonic,
            last_outcome=last_outcome,
            last_success_at=last_success_at,
            last_failure_at=last_failure_at,
            stale=stale,
        )

    def _record(self, outcome: str) -> None:
        monotonic = time.monotonic()
        wall = datetime.now(UTC)
        with self._lock:
            self._last_outcome = outcome
            if outcome == SUCCEEDED:
                self._last_success_monotonic = monotonic
                self._last_success_at = wall
            else:
                self._last_failure_monotonic = monotonic
                self._last_failure_at = wall

    def _run(self) -> None:
        delay = self.first_delay
        while not self._stopping.wait(delay):
            self.run_once()
            delay = self.interval


def stop_all(tasks: Iterable[PeriodicTask], timeout: float = DEFAULT_STOP_TIMEOUT) -> None:
    """先通知全部任务停止，再用同一个截止时间等它们全部结束（契约 8.6 第 5 条）。

    总共最多等 timeout 秒，不是每个任务各等 timeout 秒；没启动过的任务直接算停下。
    过了截止时间仍没结束的任务各记一行警告。
    """
    tasks = list(tasks)
    for task in tasks:
        task._stopping.set()
    deadline = time.monotonic() + timeout
    for task in tasks:
        if task._thread.is_alive():
            task._thread.join(max(0.0, deadline - time.monotonic()))
    for task in tasks:
        if task._thread.is_alive():
            _log.warning("periodic task %s did not stop within %s seconds", task.name, timeout)
