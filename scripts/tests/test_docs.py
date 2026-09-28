"""Документация плагина: страница workspace, заполненные каркасы, живые относительные ссылки."""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

import _util  # noqa: F401 — кладёт scripts/ в sys.path
from _util import PLUGIN

WORKSPACE_DOC = PLUGIN / "docs" / "ru" / "workspace.md"
COMMANDS = ["init", "people", "doctor"]
PEOPLE_ACTIONS = ["add", "show", "list", "context"]
FEEDBACK_FIELDS = [
    "address", "length", "tone", "opening_summary", "mark_sections", "practices",
    "skip_basics", "boundaries", "avoid", "value_focus", "show_dynamics",
    "channel", "template", "prefix",
]
FILLED_DOCS = ["STATE.md", "MAINTAINERS.md", "CONTRIBUTING.md"]
SKELETON_PHRASE = "Заполняется в этой фиче"
SKIP_DIRS = {"__pycache__", ".git", "node_modules"}

FENCE_RE = re.compile(r"^\s*(```|~~~)")
INLINE_CODE_RE = re.compile(r"`[^`]*`")
LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
EXTERNAL_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|//|#)", re.I)


def markdown_files(root: Path) -> list[Path]:
    """Все `*.md` под `root`, кроме служебных папок."""
    return sorted(
        path for path in Path(root).rglob("*.md")
        if not SKIP_DIRS.intersection(part for part in path.relative_to(root).parts)
    )


def relative_links(text: str) -> list[tuple[int, str]]:
    """Относительные ссылки markdown вне блоков кода и инлайн-кода: (строка, цель без #якоря)."""
    found = []
    in_fence = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for match in LINK_RE.finditer(INLINE_CODE_RE.sub("", line)):
            target = match.group(1)
            if EXTERNAL_RE.match(target):
                continue
            target = target.split("#", 1)[0]
            if target:
                found.append((lineno, target))
    return found


def broken_links(root: Path) -> list[str]:
    """Ссылки из `*.md` под `root`, которые не ведут на существующий файл или папку."""
    root = Path(root)
    broken = []
    for path in markdown_files(root):
        for lineno, target in relative_links(path.read_text(encoding="utf-8")):
            if not (path.parent / target).exists():
                broken.append(f"{path.relative_to(root)}:{lineno}: {target}")
    return broken


class LinkCheckerTest(unittest.TestCase):
    """Сам проверяльщик ссылок на синтетической папке."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="sr-docs-")
        self.root = Path(self._tmp.name)
        (self.root / "docs").mkdir()
        (self.root / "docs" / "page.md").write_text("# страница\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, text: str) -> None:
        (self.root / "README.md").write_text(text, encoding="utf-8")

    def test_existing_relative_link_ok(self) -> None:
        self.write("[стр](docs/page.md) и [якорь](docs/page.md#top) и [папка](docs/)\n")
        self.assertEqual(broken_links(self.root), [])

    def test_missing_relative_link_reported_with_line(self) -> None:
        self.write("текст\n[нет](docs/missing.md)\n")
        self.assertEqual(broken_links(self.root), ["README.md:2: docs/missing.md"])

    def test_external_and_anchor_links_ignored(self) -> None:
        self.write("[a](https://example.com/x) [b](mailto:a@example.com) [c](#local)\n")
        self.assertEqual(broken_links(self.root), [])

    def test_links_in_code_ignored(self) -> None:
        self.write("```\n[нет](missing.md)\n```\n`[нет](missing2.md)`\n")
        self.assertEqual(broken_links(self.root), [])

    def test_link_from_nested_page_resolves_against_its_folder(self) -> None:
        (self.root / "docs" / "other.md").write_text("[назад](../README.md) [нет](nope.md)\n",
                                                     encoding="utf-8")
        self.write("# корень\n")
        self.assertEqual(broken_links(self.root), ["docs/other.md:1: nope.md"])


class WorkspaceDocTest(unittest.TestCase):
    """Страница пользователя `docs/ru/workspace.md`."""

    @classmethod
    def setUpClass(cls) -> None:
        if not WORKSPACE_DOC.is_file():
            raise AssertionError(f"нет страницы пользователя ({WORKSPACE_DOC})")
        cls.text = WORKSPACE_DOC.read_text(encoding="utf-8")

    def test_mentions_every_command_with_example(self) -> None:
        for cmd in COMMANDS:
            with self.subTest(cmd=cmd):
                self.assertIn(f'scripts/sr.py" {cmd}', self.text)

    def test_mentions_every_people_action(self) -> None:
        for action in PEOPLE_ACTIONS:
            with self.subTest(action=action):
                self.assertIn(f'sr.py" people {action}', self.text)

    def test_explains_every_feedback_field(self) -> None:
        for field in FEEDBACK_FIELDS:
            with self.subTest(field=field):
                self.assertIn(f"`{field}`", self.text)

    def test_describes_both_state_folders(self) -> None:
        self.assertIn(".sessionretro/", self.text)
        self.assertIn("~/.sessionretro/", self.text)


class FilledDocsTest(unittest.TestCase):
    """Каркасы документов заполнены, TESTING.md описывает все файлы тестов."""

    def test_skeleton_docs_filled(self) -> None:
        for name in FILLED_DOCS:
            with self.subTest(doc=name):
                text = (PLUGIN / name).read_text(encoding="utf-8")
                self.assertNotIn(SKELETON_PHRASE, text)
                self.assertGreater(len(text.splitlines()), 20)

    def test_testing_lists_every_test_file_and_fixture(self) -> None:
        text = (PLUGIN / "TESTING.md").read_text(encoding="utf-8")
        self.assertIn("fixtures/workspace-min/", text)
        for path in sorted((PLUGIN / "scripts" / "tests").glob("test_*.py")):
            with self.subTest(test_file=path.name):
                self.assertIn(f"`{path.name}`", text)


class PluginLinksTest(unittest.TestCase):
    """Реальный артефакт: относительные ссылки во всех `*.md` плагина живые."""

    def test_relative_links_in_plugin_resolve(self) -> None:
        self.assertEqual(broken_links(PLUGIN), [])


# ── README EN/RU, индекс docs, CHANGELOG релиза ──────────────────────────────

SWITCHER = ('<p align="center"><a href="README.md">English</a> | '
            '<a href="README.ru.md">Русский</a></p>')
# Разделы `##` обоих README по порядку: правишь один — правишь второй тем же PR.
README_SECTIONS = [
    ("Quick start", "Быстрый старт"),
    ("What it does", "Что умеет"),
    ("How it works", "Как работает"),
    ("Three modes", "Три режима"),
    ("Setup", "Настройка"),
    ("Collecting session logs", "Сбор логов сессий"),
    ("Running a review", "Как запустить ревью"),
    ("Reports", "Отчёты"),
    ("Demo", "Демо"),
    ("Privacy", "Приватность"),
    ("Quality and cost", "Качество и стоимость"),
    ("Configuration", "Конфигурация"),
    ("How it differs", "Чем отличается"),
    ("Documentation", "Документация"),
    ("Roadmap", "Планы"),
    ("Contributing", "Как помочь"),
    ("License", "Лицензия"),
]
QUICK_START = [
    "/plugin marketplace add Serg1kk/sessionretro",
    "/plugin install sessionretro@sessionretro",
    "/sessionretro:setup",
]
REQUIRED_PAGES = [
    "docs/ru/privacy.md", "docs/ru/faq.md",
    "docs/en/how-it-works.md", "docs/en/privacy.md", "docs/en/customize.md",
]
HTML_ATTR_RE = re.compile(r"""(?:href|src)=["']([^"']+)["']""")


def h2_headings(text: str) -> list[str]:
    """Заголовки `## …` вне блоков кода, по порядку."""
    out, in_fence = [], False
    for line in text.splitlines():
        if FENCE_RE.match(line):
            in_fence = not in_fence
        elif not in_fence and line.startswith("## "):
            out.append(line[3:].strip())
    return out


def section(text: str, heading: str) -> str:
    """Раздел `## heading` до следующего `## ` (вне блоков кода); нет раздела — AssertionError."""
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == f"## {heading}"]
    if len(starts) != 1:
        raise AssertionError(f"раздел «## {heading}»: найдено {len(starts)}, ожидался один")
    out, in_fence = [], False
    for line in lines[starts[0] + 1:]:
        if FENCE_RE.match(line):
            in_fence = not in_fence
        elif not in_fence and line.startswith("## "):
            break
        out.append(line)
    return "\n".join(out) + "\n"


def first_code_block(text: str) -> list[str]:
    """Непустые строки первого блока кода в тексте; блока нет — AssertionError."""
    lines, block, in_fence = text.splitlines(), None, False
    for line in lines:
        if FENCE_RE.match(line):
            if in_fence:
                return [x.strip() for x in block if x.strip()]
            in_fence, block = True, []
        elif in_fence:
            block.append(line)
    raise AssertionError("блок кода не найден")


def html_links(text: str) -> list[tuple[int, str]]:
    """Относительные `href`/`src` в HTML-вставках markdown вне блоков кода: (строка, цель)."""
    found, in_fence = [], False
    for lineno, line in enumerate(text.splitlines(), start=1):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for match in HTML_ATTR_RE.finditer(INLINE_CODE_RE.sub("", line)):
            target = match.group(1)
            if not EXTERNAL_RE.match(target):
                target = target.split("#", 1)[0]
                if target:
                    found.append((lineno, target))
    return found


def links_outside(root: Path) -> list[str]:
    """Относительные ссылки (markdown и HTML) из `*.md` под `root`, которые не существуют или
    выходят за пределы `root`."""
    root = Path(root).resolve()
    bad = []
    for path in markdown_files(root):
        text = path.read_text(encoding="utf-8")
        for lineno, target in sorted(relative_links(text) + html_links(text)):
            dest = (path.parent / target).resolve()
            if not dest.exists() or not dest.is_relative_to(root):
                bad.append(f"{path.relative_to(root)}:{lineno}: {target}")
    return bad


class DocHelpersTest(unittest.TestCase):
    """Помощники проверок README на синтетике."""

    def test_h2_headings_skip_code(self) -> None:
        text = "# T\n## A\n```\n## не заголовок\n```\n### B\n## C\n"
        self.assertEqual(h2_headings(text), ["A", "C"])

    def test_section_until_next_h2(self) -> None:
        text = "## A\nx\n```\n## внутри\n```\n### sub\n## B\ny\n"
        self.assertEqual(section(text, "A"), "x\n```\n## внутри\n```\n### sub\n")
        with self.assertRaises(AssertionError):
            section(text, "Нет")

    def test_first_code_block(self) -> None:
        self.assertEqual(first_code_block("t\n```text\n a\n\nb\n```\n```\nc\n```\n"), ["a", "b"])
        with self.assertRaises(AssertionError):
            first_code_block("без кода\n")

    def test_links_outside_root_and_html(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sr-docs-") as tmp:
            outer = Path(tmp)
            root = outer / "plugin"
            (root / "img").mkdir(parents=True)
            (root / "img" / "a.png").write_bytes(b"x")
            (outer / "secret.md").write_text("x\n", encoding="utf-8")
            (root / "README.md").write_text(
                '<img src="img/a.png"> <a href="img/b.png">b</a>\n[вне](../secret.md)\n'
                '<a href="https://example.com">e</a>\n', encoding="utf-8")
            self.assertEqual(links_outside(root),
                             ["README.md:1: img/b.png", "README.md:2: ../secret.md"])


class ReadmePairTest(unittest.TestCase):
    """Реальный артефакт: README.md и README.ru.md — одна структура."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.en = (PLUGIN / "README.md").read_text(encoding="utf-8")
        cls.ru = (PLUGIN / "README.ru.md").read_text(encoding="utf-8")

    def test_switcher_first_line(self) -> None:
        for name, text in (("README.md", self.en), ("README.ru.md", self.ru)):
            with self.subTest(readme=name):
                self.assertEqual(text.splitlines()[0], SWITCHER)

    def test_same_sections_in_same_order(self) -> None:
        self.assertEqual(h2_headings(self.en), [en for en, _ in README_SECTIONS])
        self.assertEqual(h2_headings(self.ru), [ru for _, ru in README_SECTIONS])

    def test_quick_start_is_exactly_three_commands(self) -> None:
        for text, heading in ((self.en, README_SECTIONS[0][0]), (self.ru, README_SECTIONS[0][1])):
            with self.subTest(heading=heading):
                self.assertEqual(first_code_block(section(text, heading)), QUICK_START)

    def test_screenshots_per_language(self) -> None:
        cases = (("README.md", self.en, "docs/images/group.png", "docs/images/personal.png"),
                 ("README.ru.md", self.ru, "docs/images/group.ru.png", "docs/images/personal.ru.png"))
        for name, text, hero, card in cases:
            with self.subTest(readme=name):
                self.assertIn(hero, text.split("\n## ", 1)[0])
                blocks = re.findall(r"<details>.*?</details>", text, re.S)
                self.assertTrue(any(card in block for block in blocks), card)

    def test_how_it_works_gif_and_text_version(self) -> None:
        cases = ((self.en, README_SECTIONS[2][0], "docs/images/how-it-works.en.gif", "Text version"),
                 (self.ru, README_SECTIONS[2][1], "docs/images/how-it-works.ru.gif", "Текстовая версия"))
        for text, heading, gif, summary in cases:
            with self.subTest(heading=heading):
                body = section(text, heading)
                self.assertIn(gif, body)
                self.assertTrue((PLUGIN / gif).is_file(), gif)
                self.assertIn(f"<summary>{summary}</summary>", body)
                self.assertLess(body.index(gif), body.index("<details>"))

    def test_no_internal_eval_links(self) -> None:
        for path in markdown_files(PLUGIN):
            with self.subTest(page=str(path.relative_to(PLUGIN))):
                self.assertNotIn("eval/", path.read_text(encoding="utf-8"))


class DocsIndexTest(unittest.TestCase):
    """Реальный артефакт: страницы docs на месте, индекс перечисляет все, ссылки внутри плагина."""

    def test_required_pages_exist(self) -> None:
        for rel in REQUIRED_PAGES:
            with self.subTest(page=rel):
                self.assertTrue((PLUGIN / rel).is_file(), rel)

    def test_index_lists_every_page(self) -> None:
        index = PLUGIN / "docs" / "README.md"
        targets = {t for _, t in relative_links(index.read_text(encoding="utf-8"))}
        pages = sorted((PLUGIN / "docs").glob("ru/*.md")) + sorted((PLUGIN / "docs").glob("en/*.md"))
        self.assertTrue(pages)
        for page in pages:
            rel = page.relative_to(PLUGIN / "docs").as_posix()
            with self.subTest(page=rel):
                self.assertIn(rel, targets)

    def test_relative_links_stay_inside_plugin(self) -> None:
        self.assertEqual(links_outside(PLUGIN), [])


class ChangelogReleaseTest(unittest.TestCase):
    """Реальный артефакт: пустой Unreleased, под ним секция последнего релиза, секция 0.1.0 на месте."""

    def test_release_section_and_empty_unreleased(self) -> None:
        text = (PLUGIN / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn("\n## [0.1.0] — 2026-09-28\n", text)
        headings = h2_headings(text)
        self.assertEqual(headings[0], "[Unreleased]")
        self.assertRegex(headings[1], r"^\[\d+\.\d+\.\d+\] — \d{4}-\d{2}-\d{2}$")
        self.assertEqual(section(text, "[Unreleased]").strip(), "")


if __name__ == "__main__":
    unittest.main()
