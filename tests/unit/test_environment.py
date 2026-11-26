import ast
import unittest
from pathlib import Path

from app.domain import environment
from app.domain.environment import (
    InstanceMismatch,
    admin_enabled_error,
    check_instance_marker,
    health_payload,
    parse_admin_enabled,
    parse_core_env,
)


class ModuleTest(unittest.TestCase):
    def test_module_has_no_io_imports(self):
        source = Path(environment.__file__).read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertEqual(imported, set())


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


class ParseAdminEnabledTest(unittest.TestCase):
    def test_accepts_true_and_false(self):
        self.assertIs(parse_admin_enabled("true"), True)
        self.assertIs(parse_admin_enabled("false"), False)

    def test_rejects_anything_else_instead_of_guessing(self):
        # 写错时启动失败：猜成 true 会在不该开的实例上开后台，猜成 false 会让运维以为开了。
        bad_values = ("TRUE", "True", "FALSE", "False", " true", "true ", " false ", "1", "0")
        for bad in (*bad_values, "yes", "no", "on", ""):
            with self.subTest(value=bad), self.assertRaises(ValueError) as caught:
                parse_admin_enabled(bad)
            self.assertIn("CORE_ADMIN_ENABLED", str(caught.exception))


class AdminEnabledErrorTest(unittest.TestCase):
    def test_demo_with_the_switch_on_is_refused(self):
        message = admin_enabled_error("demo", True)
        self.assertIsInstance(message, str)
        self.assertNotIn("\n", message)
        self.assertIn("CORE_ADMIN_ENABLED", message)
        self.assertIn("demo", message)
        for leak in ("mysql", "://", "@"):
            self.assertNotIn(leak, message)

    def test_other_combinations_start(self):
        for env, enabled in (("demo", False), ("production", False), ("production", True)):
            with self.subTest(env=env, enabled=enabled):
                self.assertIsNone(admin_enabled_error(env, enabled))


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
