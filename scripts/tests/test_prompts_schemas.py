"""Схемы выхода агентов `collector.json` и `judge.json`, их образцы и порченые копии.

Коллектор оценок не ставит: полей оценки у него нет ни на какой глубине, каждый объект
закрыт (`additionalProperties: false` или словарь-схема не-объекта). У судьи — форма
`feedback_parts`, `artifact: null`, `level_judge` вместо итогового уровня.
"""

from __future__ import annotations

import copy
import json
import re
import unittest

import _util  # noqa: F401 — кладёт scripts/ в sys.path
from _util import FIXTURES, PLUGIN

from lib.schema import load_schema, unsupported_keywords, validate

SAMPLES = FIXTURES / "prompts"
I18N = PLUGIN / "references" / "i18n.json"

# идентификаторы исходного контекста, которых в схемах быть не должно (склейкой)
BANNED_IDS = re.compile("(?i)" + "|".join(["stu" "dent", "mod" "ule", "str" "eam", "cou" "rse"]))
SCORE_KEYS = {"score", "axes", "overall_level", "feedback_draft"}
VERDICTS = ["even", "even_but_one", "first_place_above", "second_place_above",
            "third_place_above", "not_visible"]


def _types(schema: dict) -> list[str]:
    value = schema.get("type")
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


def subschemas(schema: dict, path: str = "$"):
    """Все под-схемы с путём: через properties, items и additionalProperties-словарь."""
    yield path, schema
    for name, sub in (schema.get("properties") or {}).items():
        if isinstance(sub, dict):
            yield from subschemas(sub, f"{path}.{name}")
    for key in ("items", "additionalProperties"):
        sub = schema.get(key)
        if isinstance(sub, dict):
            yield from subschemas(sub, f"{path}.{key}")


def score_key_hits(schema: dict) -> list[str]:
    """Пути, где ключ в properties в точности равен полю оценки."""
    return [f"{path}.{name}" for path, sub in subschemas(schema)
            for name in (sub.get("properties") or {}) if name in SCORE_KEYS]


def open_object_hits(schema: dict) -> list[str]:
    """Объекты, не закрытые `additionalProperties: false` или словарём-схемой не-объекта."""
    hits = []
    for path, sub in subschemas(schema):
        if "object" not in _types(sub):
            continue
        extra = sub.get("additionalProperties", "нет")
        if extra is False:
            continue
        if isinstance(extra, dict):
            if "object" in _types(extra):
                hits.append(f"{path}: additionalProperties — схема объекта")
            continue
        hits.append(f"{path}: additionalProperties = {extra!r}")
    return hits


def banned_name_hits(schema: dict) -> list[str]:
    """Ключи properties и строковые значения enum с идентификаторами исходного контекста."""
    hits = []
    for path, sub in subschemas(schema):
        for name in sub.get("properties") or {}:
            if BANNED_IDS.search(name):
                hits.append(f"{path}.{name}")
        for value in sub.get("enum") or []:
            if isinstance(value, str) and BANNED_IDS.search(value):
                hits.append(f"{path}.enum: {value}")
    return hits


QUOTE_LIMIT = 400
PLACE_INDEX = {"first_place_above": 0, "second_place_above": 1, "third_place_above": 2}


def unlimited_quotes(schema: dict) -> list[str]:
    """Поля `quote` на любой глубине без `maxLength: 400`."""
    return [f"{path}.quote" for path, sub in subschemas(schema)
            for name, prop in (sub.get("properties") or {}).items()
            if name == "quote" and isinstance(prop, dict) and prop.get("maxLength") != QUOTE_LIMIT]


def judge_invariant_problems(sample: dict) -> list[str]:
    """Условия «Проверок скриптом», которых схема не выражает, на выходе судьи."""
    data = sample["data"]
    axes = data["axes"]
    parts = data["feedback_parts"]
    actions = parts["actions"]
    problems = []
    for axis in axes:
        if (axis["score"] is None) != bool(axis["insufficient_evidence"]):
            problems.append(f"{axis['id']}: score {axis['score']!r} при "
                            f"insufficient_evidence {axis['insufficient_evidence']!r}")
    axis_order = [a["id"] for a in axes]
    criteria_order = [c["axis"] for c in parts["criteria"]]
    if criteria_order != axis_order:
        problems.append(f"порядок criteria {criteria_order} != порядок axes {axis_order}")
    if len(actions) != len(data["recommendations"]):
        problems.append(f"actions {len(actions)} != recommendations {len(data['recommendations'])}")
    referenced = [PLACE_INDEX[c["verdict"]] for c in parts["criteria"] if c["verdict"] in PLACE_INDEX]
    for index in referenced:
        if index >= len(actions):
            problems.append(f"вердикт на пункт {index + 1}, а пунктов {len(actions)}")
    for index in range(len(actions)):
        if referenced.count(index) != 1:
            problems.append(f"на пункт {index + 1} ссылается критериев: {referenced.count(index)}")
    scored = sum(1 for a in axes if a["score"] is not None)
    summary = data["summary"]
    if scored < 3:
        for key in ("strongest_axis", "weakest_axis"):
            if summary[key] is not None:
                problems.append(f"{key} = {summary[key]!r} при {scored} оценённых осях")
    return problems


def _sample(name: str) -> dict:
    return json.loads((SAMPLES / f"{name}.sample.json").read_text(encoding="utf-8"))


def _i18n_keys(section: str) -> list[str]:
    data = json.loads(I18N.read_text(encoding="utf-8"))
    return [k for k in data[section] if not k.startswith("_")]


class HelpersSynthetic(unittest.TestCase):
    """Обходы схем на синтетике в памяти."""

    def test_score_key_found_at_depth_exact_match_only(self) -> None:
        schema = {"type": "object", "properties": {
            "axes_unobserved": {"type": "array", "items": {"type": "object", "properties": {
                "score": {"type": "integer"}, "axis_signal_sessions": {"type": "string"}}}}}}
        self.assertEqual(score_key_hits(schema), ["$.axes_unobserved.items.score"])

    def test_score_key_under_additional_properties_found(self) -> None:
        schema = {"type": "object", "additionalProperties": {
            "type": "object", "properties": {"overall_level": {"type": "string"}}}}
        self.assertEqual(score_key_hits(schema), ["$.additionalProperties.overall_level"])

    def test_open_object_missing_true_and_object_map_caught(self) -> None:
        schema = {"type": "object", "additionalProperties": False, "properties": {
            "a": {"type": ["object", "null"]},
            "b": {"type": "object", "additionalProperties": True},
            "c": {"type": "object", "additionalProperties": {"type": "object"}},
            "d": {"type": "object", "additionalProperties": {"type": "integer"}},
            "e": {"type": "array", "items": {"type": "object", "additionalProperties": False}},
        }}
        self.assertEqual(open_object_hits(schema), [
            "$.a: additionalProperties = 'нет'",
            "$.b: additionalProperties = True",
            "$.c: additionalProperties — схема объекта",
            "$.c.additionalProperties: additionalProperties = 'нет'",
        ])

    def test_banned_names_in_keys_and_enums_caught(self) -> None:
        schema = {"type": "object", "properties": {
            "person_id": {"type": "string"},
            "stu" "dent_slug": {"type": "string"},
            "status": {"type": "string", "enum": ["other_" "mod" "ule", "other_period", None]}}}
        self.assertEqual(banned_name_hits(schema), [
            "$.stu" "dent_slug", "$.status.enum: other_" "mod" "ule"])


    def test_unlimited_quote_found_at_depth(self) -> None:
        schema = {"type": "object", "properties": {
            "a": {"type": "array", "items": {"type": "object", "properties": {
                "quote": {"type": "string"}}}},
            "b": {"type": "object", "properties": {"quote": {"type": "string", "maxLength": 400}}},
            "c": {"type": "object", "properties": {"quote": {"type": "string", "maxLength": 500}}}}}
        self.assertEqual(unlimited_quotes(schema), ["$.a.items.quote", "$.c.quote"])


class BothSchemas(unittest.TestCase):
    def test_only_supported_keywords(self) -> None:
        for name in ("collector", "judge"):
            with self.subTest(schema=name):
                self.assertEqual(unsupported_keywords(load_schema(name)), [])

    def test_no_source_identifiers(self) -> None:
        for name in ("collector", "judge"):
            with self.subTest(schema=name):
                self.assertEqual(banned_name_hits(load_schema(name)), [])

    def test_schema_versions(self) -> None:
        self.assertEqual(load_schema("collector")["properties"]["schema_version"], {"const": 2})
        self.assertEqual(load_schema("judge")["properties"]["schema_version"], {"const": 3})


class CollectorSchema(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = load_schema("collector")

    def test_no_score_fields_at_any_depth(self) -> None:
        self.assertEqual(score_key_hits(self.schema), [])

    def test_every_object_closed(self) -> None:
        self.assertEqual(open_object_hits(self.schema), [])

    def test_batch_identity_and_period_shape(self) -> None:
        batch = self.schema["properties"]["batch"]
        self.assertTrue({"person_id", "workspace", "period", "index", "total"} <= set(batch["required"]))
        self.assertNotIn("of", batch["properties"])
        period = batch["properties"]["period"]
        self.assertEqual(set(period["properties"]), {"label", "from", "to", "anchor"})
        self.assertEqual(period["required"], ["label"])
        self.assertEqual(period["properties"]["label"]["type"], "string")
        for key in ("from", "to", "anchor"):
            with self.subTest(key=key):
                self.assertEqual(period["properties"][key]["type"], ["string", "null"])

    def test_every_quote_limited(self) -> None:
        self.assertEqual(unlimited_quotes(self.schema), [])

    def test_session_status_has_agent_subtranscript(self) -> None:
        status = self.schema["properties"]["sessions"]["items"]["properties"]["status"]
        self.assertIn("agent_subtranscript", status["enum"])
        self.assertIn("read", status["enum"])


class JudgeSchema(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = load_schema("judge")
        cls.data = cls.schema["properties"]["data"]
        cls.parts = cls.data["properties"]["feedback_parts"]

    def test_every_quote_limited(self) -> None:
        self.assertEqual(unlimited_quotes(self.schema), [])

    def test_artifact_is_null_and_required(self) -> None:
        self.assertEqual(self.data["properties"]["artifact"]["type"], "null")
        self.assertIn("artifact", self.data["required"])

    def test_feedback_parts_required_draft_optional(self) -> None:
        self.assertIn("feedback_parts", self.data["required"])
        self.assertNotIn("feedback_draft", self.data["required"])
        self.assertEqual(self.data["properties"]["feedback_draft"]["type"], "string")

    def test_feedback_parts_top(self) -> None:
        self.assertEqual(self.parts["required"], ["opening", "actions", "criteria", "closing"])
        self.assertIs(self.parts["additionalProperties"], False)

    def test_actions_shape(self) -> None:
        actions = self.parts["properties"]["actions"]
        self.assertEqual((actions["minItems"], actions["maxItems"]), (1, 3))
        item = actions["items"]
        self.assertTrue({"place", "date", "quote", "cost", "try", "levers"} <= set(item["required"]))
        self.assertIs(item["additionalProperties"], False)
        self.assertEqual(item["properties"]["levers"]["maxItems"], 2)
        self.assertEqual(item["properties"]["quote"]["maxLength"], 400)

    def test_criteria_shape(self) -> None:
        criteria = self.parts["properties"]["criteria"]
        self.assertEqual((criteria["minItems"], criteria["maxItems"]), (8, 8))
        props = criteria["items"]["properties"]
        self.assertEqual(props["axis"]["enum"], _i18n_keys("axes"))
        self.assertEqual(len(props["axis"]["enum"]), 8)
        self.assertEqual(props["verdict"]["enum"], VERDICTS)
        self.assertEqual(props["evidence"]["maxLength"], 400)

    def test_level_judge_seven_levels_no_overall_level(self) -> None:
        summary = self.data["properties"]["summary"]
        enum = summary["properties"]["level_judge"]["enum"]
        self.assertEqual(len(enum), 7)
        self.assertIn("insufficient", enum)
        self.assertEqual(set(enum), set(_i18n_keys("levels")))
        self.assertIn("level_judge", summary["required"])
        self.assertEqual([path for path, sub in subschemas(self.schema)
                          if "overall_level" in (sub.get("properties") or {})], [])


class Samples(unittest.TestCase):
    """Образцы валидны, порченые копии ловятся."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.collector = _sample("collector")
        cls.judge = _sample("judge")

    def assertInvalid(self, obj, name: str, expected: str) -> None:
        problems = validate(obj, name)
        self.assertTrue(any(expected in p for p in problems),
                        f"порченая копия: нет проблемы с {expected!r}, есть {problems}")

    def test_samples_valid(self) -> None:
        self.assertEqual(validate(self.collector, "collector"), [])
        self.assertEqual(validate(self.judge, "judge"), [])

    def test_collector_extra_key_rejected(self) -> None:
        bad = copy.deepcopy(self.collector)
        bad["batch"]["extra"] = 1
        self.assertInvalid(bad, "collector", "лишнее поле 'extra'")

    def test_collector_score_in_observation_rejected(self) -> None:
        bad = copy.deepcopy(self.collector)
        self.assertTrue(bad["observations"], "в образце нет наблюдений")
        bad["observations"][0]["score"] = 3
        self.assertInvalid(bad, "collector", "лишнее поле 'score'")

    def test_judge_extra_key_rejected(self) -> None:
        bad = copy.deepcopy(self.judge)
        bad["data"]["feedback_parts"]["extra"] = "x"
        self.assertInvalid(bad, "judge", "лишнее поле 'extra'")

    def test_judge_seven_criteria_rejected(self) -> None:
        bad = copy.deepcopy(self.judge)
        bad["data"]["feedback_parts"]["criteria"].pop()
        self.assertInvalid(bad, "judge", "элементов 7")

    def test_judge_four_actions_rejected(self) -> None:
        bad = copy.deepcopy(self.judge)
        actions = bad["data"]["feedback_parts"]["actions"]
        while len(actions) < 4:
            actions.append(copy.deepcopy(actions[0]))
        self.assertInvalid(bad, "judge", "элементов 4")

    def test_judge_three_levers_rejected(self) -> None:
        bad = copy.deepcopy(self.judge)
        bad["data"]["feedback_parts"]["actions"][0]["levers"] = ["a", "b", "c"]
        self.assertInvalid(bad, "judge", "элементов 3")

    def test_collector_quote_401_rejected(self) -> None:
        bad = copy.deepcopy(self.collector)
        self.assertTrue(bad["observations"], "в образце нет наблюдений")
        bad["observations"][0]["quote"] = "я" * 401
        self.assertInvalid(bad, "collector", "строка длиннее 400 знаков")

    def test_collector_quote_400_accepted(self) -> None:
        ok = copy.deepcopy(self.collector)
        ok["observations"][0]["quote"] = "я" * 400
        self.assertEqual(validate(ok, "collector"), [])

    def test_judge_artifact_not_null_rejected(self) -> None:
        bad = copy.deepcopy(self.judge)
        bad["data"]["artifact"] = {"score": 5}
        self.assertInvalid(bad, "judge", "$.data.artifact: тип dict")


class JudgeSampleInvariants(unittest.TestCase):
    """Образец судьи держит условия «Проверок скриптом»; порченые копии их нарушают."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.judge = _sample("judge")

    def _bad(self):
        return copy.deepcopy(self.judge)

    def assertCaught(self, bad: dict, expected: str) -> None:
        problems = judge_invariant_problems(bad)
        self.assertTrue(any(expected in p for p in problems),
                        f"порченая копия: нет проблемы с {expected!r}, есть {problems}")

    def test_sample_holds_invariants(self) -> None:
        self.assertEqual(judge_invariant_problems(self.judge), [])

    def test_opening_is_coverage_line(self) -> None:
        opening = self.judge["data"]["feedback_parts"]["opening"]
        self.assertRegex(opening, "(?i)лог")

    def test_score_with_insufficient_caught(self) -> None:
        bad = self._bad()
        bad["data"]["axes"][0]["insufficient_evidence"] = True
        self.assertCaught(bad, "verification: score 1")

    def test_null_score_without_insufficient_caught(self) -> None:
        bad = self._bad()
        bad["data"]["axes"][2]["insufficient_evidence"] = False
        self.assertCaught(bad, "ownership: score None")

    def test_criteria_order_caught(self) -> None:
        bad = self._bad()
        criteria = bad["data"]["feedback_parts"]["criteria"]
        criteria[0], criteria[1] = criteria[1], criteria[0]
        self.assertCaught(bad, "порядок criteria")

    def test_actions_recommendations_mismatch_caught(self) -> None:
        bad = self._bad()
        bad["data"]["recommendations"].append(dict(bad["data"]["recommendations"][0], id="REC-x-002"))
        self.assertCaught(bad, "actions 1 != recommendations 2")

    def test_place_verdict_without_action_caught(self) -> None:
        bad = self._bad()
        bad["data"]["feedback_parts"]["criteria"][1]["verdict"] = "second_place_above"
        self.assertCaught(bad, "вердикт на пункт 2, а пунктов 1")

    def test_action_without_place_verdict_caught(self) -> None:
        bad = self._bad()
        bad["data"]["feedback_parts"]["criteria"][0]["verdict"] = "even_but_one"
        self.assertCaught(bad, "на пункт 1 ссылается критериев: 0")

    def test_strongest_with_two_scored_axes_caught(self) -> None:
        bad = self._bad()
        bad["data"]["summary"]["strongest_axis"] = "task_framing"
        self.assertCaught(bad, "strongest_axis = 'task_framing' при 2 оценённых осях")

    def test_three_scored_axes_allow_strongest(self) -> None:
        ok = self._bad()
        ok["data"]["axes"][2].update(score=3, insufficient_evidence=False,
                                     insufficient_evidence_reason=None)
        ok["data"]["summary"]["strongest_axis"] = "task_framing"
        self.assertEqual(judge_invariant_problems(ok), [])


if __name__ == "__main__":
    unittest.main()
