"""`lib/tracker.py` (операции над записанными прогонами) и команды `retract`, `open-recs`,
`compare`, `mark-logs`, `verify`, `level`: статусы рекомендаций, откат пометок, сравнение,
догон покрытия, аудит, запись уровня."""

from __future__ import annotations

import json
import unittest
from datetime import timedelta
from unittest import mock

import _util  # noqa: F401 — кладёт scripts/ в sys.path
from _assess import payload
from test_tracker import (FIX_ALEX, GROWN, KIM_RECS, LATER, LOGIN, NOW, OLD_RUN, RUN_P1, SHORT,
                          TrackerCase, _entry)

from lib import batches, inventory, marks, render_review, tracker
from lib.config import load_config, save_config
from lib.lock import file_lock
from lib.workspace import SrError

RUN_P2 = "demo-team_alex_p2_20260917T120000Z"
LATEST = LATER + timedelta(days=7)
LOGIN_KEY = "2026-09-02_09-00-00Z-login-form"
FIX_KEY = "2026-09-08_14-00-00Z-fix-tests"


class OpsCase(TrackerCase):
    def append_p2(self, **patch) -> dict:
        """p2 после одобрения p1: при гейте ревьюера вердикт по черновику не пишется (B04)."""
        self.approve_live()
        return self.append(self.p2(**patch), now=LATER, period_label="p2")

    def append_p2_with_login(self) -> dict:
        """p2 с сессией LOGIN после одобрения p1 (гейт ревьюера, B04)."""
        self.approve_live()
        return self.append(self.p2_with_login(), now=LATER, period_label="p2")

    def p2_with_login(self) -> dict:
        """p2, в котором снова оценена сессия LOGIN из p1: пометка переставляется."""
        obj = self.p2()
        sessions = obj["data"]["session_scope"]["sessions"]
        sessions.append({"session_id": LOGIN, "path": f"logs/alex/{LOGIN_KEY}.md", "turns_total": 24,
                         "reviewed_from_turn": None, "state": "new", "status": "reviewed",
                         "status_note": None})
        return obj

    def code(self, *args: str) -> int:
        return self.cli(*args)[0]

    def raises(self, code: int, fn, *args, **kw) -> str:
        with self.assertRaises(SrError) as ctx:
            fn(*args, **kw)
        self.assertEqual(ctx.exception.code, code, ctx.exception.message)
        return ctx.exception.message


class RecStatusTest(OpsCase):
    def test_followed_closes_not_observable_keeps_open(self):
        self.append_p1()
        self.append_p2()
        statuses = tracker.rec_statuses(self.tracker_doc(), "alex")
        self.assertEqual(statuses, {"REC-alex-001": "followed", "REC-alex-002": "open", "REC-alex-003": "open"})

    def test_partial_keeps_open(self):
        self.append_p1()
        self.append_p2(prev_recommendations=[{"id": "REC-alex-001", "verdict": "partial",
                                              "evidence": None, "note": None}])
        self.assertEqual(tracker.rec_statuses(self.tracker_doc(), "alex")["REC-alex-001"], "open")

    def test_ignored_closes(self):
        self.append_p1()
        self.append_p2(prev_recommendations=[{"id": "REC-alex-002", "verdict": "ignored",
                                              "evidence": None, "note": None}])
        self.assertEqual(tracker.rec_statuses(self.tracker_doc(), "alex")["REC-alex-002"], "ignored")

    def test_other_person_has_none(self):
        self.append_p1()
        self.assertEqual(tracker.rec_statuses(self.tracker_doc(), "sam"), {})

    def test_open_recs_items_and_next_number(self):
        self.append_p1()
        self.append_p2()
        self.approve_live()
        result = tracker.open_recs(self.tracker_doc(), "alex", gated=True)
        self.assertEqual(result["next_number"], 4)
        self.assertEqual(result["pending_approval"], [])
        self.assertEqual([r["id"] for r in result["open"]], ["REC-alex-002", "REC-alex-003"])
        first = result["open"][0]
        self.assertEqual(first["axis"], "trust_calibration")
        self.assertEqual(first["period"], "p1")
        self.assertEqual(first["run_id"], RUN_P1)
        self.assertTrue(first["text"])
        self.assertEqual(result["open"][1]["period"], "p2")
        self.assertEqual(set(first), {"id", "axis", "text", "period", "run_id"})

    def test_retracted_run_recs_leave_open_list_but_keep_numbers(self):
        self.append_p1()
        self.append_p2()
        tracker.retract(self.root, RUN_P2, "судья перепутал сессии", now=LATEST)
        result = tracker.open_recs(self.tracker_doc(), "alex", gated=True)
        # вердикт followed отозванного прогона больше не закрывает REC-001, REC-003 ушла вместе с ним
        self.assertEqual([r["id"] for r in result["open"]], ["REC-alex-001", "REC-alex-002"])
        self.assertEqual(result["next_number"], 4)

    def test_retracted_first_run_empties_open_list(self):
        self.append_p1()
        tracker.retract(self.root, RUN_P1, "ошибка", now=LATER)
        result = tracker.open_recs(self.tracker_doc(), "alex", gated=True)
        self.assertEqual(result, {"open": [], "pending_approval": [], "next_number": 3})

    def test_cli_open_recs_json_and_text(self):
        self.append_p1()
        code, out, err = self.cli("open-recs", "alex", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["next_number"], 3)
        code, out, err = self.cli("open-recs", "alex")
        self.assertEqual(code, 0, err)
        self.assertIn("REC-alex-001", out)
        self.assertIn("REC-alex-003", out)

    def test_cli_open_recs_unknown_person_is_2(self):
        self.assertEqual(self.code("open-recs", "nobody"), 2)


P1_RECS = ["REC-alex-001", "REC-alex-002"]


class ReviewerGateRecsTest(OpsCase):
    """B04: в team/course при `reviewer_gate` рекомендации черновика не идут судье до approve.

    Человек получает текст только после одобрения; вердикт по совету, которого он не видел,
    ложный. Фикстура workspace-min: `mode: team`, `reviewer_gate: true`.
    """

    def set_config(self, **fields) -> None:
        cfg = load_config(self.root)
        cfg.update(fields)
        save_config(self.root, cfg)

    def draft_p1(self) -> None:
        """p1 записан и отрендерен, но не одобрен: `review_status: draft`."""
        judge = self.write(payload(), name="judge-p1.json")
        tracker.append(self.root, judge, "alex", now=NOW, period_label="p1")
        render_review.write_review(self.root, judge, "alex", RUN_P1, now=NOW)

    def p2_over_draft(self, **patch) -> dict:
        """p2 без одобрения p1 (`append_p2` сначала одобряет)."""
        return self.append(self.p2(**patch), now=LATER, period_label="p2")

    def cli_recs(self) -> dict:
        code, out, err = self.cli("open-recs", "alex", "--json")
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_gated_follows_config(self):
        self.assertTrue(tracker.reviewer_gated(load_config(self.root)))
        for mode, gate, expected in (("team", False, False), ("course", True, True),
                                     ("self", True, False), ("self", False, False)):
            with self.subTest(mode=mode, gate=gate):
                self.assertEqual(tracker.reviewer_gated({"mode": mode, "reviewer_gate": gate}), expected)

    def test_draft_recs_pending_not_open(self):
        self.draft_p1()
        self.assertEqual(self.tracker_doc()["runs"][0]["review_status"], "draft")
        result = self.cli_recs()
        self.assertEqual(result["open"], [])
        self.assertEqual([r["id"] for r in result["pending_approval"]], P1_RECS)
        first = result["pending_approval"][0]
        self.assertEqual(set(first), {"id", "axis", "text", "period", "run_id"})
        self.assertEqual(first["period"], "p1")
        self.assertEqual(first["run_id"], RUN_P1)
        # номер не переиспользуется: черновые REC заняты
        self.assertEqual(result["next_number"], 3)

    def test_verdict_on_draft_rec_rejected(self):
        self.draft_p1()
        before = tracker.tracker_path(self.root).read_bytes()
        message = self.raises(1, self.p2_over_draft)
        self.assertIn(f"REC-alex-001 из неодобренного черновика {RUN_P1}", message)
        self.assertIn("вердикт откладывается до approve", message)
        self.assertEqual(tracker.tracker_path(self.root).read_bytes(), before)

    def test_after_approve_recs_open_and_verdict_passes(self):
        self.draft_p1()
        render_review.approve(self.root, "alex", RUN_P1, now=NOW + timedelta(hours=1))
        result = self.cli_recs()
        self.assertEqual([r["id"] for r in result["open"]], P1_RECS)
        self.assertEqual(result["pending_approval"], [])
        self.append_p2()
        self.assertEqual(tracker.rec_statuses(self.tracker_doc(), "alex")["REC-alex-001"], "followed")

    def test_p2_without_verdicts_passes_over_draft(self):
        """Прогон следующего периода не блокируется: без вердиктов по черновику он пишется."""
        self.draft_p1()
        self.p2_over_draft(prev_recommendations=[])
        result = tracker.open_recs(self.tracker_doc(), "alex", gated=True)
        # p2 тоже черновик: его REC-003 ждёт одобрения вместе с рекомендациями p1
        self.assertEqual([r["id"] for r in result["pending_approval"]], P1_RECS + ["REC-alex-003"])
        self.assertEqual(result["open"], [])
        self.assertEqual(result["next_number"], 4)

    def test_self_mode_recs_open_at_once(self):
        self.set_config(mode="self")
        self.draft_p1()
        self.assertEqual(self.tracker_doc()["runs"][0]["review_status"], "approved")
        result = self.cli_recs()
        self.assertEqual([r["id"] for r in result["open"]], P1_RECS)
        self.assertEqual(result["pending_approval"], [])
        self.append_p2()

    def test_team_without_gate_like_self(self):
        self.set_config(reviewer_gate=False)
        self.append_p1()  # без render-review прогон ещё draft, гейта нет — REC выданы сразу
        result = self.cli_recs()
        self.assertEqual([r["id"] for r in result["open"]], P1_RECS)
        self.assertEqual(result["pending_approval"], [])
        self.append_p2()

    def test_recorded_verdicts_on_draft_do_not_redden_validate(self):
        """Трекер, записанный до исправления (вердикт по черновику), `validate` не краснит."""
        self.set_config(reviewer_gate=False)
        self.append_p1()
        self.append_p2()
        self.set_config(reviewer_gate=True)
        self.assertEqual(tracker.validate_file(self.root), [])

    def test_cli_text_shows_both_groups(self):
        self.draft_p1()
        code, out, err = self.cli("open-recs", "alex")
        self.assertEqual(code, 0, err)
        self.assertIn("открытых рекомендаций у alex нет", out)
        self.assertIn("ждут одобрения черновика", out)
        self.assertIn("REC-alex-001", out)
        self.assertIn("следующий номер: REC-alex-003", out)


class RetractTest(OpsCase):
    def test_retract_marks_run_and_restores_index(self):
        self.append_p1()
        result = tracker.retract(self.root, RUN_P1, "  не те логи  ", now=LATER)
        self.assertEqual(result["run_id"], RUN_P1)
        self.assertEqual(result["logs_unmarked"], 3)
        self.assertEqual(result["kept_newer"], [])
        doc = self.tracker_doc()
        self.assertEqual(doc["runs"][0]["retracted"],
                         {"at": "2026-09-17T12:00:00Z", "why": "не те логи", "logs_unmarked": 3})
        self.assertEqual(doc["updated_at"], "2026-09-17T12:00:00Z")
        index = self.index()
        login = _entry(index, LOGIN)
        self.assertFalse(login["reviewed"])
        self.assertIsNone(login["reviewed_in_run"])
        self.assertEqual(login["state"], "new")
        grown = _entry(index, GROWN)
        self.assertEqual(grown["reviewed_in_run"], OLD_RUN)
        self.assertEqual(grown["state"], "grown")
        self.assertEqual(grown["review_from_turn"], 10)
        short = _entry(index, SHORT)
        self.assertFalse(short.get("excluded"))
        self.assertEqual(short["state"], "new")
        self.assertNotIn("excluded_turns_total", short)
        self.assertEqual(tracker.schema_problems(doc, "tracker"), [])

    def test_repeat_retract_is_1(self):
        self.append_p1()
        tracker.retract(self.root, RUN_P1, "ошибка", now=LATER)
        before = tracker.tracker_path(self.root).read_bytes()
        self.assertIn(RUN_P1, self.raises(1, tracker.retract, self.root, RUN_P1, "ещё раз", now=LATEST))
        self.assertEqual(tracker.tracker_path(self.root).read_bytes(), before)

    def test_unknown_run_is_1(self):
        self.append_p1()
        self.assertIn("nope", self.raises(1, tracker.retract, self.root, "nope", "ошибка"))

    def test_empty_why_is_2(self):
        self.append_p1()
        self.raises(2, tracker.retract, self.root, RUN_P1, "   ")
        self.assertNotIn("retracted", self.tracker_doc()["runs"][0])

    def test_cli_without_why_is_2_and_with_why_prints_json(self):
        self.append_p1()
        self.assertEqual(self.code("retract", RUN_P1), 2)
        code, out, err = self.cli("retract", RUN_P1, "--why", "судья ошибся")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["logs_unmarked"], 3)
        self.assertEqual(self.code("retract", RUN_P1, "--why", "ещё раз"), 1)

    def test_newer_mark_kept(self):
        self.append_p1()
        self.append_p2_with_login()
        self.assertEqual(_entry(self.index(), LOGIN)["reviewed_in_run"], RUN_P2)
        result = tracker.retract(self.root, RUN_P1, "ошибка", now=LATEST)
        self.assertEqual(result["kept_newer"], [LOGIN_KEY])
        self.assertEqual(result["logs_unmarked"], 2)
        self.assertEqual(_entry(self.index(), LOGIN)["reviewed_in_run"], RUN_P2)

    def test_dedup_cycle_retract_newer_then_older(self):
        self.append_p1()
        self.append_p2_with_login()
        tracker.retract(self.root, RUN_P2, "ошибка", now=LATEST)
        login = _entry(self.index(), LOGIN)
        self.assertEqual(login["reviewed_in_run"], RUN_P1)
        self.assertEqual(login["state"], "unchanged")
        self.assertFalse(_entry(self.index(), FIX_ALEX)["reviewed"])
        tracker.retract(self.root, RUN_P1, "тоже ошибка", now=LATEST)
        login = _entry(self.index(), LOGIN)
        self.assertFalse(login["reviewed"])
        self.assertEqual(login["state"], "new")

    def test_dedup_cycle_retract_older_then_newer_goes_back_to_new(self):
        """Прошлая пометка от уже отозванного прогона не возвращается: откат идёт дальше по цепочке."""
        self.append_p1()
        self.append_p2_with_login()
        tracker.retract(self.root, RUN_P1, "ошибка", now=LATEST)
        tracker.retract(self.root, RUN_P2, "тоже ошибка", now=LATEST)
        login = _entry(self.index(), LOGIN)
        self.assertFalse(login["reviewed"])
        self.assertIsNone(login["reviewed_in_run"])
        self.assertEqual(login["state"], "new")

    def test_retracted_run_not_validated(self):
        self.append_p1()
        tracker.retract(self.root, RUN_P1, "ошибка", now=LATER)
        doc = self.tracker_doc()
        doc["runs"][0]["axes"][0]["evidence"] = []
        self.save_doc(doc)
        self.assertEqual(tracker.validate_file(self.root), [])

    def test_retract_without_index_reports_missing(self):
        self.append_p1()
        self.paths["logs_index"].unlink()
        result = tracker.retract(self.root, RUN_P1, "ошибка", now=LATER)
        self.assertEqual(result["logs_unmarked"], 0)
        self.assertEqual(len(result["missing"]), 3)
        self.assertFalse(self.paths["logs_index"].exists())


class CompareTest(OpsCase):
    def test_compare_axes_and_flags(self):
        self.append_p1()
        self.append_p2()
        result = tracker.compare(self.tracker_doc(), RUN_P1, RUN_P2)
        axes = result["axes"]
        self.assertEqual(list(axes)[0], "verification")
        self.assertEqual(axes["verification"], {"a": 2, "b": 3, "delta": 1, "evidence_shift": False})
        self.assertEqual(axes["self_correction"], {"a": 3, "b": None, "delta": None, "evidence_shift": True})
        self.assertEqual(axes["context_engineering"], {"a": None, "b": 2, "delta": None, "evidence_shift": True})
        self.assertEqual(result["max_abs_delta"], 1)
        self.assertEqual(result["shifted_ge2"], [])
        self.assertFalse(result["level_same"])
        self.assertTrue(result["gate_same"])
        self.assertFalse(result["antipatterns_same"])

    def test_compare_counts_override(self):
        self.append_p1()
        self.append_p2()
        doc = self.tracker_doc()
        doc["runs"][1]["operator_override"] = {"axes": {"verification": 0, "self_correction": 3},
                                               "why": "пересмотр", "by": "reviewer", "at": "t"}
        result = tracker.compare(doc, RUN_P1, RUN_P2)
        self.assertEqual(result["axes"]["verification"]["delta"], -2)
        self.assertEqual(result["axes"]["self_correction"],
                         {"a": 3, "b": 3, "delta": 0, "evidence_shift": False})
        self.assertEqual(result["max_abs_delta"], 2)
        self.assertEqual(result["shifted_ge2"], ["verification"])

    def test_compare_same_run_is_identical(self):
        self.append_p1()
        result = tracker.compare(self.tracker_doc(), RUN_P1, RUN_P1)
        self.assertEqual(result["max_abs_delta"], 0)
        self.assertTrue(result["level_same"] and result["gate_same"] and result["antipatterns_same"])

    def test_compare_different_people_is_1(self):
        self.append_p1()
        obj = payload(recommendations=KIM_RECS)
        kim_run = self.append(obj, person="kim", now=LATER, period_label="p1")["run_id"]
        message = self.raises(1, tracker.compare, self.tracker_doc(), RUN_P1, kim_run)
        self.assertIn("kim", message)

    def test_compare_unknown_run_is_1(self):
        self.append_p1()
        self.raises(1, tracker.compare, self.tracker_doc(), RUN_P1, "nope")

    def test_cli_compare(self):
        self.append_p1()
        self.append_p2()
        code, out, err = self.cli("compare", RUN_P1, RUN_P2, "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["max_abs_delta"], 1)
        code, out, err = self.cli("compare", RUN_P1, RUN_P2)
        self.assertEqual(code, 0, err)
        self.assertIn("verification", out)
        self.assertIn("self_correction", out)


class MarkLogsTest(OpsCase):
    def append_without_index(self) -> None:
        saved = self.paths["logs_index"].read_bytes()
        self.paths["logs_index"].unlink()
        self.append_p1()
        self.paths["logs_index"].write_bytes(saved)

    def test_catches_up_coverage(self):
        self.append_without_index()
        result = tracker.mark_logs(self.root, RUN_P1, now=LATER)
        self.assertEqual(result["marked"], 3)
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["coverage"], {"marked": 3, "expected": 3})
        index = self.index()
        login = _entry(index, LOGIN)
        self.assertEqual(login["reviewed_in_run"], RUN_P1)
        self.assertEqual(login["reviewed_at"], "2026-09-17T12:00:00Z")
        self.assertEqual(_entry(index, SHORT)["excluded_reason"], "transcript_too_short")
        self.assertEqual(index["updated_at"], "2026-09-17T12:00:00Z")
        doc = self.tracker_doc()
        self.assertEqual(len(doc["runs"][0]["logs_index_marks"]), 3)
        self.assertEqual(doc["updated_at"], "2026-09-17T12:00:00Z")

    def test_already_marked_without_force_adds_nothing_and_writes_nothing(self):
        self.append_p1()
        before_tr = tracker.tracker_path(self.root).read_bytes()
        before_ix = self.paths["logs_index"].read_bytes()
        result = tracker.mark_logs(self.root, RUN_P1, now=LATER)
        self.assertEqual(result["marked"], 0)
        self.assertEqual(result["coverage"], {"marked": 3, "expected": 3})
        self.assertEqual(tracker.tracker_path(self.root).read_bytes(), before_tr)
        self.assertEqual(self.paths["logs_index"].read_bytes(), before_ix)

    def test_additive_catch_up_after_partial_append(self):
        """append пометил 2 из 3 → mark-logs --excluded третьей без --force → покрытие 3/3, код 0."""
        index = self.index()
        short = _entry(index, SHORT)
        index["logs"] = [e for e in index["logs"] if e["session_id"] != SHORT]
        self.paths["logs_index"].write_text(json.dumps(index), encoding="utf-8")
        result = self.append_p1()
        self.assertEqual(result["coverage"], {"marked": 2, "expected": 3})
        index = self.index()
        index["logs"].append(short)  # сбор логов снова увидел сессию
        self.paths["logs_index"].write_text(json.dumps(index), encoding="utf-8")
        code, out, err = self.cli("mark-logs", RUN_P1, "--excluded", f"{SHORT}=transcript_too_short")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["coverage"], {"marked": 3, "expected": 3})
        self.assertEqual(json.loads(out)["marked"], 1)
        self.assertEqual(len(self.tracker_doc()["runs"][0]["logs_index_marks"]), 3)
        self.assertEqual(_entry(self.index(), SHORT)["excluded_reason"], "transcript_too_short")
        # прежние пометки не переставлены: prev у LOGIN всё ещё состояние до p1
        tracker.retract(self.root, RUN_P1, "ошибка", now=LATEST)
        self.assertEqual(_entry(self.index(), LOGIN)["state"], "new")
        self.assertEqual(_entry(self.index(), SHORT)["state"], "new")

    def test_additive_conflict_is_1(self):
        self.append_p1()
        before = self.paths["logs_index"].read_bytes()
        message = self.raises(1, tracker.mark_logs, self.root, RUN_P1, excluded=[f"{LOGIN}=not_batched"], now=LATER)
        self.assertIn("--force", message)
        self.assertIn(LOGIN_KEY, message)
        self.assertEqual(self.paths["logs_index"].read_bytes(), before)

    def test_force_on_older_run_keeps_newer_mark(self):
        """p1 и p2 оценили LOGIN (штамп p2). mark-logs p1 --force его не перетирает; цепочка цела."""
        self.append_p1()
        self.append_p2_with_login()
        result = tracker.mark_logs(self.root, RUN_P1, force=True, now=LATEST)
        self.assertEqual(result["kept_newer"], [LOGIN_KEY])
        self.assertEqual(result["coverage"], {"marked": 3, "expected": 3})
        login = _entry(self.index(), LOGIN)
        self.assertEqual(login["reviewed_in_run"], RUN_P2)
        tracker.retract(self.root, RUN_P2, "ошибка", now=LATEST)
        self.assertEqual(_entry(self.index(), LOGIN)["reviewed_in_run"], RUN_P1)

    def test_older_run_without_marks_does_not_overwrite_newer(self):
        """Путь без --force: у p1 пометок не было (индекса не было), p2 уже поставил свой штамп."""
        self.append_without_index()
        self.append_p2_with_login()
        result = tracker.mark_logs(self.root, RUN_P1, now=LATEST)
        self.assertEqual(result["kept_newer"], [LOGIN_KEY])
        self.assertEqual(result["coverage"], {"marked": 3, "expected": 3})
        self.assertEqual(_entry(self.index(), LOGIN)["reviewed_in_run"], RUN_P2)
        tracker.retract(self.root, RUN_P1, "ошибка", now=LATEST)
        self.assertEqual(_entry(self.index(), LOGIN)["reviewed_in_run"], RUN_P2)

    def test_replace_older_run_keeps_newer_mark(self):
        self.append_p1()
        self.append_p2_with_login()
        result = self.append(payload(), now=LATEST, period_label="p1", replace=RUN_P1)
        self.assertEqual(result["logs_kept_newer"], [LOGIN_KEY])
        self.assertEqual(result["coverage"], {"marked": 3, "expected": 3})
        self.assertEqual(_entry(self.index(), LOGIN)["reviewed_in_run"], RUN_P2)
        tracker.retract(self.root, RUN_P2, "ошибка", now=LATEST)
        login = _entry(self.index(), LOGIN)
        self.assertEqual(login["reviewed_in_run"], RUN_P1)
        self.assertEqual(login["state"], "unchanged")

    def test_force_remarks(self):
        self.append_p1()
        result = tracker.mark_logs(self.root, RUN_P1, force=True, now=LATER)
        self.assertEqual(result["marked"], 3)
        run = self.tracker_doc()["runs"][0]
        self.assertEqual(len(run["logs_index_marks"]), 3)
        grown_mark = next(m for m in run["logs_index_marks"] if m["session_id"] == GROWN)
        # откат старой пометки перед новой: prev снова указывает на прошлую оценку
        self.assertEqual(grown_mark["prev"]["reviewed_in_run"], OLD_RUN)
        tracker.retract(self.root, RUN_P1, "ошибка", now=LATEST)
        self.assertEqual(_entry(self.index(), GROWN)["reviewed_in_run"], OLD_RUN)

    def test_unknown_explicit_session_is_1_and_nothing_written(self):
        self.append_without_index()
        before_tr = tracker.tracker_path(self.root).read_bytes()
        before_ix = self.paths["logs_index"].read_bytes()
        message = self.raises(1, tracker.mark_logs, self.root, RUN_P1, sessions=["no-such-session"])
        self.assertIn("no-such-session", message)
        self.assertEqual(tracker.tracker_path(self.root).read_bytes(), before_tr)
        self.assertEqual(self.paths["logs_index"].read_bytes(), before_ix)

    def test_session_missing_in_index_reported_and_cli_1(self):
        self.append_without_index()
        index = self.index()
        index["logs"] = [e for e in index["logs"] if e["session_id"] != LOGIN]
        self.paths["logs_index"].write_text(json.dumps(index), encoding="utf-8")
        result = tracker.mark_logs(self.root, RUN_P1, force=True, now=LATER)
        self.assertEqual(result["missing"], [LOGIN])
        self.assertEqual(result["coverage"], {"marked": 2, "expected": 3})
        code, out, err = self.cli("mark-logs", RUN_P1, "--force")
        self.assertEqual(code, 1)
        self.assertIn(LOGIN, out + err)

    def test_explicit_sessions_and_excluded_with_reason(self):
        self.append_without_index()
        code, out, err = self.cli("mark-logs", RUN_P1, "--excluded", f"{FIX_ALEX}=duplicate_export",
                                  "--exclude-note", "та же выгрузка второй раз")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["marked"], 4)
        fix = _entry(self.index(), FIX_ALEX)
        self.assertTrue(fix["excluded"])
        self.assertEqual(fix["excluded_reason"], "duplicate_export")
        self.assertEqual(fix["excluded_note"], "та же выгрузка второй раз")

    def test_excluded_default_reason_and_path_ref(self):
        self.append_without_index()
        tracker.mark_logs(self.root, RUN_P1, excluded=[f"logs/alex/{FIX_KEY}.md"], now=LATER)
        self.assertEqual(_entry(self.index(), FIX_ALEX)["excluded_reason"], "not_batched")

    def test_explicit_sessions_mark_reviewed(self):
        self.append_without_index()
        result = tracker.mark_logs(self.root, RUN_P1, sessions=[FIX_ALEX[:8]], now=LATER)
        self.assertEqual(result["marked"], 4)
        self.assertEqual(_entry(self.index(), FIX_ALEX)["reviewed_in_run"], RUN_P1)

    def test_unknown_exclude_reason_is_2(self):
        self.append_without_index()
        self.raises(2, tracker.mark_logs, self.root, RUN_P1, excluded=[f"{FIX_ALEX}=bored"])

    def test_retracted_run_is_1(self):
        self.append_p1()
        tracker.retract(self.root, RUN_P1, "ошибка", now=LATER)
        self.raises(1, tracker.mark_logs, self.root, RUN_P1, force=True)

    def test_no_index_is_1(self):
        self.append_p1()
        self.paths["logs_index"].unlink()
        self.raises(1, tracker.mark_logs, self.root, RUN_P1, force=True)


KIM_SHORT = [f"c0ffee0{i}-1a2b-4c3d-8e4f-5a6b7c8d9e0{i}" for i in range(5)]


class MarkLogsNoRunTest(OpsCase):
    """B03: период, где все новые сессии человека короче `min_turns`, закрывается без прогона."""

    def setUp(self) -> None:
        super().setUp()
        index = self.index()
        base = _entry(index, SHORT)
        for i, sid in enumerate(KIM_SHORT):
            key = f"2026-09-0{i + 1}_10-00-00Z-short-{i}"
            entry = json.loads(json.dumps(base))
            entry.update({"person_id": "kim", "session_id": sid, "key": key, "path": f"logs/kim/{key}.md",
                          "source_path": f"/home/user/demo/.specstory/history/{key}.md",
                          "user_turns": 1 + i % 3, "turns_total": 2 + 2 * (i % 3), "state": "new"})
            index["logs"].append(entry)
        self.paths["logs_index"].write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")

    def kim_batches(self) -> dict:
        return batches.for_person(self.root, "kim", measure="index", period_label="p1")

    def no_run(self, *refs: str, person: str = "kim") -> tuple[int, str, str]:
        return self.cli("mark-logs", "--no-run", "--person", person, "--excluded", *refs)

    def review_file(self, run_id: str = RUN_P1) -> None:
        path = self.paths["reviews"] / "alex" / f"{run_id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Текст ревью.\n", encoding="utf-8")

    def test_b03_all_short_period_closed_without_run(self):
        before = self.kim_batches()
        self.assertEqual(before["batches"], [])
        self.assertEqual(sorted(s["session_id"] for s in before["short"]), KIM_SHORT)
        code, out, err = self.no_run(*(f"{sid}=transcript_too_short" for sid in KIM_SHORT))
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(result["person_id"], "kim")
        self.assertEqual(result["missing"], [])
        self.assertEqual(sorted(m["session_id"] for m in result["marked"]), KIM_SHORT)
        index = self.index()
        for sid in KIM_SHORT:
            entry = _entry(index, sid)
            self.assertTrue(entry["excluded"])
            self.assertEqual(entry["excluded_reason"], "transcript_too_short")
            self.assertIsNone(entry["excluded_in_run"])
            self.assertRegex(entry["excluded_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
            self.assertEqual(entry["excluded_turns_total"], entry["turns_total"])
            self.assertEqual(entry["state"], "excluded")
            self.assertFalse(entry["reviewed"])
        after = self.kim_batches()
        self.assertEqual((after["batches"], after["short"], after["skipped"]), ([], [], []))
        self.assertFalse(tracker.tracker_path(self.root).exists(), "прогон без оценки не пишется")
        self.assertEqual(tracker.verify(self.root), [])
        self.assertEqual(self.code("verify"), 0)
        self.assertEqual(self.code("validate"), 0)

    def test_default_reason_is_too_short_and_prefix_ref(self):
        code, out, err = self.no_run(KIM_SHORT[0][:8], f"logs/kim/2026-09-02_10-00-00Z-short-1.md")
        self.assertEqual(code, 0, err)
        self.assertEqual([m["reason"] for m in json.loads(out)["marked"]], ["transcript_too_short"] * 2)
        self.assertEqual(_entry(self.index(), KIM_SHORT[1])["excluded_reason"], "transcript_too_short")

    def test_other_short_reasons_allowed(self):
        code, _, err = self.no_run(f"{KIM_SHORT[0]}=empty_transcript", f"{KIM_SHORT[1]}=unreadable_input")
        self.assertEqual(code, 0, err)
        self.assertEqual(_entry(self.index(), KIM_SHORT[0])["excluded_reason"], "empty_transcript")

    def test_reason_outside_no_run_set_is_2_and_nothing_written(self):
        before = self.paths["logs_index"].read_bytes()
        for reason in ("not_batched", "duplicate_export", "other_period", "bored"):
            with self.subTest(reason=reason):
                code, _, err = self.no_run(KIM_SHORT[0], f"{KIM_SHORT[1]}={reason}")
                self.assertEqual(code, 2, err)
                self.assertIn(reason, err)
        self.assertEqual(self.paths["logs_index"].read_bytes(), before)

    def test_other_persons_session_is_1_and_nothing_written(self):
        before = self.paths["logs_index"].read_bytes()
        code, out, err = self.no_run(KIM_SHORT[0], SHORT)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out), {"person_id": "kim", "marked": [], "missing": [SHORT]})
        self.assertIn(SHORT, err)
        self.assertEqual(self.paths["logs_index"].read_bytes(), before)

    def test_already_marked_is_1_and_nothing_written(self):
        self.assertEqual(self.no_run(KIM_SHORT[0])[0], 0)
        before = self.paths["logs_index"].read_bytes()
        code, _, err = self.no_run(KIM_SHORT[1], KIM_SHORT[0])
        self.assertEqual(code, 1)
        self.assertIn(KIM_SHORT[0], err)
        self.assertEqual(self.paths["logs_index"].read_bytes(), before)
        # оценённая прогоном (reviewed) тоже не перетирается
        code, _, err = self.no_run(GROWN, person="alex")
        self.assertEqual(code, 1)
        self.assertIn(GROWN, err)

    def test_argument_combinations_are_2(self):
        before = self.paths["logs_index"].read_bytes()
        cases = [
            ("mark-logs", "--no-run", RUN_P1, "--person", "kim", "--excluded", KIM_SHORT[0]),
            ("mark-logs", "--no-run", "--person", "kim", "--sessions", KIM_SHORT[1], "--excluded", KIM_SHORT[0]),
            ("mark-logs", RUN_P1, "--person", "kim", "--excluded", KIM_SHORT[0]),
            ("mark-logs", "--no-run", "--excluded", KIM_SHORT[0]),
            ("mark-logs", "--no-run", "--person", "kim"),
            ("mark-logs", "--no-run", "--person", "kim", "--force", "--excluded", KIM_SHORT[0]),
            ("mark-logs", "--excluded", KIM_SHORT[0]),
        ]
        for args in cases:
            with self.subTest(args=args):
                code, _, err = self.cli(*args)
                self.assertEqual(code, 2, err)
                self.assertTrue(err.strip())
        self.assertEqual(self.paths["logs_index"].read_bytes(), before)

    def test_grown_session_is_candidate_again(self):
        """Рост после пометки: настоящий `inventory.merge` снимает исключение, сессия снова в нарезке."""
        self.assertEqual(self.no_run(*KIM_SHORT)[0], 0)
        index = self.index()
        fresh = [{k: v for k, v in e.items() if k not in marks.MARK_FIELDS}
                 for e in index["logs"] if e["state"] != "gone"]
        grow = {KIM_SHORT[0]: (5, 12), KIM_SHORT[1]: (3, 6)}  # в пачку и снова короткая
        for e in fresh:
            if e["session_id"] in grow:
                e["user_turns"], e["turns_total"] = grow[e["session_id"]]
        merged = inventory.merge(index, fresh, "demo-team", "2026-09-20T10:00:00Z")
        marks.save_index(self.root, merged)
        self.assertEqual(_entry(self.index(), KIM_SHORT[0])["state"], "new")
        self.assertNotIn("excluded", _entry(self.index(), KIM_SHORT[0]))
        self.assertEqual(_entry(self.index(), KIM_SHORT[2])["state"], "excluded")
        result = self.kim_batches()
        self.assertEqual([[s["session_id"] for s in b["sessions"]] for b in result["batches"]], [[KIM_SHORT[0]]])
        self.assertEqual([s["session_id"] for s in result["short"]], [KIM_SHORT[1]])
        # короткую снова можно закрыть без прогона
        self.assertEqual(self.no_run(KIM_SHORT[1])[0], 0)

    def test_verify_clean_and_retract_leaves_no_run_marks(self):
        self.append_p1()
        self.review_file()
        tracker.set_level(self.root, RUN_P1, now=LATER)
        self.assertEqual(self.no_run(*KIM_SHORT)[0], 0)
        self.assertEqual(tracker.verify(self.root), [])
        self.assertEqual(self.code("validate"), 0)
        before = {sid: _entry(self.index(), sid) for sid in KIM_SHORT}
        tracker.retract(self.root, RUN_P1, "ошибка", now=LATEST)
        self.assertEqual({sid: _entry(self.index(), sid) for sid in KIM_SHORT}, before)
        self.assertEqual(_entry(self.index(), LOGIN)["state"], "new")

    def test_busy_collect_lock_is_1_and_nothing_written(self):
        """Индекс пишется под замком сбора логов (`collect.lock`), как у обычного `mark-logs`."""
        before = self.paths["logs_index"].read_bytes()
        with file_lock(self.paths["state_dir"] / marks.COLLECT_LOCK, timeout=0), \
                mock.patch.object(marks, "INDEX_LOCK_TIMEOUT", 0.3):
            code, _, err = self.no_run(KIM_SHORT[0])
        self.assertEqual(code, 1)
        self.assertIn("collect.lock", err)
        self.assertEqual(self.paths["logs_index"].read_bytes(), before)


class VerifyTest(OpsCase):
    def review_file(self, run_id: str = RUN_P1, text: str = "Текст ревью.\n"):
        path = self.paths["reviews"] / "alex" / f"{run_id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_fresh_run_lacks_review_and_level(self):
        self.append_p1()
        problems = "\n".join(tracker.verify(self.root))
        self.assertIn(f"reviews/alex/{RUN_P1}.md", problems)
        self.assertIn("level_computed", problems)

    def test_complete_run_is_clean(self):
        self.append_p1()
        self.review_file()
        tracker.set_level(self.root, RUN_P1, now=LATER)
        self.assertEqual(tracker.verify(self.root), [])
        self.assertEqual(self.code("verify"), 0)

    def test_empty_review_file_is_problem(self):
        self.append_p1()
        self.review_file(text="  \n")
        tracker.set_level(self.root, RUN_P1, now=LATER)
        self.assertTrue(any("пуст" in p for p in tracker.verify(self.root)))

    def test_coverage_gap(self):
        self.append_p1()
        self.review_file()
        tracker.set_level(self.root, RUN_P1, now=LATER)
        doc = self.tracker_doc()
        doc["runs"][0]["logs_index_marks"] = doc["runs"][0]["logs_index_marks"][:1]
        self.save_doc(doc)
        problems = tracker.verify(self.root)
        self.assertTrue(any("1 из 3" in p for p in problems), problems)

    def test_orphan_judge_caught_stub_and_recorded_not(self):
        self.append_p1()
        self.review_file()
        tracker.set_level(self.root, RUN_P1, now=LATER)
        self.write({"stub": True}, person="kim")
        orphan = self.write(payload("judge-alex-p2.json"), person="sam")
        problems = tracker.verify(self.root)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("runs/pending/sam/judge.json", problems[0])
        self.assertNotIn(str(orphan.parent.parent.parent.parent), problems[0])
        code, out, err = self.cli("verify")
        self.assertEqual(code, 1)
        self.assertIn("sam", out)

    def test_retracted_runs_skipped(self):
        self.append_p1()
        tracker.retract(self.root, RUN_P1, "ошибка", now=LATER)
        self.assertEqual(tracker.verify(self.root), [])

    def test_no_tracker_is_clean(self):
        self.assertEqual(tracker.verify(self.root), [])


class LevelAndUpdateTest(OpsCase):
    def test_set_level_writes_field(self):
        self.append_p1()
        result = tracker.set_level(self.root, RUN_P1, now=LATER)
        self.assertEqual(result, {"run_id": RUN_P1, "level": "competent", "median": 2,
                                  "values": [2, 3, 2, 3, 2, 2, 3, 2]})
        doc = self.tracker_doc()
        self.assertEqual(doc["runs"][0]["level_computed"], "competent")
        self.assertEqual(doc["updated_at"], "2026-09-17T12:00:00Z")

    def test_set_level_uses_override(self):
        self.append_p1()
        doc = self.tracker_doc()
        doc["runs"][0]["operator_override"] = {"axes": {"verification": 4, "ownership": 4, "decomposition": 4,
                                                        "trust_calibration": 4},
                                               "why": "пересмотр", "by": "reviewer", "at": "t"}
        self.save_doc(doc)
        self.assertEqual(tracker.set_level(self.root, RUN_P1)["level"], "orchestrator")

    def test_cli_level_prints_json(self):
        self.append_p1()
        code, out, err = self.cli("level", RUN_P1)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["level"], "competent")
        self.assertEqual(self.tracker_doc()["runs"][0]["level_computed"], "competent")
        self.assertEqual(self.code("level", "nope"), 1)

    def test_update_run_sets_fields_and_updated_at(self):
        self.append_p1()
        run = tracker.update_run(self.root, RUN_P1, now=LATER, review_status="approved",
                                 feedback_draft="Готовый текст.")
        self.assertEqual(run["review_status"], "approved")
        doc = self.tracker_doc()
        self.assertEqual(doc["runs"][0]["feedback_draft"], "Готовый текст.")
        self.assertEqual(doc["updated_at"], "2026-09-17T12:00:00Z")

    def test_update_run_rejects_unknown_and_identity_fields(self):
        self.append_p1()
        before = tracker.tracker_path(self.root).read_bytes()
        self.assertIn("colour", self.raises(2, tracker.update_run, self.root, RUN_P1, colour="red"))
        self.raises(2, tracker.update_run, self.root, RUN_P1, run_id="other")
        self.raises(2, tracker.update_run, self.root, RUN_P1, person_id="sam")
        self.raises(2, tracker.update_run, self.root, RUN_P1)
        self.assertEqual(tracker.tracker_path(self.root).read_bytes(), before)

    def test_update_run_value_checked_by_schema(self):
        self.append_p1()
        before = tracker.tracker_path(self.root).read_bytes()
        message = self.raises(1, tracker.update_run, self.root, RUN_P1, review_status="sent")
        self.assertIn("review_status", message)
        self.assertEqual(tracker.tracker_path(self.root).read_bytes(), before)

    def test_update_run_retracted_or_unknown_is_1(self):
        self.append_p1()
        self.raises(1, tracker.update_run, self.root, "nope", review_status="approved")
        tracker.retract(self.root, RUN_P1, "ошибка", now=LATER)
        self.raises(1, tracker.update_run, self.root, RUN_P1, review_status="approved")


if __name__ == "__main__":
    unittest.main()
