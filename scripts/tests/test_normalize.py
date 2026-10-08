"""Нормализатор логов: шесть форматов, стабильные индексы ходов, метрики, `sr.py normalize`."""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

import _util  # noqa: F401 — кладёт scripts/ в sys.path
from _util import FIXTURES, SCRIPTS, run_sr

from lib import normalize

LOGS = FIXTURES / "logs"

# фикстура → (формат, хвост в том же формате для проверки стабильности индексов)
FIXTURE_FORMATS = {
    "specstory-claude.md": ("specstory",
                            "\n---\n\n_**User (2026-09-01 10:10:00Z)**_\n\nещё один вопрос\n"),
    "specstory-codex-exec.md": ("specstory",
                                "\n---\n\n_**User (2026-09-02 09:10:00Z)**_\n\nещё\n"),
    "specstory-ru-roles.md": ("specstory",
                              "\n---\n\n_**Человек (2026-09-06 08:10:00Z)**_\n\nещё\n"),
    "cc-scrollback.txt": ("cc-scrollback", "\n❯ ещё один вопрос\n\n● Ответ.\n"),
    "md-headings.md": ("md-headings", "\n## User\n\nещё один вопрос\n\n## Assistant\n\nОтвет.\n"),
    "md-bold-roles.md": ("md-bold-roles", "\n**Dev:**\n\n````\none more\n````\n"),
    "cursor-export.md": ("cursor-export", "\n---\n\n**User**\n\nещё один вопрос\n"),
    "jsonl-export.md": ("jsonl-export",
                        "\n## user — 2026-09-12T10:30:00.000Z\n\n```text\nещё\n```\n"),
}

METRIC_KEYS = {
    "timestamps_available", "tool_calls_available", "commands_available", "turns_total",
    "user_turns", "synthetic_turns", "sidechain_turns", "tool_calls", "shell_tool_calls",
    "write_tool_calls", "verification_commands", "verification_by_kind",
    "verification_examples", "approvals", "instant_approvals", "speed_accept_count",
    "interrupts", "repeated_prompts", "session_span_seconds",
}
DOC_KEYS = {
    "format", "session_id", "started_at", "ended_at", "turns_total", "user_turns",
    "synthetic_turns", "assistant_turns", "sidechain_turns", "models", "bytes", "collector",
    "started_at_source", "turns", "mechanical_metrics",
}

CODEX_HEADER = ("<!-- Codex CLI Session 44444444-5555-6666-7777-888888888888 "
                "(2026-09-02 09:00:00Z) -->\n\n"
                "_**User (2026-09-02 09:00:00Z)**_\n\nпосмотри проект\n\n---\n\n")
TOOL_BLOCK_WITHOUT_CMD = (
    "_**Agent (gpt-5-codex 2026-09-02 09:00:10Z)**_\n\n"
    '<tool-use data-tool-type="unknown" data-tool-name="exec"><details>\n'
    "<summary>Tool use: **exec**</summary>\n\n"
    "```text\nвызов без видимой команды\n```\n"
    "</details></tool-use>\n\n---\n\n"
)


def read(name: str) -> str:
    return (LOGS / name).read_text(encoding="utf-8")


class SecondsBetween(unittest.TestCase):
    def test_fractional_z(self):
        self.assertEqual(normalize.seconds_between("2026-09-12T09:53:26.399Z",
                                                   "2026-09-12T09:53:36.399Z"), 10.0)

    def test_offset(self):
        self.assertEqual(normalize.seconds_between("2026-09-12T12:00:00+03:00",
                                                   "2026-09-12T09:00:10Z"), 10.0)

    def test_garbage_none(self):
        self.assertIsNone(normalize.seconds_between("вчера", "2026-09-12T09:00:10Z"))

    def test_missing_none(self):
        self.assertIsNone(normalize.seconds_between(None, "2026-09-12T09:00:10Z"))

    def test_one_digit_fraction(self):
        self.assertEqual(normalize.seconds_between("2026-09-12T09:00:00.1Z",
                                                   "2026-09-12T09:00:01.1Z"), 1.0)

    def test_seven_digit_fraction_truncated(self):
        self.assertAlmostEqual(normalize.seconds_between("2026-09-12T09:00:00.1234567Z",
                                                         "2026-09-12T09:00:02.1234567Z"), 2.0)

    def test_space_separator_and_naive_is_utc(self):
        self.assertEqual(normalize.seconds_between("2026-09-01 10:00:00Z",
                                                   "2026-09-01 10:00:08"), 8.0)


class CodexExec(unittest.TestCase):
    def test_exec_command_counted(self):
        doc = normalize.parse_file(LOGS / "specstory-codex-exec.md")
        cmds = [t["command"] for turn in doc["turns"] for t in turn["tools"] if t.get("command")]
        self.assertIn("pytest -q", cmds)
        m = doc["mechanical_metrics"]
        self.assertEqual(m["shell_tool_calls"], 3)
        self.assertEqual(m["write_tool_calls"], 1)   # apply_patch через exec
        self.assertEqual(m["verification_commands"], 2)
        self.assertTrue(m["commands_available"])

    def test_apply_patch_command_unescaped(self):
        doc = normalize.parse_file(LOGS / "specstory-codex-exec.md")
        cmd = doc["turns"][3]["tools"][0]["command"]
        self.assertTrue(cmd.startswith("apply_patch <<'PATCH'\n*** Begin Patch"), cmd)

    def test_json_cmd_form(self):
        text = CODEX_HEADER + TOOL_BLOCK_WITHOUT_CMD.replace(
            "вызов без видимой команды", '{"cmd": "npm test"}')
        tool = normalize.parse(text)["turns"][1]["tools"][0]
        self.assertEqual(tool["command"], "npm test")

    def test_invisible_commands_none(self):
        # вызовы есть, команд не видно → None, а не 0
        text = CODEX_HEADER + TOOL_BLOCK_WITHOUT_CMD * 2
        m = normalize.parse(text)["mechanical_metrics"]
        self.assertEqual(m["tool_calls"], 2)
        self.assertEqual(m["shell_tool_calls"], 2)
        self.assertIsNone(m["verification_commands"])
        self.assertIsNone(m["verification_by_kind"])
        self.assertFalse(m["commands_available"])

    def test_invisible_commands_hide_name_based_verification(self):
        # MCP-вызов опознаётся по имени, но шелл-команды скрыты: число было бы заниженным
        turns = [{"index": 0, "role": "assistant", "text": "", "ts": None, "tools": [
            {"name": "exec", "type": "unknown", "command": None},
            {"name": "mcp__demo-srv__get_run", "type": "mcp", "command": None},
        ]}]
        m = normalize.metrics(turns)
        self.assertFalse(m["commands_available"])
        self.assertIsNone(m["verification_commands"])
        self.assertEqual(len(m["verification_examples"]), 1)

    def test_unknown_non_exec_is_not_shell(self):
        turns = [{"index": 0, "role": "assistant", "text": "", "ts": None, "tools": [
            {"name": "view_image", "type": "unknown", "command": None}]}]
        m = normalize.metrics(turns)
        self.assertEqual(m["shell_tool_calls"], 0)
        self.assertTrue(m["commands_available"])


class JsonlExportTimestamps(unittest.TestCase):
    def test_span_and_instant(self):
        m = normalize.parse_file(LOGS / "jsonl-export.md")["mechanical_metrics"]
        self.assertIsNotNone(m["session_span_seconds"])
        self.assertEqual(m["instant_approvals"], 1)
        self.assertEqual(m["speed_accept_count"], 1)


class EveryFixture(unittest.TestCase):
    """Восемь фикстур, шесть форматов: общий контракт документа."""

    def test_fixture_set_complete(self):
        # нативный JSONL Claude Code (`*.jsonl`) проверяет test_cc_jsonl.py
        on_disk = {p.name for p in LOGS.iterdir() if p.is_file() and p.suffix != ".jsonl"}
        self.assertEqual(on_disk, set(FIXTURE_FORMATS))
        self.assertEqual({f for f, _ in FIXTURE_FORMATS.values()},
                         {"specstory", "cc-scrollback", "cursor-export", "md-headings",
                          "md-bold-roles", "jsonl-export"})

    def test_detect_and_format(self):
        for name, (fmt, _) in FIXTURE_FORMATS.items():
            with self.subTest(fixture=name):
                self.assertEqual(normalize.detect_format(read(name)), fmt)
                self.assertEqual(normalize.parse_file(LOGS / name)["format"], fmt)

    def test_document_keys_and_path(self):
        for name in FIXTURE_FORMATS:
            with self.subTest(fixture=name):
                doc = normalize.parse_file(LOGS / name)
                self.assertTrue(DOC_KEYS.issubset(doc), DOC_KEYS - set(doc))
                self.assertEqual(doc["path"], str(LOGS / name))

    def test_turns_and_human_turns(self):
        for name in FIXTURE_FORMATS:
            with self.subTest(fixture=name):
                doc = normalize.parse_file(LOGS / name)
                self.assertGreaterEqual(doc["turns_total"], 4)
                self.assertGreaterEqual(doc["user_turns"], 4)

    def test_index_is_dense(self):
        for name in FIXTURE_FORMATS:
            with self.subTest(fixture=name):
                turns = normalize.parse_file(LOGS / name)["turns"]
                self.assertEqual([t["index"] for t in turns], list(range(len(turns))))

    def test_index_stable_when_log_grows(self):
        for name, (_, tail) in FIXTURE_FORMATS.items():
            with self.subTest(fixture=name):
                text = read(name)
                before = normalize.parse(text)["turns"]
                after = normalize.parse(text + tail)["turns"]
                self.assertGreater(len(after), len(before))
                self.assertEqual([(t["index"], t["text"]) for t in before],
                                 [(t["index"], t["text"]) for t in after[:len(before)]])

    def test_session_id_does_not_depend_on_file_name(self):
        for name in FIXTURE_FORMATS:
            with self.subTest(fixture=name):
                text = read(name)
                a = normalize.parse(text, path=Path("/home/user/logs") / name)
                b = normalize.parse(text, path=Path("/home/user/other/renamed-log.md"))
                self.assertTrue(a["session_id"])
                self.assertEqual(a["session_id"], b["session_id"])

    def test_all_metric_keys_present(self):
        for name in FIXTURE_FORMATS:
            with self.subTest(fixture=name):
                m = normalize.parse_file(LOGS / name)["mechanical_metrics"]
                self.assertEqual(set(m), METRIC_KEYS | set(normalize.EXTRA_METRIC_KEYS))
                for key in normalize.EXTRA_METRIC_KEYS:
                    self.assertIsNone(m[key], key)

    def test_specstory_header_session(self):
        doc = normalize.parse_file(LOGS / "specstory-claude.md")
        self.assertEqual(doc["session_id"], "11111111-2222-3333-4444-555555555555")
        self.assertEqual(doc["collector"], "Claude Code")
        self.assertEqual(doc["started_at"], "2026-09-01 10:00:00Z")
        m = doc["mechanical_metrics"]
        self.assertEqual(m["verification_commands"], 2)   # pytest и ruff
        self.assertEqual(m["write_tool_calls"], 2)
        self.assertEqual(m["instant_approvals"], 1)
        self.assertEqual(m["speed_accept_count"], 1)
        self.assertEqual(m["session_span_seconds"], 245.0)

    def test_scrollback_collapsed_hides_commands(self):
        doc = normalize.parse_file(LOGS / "cc-scrollback.txt")
        self.assertEqual(doc["collector"], "Claude Code 2.1.0")
        self.assertEqual(doc["models"], ["Opus 4.5"])
        self.assertEqual(doc["synthetic_turns"], 1)       # /clear
        m = doc["mechanical_metrics"]
        self.assertFalse(m["commands_available"])
        self.assertIsNone(m["verification_commands"])
        self.assertFalse(m["timestamps_available"])
        self.assertIsNone(m["instant_approvals"])


class Formats(unittest.TestCase):
    def test_formats_order_and_shape(self):
        names = [name for name, _, _ in normalize.FORMATS]
        self.assertEqual(names[:2], ["claude-jsonl", "jsonl-export"])
        self.assertEqual(names[-1], "specstory")
        self.assertEqual(set(names), {"claude-jsonl", "specstory", "cc-scrollback", "cursor-export",
                                      "md-headings", "md-bold-roles", "jsonl-export"})
        for name, detector, parser in normalize.FORMATS:
            with self.subTest(fmt=name):
                self.assertTrue(callable(detector) and callable(parser))

    def test_registered_format_wins_when_first(self):
        saved = list(normalize.FORMATS)
        try:
            def parser(text, keep_tool_output=True, max_turn_chars=0, path=None):
                return {"format": "probe", "turns_total": 0, "turns": [],
                        "mechanical_metrics": {}}
            normalize.FORMATS.insert(0, ("probe", lambda t: t.startswith("PROBE"), parser))
            self.assertEqual(normalize.detect_format("PROBE\n"), "probe")
            doc = normalize.parse("PROBE\n")
            self.assertEqual(doc["format"], "probe")
            self.assertEqual(set(doc["mechanical_metrics"]), set(normalize.EXTRA_METRIC_KEYS))
        finally:
            normalize.FORMATS[:] = saved

    def test_plain_text_falls_back_to_specstory_without_turns(self):
        self.assertEqual(normalize.detect_format("просто заметка\n"), "specstory")
        self.assertEqual(normalize.parse("просто заметка\n")["turns_total"], 0)

    def test_metrics_has_nineteen_keys(self):
        self.assertEqual(set(normalize.metrics([])), METRIC_KEYS)

    def test_tool_denials_is_extra_metric(self):
        self.assertIn("tool_denials", normalize.EXTRA_METRIC_KEYS)


class ContentKeys(unittest.TestCase):
    """Форматы без идентификатора в файле: ключ сеется первой репликой, первыми тремя ходами
    и временем старта из содержимого. Разные сессии с одинаковым началом разводятся, дописанный
    хвост ключ не меняет."""

    # фикстура → (префикс ключа, замена во 2–3 ходе)
    CASES = {
        "md-headings.md": ("mdex-", ("Прогони тесты.", "Прогони линтер.")),
        "md-bold-roles.md": ("mdbold-", ("go ahead", "wait, not yet")),
        "cc-scrollback.txt": ("ccsb-", ("❯ ок\n", "❯ нет, иначе\n")),
        "cursor-export.md": ("curex-", ("Покажи, как её вызвать.", "Объясни алгоритм.")),
    }

    def test_second_and_third_turns_split_keys(self):
        for name, (prefix, (old, new)) in self.CASES.items():
            with self.subTest(fixture=name):
                text = read(name)
                self.assertEqual(text.count(old), 1)
                a = normalize.parse(text)
                b = normalize.parse(text.replace(old, new))
                self.assertTrue(a["session_id"].startswith(prefix), a["session_id"])
                # первая реплика та же, а сессии разные
                first = [t["text"] for t in a["turns"] if t["role"] == "user"][0]
                self.assertEqual(first, [t["text"] for t in b["turns"] if t["role"] == "user"][0])
                self.assertNotEqual(a["session_id"], b["session_id"])

    def test_tail_keeps_key(self):
        for name in self.CASES:
            with self.subTest(fixture=name):
                text = read(name)
                tail = FIXTURE_FORMATS[name][1]
                self.assertEqual(normalize.parse(text)["session_id"],
                                 normalize.parse(text + tail + tail)["session_id"])

    def test_header_date_splits_key_file_date_does_not(self):
        text = read("md-headings.md")
        self.assertNotEqual(normalize.parse(text)["session_id"],
                            normalize.parse(text.replace("2026-09-03", "2026-09-04"))["session_id"])
        no_date = text.replace("**Date:** 2026-09-03\n", "")
        self.assertEqual(normalize.parse(no_date, path=Path("log-2026-09-03.md"))["session_id"],
                         normalize.parse(no_date, path=Path("log-2026-09-09.md"))["session_id"])

    def test_export_moment_does_not_split_key(self):
        # время выгрузки у Cursor и скроллбека — не старт сессии: повторная выгрузка — тот же ключ
        text = read("cursor-export.md")
        self.assertEqual(normalize.parse(text)["session_id"],
                         normalize.parse(text.replace("9/5/2026 at 14:03:22",
                                                      "9/7/2026 at 09:00:00"))["session_id"])
        sb = read("cc-scrollback.txt")
        self.assertEqual(normalize.parse(sb, path=Path("2026-09-05-140322.txt"))["session_id"],
                         normalize.parse(sb, path=Path("2026-09-07-090000.txt"))["session_id"])


class HeadingExportParser(unittest.TestCase):
    """Ручной экспорт заголовками `## User` / `## Assistant`."""

    LOG = """# Session Log

**Date:** 2026-08-19

**Summary:** something

## User

Проверь код и не выдумывай номера строк.

## Thinking

<details><summary>Thinking</summary>
Надо сначала прочитать файлы.
</details>

## Assistant

Начинаю разбор.

## User

Прогони тесты.

## Assistant

Готово.
"""

    def test_parses_turns(self):
        doc = normalize.parse(self.LOG)
        self.assertEqual(doc["format"], "md-headings")
        self.assertEqual(doc["user_turns"], 2)
        # размышления приклеены к ответу, а не посчитаны отдельным ходом
        self.assertEqual(doc["assistant_turns"], 2)
        self.assertEqual(doc["turns_total"], 4)
        self.assertIn("Надо сначала прочитать файлы", doc["turns"][1]["text"])
        self.assertIn("Начинаю разбор", doc["turns"][1]["text"])

    def test_started_at_from_header(self):
        doc = normalize.parse(self.LOG)
        self.assertEqual(doc["started_at"], "2026-08-19")
        self.assertEqual(doc["started_at_source"], "header_date")

    def test_started_at_from_filename(self):
        text = self.LOG.replace("**Date:** 2026-08-19\n", "")
        doc = normalize.parse(text, path=Path("ai-logs/session-04-2026-08-17.md"))
        self.assertEqual(doc["started_at"], "2026-08-17")
        self.assertEqual(doc["started_at_source"], "filename")

    def test_session_id_survives_rename(self):
        a = normalize.parse(self.LOG, path=Path("ai-logs/session-01-2026-08-19.md"))
        b = normalize.parse(self.LOG, path=Path("ai-logs/session-09-2026-08-19.md"))
        self.assertEqual(a["session_id"], b["session_id"])
        self.assertTrue(a["session_id"].startswith("mdex-"))

    def test_tools_absent_not_zero_verification(self):
        m = normalize.parse(self.LOG)["mechanical_metrics"]
        self.assertFalse(m["commands_available"])
        self.assertIsNone(m["verification_commands"])

    def test_does_not_hijack_specstory(self):
        spec = ("<!-- Claude Code Session abc12345 (2026-08-01 10:00:00Z) -->\n\n"
                "_**User**_\n\nпривет\n\n---\n\n_**Assistant**_\n\nответ\n")
        self.assertEqual(normalize.parse(spec)["format"], "specstory")

    def test_plain_markdown_is_not_a_log(self):
        self.assertFalse(normalize.looks_like_heading_export(
            "# Заголовок\n\n## Установка\n\nтекст\n"))


class BoldRoleExport(unittest.TestCase):
    """Экспорт с ролями жирным (`**Dev:**` / `**Claude:**`)."""

    LOG = """# IDE session — implementation

> Generated from the live session transcript.
> Built 2026-08-20 19:24 UTC.

---

**Dev:**

````
gather context first - then STOP before any real actions
````

**Claude:**

Reading the plan and verifying local state now.

> `Read(file_path=/home/user/demo/scope.md)`

> `Bash(command=pytest -q tests/, description=Run the suite)`

**Money:**

- one-off label inside the answer, not a role

**Dev:**

````
go ahead
````

**Claude:**

Done.

> `Edit(replace_all=false, file_path=/home/user/demo/a.py)`
"""

    def test_parses_turns_and_roles(self):
        doc = normalize.parse(self.LOG)
        self.assertEqual(doc["format"], "md-bold-roles")
        self.assertEqual(doc["user_turns"], 2)
        self.assertEqual(doc["assistant_turns"], 2)
        self.assertEqual(doc["turns_total"], 4)          # `**Money:**` ходом не считается
        self.assertEqual(doc["turns"][0]["text"],
                         "gather context first - then STOP before any real actions")
        self.assertIn("one-off label", doc["turns"][1]["text"])

    def test_tools_and_commands_are_visible(self):
        doc = normalize.parse(self.LOG)
        tools = doc["turns"][1]["tools"]
        self.assertEqual([t["name"] for t in tools], ["Read", "Bash"])
        self.assertEqual(tools[1]["type"], "shell")
        self.assertEqual(tools[1]["command"], "pytest -q tests/")
        m = doc["mechanical_metrics"]
        self.assertTrue(m["commands_available"])
        self.assertEqual(m["verification_commands"], 1)
        self.assertEqual(m["write_tool_calls"], 1)      # Edit в последнем ответе

    def test_started_at_from_built_header(self):
        doc = normalize.parse(self.LOG)
        self.assertEqual(doc["started_at"], "2026-08-20")
        self.assertEqual(doc["started_at_source"], "header_date")

    def test_session_id_survives_rename(self):
        a = normalize.parse(self.LOG, path=Path("ide-session-a.md"))
        b = normalize.parse(self.LOG, path=Path("ide-session-a-final.md"))
        self.assertEqual(a["session_id"], b["session_id"])
        self.assertTrue(a["session_id"].startswith("mdbold-"))

    def test_does_not_hijack_specstory_or_headings(self):
        spec = ("<!-- Claude Code Session abc123 (2026-08-01 10:00:00Z) -->\n\n"
                "_**User**_\n\nпривет\n\n---\n\n_**Assistant**_\n\nответ\n")
        self.assertEqual(normalize.parse(spec)["format"], "specstory")
        heading = ("**Date:** 2026-08-19\n\n## User\n\nа\n\n## Assistant\n\nб\n\n"
                   "## User\n\nв\n\n## Assistant\n\nг\n")
        self.assertEqual(normalize.parse(heading)["format"], "md-headings")

    def test_single_bold_labels_are_not_a_log(self):
        self.assertFalse(normalize.looks_like_bold_export(
            "**Claude:**\n\nодин раз\n\n**Money:**\n\n- x\n"))


class RussianRoleExport(unittest.TestCase):
    """Экспорт на каркасе SpecStory с ролями `Человек` / `Агент` и блоками `Инструмент:`."""

    LOG = (
        "<!-- Собрано скриптом экспорта из транскрипта Claude Code -->\n\n"
        "# Сессия 2026-09-12T09:53:26.399Z\n\n"
        "<!-- Claude Code Session 99999999-aaaa-bbbb-cccc-dddddddddddd -->\n\n"
        "_**Человек (2026-09-12 09:53:26Z)**_\n\nсделай ревью свежего PR\n\n---\n\n"
        "_**Агент (claude-opus-5 2026-09-12 09:53:33Z)**_\n\n"
        "<details><summary>Инструмент: <b>Bash</b></summary>\n\nRun tests\n\n"
        "```\npytest -q\n```\n</details>\n\n---\n\n"
        "<details><summary>Результат: Bash</summary>\n\n```\n3 passed\n```\n</details>\n\n---\n\n"
        "_**Агент (claude-opus-5 2026-09-12 09:53:40Z)**_\n\n"
        "<details><summary>Инструмент: <b>Edit</b></summary>\n\n```\n{\"file_path\": \"a.py\"}\n```\n"
        "</details>\n\nПоправил a.py.\n\n---\n\n"
        "_**Человек (2026-09-12 09:54:00Z)**_\n\nок, дальше\n"
    )

    def setUp(self):
        self.doc = normalize.parse(self.LOG)
        self.turns = self.doc["turns"]

    def test_roles_mapped(self):
        self.assertEqual([t["role"] for t in self.turns],
                         ["user", "assistant", "assistant", "user"])
        self.assertEqual(self.doc["user_turns"], 2)

    def test_header_and_session(self):
        self.assertEqual(self.doc["session_id"], "99999999-aaaa-bbbb-cccc-dddddddddddd")
        self.assertEqual(self.doc["started_at"], "2026-09-12T09:53:26.399Z")

    def test_shell_call_with_command_and_output(self):
        tool = self.turns[1]["tools"][0]
        self.assertEqual((tool["name"], tool["type"], tool["command"]), ("Bash", "shell", "pytest -q"))
        self.assertEqual(tool["output_head"], "3 passed")
        self.assertEqual(self.doc["mechanical_metrics"]["verification_commands"], 1)

    def test_write_call_typed_without_command(self):
        tool = self.turns[2]["tools"][0]
        self.assertEqual((tool["name"], tool["type"], tool["command"]), ("Edit", "write", None))
        self.assertEqual(self.doc["mechanical_metrics"]["write_tool_calls"], 1)

    def test_blocks_stripped_from_prose(self):
        self.assertNotIn("Инструмент", self.turns[2]["text"])
        self.assertNotIn("3 passed", self.turns[1]["text"])
        self.assertIn("Поправил a.py.", self.turns[2]["text"])

    def test_english_specstory_unchanged(self):
        spec = ("<!-- Claude Code Session abc12345 (2026-08-01 10:00:00Z) -->\n\n"
                "_**User (2026-08-01 10:00:00Z)**_\n\nпривет\n\n---\n\n"
                "_**Agent (claude 2026-08-01 10:00:05Z)**_\n\nответ\n")
        doc = normalize.parse(spec)
        self.assertEqual([t["role"] for t in doc["turns"]], ["user", "assistant"])
        self.assertEqual(doc["started_at"], "2026-08-01 10:00:00Z")


class JsonlExport(unittest.TestCase):
    """Экспорт native JSONL заголовками с временем и разметкой вызовов."""

    HEAD = ("# codex — 2026-09-22T17:12:02.491Z\n\n"
            "Offline snapshot of existing JSONL records, through 2026-09-22T20:16:06.528Z.\n"
            "Visible user/assistant messages, agent communications and textual tool "
            "calls/results are retained.\n\n")

    LOG = HEAD + (
        "## user — 2026-09-22T17:12:04.000Z\n\n"
        "```text\n"
        "собери дашборд по макету и прогони тесты\n"
        "```\n\n"
        "### Tool call: exec — 2026-09-22T17:12:11.351Z\n\n"
        "Call ID: `call_A`\n\n"
        "```text\n"
        'text(await tools.exec_command({cmd:"mvn -q test",max_output_tokens:6000}));\n'
        "```\n\n"
        "### Tool result — 2026-09-22T17:12:31.779Z\n\n"
        "Call ID: `call_A`\n\n"
        "````text\n"
        "3 tests passed\n"
        "````\n\n"
        "## assistant — 2026-09-22T17:13:00.000Z\n\n"
        "```text\n"
        "Тесты зелёные, дашборд собран.\n"
        "```\n"
    )

    def setUp(self):
        self.doc = normalize.parse(self.LOG)

    def test_format_and_collector(self):
        self.assertEqual(self.doc["format"], "jsonl-export")
        self.assertEqual(self.doc["collector"], "codex-jsonl-export")

    def test_roles_and_timestamps(self):
        # вызов без предшествующей прозы открывает ход агента
        self.assertEqual([t["role"] for t in self.doc["turns"]],
                         ["user", "assistant", "assistant"])
        self.assertEqual(self.doc["user_turns"], 1)
        self.assertEqual(self.doc["started_at"], "2026-09-22T17:12:04.000Z")
        self.assertEqual(self.doc["ended_at"], "2026-09-22T17:13:00.000Z")
        m = self.doc["mechanical_metrics"]
        self.assertTrue(m["timestamps_available"])
        self.assertEqual(m["session_span_seconds"], 56.0)

    def test_prose_comes_from_the_fence(self):
        self.assertEqual(self.doc["turns"][0]["text"], "собери дашборд по макету и прогони тесты")
        self.assertNotIn("Call ID", self.doc["turns"][1]["text"])

    def test_tool_call_command_and_result(self):
        tools = self.doc["turns"][1]["tools"]
        self.assertEqual([t["name"] for t in tools], ["exec"])
        self.assertEqual(tools[0]["type"], "shell")
        self.assertEqual(tools[0]["command"], "mvn -q test")
        self.assertEqual(tools[0]["output_chars"], len("3 tests passed"))
        self.assertTrue(self.doc["mechanical_metrics"]["commands_available"])
        self.assertEqual(self.doc["mechanical_metrics"]["verification_commands"], 1)

    def test_maven_with_flags_and_windows_wrapper_count_as_verification(self):
        for cmd in ("mvn -q test", "mvn -DskipTests=false verify", "& $maven.FullName test",
                    "./mvnw -q verify", ".\\gradlew.bat check"):
            with self.subTest(cmd=cmd):
                self.assertTrue(normalize.VERIFY_CMD_RE.search(cmd), cmd)
        for cmd in ("mvn -DskipTests dependency:go-offline", "git status"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(normalize.VERIFY_CMD_RE.search(cmd), cmd)

    def test_json_command_array(self):
        log = self.HEAD + ("### Tool call: local_shell — 2026-09-22T17:12:11.351Z\n\n"
                           "```json\n"
                           '{"command": ["bash", "-lc", "pytest -q"]}\n'
                           "```\n")
        self.assertEqual(normalize.parse(log)["turns"][0]["tools"][0]["command"],
                         "bash -lc pytest -q")

    def test_mcp_tool_has_no_command(self):
        log = self.HEAD + ("### Tool call: mcp__demo-srv__ingestion_get_run — "
                           "2026-09-13T20:25:31.833Z\n\n"
                           "```json\n"
                           '{"runId": "a072d3c3"}\n'
                           "```\n")
        tool = normalize.parse(log)["turns"][0]["tools"][0]
        self.assertEqual(tool["type"], "mcp")
        self.assertIsNone(tool["command"])

    def test_agent_message_is_not_a_human_turn(self):
        log = self.HEAD + ("### Agent message — 2026-09-22T17:12:06.854Z\n\n"
                           "```text\n"
                           '{"author": "/root", "recipient": "/root/status_regression"}\n'
                           "```\n\n"
                           "## assistant — 2026-09-22T17:12:20.000Z\n\n"
                           "```text\nПринял задачу.\n```\n")
        doc = normalize.parse(log)
        self.assertEqual(doc["user_turns"], 0)
        self.assertEqual(doc["synthetic_turns"], 1)
        self.assertEqual(doc["turns"][0]["synthetic_reason"], "agent_message")

    def test_markers_inside_fences_are_ignored(self):
        log = self.HEAD + (
            "## user — 2026-09-22T17:12:04.000Z\n\n"
            "```text\nпосмотри прошлый лог\n```\n\n"
            "### Tool call: exec — 2026-09-22T17:12:11.351Z\n\n"
            "```text\n"
            'text(await tools.exec_command({cmd:"Get-Content old.md",max_output_tokens:6000}));\n'
            "```\n\n"
            "### Tool result — 2026-09-22T17:12:31.779Z\n\n"
            "````text\n"
            "## user — 2026-09-01T10:00:00.000Z\n"
            "### Tool call: exec — 2026-09-01T10:00:01.000Z\n"
            "````\n"
        )
        doc = normalize.parse(log)
        self.assertEqual(doc["user_turns"], 1)
        self.assertEqual([t["role"] for t in doc["turns"]], ["user", "assistant"])
        self.assertEqual(len(doc["turns"][1]["tools"]), 1)

    def test_not_hijacked_by_scrollback_detector(self):
        log = self.LOG + ("\n### Tool result — 2026-09-22T17:14:00.000Z\n\n"
                          "```text\n❯ mvn -q test\n● готово\n```\n")
        self.assertEqual(normalize.parse(log)["format"], "jsonl-export")

    def test_session_id_survives_rename(self):
        a = normalize.parse(self.LOG, path=Path("codex/2026-09-22_17-12-02Z.md"))
        b = normalize.parse(self.LOG, path=Path("logs/other-name.md"))
        self.assertEqual(a["session_id"], b["session_id"])
        self.assertTrue(a["session_id"].startswith("jex-"))

    def test_session_id_seeds_on_the_banner_not_the_prose(self):
        body = ("## user — 2026-09-13T20:14:25.000Z\n\n"
                "```text\nConnection check only. List MCP tools.\n```\n")
        one = normalize.parse("# claude — 2026-09-13T20:14:23.000Z\n\n"
                              "Offline export of available JSONL records.\n\n" + body)
        two = normalize.parse("# claude — 2026-09-13T20:15:58.000Z\n\n"
                              "Offline export of available JSONL records.\n\n" + body)
        self.assertNotEqual(one["session_id"], two["session_id"])

    def test_vendor_context_is_not_a_human_turn(self):
        log = self.HEAD + ("## user — 2026-09-12T21:16:34.628Z\n\n"
                           "```text\n"
                           "<recommended_plugins>\n- Demo (demo-plugin)\n"
                           "</recommended_plugins>\n"
                           "<environment_context>\n  <shell>powershell</shell>\n"
                           "</environment_context>\n"
                           "```\n")
        doc = normalize.parse(log)
        self.assertEqual(doc["user_turns"], 0)
        self.assertEqual(doc["turns"][0]["synthetic_reason"], "vendor_context")

    def test_does_not_hijack_specstory(self):
        spec = ("<!-- Claude Code Session abc12345 (2026-08-01 10:00:00Z) -->\n\n"
                "_**User (2026-08-01 10:00:00Z)**_\n\nпривет\n\n---\n\n"
                "_**Agent (claude 2026-08-01 10:00:05Z)**_\n\nответ\n")
        self.assertEqual(normalize.parse(spec)["format"], "specstory")

    def test_banner_without_the_note_is_not_this_format(self):
        plain = "# codex — 2026-09-22T17:12:02.491Z\n\nЗаметки.\n"
        self.assertFalse(normalize.looks_like_jsonl_export(plain))


class NormalizeCli(unittest.TestCase):
    """`sr.py normalize`: флаги, коды выхода, JSON в stdout."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="sr-normalize-")
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def sr(self, *args):
        return run_sr("normalize", *args, home=self.home, cwd=self.tmp)

    def ok_json(self, *args) -> dict:
        res = self.sr(*args)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    def test_full_output(self):
        doc = self.ok_json(str(LOGS / "specstory-claude.md"))
        self.assertEqual(doc["format"], "specstory")
        self.assertEqual(len(doc["turns"]), doc["turns_total"])
        self.assertEqual(doc["path"], str(LOGS / "specstory-claude.md"))

    def test_stats_drops_turns(self):
        doc = self.ok_json(str(LOGS / "specstory-claude.md"), "--stats")
        self.assertNotIn("turns", doc)
        self.assertIn("mechanical_metrics", doc)

    def test_turn_slice(self):
        doc = self.ok_json(str(LOGS / "specstory-claude.md"), "--from-turn", "1", "--to-turn", "2")
        self.assertEqual([t["index"] for t in doc["turns"]], [1])
        self.assertEqual(doc["turns_slice"]["from_turn"], 1)
        self.assertEqual(doc["turns_slice"]["to_turn"], 2)
        self.assertEqual(doc["turns_slice"]["of_total"], doc["turns_total"])
        self.assertEqual(doc["mechanical_metrics"]["turns_total"], doc["turns_total"])

    def test_roles_and_drop_synthetic(self):
        doc = self.ok_json(str(LOGS / "cc-scrollback.txt"), "--roles", "user", "--drop-synthetic")
        self.assertTrue(doc["turns"])
        self.assertTrue(all(t["role"] == "user" and not t["synthetic"] for t in doc["turns"]))
        self.assertEqual(doc["turns_slice"]["roles"], ["user"])
        self.assertTrue(doc["turns_slice"]["synthetic_dropped"])

    def test_roles_bogus_is_code_2(self):
        res = self.sr(str(LOGS / "specstory-claude.md"), "--roles", "bogus")
        self.assertEqual(res.returncode, 2)
        self.assertIn("bogus", res.stderr)

    def test_negative_slice_is_code_2(self):
        res = self.sr(str(LOGS / "specstory-claude.md"), "--from-turn", "-1")
        self.assertEqual(res.returncode, 2)
        self.assertIn("--from-turn", res.stderr)

    def test_missing_file_is_code_2(self):
        missing = self.tmp / "nope.md"
        res = self.sr(str(missing))
        self.assertEqual(res.returncode, 2)
        self.assertIn(str(missing), res.stderr)

    def test_file_without_turns_is_code_1(self):
        empty = self.tmp / "note.md"
        empty.write_text("# Заметка\n\nпросто текст\n", encoding="utf-8")
        res = self.sr(str(empty))
        self.assertEqual(res.returncode, 1)
        for name, _, _ in normalize.FORMATS:
            self.assertIn(name, res.stderr)

    def test_tools_map(self):
        doc = self.ok_json(str(LOGS / "specstory-claude.md"), "--tools-map")
        self.assertNotIn("turns", doc)
        names = [row["name"] for row in doc["tools_map"]]
        self.assertNotIn("Read", names)                 # чтения выброшены
        self.assertIn({"turn_index": 1, "name": "Bash", "output_chars": len("1 passed in 0.01s\n"),
                       "command": "python3 -m pytest -q", "type": "shell"}, doc["tools_map"])
        self.assertEqual(doc["tools_map_scope"]["calls"], len(doc["tools_map"]))

    def test_tools_map_collapses_hidden_calls(self):
        doc = self.ok_json(str(LOGS / "cc-scrollback.txt"), "--tools-map")
        hidden = [row for row in doc["tools_map"] if row.get("commands_hidden")]
        self.assertEqual(hidden, [{"turn_index": 1, "name": "Bash", "type": "shell",
                                   "count": 2, "commands_hidden": True}])

    def test_no_tool_output_and_max_turn_chars(self):
        doc = self.ok_json(str(LOGS / "specstory-claude.md"), "--no-tool-output",
                           "--max-turn-chars", "5")
        tools = [t for turn in doc["turns"] for t in turn["tools"]]
        self.assertTrue(all(t["output_head"] is None for t in tools))
        first = doc["turns"][0]
        self.assertTrue(first["text"].startswith("Добав\n…[обрезано, всего "), first["text"])
        self.assertGreater(first["chars"], 5)

    def test_works_without_workspace(self):
        # в tmp нет .retropair — команде workspace не нужен
        self.ok_json(str(LOGS / "md-headings.md"), "--stats")


class Cleanliness(unittest.TestCase):
    def test_no_private_words_in_module(self):
        words = ["студ" + "ент", "мод" + "ул", "кур" + "с", "опер" + "атор"]
        for path in (SCRIPTS / "lib" / "normalize.py", SCRIPTS / "lib" / "cmd_normalize.py"):
            text = path.read_text(encoding="utf-8")
            for word in words:
                with self.subTest(file=path.name, word=word):
                    self.assertIsNone(re.search(word, text, re.I))


if __name__ == "__main__":
    unittest.main()
