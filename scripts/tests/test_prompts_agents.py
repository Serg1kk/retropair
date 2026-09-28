"""Агенты и контракт входа: frontmatter, версии в теле, пути `${CLAUDE_PLUGIN_ROOT}`, оговорка о литерале.

Frontmatter читается парсером из `test_manifest`, модель и effort сверяются с его `AGENTS`.
Для судьи дополнительно: таблица «параметр карточки → как меняет текст», ключевые поля выхода,
восемь осей, вердикты ключами, `feedback_draft` не записан как его обязанность.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

import _util  # noqa: F401 — кладёт scripts/ в sys.path
from _util import PLUGIN
from test_manifest import AGENTS, AXES, frontmatter

TOOLS = {
    "log-collector": "Read, Grep, Glob, Bash, Write, Edit",
    "log-judge": "Read, Grep, Glob, Bash, Write, Edit",
    "feedback-critic": "Read, Write, Edit",
}
BODY_VERSIONS = {
    "log-collector": "collector_version: v4",
    "log-judge": "prompt_version: v13",
    "feedback-critic": "critic_version: v1",
}
FILE_VERSIONS = {
    "references/axes.md": "axes_version: v3",
    "references/antipatterns.md": "antipatterns_version: v4",
    "references/best-practices.md": "best_practices_version: v2",
    "references/output-contract.md": "output_contract_version: v4",
    "references/feedback-examples.md": "feedback_examples_version: v4",
    "skills/review/references/judge-input.md": "judge_input_version: v1",
}
FRONTMATTER_KEYS = {"name", "description", "tools", "model", "effort"}
LITERAL_NOTE = "Если в пути виден литерал `${CLAUDE_PLUGIN_ROOT}`, подставь `plugin_root` из входа."

JUDGE_INPUT = "skills/review/references/judge-input.md"
INPUT_SECTION = "## Вход коллектора и судьи"
CRITIC_SECTION = "## Вход критика"
INPUT_FIELDS = [
    "plugin_root", "workspace_root", "person_id", "period", "label", "from", "to", "anchor",
    "run_id", "lang", "output_path", "log_paths", "batch", "index", "total",
    "collector_outputs", "card_slice", "task_summary", "open_recs", "id", "text", "show_scores",
]
CRITIC_FIELDS = ["plugin_root", "lang", "draft_path", "card_slice", "output_path"]

CARD_SECTION = "## Карточка участника: параметр и как он меняет текст"
CARD_FIELDS = [
    "address", "length", "tone", "opening_summary", "mark_sections", "practices",
    "skip_basics", "boundaries", "avoid", "value_focus", "show_dynamics",
]
NOT_FOR_JUDGE = ["channel", "template", "prefix"]
JUDGE_MUST_NAME = ["feedback_parts", "level_judge", "insufficient_evidence", "open_recs", "card_slice"]
VERDICT_KEYS = [
    "even", "even_but_one", "first_place_above", "second_place_above", "third_place_above", "not_visible",
]
SESSION_STATUSES = [
    "reviewed", "empty_transcript", "transcript_too_short", "unreadable_input", "agent_subtranscript",
    "duplicate_export", "other_period", "not_batched", "excluded_other",
]
# идентификаторы исходника, которых в судье плагина быть не должно (склейкой: гейт сканирует .py)
JUDGE_FORBIDDEN_IDS = [
    "overall_level", "provided_by_" "course", "course" "_prompts", "rules_audit", "student" "_slug",
    "spans_" "modules", "other_" "module", "no_" "student_profile",
]
DRAFT_NEGATION = re.compile(r"не пишешь|не писать|НИКОГДА|собирает рендер")

VERSION_WINDOW = 10
PLUGIN_PATH = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}/([\w./-]+)")
CHECKED_DIRS = ("references/", "skills/", "agents/", "templates/")
SCRIPT_ENTRY = "scripts/sr.py"


def body(text: str) -> str:
    """Текст после frontmatter; без frontmatter — весь текст."""
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---\n", 4)
    if end < 0:
        raise AssertionError("frontmatter не закрыт")
    return text[end + len("\n---\n"):]


def has_version_line(text: str, line: str) -> bool:
    """Строка версии отдельной строкой в первых VERSION_WINDOW строках тела."""
    head = body(text).splitlines()[:VERSION_WINDOW]
    return any(row.strip() == line for row in head)


def bad_paths(root: Path, text: str) -> list[str]:
    """Пути `${CLAUDE_PLUGIN_ROOT}/…`, которые не ведут на файл плагина, с причиной."""
    hits: list[str] = []
    for match in PLUGIN_PATH.finditer(text):
        rel = match.group(1).rstrip(".,;:")
        nxt = text[match.end():match.end() + 1]
        if nxt == "<":
            continue  # шаблонный путь (`feedback.<lang>.md`): файла с таким именем нет намеренно
        if nxt == "{":
            hits.append(rel + "{…}: brace-expansion вместо полного пути")
            continue
        if rel.startswith("scripts/"):
            if rel != SCRIPT_ENTRY:
                hits.append(f"{rel}: из scripts/ ссылаться можно только на {SCRIPT_ENTRY}")
            elif not (root / rel).is_file():
                hits.append(f"{rel}: файла нет")
            continue
        if not rel.startswith(CHECKED_DIRS):
            hits.append(f"{rel}: путь вне {', '.join(CHECKED_DIRS)}")
            continue
        if not (root / rel).is_file():
            hits.append(f"{rel}: файла нет")
    return hits


def section(text: str, heading: str) -> str:
    """Раздел от заголовка `heading` до следующего заголовка того же уровня."""
    start = text.find(heading + "\n")
    if start < 0:
        raise AssertionError(f"нет раздела «{heading}»")
    rest = text[start + len(heading) + 1:]
    nxt = re.search(r"^## ", rest, flags=re.M)
    return rest[: nxt.start()] if nxt else rest


def card_table_rows(text: str) -> dict[str, str]:
    """Строки таблицы раздела `CARD_SECTION`: имя параметра из первой ячейки → вся строка.

    Имя — первый токен в бэктиках первой ячейки, без префикса `feedback.`; `profile…` даёт ключ `profile`.
    """
    rows: dict[str, str] = {}
    for line in section(text, CARD_SECTION).splitlines():
        if not line.startswith("|") or set(line) <= set("|-: "):
            continue
        first = re.search(r"`([a-z_.]+)", line.strip("|").split("|", 1)[0])
        if not first:
            continue
        key = first.group(1).removeprefix("feedback.")
        rows.setdefault("profile" if key.startswith("profile") else key, line)
    return rows


def draft_duty_lines(text: str) -> list[str]:
    """Строки, где `feedback_draft` упомянут без отрицания: похоже на обязанность писать его."""
    return [line for line in text.splitlines() if "`feedback_draft`" in line and not DRAFT_NEGATION.search(line)]


def missing_fields(text: str, fields: list[str]) -> list[str]:
    """Поля, которые не названы в тексте в бэктиках."""
    return [f for f in fields if not re.search(r"`" + re.escape(f) + r"(`|\{|\[)", text)]


class AgentsSynthetic(unittest.TestCase):
    """Каждая ветка проверки на синтетике."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="sr-agents-")
        self.root = Path(self._tmp.name)
        for rel in ("references/a.md", "references/schemas/c.json", "scripts/sr.py", "scripts/other.py"):
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text("x\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_existing_paths_pass(self) -> None:
        text = ("см. `${CLAUDE_PLUGIN_ROOT}/references/a.md`, схема ${CLAUDE_PLUGIN_ROOT}/references/schemas/c.json.\n"
                'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sr.py" normalize x\n')
        self.assertEqual(bad_paths(self.root, text), [])

    def test_missing_file_caught(self) -> None:
        text = "`${CLAUDE_PLUGIN_ROOT}/references/a.md` и `${CLAUDE_PLUGIN_ROOT}/references/nope.md`"
        self.assertEqual(bad_paths(self.root, text), ["references/nope.md: файла нет"])

    def test_brace_expansion_caught(self) -> None:
        text = "`${CLAUDE_PLUGIN_ROOT}/references/schemas/{c,d}.json`"
        hits = bad_paths(self.root, text)
        self.assertEqual(len(hits), 1)
        self.assertIn("brace-expansion", hits[0])

    def test_scripts_only_entry_point(self) -> None:
        hits = bad_paths(self.root, "${CLAUDE_PLUGIN_ROOT}/scripts/other.py")
        self.assertEqual(len(hits), 1)
        self.assertIn("scripts/other.py", hits[0])
        (self.root / "scripts/sr.py").unlink()
        self.assertEqual(bad_paths(self.root, "${CLAUDE_PLUGIN_ROOT}/scripts/sr.py"),
                         ["scripts/sr.py: файла нет"])

    def test_placeholder_path_skipped_explicitly(self) -> None:
        text = ("шаблон `${CLAUDE_PLUGIN_ROOT}/templates/feedback.<lang>.md`, "
                "схема `${CLAUDE_PLUGIN_ROOT}/references/<name>.json`")
        self.assertEqual(bad_paths(self.root, text), [])

    def test_path_outside_checked_dirs_caught(self) -> None:
        hits = bad_paths(self.root, "${CLAUDE_PLUGIN_ROOT}/hooks/hooks.json")
        self.assertEqual(len(hits), 1)
        self.assertIn("вне", hits[0])

    def test_version_line_position_and_form(self) -> None:
        fm = "---\nname: x\n---\n"
        self.assertTrue(has_version_line(fm + "\n# T\n\nv_version: v1\n", "v_version: v1"))
        self.assertFalse(has_version_line(fm + "\n" * 11 + "v_version: v1\n", "v_version: v1"))
        self.assertFalse(has_version_line(fm + "текст v_version: v1 в строке\n", "v_version: v1"))
        self.assertTrue(has_version_line("# T\n\nv_version: v1\n", "v_version: v1"))

    def test_body_drops_frontmatter(self) -> None:
        self.assertEqual(body("---\nname: x\n---\nтело\n"), "тело\n")
        with self.assertRaises(AssertionError):
            body("---\nname: x\n")

    def test_missing_fields_and_sections(self) -> None:
        text = "## Вход\n\n| `a` | `b{x}` | `c[]` |\n\n## Другое\n\n`d`\n"
        part = section(text, "## Вход")
        self.assertEqual(missing_fields(part, ["a", "b", "c", "d"]), ["d"])
        with self.assertRaises(AssertionError):
            section(text, "## Нет такого")

    def test_card_table_rows(self) -> None:
        text = (CARD_SECTION + "\n\n| Параметр | Как меняет |\n|---|---|\n"
                "| `feedback.address` (`you`) | одно обращение |\n| `tone` | тон |\n"
                "| `profile.grade`, `profile.role` | формулировка |\n\n## Дальше\n\n| `avoid` | x |\n")
        rows = card_table_rows(text)
        self.assertEqual(set(rows), {"address", "tone", "profile"})
        self.assertEqual([f for f in CARD_FIELDS if f not in rows][:1], ["length"])

    def test_draft_duty_lines(self) -> None:
        text = ("Заполни `feedback_draft` по контракту.\n"
                "`feedback_draft` ты не пишешь: его собирает рендер.\n"
                "- НИКОГДА не писать `feedback_draft`.\n")
        self.assertEqual(draft_duty_lines(text), ["Заполни `feedback_draft` по контракту."])

    def test_draft_duty_loose_negation_not_enough(self) -> None:
        text = ("Заполни `feedback_draft`, не забудь про скрипт.\n"
                "`feedback_draft` собирает рендер по шаблону.\n"
                "`feedback_draft` не писать.\n")
        self.assertEqual(draft_duty_lines(text), ["Заполни `feedback_draft`, не забудь про скрипт."])


class AgentsReal(unittest.TestCase):
    """Реальные агенты и референсы плагина."""

    def check_agent(self, name: str) -> None:
        path = PLUGIN / "agents" / f"{name}.md"
        fm = frontmatter(path)
        model, effort = AGENTS[name]
        self.assertEqual(set(fm), FRONTMATTER_KEYS, name)
        self.assertEqual(fm["name"], name)
        self.assertEqual((fm["model"], fm["effort"]), (model, effort), name)
        self.assertEqual(fm["tools"], TOOLS[name], name)
        desc = fm["description"]
        self.assertTrue(desc.startswith('"') and desc.endswith('"'), name)
        self.assertIn("Триггеры:", desc, name)
        self.assertRegex(desc, r"«[А-Яа-яЁё][^»]*»", f"{name}: нет ru-триггера")
        self.assertRegex(desc, r"«[A-Za-z][^»]*»", f"{name}: нет en-триггера")
        text = path.read_text(encoding="utf-8")
        self.assertTrue(has_version_line(text, BODY_VERSIONS[name]), f"{name}: {BODY_VERSIONS[name]}")
        self.assertIn(LITERAL_NOTE, body(text), f"{name}: нет оговорки о литерале")
        self.assertRegex(body(text), PLUGIN_PATH, f"{name}: нет ни одного пути плагина")
        self.assertEqual(bad_paths(PLUGIN, body(text)), [], name)

    def test_collector_agent(self) -> None:
        self.check_agent("log-collector")

    def test_critic_agent(self) -> None:
        self.check_agent("feedback-critic")

    def test_judge_agent(self) -> None:
        self.check_agent("log-judge")

    def judge_body(self) -> str:
        return body((PLUGIN / "agents" / "log-judge.md").read_text(encoding="utf-8"))

    def test_judge_description_says_structure(self) -> None:
        desc = frontmatter(PLUGIN / "agents" / "log-judge.md")["description"]
        self.assertIn("пишет структуру текста", desc)
        self.assertNotIn("черновик", desc.lower())

    def test_judge_card_table(self) -> None:
        rows = card_table_rows(self.judge_body())
        self.assertEqual([f for f in CARD_FIELDS if f not in rows], [])
        self.assertIn("profile", rows)
        self.assertEqual([f for f in NOT_FOR_JUDGE if f in rows], [], "channel/template/prefix судье не подаются")

    def test_judge_names_outputs_axes_verdicts_statuses(self) -> None:
        text = self.judge_body()
        for group in (JUDGE_MUST_NAME, AXES, VERDICT_KEYS, SESSION_STATUSES):
            self.assertEqual(missing_fields(text, group), [])
        self.assertIn("REC-<person_id>-<NNN>", text)

    def test_judge_drops_source_ids(self) -> None:
        text = self.judge_body()
        self.assertEqual([i for i in JUDGE_FORBIDDEN_IDS if i in text], [])

    def test_judge_does_not_own_draft(self) -> None:
        text = self.judge_body()
        self.assertIn("`feedback_draft`", text)
        self.assertEqual(draft_duty_lines(text), [])

    def test_reference_versions(self) -> None:
        for rel, line in FILE_VERSIONS.items():
            with self.subTest(rel=rel):
                text = (PLUGIN / rel).read_text(encoding="utf-8")
                self.assertTrue(has_version_line(text, line), f"{rel}: нет строки «{line}»")

    def test_judge_input_fields(self) -> None:
        text = (PLUGIN / JUDGE_INPUT).read_text(encoding="utf-8")
        self.assertEqual(missing_fields(section(text, INPUT_SECTION), INPUT_FIELDS), [])
        self.assertEqual(missing_fields(section(text, CRITIC_SECTION), CRITIC_FIELDS), [])

    def test_reference_paths(self) -> None:
        for path in sorted((PLUGIN / "references").glob("*.md")):
            with self.subTest(ref=path.name):
                self.assertEqual(bad_paths(PLUGIN, path.read_text(encoding="utf-8")), [])

    def test_judge_input_paths_and_checks_link(self) -> None:
        text = (PLUGIN / JUDGE_INPUT).read_text(encoding="utf-8")
        self.assertEqual(bad_paths(PLUGIN, text), [])
        self.assertIn("${CLAUDE_PLUGIN_ROOT}/references/output-contract.md", text)
        self.assertIn("«Проверки скриптом»", text)
        self.assertIn("## Проверки скриптом\n", (PLUGIN / "references/output-contract.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
