"""Тесты валидатора схем `lib/schema.py`, схем config/person и шаблонов."""

from __future__ import annotations

import json
import unittest

from _util import PLUGIN

from lib import schema, yamlmini

TEMPLATES = PLUGIN / "templates"
SCHEMAS = PLUGIN / "references" / "schemas"


def _config_from_template(mode: str = "self") -> dict:
    text = (TEMPLATES / "config.template.json").read_text(encoding="utf-8")
    for key, value in {"id": "myproj", "title": "My project", "mode": mode, "lang": "ru"}.items():
        text = text.replace("{{" + key + "}}", value)
    return json.loads(text)


def _person_from_template() -> dict:
    text = (TEMPLATES / "person.template.yaml").read_text(encoding="utf-8")
    subst = {"id": "alex", "name": '"Alex Doe"', "group": "default", "date": "2026-09-27"}
    for key, value in subst.items():
        text = text.replace("{{" + key + "}}", value)
    card = yamlmini.load(text)
    default = yamlmini.load((TEMPLATES / "_default.yaml").read_text(encoding="utf-8"))
    feedback = dict(default["feedback"])
    feedback.update(card.get("feedback") or {})
    for key in ("skip_basics", "boundaries", "avoid"):
        if feedback.get(key) is None:
            feedback[key] = []
    card["feedback"] = feedback
    if card.get("sources") is None:
        card["sources"] = []
    return card


class MiniValidateTest(unittest.TestCase):
    def test_type_mismatch(self):
        problems = schema.mini_validate("x", {"type": "integer"})
        self.assertEqual(problems, ["$: тип str, а нужен integer"])

    def test_bool_is_not_integer(self):
        self.assertTrue(schema.mini_validate(True, {"type": "integer"}))

    def test_required(self):
        problems = schema.mini_validate({}, {"type": "object", "required": ["id"]})
        self.assertEqual(problems, ["$: нет обязательного поля 'id'"])

    def test_enum(self):
        problems = schema.mini_validate({"m": "x"}, {"properties": {"m": {"enum": ["a", "b"]}}})
        self.assertEqual(problems, ["$.m: значение 'x' вне списка допустимых"])

    def test_additional_properties_false(self):
        s = {"type": "object", "properties": {"a": {}}, "additionalProperties": False}
        self.assertEqual(
            schema.mini_validate({"a": 1, "b": 2}, s),
            ["$: лишнее поле 'b', схема его не знает"],
        )

    def test_items(self):
        s = {"type": "array", "items": {"type": "string"}}
        self.assertEqual(schema.mini_validate(["a", 1], s), ["$[1]: тип int, а нужен string"])

    def test_pattern(self):
        s = {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]*$"}
        self.assertEqual(schema.mini_validate("ok-1", s), [])
        self.assertEqual(
            schema.mini_validate("Bad_Id", s),
            ["$: 'Bad_Id' не подходит под шаблон ^[a-z0-9][a-z0-9-]*$"],
        )

    def test_pattern_dollar_rejects_trailing_newline(self):
        s = {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]*$"}
        self.assertEqual(len(schema.mini_validate("bob\n", s)), 1)
        self.assertEqual(schema.mini_validate("bob", s), [])

    def test_pattern_without_anchor_is_search(self):
        self.assertEqual(schema.mini_validate("xaby", {"type": "string", "pattern": "ab"}), [])

    def test_min_length(self):
        s = {"type": "string", "minLength": 1}
        self.assertEqual(schema.mini_validate("", s), ["$: строка короче 1 знаков"])
        self.assertEqual(schema.mini_validate("a", s), [])

    def test_unknown_keyword_reported(self):
        problems = schema.mini_validate({}, {"oneOf": []})
        self.assertEqual(problems, ["$: ключевое слово 'oneOf' не поддержано"])

    def test_unknown_keyword_nested_reported(self):
        s = {"type": "object", "properties": {"x": {"type": "string", "format": "date"}}}
        self.assertEqual(
            schema.unsupported_keywords(s),
            ["$.properties.x: ключевое слово 'format' не поддержано"],
        )

    def test_annotations_allowed(self):
        s = {"$schema": "x", "$id": "y", "title": "t", "description": "d",
             "default": 1, "examples": [1], "type": "integer"}
        self.assertEqual(schema.mini_validate(1, s), [])


class PluginSchemasTest(unittest.TestCase):
    def test_load_schema_missing_file(self):
        with self.assertRaises(FileNotFoundError) as ctx:
            schema.load_schema("no-such-schema")
        self.assertIn("no-such-schema.json", str(ctx.exception))

    def test_schema_ids(self):
        for name in ("config", "person"):
            self.assertEqual(schema.load_schema(name)["$id"], f"retropair/{name}.json")

    def test_all_plugin_schemas_use_supported_keywords(self):
        paths = sorted(SCHEMAS.glob("*.json"))
        self.assertGreaterEqual(len(paths), 2)
        for path in paths:
            with self.subTest(schema=path.name):
                data = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(schema.unsupported_keywords(data), [])

    def test_config_template_valid(self):
        for mode in ("self", "team", "course"):
            with self.subTest(mode=mode):
                self.assertEqual(schema.validate(_config_from_template(mode), "config"), [])

    def test_bad_mode_rejected(self):
        cfg = _config_from_template()
        cfg["mode"] = "solo"
        self.assertEqual(
            schema.validate(cfg, "config"),
            ["$.mode: значение 'solo' вне списка допустимых"],
        )

    def test_config_bad_workspace_id_and_models(self):
        cfg = _config_from_template()
        cfg["workspace"]["id"] = "My Proj"
        cfg["models"]["judge"]["effort"] = "extreme"
        problems = schema.validate(cfg, "config")
        self.assertEqual(len(problems), 2, problems)
        self.assertTrue(problems[0].startswith("$.workspace.id:"))
        self.assertTrue(problems[1].startswith("$.models.judge.effort:"))

    def test_config_top_level_extra_allowed(self):
        cfg = _config_from_template()
        cfg["future_field"] = {"x": 1}
        self.assertEqual(schema.validate(cfg, "config"), [])

    def test_person_template_valid(self):
        card = _person_from_template()
        self.assertEqual(card["id"], "alex")
        self.assertEqual(card["name"], "Alex Doe")
        self.assertEqual(card["feedback"]["address"], "ты")
        self.assertEqual(schema.validate(card, "person"), [])

    def test_person_template_feedback_commented_out(self):
        text = (TEMPLATES / "person.template.yaml").read_text(encoding="utf-8")
        raw = yamlmini.load(text.replace("{{", "x").replace("}}", "x"))
        self.assertNotIn("feedback", raw)
        self.assertRegex(text, r"(?m)^#\s+address: ты")
        self.assertRegex(text, r"(?m)^# feedback:$")
        self.assertNotIn("[]", text)

    def test_default_yaml_only_feedback(self):
        default = yamlmini.load((TEMPLATES / "_default.yaml").read_text(encoding="utf-8"))
        self.assertEqual(list(default), ["feedback"])
        self.assertIsNone(default["feedback"]["skip_basics"])
        self.assertEqual(default["feedback"]["prefix"], "")

    def test_person_id_trailing_newline_rejected(self):
        card = _person_from_template()
        card["id"] = "bob\n"
        problems = schema.validate(card, "person")
        self.assertEqual(len(problems), 1, problems)
        self.assertTrue(problems[0].startswith("$.id:"), problems)

    def test_person_nested_blocks_closed_top_open(self):
        card = _person_from_template()
        card["feedback"]["adress"] = "вы"
        card["profile"]["rol"] = "dev"
        card["consent"]["log"] = True
        card["notes"] = "x"
        problems = schema.validate(card, "person")
        self.assertEqual(sorted(p.split(":")[0] for p in problems),
                         ["$.consent", "$.feedback", "$.profile"], problems)

    def test_person_bad_values_rejected(self):
        card = _person_from_template()
        card["id"] = "-bad"
        card["feedback"]["channel"] = "fax"
        card["profile"]["years"] = "nine"
        problems = schema.validate(card, "person")
        self.assertEqual(len(problems), 3, problems)


if __name__ == "__main__":
    unittest.main()
