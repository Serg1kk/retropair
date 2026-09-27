"""`lib/level.py`: формула уровня по медиане осей, потолок гейта, правка баллов ревьюером."""

from __future__ import annotations

import unittest

import _util  # noqa: F401 — кладёт scripts/ в sys.path
from _assess import payload

from lib import i18n, level
from lib.workspace import SrError

AXES = i18n.keys("axes")


def make_run(scores: dict, *, triggers=(), override=None) -> dict:
    """Прогон с осями: значение `None` — ось без данных (`insufficient_evidence`)."""
    axes = []
    for axis in AXES:
        score = scores.get(axis)
        axes.append({
            "id": axis, "score": score, "insufficient_evidence": score is None,
            "insufficient_evidence_reason": "мало данных" if score is None else None,
            "evidence": [], "subcriteria": {}, "note": "",
        })
    return {
        "run_id": "demo-team_alex_p1_20260910T120000Z",
        "axes": axes,
        "gating_evidence": [
            {"trigger": t, "session_id": None, "turn_index": None, "quote": "q"} for t in triggers
        ],
        "operator_override": override,
    }


class LevelFromValuesTest(unittest.TestCase):
    def check(self, median, expected, triggers=0):
        self.assertEqual(level.level_from_values([median] * 3, 3, triggers), expected, median)

    def test_thresholds(self):
        self.check(4.0, "orchestrator")
        self.check(3.5, "orchestrator")
        self.check(3.49, "confident_operator")
        self.check(2.75, "confident_operator")
        self.check(2.74, "competent")
        self.check(2.0, "competent")
        self.check(1.99, "naive")
        self.check(1.0, "naive")
        self.check(0.99, "passenger")
        self.check(0.0, "passenger")

    def test_median_of_even_list(self):
        self.assertEqual(level.level_from_values([2.5, 3.0, 2.5, 3.0], 4, 0), "confident_operator")

    def test_fewer_than_three_scored_is_insufficient(self):
        self.assertEqual(level.level_from_values([4, 4, 4], 2, 0), "insufficient")
        self.assertEqual(level.level_from_values([], 0, 2), "insufficient")

    def test_one_trigger_caps_at_competent(self):
        self.check(3.6, "competent", triggers=1)
        self.check(2.8, "competent", triggers=1)
        self.check(2.0, "competent", triggers=1)
        self.check(1.5, "naive", triggers=1)

    def test_two_triggers_developing(self):
        self.check(3.6, "developing", triggers=2)
        self.check(2.0, "developing", triggers=3)

    def test_two_triggers_keep_lower_levels_under_ceiling(self):
        self.check(1.5, "naive", triggers=2)
        self.check(0.5, "passenger", triggers=2)

    def test_gate_ceiling_sets(self):
        self.assertEqual(level.GATE_CEILING[1], {"passenger", "naive", "competent", "developing"})
        self.assertEqual(level.GATE_CEILING[2], {"passenger", "naive", "developing"})

    def test_levels_are_i18n_keys(self):
        keys = set(i18n.keys("levels"))
        for median in (0.5, 1.5, 2.5, 3.0, 3.8):
            for triggers in (0, 1, 2):
                self.assertIn(level.level_from_values([median] * 3, 3, triggers), keys)

    def test_bad_input_code2(self):
        with self.assertRaises(SrError) as ctx:
            level.level_from_values([], 3, 0)
        self.assertEqual(ctx.exception.code, 2)
        with self.assertRaises(SrError):
            level.level_from_values([2, 2, 2], 3, -1)


class ComputeTest(unittest.TestCase):
    def test_verification_is_doubled(self):
        run = make_run({"verification": 4, "task_framing": 2, "ownership": 2, "self_correction": 3})
        result = level.compute(run)
        self.assertEqual(sorted(result["values"]), [2, 2, 3, 4, 4])
        self.assertEqual(result["median"], 3)
        self.assertEqual(result["level"], "confident_operator")

    def test_insufficient_verification_not_doubled(self):
        run = make_run({"verification": None, "task_framing": 2, "ownership": 3, "self_correction": 4})
        result = level.compute(run)
        self.assertEqual(sorted(result["values"]), [2, 3, 4])
        self.assertEqual(result["level"], "confident_operator")

    def test_two_scored_axes_insufficient(self):
        run = make_run({"verification": 4, "task_framing": 4})
        result = level.compute(run)
        self.assertEqual(result["level"], "insufficient")
        self.assertEqual(sorted(result["values"]), [4, 4, 4])

    def test_no_scored_axes(self):
        result = level.compute(make_run({}))
        self.assertEqual(result, {"level": "insufficient", "median": None, "values": []})

    def test_one_distinct_trigger_caps(self):
        run = make_run({a: 4 for a in AXES[:5]}, triggers=("G2", "G2"))
        self.assertEqual(level.compute(run)["level"], "competent")

    def test_two_distinct_triggers_developing(self):
        run = make_run({a: 4 for a in AXES[:5]}, triggers=("G1", "G4"))
        self.assertEqual(level.compute(run)["level"], "developing")

    def test_override_changes_level(self):
        scores = {"verification": 2, "task_framing": 2, "ownership": 2, "self_correction": 2}
        self.assertEqual(level.compute(make_run(scores))["level"], "competent")
        override = {"axes": {"verification": 4, "task_framing": 4, "ownership": 4},
                    "why": "пропущены проверки", "by": "lead", "at": "2026-09-11T10:00:00Z"}
        result = level.compute(make_run(scores, override=override))
        self.assertEqual(sorted(result["values"]), [2, 4, 4, 4, 4])
        self.assertEqual(result["level"], "orchestrator")

    def test_override_scores_insufficient_axis(self):
        scores = {"task_framing": 2, "ownership": 2}
        override = {"axes": {"verification": 3}, "why": "видел проверку", "by": "lead",
                    "at": "2026-09-11T10:00:00Z"}
        result = level.compute(make_run(scores, override=override))
        self.assertEqual(sorted(result["values"]), [2, 2, 3, 3])
        self.assertEqual(result["level"], "competent")

    def test_override_unknown_axis_code1(self):
        override = {"axes": {"charisma": 3}, "why": "x", "by": "lead", "at": "2026-09-11T10:00:00Z"}
        with self.assertRaises(SrError) as ctx:
            level.compute(make_run({"verification": 2}, override=override))
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("charisma", ctx.exception.message)

    def test_effective_scores(self):
        override = {"axes": {"ownership": 1}, "why": "x", "by": "lead", "at": "2026-09-11T10:00:00Z"}
        scores = level.effective_scores(make_run({"verification": 3, "ownership": 3}, override=override))
        self.assertEqual(list(scores), AXES)
        self.assertEqual(scores["verification"], 3)
        self.assertEqual(scores["ownership"], 1)
        self.assertIsNone(scores["task_framing"])

    def test_judge_fixture_data_computes(self):
        data = payload()["data"]
        result = level.compute(data)
        self.assertIn(result["level"], set(i18n.keys("levels")))


if __name__ == "__main__":
    unittest.main()
