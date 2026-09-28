"""Фикстуры трекера и ревью для отчётов: `tests/fixtures/make_assess_samples.py`.

Генератор во временную папку даёт ровно те же байты, что лежат в репо; трекер проходит схему
`tracker` и `validate`; в нём три человека по три живых периода, отозванный прогон, правка
ревьюера, рекомендации в разных статусах; три файла ревью по формату парсера отчёта, один
черновик; абсолютных путей нет.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

import _util  # noqa: F401 — кладёт scripts/ в sys.path
from _assess import make_ws
from _util import FIXTURES, TempHome
from test_render_review import parse_review

from lib import schema, tracker

SAMPLE = FIXTURES / "process-reviews.sample.json"
REVIEWS = FIXTURES / "reviews"
GENERATOR = FIXTURES / "make_assess_samples.py"
PEOPLE = ("alex", "sam", "kim")
LABELS = ["p1", "p2", "p3"]
ABSOLUTE = ("/" + "Users/", "/tmp", "/private", "/var/folders")  # склейка: гейт ловит сам литерал


def _load_generator():
    spec = importlib.util.spec_from_file_location("make_assess_samples", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("make_assess_samples", module)
    spec.loader.exec_module(module)
    return module


def _files(base: Path) -> dict[str, bytes]:
    out = {"process-reviews.sample.json": (base / "process-reviews.sample.json").read_bytes()}
    for path in sorted((base / "reviews").rglob("*")):
        if path.is_file():
            out[path.relative_to(base).as_posix()] = path.read_bytes()
    return out


class AssessSamplesTest(TempHome, unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.gen = _load_generator()
        cls.doc = json.loads(SAMPLE.read_text(encoding="utf-8"))
        cls.live = [r for r in cls.doc["runs"] if tracker.is_live(r)]

    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="sr-samples-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    # --- генератор ---------------------------------------------------------------

    def test_generator_output_equals_repo_files_bytewise(self):
        out = self.tmp / "out"
        result = self.gen.build(out, self.gen.DEFAULT_NOW)
        generated, stored = _files(out), _files(FIXTURES)
        self.assertEqual(sorted(generated), sorted(stored))
        for name in stored:
            with self.subTest(file=name):
                self.assertEqual(generated[name], stored[name],
                                 f"{name}: фикстура устарела, перегенерируйте make_assess_samples.py --out")
        self.assertEqual(len(result["reviews"]), 3)

    def test_generator_is_deterministic_for_other_now(self):
        from datetime import timedelta
        a, b = self.tmp / "a", self.tmp / "b"
        later = self.gen.DEFAULT_NOW + timedelta(days=30)
        self.gen.build(a, later)
        self.gen.build(b, later)
        self.assertEqual(_files(a), _files(b))
        self.assertNotEqual(_files(a)["process-reviews.sample.json"],
                            SAMPLE.read_bytes())  # время действительно берётся из now

    def test_generator_cli_writes_to_out(self):
        import subprocess
        out = self.tmp / "cli"
        proc = subprocess.run([sys.executable, str(GENERATOR), "--out", str(out)],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(_files(out), _files(FIXTURES))

    def test_generator_rebuild_drops_stale_review_files(self):
        out = self.tmp / "out"
        stale = out / "reviews" / "alex" / "demo-team_alex_p9_20200101T000000Z.md"
        stale.parent.mkdir(parents=True)
        stale.write_text("old\n", encoding="utf-8")
        self.gen.build(out, self.gen.DEFAULT_NOW)
        self.assertFalse(stale.exists())

    # --- трекер ------------------------------------------------------------------

    def test_tracker_passes_schema(self):
        self.assertEqual(schema.validate(self.doc, "tracker"), [])

    def test_validate_file_is_clean(self):
        root = make_ws(self.tmp)
        target = tracker.tracker_path(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SAMPLE, target)
        self.assertEqual(tracker.validate_file(root), [])

    def test_verdicts_only_on_approved_texts(self):
        """B04: фикстура — team с гейтом ревьюера; вердикты только по рекомендациям одобренных прогонов."""
        for run in self.live:
            with self.subTest(run=run["run_id"]):
                self.assertEqual(tracker.draft_verdict_problems(run, self.doc["runs"], True), [])
        earlier = [r for r in self.live if r["period"]["label"] != LABELS[-1]]
        self.assertTrue(earlier)
        self.assertTrue(all(r["review_status"] == "approved" for r in earlier))

    def test_three_people_three_live_periods(self):
        periods = {p: [r["period"]["label"] for r in self.live if r["person_id"] == p] for p in PEOPLE}
        self.assertEqual(periods, {p: LABELS for p in PEOPLE})
        for run in self.live:
            with self.subTest(run=run["run_id"]):
                self.assertTrue(run["period"]["from"] and run["period"]["to"])
                self.assertIsNotNone(run["level_computed"])
                self.assertIsNotNone(run["level_judge"])

    def test_one_retracted_run_replaced_by_live_run(self):
        retracted = [r for r in self.doc["runs"] if not tracker.is_live(r)]
        self.assertEqual(len(retracted), 1)
        gone = retracted[0]
        self.assertTrue(gone["retracted"]["why"])
        twin = [r for r in self.live if r["person_id"] == gone["person_id"]
                and r["period"]["label"] == gone["period"]["label"]]
        self.assertEqual(len(twin), 1)
        self.assertNotEqual(twin[0]["run_id"], gone["run_id"])

    def test_one_override_by_reviewer(self):
        overrides = [r["operator_override"] for r in self.doc["runs"] if r.get("operator_override")]
        self.assertEqual(len(overrides), 1)
        self.assertEqual(overrides[0]["by"], "reviewer")
        self.assertTrue(overrides[0]["axes"] and overrides[0]["why"] and overrides[0]["at"])

    def test_recommendation_statuses_and_verdicts_vary(self):
        statuses = Counter()
        for person in PEOPLE:
            statuses.update(tracker.rec_statuses(self.doc, person).values())
        self.assertTrue({"open", "followed", "ignored"} <= set(statuses), statuses)
        verdicts = {v["verdict"] for r in self.live for v in r["prev_recommendations"]}
        self.assertEqual(verdicts, {"followed", "partial", "ignored", "not_observable"})

    def test_levels_and_scores_have_dynamics(self):
        levels = {r["level_computed"] for r in self.live}
        self.assertGreaterEqual(len(levels), 5, levels)
        for person in PEOPLE:
            seq = [r["level_computed"] for r in self.live if r["person_id"] == person]
            with self.subTest(person=person):
                self.assertEqual(len(set(seq)), 3, seq)
        self.assertTrue(any(r["gating_triggered"] for r in self.live))
        self.assertTrue(any(a["insufficient_evidence"] for r in self.live for a in r["axes"]))

    # --- файлы ревью -------------------------------------------------------------

    def test_three_reviews_for_last_period_one_draft(self):
        files = sorted(REVIEWS.rglob("*.md"))
        self.assertEqual(len(files), 3)
        by_id = {r["run_id"]: r for r in self.live}
        statuses = []
        for path in files:
            doc = parse_review(path.read_text(encoding="utf-8"))
            meta = doc["meta"]
            with self.subTest(file=path.name):
                self.assertEqual(path.parent.name, meta["person_id"])
                self.assertEqual(path.stem, meta["run_id"])
                run = by_id[meta["run_id"]]
                self.assertEqual(run["period"]["label"], "p3")
                self.assertEqual(meta["period"], "p3")
                self.assertEqual(run["review_status"], meta["status"])
                self.assertTrue(doc["service"].lstrip().startswith("<!-- service -->"))
                self.assertTrue(doc["sent"])
                self.assertEqual(doc["sent"], run["feedback_draft"].strip())
                self.assertTrue(doc["dossier"].startswith("## "))
            statuses.append(meta["status"])
        self.assertEqual(sorted(statuses), ["approved", "approved", "draft"])
        self.assertEqual({p.parent.name for p in files}, set(PEOPLE))

    def test_no_absolute_paths(self):
        for name, data in _files(FIXTURES).items():
            text = data.decode("utf-8")
            for bad in ABSOLUTE:
                with self.subTest(file=name, bad=bad):
                    self.assertNotIn(bad, text)
        for run in self.doc["runs"]:
            self.assertFalse(Path(run["source"]["path"]).is_absolute(), run["source"])


if __name__ == "__main__":
    unittest.main()
