"""`lib/self_test.py` и команда `self-test`: прогон тестов движка по списку, отсутствующий
файл тестов — «не найден» и код 1, итоговая строка «проверок N, упало K»."""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _util  # noqa: F401 — кладёт scripts/ в sys.path
from _util import TESTS

import sr
from lib import self_test

PASSING = """import unittest


class T(unittest.TestCase):
    def test_one(self):
        self.assertTrue(True)

    def test_two(self):
        self.assertEqual(1 + 1, 2)
"""

FAILING = """import unittest


class T(unittest.TestCase):
    def test_bad(self):
        self.assertEqual(1, 2)
"""


class RunTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="sr-selftest-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        # Имена файлов тестов уникальны на тест: sys.modules общий на процесс.
        self.ok = f"test_st_ok_{id(self)}"
        self.bad = f"test_st_bad_{id(self)}"
        (self.dir / f"{self.ok}.py").write_text(PASSING, encoding="utf-8")
        (self.dir / f"{self.bad}.py").write_text(FAILING, encoding="utf-8")
        self.addCleanup(lambda: [sys.modules.pop(m, None) for m in (self.ok, self.bad)])

    def test_one_existing_module_passes(self):
        code, text = self_test.run([self.ok], self.dir)
        self.assertEqual(code, 0, text)
        self.assertIn("проверок 2, упало 0", text)

    def test_missing_module_is_1(self):
        code, text = self_test.run([self.ok, "test_nowhere"], self.dir)
        self.assertEqual(code, 1)
        self.assertIn("не найден test_nowhere", text)
        self.assertIn("проверок 2, упало 0", text)

    def test_failing_module_is_1_and_named(self):
        code, text = self_test.run([self.ok, self.bad], self.dir)
        self.assertEqual(code, 1)
        self.assertIn("проверок 3, упало 1", text)
        self.assertIn("test_bad", text)

    def test_missing_tests_dir_is_1(self):
        code, text = self_test.run([self.ok], self.dir / "nope")
        self.assertEqual(code, 1)
        self.assertIn("не найден", text)

    def test_real_engine_module(self):
        code, text = self_test.run(["test_level"], TESTS)
        self.assertEqual(code, 0, text)
        self.assertRegex(text, r"проверок [1-9]\d*, упало 0")

    def test_engine_list(self):
        self.assertEqual(self_test.ENGINE_MODULES, [
            "test_tracker", "test_tracker_ops", "test_marks", "test_level", "test_batches",
            "test_collector_check", "test_render_review", "test_approve"])


class SelfTestCliTest(_util.TempHome, unittest.TestCase):
    """HOME временный: `sr.main` сдвигает глобальную отметку `plugin-state.json`."""

    def cli(self) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        before = sys.dont_write_bytecode
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = sr.main(["self-test"])
            self.bytecode_flag = sys.dont_write_bytecode
        finally:
            sys.dont_write_bytecode = before
        return code, out.getvalue(), err.getvalue()

    def test_cli_on_substituted_list(self):
        with mock.patch.object(self_test, "ENGINE_MODULES", ["test_level"]):
            code, out, _ = self.cli()
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"проверок [1-9]\d*, упало 0")
        self.assertTrue(self.bytecode_flag)

    def test_cli_missing_module_is_1(self):
        with mock.patch.object(self_test, "ENGINE_MODULES", ["test_level", "test_nowhere"]):
            code, out, _ = self.cli()
        self.assertEqual(code, 1)
        self.assertIn("не найден test_nowhere", out)


if __name__ == "__main__":
    unittest.main()
