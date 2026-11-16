import unittest

from app.domain.environment import (
    InstanceMismatch,
    check_instance_marker,
    health_payload,
    parse_core_env,
)


class ParseCoreEnvTest(unittest.TestCase):
    def test_accepts_the_two_known_environments(self):
        self.assertEqual(parse_core_env("demo"), "demo")
        self.assertEqual(parse_core_env("production"), "production")

    def test_rejects_anything_else_instead_of_guessing(self):
        # 写错环境时必须启动失败：猜成 demo 会让生产实例开放 Demo 重置，
        # 猜成 production 会让 Demo 网店连错实例。
        for bad in ("", "Demo", "prod", " demo", "staging"):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                parse_core_env(bad)


class CheckInstanceMarkerTest(unittest.TestCase):
    def test_same_environment_passes(self):
        for env in ("demo", "production"):
            with self.subTest(env=env):
                self.assertIsNone(check_instance_marker(env, env))

    def test_different_environment_is_a_mismatch(self):
        # 契约 6.1：Demo 配置的进程不能迁移或连上生产库，反过来也一样。
        for marker, core_env in (("production", "demo"), ("demo", "production")):
            with self.subTest(marker=marker), self.assertRaises(InstanceMismatch) as caught:
                check_instance_marker(marker, core_env)
            self.assertMessageNamesOnly(str(caught.exception), {marker, core_env})

    def test_missing_row_is_a_mismatch(self):
        # 有表无行说明标记迁移没有走完，不能当作哪一种实例。
        with self.assertRaises(InstanceMismatch) as caught:
            check_instance_marker(None, "demo")
        self.assertMessageNamesOnly(str(caught.exception), {"demo"})

    def test_mismatch_is_a_value_error(self):
        self.assertTrue(issubclass(InstanceMismatch, ValueError))

    def assertMessageNamesOnly(self, message: str, names: set[str]) -> None:
        # 一行；只出现给定的环境名，没有连接串的痕迹。
        self.assertNotIn("\n", message)
        for name in names:
            self.assertIn(name, message)
        for other in {"demo", "production"} - names:
            self.assertNotIn(other, message)
        for leak in ("mysql", "://", "@"):
            self.assertNotIn(leak, message)


class HealthPayloadTest(unittest.TestCase):
    def test_reports_commit_and_environment(self):
        # 部署工作流靠 commit 判定新版本已上线（D3），网店靠 env 核对没有连错实例（契约 §9.2）。
        self.assertEqual(
            health_payload("a" * 40, "demo"),
            {"status": "ok", "commit": "a" * 40, "env": "demo"},
        )

    def test_missing_commit_is_reported_as_unknown_not_empty(self):
        self.assertEqual(health_payload("", "production")["commit"], "unknown")


if __name__ == "__main__":
    unittest.main()
