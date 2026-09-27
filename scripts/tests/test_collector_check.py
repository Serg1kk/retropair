"""`lib/collector_check.py` и команда `check-collector`: выход коллектора до передачи судье —
поля оценки на любой глубине, схема `collector`, смысловые сходимости, коды по файлам."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _util  # noqa: F401 — кладёт scripts/ в sys.path
from _assess import ASSESS

import sr
from lib import collector_check, schema
from lib.workspace import SrError

AXES = ["verification", "task_framing", "ownership", "self_correction", "context_engineering",
        "decomposition", "trust_calibration", "reversibility"]


def batch() -> dict:
    return json.loads((ASSESS / "batch-ok.json").read_text(encoding="utf-8"))


class CollectorCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="sr-collector-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def problems(self, obj) -> list[str]:
        return collector_check.check_batch(obj)

    def assertProblem(self, obj, needle: str) -> None:
        found = self.problems(obj)
        self.assertTrue(any(needle in p for p in found), f"нет «{needle}» в {found}")

    def put(self, name: str, obj) -> str:
        path = self.dir / name
        text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
        path.write_text(text, encoding="utf-8")
        return str(path)


class CheckBatchTest(CollectorCase):
    def test_valid_batch_passes(self):
        self.assertEqual(self.problems(batch()), [])

    def test_forbidden_set_is_exact(self):
        self.assertEqual(collector_check.FORBIDDEN, {
            "score", "scores", "axes", "overall_level", "level", "feedback_draft", "feedback_parts",
            "recommendations", "insufficient_evidence", "gating_triggered", "verdict"})

    def test_each_forbidden_key_rejected_deep_inside(self):
        for key in sorted(collector_check.FORBIDDEN):
            with self.subTest(key=key):
                obj = batch()
                obj["observations"][1][key] = 3
                found = self.problems(obj)
                self.assertTrue(any(f"$.observations[1].{key}" in p and "не ставит" in p for p in found),
                                found)

    def test_forbidden_key_on_top_level_rejected(self):
        obj = batch()
        obj["axes"] = [{"id": "verification", "score": 2}]
        found = self.problems(obj)
        self.assertTrue(any(p.startswith("$.axes:") for p in found), found)
        self.assertTrue(any("$.axes[0].score" in p for p in found), found)

    def test_observation_without_session_id(self):
        obj = batch()
        del obj["observations"][0]["session_id"]
        self.assertProblem(obj, "session_id")

    def test_observation_with_null_session_id(self):
        obj = batch()
        obj["observations"][2]["session_id"] = None
        self.assertProblem(obj, "$.observations[2]: наблюдение без session_id")

    def test_session_status_reviewed_rejected(self):
        obj = batch()
        obj["sessions"][0]["status"] = "reviewed"
        self.assertProblem(obj, "$.sessions[0].status")

    def test_axes_unobserved_required(self):
        obj = batch()
        del obj["axes_unobserved"]
        self.assertProblem(obj, "axes_unobserved")

    def test_axis_not_covered(self):
        obj = batch()
        obj["observations"] = [o for o in obj["observations"] if o["axis"] != "ownership"]
        self.assertProblem(obj, "не покрыты")
        self.assertProblem(obj, "ownership")

    def test_axis_in_both_lists(self):
        obj = batch()
        obj["axes_unobserved"].append({"axis": "verification", "reason": "behaviour_absent", "note": None})
        self.assertProblem(obj, "одновременно")

    def test_axis_listed_twice(self):
        obj = batch()
        obj["axes_unobserved"].append({"axis": "context_engineering", "reason": "short_log", "note": None})
        self.assertProblem(obj, "дважды")

    def test_index_greater_than_total(self):
        obj = batch()
        obj["batch"]["index"] = 3
        obj["batch"]["total"] = 2
        self.assertProblem(obj, "index 3 больше total 2")

    def test_old_of_field_rejected_by_real_schema(self):
        obj = batch()
        obj["batch"]["of"] = obj["batch"].pop("total")
        self.assertProblem(obj, "total")
        self.assertProblem(obj, "'of'")

    def test_period_is_object_with_anchor(self):
        obj = batch()
        obj["batch"]["period"] = "p1"
        self.assertProblem(obj, "$.batch.period")

    def test_success_with_ok_false(self):
        obj = batch()
        obj["status"]["ok"] = False
        self.assertProblem(obj, "не сходится")

    def test_failure_code_with_ok_true(self):
        obj = batch()
        obj["status"]["code"] = "empty_transcript"
        self.assertProblem(obj, "не сходится")

    def test_unobserved_without_reason(self):
        obj = batch()
        del obj["axes_unobserved"][0]["reason"]
        self.assertProblem(obj, "reason")

    def test_stub_rejected(self):
        found = self.problems({"stub": True})
        self.assertEqual(len(found), 1)
        self.assertIn("стаб не заполнен", found[0])

    def test_not_an_object(self):
        self.assertProblem([1, 2], "не объект")

    def test_wrong_block_types_do_not_crash(self):
        obj = batch()
        obj["observations"] = {"axis": "verification"}
        obj["batch"] = [1]
        found = self.problems(obj)
        self.assertTrue(any("$.observations" in p for p in found), found)
        self.assertTrue(any("$.batch" in p for p in found), found)

    def test_missing_schema_is_error_1_with_path(self):
        missing = self.dir / "nope" / "collector.json"
        real = schema.schema_path
        with mock.patch.object(schema, "schema_path",
                               side_effect=lambda name: missing if name == "collector" else real(name)):
            with self.assertRaises(SrError) as ctx:
                self.problems(batch())
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn(str(missing), ctx.exception.message)


class CheckFilesTest(CollectorCase):
    def test_all_valid_is_0(self):
        code, results = collector_check.check_files([self.put("b1.json", batch()), self.put("b2.json", batch())])
        self.assertEqual(code, 0)
        self.assertEqual([r["problems"] for r in results], [[], []])

    def test_one_bad_is_1(self):
        bad = batch()
        bad["observations"][0]["score"] = 2
        code, results = collector_check.check_files([self.put("ok.json", batch()), self.put("bad.json", bad)])
        self.assertEqual(code, 1)
        self.assertEqual(results[0]["problems"], [])
        self.assertTrue(results[1]["problems"])
        self.assertTrue(results[1]["path"].endswith("bad.json"))

    def test_only_missing_is_2(self):
        code, results = collector_check.check_files([str(self.dir / "a.json"), str(self.dir / "b.json")])
        self.assertEqual(code, 2)
        self.assertEqual([r["problems"] for r in results], [["файла нет"], ["файла нет"]])

    def test_missing_and_bad_is_1(self):
        bad = batch()
        bad["level"] = "competent"
        code, _ = collector_check.check_files([str(self.dir / "a.json"), self.put("bad.json", bad)])
        self.assertEqual(code, 1)

    def test_broken_json_is_1(self):
        code, results = collector_check.check_files([self.put("broken.json", "{oops")])
        self.assertEqual(code, 1)
        self.assertIn("не разбирается", results[0]["problems"][0])

    def test_problems_capped_with_total(self):
        bad = batch()
        for obs in bad["observations"]:
            obs["score"] = 1
            obs["verdict"] = "x"
            obs["level"] = "x"
            obs["scores"] = []
        code, results = collector_check.check_files([self.put("bad.json", bad)])
        self.assertEqual(code, 1)
        self.assertLessEqual(len(results[0]["problems"]), collector_check.MAX_PROBLEMS)
        self.assertGreater(results[0]["problems_total"], collector_check.MAX_PROBLEMS)


class CheckCollectorCliTest(CollectorCase):
    def cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sr.main(["check-collector", *args])
        return code, out.getvalue(), err.getvalue()

    def test_cli_ok_prints_json(self):
        code, out, _ = self.cli(self.put("b1.json", batch()))
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["checked"], 1)
        self.assertEqual(report["with_problems"], 0)
        self.assertEqual(report["batches"][0]["index"], 1)
        self.assertEqual(report["batches"][0]["total"], 1)

    def test_cli_bad_is_1(self):
        bad = copy.deepcopy(batch())
        bad["feedback_draft"] = "текст"
        code, out, _ = self.cli(self.put("bad.json", bad))
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["with_problems"], 1)

    def test_cli_missing_is_2(self):
        code, out, _ = self.cli(str(self.dir / "none.json"))
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["missing_files"], 1)


if __name__ == "__main__":
    unittest.main()
