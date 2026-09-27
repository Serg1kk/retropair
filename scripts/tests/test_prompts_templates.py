"""Шаблоны текста адресату, болванки проектных агентов, страница `docs/ru/axes.md`.

Шаблон `templates/feedback.<lang>.md`: комментарий-шапка (упоминает режим `short`), затем тело
ровно с пятью плейсхолдерами в порядке prefix → opening → actions → criteria → closing, каждый
один раз. Болванка `templates/agents/<имя>.md`: frontmatter построчно равен frontmatter агента
плагина, кроме строк name/model/effort; тело байт в байт равно телу агента. Страница осей
называет восемь осей человеческими именами из i18n и ссылается только на свои страницы.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

import _util  # noqa: F401 — кладёт scripts/ в sys.path
from _util import PLUGIN
from test_docs import relative_links
from test_manifest import AGENTS, AXES, frontmatter
from test_prompts_agents import body

TEMPLATES = PLUGIN / "templates"
FEEDBACK_TEMPLATES = {"ru": TEMPLATES / "feedback.ru.md", "en": TEMPLATES / "feedback.en.md"}
AGENT_STUBS = TEMPLATES / "agents"
AXES_PAGE = PLUGIN / "docs" / "ru" / "axes.md"
DOCS_RU = PLUGIN / "docs" / "ru"
I18N = PLUGIN / "references" / "i18n.json"

PLACEHOLDER_ORDER = ["prefix", "opening", "actions", "criteria", "closing"]
PLACEHOLDER = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")
HEADER_START = "<!--"
HEADER_END = "-->"
CYRILLIC = re.compile("[Ѐ-ӿ]")
RU_OPENERS = [
    "**Если читать быстро",
    "**Дальше по восьми "
    "критериям",
]

STUB_OVERRIDES = {"name", "model", "effort"}
PAGE_WORDS = ["гейт", "уровень",
              "мало данных"]


def ph(name: str) -> str:
    """Плейсхолдер `name` в двойных фигурных скобках (склейкой, чтобы не держать литерал в исходнике)."""
    return "{" * 2 + name + "}" * 2


def placeholders(text: str) -> list[str]:
    """Имена плейсхолдеров в порядке появления."""
    return PLACEHOLDER.findall(text)


def template_problems(text: str) -> list[str]:
    """Нарушения формы шаблона текста; пустой список — шаблон годен."""
    problems: list[str] = []
    if not text.startswith(HEADER_START) or HEADER_END not in text:
        return ["нет комментария-шапки в начале файла"]
    end = text.index(HEADER_END)
    header, rest = text[:end], text[end + len(HEADER_END):]
    if "short" not in header:
        problems.append("шапка не описывает режим short")
    names = placeholders(text)
    extra = sorted(set(names) - set(PLACEHOLDER_ORDER))
    missing = [n for n in PLACEHOLDER_ORDER if n not in names]
    if extra:
        problems.append(f"лишние плейсхолдеры: {', '.join(extra)}")
    if missing:
        problems.append(f"нет плейсхолдеров: {', '.join(missing)}")
    body_names = placeholders(rest)
    if not extra and not missing and body_names != PLACEHOLDER_ORDER:
        problems.append(f"тело: плейсхолдеры {body_names}, ожидается по разу в порядке {PLACEHOLDER_ORDER}")
    return problems


def _frontmatter_lines(text: str, label: str) -> list[str]:
    if not text.startswith("---\n"):
        raise AssertionError(f"frontmatter не начинается с '---' ({label})")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise AssertionError(f"frontmatter не закрыт ({label})")
    return text[4:end].split("\n")


def stub_problems(agent_text: str, stub_text: str, name: str) -> list[str]:
    """Расхождения болванки с агентом плагина `name`; пустой список — болванка годна."""
    expected = {"name": f"name: sessionretro-{name}", "model": "model: " + ph("model"),
                "effort": "effort: " + ph("effort")}
    agent_lines = _frontmatter_lines(agent_text, f"agents/{name}.md")
    stub_lines = _frontmatter_lines(stub_text, f"templates/agents/{name}.md")
    problems: list[str] = []
    if len(agent_lines) != len(stub_lines):
        problems.append(f"frontmatter: строк {len(stub_lines)}, у агента {len(agent_lines)}")
    for i, (a_line, s_line) in enumerate(zip(agent_lines, stub_lines), start=2):
        key = a_line.partition(":")[0].strip()
        want = expected[key] if key in STUB_OVERRIDES else a_line
        if s_line != want:
            problems.append(f"frontmatter строка {i}: {s_line!r}, ожидается {want!r}")
    if body(stub_text) != body(agent_text):
        problems.append("тело не равно телу агента")
    return problems


def axes_page_problems(text: str, axis_names: list[str], page_dir: Path, docs_dir: Path) -> list[str]:
    """Пробелы страницы осей: имена осей, ключевые слова, коды осей, ссылки вне своих страниц."""
    problems = [f"нет оси «{n}»" for n in axis_names if n not in text]
    lower = text.lower()
    problems += [f"нет слова «{w}»" for w in PAGE_WORDS if w not in lower]
    problems += [f"код оси {a} в тексте" for a in AXES if re.search(rf"\b{a}\b", text)]
    docs = docs_dir.resolve()
    for lineno, target in relative_links(text):
        resolved = (page_dir / target).resolve()
        if resolved.parent != docs or not resolved.is_file():
            problems.append(f"строка {lineno}: ссылка {target} не на существующую страницу docs/ru")
    return problems


def ru_axis_names() -> list[str]:
    data = json.loads(I18N.read_text(encoding="utf-8"))
    return [data["axes"][a]["ru"] for a in AXES]


def good_template() -> str:
    return (
        "<!--\nшапка: short\n-->\n"
        + "\n\n".join(ph(n) for n in PLACEHOLDER_ORDER) + "\n"
    )


class TemplatesSynthetic(unittest.TestCase):
    """Проверка шаблона на синтетике в памяти."""

    def test_good_template_passes(self) -> None:
        self.assertEqual(template_problems(good_template()), [])

    def test_extra_placeholder_caught(self) -> None:
        text = good_template() + ph("period") + "\n"
        self.assertEqual(template_problems(text), ["лишние плейсхолдеры: period"])

    def test_missing_placeholder_caught(self) -> None:
        text = good_template().replace(ph("closing"), "")
        self.assertEqual(template_problems(text), ["нет плейсхолдеров: closing"])

    def test_spaces_inside_braces_count(self) -> None:
        text = good_template() + "{" * 2 + " score " + "}" * 2
        self.assertEqual(template_problems(text), ["лишние плейсхолдеры: score"])

    def test_wrong_order_and_duplicate_caught(self) -> None:
        swapped = good_template().replace(ph("actions"), "X").replace(ph("criteria"), ph("actions"))
        swapped = swapped.replace("X", ph("criteria"))
        self.assertEqual(len(template_problems(swapped)), 1)
        self.assertIn("тело", template_problems(swapped)[0])
        dup = good_template() + ph("opening") + "\n"
        self.assertIn("тело", template_problems(dup)[0])

    def test_placeholder_only_in_header_caught(self) -> None:
        text = "<!--\nshort " + ph("closing") + "\n-->\n" + "\n".join(ph(n) for n in PLACEHOLDER_ORDER[:-1])
        self.assertIn("тело", template_problems(text)[0])

    def test_header_required_and_mentions_short(self) -> None:
        no_header = "\n".join(ph(n) for n in PLACEHOLDER_ORDER)
        self.assertEqual(template_problems(no_header), ["нет комментария-шапки в начале файла"])
        no_short = good_template().replace("short", "long")
        self.assertEqual(template_problems(no_short), ["шапка не описывает режим short"])


AGENT_SAMPLE = (
    "---\nname: log-collector\ndescription: \"x: y\"\ntools: Read\nmodel: sonnet\neffort: medium\n---\n"
    "\n# Тело\n\nversion: v1\n"
)


def stub_of(agent_text: str, name: str = "log-collector") -> str:
    lines = agent_text.split("\n")
    out = []
    for line in lines:
        key = line.partition(":")[0]
        if key == "name" and line.startswith("name: "):
            line = f"name: sessionretro-{name}"
        elif line.startswith("model: "):
            line = "model: " + ph("model")
        elif line.startswith("effort: "):
            line = "effort: " + ph("effort")
        out.append(line)
    return "\n".join(out)


class StubsSynthetic(unittest.TestCase):
    """Проверка болванки на синтетике."""

    def test_good_stub_passes(self) -> None:
        self.assertEqual(stub_problems(AGENT_SAMPLE, stub_of(AGENT_SAMPLE), "log-collector"), [])

    def test_body_difference_caught(self) -> None:
        stub = stub_of(AGENT_SAMPLE) + " "
        self.assertEqual(stub_problems(AGENT_SAMPLE, stub, "log-collector"), ["тело не равно телу агента"])

    def test_changed_frontmatter_line_caught(self) -> None:
        stub = stub_of(AGENT_SAMPLE).replace("tools: Read", "tools: Read, Bash")
        self.assertEqual(len(stub_problems(AGENT_SAMPLE, stub, "log-collector")), 1)

    def test_override_values_checked(self) -> None:
        for bad in ("name: log-collector", "model: sonnet", "effort: medium"):
            key = bad.partition(":")[0]
            with self.subTest(line=bad):
                stub = re.sub(rf"^{key}: .*$", bad, stub_of(AGENT_SAMPLE), count=1, flags=re.M)
                self.assertEqual(len(stub_problems(AGENT_SAMPLE, stub, "log-collector")), 1)

    def test_missing_line_caught(self) -> None:
        stub = stub_of(AGENT_SAMPLE).replace("tools: Read\n", "")
        self.assertTrue(stub_problems(AGENT_SAMPLE, stub, "log-collector"))


class AxesPageSynthetic(unittest.TestCase):
    """Проверка страницы осей на синтетике."""

    NAMES = ["Ось один", "Ось два"]

    def good(self) -> str:
        return "# Оси\n\nОсь один и Ось два. Гейт, уровень, мало данных. [Workspace](workspace.md)\n"

    def test_good_page_passes(self) -> None:
        self.assertEqual(axes_page_problems(self.good(), self.NAMES, DOCS_RU, DOCS_RU), [])

    def test_missing_axis_and_word_caught(self) -> None:
        text = self.good().replace("Ось два", "").replace("Гейт", "")
        self.assertEqual(axes_page_problems(text, self.NAMES, DOCS_RU, DOCS_RU),
                         ["нет оси «Ось два»", "нет слова «гейт»"])

    def test_axis_code_caught(self) -> None:
        text = self.good() + "Про `verification` подробнее.\n"
        self.assertEqual(axes_page_problems(text, self.NAMES, DOCS_RU, DOCS_RU), ["код оси verification в тексте"])

    def test_links_outside_own_pages_caught(self) -> None:
        text = self.good() + "[a](../../README.md) [b](missing.md)\n"
        found = axes_page_problems(text, self.NAMES, DOCS_RU, DOCS_RU)
        self.assertEqual(len(found), 2)


class TemplatesReal(unittest.TestCase):
    """Реальные файлы плагина."""

    def test_feedback_templates(self) -> None:
        for lang, path in FEEDBACK_TEMPLATES.items():
            with self.subTest(lang=lang):
                self.assertTrue(path.is_file(), f"нет шаблона ({path})")
                text = path.read_text(encoding="utf-8")
                self.assertEqual(template_problems(text), [])
                self.assertEqual(set(placeholders(text)), set(PLACEHOLDER_ORDER))

    def test_template_language(self) -> None:
        ru = FEEDBACK_TEMPLATES["ru"].read_text(encoding="utf-8")
        for opener in RU_OPENERS:
            self.assertIn(opener, ru)
        en = FEEDBACK_TEMPLATES["en"].read_text(encoding="utf-8")
        self.assertIsNone(CYRILLIC.search(en), "в английском шаблоне кириллица")

    def test_agent_stubs(self) -> None:
        self.assertEqual({p.stem for p in AGENT_STUBS.glob("*.md")}, set(AGENTS))
        for name in AGENTS:
            with self.subTest(agent=name):
                agent = (PLUGIN / "agents" / f"{name}.md").read_text(encoding="utf-8")
                stub_path = AGENT_STUBS / f"{name}.md"
                self.assertEqual(stub_problems(agent, stub_path.read_text(encoding="utf-8"), name), [])
                fm = frontmatter(stub_path)
                self.assertEqual(fm["name"], f"sessionretro-{name}")
                self.assertEqual(fm["model"], ph("model"))
                self.assertEqual(fm["effort"], ph("effort"))

    def test_axes_page(self) -> None:
        self.assertTrue(AXES_PAGE.is_file(), f"нет страницы ({AXES_PAGE})")
        text = AXES_PAGE.read_text(encoding="utf-8")
        self.assertEqual(axes_page_problems(text, ru_axis_names(), AXES_PAGE.parent, DOCS_RU), [])


if __name__ == "__main__":
    unittest.main()
