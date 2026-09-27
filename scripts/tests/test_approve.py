"""`render_review.approve` и команда `approve`: черновик ревью → одобрено (файл и прогон)."""

from __future__ import annotations

import _util  # noqa: F401 — кладёт scripts/ в sys.path
from test_render_review import TEMPLATE_RU, parse_review
from test_tracker import LATER, NOW, RUN_P1, TrackerCase

from lib import render_review, tracker
from lib.workspace import SrError


class ApproveTest(TrackerCase):
    def setUp(self) -> None:
        super().setUp()
        self.append_p1()
        self.judge = self.paths["pending"] / "alex" / "judge.json"
        self.review = self.paths["reviews"] / "alex" / f"{RUN_P1}.md"

    def render(self) -> None:
        render_review.write_review(self.root, self.judge, "alex", template=str(TEMPLATE_RU), now=NOW)

    def status(self) -> tuple[str, str]:
        doc = parse_review(self.review.read_text(encoding="utf-8"))
        run = tracker.find_run(tracker.load(self.root), RUN_P1)
        return doc["meta"]["status"], run["review_status"]

    def test_approve_changes_file_and_run(self):
        self.render()
        before = parse_review(self.review.read_text(encoding="utf-8"))
        result = render_review.approve(self.root, "alex", RUN_P1, now=LATER)
        self.assertEqual(result, {"run_id": RUN_P1, "status": "approved", "changed": True})
        self.assertEqual(self.status(), ("approved", "approved"))
        after = parse_review(self.review.read_text(encoding="utf-8"))
        self.assertEqual(after["sent"], before["sent"])  # меняется только статус
        self.assertEqual(after["dossier"], before["dossier"])
        self.assertEqual(self.tracker_doc()["updated_at"], "2026-09-17T12:00:00Z")

    def test_approve_latest_live_run_without_id(self):
        self.render()
        result = render_review.approve(self.root, "alex", now=LATER)
        self.assertEqual(result["run_id"], RUN_P1)
        self.assertTrue(result["changed"])

    def test_repeat_is_noop(self):
        self.render()
        render_review.approve(self.root, "alex", RUN_P1, now=LATER)
        text = self.review.read_text(encoding="utf-8")
        result = render_review.approve(self.root, "alex", RUN_P1, now=LATER)
        self.assertEqual(result, {"run_id": RUN_P1, "status": "approved", "changed": False})
        self.assertEqual(self.review.read_text(encoding="utf-8"), text)

    def test_missing_file_is_1(self):
        with self.assertRaises(SrError) as ctx:
            render_review.approve(self.root, "alex", RUN_P1)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn(f"{RUN_P1}.md", ctx.exception.message)

    def test_file_without_frontmatter_status_is_1(self):
        self.render()
        self.review.write_text("просто текст\n", encoding="utf-8")
        with self.assertRaises(SrError) as ctx:
            render_review.approve(self.root, "alex", RUN_P1)
        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(tracker.find_run(tracker.load(self.root), RUN_P1)["review_status"], "draft")

    def test_other_person_run_is_1(self):
        self.render()
        with self.assertRaises(SrError) as ctx:
            render_review.approve(self.root, "sam", RUN_P1)
        self.assertEqual(ctx.exception.code, 1)

    def test_no_runs_is_1(self):
        with self.assertRaises(SrError) as ctx:
            render_review.approve(self.root, "kim")
        self.assertEqual(ctx.exception.code, 1)

    def test_retracted_run_is_1(self):
        self.render()
        tracker.retract(self.root, RUN_P1, "не те логи", now=LATER)
        with self.assertRaises(SrError) as ctx:
            render_review.approve(self.root, "alex", RUN_P1)
        self.assertEqual(ctx.exception.code, 1)

    def test_cli_approve_and_repeat(self):
        self.render()
        code, out, err = self.cli("approve", "alex", RUN_P1)
        self.assertEqual(code, 0, err)
        self.assertIn("approved", out)
        code, out, err = self.cli("approve", "alex")
        self.assertEqual(code, 0, err)
        self.assertIn(RUN_P1, out)
        self.assertEqual(self.status(), ("approved", "approved"))

    def test_cli_missing_file_is_1(self):
        code, _, err = self.cli("approve", "alex", RUN_P1)
        self.assertEqual(code, 1)
        self.assertIn(f"{RUN_P1}.md", err)

    def test_cli_unknown_person_is_2(self):
        self.assertEqual(self.cli("approve", "nobody")[0], 2)
