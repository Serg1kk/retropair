"""Документация сбора логов: инструкции коллекторов, конфиг SpecStory без облака, чистота текста, ссылки."""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

import _util  # noqa: F401 — кладёт scripts/ в sys.path
from _util import PLUGIN
from lib import inventory, sanitize
from test_docs import relative_links
from test_manifest import frontmatter

SKILL_MD = PLUGIN / "skills" / "collect" / "SKILL.md"
COLLECT_DOC = PLUGIN / "docs" / "ru" / "collect.md"
PLUGIN_ROOT_VAR = "${CLAUDE_PLUGIN_ROOT}"
# `description` скилла — контракт триггеров: тело меняется, описание нет.
SKILL_DESCRIPTION = (
    '"Collects coding-agent session logs for a period: finds Claude Code JSONL transcripts and '
    "SpecStory exports, sanitizes secrets and personal paths, normalizes them and updates the logs "
    "inventory with statuses. Use when the user wants to gather, refresh or inspect the session "
    "logs that a retro will be based on. Триггеры: «собери логи сессий», «обнови логи для ретро», "
    "«что собралось за период», «почисти логи», «collect session logs», «refresh the logs», "
    "«gather transcripts for the retro». Do NOT use for first-time setup (setup), assessing the "
    'logs (review) or building reports (report)."'
)

INSTRUCTIONS_DIR = PLUGIN / "references" / "collector-instructions"
SPECSTORY_TOML = INSTRUCTIONS_DIR / "specstory-config.toml"
INSTRUCTIONS = [
    "antigravity.md", "crush.md", "cursor.md",
    "intellij-claude-code.md", "intellij-cline.md", "intellij-codex.md", "intellij-copilot.md",
    "ne-vybral.md", "opencode.md", "visual-studio.md",
    "vscode-claude-code.md", "vscode-codex.md", "windows.md",
]
# Файлы-указатели без собственного пути сбора: отсылают к соседним инструкциям.
POINTER_FILES = {"ne-vybral.md", "visual-studio.md"}
COLLECT_CMD = 'sr.py" collect'

# Запрещённые фрагменты собираются в рантайме: гейт чистоты сканирует и сам этот файл.
_FORBIDDEN_PARTS = [
    ("сту", "дент"), ("до", "машк"), ("сд", "а[тч]"), ("ку", "рс"), ("мо", "дул"),
    ("опе", "ратор"), ("напиши", " мне"), ("/", "Users/"),
]
FORBIDDEN_RE = re.compile("|".join(a + b for a, b in _FORBIDDEN_PARTS), re.IGNORECASE)

# Те же исключения, что у гейта no-personal.
EMAIL_RE = re.compile(r"[\w.%+-]+@[\w-]+(?:\.[\w-]+)+")
EMAIL_ALLOWED_EXACT = {"git@github.com"}

# Ожидаемые ключи конфига SpecStory: (секция, ключ) → значение.
TOML_EXPECTED = {
    ("cloud_sync", "enabled"): "false",
    ("analytics", "enabled"): "false",
    ("version_check", "enabled"): "false",
    ("telemetry", "prompts"): "false",
    ("redaction", "enabled"): "true",
}


def forbidden_hits(text: str) -> list[str]:
    """Строки с запрещёнными словами: `N: фрагмент`."""
    return [f"{n}: {line.strip()[:120]}"
            for n, line in enumerate(text.splitlines(), start=1) if FORBIDDEN_RE.search(line)]


def bad_emails(text: str) -> list[str]:
    """E-mail вне исключений гейта (`@example.*`, `.test`, `.invalid`, `noreply@`, `git@github.com`)."""
    bad = []
    for match in EMAIL_RE.finditer(text):
        mail = match.group(0).rstrip(".")
        low = mail.lower()
        if (low in EMAIL_ALLOWED_EXACT or low.startswith("noreply@") or "@example." in low
                or low.endswith(".invalid") or low.endswith(".test")):
            continue
        bad.append(mail)
    return bad


def toml_values(text: str) -> dict[tuple[str, str], str]:
    """Плоский разбор простого TOML: `[секция]` и `ключ = значение`, комментарии отброшены."""
    values: dict[tuple[str, str], str] = {}
    section = ""
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        head = re.fullmatch(r"\[([A-Za-z0-9_.-]+)\]", line)
        if head:
            section = head.group(1)
            continue
        pair = re.fullmatch(r"([A-Za-z0-9_-]+)\s*=\s*(.+)", line)
        if pair:
            values[(section, pair.group(1))] = pair.group(2).strip()
    return values


def broken_or_outside_links(path: Path, plugin_root: Path) -> list[str]:
    """Относительные ссылки файла, которые не ведут на существующий файл внутри `plugin_root`."""
    bad = []
    root = plugin_root.resolve()
    for lineno, target in relative_links(path.read_text(encoding="utf-8")):
        resolved = (path.parent / target).resolve()
        if not resolved.is_file() or root not in resolved.parents:
            bad.append(f"{path.name}:{lineno}: {target}")
    return bad


class CheckersTest(unittest.TestCase):
    """Сами проверки на синтетике."""

    def test_forbidden_words_caught_case_insensitive(self) -> None:
        sample = "ок\n" + "Сту" + "денты сдают\n" + "ещё " + "/" + "Users/alice/x\nок\n"
        self.assertEqual([h.split(":")[0] for h in forbidden_hits(sample)], ["2", "3"])

    def test_neutral_text_passes(self) -> None:
        self.assertEqual(forbidden_hits("Cursor, ресурс, создать, передача, /home/user/x\n"), [])

    def test_email_exceptions_match_gate(self) -> None:
        ok = "a@example.com b@example.org noreply@host.io c@x.test d@y.invalid git@github.com"
        self.assertEqual(bad_emails(ok), [])
        personal = "a" + "@b.co"
        self.assertEqual(bad_emails(f"пишите на {personal}."), [personal])

    def test_toml_values_ignore_comments(self) -> None:
        text = "# шапка\n[cloud_sync]\n# пояснение\nenabled = false  # выкл\n[x]\nk = 1\n"
        self.assertEqual(toml_values(text), {("cloud_sync", "enabled"): "false", ("x", "k"): "1"})

    def test_links_outside_root_or_missing_reported(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sr-cdocs-") as tmp:
            root = Path(tmp) / "plugin"
            (root / "refs").mkdir(parents=True)
            (Path(tmp) / "outside.md").write_text("x\n", encoding="utf-8")
            (root / "refs" / "b.md").write_text("b\n", encoding="utf-8")
            page = root / "refs" / "a.md"
            page.write_text("[ок](b.md) [нет](c.md)\n[вне](../../outside.md) [web](https://example.com)\n",
                            encoding="utf-8")
            self.assertEqual(broken_or_outside_links(page, root),
                             ["a.md:1: c.md", "a.md:2: ../../outside.md"])


class CollectorInstructionsTest(unittest.TestCase):
    """Реальный артефакт: `references/collector-instructions/`."""

    def all_files(self) -> list[Path]:
        return sorted(p for p in INSTRUCTIONS_DIR.rglob("*") if p.is_file())

    def test_full_set_present(self) -> None:
        for name in ["README.md", *INSTRUCTIONS, "specstory-config.toml"]:
            with self.subTest(file=name):
                self.assertTrue((INSTRUCTIONS_DIR / name).is_file(), f"нет файла ({INSTRUCTIONS_DIR / name})")

    def test_specstory_config_has_cloud_and_network_off(self) -> None:
        values = toml_values(SPECSTORY_TOML.read_text(encoding="utf-8"))
        for key, expected in TOML_EXPECTED.items():
            with self.subTest(key=key):
                self.assertEqual(values.get(key), expected, f"{SPECSTORY_TOML.name}: [{key[0]}] {key[1]}")

    def test_no_forbidden_words(self) -> None:
        for path in self.all_files():
            with self.subTest(file=path.name):
                self.assertEqual(forbidden_hits(path.read_text(encoding="utf-8")), [])

    def test_no_personal_emails(self) -> None:
        for path in self.all_files():
            with self.subTest(file=path.name):
                self.assertEqual(bad_emails(path.read_text(encoding="utf-8")), [])

    def test_relative_links_resolve_inside_plugin(self) -> None:
        for path in sorted(INSTRUCTIONS_DIR.glob("*.md")):
            with self.subTest(file=path.name):
                self.assertEqual(broken_or_outside_links(path, PLUGIN), [])

    def test_readme_links_every_instruction(self) -> None:
        targets = {t for _, t in relative_links((INSTRUCTIONS_DIR / "README.md").read_text(encoding="utf-8"))}
        for name in INSTRUCTIONS:
            with self.subTest(file=name):
                self.assertIn(name, targets)

    def test_instructions_use_plugin_collect_not_manual_script(self) -> None:
        for name in INSTRUCTIONS:
            text = (INSTRUCTIONS_DIR / name).read_text(encoding="utf-8")
            with self.subTest(file=name):
                self.assertNotIn("sanitize-log", text)
                if name not in POINTER_FILES:
                    self.assertIn(COLLECT_CMD, text)

    def test_readme_hands_over_cleaned_person_folder_not_whole_logs(self) -> None:
        # Совет инструкций и collect.md не противоречат друг другу: в self не коммитить,
        # ревьюеру — папку logs/<id>/ после проверки глазами.
        text = (INSTRUCTIONS_DIR / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("закоммитьте эту папку", text)
        for needle in (".sessionretro/logs/<ваш id>/", "глазами", "без логина", "self"):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)

    def test_windows_states_collect_unsupported(self) -> None:
        text = (INSTRUCTIONS_DIR / "windows.md").read_text(encoding="utf-8")
        self.assertIn("/export", text)
        self.assertIn("не поддерживается", text)


def body_of(skill_text: str) -> str:
    """Тело SKILL.md после закрывающего `---` frontmatter."""
    parts = skill_text.split("---", 2)
    return parts[2] if skill_text.startswith("---") and len(parts) == 3 else skill_text


class CollectPagesTest(unittest.TestCase):
    """Реальный артефакт: `skills/collect/SKILL.md` и страница пользователя `docs/ru/collect.md`."""

    @classmethod
    def setUpClass(cls) -> None:
        for path in (SKILL_MD, COLLECT_DOC):
            if not path.is_file():
                raise AssertionError(f"нет файла ({path})")
        cls.skill = SKILL_MD.read_text(encoding="utf-8")
        cls.body = body_of(cls.skill)
        cls.doc = COLLECT_DOC.read_text(encoding="utf-8")

    # ── SKILL.md ──────────────────────────────────────────────────────────────

    def test_skill_description_unchanged(self) -> None:
        self.assertEqual(frontmatter(SKILL_MD)["description"], SKILL_DESCRIPTION)

    def test_skill_is_not_a_stub(self) -> None:
        self.assertNotIn("Заглушка", self.body)
        self.assertGreater(len(self.body.splitlines()), 25)

    def test_skill_runs_collect_with_plugin_root_and_fallback(self) -> None:
        self.assertIn(f'python3 "{PLUGIN_ROOT_VAR}/scripts/sr.py" collect', self.body)
        self.assertIn("два уровня выше", self.body)

    def test_skill_explains_orig_slug_setup_and_hook(self) -> None:
        for needle in ("*.orig", "слаг", "/sessionretro:setup", "hook.enabled", ".sessionretro/logs/"):
            with self.subTest(needle=needle):
                self.assertIn(needle, self.body)

    def test_skill_points_to_user_page_and_links_instructions(self) -> None:
        # Страница — путём в обратных кавычках: гейт no-internals запрещает ссылки вверх на папку docs.
        self.assertIn("`docs/ru/collect.md`", self.body)
        self.assertTrue(COLLECT_DOC.is_file())
        targets = {t for _, t in relative_links(self.body)}
        self.assertIn("../../references/collector-instructions/README.md", targets)

    def test_skill_explains_replace_and_other_session(self) -> None:
        for needle in ("--replace", "другая сессия", "сбор уже идёт", "sanitize.names"):
            with self.subTest(needle=needle):
                self.assertIn(needle, self.body)

    # ── docs/ru/collect.md ────────────────────────────────────────────────────

    def test_doc_replace_lock_commit_and_reclean(self) -> None:
        for needle in ("--replace", "300 секунд", "сбор уже идёт; повтор запрошен", "collect.again",
                       "сбор логов уже идёт (collect.lock)", "другая сессия",
                       ".sessionretro/logs/<ваш id>/", "не коммитьте", "без логина", "перечист"):
            with self.subTest(needle=needle):
                self.assertIn(needle, self.doc)
        self.assertNotIn("не стоит коммитить в общий репозиторий", self.doc)

    def test_doc_shows_commands_with_examples(self) -> None:
        for cmd in ("collect", "inventory", "normalize"):
            with self.subTest(cmd=cmd):
                self.assertIn(f'scripts/sr.py" {cmd}', self.doc)
        for flag in ("--json", "--quiet", "--provider"):
            with self.subTest(flag=flag):
                self.assertIn(flag, self.doc)

    def test_doc_covers_sources_hook_and_config(self) -> None:
        for needle in ("`claude-jsonl`", "`specstory`", "`dir`", "`git`", "SessionEnd",
                       "hook.enabled", "hook.providers", "collect.log", "sanitize.names",
                       "extra_patterns", "*.orig", ".sessionretro/logs/"):
            with self.subTest(needle=needle):
                self.assertIn(needle, self.doc)

    def test_doc_names_every_inventory_state(self) -> None:
        for state in inventory.STATES:
            with self.subTest(state=state):
                self.assertIn(f"`{state}`", self.doc)

    def test_doc_names_every_sanitizer_rule(self) -> None:
        rules = [name for name, _, _ in sanitize.RULES] + [sanitize.NAMES_KEY, sanitize.EXTRA_KEY,
                                                              sanitize.LOCAL_USER_KEY]
        for rule in rules:
            with self.subTest(rule=rule):
                self.assertIn(f"`{rule}`", self.doc)

    def test_doc_links_specstory_config_and_says_windows_unsupported(self) -> None:
        targets = {t for _, t in relative_links(self.doc)}
        self.assertIn("../../references/collector-instructions/README.md", targets)
        self.assertIn("../../references/collector-instructions/specstory-config.toml", targets)
        self.assertIn("Windows", self.doc)
        self.assertIn("не поддерживается", self.doc)

    # ── общее ─────────────────────────────────────────────────────────────────

    def test_pages_clean_and_links_inside_plugin(self) -> None:
        for path in (SKILL_MD, COLLECT_DOC):
            text = path.read_text(encoding="utf-8")
            with self.subTest(file=path.name):
                self.assertEqual(forbidden_hits(text), [])
                self.assertEqual(bad_emails(text), [])
                self.assertEqual(broken_or_outside_links(path, PLUGIN), [])


if __name__ == "__main__":
    unittest.main()
