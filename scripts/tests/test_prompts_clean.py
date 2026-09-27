"""Чистота промптов, референсов и шаблонов: ни следов исходного контекста, ни внутренней нумерации.

Проверяются файлы `.md` и `.json` в папках `CLEAN_DIRS`. Слова и строки, которые ищет
проверка, собраны в исходнике эскейпами и склейкой, чтобы гейт чистоты не находил их
в самом этом файле.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

import _util  # noqa: F401 — кладёт scripts/ в sys.path
from _util import PLUGIN

# слова собраны эскейпами/склейкой, чтобы гейт no-personal не видел их в этом файле
_W = [
    "\u0441\u0442\u0443\u0434\u0435\u043d\u0442",
    "\u043c\u043e\u0434\u0443\u043b",
    "\u043a\u0443\u0440\u0441",
    "\u043e\u043f\u0435\u0440\u0430\u0442\u043e\u0440",
    "\u0421\u0435\u0440\u0433\u0435\u0439",
    "H" "SS",
    "AI" " Courses",
    "review" "-homework",
    r"\.claude/skills",
    "/" "Users/",
]
FORBIDDEN = re.compile("(?i)" + "|".join(_W))

# копия INTERNAL_NUMBERING из гейта чистоты: номер фичи, Task N, q-exec-N, D-N, маркер фичи
INTERNAL = re.compile(
    "(?i:\u0444\u0438\u0447[\u0430\u0435\u0438\u0443\u044b]\u0439? \\d)"
    r"|\bTask \d|q-exec-\d|\bD-\d+\b|<!-- /?feature-\d+ -->"
)

# ссылки на внутренние папки репозитория разработки (копия NO_INTERNALS гейта)
INTERNAL_LINKS = re.compile(
    r"\.\./docs/|\bdocs/(backlog|spec|strategy|vision|roadmap|brief|plan)"
    "|reference" r"-implementation/|(?<![\w-])intern" r"al/|knowledge" r"-base/|session" r"-history/"
)

PLACEHOLDER = re.compile(r"\{\{[^{}]*\}\}")

CLEAN_DIRS = ["agents", "references", "templates", "skills/review/references", "docs/ru/axes.md"]
SUFFIXES = (".md", ".json")

# Агент-заглушка каркаса: тело из одной строки о будущей реализации. Такие файлы
# заменяются полным промптом; пока они заглушки, чистота по ним не проверяется, и это
# видно отдельным пропущенным тестом.
STUB_BODY = re.compile("^\u0417\u0430\u0433\u043b\u0443\u0448\u043a\u0430: .*\\.$")


def _files(root: Path, rel_paths) -> list[Path]:
    """Развернуть относительные пути (файлы и папки) в список файлов .md/.json."""
    out: list[Path] = []
    for rel in rel_paths:
        path = root / rel
        if path.is_file():
            out.append(path)
        elif path.is_dir():
            out += sorted(p for p in path.rglob("*") if p.is_file() and p.suffix in SUFFIXES)
        else:
            raise FileNotFoundError(f"sessionretro: проверка чистоты: нет пути ({path})")
    return [p for p in out if p.suffix in SUFFIXES]


def _in_templates(rel: str) -> bool:
    return "templates" in rel.split("/")[:-1]


def clean_hits(root, rel_paths) -> list[str]:
    """Список «путь:строка: причина» по файлам .md/.json; плейсхолдеры разрешены только под templates/."""
    root = Path(root)
    hits: list[str] = []
    for path in _files(root, rel_paths):
        rel = path.relative_to(root).as_posix()
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            reasons = []
            if FORBIDDEN.search(line):
                reasons.append("запрещённое слово или путь")
            if INTERNAL.search(line):
                reasons.append("внутренняя нумерация")
            if INTERNAL_LINKS.search(line):
                reasons.append("ссылка на внутреннюю папку")
            if not _in_templates(rel) and PLACEHOLDER.search(line):
                reasons.append("плейсхолдер вне templates/")
            if reasons:
                hits.append(f"{rel}:{lineno}: {', '.join(reasons)}")
    return hits


def is_stub_agent(path: Path) -> bool:
    """Файл агента, у которого тело после frontmatter — одна строка-заглушка."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return False
    parts = text.split("\n---\n", 1)
    if len(parts) != 2:
        return False
    body = [line for line in parts[1].splitlines() if line.strip()]
    return len(body) == 1 and bool(STUB_BODY.match(body[0].strip()))


def real_targets(root: Path) -> list[str]:
    """Файлы CLEAN_DIRS плагина без агентов-заглушек, относительными путями."""
    rels = [p for p in CLEAN_DIRS if (root / p).exists()]
    files = _files(root, rels)
    return [p.relative_to(root).as_posix() for p in files if not is_stub_agent(p)]


class CleanSynthetic(unittest.TestCase):
    """Каждая ветка проверки на синтетической папке."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="sr-clean-")
        self.root = Path(self._tmp.name)
        for sub in ("references", "templates", "agents"):
            (self.root / sub).mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, rel: str, text: str) -> None:
        (self.root / rel).write_text(text, encoding="utf-8")

    def test_clean_file_has_no_hits(self) -> None:
        self.write("references/a.md", "# Оси\nучастник, период, ревьюер\n")
        self.assertEqual(clean_hits(self.root, ["references"]), [])

    def test_forbidden_word_caught(self) -> None:
        for word in _W[:5] + ["H" "SS", "AI" " Courses", "review" "-homework"]:
            with self.subTest(word=word):
                self.write("references/a.md", f"строка\nтут {word.upper()} есть\n")
                self.assertEqual(clean_hits(self.root, ["references"]),
                                 ["references/a.md:2: запрещённое слово или путь"])

    def test_forbidden_path_caught(self) -> None:
        for text in ["/" "Users/someone/x", "см. ." "claude/skills/x"]:
            with self.subTest(text=text):
                self.write("references/a.json", '{"d": "' + text + '"}\n')
                self.assertEqual(clean_hits(self.root, ["references"]),
                                 ["references/a.json:1: запрещённое слово или путь"])

    def test_internal_numbering_caught(self) -> None:
        samples = ["\u0444\u0438\u0447\u0430 3", "\u0444\u0438\u0447\u0435\u0439 4", "Task " "1",
                   "q-exec-" "2", "D-" "13", "<!-- feature-" "3 -->"]
        for text in samples:
            with self.subTest(text=text):
                self.write("agents/a.md", f"x {text} y\n")
                self.assertEqual(clean_hits(self.root, ["agents"]),
                                 ["agents/a.md:1: внутренняя нумерация"])

    def test_internal_link_caught(self) -> None:
        for text in ["../" "docs/x.md", "docs/" "backlog/x", "reference" "-implementation/a",
                     "internal" "/b", "knowledge" "-base/c", "session" "-history/d"]:
            with self.subTest(text=text):
                self.write("references/a.md", f"см. {text}\n")
                self.assertEqual(clean_hits(self.root, ["references"]),
                                 ["references/a.md:1: ссылка на внутреннюю папку"])

    def test_placeholder_outside_templates_caught(self) -> None:
        self.write("references/a.md", "привет, " "{{" "name" "}}\n")
        self.assertEqual(clean_hits(self.root, ["references"]),
                         ["references/a.md:1: плейсхолдер вне templates/"])

    def test_placeholder_in_templates_allowed(self) -> None:
        self.write("templates/feedback.ru.md", "{{" "opening" "}}\n{{" "actions" "}}\n")
        self.assertEqual(clean_hits(self.root, ["templates"]), [])

    def test_only_md_and_json_scanned(self) -> None:
        self.write("references/a.py", "x = '/" "Users/'\n")
        self.write("references/b.md", "чисто\n")
        self.assertEqual(clean_hits(self.root, ["references"]), [])

    def test_single_file_path_accepted(self) -> None:
        self.write("references/a.md", "D-" "7\n")
        self.assertEqual(clean_hits(self.root, ["references/a.md"]),
                         ["references/a.md:1: внутренняя нумерация"])

    def test_missing_path_raises(self) -> None:
        with self.assertRaises(FileNotFoundError) as ctx:
            clean_hits(self.root, ["nope"])
        self.assertIn("nope", str(ctx.exception))

    def test_stub_agent_detected(self) -> None:
        self.write("agents/stub.md", "---\nname: x\n---\n\n\u0417\u0430\u0433\u043b\u0443\u0448\u043a\u0430: "
                   "\u0431\u0443\u0434\u0435\u0442 \u043f\u043e\u0437\u0436\u0435.\n")
        self.write("agents/full.md", "---\nname: y\n---\n\n# Роль\n\n"
                   "\u0417\u0430\u0433\u043b\u0443\u0448\u043a\u0430: \u043d\u0435\u0442.\n")
        self.assertTrue(is_stub_agent(self.root / "agents" / "stub.md"))
        self.assertFalse(is_stub_agent(self.root / "agents" / "full.md"))


class CleanReal(unittest.TestCase):
    """Реальные файлы плагина."""

    def test_plugin_prompt_dirs_clean(self) -> None:
        self.assertEqual(clean_hits(PLUGIN, real_targets(PLUGIN)), [])

    def test_agent_stubs_replaced(self) -> None:
        stubs = sorted(p.name for p in (PLUGIN / "agents").glob("*.md") if is_stub_agent(p))
        self.assertEqual(stubs, [], "агент-заглушка выпадает из проверки чистоты (real_targets)")


if __name__ == "__main__":
    unittest.main()
