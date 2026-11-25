import ast
import unittest
from pathlib import Path

from app.domain import channels
from app.domain.channels import (
    API,
    BACKOFFICE,
    CHANNEL_KINDS,
    PLATFORM,
    combination_error,
    is_platform_shop_id,
)


class ModuleTest(unittest.TestCase):
    def test_module_has_no_io_imports(self):
        source = Path(channels.__file__).read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertTrue(imported <= {"collections", "re"}, imported)


class CombinationTest(unittest.TestCase):
    """开通之后仍能成为两种组合之一：恰好一个 api；或若干 platform 加一个 backoffice。"""

    def assert_allowed(self, existing: list[str], kind: str) -> None:
        with self.subTest(existing=existing, kind=kind):
            self.assertIsNone(combination_error(existing, kind))

    def assert_refused(self, existing: list[str], kind: str) -> None:
        with self.subTest(existing=existing, kind=kind):
            refusal = combination_error(existing, kind)
            self.assertIsInstance(refusal, str)
            self.assertTrue(refusal)

    def test_an_empty_pool_accepts_any_kind(self):
        for kind in CHANNEL_KINDS:
            self.assert_allowed([], kind)

    def test_a_pool_with_an_api_channel_accepts_nothing_more(self):
        for kind in CHANNEL_KINDS:
            self.assert_refused([API], kind)

    def test_api_cannot_join_a_pool_with_platform_or_backoffice_channels(self):
        for existing in ([PLATFORM], [BACKOFFICE], [PLATFORM, PLATFORM, BACKOFFICE]):
            self.assert_refused(existing, API)

    def test_a_pool_has_at_most_one_backoffice_channel(self):
        self.assert_refused([BACKOFFICE], BACKOFFICE)
        self.assert_refused([PLATFORM, BACKOFFICE], BACKOFFICE)
        self.assert_allowed([PLATFORM], BACKOFFICE)
        self.assert_allowed([PLATFORM, PLATFORM], BACKOFFICE)

    def test_platform_channels_may_be_many(self):
        self.assert_allowed([PLATFORM], PLATFORM)
        self.assert_allowed([BACKOFFICE], PLATFORM)
        self.assert_allowed([PLATFORM, PLATFORM, BACKOFFICE], PLATFORM)

    def test_existing_kinds_may_be_any_iterable(self):
        self.assertIsNone(combination_error(iter([PLATFORM, BACKOFFICE]), PLATFORM))
        self.assertIsNotNone(combination_error(iter([API]), PLATFORM))

    def test_refusals_are_fixed_sentences(self):
        reasons = {
            combination_error([API], PLATFORM),
            combination_error([PLATFORM], API),
            combination_error([BACKOFFICE], BACKOFFICE),
        }
        self.assertEqual(len(reasons), 3)
        for reason in reasons:
            self.assertTrue(reason.isascii())
            self.assertNotIn("\n", reason)


class PlatformShopIdTest(unittest.TestCase):
    def test_accepts_1_to_64_allowed_characters(self):
        for value in ("1", "a" * 64, "Shop.01_my:main-2", "1234567890", "AbC"):
            with self.subTest(value=value):
                self.assertTrue(is_platform_shop_id(value))

    def test_rejects_empty_too_long_and_other_characters(self):
        for value in (
            "",
            "a" * 65,
            "shop 1",
            "shop/1",
            " shop",
            "shop\n",
            "shop\t1",
            "shöp",
            "１２３",
            "shop@1",
        ):
            with self.subTest(value=value):
                self.assertFalse(is_platform_shop_id(value))

    def test_rejects_non_strings(self):
        for value in (None, 123, b"shop"):
            with self.subTest(value=value):
                self.assertFalse(is_platform_shop_id(value))
