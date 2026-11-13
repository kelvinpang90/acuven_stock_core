"""进程内的定时任务：一个守护线程按固定间隔反复执行一个函数。

第一项是安全事件满 1 年的清理（契约 7.2，接入见 app/main.py）；
之后的预留到期扫描（契约 5.2 第 4 条）复用同一机制。
间隔与首次等待由调用代码传入，不设配置项。
"""

import logging
import threading
from collections.abc import Callable

DEFAULT_FIRST_DELAY = 60.0
DEFAULT_INTERVAL = 24 * 60 * 60.0
DEFAULT_STOP_TIMEOUT = 30.0

# 写到 app.api 的子日志器：app/api/requests.py 给 app.api 挂了输出、级别 INFO，生产上看得到。
_log = logging.getLogger("app.api.periodic")


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

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = DEFAULT_STOP_TIMEOUT) -> None:
        """通知线程停止并等它结束；正在跑的那一次跑完才结束，最多等 timeout 秒。"""
        self._stopping.set()
        if self._thread.is_alive():
            self._thread.join(timeout)
        if self._thread.is_alive():
            _log.warning("periodic task %s did not stop within %s seconds", self.name, timeout)

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def _run(self) -> None:
        delay = self.first_delay
        while not self._stopping.wait(delay):
            try:
                self.function()
            except Exception:
                _log.exception("periodic task %s failed", self.name)
            delay = self.interval
