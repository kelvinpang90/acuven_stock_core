import unittest

from app.domain.environment import health_payload, parse_core_env


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
