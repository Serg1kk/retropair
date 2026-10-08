"""Фикстуры движка оценки: схема трекера, выходы судьи и пачка по настоящим схемам, индекс логов, хелперы `_assess`."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

import _util  # noqa: F401 — кладёт scripts/ в sys.path
import _assess
from _util import PLUGIN, TESTS

from lib import people, schema

ASSESS = TESTS / "fixtures" / "assess"
AXES = [
    "verification", "task_framing", "ownership", "self_correction",
    "context_engineering", "decomposition", "trust_calibration", "reversibility",
]
# поля записи в порядке `inventory.merge`; у `grown` после `state` ещё `review_from_turn`
INDEX_FIELDS = [
    "person_id", "source", "session_id", "key", "path", "source_path", "format", "sha", "bytes",
    "turns_total", "user_turns", "log_started_at", "log_ended_at", "started_at_source", "collector",
    "models", "period_guess", "period_guess_source", "metrics", "sanitize", "reviewed",
    "reviewed_in_run", "reviewed_at", "reviewed_turns_total", "reviewed_bytes", "state",
]
RUN_KEYS = [
    "run_id", "workspace", "period", "period_confidence", "person_id", "reviewed_at", "reviewer",
    "collector", "batches", "sessions_total", "tracker_version", "sessions_reviewed",
    "sessions_skipped_known", "agent_stack", "metrics_available", "context_report_available",
    "warnings", "turns_total", "axes", "antipatterns", "flags", "gating_triggered",
    "gating_evidence", "task_solved", "summary", "recommendations", "prev_recommendations",
    "feedback_draft", "feedback_parts", "revision", "operator_override", "logs_index_marks",
    "retracted", "level_judge", "level_computed", "review_status", "approved_at", "artifact", "source",
    "card_suggestions",
]
# необязательные поля прогона: отзыв, время approve (A16: момент выдачи рекомендаций при гейте)
# и предложения правок карточки от судьи (есть, только если судья их дал)
OPTIONAL_RUN_KEYS = {"retracted", "approved_at", "card_suggestions"}
AXIS_FIELDS = {"id", "score", "insufficient_evidence", "insufficient_evidence_reason", "evidence",
               "subcriteria", "note"}


def read(rel: str):
    return json.loads((ASSESS / rel).read_text(encoding="utf-8"))


def sample_run(**patch) -> dict:
    """Прогон трекера, собранный руками из выхода судьи p1 (как его соберёт append)."""
    judge = read("judge-alex-p1.json")
    data, meta = judge["data"], judge["metadata"]
    run = {
        "run_id": "demo-team_alex_p1_20260910T120000Z",
        "workspace": "demo-team",
        "period": {"label": "p1", "from": "2026-09-02T09:00:00Z", "to": "2026-09-05T12:00:00Z",
                   "anchor": None},
        "period_confidence": "explicit",
        "person_id": "alex",
        "reviewed_at": "2026-09-10T12:00:00Z",
        "reviewer": f"log-judge@{meta['prompt_version']}",
        "collector": meta["collector_version"],
        "batches": meta["batches"],
        "sessions_total": meta["sessions_total"],
        "tracker_version": 2,
        "sessions_reviewed": data["session_scope"]["sessions"],
        "sessions_skipped_known": [],
        "agent_stack": data["session_scope"]["agent_stack"],
        "metrics_available": meta["metrics_available"],
        "context_report_available": meta["context_report_available"],
        "warnings": meta["warnings"],
        "turns_total": meta["turns_analyzed"],
        "axes": data["axes"],
        "antipatterns": [a for a in data["antipatterns"] if a["found"]],
        "flags": data["flags"],
        "gating_triggered": data["gating_triggered"],
        "gating_evidence": data["gating_evidence"],
        "task_solved": data["summary"]["task_solved"],
        "summary": data["summary"]["one_thing_to_change"],
        "recommendations": data["recommendations"],
        "prev_recommendations": data["prev_recommendations"],
        "feedback_draft": None,
        "feedback_parts": data["feedback_parts"],
        "revision": 1,
        "operator_override": None,
        "logs_index_marks": [],
        "level_judge": data["summary"]["level_judge"],
        "level_computed": None,
        "review_status": "draft",
        "artifact": None,
        "source": {"path": ".retropair/runs/pending/alex/judge.json", "sha256": "0" * 64},
    }
    run.update(patch)
    return run


def sample_tracker(*runs) -> dict:
    return {"tracker_version": 2, "workspace": "demo-team", "updated_at": None, "runs": list(runs)}


class RealSchemasTest(unittest.TestCase):
    """Движок и тесты работают с настоящими схемами и шаблонами: временных копий в фикстурах нет."""

    def test_tracker_schema_in_supported_subset(self) -> None:
        self.assertEqual(schema.unsupported_keywords(schema.load_schema("tracker")), [])

    def test_no_temporary_copies_in_fixtures(self) -> None:
        self.assertFalse((ASSESS / "schemas").exists())
        self.assertFalse((ASSESS / "templates").exists())

    def test_schemas_resolve_to_plugin(self) -> None:
        for name in ("collector", "judge"):
            with self.subTest(schema=name):
                self.assertEqual(schema.schema_path(name), PLUGIN / "references" / "schemas" / f"{name}.json")
                self.assertTrue(schema.schema_path(name).is_file())

    def test_helper_templates_are_plugin_templates(self) -> None:
        self.assertEqual(_assess.TEMPLATE_RU, PLUGIN / "templates" / "feedback.ru.md")
        self.assertEqual(_assess.TEMPLATE_EN, PLUGIN / "templates" / "feedback.en.md")
        self.assertFalse(hasattr(_assess, "use_fixture_schemas"))


class JudgeFixturesTest(unittest.TestCase):
    def test_judge_outputs_valid(self) -> None:
        for name in ("judge-alex-p1.json", "judge-alex-p2.json"):
            with self.subTest(fixture=name):
                self.assertEqual(schema.validate(read(name), "judge"), [])

    def test_judge_has_eight_axes_in_order_and_criteria_in_order(self) -> None:
        for name in ("judge-alex-p1.json", "judge-alex-p2.json"):
            data = read(name)["data"]
            with self.subTest(fixture=name):
                self.assertEqual([a["id"] for a in data["axes"]], AXES)
                self.assertEqual([c["axis"] for c in data["feedback_parts"]["criteria"]], AXES)
                self.assertIsNone(data["artifact"])
                self.assertNotIn("feedback_draft", data)

    def test_scored_axes_have_quotes(self) -> None:
        for name in ("judge-alex-p1.json", "judge-alex-p2.json"):
            for axis in read(name)["data"]["axes"]:
                with self.subTest(fixture=name, axis=axis["id"]):
                    if axis["insufficient_evidence"]:
                        self.assertIsNone(axis["score"])
                    else:
                        self.assertIsInstance(axis["score"], int)
                        self.assertTrue(all(e["session_id"] and e["quote"] for e in axis["evidence"]))
                        self.assertTrue(axis["evidence"])

    def test_p2_answers_p1_recommendations(self) -> None:
        p1_ids = {r["id"] for r in read("judge-alex-p1.json")["data"]["recommendations"]}
        p2 = read("judge-alex-p2.json")["data"]
        prev_ids = {r["id"] for r in p2["prev_recommendations"]}
        self.assertTrue(prev_ids)
        self.assertLessEqual(prev_ids, p1_ids)
        for rec in p2["recommendations"]:
            self.assertNotIn(rec["id"], p1_ids)
            self.assertRegex(rec["id"], r"^REC-alex-\d{3}$")

    def test_judge_sessions_exist_in_index(self) -> None:
        index = read("logs-index.json")
        alex_ids = {e["session_id"] for e in index["logs"] if e["person_id"] == "alex"}
        for name in ("judge-alex-p1.json", "judge-alex-p2.json"):
            for sess in read(name)["data"]["session_scope"]["sessions"]:
                with self.subTest(fixture=name, session=sess["session_id"]):
                    self.assertIn(sess["session_id"], alex_ids)

    def test_schema_catches_broken_feedback_parts(self) -> None:
        cases = {
            "criteria 7": lambda d: d["feedback_parts"]["criteria"].pop(),
            "actions 4": lambda d: d["feedback_parts"]["actions"].extend(
                [d["feedback_parts"]["actions"][0]] * 3),
            "levers 3": lambda d: d["feedback_parts"]["actions"][0].__setitem__("levers", ["a", "b", "c"]),
            "verdict": lambda d: d["feedback_parts"]["criteria"][0].__setitem__("verdict", "great"),
            "artifact": lambda d: d.__setitem__("artifact", {"score": 1}),
            "level": lambda d: d["summary"].__setitem__("level_judge", "wizard"),
            "quote no session": lambda d: d["axes"][0]["evidence"][0].pop("session_id"),
        }
        for label, breaker in cases.items():
            with self.subTest(case=label):
                obj = read("judge-alex-p1.json")
                breaker(obj["data"])
                self.assertNotEqual(schema.validate(obj, "judge"), [])

    def test_levers_match_best_practices_per_axis(self) -> None:
        for name in ("judge-alex-p1.json", "judge-alex-p2.json"):
            data = read(name)["data"]
            with self.subTest(fixture=name):
                levers: dict[str, int] = {}
                for rec, action in zip(data["recommendations"], data["feedback_parts"]["actions"]):
                    levers[rec["axis"]] = levers.get(rec["axis"], 0) + len(action["levers"])
                practices: dict[str, int] = {}
                for item in data.get("best_practices") or []:
                    practices[item["axis"]] = practices.get(item["axis"], 0) + len(item["practices"])
                self.assertEqual({k: v for k, v in levers.items() if v}, practices)

    def test_fields_carry_no_intro_words(self) -> None:
        for name in ("judge-alex-p1.json", "judge-alex-p2.json"):
            for action in read(name)["data"]["feedback_parts"]["actions"]:
                with self.subTest(fixture=name, place=action["place"]):
                    self.assertFalse(action["try"].lower().startswith("стоит попробовать"))
                    self.assertTrue(action["place"][:1].islower())


class CollectorFixtureTest(unittest.TestCase):
    def test_batch_ok_valid(self) -> None:
        self.assertEqual(schema.validate(read("batch-ok.json"), "collector"), [])
        self.assertEqual(set(read("batch-ok.json")["batch"]), {"index", "total", "person_id", "workspace", "period"})

    def test_batch_ok_covers_all_axes(self) -> None:
        batch = read("batch-ok.json")
        seen = {o["axis"] for o in batch["observations"]}
        unobserved = {u["axis"] for u in batch["axes_unobserved"]}
        self.assertEqual(seen | unobserved, set(AXES))
        self.assertEqual(seen & unobserved, set())

    def test_collector_schema_rejects_score(self) -> None:
        obj = read("batch-ok.json")
        obj["observations"][0]["score"] = 3
        self.assertTrue(any("score" in p for p in schema.validate(obj, "collector")))


class IndexFixtureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.index = read("logs-index.json")
        self.logs = self.index["logs"]

    def test_top_level_shape(self) -> None:
        self.assertEqual(list(self.index), ["schema_version", "workspace", "updated_at", "logs", "warnings"])
        self.assertEqual(self.index["schema_version"], 1)
        self.assertEqual(self.index["workspace"], "demo-team")

    def test_every_entry_has_exact_fields(self) -> None:
        for entry in self.logs:
            with self.subTest(key=entry["key"]):
                grown = ["review_from_turn"] if entry["state"] == "grown" else []
                self.assertEqual(list(entry), INDEX_FIELDS + grown)
                self.assertEqual(entry["path"], f"logs/{entry['person_id']}/{entry['key']}.md")
                self.assertFalse(entry["reviewed"] and entry.get("excluded"))

    def test_alex_states(self) -> None:
        alex = [e for e in self.logs if e["person_id"] == "alex"]
        states = sorted(e["state"] for e in alex if e["user_turns"] >= 4)
        self.assertEqual(states, ["grown", "new", "new"])
        grown = next(e for e in alex if e["state"] == "grown")
        self.assertEqual(grown["reviewed_turns_total"], 10)
        self.assertEqual(grown["review_from_turn"], 10)
        self.assertTrue(grown["reviewed"])
        self.assertTrue(any(e["user_turns"] == 2 for e in alex))

    def test_sam_states(self) -> None:
        sam = [e for e in self.logs if e["person_id"] == "sam"]
        self.assertEqual(sorted(e["state"] for e in sam), ["gone", "new", "new"])

    def test_alex_and_sam_share_path_tail(self) -> None:
        tails = {}
        for entry in self.logs:
            tails.setdefault(Path(entry["path"]).name, set()).add(entry["person_id"])
        self.assertIn({"alex", "sam"}, tails.values())

    def test_session_ids_unique(self) -> None:
        ids = [e["session_id"] for e in self.logs]
        self.assertEqual(len(ids), len(set(ids)))


class TrackerSchemaTest(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = schema.load_schema("tracker")
        self.run_schema = self.schema["properties"]["runs"]["items"]

    def test_top_level(self) -> None:
        self.assertEqual(self.schema["properties"]["tracker_version"], {"const": 2})
        self.assertEqual(set(self.schema["required"]), {"tracker_version", "workspace", "updated_at", "runs"})
        self.assertIs(self.schema["additionalProperties"], False)

    def test_run_keys_and_required(self) -> None:
        self.assertEqual(list(self.run_schema["properties"]), RUN_KEYS)
        self.assertEqual(set(self.run_schema["required"]), set(RUN_KEYS) - OPTIONAL_RUN_KEYS)
        self.assertIs(self.run_schema["additionalProperties"], False)

    def test_closed_nested_objects(self) -> None:
        props = self.run_schema["properties"]
        for name in ("period", "operator_override", "source"):
            with self.subTest(field=name):
                self.assertIs(props[name]["additionalProperties"], False)
        axis = props["axes"]["items"]
        self.assertIs(axis["additionalProperties"], False)
        self.assertEqual(set(axis["properties"]), AXIS_FIELDS)
        evidence = axis["properties"]["evidence"]["items"]
        self.assertEqual(set(evidence["properties"]), {"session_id", "turn_index", "quote", "reading"})

    def test_sample_run_valid(self) -> None:
        self.assertEqual(schema.validate(sample_tracker(sample_run()), "tracker"), [])

    def test_override_and_retracted_valid(self) -> None:
        run = sample_run(
            operator_override={"axes": {"verification": 3}, "why": "видно в логе", "by": "reviewer",
                               "at": "2026-09-11T09:00:00Z"},
            retracted={"at": "2026-09-12T09:00:00Z", "why": "повтор", "logs_unmarked": 2},
            level_computed="competent", review_status="approved",
        )
        self.assertEqual(schema.validate(sample_tracker(run), "tracker"), [])

    def test_schema_catches_broken_runs(self) -> None:
        cases = {
            "extra key": {"overall_level": "competent"},
            "override form": {"operator_override": {"score": 3, "why": "x", "by": "r", "at": "t"}},
            "override score": {"operator_override": {"axes": {"verification": 7}, "why": "x", "by": "r",
                                                     "at": "t"}},
            "status": {"review_status": "sent"},
            "confidence": {"period_confidence": "guess"},
            "artifact": {"artifact": {"x": 1}},
            "level": {"level_computed": "wizard"},
            "tracker_version": {"tracker_version": 1},
            "source extra": {"source": {"path": "p", "sha256": "0", "extra": 1}},
        }
        for label, patch in cases.items():
            with self.subTest(case=label):
                self.assertNotEqual(schema.validate(sample_tracker(sample_run(**patch)), "tracker"), [])

    def test_missing_run_key_caught(self) -> None:
        run = sample_run()
        del run["level_computed"]
        self.assertNotEqual(schema.validate(sample_tracker(run), "tracker"), [])

    def test_axis_extra_field_caught(self) -> None:
        run = sample_run()
        run["axes"] = copy.deepcopy(run["axes"])
        run["axes"][0]["weight"] = 2
        self.assertNotEqual(schema.validate(sample_tracker(run), "tracker"), [])


class HelpersTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="sr-assess-")
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_make_ws(self) -> None:
        root = _assess.make_ws(self.tmp)
        state = root / ".retropair"
        self.assertTrue((state / "config.json").is_file())
        self.assertEqual(json.loads((state / "logs-index.json").read_text(encoding="utf-8")),
                         read("logs-index.json"))
        kim = people.load_person(root, "kim")
        self.assertEqual(kim["group"], "team-a")
        self.assertEqual(people.load_person(root, "alex")["id"], "alex")

    def test_payload_is_deep_copy_with_patch(self) -> None:
        obj = _assess.payload(gating_triggered=True)
        self.assertTrue(obj["data"]["gating_triggered"])
        obj["data"]["axes"][0]["score"] = 99
        fresh = _assess.payload()
        self.assertFalse(fresh["data"]["gating_triggered"])
        self.assertNotEqual(fresh["data"]["axes"][0]["score"], 99)
        self.assertEqual(_assess.payload("judge-alex-p2.json"), read("judge-alex-p2.json"))

    def test_payload_can_add_optional_data_field(self) -> None:
        self.assertEqual(_assess.payload(feedback_draft="текст")["data"]["feedback_draft"], "текст")


class NoPrivatePathsTest(unittest.TestCase):
    def test_fixtures_have_no_user_paths(self) -> None:
        private_home = "/" + "Users" + "/"  # склейка: сам тест не должен содержать запрещённый путь
        files = [p for p in ASSESS.rglob("*") if p.is_file()] + [TESTS / "_assess.py"]
        self.assertGreater(len(files), 4)
        for path in files:
            with self.subTest(file=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn(private_home, text)
                self.assertNotIn("/tmp/", text)


if __name__ == "__main__":
    unittest.main()
