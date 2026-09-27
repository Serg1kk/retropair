"""Полнота `i18n.json` против enum схем судьи и коллектора и против референсов.

Каждое значение перечислений (оси, уровни, вердикты, статусы, причины нехватки данных,
флаги, триггеры гейта) и каждый id антипаттерна из `antipatterns.md` имеет в i18n
непустые `ru` и `en`. Имена осей совпадают с заголовками `axes.md`, короткие имена
антипаттернов и флагов — с колонкой «Короткое имя». Тест читает только файлы плагина.
"""

from __future__ import annotations

import json
import re
import unittest

import _util  # noqa: F401 — кладёт scripts/ в sys.path
from _util import PLUGIN

from lib.schema import load_schema

I18N = PLUGIN / "references" / "i18n.json"
AXES_MD = PLUGIN / "references" / "axes.md"
ANTIPATTERNS_MD = PLUGIN / "references" / "antipatterns.md"

AXIS_HEADING = re.compile(r"^## \d\. `([a-z_]+)` — (.+)$", re.M)
CATALOG_ROW = re.compile(r"^\| `([a-z_]+)` \| ([^|]+?) \|", re.M)
FLAGS_HEADING = "## Флаги безопасности"


# секция i18n → [(схема, путь)]; путь идёт по properties, сегмент items — в items
ENUM_MAP = {
    "axes": [
        ("judge", "data.axes.items.id"),
        ("judge", "data.best_practices.items.axis"),
        ("judge", "data.feedback_parts.criteria.items.axis"),
        ("collector", "observations.items.axis"),
        ("collector", "axes_unobserved.items.axis"),
        ("collector", "subcriteria_unobserved.items.axis"),
    ],
    "levels": [("judge", "data.summary.level_judge")],
    "verdicts": [("judge", "data.feedback_parts.criteria.items.verdict")],
    "rec_statuses": [("judge", "data.recommendations.items.status")],
    "rec_verdicts": [("judge", "data.prev_recommendations.items.verdict")],
    "session_statuses": [
        ("judge", "data.session_scope.sessions.items.status"),
        ("collector", "sessions.items.status"),
    ],
    "insufficient_reasons": [
        ("judge", "data.axes.items.insufficient_evidence_reason"),
        ("collector", "axes_unobserved.items.reason"),
    ],
    "flags": [
        ("judge", "data.flags.items.id"),
        ("collector", "flag_candidates.items.id"),
    ],
    "gates": [
        ("judge", "data.gating_evidence.items.trigger"),
        ("collector", "gating_candidates.items.trigger"),
    ],
}
# служебные значения, которым подпись в UI не нужна: (секция, схема, значение)
SKIP_VALUES = {("session_statuses", "collector", "read")}
# секции, которые целиком выводятся из источника: лишний ключ — тоже ошибка
EXACT_SECTIONS = {"verdicts", "levels", "rec_statuses", "rec_verdicts",
                  "insufficient_reasons", "flags", "gates", "antipatterns", "axes"}


def walk(schema: dict, path: str) -> dict:
    """Под-схема по пути `a.b.items.c`; нет узла — AssertionError с путём."""
    node = schema
    done: list[str] = []
    for seg in path.split("."):
        done.append(seg)
        if seg == "items" and isinstance(node.get("items"), dict):
            node = node["items"]
            continue
        props = node.get("properties")
        if not isinstance(props, dict) or seg not in props:
            raise AssertionError(f"в схеме нет пути {'.'.join(done)} (искали {path})")
        node = props[seg]
    return node


def enum_values(schema: dict, path: str) -> list[str]:
    """Значения enum по пути без `None`; нет enum — AssertionError с путём."""
    node = walk(schema, path)
    values = node.get("enum")
    if not isinstance(values, list) or not values:
        raise AssertionError(f"по пути {path} нет enum")
    return [v for v in values if v is not None]


def i18n_key(section: str, value: str) -> str:
    return value.lower() if section == "gates" else value


def missing(section_name: str, section: dict, keys: list[str]) -> list[str]:
    """Проблемы секции: ключа нет, `ru` или `en` пусты. Пустой список — всё на месте."""
    problems = []
    for key in keys:
        entry = section.get(key)
        if not isinstance(entry, dict):
            problems.append(f"{section_name}.{key}: ключа нет")
            continue
        for lang in ("ru", "en"):
            text = entry.get(lang)
            if not isinstance(text, str) or not text.strip():
                problems.append(f"{section_name}.{key}.{lang}: пусто")
    return problems


def catalog(text: str) -> tuple[dict[str, str], dict[str, str]]:
    """(антипаттерны, флаги) из antipatterns.md: id → короткое имя, в порядке файла."""
    if FLAGS_HEADING not in text:
        raise AssertionError(f"в antipatterns.md нет заголовка {FLAGS_HEADING!r}")
    head, tail = text.split(FLAGS_HEADING, 1)
    anti = {m.group(1): m.group(2).strip() for m in CATALOG_ROW.finditer(head)}
    flags = {m.group(1): m.group(2).strip() for m in CATALOG_ROW.finditer(tail)}
    return anti, flags


def expected_keys(i18n: dict) -> dict[str, list[str]]:
    """Секция → ключи, которые обязаны быть в i18n, из схем и референсов."""
    schemas = {"judge": load_schema("judge"), "collector": load_schema("collector")}
    out: dict[str, list[str]] = {}
    for section, refs in ENUM_MAP.items():
        keys: list[str] = []
        for schema_name, path in refs:
            for value in enum_values(schemas[schema_name], path):
                if (section, schema_name, value) in SKIP_VALUES:
                    continue
                key = i18n_key(section, value)
                if key not in keys:
                    keys.append(key)
        out[section] = keys
    anti, _ = catalog(ANTIPATTERNS_MD.read_text(encoding="utf-8"))
    out["antipatterns"] = list(anti)
    return out


class SyntheticTest(unittest.TestCase):
    SCHEMA = {"properties": {"a": {"type": "array", "items": {"properties": {
        "b": {"type": ["string", "null"], "enum": ["x", "y", None]}}}}}}

    def test_walk_finds_enum_and_skips_none(self):
        self.assertEqual(enum_values(self.SCHEMA, "a.items.b"), ["x", "y"])

    def test_walk_missing_path_names_path(self):
        with self.assertRaisesRegex(AssertionError, r"a\.items\.c"):
            walk(self.SCHEMA, "a.items.c")

    def test_enum_absent_is_error(self):
        schema = {"properties": {"a": {"type": "string"}}}
        with self.assertRaisesRegex(AssertionError, "нет enum"):
            enum_values(schema, "a")

    def test_missing_key_caught(self):
        section = {"x": {"ru": "Икс", "en": "Ex"}}
        self.assertEqual(missing("s", section, ["x", "y"]), ["s.y: ключа нет"])

    def test_empty_en_caught(self):
        section = {"x": {"ru": "Икс", "en": " "}}
        self.assertEqual(missing("s", section, ["x"]), ["s.x.en: пусто"])

    def test_catalog_split_by_flags_heading(self):
        text = ("| `a_b` | Имя А | x |\n| `c` | Имя Ц | y |\n"
                + FLAGS_HEADING + "\n| `f` | Флаг | z |\n")
        self.assertEqual(catalog(text), ({"a_b": "Имя А", "c": "Имя Ц"}, {"f": "Флаг"}))

    def test_catalog_without_flags_heading_fails(self):
        with self.assertRaises(AssertionError):
            catalog("| `a` | А | x |\n")

    def test_gate_key_lowered(self):
        self.assertEqual(i18n_key("gates", "G1"), "g1")
        self.assertEqual(i18n_key("flags", "G1"), "G1")


class RealI18nTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.i18n = json.loads(I18N.read_text(encoding="utf-8"))
        cls.expected = expected_keys(cls.i18n)

    def test_every_enum_value_has_ru_and_en(self):
        problems = []
        for section, keys in self.expected.items():
            if section not in self.i18n:
                problems.append(f"{section}: секции нет")
                continue
            problems += missing(section, self.i18n[section], keys)
        self.assertEqual(problems, [])

    def test_derived_sections_have_no_extra_keys(self):
        for section in EXACT_SECTIONS:
            have = [k for k in self.i18n[section] if not k.startswith("_")]
            self.assertEqual(sorted(have), sorted(self.expected[section]), section)

    def test_axes_names_match_axes_md(self):
        headings = AXIS_HEADING.findall(AXES_MD.read_text(encoding="utf-8"))
        self.assertEqual(len(headings), 8)
        section = self.i18n["axes"]
        got = [(k, section[k]["ru"]) for k in section if not k.startswith("_")]
        self.assertEqual(got, headings)

    def test_catalog_short_names_are_ru(self):
        anti, flags = catalog(ANTIPATTERNS_MD.read_text(encoding="utf-8"))
        self.assertTrue(anti)
        self.assertEqual(len(flags), 7)
        for section, source in (("antipatterns", anti), ("flags", flags)):
            got = {k: v["ru"] for k, v in self.i18n[section].items() if not k.startswith("_")}
            self.assertEqual(got, source, section)
            order = [k for k in self.i18n[section] if not k.startswith("_")]
            self.assertEqual(order, list(source), f"{section}: порядок как в antipatterns.md")

    def test_ignored_same_ru_in_statuses_and_verdicts(self):
        self.assertEqual(self.i18n["rec_statuses"]["ignored"]["ru"],
                         self.i18n["rec_verdicts"]["ignored"]["ru"])

    def test_new_sections_between_session_and_review_statuses(self):
        order = [k for k in self.i18n if not k.startswith("_")]
        start = order.index("session_statuses")
        self.assertEqual(order[start + 1:start + 6], [
            "insufficient_reasons", "antipatterns", "flags", "gates", "review_statuses"])


if __name__ == "__main__":
    unittest.main()
