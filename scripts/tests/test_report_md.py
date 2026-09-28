"""Генератор фикстуры отчётов и разбор файла ревью (`lib/md.py`)."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import _util  # noqa: F401  (кладёт scripts/ в sys.path)
import report_fixture
from lib import md
from lib.config import load_config
from lib.people import list_people

RUN_ID_RE = re.compile(r"^demo-team_[a-z0-9-]+_P\d+_\d{8}T\d{6}Z$")
FORBIDDEN_PATH = "/" + "Users" + "/"


def _review_text(status: str = "approved", body_extra: str = "") -> str:
    return (
        "---\n"
        "person_id: anna\n"
        "run_id: demo-team_anna_P1_20260108T100000Z\n"
        "period: P1\n"
        f"status: {status}\n"
        "lang: ru\n"
        "template: default\n"
        "---\n"
        "<!-- service -->\n"
        "Служебная шапка: разобрано 3 сессии.\n"
        f"{body_extra}"
        "\n---\n\n"
        "### Ревью по сессиям, P1\n\n"
        "Анна, разобрано три сессии.\n\n"
        "**Проверка результата.** Здесь ровно.\n"
        "\n---\n\n"
        "## Досье\n\n"
        "| Дата | Файл лога |\n|---|---|\n| 2026-01-06 | /home/user/proj/a.md |\n"
    )


class FixtureTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="sr-report-")
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_fixture_3x3_files_and_tracker(self) -> None:
        root = report_fixture.make_workspace(self.tmp / "ws")
        state = root / ".sessionretro"
        for rel in ("config.json", "plugin-state.json", "people/_default.yaml",
                    "runs/process-reviews.json"):
            self.assertTrue((state / rel).is_file(), rel)
        cfg = load_config(root)
        self.assertEqual(cfg["mode"], "team")
        self.assertIs(cfg["show_scores"], False)
        ids = [card["id"] for card in list_people(root)]
        self.assertEqual(len(ids), 4)
        self.assertIn("nora", ids)

        tracker = json.loads((state / "runs/process-reviews.json").read_text(encoding="utf-8"))
        self.assertEqual(tracker["tracker_version"], 2)
        self.assertEqual(tracker["workspace"], "demo-team")
        runs = tracker["runs"]
        self.assertEqual(len(runs), 10)
        live = [r for r in runs if not r.get("retracted")]
        self.assertEqual(len(runs) - len(live), 1)
        self.assertEqual(len({(r["person_id"], r["period"]["label"]) for r in live}), 8)
        self.assertEqual(sum(1 for r in runs if r["revision"] == 2), 1)
        self.assertNotIn("nora", {r["person_id"] for r in runs})
        for run in runs:
            self.assertRegex(run["run_id"], RUN_ID_RE)
            self.assertEqual(run["tracker_version"], 2)
            self.assertEqual(len(run["axes"]), 8)
            self.assertEqual(set(run["period"]), {"label", "from", "to", "anchor"})

        by = {(r["person_id"], r["period"]["label"], r["revision"], bool(r.get("retracted"))): r
              for r in runs}
        first, second, third = sorted({r["person_id"] for r in runs if r["person_id"] != "nora"},
                                      key=lambda pid: report_fixture.person_index(pid))
        self.assertNotIn((third, "P2", 1, False), by)
        rev = {a["id"]: a for a in by[(first, "P3", 1, False)]["axes"]}["reversibility"]
        self.assertEqual((rev["score"], rev["insufficient_evidence"],
                          rev["insufficient_evidence_reason"]), (None, True, "short_log"))
        twin = by[(first, "P1", 1, True)]
        self.assertGreater(twin["reviewed_at"], by[(first, "P1", 1, False)]["reviewed_at"])
        self.assertEqual(set(twin["retracted"]), {"at", "why"})
        over = by[(second, "P2", 1, False)]
        self.assertEqual(over["operator_override"], {"axes": {"verification": 4}})
        self.assertIs(over["gating_triggered"], True)
        judge_only = by[(third, "P1", 1, False)]
        self.assertIsNone(judge_only["level_computed"])
        self.assertTrue(judge_only["level_judge"])
        self.assertGreater(by[(second, "P1", 2, False)]["reviewed_at"],
                           by[(second, "P1", 1, False)]["reviewed_at"])
        verdicts = {}
        for r in live:
            for prev in r["prev_recommendations"]:
                verdicts[prev["id"]] = prev["verdict"]
        self.assertEqual(verdicts[f"REC-{first}-001"], "followed")
        self.assertEqual(verdicts[f"REC-{second}-001"], "not_observable")
        self.assertEqual(verdicts[f"REC-{third}-001"], "ignored")

        raw = "\n".join(p.read_text(encoding="utf-8") for p in state.rglob("*") if p.is_file())
        self.assertNotIn(FORBIDDEN_PATH, raw)
        self.assertIn("/home/user/proj/", raw)
        self.assertIn(f"@fake-handle-{first}", raw)
        self.assertIn(f"https://github.com/fake-{first}/repo", raw)
        self.assertIn("<img src=x onerror=alert(1)>", raw)

    def test_fixture_reviews_statuses_and_addressee(self) -> None:
        root = report_fixture.make_workspace(self.tmp / "ws")
        state = root / ".sessionretro"
        files = sorted((state / "reviews").rglob("*.md"))
        self.assertEqual(len(files), 9)
        names = {c["id"]: c["name"] for c in list_people(root)}
        for f in files:
            parsed = md.parse_review(f.read_text(encoding="utf-8"))
            self.assertTrue(parsed["recognized"], f)
            self.assertIsNone(parsed["warning"], f)
            pid = parsed["meta"]["person_id"]
            self.assertEqual(f.parent.name, pid)
            self.assertEqual(f.stem, parsed["meta"]["run_id"])
            last = parsed["meta"]["period"] == "P3"
            self.assertEqual(parsed["meta"]["status"], "draft" if last else "approved")
            others = [n.split()[0] for i, n in names.items() if i != pid]
            for other in others:
                self.assertNotIn(other, parsed["sent_md"], f)
            self.assertIn(names[pid].split()[0], parsed["sent_md"])

    def test_fixture_self_mode_has_one_card(self) -> None:
        root = report_fixture.make_workspace(self.tmp / "ws", mode="self", periods=2)
        self.assertEqual([c["id"] for c in list_people(root)], ["me"])
        tracker = json.loads((root / ".sessionretro/runs/process-reviews.json")
                             .read_text(encoding="utf-8"))
        self.assertEqual({r["person_id"] for r in tracker["runs"]}, {"me"})
        self.assertEqual(load_config(root)["mode"], "self")

    def test_fixture_realistic_sizes(self) -> None:
        root = report_fixture.make_workspace(self.tmp / "ws", people=1, periods=1, realistic=True)
        state = root / ".sessionretro"
        run = json.loads((state / "runs/process-reviews.json").read_text(encoding="utf-8"))["runs"][0]
        self.assertGreaterEqual(len(run["feedback_draft"].encode("utf-8")), 5000)
        self.assertTrue(all(len(a["evidence"]) == 3 for a in run["axes"] if a["score"] is not None))
        review = next((state / "reviews").rglob("*.md")).read_text(encoding="utf-8")
        dossier = md.parse_review(review)["dossier_md"]
        self.assertGreaterEqual(len(dossier.encode("utf-8")), 15000)

    def test_fixture_rejects_existing_state(self) -> None:
        root = report_fixture.make_workspace(self.tmp / "ws")
        with self.assertRaises(FileExistsError):
            report_fixture.make_workspace(root)

    def test_names_do_not_prefix_payload_keys(self) -> None:
        self.assertGreaterEqual(len(report_fixture.NAMES), 24)
        for pid, name in report_fixture.NAMES:
            for key in report_fixture.PAYLOAD_KEYS:
                self.assertFalse(key.startswith(pid[:3]), (pid, key))
                self.assertFalse(key.startswith(name[:3].lower()), (name, key))
        with self.assertRaises(ValueError):
            report_fixture.check_names([("lev", "Лев Тестов")])

    def test_fixture_cli_prints_root(self) -> None:
        out = self.tmp / "cli"
        res = subprocess.run(
            [sys.executable, str(_util.TESTS / "report_fixture.py"), "--out", str(out),
             "--people", "5", "--periods", "2", "--mode", "course"],
            text=True, capture_output=True,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(Path(res.stdout.strip()), out)
        self.assertEqual(len(list_people(out)), 6)
        self.assertEqual(load_config(out)["mode"], "course")


class ParseReviewTest(unittest.TestCase):
    def test_three_blocks_and_meta(self) -> None:
        parsed = md.parse_review(_review_text())
        self.assertTrue(parsed["recognized"])
        self.assertIsNone(parsed["warning"])
        self.assertEqual(parsed["meta"]["status"], "approved")
        self.assertEqual(parsed["meta"]["run_id"], "demo-team_anna_P1_20260108T100000Z")
        self.assertEqual(parsed["service_md"], "Служебная шапка: разобрано 3 сессии.")
        self.assertTrue(parsed["sent_md"].startswith("### Ревью по сессиям, P1"))
        self.assertTrue(parsed["sent_md"].endswith("Здесь ровно."))
        self.assertTrue(parsed["dossier_md"].startswith("## Досье"))

    def test_without_frontmatter_is_unrecognized(self) -> None:
        text = _review_text().split("---\n", 2)[2]
        parsed = md.parse_review(text)
        self.assertFalse(parsed["recognized"])
        self.assertEqual(parsed["sent_md"], "")
        self.assertEqual(parsed["dossier_md"], text.strip())
        self.assertIn("frontmatter", parsed["warning"])

    def test_single_rule_is_unrecognized(self) -> None:
        text = _review_text().rsplit("\n---\n", 1)[0]
        parsed = md.parse_review(text)
        self.assertFalse(parsed["recognized"])
        self.assertEqual(parsed["sent_md"], "")
        self.assertTrue(parsed["warning"])

    def test_rule_inside_fence_does_not_cut(self) -> None:
        parsed = md.parse_review(_review_text(body_extra="```\n---\nкод\n```\n"))
        self.assertTrue(parsed["recognized"])
        self.assertIn("---\nкод", parsed["service_md"])
        self.assertTrue(parsed["sent_md"].startswith("### Ревью по сессиям"))

    def test_without_service_mark_is_unrecognized(self) -> None:
        text = (
            "---\nperson_id: anna\nrun_id: r1\nstatus: approved\n---\n"
            "Текст адресату без служебной шапки.\n\n---\n\n"
            "## Досье\nЦитата: путь /home/user/secret\n\n***\n\nхвост\n"
        )
        parsed = md.parse_review(text)
        self.assertFalse(parsed["recognized"])
        self.assertEqual(parsed["sent_md"], "")
        self.assertEqual(parsed["service_md"], "")
        self.assertIn("## Досье", parsed["dossier_md"])
        self.assertIn("<!-- service -->", parsed["warning"])

    def test_service_mark_must_be_first_line_of_body(self) -> None:
        text = _review_text().replace("<!-- service -->\nСлужебная шапка: разобрано 3 сессии.\n",
                                      "Служебная шапка: разобрано 3 сессии.\n<!-- service -->\n")
        parsed = md.parse_review(text)
        self.assertFalse(parsed["recognized"])
        self.assertEqual(parsed["sent_md"], "")
        self.assertIn("<!-- service -->", parsed["warning"])

    def test_blank_lines_before_service_mark_are_allowed(self) -> None:
        text = _review_text().replace("---\n<!-- service -->", "---\n\n\n<!-- service -->", 1)
        parsed = md.parse_review(text)
        self.assertTrue(parsed["recognized"], parsed["warning"])

    def test_unknown_status_becomes_draft(self) -> None:
        parsed = md.parse_review(_review_text(status="sent"))
        self.assertTrue(parsed["recognized"])
        self.assertEqual(parsed["meta"]["status"], "draft")
        self.assertIn("sent", parsed["warning"])


class MdToHtmlTest(unittest.TestCase):
    def test_converter(self) -> None:
        text = (
            "## Заголовок\n\n### Под\n\nАбзац **жирно** и *курсив* и `код`.\n\n"
            "- один\n- два\n\n1. первый\n2. второй\n\n"
            "| a | b |\n|---|---|\n| 1 | x \\| y |\n\n"
            "> цитата\n\n<script>alert(1)</script>\n\n<!-- скрыто -->\n\n"
            "[ссылка](https://example.org)\n"
        )
        out = md.md_to_html(text)
        self.assertIn("<h3>Заголовок</h3>", out)
        self.assertIn("<h4>Под</h4>", out)
        self.assertIn("<b>жирно</b>", out)
        self.assertIn("<i>курсив</i>", out)
        self.assertIn("<code>код</code>", out)
        self.assertIn("<ul><li>один</li><li>два</li></ul>", out)
        self.assertIn("<ol><li>первый</li><li>второй</li></ol>", out)
        self.assertIn("<thead><tr><th>a</th><th>b</th></tr></thead>", out)
        self.assertIn("<td>x | y</td>", out)
        self.assertIn("<blockquote><p>цитата</p></blockquote>", out)
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)
        self.assertNotIn("скрыто", out)
        self.assertNotIn("<a", out)

    def test_empty(self) -> None:
        self.assertEqual(md.md_to_html(""), "")


class LoadReviewsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="sr-report-")
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_fixture_reviews_keyed_by_run_id(self) -> None:
        root = report_fixture.make_workspace(self.tmp / "ws")
        state = root / ".sessionretro"
        runs = json.loads((state / "runs/process-reviews.json").read_text(encoding="utf-8"))["runs"]
        superseded = {
            r["run_id"] for r in runs
            if any(o is not r and not o.get("retracted") and o["person_id"] == r["person_id"]
                   and o["period"]["label"] == r["period"]["label"] and o["revision"] > r["revision"]
                   for o in runs)
        }
        self.assertEqual(len(superseded), 1)
        reviews, warnings = md.load_reviews(state / "reviews")
        self.assertEqual(warnings, [])
        self.assertEqual(set(reviews), {r["run_id"] for r in runs} - superseded)
        retracted = next(r for r in runs if r.get("retracted"))
        self.assertIn(retracted["run_id"], reviews)
        for run_id, review in reviews.items():
            pid = review["meta"]["person_id"]
            self.assertEqual(review["path"], f"reviews/{pid}/{run_id}.md")
            self.assertIn(review["status"], ("draft", "approved"))

    def test_missing_dir_and_file_without_run_id(self) -> None:
        self.assertEqual(md.load_reviews(self.tmp / "nope"), ({}, []))
        folder = self.tmp / "reviews" / "anna"
        folder.mkdir(parents=True)
        (folder / "x.md").write_text(_review_text().replace("run_id: demo-team_anna_P1_20260108T100000Z\n", ""),
                                     encoding="utf-8")
        (folder / "ok.md").write_text(_review_text(), encoding="utf-8")
        reviews, warnings = md.load_reviews(self.tmp / "reviews")
        self.assertEqual(list(reviews), ["demo-team_anna_P1_20260108T100000Z"])
        self.assertEqual(len(warnings), 1)
        self.assertIn("reviews/anna/x.md", warnings[0])


if __name__ == "__main__":
    unittest.main()
