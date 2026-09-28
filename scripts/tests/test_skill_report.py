"""Скилл `report`: вопрос «баллы или только динамика?» при сборке личных и правило `--no-ask`.

Проверяется реальный `skills/report/SKILL.md`: раздел о виде личного отчёта есть, вопрос задаётся
один раз на весь запуск, ответы ведут к флагам `--scores` / `--trend`, при `--no-ask` вопроса
нет и вид берётся из `config.show_scores` и `feedback.show_scores` карточек; флаги в командах
скилла живые (есть в парсере `sr.py report`).
"""

from __future__ import annotations

import argparse
import re
import unittest

import _util  # noqa: F401 — кладёт scripts/ в sys.path
from _util import PLUGIN

from lib import registry_cmds

SKILL = PLUGIN / "skills" / "report" / "SKILL.md"
SECTION_RE = re.compile(r"^## Баллы или динамика\n(.*?)(?=^## )", re.M | re.S)
FLAG_RE = re.compile(r"(?<![\w-])--[a-z][a-z-]+")


def _skill() -> str:
    if not SKILL.is_file():
        raise AssertionError(f"нет файла ({SKILL})")
    return SKILL.read_text(encoding="utf-8")


def _section() -> str:
    found = SECTION_RE.search(_skill())
    if not found:
        raise AssertionError(f"в {SKILL} нет раздела «## Баллы или динамика»")
    return found.group(1)


def _report_flags() -> set[str]:
    """Флаги `sr.py report` и общие флаги `sr.py` (`--workspace`)."""
    parser = registry_cmds.build_parser()
    common = {opt for a in parser._actions for opt in a.option_strings}  # noqa: SLF001
    for action in parser._actions:  # noqa: SLF001 — argparse не даёт публичного доступа
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            report = action.choices["report"]
            return common | {opt for a in report._actions  # noqa: SLF001
                             for opt in a.option_strings}
    raise AssertionError("в парсере sr.py нет подкоманд")


class ReportSkillViewQuestionTest(unittest.TestCase):
    def test_question_asked_once_for_personal_reports(self):
        text = " ".join(_section().split())
        self.assertIn("«Баллы или только динамика?»", text)
        self.assertIn("один раз на весь запуск", text)
        self.assertIn("--personal", text)
        self.assertIn("--all-personal", text)

    def test_answers_map_to_flags(self):
        text = _section()
        self.assertRegex(text, r"(?s)Баллы.*?`--scores`")
        self.assertRegex(text, r"(?s)динамика.*?`--trend`")
        self.assertIn("↑", text)
        self.assertIn("◇", text)

    def test_no_ask_takes_config_and_cards_without_question(self):
        joined = " ".join(_section().split())
        self.assertIn("`--no-ask` не передан, до команды задай", joined,
                      "вопрос задаётся только без --no-ask")
        rule = re.search(r"При `--no-ask`.*?\.(?:\s|$)", joined)
        self.assertIsNotNone(rule, "нет правила для --no-ask")
        rule = rule.group(0)
        self.assertIn("не спрашивай", rule)
        self.assertIn("флаг вида не добавляй", rule)
        self.assertIn("`config.show_scores`", rule)
        self.assertIn("`feedback.show_scores`", rule)
        self.assertNotRegex(rule, r"`--(scores|trend)`", "при --no-ask флаг вида не подставляется")

    def test_priority_documented(self):
        joined = " ".join(_section().split())
        self.assertIn("флаг запуска > `feedback.show_scores` карточки > `config.show_scores`",
                      joined)

    def test_no_ask_is_listed_as_skill_argument(self):
        self.assertIn("`--no-ask`", _skill().split("## Команда", 1)[0])

    def test_flags_in_skill_exist_in_parser(self):
        known = _report_flags() | {"--no-ask", "--help"}
        used = set(FLAG_RE.findall(_skill()))
        self.assertTrue({"--scores", "--trend"} <= used)
        self.assertEqual(used - known, set())


if __name__ == "__main__":
    unittest.main()
