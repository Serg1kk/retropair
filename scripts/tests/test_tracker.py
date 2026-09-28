"""`lib/tracker.py` (ядро) и команды `append`, `validate`: запись прогона, инварианты, пометки логов."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

import _util  # noqa: F401 — кладёт scripts/ в sys.path
from _assess import make_ws, payload
from _util import TempHome, run_sr

import sr
from lib import batches, marks, registry, render_review, report_data, schema, tracker
from lib.config import load_config, save_config
from lib.workspace import SrError, ws_paths

NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(days=7)
RUN_P1 = "demo-team_alex_p1_20260910T120000Z"
OLD_RUN = "demo-team_alex_run_20260901T180000Z"

LOGIN = "3f9a2c71-8d4e-4b6a-9c15-2e7f0a4b8d31"
FIX_ALEX = "b61d0e48-27c3-4f95-a8e1-6d0c93b2f574"
GROWN = "5c2e8f19-a4b7-4d20-8e63-f1b09c7d2a46"
SHORT = "e07a4d93-6f18-4c5b-b2d7-8a3e10f4c965"
KIM_RECS = [
    {"id": "REC-kim-001", "axis": "verification", "text": "Запускать тесты до приёмки.", "status": "open"},
    {"id": "REC-kim-002", "axis": "trust_calibration", "text": "Принимать правку по частям.", "status": "open"},
]
PROMPTS_SAMPLE = Path(__file__).resolve().parent / "fixtures" / "prompts" / "judge.sample.json"


def _entry(index: dict, session_id: str) -> dict:
    return next(e for e in index["logs"] if e["session_id"] == session_id)


class TrackerCase(TempHome, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="sr-tracker-")
        self.addCleanup(self._tmp.cleanup)
        self.root = make_ws(Path(self._tmp.name))
        self.paths = ws_paths(self.root)

    # --- хелперы ---------------------------------------------------------------

    def write(self, obj, person: str = "alex", name: str = "judge.json") -> Path:
        path = self.paths["pending"] / person / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def append(self, obj, person: str = "alex", now: datetime = NOW, **kw) -> dict:
        return tracker.append(self.root, self.write(obj, person), person, now=now, **kw)

    def append_p1(self, **kw) -> dict:
        return self.append(payload(), period_label="p1", **kw)

    def rejected(self, obj, code: int = 1, person: str = "alex", **kw) -> str:
        with self.assertRaises(SrError) as ctx:
            self.append(obj, person=person, **kw)
        self.assertEqual(ctx.exception.code, code, ctx.exception.message)
        return ctx.exception.message

    def cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sr.main(["--workspace", str(self.root), *args])
        return code, out.getvalue(), err.getvalue()

    def tracker_doc(self) -> dict:
        return json.loads(tracker.tracker_path(self.root).read_text(encoding="utf-8"))

    def save_doc(self, doc: dict) -> None:
        tracker.tracker_path(self.root).write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    def index(self) -> dict:
        return json.loads(self.paths["logs_index"].read_text(encoding="utf-8"))

    def p2(self, **patch) -> dict:
        return payload("judge-alex-p2.json", **patch)

    def approve_live(self, person: str = "alex") -> None:
        """Одобрить живые прогоны человека (`review_status: approved`), как ревьюер перед следующим периодом.

        Фикстура workspace-min — team с `reviewer_gate: true`: вердикт по рекомендации
        неодобренного черновика `append` не пишет (человек текст не получал).
        """
        for run in tracker.live_runs(tracker.load(self.root), person):
            if run.get("review_status") != "approved":
                tracker.update_run(self.root, run["run_id"], review_status="approved")


class CoreFunctionsTest(TrackerCase):
    def test_tracker_path_in_runs(self):
        self.assertEqual(tracker.tracker_path(self.root), self.paths["runs"] / "process-reviews.json")

    def test_load_without_file_is_empty_v2_with_workspace_from_config(self):
        tr = tracker.load(self.root)
        self.assertEqual(tr, {"tracker_version": 2, "workspace": "demo-team", "updated_at": None, "runs": []})

    def test_load_broken_json_is_error_1(self):
        path = tracker.tracker_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(SrError) as ctx:
            tracker.load(self.root)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn(str(path), ctx.exception.message)

    def test_load_wrong_version_is_error_1(self):
        path = tracker.tracker_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"tracker_version": 1, "runs": []}), encoding="utf-8")
        with self.assertRaises(SrError) as ctx:
            tracker.load(self.root)
        self.assertEqual(ctx.exception.code, 1)

    def test_make_run_id_has_person_and_label(self):
        self.assertEqual(tracker.make_run_id("demo-team", "alex", "p1", NOW), RUN_P1)
        self.assertEqual(tracker.make_run_id("demo-team", "alex", None, NOW),
                         "demo-team_alex_run_20260910T120000Z")

    def test_make_run_id_rejects_bad_label(self):
        for label in ("../x", "a/b", "", "-x", "x" * 65, "p 1"):
            with self.subTest(label=label):
                with self.assertRaises(SrError) as ctx:
                    tracker.make_run_id("demo-team", "alex", label, NOW)
                self.assertEqual(ctx.exception.code, 2)

    def test_find_run_and_live_runs(self):
        tr = {"runs": [{"run_id": "a", "person_id": "alex"},
                       {"run_id": "b", "person_id": "sam"},
                       {"run_id": "c", "person_id": "alex", "retracted": {"at": "x", "why": "y", "logs_unmarked": 0}}]}
        self.assertEqual([r["run_id"] for r in tracker.live_runs(tr)], ["a", "b"])
        self.assertEqual([r["run_id"] for r in tracker.live_runs(tr, "alex")], ["a"])
        self.assertEqual(tracker.find_run(tr, "c")["run_id"], "c")
        with self.assertRaises(SrError) as ctx:
            tracker.find_run(tr, "nope")
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("nope", ctx.exception.message)

    def test_next_rec_number_counts_retracted_and_only_this_person(self):
        tr = {"runs": [
            {"run_id": "a", "person_id": "alex", "recommendations": [{"id": "REC-alex-002"}]},
            {"run_id": "b", "person_id": "alex", "recommendations": [{"id": "REC-alex-004"}],
             "retracted": {"at": "x", "why": "y", "logs_unmarked": 0}},
            {"run_id": "c", "person_id": "sam", "recommendations": [{"id": "REC-sam-009"}]},
        ]}
        self.assertEqual(tracker.next_rec_number(tr, "alex"), 5)
        self.assertEqual(tracker.next_rec_number(tr, "sam"), 10)
        self.assertEqual(tracker.next_rec_number(tr, "kim"), 1)

    def test_effective_axes_uses_override(self):
        run = {"run_id": "r", "axes": [{"id": "verification", "score": 1, "insufficient_evidence": False},
                                       {"id": "ownership", "score": None, "insufficient_evidence": True}],
               "operator_override": {"axes": {"verification": 3}, "why": "w", "by": "b", "at": "t"}}
        self.assertEqual(tracker.effective_axes(run), {"verification": 3, "ownership": None})

    def test_schema_problems_missing_schema_is_error_1_with_path(self):
        with self.assertRaises(SrError) as ctx:
            tracker.schema_problems({}, "no-such-schema")
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("no-such-schema.json", ctx.exception.message)
        self.assertIn("ставится вместе с промптами оценки", ctx.exception.message)


class AppendTest(TrackerCase):
    def test_append_writes_run_fields(self):
        result = self.append_p1()
        self.assertEqual(result["run_id"], RUN_P1)
        self.assertIn("alex", result["run_id"])
        self.assertEqual(result["revision"], 1)
        doc = self.tracker_doc()
        self.assertEqual(doc["updated_at"], "2026-09-10T12:00:00Z")
        run = doc["runs"][0]
        self.assertEqual(run["reviewed_at"], "2026-09-10T12:00:00Z")
        self.assertEqual(run["person_id"], "alex")
        self.assertEqual(run["workspace"], "demo-team")
        self.assertEqual(run["review_status"], "draft")
        self.assertIsNone(run["feedback_draft"])
        self.assertIsNone(run["level_computed"])
        self.assertEqual(run["level_judge"], "competent")
        self.assertIsNone(run["artifact"])
        self.assertIsNone(run["operator_override"])
        self.assertEqual(run["reviewer"], "log-judge@v13")
        self.assertEqual(run["period"], {"label": "p1", "from": "2026-09-01T11:30:00Z",
                                         "to": "2026-09-03T12:40:00Z", "anchor": None})
        self.assertEqual(run["period_confidence"], "inferred")
        self.assertEqual(len(run["sessions_reviewed"]), 3)
        judge = self.paths["pending"] / "alex" / "judge.json"
        self.assertEqual(run["source"], {
            "path": ".sessionretro/runs/pending/alex/judge.json",
            "sha256": hashlib.sha256(judge.read_bytes()).hexdigest(),
        })
        self.assertFalse(Path(run["source"]["path"]).is_absolute())
        self.assertEqual(tracker.schema_problems(doc, "tracker"), [])

    def test_dedup_marks_new_grown_and_excluded(self):
        result = self.append_p1()
        self.assertEqual(result["logs_marked"], 3)
        self.assertEqual(result["logs_unmatched"], 0)
        self.assertEqual(result["coverage"], {"marked": 3, "expected": 3})
        index = self.index()
        login = _entry(index, LOGIN)
        self.assertTrue(login["reviewed"])
        self.assertEqual(login["reviewed_in_run"], RUN_P1)
        self.assertEqual(login["reviewed_at"], "2026-09-10T12:00:00Z")
        self.assertEqual(login["state"], "unchanged")
        grown = _entry(index, GROWN)
        self.assertEqual(grown["state"], "unchanged")
        self.assertEqual(grown["reviewed_in_run"], RUN_P1)
        self.assertNotIn("review_from_turn", grown)
        self.assertEqual(grown["reviewed_turns_total"], 18)
        short = _entry(index, SHORT)
        self.assertFalse(short["reviewed"])
        self.assertTrue(short["excluded"])
        self.assertEqual(short["excluded_reason"], "transcript_too_short")
        self.assertEqual(short["state"], "excluded")
        run = self.tracker_doc()["runs"][0]
        self.assertEqual(sorted(m["kind"] for m in run["logs_index_marks"]),
                         ["excluded", "reviewed", "reviewed"])
        self.assertEqual(index["updated_at"], "2026-09-10T12:00:00Z")

    def test_no_index_run_written_all_unmatched(self):
        self.paths["logs_index"].unlink()
        result = self.append_p1()
        self.assertEqual(result["logs_unmatched"], 3)
        self.assertEqual(result["logs_marked"], 0)
        self.assertEqual(len(self.tracker_doc()["runs"]), 1)
        self.assertTrue(any("индекс" in w for w in result["warnings"]))
        self.assertFalse(self.paths["logs_index"].exists())

    def test_failed_validation_leaves_tracker_and_index_bytewise(self):
        self.append_p1()
        before_tr = tracker.tracker_path(self.root).read_bytes()
        before_ix = self.paths["logs_index"].read_bytes()
        bad = self.p2()
        bad["data"]["axes"][0]["evidence"] = []
        self.rejected(bad, now=LATER, period_label="p2")
        self.assertEqual(tracker.tracker_path(self.root).read_bytes(), before_tr)
        self.assertEqual(self.paths["logs_index"].read_bytes(), before_ix)

    def test_failed_first_append_creates_no_tracker(self):
        bad = payload()
        bad["data"]["axes"][0]["evidence"] = []
        before_ix = self.paths["logs_index"].read_bytes()
        self.rejected(bad)
        self.assertFalse(tracker.tracker_path(self.root).exists())
        self.assertEqual(self.paths["logs_index"].read_bytes(), before_ix)

    def test_score_without_quote(self):
        bad = payload()
        bad["data"]["axes"][0]["evidence"] = []
        message = self.rejected(bad)
        self.assertIn("балл без цитаты", message)
        self.assertIn("verification", message)

    def test_score_with_empty_quote_is_without_quote(self):
        bad = payload()
        bad["data"]["axes"][1]["evidence"][0]["quote"] = "  "
        self.assertIn("task_framing", self.rejected(bad))

    def test_insufficient_with_score(self):
        bad = payload()
        bad["data"]["axes"][4]["score"] = 2
        message = self.rejected(bad)
        self.assertIn("context_engineering", message)
        self.assertIn("insufficient_evidence", message)

    def test_no_score_and_not_insufficient(self):
        bad = payload()
        bad["data"]["axes"][4]["insufficient_evidence"] = False
        bad["data"]["axes"][4]["insufficient_evidence_reason"] = None
        self.assertIn("context_engineering", self.rejected(bad))

    def test_axes_out_of_order(self):
        """Условие 3: `data.axes` в порядке `axes.md`, как и `criteria`."""
        obj = payload()
        axes = obj["data"]["axes"]
        axes[0], axes[1] = axes[1], axes[0]
        message = self.rejected(obj)
        self.assertIn("порядк", message)
        self.assertIn("условие 3", message)
        self.assertIn("task_framing, verification", message)

    def test_duplicate_axis(self):
        bad = payload()
        bad["data"]["axes"][1] = json.loads(json.dumps(bad["data"]["axes"][0]))
        message = self.rejected(bad)
        self.assertIn("verification", message)
        self.assertIn("task_framing", message)

    def _gated(self, level: str, triggers: list[str]) -> dict:
        obj = payload()
        obj["data"]["gating_triggered"] = bool(triggers)
        obj["data"]["gating_evidence"] = [
            {"trigger": t, "session_id": LOGIN, "turn_index": 14, "quote": "ок, мержим"} for t in triggers
        ]
        obj["data"]["summary"]["level_judge"] = level
        return obj

    def test_gate_one_trigger_allows_competent(self):
        self.append(self._gated("competent", ["G1"]))

    def test_gate_repeated_trigger_counts_once(self):
        self.append(self._gated("competent", ["G1", "G1", "G1"]))

    def test_gate_one_trigger_level_above_ceiling(self):
        message = self.rejected(self._gated("confident_operator", ["G1"]))
        self.assertIn("потол", message)

    def test_gate_three_triggers_ceiling_developing(self):
        message = self.rejected(self._gated("competent", ["G1", "G2", "G3"]))
        self.assertIn("developing", message)

    def test_gate_evidence_without_trigger_flag(self):
        obj = self._gated("competent", ["G1"])
        obj["data"]["gating_triggered"] = False
        self.assertIn("gating_evidence", self.rejected(obj))

    def test_dash_in_prose_caught(self):
        obj = payload()
        obj["data"]["feedback_parts"]["opening"] = "За период три сессии — форма и API."
        message = self.rejected(obj)
        self.assertIn("тире", message)
        self.assertIn("opening", message)

    def test_dash_inside_quotes_and_quote_field_allowed(self):
        obj = payload()
        obj["data"]["feedback_parts"]["opening"] = "Сказано: «делай — потом посмотрим»."
        obj["data"]["feedback_parts"]["actions"][0]["quote"] = "ок — мержим"
        self.append(obj)

    def test_score_words_in_prose_caught(self):
        obj = payload()
        obj["data"]["feedback_parts"]["closing"] = "Итого 3 балла из 4."
        self.assertIn("closing", self.rejected(obj))

    def test_judge_schema_problem_has_field_path(self):
        obj = payload()
        del obj["data"]["axes"][0]["evidence"][0]["session_id"]
        message = self.rejected(obj)
        self.assertIn("$.data.axes[0].evidence[0]", message)
        self.assertIn("session_id", message)

    def test_rec_id_of_other_person(self):
        obj = payload()
        obj["data"]["recommendations"][0]["id"] = "REC-sam-001"
        self.assertIn("REC-sam-001", self.rejected(obj))

    def test_rec_id_duplicate_in_run(self):
        obj = payload()
        obj["data"]["recommendations"][1]["id"] = "REC-alex-001"
        self.assertIn("REC-alex-001", self.rejected(obj))

    def test_rec_id_not_next_number_renumbered(self):
        """A07: номер с пропуском `append` перенумеровывает в следующий свободный и предупреждает."""
        self.append_p1()
        self.approve_live()  # фикстура — team с гейтом: вердикты только по одобренным
        obj = self.p2()
        obj["data"]["recommendations"][0]["id"] = "REC-alex-005"
        result = self.append(obj, now=LATER, period_label="p2")
        self.assertEqual([r["id"] for r in self.tracker_doc()["runs"][1]["recommendations"]], ["REC-alex-003"])
        self.assertTrue(any("перенумерованы" in w and "REC-alex-005 → REC-alex-003" in w
                            for w in result["warnings"]), result["warnings"])

    def test_rec_id_reused_from_earlier_run_renumbered(self):
        """A07: занятый номер живого прогона тоже перенумеровывается, а не рвёт запись."""
        self.append_p1()
        self.approve_live()  # фикстура — team с гейтом: вердикты только по одобренным
        obj = self.p2()
        obj["data"]["recommendations"][0]["id"] = "REC-alex-002"
        result = self.append(obj, now=LATER, period_label="p2")
        self.assertEqual([r["id"] for r in self.tracker_doc()["runs"][1]["recommendations"]], ["REC-alex-003"])
        self.assertTrue(any("REC-alex-002 → REC-alex-003" in w for w in result["warnings"]), result["warnings"])

    def test_rec_id_out_of_order_in_file_still_caught_by_validate(self):
        """Правка трекера руками мимо `append`: номер не по порядку и занятый ловит `validate`."""
        self.append_p1()
        self.approve_live()  # фикстура — team с гейтом: вердикты только по одобренным
        self.append(self.p2(), now=LATER, period_label="p2")
        doc = self.tracker_doc()
        doc["runs"][1]["recommendations"][0]["id"] = "REC-alex-005"
        self.save_doc(doc)
        problems = "\n".join(tracker.validate_file(self.root))
        self.assertIn("REC-alex-005", problems)
        self.assertIn("003", problems)
        doc["runs"][1]["recommendations"][0]["id"] = "REC-alex-002"
        self.save_doc(doc)
        self.assertIn("уже выдан", "\n".join(tracker.validate_file(self.root)))

    def test_rec_axis_outside_axes(self):
        obj = payload()
        obj["data"]["recommendations"][0]["axis"] = "speed"
        self.assertIn("speed", self.rejected(obj))

    def test_prev_recommendation_unknown_id(self):
        self.append_p1()
        obj = self.p2()
        obj["data"]["prev_recommendations"][0]["id"] = "REC-alex-009"
        self.assertIn("REC-alex-009", self.rejected(obj, now=LATER))

    def test_prev_recommendation_before_it_was_issued(self):
        self.assertIn("REC-alex-001", self.rejected(self.p2()))

    def test_prev_recommendation_status_on_the_fly(self):
        """followed закрывает, not_observable оставляет открытой: третий прогон видит это."""
        self.append_p1()
        self.approve_live()
        self.append(self.p2(), now=LATER, period_label="p2")
        self.approve_live()
        p3 = self.p2(recommendations=[{"id": "REC-alex-004", "axis": "context_engineering",
                                       "text": "Давать агенту выдержку из лога.", "status": "open"}],
                     prev_recommendations=[{"id": "REC-alex-002", "verdict": "partial",
                                            "evidence": None, "note": None}])
        self.append(p3, now=LATER + timedelta(days=7), period_label="p3")
        self.approve_live()
        p4 = self.p2(recommendations=[{"id": "REC-alex-005", "axis": "context_engineering",
                                       "text": "Давать агенту только упавший шаг.", "status": "open"}],
                     prev_recommendations=[{"id": "REC-alex-001", "verdict": "followed",
                                            "evidence": None, "note": None}])
        message = self.rejected(p4, now=LATER + timedelta(days=14), period_label="p4")
        self.assertIn("REC-alex-001", message)
        p4["data"]["prev_recommendations"][0]["id"] = "REC-alex-002"  # partial → всё ещё открыта
        self.append(p4, now=LATER + timedelta(days=14), period_label="p4")

    def test_prev_recommendation_of_other_person(self):
        self.append_p1()
        obj = self.p2()
        obj["data"]["prev_recommendations"][0]["id"] = "REC-sam-001"
        self.assertIn("REC-sam-001", self.rejected(obj, now=LATER))

    def test_criteria_not_eight(self):
        obj = payload()
        obj["data"]["feedback_parts"]["criteria"].pop()
        self.rejected(obj)

    def test_criteria_out_of_axis_order(self):
        obj = payload()
        crit = obj["data"]["feedback_parts"]["criteria"]
        crit[1], crit[2] = crit[2], crit[1]
        self.assertIn("criteria", self.rejected(obj))

    def test_second_place_without_second_action(self):
        obj = self.p2()
        self.append_p1()
        obj["data"]["feedback_parts"]["criteria"][1]["verdict"] = "second_place_above"
        self.assertIn("second_place_above", self.rejected(obj, now=LATER))

    def test_action_without_criterion_pointing_to_it(self):
        obj = payload()
        obj["data"]["feedback_parts"]["criteria"][6]["verdict"] = "even"
        self.assertIn("second_place_above", self.rejected(obj))

    def test_five_levers_total(self):
        obj = payload()
        parts = obj["data"]["feedback_parts"]
        parts["actions"][1]["levers"] = ["Первый рычаг", "Второй рычаг"]
        third = dict(parts["actions"][1], levers=["Третий рычаг"])
        parts["actions"].append(third)
        parts["criteria"][5]["verdict"] = "third_place_above"
        message = self.rejected(obj)
        self.assertIn("рычаг", message)
        self.assertIn("5", message)

    def test_short_card_limits_actions_and_levers(self):
        card = self.paths["people"] / "kim.yaml"
        card.write_text(card.read_text(encoding="utf-8").replace(
            "  address: ты\n", "  address: ты\n  length: short\n"), encoding="utf-8")
        obj = payload(recommendations=KIM_RECS)
        message = self.rejected(obj, person="kim")
        self.assertIn("short", message)
        for action in obj["data"]["feedback_parts"]["actions"]:
            action["levers"] = []
        del obj["data"]["best_practices"]
        result = self.append(obj, person="kim")
        self.assertEqual(result["logs_unmatched"], 3)

    def test_label_with_slash_is_error_2(self):
        self.rejected(payload(), code=2, period_label="../x")

    def test_no_card_is_error_2(self):
        self.rejected(payload(), code=2, person="nobody")

    def test_stub_is_error_1(self):
        message = self.rejected({"stub": True, "note": "агент пишет сюда"})
        self.assertIn("стаб не заполнен", message)
        self.assertIn("judge.json", message)

    def test_missing_payload_file_is_error_2(self):
        with self.assertRaises(SrError) as ctx:
            tracker.append(self.root, self.root / "nope.json", "alex", now=NOW)
        self.assertEqual(ctx.exception.code, 2)

    def test_broken_json_payload_is_error_1(self):
        path = self.paths["pending"] / "alex" / "judge.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{oops", encoding="utf-8")
        with self.assertRaises(SrError) as ctx:
            tracker.append(self.root, path, "alex", now=NOW)
        self.assertEqual(ctx.exception.code, 1)

    def test_status_not_ok_is_error_1(self):
        obj = payload()
        obj["status"] = {"code": "empty_transcript", "ok": False}
        obj["data"] = None
        self.assertIn("empty_transcript", self.rejected(obj))

    def test_duplicate_run_id_same_second(self):
        """Метку отозванного прогона можно назвать явно, но тот же id в ту же секунду не пишется."""
        self.append_p1()
        tracker.retract(self.root, RUN_P1, "ошибка", now=NOW)
        self.assertIn("дубликат", self.rejected(payload(), period_label="p1"))

    def test_broken_old_run_does_not_block_new(self):
        self.append_p1()
        doc = self.tracker_doc()
        doc["runs"][0]["axes"][0]["evidence"] = []
        doc["runs"][0]["review_status"] = "approved"
        self.save_doc(doc)
        result = self.append(self.p2(), now=LATER, period_label="p2")
        self.assertEqual(len(self.tracker_doc()["runs"]), 2)
        self.assertTrue(result["warnings_other_runs"])
        self.assertTrue(all(RUN_P1 in w for w in result["warnings_other_runs"]))
        self.assertTrue(any("балл без цитаты" in w for w in result["warnings_other_runs"]))


class ContractConditionsTest(TrackerCase):
    """Условия 1–9 «Проверок скриптом» (`references/output-contract.md`), которых схема judge не выражает."""

    # условие 1: score = null ⇔ insufficient_evidence, причина только при нехватке данных
    def test_c1_insufficient_without_reason(self):
        obj = payload()
        obj["data"]["axes"][4]["insufficient_evidence_reason"] = None
        message = self.rejected(obj)
        self.assertIn("context_engineering", message)
        self.assertIn("insufficient_evidence_reason", message)

    def test_c1_score_with_reason(self):
        obj = payload()
        obj["data"]["axes"][0]["insufficient_evidence_reason"] = "short_log"
        message = self.rejected(obj)
        self.assertIn("verification", message)
        self.assertIn("short_log", message)

    # условие 2: для context_engineering законно числовое доказательство без хода
    def test_c2_context_numeric_evidence_without_turn(self):
        self.append_p1()
        self.approve_live()
        obj = self.p2()
        obj["data"]["axes"][4]["evidence"] = [{"session_id": None, "turn_index": None,
                                               "quote": "пик заполнения окна 92%, два компакта",
                                               "reading": "сводка по окну"}]
        self.append(obj, now=LATER, period_label="p2")

    def test_c2_numeric_evidence_on_other_axis_rejected(self):
        obj = payload()
        obj["data"]["axes"][0]["evidence"] = [{"session_id": None, "turn_index": None,
                                               "quote": "92%", "reading": "число"}]
        self.assertIn("балл без цитаты", self.rejected(obj))

    # условие 5: на пункт ссылается ровно один критерий, actions ↔ recommendations по индексу
    def test_c5_two_criteria_point_to_one_action(self):
        obj = payload()
        obj["data"]["feedback_parts"]["criteria"][2]["verdict"] = "first_place_above"
        message = self.rejected(obj)
        self.assertIn("ровно один", message)
        self.assertIn("actions[0]", message)

    def test_c5_actions_count_differs_from_recommendations(self):
        obj = payload()
        obj["data"]["recommendations"].pop()
        message = self.rejected(obj)
        self.assertIn("recommendations", message)
        self.assertIn("пунктов 2", message)

    def test_c5_criterion_axis_differs_from_recommendation(self):
        obj = payload()
        crit = obj["data"]["feedback_parts"]["criteria"]
        crit[0]["verdict"], crit[6]["verdict"] = "second_place_above", "first_place_above"
        message = self.rejected(obj)
        self.assertIn("recommendations[0]", message)
        self.assertIn("trust_calibration", message)

    # условие 7: рычаги ↔ приёмы best_practices по каждой оси
    def test_c7_levers_differ_from_practices(self):
        obj = payload()
        obj["data"]["best_practices"][0]["practices"].pop()
        message = self.rejected(obj)
        self.assertIn("best_practices", message)
        self.assertIn("verification", message)

    def test_c7_practices_without_levers(self):
        obj = payload()
        obj["data"]["best_practices"].append({
            "axis": "trust_calibration",
            "anchor": {"session_id": GROWN, "turn_index": 7, "quote": "да, применяй всё"},
            "practices": ["принимать сначала одно место из трёх"]})
        message = self.rejected(obj)
        self.assertIn("trust_calibration", message)
        self.assertIn("best_practices", message)

    def test_c7_levers_without_best_practices(self):
        obj = payload()
        del obj["data"]["best_practices"]
        self.assertIn("best_practices", self.rejected(obj))

    # условие 8: insufficient допустим при любом гейте
    def test_c8_insufficient_under_two_triggers_allowed(self):
        obj = payload()
        obj["data"]["gating_triggered"] = True
        obj["data"]["gating_evidence"] = [
            {"trigger": t, "session_id": LOGIN, "turn_index": 14, "quote": "ок, мержим"} for t in ("G1", "G2")]
        obj["data"]["summary"]["level_judge"] = "insufficient"
        self.append(obj)

    # условие 9: запреты текста в полях feedback_parts
    def field_rejected(self, path: tuple, text: str) -> str:
        obj = payload()
        node = obj["data"]["feedback_parts"]
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = text
        return self.rejected(obj)

    def test_c9_old_form_marker(self):
        message = self.field_rejected(("opening",), "Приёмы под то, что стоит подтянуть, ниже.")
        self.assertIn("старой формы", message)
        self.assertIn("feedback_parts.opening", message)

    def test_c9_old_recommendation_marker(self):
        message = self.field_rejected(("actions", 0, "try"), "Рекомендация (попробовать): запускать тесты")
        self.assertIn("старой формы", message)
        self.assertIn("actions[0].try", message)

    def test_c9_angle_bracket_outside_quotes(self):
        message = self.field_rejected(("actions", 0, "cost"), "> дифф ушёл без тестов")
        self.assertIn("«>»", message)
        self.assertIn("actions[0].cost", message)

    def test_c9_angle_bracket_inside_quotes_allowed(self):
        obj = payload()
        obj["data"]["feedback_parts"]["actions"][0]["cost"] = "в выводе стояло «exit code > 0», и это ушло в ветку"
        self.append(obj)

    def test_c9_level_name(self):
        message = self.field_rejected(("criteria", 1, "evidence"), "Это уровень Оркестратор.")
        self.assertIn("имя уровня", message)
        self.assertIn("criteria[1].evidence", message)

    def test_c9_ambiguous_ru_level_names_in_plain_speech_allowed(self):
        """«Мало данных» и «Развивается» — обычная речь; запрещены только рядом со словом «уровень»."""
        obj = payload()
        crit = obj["data"]["feedback_parts"]["criteria"]
        crit[1]["evidence"] = "В логах мало данных о проверке."
        crit[2]["evidence"] = "Навык развивается от сессии к сессии."
        self.append(obj)

    def test_c9_ambiguous_ru_level_names_next_to_level_caught(self):
        for text in ("Твой уровень: мало данных.", "Сейчас уровень развивается.", "Развивается, это уровень."):
            with self.subTest(text=text):
                message = self.field_rejected(("criteria", 1, "evidence"), text)
                self.assertIn("имя уровня", message)

    def test_c9_axis_identifier(self):
        message = self.field_rejected(("actions", 1, "levers"), ["смотреть на trust_calibration"])
        self.assertIn("snake_case", message)

    def test_c9_comparison_with_others(self):
        self.assertIn("сравнение", self.field_rejected(("closing",), "Здесь лучше, чем у других."))

    def test_c9_address_mixed(self):
        message = self.field_rejected(("actions", 0, "try"), "попробуйте запускать тесты, как вам удобнее")
        self.assertIn("обращение", message)

    # образец выхода судьи из fixtures/prompts проходит все условия
    def test_prompts_judge_sample_passes_all_conditions(self):
        obj = json.loads(PROMPTS_SAMPLE.read_text(encoding="utf-8"))
        result = self.append(obj, period_label="p1")
        self.assertEqual(result["revision"], 1)


class ReplaceTest(TrackerCase):
    def test_replace_keeps_override_and_id_and_unmarks_victim(self):
        self.append_p1()
        doc = self.tracker_doc()
        override = {"axes": {"verification": 3}, "why": "проверка была во второй сессии",
                    "by": "reviewer", "at": "2026-09-11T09:00:00Z"}
        doc["runs"][0]["operator_override"] = override
        self.save_doc(doc)
        obj = payload()
        obj["data"]["session_scope"]["sessions"] = obj["data"]["session_scope"]["sessions"][:1]
        result = self.append(obj, now=LATER, period_label="p1", replace=RUN_P1)
        self.assertEqual(result["run_id"], RUN_P1)
        self.assertEqual(result["revision"], 2)
        runs = self.tracker_doc()["runs"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["operator_override"], override)
        self.assertEqual(runs[0]["revision"], 2)
        index = self.index()
        self.assertEqual(_entry(index, LOGIN)["reviewed_in_run"], RUN_P1)
        grown = _entry(index, GROWN)
        self.assertEqual(grown["reviewed_in_run"], OLD_RUN)
        self.assertEqual(grown["state"], "grown")
        self.assertEqual(grown["review_from_turn"], 10)
        short = _entry(index, SHORT)
        self.assertFalse(short.get("excluded"))
        self.assertEqual(short["state"], "new")
        self.assertEqual(len(runs[0]["logs_index_marks"]), 1)

    def test_replace_older_run_in_place_keeps_order_and_rec_statuses(self):
        """Замена не последнего прогона с теми же REC-id: место в списке, статусы и validate прежние."""
        self.append_p1()
        self.approve_live()
        run_p2 = self.append(self.p2(), now=LATER, period_label="p2")["run_id"]
        statuses = tracker.rec_statuses(self.tracker_doc(), "alex")
        result = self.append(payload(), now=LATER + timedelta(days=7), period_label="p1", replace=RUN_P1)
        self.assertEqual(result["run_id"], RUN_P1)
        self.assertEqual(result["revision"], 2)
        doc = self.tracker_doc()
        self.assertEqual([r["run_id"] for r in doc["runs"]], [RUN_P1, run_p2])
        self.assertEqual(doc["runs"][0]["revision"], 2)
        self.assertEqual(tracker.rec_statuses(doc, "alex"), statuses)
        self.assertEqual(tracker.validate_file(self.root), [])
        self.assertEqual(result["warnings_other_runs"], [])

    def test_replace_unknown_run(self):
        self.assertIn("nope", self.rejected(payload(), replace="nope"))

    def test_replace_run_of_other_person(self):
        obj = payload(recommendations=KIM_RECS)
        kim_run = self.append(obj, person="kim", period_label="p1")["run_id"]
        self.assertIn(kim_run, self.rejected(payload(), now=LATER, replace=kim_run))


class GrownTailTest(TrackerCase):
    """У выросшей сессии `turns_total` и начало периода считаются по хвосту, а не по всей сессии.

    p1 оценивает сессию S (10 ходов) целиком; к p2 S выросла до 25 (`grown`, `review_from_turn: 10`),
    плюс новая сессия T (7 ходов). p2 прочитал 15 + 7 ходов, а период p2 не заходит в период p1.
    """

    S_PATH = "logs/alex/2026-09-01_11-30-00Z-refactor-api.md"
    T_PATH = "logs/alex/2026-09-08_14-00-00Z-fix-tests.md"

    def edit_index(self, session_id: str, **fields) -> None:
        index = self.index()
        _entry(index, session_id).update(fields)
        self.paths["logs_index"].write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")

    def setUp(self) -> None:
        super().setUp()
        self.edit_index(GROWN, turns_total=10, state="new", reviewed=False, reviewed_in_run=None,
                        reviewed_at=None, reviewed_turns_total=None, reviewed_bytes=None,
                        review_from_turn=None)
        p1 = payload()
        p1["data"]["session_scope"]["sessions"][1].update(
            turns_total=10, reviewed_from_turn=None, state="new")
        self.append(p1, period_label="p1")
        self.approve_live()
        self.edit_index(GROWN, turns_total=25, state="grown", review_from_turn=10,
                        log_ended_at="2026-09-09T10:00:00Z")
        self.edit_index(FIX_ALEX, turns_total=7)

    def p2_grown(self, turns_analyzed: int = 22, extra=()) -> dict:
        obj = self.p2()
        obj["metadata"]["turns_analyzed"] = turns_analyzed
        obj["data"]["session_scope"]["sessions"] = [
            {"session_id": GROWN, "path": self.S_PATH, "turns_total": 25, "reviewed_from_turn": 10,
             "state": "grown", "status": "reviewed", "status_note": None},
            {"session_id": FIX_ALEX, "path": self.T_PATH, "turns_total": 7, "reviewed_from_turn": None,
             "state": "new", "status": "reviewed", "status_note": None},
            *extra,
        ]
        return obj

    def p2_run(self) -> dict:
        return self.tracker_doc()["runs"][-1]

    def test_turns_total_counts_only_tail_of_grown_session(self):
        result = self.append(self.p2_grown(), now=LATER, period_label="p2")
        self.assertEqual(self.p2_run()["turns_total"], 15 + 7)
        self.assertFalse([w for w in result["warnings"] if "ходов" in w], result["warnings"])

    def test_period_from_not_before_previous_run_to(self):
        self.append(self.p2_grown(), now=LATER, period_label="p2")
        p1, p2 = self.tracker_doc()["runs"]
        self.assertEqual(p1["period"]["to"], "2026-09-03T12:40:00Z")
        self.assertGreaterEqual(p2["period"]["from"], p1["period"]["to"])
        self.assertEqual(p2["period"]["from"], "2026-09-03T12:40:00Z")
        # конец периода — самая поздняя сессия p2: T в фикстуре перенесена на 11.09 (A16)
        self.assertEqual(p2["period"]["to"], "2026-09-11T15:05:00Z")
        self.assertEqual(tracker.validate_file(self.root), [])

    def test_judge_turns_mismatch_is_warning_with_both_numbers(self):
        result = self.append(self.p2_grown(turns_analyzed=32), now=LATER, period_label="p2")
        self.assertEqual(self.p2_run()["turns_total"], 22)
        hits = [w for w in result["warnings"] if "судья указал 32" in w and "по индексу 22" in w]
        self.assertEqual(len(hits), 1, result["warnings"])

    def test_session_outside_index_takes_judge_turns_with_warning(self):
        ghost = {"session_id": "0a0b0c0d-1111-4222-8333-944445555666", "path": "logs/alex/ghost.md",
                 "turns_total": 5, "reviewed_from_turn": None, "state": "new", "status": "reviewed",
                 "status_note": None}
        result = self.append(self.p2_grown(turns_analyzed=27, extra=[ghost]), now=LATER, period_label="p2")
        self.assertEqual(self.p2_run()["turns_total"], 27)
        self.assertEqual(result["logs_unmatched"], 1)
        hits = [w for w in result["warnings"] if "turns_analyzed" in w and ghost["session_id"] in w]
        self.assertEqual(len(hits), 1, result["warnings"])

    def test_first_run_period_unchanged_without_previous_run(self):
        p1 = self.tracker_doc()["runs"][0]
        self.assertEqual(p1["period"]["from"], "2026-09-01T11:30:00Z")
        self.assertEqual(p1["turns_total"], 24 + 10)


class RecRenumberTest(TrackerCase):
    """A07: `append` перенумеровывает новые рекомендации с `next_rec_number`, в том числе после `retract`."""

    def append(self, obj, person: str = "alex", **kw) -> dict:
        # фикстура — team с `reviewer_gate`: вердикты только по одобренным прогонам (B04)
        self.approve_live(person)
        return super().append(obj, person, **kw)

    def retracted_p2(self) -> None:
        """p1 выдаёт REC-alex-001/002, p2 — REC-alex-003, p2 отозван: номер 003 занят навсегда."""
        self.append_p1()
        run_p2 = self.append(self.p2(), now=LATER, period_label="p2")["run_id"]
        tracker.retract(self.root, run_p2, "оценщик перепутал сессии", now=LATER + timedelta(hours=1))
        self.approve_live()

    @staticmethod
    def renumber_warnings(result: dict) -> list[str]:
        return [w for w in result["warnings"] if "перенумерованы" in w]

    def test_a07_after_retract_judge_number_renumbered(self):
        self.retracted_p2()
        self.assertEqual(tracker.open_recs(self.tracker_doc(), "alex", gated=True)["next_number"], 4)
        result = self.append(self.p2(), now=LATER + timedelta(days=1), period_label="p2")
        run = tracker.find_run(self.tracker_doc(), result["run_id"])
        self.assertEqual([r["id"] for r in run["recommendations"]], ["REC-alex-004"])
        warns = self.renumber_warnings(result)
        self.assertEqual(len(warns), 1, result["warnings"])
        self.assertIn("REC-alex-003 → REC-alex-004", warns[0])
        self.assertEqual(tracker.validate_file(self.root), [])
        self.assertEqual(tracker.open_recs(self.tracker_doc(), "alex", gated=True)["next_number"], 5)

    def test_a07_cli_append_after_retract_is_0_with_warning(self):
        self.retracted_p2()
        code, out, err = self.cli("append", str(self.write(self.p2())), "--person", "alex",
                                  "--period-label", "p2")
        self.assertEqual(code, 0, err)
        self.assertIn("REC-alex-003 → REC-alex-004", err)
        self.assertIn("перенумерованы", json.loads(out)["warnings"][0])

    def test_correct_number_no_warning(self):
        self.append_p1()
        result = self.append(self.p2(), now=LATER, period_label="p2")
        self.assertEqual(self.renumber_warnings(result), [])
        self.assertEqual([r["id"] for r in self.tracker_doc()["runs"][1]["recommendations"]], ["REC-alex-003"])

    def test_two_recs_renumbered_in_same_order(self):
        self.append_p1()
        self.append(self.p2(), now=LATER, period_label="p2")
        obj = payload()
        obj["data"]["recommendations"][0]["id"] = "REC-alex-002"
        obj["data"]["recommendations"][1]["id"] = "REC-alex-003"
        texts = [r["text"] for r in obj["data"]["recommendations"]]
        result = self.append(obj, now=LATER + timedelta(days=7), period_label="p3")
        recs = tracker.find_run(self.tracker_doc(), result["run_id"])["recommendations"]
        self.assertEqual([r["id"] for r in recs], ["REC-alex-004", "REC-alex-005"])
        self.assertEqual([r["text"] for r in recs], texts)
        warns = self.renumber_warnings(result)
        self.assertEqual(len(warns), 1, result["warnings"])
        self.assertIn("REC-alex-002 → REC-alex-004, REC-alex-003 → REC-alex-005", warns[0])

    def test_replace_keeps_own_numbers(self):
        """`--replace` не последнего и последнего прогона: номера заменяемого доступны замене."""
        self.append_p1()
        run_p2 = self.append(self.p2(), now=LATER, period_label="p2")["run_id"]
        first = self.append(payload(), now=LATER + timedelta(days=7), period_label="p1", replace=RUN_P1)
        self.assertEqual(self.renumber_warnings(first), [])
        second = self.append(self.p2(), now=LATER + timedelta(days=7), period_label="p2", replace=run_p2)
        self.assertEqual(self.renumber_warnings(second), [])
        doc = self.tracker_doc()
        self.assertEqual([r["id"] for r in doc["runs"][0]["recommendations"]], ["REC-alex-001", "REC-alex-002"])
        self.assertEqual([r["id"] for r in doc["runs"][1]["recommendations"]], ["REC-alex-003"])
        self.assertEqual(tracker.validate_file(self.root), [])

    def test_bad_form_or_other_person_or_duplicate_still_1(self):
        self.retracted_p2()
        for bad in ("REC-alex-3", "REC-sam-004", "rec-alex-004"):
            with self.subTest(bad=bad):
                obj = self.p2()
                obj["data"]["recommendations"][0]["id"] = bad
                self.assertIn(bad, self.rejected(obj, now=LATER + timedelta(days=1), period_label="p2"))
        obj = payload()
        obj["data"]["recommendations"][1]["id"] = obj["data"]["recommendations"][0]["id"]
        self.assertIn("повторён", self.rejected(obj, now=LATER + timedelta(days=1), period_label="p3"))
        self.assertEqual(len(self.tracker_doc()["runs"]), 2)

    def test_render_review_after_renumber_sha_matches(self):
        """Выход судьи на диске не переписывается: sha совпадает, в досье — новый номер."""
        self.retracted_p2()
        judge = self.write(self.p2())
        before = judge.read_bytes()
        result = tracker.append(self.root, judge, "alex", period_label="p2", now=LATER + timedelta(days=1))
        self.assertEqual(judge.read_bytes(), before)
        run = tracker.find_run(self.tracker_doc(), result["run_id"])
        self.assertEqual(run["source"]["sha256"], hashlib.sha256(before).hexdigest())
        path = render_review.write_review(self.root, judge, "alex", result["run_id"],
                                          now=LATER + timedelta(days=1))
        text = path.read_text(encoding="utf-8")
        self.assertIn("REC-alex-004", text)
        self.assertNotIn("REC-alex-003", text)


class OtherPeriodTest(TrackerCase):
    """Сессия чужого периода в индексе не закрывается: её оценит прогон своего периода."""

    def p1_with_other_period(self) -> dict:
        obj = payload()
        obj["data"]["session_scope"]["sessions"].append({
            "session_id": FIX_ALEX, "path": "logs/alex/2026-09-08_14-00-00Z-fix-tests.md", "turns_total": 12,
            "reviewed_from_turn": None, "state": "new", "status": "other_period", "status_note": None})
        return obj

    def test_other_period_session_left_open_and_taken_by_own_period(self):
        result = self.append(self.p1_with_other_period(), period_label="p1")
        self.assertEqual(result["coverage"], {"marked": 3, "expected": 3})
        self.assertEqual(result["logs_marked"], 3)
        self.assertEqual(result["logs_unmatched"], 0)
        self.assertEqual(result["logs_left_open"], [FIX_ALEX])
        fix = _entry(self.index(), FIX_ALEX)
        self.assertFalse(fix.get("excluded"))
        self.assertFalse(fix["reviewed"])
        self.assertEqual(fix["state"], "new")
        run = self.tracker_doc()["runs"][0]
        self.assertIn("other_period", [s["status"] for s in run["sessions_reviewed"]])
        self.assertNotIn(FIX_ALEX, [m["session_id"] for m in run["logs_index_marks"]])
        plan = batches.for_person(self.root, "alex", measure="index", period_label="p2")
        taken = [s["session_id"] for b in plan["batches"] for s in b["sessions"]]
        self.assertEqual(taken, [FIX_ALEX])

    def test_mark_logs_leaves_other_period_open(self):
        self.append(self.p1_with_other_period(), period_label="p1")
        result = tracker.mark_logs(self.root, RUN_P1, force=True, now=LATER)
        self.assertEqual(result["coverage"], {"marked": 3, "expected": 3})
        self.assertEqual(_entry(self.index(), FIX_ALEX)["state"], "new")

    def test_mark_logs_explicit_other_period_is_2(self):
        self.append_p1()
        before = self.paths["logs_index"].read_bytes()
        with self.assertRaises(SrError) as ctx:
            tracker.mark_logs(self.root, RUN_P1, excluded=[f"{FIX_ALEX}=other_period"], now=LATER)
        self.assertEqual(ctx.exception.code, 2, ctx.exception.message)
        self.assertIn("other_period", ctx.exception.message)
        self.assertEqual(self.paths["logs_index"].read_bytes(), before)


class CliTest(TrackerCase):
    def test_append_cli_prints_json_and_validate_clean(self):
        path = self.write(payload())
        code, out, err = self.cli("append", str(path), "--person", "alex", "--period-label", "p1",
                                  "--period-confidence", "explicit", "--skipped", "s-1", "s-2")
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertIn("alex", result["run_id"])
        run = self.tracker_doc()["runs"][0]
        self.assertEqual(run["period_confidence"], "explicit")
        self.assertEqual(run["sessions_skipped_known"], ["s-1", "s-2"])
        code, out, err = self.cli("validate")
        self.assertEqual(code, 0, out + err)

    def test_append_cli_score_without_quote(self):
        bad = payload()
        bad["data"]["axes"][0]["evidence"] = []
        code, out, err = self.cli("append", str(self.write(bad)), "--person", "alex")
        self.assertEqual(code, 1)
        self.assertIn("балл без цитаты", err)
        self.assertIn("verification", err)

    def test_append_cli_bad_label_and_no_card(self):
        path = self.write(payload())
        self.assertEqual(self.cli("append", str(path), "--person", "alex", "--period-label", "../x")[0], 2)
        self.assertEqual(self.cli("append", str(path), "--person", "nobody")[0], 2)

    def test_append_cli_missing_schema_names_schema_path(self):
        path = self.write(payload())
        missing = self.root / "no-schemas" / "judge.json"
        real = schema.schema_path
        with mock.patch.object(schema, "schema_path",
                               side_effect=lambda name: missing if name == "judge" else real(name)):
            code, out, err = self.cli("append", str(path), "--person", "alex")
        self.assertEqual(code, 1)
        self.assertIn(str(missing), err)
        self.assertIn("ставится вместе с промптами оценки", err)
        self.assertFalse(tracker.tracker_path(self.root).exists())

    def test_validate_catches_bad_override_form(self):
        self.append_p1()
        doc = self.tracker_doc()
        doc["runs"][0]["operator_override"] = {"score": 3}
        self.save_doc(doc)
        code, out, err = self.cli("validate")
        self.assertEqual(code, 1)
        self.assertIn("operator_override", out)
        problems = tracker.validate_file(self.root)
        self.assertTrue(any("operator_override" in p for p in problems))

    def test_validate_override_bad_axis_and_empty_why(self):
        self.append_p1()
        doc = self.tracker_doc()
        doc["runs"][0]["operator_override"] = {"axes": {"speed": 3}, "why": "", "by": "r", "at": "t"}
        self.save_doc(doc)
        problems = "\n".join(tracker.validate_file(self.root))
        self.assertIn("speed", problems)
        self.assertIn("why", problems)

    def test_validate_skips_retracted_runs(self):
        self.append_p1()
        doc = self.tracker_doc()
        doc["runs"][0]["axes"][0]["evidence"] = []
        doc["runs"][0]["retracted"] = {"at": "2026-09-11T00:00:00Z", "why": "ошибка", "logs_unmarked": 0}
        self.save_doc(doc)
        self.assertEqual(tracker.validate_file(self.root), [])

    def test_validate_without_tracker_is_clean(self):
        self.assertEqual(self.cli("validate")[0], 0)

    def test_validate_duplicate_run_id(self):
        self.append_p1()
        doc = self.tracker_doc()
        doc["runs"].append(json.loads(json.dumps(doc["runs"][0])))
        self.save_doc(doc)
        self.assertTrue(any("дубликат" in p for p in tracker.validate_file(self.root)))


DAY_LABEL = "run-20260910"
SAME_DAY = NOW + timedelta(hours=2)


class RunModeLabelTest(TrackerCase):
    """Режим `period.mode: run`: метку прогона выдаёт движок, прогоны одних суток — разные периоды."""

    def p2_partial(self) -> dict:
        obj = self.p2()
        obj["data"]["prev_recommendations"][0]["verdict"] = "partial"
        return obj

    def set_period_mode(self, mode: str) -> None:
        cfg = load_config(self.root)
        cfg["period"] = {"mode": mode}
        save_config(self.root, cfg)

    def test_two_runs_same_day_get_distinct_labels(self):
        """Регрессия: второй прогон за сутки не ревизия первого, вердикт по рекомендации первого находит её."""
        first = self.append(payload())
        self.approve_live()
        second = self.append(self.p2_partial(), now=SAME_DAY)
        self.assertEqual(first["period_label"], DAY_LABEL)
        self.assertEqual(second["period_label"], DAY_LABEL + "-2")
        self.assertEqual(first["run_id"], f"demo-team_alex_{DAY_LABEL}_20260910T120000Z")
        self.assertEqual(second["run_id"], f"demo-team_alex_{DAY_LABEL}-2_20260910T140000Z")
        tr = tracker.load(self.root)
        self.assertEqual([r["period"]["label"] for r in tracker.live_runs(tr, "alex")],
                         [DAY_LABEL, DAY_LABEL + "-2"])
        self.assertEqual(second["revision"], 1)
        opened = {item["id"]: item for item in tracker.open_recs(tr, "alex", gated=True)["open"]}
        self.assertIn("REC-alex-001", opened)
        self.assertEqual(opened["REC-alex-001"]["period"], DAY_LABEL)
        self.assertEqual(tracker.validate_file(self.root), [])

    def test_suffix_skips_label_of_retracted_run(self):
        """Метка отозванного прогона занята: новая берёт следующий свободный суффикс."""
        self.append(payload())
        self.approve_live()
        second = self.append(self.p2(), now=SAME_DAY)
        tracker.retract(self.root, second["run_id"], "повтор по ошибке", now=SAME_DAY + timedelta(minutes=5))
        third = self.append(self.p2(recommendations=[dict(self.p2()["data"]["recommendations"][0],
                                                          id="REC-alex-004")]),
                            now=SAME_DAY + timedelta(hours=1))
        self.assertEqual(third["period_label"], DAY_LABEL + "-3")
        self.assertEqual(tracker.validate_file(self.root), [])

    def test_other_people_same_day_have_no_suffix(self):
        self.append(payload())
        recs = [dict(r, id=r["id"].replace("alex", "kim")) for r in payload()["data"]["recommendations"]]
        kim = self.append(payload(recommendations=recs), person="kim", now=SAME_DAY)
        self.assertEqual(kim["period_label"], DAY_LABEL)
        self.assertEqual(kim["run_id"], f"demo-team_kim_{DAY_LABEL}_20260910T140000Z")

    def test_explicit_label_is_kept_in_run_mode(self):
        result = self.append_p1()
        self.assertEqual(result["period_label"], "p1")
        self.assertEqual(result["run_id"], RUN_P1)

    def test_explicit_duplicate_label_without_replace_is_error_1(self):
        self.append_p1()
        before = tracker.tracker_path(self.root).read_bytes()
        message = self.rejected(self.p2(), now=LATER, period_label="p1")
        self.assertIn(f"у alex уже есть прогон {RUN_P1} с меткой p1", message)
        self.assertIn(f"--replace {RUN_P1}", message)
        self.assertIn("другая метка", message)
        self.assertEqual(tracker.tracker_path(self.root).read_bytes(), before)

    def test_explicit_duplicate_label_with_replace_is_revision(self):
        self.append_p1()
        result = self.append(payload(), now=LATER, period_label="p1", replace=RUN_P1)
        self.assertEqual(result["run_id"], RUN_P1)
        self.assertEqual(result["revision"], 2)
        self.assertEqual(result["period_label"], "p1")

    def test_replace_without_label_keeps_label(self):
        first = self.append(payload())
        result = self.append(payload(), now=SAME_DAY, replace=first["run_id"])
        self.assertEqual(result["period_label"], DAY_LABEL)
        self.assertEqual(len(self.tracker_doc()["runs"]), 1)

    def test_labels_mode_without_label_stays_unlabeled(self):
        self.set_period_mode("labels")
        result = self.append(payload())
        self.assertIsNone(result["period_label"])
        self.assertEqual(result["run_id"], "demo-team_alex_run_20260910T120000Z")

    def test_cli_prints_period_label(self):
        code, out, err = self.cli("append", str(self.write(payload())), "--person", "alex")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["period_label"].split("-")[0], "run")

    def test_cli_duplicate_label_is_code_1(self):
        self.append_p1()
        code, out, err = self.cli("append", str(self.write(self.p2())), "--person", "alex",
                                  "--period-label", "p1")
        self.assertEqual(code, 1)
        self.assertIn("--replace", err)

    def test_report_two_runs_same_day_are_two_periods(self):
        """`report --personal`: два прогона за сутки — две точки, без «ревью не привязано»."""
        runs = []
        for obj, moment, name in ((payload(), NOW, "judge-1.json"),
                                  (self.p2_partial(), SAME_DAY, "judge-2.json")):
            judge = self.write(obj, name=name)
            result = tracker.append(self.root, judge, "alex", now=moment)
            render_review.write_review(self.root, judge, "alex", result["run_id"], now=moment)
            render_review.approve(self.root, "alex", result["run_id"], now=moment)
            runs.append(result["run_id"])
        res = run_sr("report", "--personal", "alex", "--json", home=self.home, cwd=self.root)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("не привязано", res.stderr)
        self.assertNotIn("неизвестной рекомендации", res.stderr)
        model = report_data.collect(self.root, lambda _m: None)
        alex = next(p for p in model["people"] if p["id"] == "alex")
        self.assertEqual([p["period"] for p in alex["points"]], [DAY_LABEL, DAY_LABEL + "-2"])
        self.assertEqual(model["unbound_reviews"], [])


class RegistryLastRunTest(TrackerCase):
    """`append` пишет `last_run_at` = `reviewed_at` прогона в реестр workspaces."""

    def registered(self) -> dict:
        return registry.register(self.root, "Demo team", "team", [])

    def test_append_writes_last_run_at(self):
        entry = self.registered()
        self.append_p1()
        run = self.tracker_doc()["runs"][0]
        saved = registry.find_by_path(self.root)
        self.assertEqual(saved["id"], entry["id"])
        self.assertEqual(saved["last_run_at"], run["reviewed_at"])
        self.assertEqual(saved["last_run_at"], "2026-09-10T12:00:00Z")

    def test_second_append_moves_last_run_at(self):
        self.registered()
        self.append_p1()
        self.approve_live()
        result = self.append(self.p2(), now=LATER, period_label="p2")
        runs = self.tracker_doc()["runs"]
        self.assertEqual(registry.find_by_path(self.root)["last_run_at"], runs[-1]["reviewed_at"])
        self.assertEqual(runs[-1]["reviewed_at"], "2026-09-17T12:00:00Z")
        self.assertFalse(any("реестр" in w for w in result["warnings"]), result["warnings"])

    def test_not_registered_warns_and_run_is_written(self):
        code, out, err = self.cli("append", str(self.write(payload())), "--person", "alex")
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        hits = [w for w in result["warnings"] if "не в реестре" in w]
        self.assertEqual(len(hits), 1, result["warnings"])
        self.assertIn("last_run_at не записан", hits[0])
        self.assertIn(str(self.root), hits[0])
        self.assertIn("не в реестре", err)
        self.assertEqual(len(self.tracker_doc()["runs"]), 1)
        self.assertIsNone(registry.find_by_path(self.root))

    def test_registry_error_keeps_run_and_warns(self):
        self.registered()
        boom = SrError("реестр workspaces: сбой записи (файл: x)", 1)
        with mock.patch.object(registry, "update", side_effect=boom):
            result = self.append_p1()
        self.assertEqual(len(self.tracker_doc()["runs"]), 1)
        hits = [w for w in result["warnings"] if "сбой записи" in w]
        self.assertEqual(len(hits), 1, result["warnings"])
        self.assertIn("прогон записан", hits[0])
        self.assertIsNone(registry.find_by_path(self.root)["last_run_at"])

    def test_retract_keeps_last_run_at(self):
        self.registered()
        self.append_p1()
        tracker.retract(self.root, RUN_P1, "ошибка", now=LATER)
        self.assertEqual(registry.find_by_path(self.root)["last_run_at"], "2026-09-10T12:00:00Z")

    def test_doctor_shows_last_run_at(self):
        self.registered()
        self.append_p1()
        code, out, err = self.cli("doctor", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["last_run_at"], "2026-09-10T12:00:00Z")


if __name__ == "__main__":
    unittest.main()
