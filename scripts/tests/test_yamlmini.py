"""Тесты мини-парсера YAML `lib/yamlmini.py`."""

from __future__ import annotations

import unittest

import _util  # noqa: F401  кладёт plugin/scripts в sys.path

from lib import yamlmini
from lib.yamlmini import YamlError


class YamlMiniTest(unittest.TestCase):
    def test_nested_maps(self):
        text = "id: me\nprofile:\n  role: dev\n  years: 9\n  inner:\n    flag: true\n"
        self.assertEqual(
            yamlmini.load(text),
            {"id": "me", "profile": {"role": "dev", "years": 9, "inner": {"flag": True}}},
        )

    def test_list_of_scalars(self):
        text = "skip_basics:\n  - git\n  - tests\ngoals:\n- a\n- b\n"
        self.assertEqual(
            yamlmini.load(text),
            {"skip_basics": ["git", "tests"], "goals": ["a", "b"]},
        )

    def test_list_of_maps(self):
        text = (
            "sources:\n"
            "  - type: git\n"
            "    url: https://example.org/repo.git\n"
            "    branch: main\n"
            "  - type: dir\n"
            "    path: /home/user/logs\n"
        )
        self.assertEqual(
            yamlmini.load(text),
            {
                "sources": [
                    {"type": "git", "url": "https://example.org/repo.git", "branch": "main"},
                    {"type": "dir", "path": "/home/user/logs"},
                ]
            },
        )

    def test_inline_comments(self):
        text = "# шапка\naddress: ты   # ты | вы | you\nprefix: \"a # b\"  # не комментарий внутри\n"
        self.assertEqual(yamlmini.load(text), {"address": "ты", "prefix": "a # b"})

    def test_key_without_value_is_none(self):
        self.assertEqual(yamlmini.load("sources:\nname: x\n"), {"sources": None, "name": "x"})

    def test_block_scalar_rejected(self):
        with self.assertRaises(YamlError) as ctx:
            yamlmini.load("goals: |\n  line\n")
        self.assertIn("строка 1", str(ctx.exception))

    def test_tabs_rejected(self):
        with self.assertRaises(YamlError):
            yamlmini.load("feedback:\n\taddress: ты\n")

    def test_date_stays_string(self):
        data = yamlmini.load("consent:\n  logs: true\n  date: 2026-09-27\n")
        self.assertEqual(data["consent"]["date"], "2026-09-27")
        self.assertIsInstance(data["consent"]["date"], str)

    def test_empty_quoted_and_null(self):
        self.assertEqual(
            yamlmini.load('role: ""\nyears: null\nstack: ~\n'),
            {"role": "", "years": None, "stack": None},
        )


if __name__ == "__main__":
    unittest.main()
