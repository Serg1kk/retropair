"""`lib/i18n.py`: строки интерфейса из `references/i18n.json` по секции, ключу и языку."""

from __future__ import annotations

import unittest

import _util  # noqa: F401 — кладёт scripts/ в sys.path

from lib import i18n

AXES = [
    "verification", "task_framing", "ownership", "self_correction",
    "context_engineering", "decomposition", "trust_calibration", "reversibility",
]


class LoadTest(unittest.TestCase):
    def test_load_is_cached(self) -> None:
        self.assertIs(i18n.load(), i18n.load())

    def test_load_reads_plugin_file(self) -> None:
        data = i18n.load()
        self.assertIn("axes", data)
        self.assertIn("assess", data["cli"])


class TranslateTest(unittest.TestCase):
    def test_section_as_string(self) -> None:
        self.assertEqual(i18n.t("axes", "verification", "ru"), "Проверка результата")
        self.assertEqual(i18n.t("axes", "verification", "en"), "Verifying results")

    def test_section_as_one_element_tuple(self) -> None:
        self.assertEqual(i18n.t(("review_statuses",), "draft", "ru"), "Черновик")
        self.assertEqual(i18n.t(("review_statuses",), "approved", "en"), "Approved")

    def test_nested_section_tuple_missing_key_names_full_path(self) -> None:
        with self.assertRaises(KeyError) as ctx:
            i18n.t(("cli", "assess"), "no_such_key", "ru")
        self.assertIn("i18n: нет cli.assess.no_such_key", str(ctx.exception))

    def test_missing_section(self) -> None:
        with self.assertRaises(KeyError) as ctx:
            i18n.t("no_such_section", "x", "ru")
        self.assertIn("i18n: нет no_such_section", str(ctx.exception))

    def test_missing_nested_section(self) -> None:
        with self.assertRaises(KeyError) as ctx:
            i18n.t(("cli", "nothing"), "x", "ru")
        self.assertIn("i18n: нет cli.nothing", str(ctx.exception))

    def test_missing_key(self) -> None:
        with self.assertRaises(KeyError) as ctx:
            i18n.t("levels", "wizard", "en")
        self.assertIn("i18n: нет levels.wizard", str(ctx.exception))

    def test_missing_language(self) -> None:
        with self.assertRaises(KeyError) as ctx:
            i18n.t("axes", "verification", "de")
        self.assertIn("i18n: нет axes.verification.de", str(ctx.exception))

    def test_service_key_is_not_a_string_entry(self) -> None:
        with self.assertRaises(KeyError) as ctx:
            i18n.t("axes", "_owner", "ru")
        self.assertIn("i18n: нет axes._owner", str(ctx.exception))

    def test_key_error_is_not_empty_string(self) -> None:
        # Отсутствие перевода — ошибка, а не пустая строка в тексте ревью.
        with self.assertRaises(KeyError):
            i18n.t("verdicts", "even", "")


class KeysTest(unittest.TestCase):
    def test_axes_in_file_order(self) -> None:
        self.assertEqual(i18n.keys("axes"), AXES)

    def test_keys_skip_service_entries(self) -> None:
        keys = i18n.keys("levels")
        self.assertNotIn("_owner", keys)
        self.assertEqual(len(keys), 7)
        self.assertIn("insufficient", keys)

    def test_keys_of_nested_section(self) -> None:
        self.assertNotIn("_owner", i18n.keys(("cli", "assess")))
        self.assertEqual(i18n.keys(("review_statuses",)), ["draft", "approved"])

    def test_keys_of_top_level_skip_readme(self) -> None:
        self.assertNotIn("_readme", i18n.keys(()))

    def test_keys_missing_section(self) -> None:
        with self.assertRaises(KeyError) as ctx:
            i18n.keys("nope")
        self.assertIn("i18n: нет nope", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
