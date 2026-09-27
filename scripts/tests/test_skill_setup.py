"""Скилл `setup`: 11 вопросов по порядку, варианты AskUserQuestion, живые команды, ответы проходят `setup-apply`.

Проверяются реальные файлы плагина: `skills/setup/SKILL.md`, `skills/setup/references/questions.md` и
строка-ссылка на протокол предложений в конце `collect`, `review`, `report`. JSON-примеры ответов из
обоих файлов прогоняются через `setup.validate_answers` — ту же проверку, что делает `setup-apply`.
Страницы пользователя `docs/ru/setup.md`, `customize.md`, `feedback.md`: есть, инструкция проверки
хука в `setup.md` дословно из i18n, JSON-примеры ответов проходят ту же проверку, команды — в парсере.
Запрещённые слова берутся из `test_skill_review` (собраны там эскейпами: гейт сканирует .py).
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import shlex
import unittest

import _util  # noqa: F401 — кладёт scripts/ в sys.path
from _util import PLUGIN
from test_manifest import frontmatter
from test_prompts_agents import bad_paths
from test_skill_review import FORBIDDEN, INTERNAL, LITERAL, command_lines, commands_in, parser_commands

from lib import registry_cmds, setup
from lib.i18n import t
from lib.workspace import SrError

SKILL_DIR = PLUGIN / "skills" / "setup"
SKILL = SKILL_DIR / "SKILL.md"
QUESTIONS = SKILL_DIR / "references" / "questions.md"
PROTOCOL = "${CLAUDE_PLUGIN_ROOT}/references/improvements-protocol.md"
NEIGHBOURS = ("collect", "review", "report")

QUESTION_RE = re.compile(r"^### Вопрос (\d+)\b", re.M)
OPTION_RE = re.compile(r"^\s*\d+\. ")
JSON_FENCE_RE = re.compile(r"^```json\n(.*?)^```", re.M | re.S)
HEREDOC_RE = re.compile(r"<<'EOF'\n(.*?)^EOF$", re.M | re.S)
USERS = "/" + "Users/"
TOTAL = 11
MAX_OPTIONS = 4
MIN_OPTIONS = 2
PRESETS = ("Сбалансировано", "Экономно", "Максимум", "Свой")
REPEAT_BLOCKS = ("люди и источники", "хук, cleanup, gitignore", "модели", "баллы, периоды, шаблон")
DOCS = PLUGIN / "docs" / "ru"
DOC_PAGES = ("setup", "customize", "feedback")
DOCTOR_KEYS = ("sources_found", "project_agents", "gitignore", "show_scores", "period_mode",
               "template", "improvements_pending", "cleanup_period_days")


def _read(path) -> str:
    if not path.is_file():
        raise AssertionError(f"нет файла ({path})")
    return path.read_text(encoding="utf-8")


def body(text: str) -> str:
    return text.split("\n---\n", 1)[1] if text.startswith("---\n") else text


def question_numbers(text: str) -> list[int]:
    return [int(n) for n in QUESTION_RE.findall(text)]


def question_sections(text: str) -> dict[int, str]:
    """Номер вопроса → текст до следующего заголовка `###`/`##`."""
    out = {}
    for part in re.split(r"^(?=##+ )", text, flags=re.M):
        m = QUESTION_RE.match(part)
        if m:
            out[int(m.group(1))] = part
    return out


def option_blocks(section: str) -> list[int]:
    """Длины подряд идущих нумерованных списков раздела: каждый — варианты одного AskUserQuestion."""
    sizes, run = [], 0
    for line in section.splitlines():
        if OPTION_RE.match(line):
            run += 1
        elif line.strip() and not line.startswith(" ") and run:
            sizes.append(run)
            run = 0
    if run:
        sizes.append(run)
    return sizes


def answer_examples(text: str) -> list[dict]:
    """JSON-объекты ответов: блоки ```json и тела heredoc `<<'EOF' … EOF`."""
    raw = JSON_FENCE_RE.findall(text) + HEREDOC_RE.findall(text)
    return [json.loads(block) for block in raw]


def call_argv(line: str) -> list[str]:
    """Аргументы вызова без префикса `python3 "…/sr.py"` и без heredoc."""
    rest = line[len(LITERAL):].split("<<", 1)[0]
    return shlex.split(rest)


class HelpersTest(unittest.TestCase):
    """Разборщики теста на синтетике."""

    TEXT = ("## Первый запуск\n\n### Вопрос 1. Режим\n\nтекст\n\n1. «A (Recommended)» — а\n2. «B» — б\n\n"
            "Повтор:\n\n1. «C» — в\n   продолжение\n2. «D»\n3. «E»\n\n### Вопрос 2. Люди\n\n1. x\n2. y\n"
            "3. z\n4. w\n5. q\n\n## Дальше\n\n1. не вариант\n")

    def test_question_numbers_and_sections(self) -> None:
        self.assertEqual(question_numbers(self.TEXT), [1, 2])
        sections = question_sections(self.TEXT)
        self.assertNotIn("не вариант", sections[2])

    def test_option_blocks_split_by_text(self) -> None:
        sections = question_sections(self.TEXT)
        self.assertEqual(option_blocks(sections[1]), [2, 3])
        self.assertEqual(option_blocks(sections[2]), [5])

    def test_answer_examples_from_fence_and_heredoc(self) -> None:
        text = ('```json\n{"mode": "self"}\n```\n\n```bash\n' + LITERAL
                + " setup-apply --answers - <<'EOF'\n{\"hook\": true}\nEOF\n```\n")
        self.assertEqual(answer_examples(text), [{"mode": "self"}, {"hook": True}])

    def test_call_argv_drops_heredoc(self) -> None:
        self.assertEqual(call_argv(LITERAL + " setup-apply --answers - <<'EOF'"), ["setup-apply", "--answers", "-"])

    def test_validate_answers_rejects_bad_example(self) -> None:
        with self.assertRaises(SrError):
            setup.validate_answers({"mode": "self", "cleanup_period_days": {"value": 90}})


class SkillTest(unittest.TestCase):
    """`skills/setup/SKILL.md`."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _read(SKILL)
        cls.body = body(cls.text)
        cls.known = parser_commands()

    def test_frontmatter_kept(self) -> None:
        self.assertTrue(self.text.startswith("---\nname: setup\n"))
        fm = frontmatter(SKILL)
        self.assertEqual(set(fm), {"name", "description"})
        self.assertIn("Use when", fm["description"])
        self.assertIn("Триггеры:", fm["description"])

    def test_not_a_stub(self) -> None:
        self.assertNotIn("Заглушка", self.body)
        self.assertGreater(len(self.body.splitlines()), 80)

    def test_eleven_questions_in_order(self) -> None:
        self.assertEqual(question_numbers(self.body), list(range(1, TOTAL + 1)))

    def test_every_command_exists_in_parser(self) -> None:
        used = set(commands_in(self.text))
        for cmd in ("setup-state", "setup-apply", "doctor"):
            with self.subTest(cmd=cmd):
                self.assertIn(cmd, used)
        self.assertEqual(sorted(used - self.known), [], "команды из SKILL.md, которых нет в sr.py")

    def test_command_lines_use_literal_and_parse(self) -> None:
        parser = registry_cmds.build_parser()
        lines = command_lines(self.text)
        self.assertGreaterEqual(len(lines), 3)
        for line in lines:
            with self.subTest(line=line):
                self.assertTrue(line.startswith(LITERAL + " "), "вызов не через литерал плагина")
                err = io.StringIO()
                try:
                    with contextlib.redirect_stderr(err):
                        parser.parse_args(call_argv(line))
                except SystemExit as exc:
                    self.fail(f"sr.py не принял вызов (код {exc.code}): {err.getvalue().strip()}")

    def test_step0_root_and_step1_state(self) -> None:
        self.assertIn("два уровня выше", self.body)
        self.assertIn('sr.py" setup-state --json', self.body)
        self.assertLess(self.body.index("setup-state --json"), self.body.index("### Вопрос 1"))

    def test_final_single_apply_then_doctor(self) -> None:
        self.assertIn("setup-apply --answers - <<'EOF'", self.body)
        self.assertEqual(self.body.count("setup-apply --answers - <<'EOF'"), 1)
        self.assertLess(self.body.index("setup-apply --answers -"), self.body.rindex('sr.py" doctor'))
        self.assertIn("дословно", self.body)

    def test_dialog_mechanics(self) -> None:
        for phrase in ("AskUserQuestion", "(Recommended)", "Other", "multiSelect", "зачем"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.body)

    def test_cleanup_lines_mention_write(self) -> None:
        # ключ ответа (не находка `found.cleanup_period_days`) всегда рядом с `write`
        lines = [ln for ln in self.body.splitlines() if re.search(r"(?<!found\.)cleanup_period_days", ln)]
        self.assertTrue(lines)
        for line in lines:
            with self.subTest(line=line):
                self.assertIn("write", line)

    def test_skip_conditions(self) -> None:
        sections = question_sections(self.body)
        self.assertIn("self", sections[2])
        self.assertIn("specstory_version", sections[4])
        self.assertIn("90", sections[10])
        self.assertIn("found.cleanup_period_days", sections[10])

    def test_repeat_run_four_blocks(self) -> None:
        for block in REPEAT_BLOCKS:
            with self.subTest(block=block):
                self.assertIn(block, self.body)

    def test_contract_and_proposals_sections(self) -> None:
        self.assertIn("## Что скилл вправе писать", self.body)
        self.assertIn("~/.claude/settings.json", self.body.split("## Что скилл вправе писать", 1)[1])
        self.assertIn("## Предложения движку", self.body)
        self.assertIn(PROTOCOL, self.body.split("## Предложения движку", 1)[1])

    def test_group_mode_wording(self) -> None:
        self.assertIn("учебная группа", self.body)

    def test_references_questions(self) -> None:
        self.assertIn("references/questions.md", self.body)

    def test_answer_examples_pass_validation(self) -> None:
        examples = answer_examples(self.body)
        self.assertTrue(examples, "в SKILL.md нет примера ответов")
        for example in examples:
            with self.subTest(example=example):
                setup.validate_answers(example)
        covered = set().union(*(set(e) for e in examples))
        self.assertEqual(covered, set(setup.ANSWER_KEYS), "пример ответов покрывает не все ключи")

    def test_plugin_paths_exist(self) -> None:
        self.assertEqual(bad_paths(PLUGIN, self.text), [])

    def test_clean(self) -> None:
        for lineno, line in enumerate(self.text.splitlines(), start=1):
            with self.subTest(line=lineno):
                self.assertIsNone(FORBIDDEN.search(line), line)
                self.assertIsNone(INTERNAL.search(line), line)
                self.assertNotIn(USERS, line)
                self.assertNotRegex(line, r"\{\{[^{}]*\}\}")


class QuestionsTest(unittest.TestCase):
    """`skills/setup/references/questions.md`."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _read(QUESTIONS)
        cls.sections = question_sections(cls.text)

    def test_eleven_questions_in_order(self) -> None:
        self.assertEqual(question_numbers(self.text), list(range(1, TOTAL + 1)))

    def test_each_question_has_why_and_options(self) -> None:
        for n in range(1, TOTAL + 1):
            with self.subTest(question=n):
                section = self.sections[n]
                self.assertIn("Зачем:", section)
                blocks = option_blocks(section)
                self.assertTrue(blocks, "нет вариантов ответа")
                for size in blocks:
                    self.assertGreaterEqual(size, MIN_OPTIONS)
                    self.assertLessEqual(size, MAX_OPTIONS)

    def test_first_option_recommended(self) -> None:
        for n in range(1, TOTAL + 1):
            with self.subTest(question=n):
                first = next(ln for ln in self.sections[n].splitlines() if OPTION_RE.match(ln))
                self.assertIn("(Recommended)", first)

    def test_model_presets(self) -> None:
        section = self.sections[6]
        for preset in PRESETS:
            with self.subTest(preset=preset):
                self.assertIn(preset, section)
        for word in ("цена", "качеств"):
            self.assertIn(word, section.lower())
        presets = [e for e in answer_examples(section) if "models" in e]
        self.assertGreaterEqual(len(presets), 3)

    def test_free_input_through_other(self) -> None:
        self.assertIn("Other", self.sections[2])
        self.assertIn("Other", self.sections[3])

    def test_cleanup_write_only_after_yes(self) -> None:
        section = self.sections[10]
        self.assertIn('"write": true', section)
        self.assertIn('"write": false', section)

    def test_specstory_install_refs(self) -> None:
        section = self.sections[4]
        self.assertIn("${CLAUDE_PLUGIN_ROOT}/references/collector-instructions/specstory-config.toml", section)
        self.assertIn("brew install specstory", section)

    def test_answer_examples_pass_validation(self) -> None:
        examples = answer_examples(self.text)
        self.assertGreaterEqual(len(examples), TOTAL - 1)
        for example in examples:
            with self.subTest(example=example):
                setup.validate_answers(example)

    def test_every_command_exists_in_parser(self) -> None:
        self.assertEqual(sorted(set(commands_in(self.text)) - parser_commands()), [])

    def test_plugin_paths_exist(self) -> None:
        self.assertEqual(bad_paths(PLUGIN, self.text), [])

    def test_clean(self) -> None:
        for lineno, line in enumerate(self.text.splitlines(), start=1):
            with self.subTest(line=lineno):
                self.assertIsNone(FORBIDDEN.search(line), line)
                self.assertIsNone(INTERNAL.search(line), line)
                self.assertNotIn(USERS, line)
                self.assertNotRegex(line, r"\{\{[^{}]*\}\}")


class UserDocsTest(unittest.TestCase):
    """Страницы пользователя `docs/ru/setup.md`, `customize.md`, `feedback.md`."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.pages = {name: _read(DOCS / f"{name}.md") for name in DOC_PAGES}

    def test_pages_exist_and_not_empty(self) -> None:
        for name, text in self.pages.items():
            with self.subTest(page=name):
                self.assertTrue(text.startswith("# "), "нет заголовка H1")
                self.assertGreater(len(text.splitlines()), 40)

    def test_setup_names_skill_and_hook_check_verbatim(self) -> None:
        text = self.pages["setup"]
        self.assertIn("/sessionretro:setup", text)
        self.assertIn(t(("cli", "setup"), "hook_check", "ru"), text)

    def test_setup_answer_examples_pass_validation(self) -> None:
        examples = answer_examples(self.pages["setup"])
        self.assertTrue(examples, "в setup.md нет примера ответов")
        for example in examples:
            with self.subTest(example=example):
                setup.validate_answers(example)

    def test_setup_explains_doctor_lines(self) -> None:
        for key in DOCTOR_KEYS:
            with self.subTest(key=key):
                self.assertIn(f"`{key}`", self.pages["setup"])

    def test_commands_exist_and_parse(self) -> None:
        known = parser_commands()
        parser = registry_cmds.build_parser()
        for name, text in self.pages.items():
            with self.subTest(page=name):
                self.assertEqual(sorted(set(commands_in(text)) - known), [])
            for line in command_lines(text):
                if not line.startswith(LITERAL + " "):
                    continue
                with self.subTest(page=name, line=line):
                    err = io.StringIO()
                    try:
                        with contextlib.redirect_stderr(err):
                            parser.parse_args(call_argv(line))
                    except SystemExit as exc:
                        self.fail(f"sr.py не принял вызов (код {exc.code}): {err.getvalue().strip()}")

    def test_feedback_page_covers_commands_and_templates(self) -> None:
        text = self.pages["feedback"]
        for action in ("list", "pack", "sent", "sync"):
            with self.subTest(action=action):
                self.assertIn(f'sr.py" improve {action}', text)
        self.assertIn("gh issue create", text)
        for tpl in ("proposal", "bug", "privacy"):
            with self.subTest(template=tpl):
                self.assertTrue((PLUGIN / ".github" / "ISSUE_TEMPLATE" / f"{tpl}.md").is_file())
                self.assertIn(f"`{tpl}`", text)

    def test_customize_covers_agents(self) -> None:
        text = self.pages["customize"]
        for phrase in ('sr.py" agents check', 'sr.py" agents write --force', "_default.yaml",
                       ".sessionretro/templates/feedback.md", ".claude/agents/sessionretro-"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_workspace_links_new_pages(self) -> None:
        text = _read(DOCS / "workspace.md")
        for name in DOC_PAGES:
            with self.subTest(page=name):
                self.assertIn(f"]({name}.md)", text)

    def test_clean(self) -> None:
        for name, text in self.pages.items():
            for lineno, line in enumerate(text.splitlines(), start=1):
                with self.subTest(page=name, line=lineno):
                    self.assertIsNone(FORBIDDEN.search(line), line)
                    self.assertIsNone(INTERNAL.search(line), line)
                    self.assertNotIn(USERS, line)


class NeighbourSkillsTest(unittest.TestCase):
    """Строка-ссылка на протокол предложений в конце `collect`, `review`, `report`."""

    def test_protocol_exists(self) -> None:
        self.assertTrue((PLUGIN / "references" / "improvements-protocol.md").is_file())

    def test_last_line_links_protocol(self) -> None:
        for name in NEIGHBOURS:
            with self.subTest(skill=name):
                lines = [ln for ln in _read(PLUGIN / "skills" / name / "SKILL.md").splitlines() if ln.strip()]
                self.assertIn(PROTOCOL, lines[-1])


if __name__ == "__main__":
    unittest.main()
