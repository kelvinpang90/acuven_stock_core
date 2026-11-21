import ast
import unittest
from pathlib import Path

from app.domain import periodic
from app.domain.periodic import FAILED, OK, STALE, SUCCEEDED, is_stale, periodic_status

# 契约 8.5 的两个任务：到期扫描与安全事件清理（间隔、首次等待）。
RESERVATION_EXPIRY = (30.0, 30.0)
SECURITY_EVENT_PURGE = (24 * 60 * 60.0, 60.0)
STARTED_AT = 1000.0


class ModuleTest(unittest.TestCase):
    def test_module_has_no_io_imports_and_reads_no_clock(self):
        source = Path(periodic.__file__).read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertTrue(imported <= {"collections"}, imported)

    def test_grace_is_300_seconds(self):
        self.assertEqual(periodic.GRACE_SECONDS, 300)


class StaleTestMixin:
    """两组参数共用的用例；子类给出 interval 与 first_delay。"""

    interval: float
    first_delay: float

    def stale(self, now, *, alive=True, last_success_at=None, last_outcome=None):
        return is_stale(
            alive=alive,
            interval=self.interval,
            first_delay=self.first_delay,
            started_at=STARTED_AT,
            last_success_at=last_success_at,
            last_outcome=last_outcome,
            now=now,
        )

    @property
    def limit(self) -> float:
        return 2 * self.interval + 300

    def test_success_exactly_at_the_limit_is_not_stale(self):
        success = STARTED_AT + self.first_delay + 5
        now = success + self.limit
        self.assertFalse(self.stale(now, last_success_at=success, last_outcome=SUCCEEDED))

    def test_success_just_over_the_limit_is_stale(self):
        success = STARTED_AT + self.first_delay + 5
        now = success + self.limit + 0.001
        self.assertTrue(self.stale(now, last_success_at=success, last_outcome=SUCCEEDED))

    def test_without_a_success_counts_from_the_first_due_run(self):
        first_due = STARTED_AT + self.first_delay
        self.assertFalse(self.stale(STARTED_AT))
        self.assertFalse(self.stale(first_due + self.limit))
        self.assertTrue(self.stale(first_due + self.limit + 0.001))

    def test_latest_run_failed_is_stale(self):
        self.assertTrue(self.stale(STARTED_AT + self.first_delay, last_outcome=FAILED))
        success = STARTED_AT + self.first_delay
        self.assertTrue(self.stale(success + 1, last_success_at=success, last_outcome=FAILED))

    def test_success_after_a_failure_recovers(self):
        success = STARTED_AT + self.first_delay + self.interval
        self.assertFalse(self.stale(success + 1, last_success_at=success, last_outcome=SUCCEEDED))

    def test_a_thread_not_running_is_stale(self):
        success = STARTED_AT + self.first_delay
        self.assertTrue(
            self.stale(success + 1, alive=False, last_success_at=success, last_outcome=SUCCEEDED)
        )
        # 还没启动的任务。
        self.assertTrue(
            is_stale(
                alive=False,
                interval=self.interval,
                first_delay=self.first_delay,
                started_at=None,
                last_success_at=None,
                last_outcome=None,
                now=STARTED_AT,
            )
        )


class ReservationExpiryStaleTest(StaleTestMixin, unittest.TestCase):
    interval, first_delay = RESERVATION_EXPIRY

    def test_limit_is_360_seconds(self):
        self.assertEqual(self.limit, 360)


class SecurityEventPurgeStaleTest(StaleTestMixin, unittest.TestCase):
    interval, first_delay = SECURITY_EVENT_PURGE

    def test_limit_is_48_hours_5_minutes(self):
        self.assertEqual(self.limit, 48 * 60 * 60 + 5 * 60)


class PeriodicStatusTest(unittest.TestCase):
    def test_ok_when_no_task_is_stale(self):
        self.assertEqual(periodic_status([False, False]), OK)
        self.assertEqual(periodic_status([]), OK)

    def test_stale_when_any_task_is_stale(self):
        self.assertEqual(periodic_status([False, True]), STALE)
        self.assertEqual(periodic_status([True, False]), STALE)
        self.assertEqual(periodic_status([True, True]), STALE)

    def test_values(self):
        self.assertEqual((OK, STALE), ("ok", "stale"))
