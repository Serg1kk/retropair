"""`lib/batches.py` и команда `batches`: нарезка сессий человека на пачки коллектора —
хронология, целые сессии, два бюджета, тяжёлая сессия отдельно, короткие и чужой период
в свои списки, хвост выросшей сессии, замер человеческого слоя подпроцессом `normalize`."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _util  # noqa: F401 — кладёт scripts/ в sys.path
from _assess import make_ws
from _util import SCRIPTS, TempHome

import sr
from lib import batches
from lib.workspace import SrError, ws_paths

CFG = {"max_bytes": 100, "max_user_turns": 20, "min_turns": 4}

LOGIN = "3f9a2c71-8d4e-4b6a-9c15-2e7f0a4b8d31"
FIX_ALEX = "b61d0e48-27c3-4f95-a8e1-6d0c93b2f574"
GROWN = "5c2e8f19-a4b7-4d20-8e63-f1b09c7d2a46"
SHORT = "e07a4d93-6f18-4c5b-b2d7-8a3e10f4c965"


def e(sid: str, day: int, size: int = 10, turns: int = 5, state: str = "new", **kw) -> dict:
    """Синтетическая запись индекса `alex`; `day` — число сентября начала сессии."""
    entry = {
        "person_id": "alex", "session_id": sid, "key": f"k-{sid}", "path": f"logs/alex/k-{sid}.md",
        "bytes": size, "turns_total": turns * 2, "user_turns": turns,
        "log_started_at": f"2026-09-{day:02d}T10:00:00Z", "log_ended_at": f"2026-09-{day:02d}T11:00:00Z",
        "period_guess": "p1", "state": state, "duplicate_of": None, "excluded": False,
        "reviewed_turns_total": None, "review_from_turn": None,
    }
    entry.update(kw)
    return entry


def ids(result: dict) -> list[list[str]]:
    return [[s["session_id"] for s in b["sessions"]] for b in result["batches"]]


class PlanTest(unittest.TestCase):
    def plan(self, entries, cfg=CFG, **kw) -> dict:
        return batches.plan(entries, cfg, batches.index_measure, **kw)

    def test_chronological_by_start(self):
        result = self.plan([e("c", 5), e("a", 1), e("b", 3)])
        self.assertEqual(ids(result), [["a", "b", "c"]])
        self.assertEqual(result["batches"][0]["date_from"], "2026-09-01T10:00:00Z")
        self.assertEqual(result["batches"][0]["date_to"], "2026-09-05T11:00:00Z")

    def test_session_never_split(self):
        result = self.plan([e("a", 1, size=60), e("b", 2, size=60)])
        self.assertEqual(ids(result), [["a"], ["b"]])
        self.assertEqual([b["bytes"] for b in result["batches"]], [60, 60])
        self.assertEqual([b["batch_truncated"] for b in result["batches"]], [False, False])

    def test_byte_budget_cuts(self):
        result = self.plan([e("a", 1, size=40), e("b", 2, size=40), e("c", 3, size=40)])
        self.assertEqual(ids(result), [["a", "b"], ["c"]])

    def test_byte_budget_inclusive(self):
        result = self.plan([e("a", 1, size=50), e("b", 2, size=50), e("c", 3, size=1)])
        self.assertEqual(ids(result), [["a", "b"], ["c"]])

    def test_turn_budget_cuts_separately(self):
        result = self.plan([e("a", 1, turns=8), e("b", 2, turns=8), e("c", 3, turns=8)])
        self.assertEqual(ids(result), [["a", "b"], ["c"]])
        self.assertEqual([b["user_turns"] for b in result["batches"]], [16, 8])

    def test_heavy_by_bytes_alone_truncated(self):
        result = self.plan([e("a", 1), e("h", 2, size=150), e("c", 3)])
        self.assertEqual(ids(result), [["a"], ["h"], ["c"]])
        self.assertEqual([b["batch_truncated"] for b in result["batches"]], [False, True, False])

    def test_heavy_by_turns_alone_truncated(self):
        result = self.plan([e("h", 1, turns=30), e("b", 2)])
        self.assertEqual(ids(result), [["h"], ["b"]])
        self.assertTrue(result["batches"][0]["batch_truncated"])

    def test_index_and_total(self):
        result = self.plan([e("a", 1, size=60), e("b", 2, size=60), e("c", 3, size=60)])
        self.assertEqual([(b["index"], b["total"]) for b in result["batches"]], [(1, 3), (2, 3), (3, 3)])
        self.assertTrue(all("of" not in b for b in result["batches"]))  # имя поля как во входе коллектора

    def test_short_listed_not_batched(self):
        result = self.plan([e("a", 1), e("s", 2, turns=3)])
        self.assertEqual(ids(result), [["a"]])
        self.assertEqual([s["session_id"] for s in result["short"]], ["s"])
        self.assertEqual(result["short"][0]["user_turns"], 3)

    def test_grown_from_turn_is_review_from_turn(self):
        result = self.plan([e("g", 1, state="grown", review_from_turn=10, reviewed_turns_total=12)])
        session = result["batches"][0]["sessions"][0]
        self.assertEqual(session["from_turn"], 10)
        self.assertEqual(session["state"], "grown")

    def test_grown_without_field_uses_reviewed_turns_total(self):
        result = self.plan([e("g", 1, state="grown", reviewed_turns_total=12)])
        self.assertEqual(result["batches"][0]["sessions"][0]["from_turn"], 12)

    def test_grown_without_any_mark_is_error(self):
        with self.assertRaises(SrError) as ctx:
            self.plan([e("g", 1, state="grown")])
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("k-g", ctx.exception.message)

    def test_new_has_no_from_turn(self):
        self.assertIsNone(self.plan([e("a", 1)])["batches"][0]["sessions"][0]["from_turn"])

    def test_excluded_gone_duplicate_unchanged_not_taken(self):
        result = self.plan([
            e("a", 1),
            e("x", 2, state="excluded", excluded=True),
            e("y", 3, excluded=True),
            e("g", 4, state="gone"),
            e("d", 5, duplicate_of="k-a"),
            e("u", 6, state="unchanged"),
        ])
        self.assertEqual(ids(result), [["a"]])
        self.assertEqual(result["short"], [])
        self.assertEqual(result["skipped"], [])

    def test_other_period_skipped(self):
        result = self.plan([e("a", 1), e("b", 9, period_guess="p2")], period_label="p1")
        self.assertEqual(ids(result), [["a"]])
        self.assertEqual(result["skipped"][0]["session_id"], "b")
        self.assertEqual(result["skipped"][0]["reason"], "other_period")
        self.assertEqual(result["skipped"][0]["period_guess"], "p2")

    def test_unknown_period_guess_is_taken(self):
        result = self.plan([e("a", 1, period_guess=None)], period_label="p1")
        self.assertEqual(ids(result), [["a"]])

    def test_without_label_period_not_checked(self):
        result = self.plan([e("a", 1), e("b", 9, period_guess="p2")])
        self.assertEqual(ids(result), [["a", "b"]])

    def test_measure_callable_drives_budget(self):
        result = batches.plan([e("a", 1, size=1), e("b", 2, size=1)], CFG, lambda entry: 70,
                              measure_name="human")
        self.assertEqual(ids(result), [["a"], ["b"]])
        self.assertEqual(result["batches"][0]["bytes"], 70)
        self.assertEqual(result["measure"], "human")

    def test_bad_measure_value_is_error(self):
        with self.assertRaises(SrError):
            batches.plan([e("a", 1)], CFG, lambda entry: "10")

    def test_missing_user_turns_is_error(self):
        with self.assertRaises(SrError) as ctx:
            self.plan([e("a", 1, user_turns=None)])
        self.assertIn("user_turns", ctx.exception.message)

    def test_mixed_people_is_error_2(self):
        with self.assertRaises(SrError) as ctx:
            self.plan([e("a", 1), e("b", 2, person_id="sam")])
        self.assertEqual(ctx.exception.code, 2)

    def test_index_measure_is_bytes(self):
        self.assertEqual(batches.index_measure(e("a", 1, size=777)), 777)


class HumanMeasureTest(TempHome, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="sr-batches-")
        self.addCleanup(self._tmp.cleanup)
        self.root = make_ws(Path(self._tmp.name))
        self.entry = e("a", 1)

    def done(self, code: int, out: bytes = b"", err: bytes = b"") -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args=[], returncode=code, stdout=out, stderr=err)

    def test_length_of_stdout_and_exact_call(self):
        with mock.patch("lib.batches.subprocess.run", return_value=self.done(0, "ёж".encode())) as run:
            size = batches.human_measure(self.root)(self.entry)
        self.assertEqual(size, 4)
        args, kwargs = run.call_args
        state = ws_paths(self.root)["state_dir"]
        self.assertEqual(args[0], [sys.executable, str(SCRIPTS / "sr.py"), "--workspace", str(self.root),
                                   "normalize", str(state / "logs/alex/k-a.md"), "--roles", "user",
                                   "--drop-synthetic", "--max-turn-chars", "4000"])
        self.assertEqual(kwargs["timeout"], 120)
        self.assertIs(kwargs["shell"], False)
        self.assertTrue(kwargs["capture_output"])

    def test_no_normalize_command_is_1(self):
        err = b"sr.py: error: argument command: invalid choice: 'normalize' (choose from 'init')"
        with mock.patch("lib.batches.subprocess.run", return_value=self.done(2, err=err)):
            with self.assertRaises(SrError) as ctx:
                batches.human_measure(self.root)(self.entry)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("нет команды normalize", ctx.exception.message)
        self.assertIn("--measure index", ctx.exception.message)

    def test_failure_on_file_is_1_with_path_and_stderr(self):
        with mock.patch("lib.batches.subprocess.run", return_value=self.done(1, err=b"no turns")):
            with self.assertRaises(SrError) as ctx:
                batches.human_measure(self.root)(self.entry)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("logs/alex/k-a.md", ctx.exception.message)
        self.assertIn("no turns", ctx.exception.message)

    def test_timeout_is_1_with_path(self):
        with mock.patch("lib.batches.subprocess.run", side_effect=subprocess.TimeoutExpired(["x"], 120)):
            with self.assertRaises(SrError) as ctx:
                batches.human_measure(self.root)(self.entry)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("logs/alex/k-a.md", ctx.exception.message)


class ForPersonTest(TempHome, unittest.TestCase):
    """Нарезка на фикстуре индекса (`alex`: две new, grown, короткая)."""

    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="sr-batches-")
        self.addCleanup(self._tmp.cleanup)
        self.root = make_ws(Path(self._tmp.name))

    def cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sr.main(["--workspace", str(self.root), *args])
        return code, out.getvalue(), err.getvalue()

    def test_fixture_period_p1(self):
        result = batches.for_person(self.root, "alex", measure="index", period_label="p1")
        self.assertEqual(result["person_id"], "alex")
        self.assertEqual(result["measure"], "file_bytes")
        self.assertEqual(ids(result), [[GROWN, LOGIN]])
        self.assertEqual(result["batches"][0]["sessions"][0]["from_turn"], 10)
        self.assertEqual(result["batches"][0]["bytes"], 13900 + 18400)
        self.assertEqual([s["session_id"] for s in result["short"]], [SHORT])
        self.assertEqual([(s["session_id"], s["reason"]) for s in result["skipped"]], [(FIX_ALEX, "other_period")])

    def test_other_person_entries_not_mixed(self):
        result = batches.for_person(self.root, "sam", measure="index")
        self.assertEqual(len(ids(result)[0]), 2)
        self.assertTrue(all(s["path"].startswith("logs/sam/") for s in result["batches"][0]["sessions"]))

    def test_unknown_person_is_1(self):
        with self.assertRaises(SrError) as ctx:
            batches.for_person(self.root, "nobody", measure="index")
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("nobody", ctx.exception.message)

    def test_person_without_consent_is_1(self):      # B02
        card = self.root / ".retropair" / "people" / "alex.yaml"
        card.write_text(card.read_text(encoding="utf-8").replace("logs: true", "logs: false"), encoding="utf-8")
        with self.assertRaises(SrError) as ctx:
            batches.for_person(self.root, "alex", measure="index")
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("alex", ctx.exception.message)
        self.assertIn("нет согласия на логи", ctx.exception.message)
        self.assertEqual(len(ids(batches.for_person(self.root, "sam", measure="index"))[0]), 2)

    def test_no_index_is_1(self):
        ws_paths(self.root)["logs_index"].unlink()
        with self.assertRaises(SrError) as ctx:
            batches.for_person(self.root, "alex", measure="index")
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("logs-index.json", ctx.exception.message)

    def test_bad_measure_name_is_2(self):
        with self.assertRaises(SrError) as ctx:
            batches.for_person(self.root, "alex", measure="words")
        self.assertEqual(ctx.exception.code, 2)

    def test_cli_index_json(self):
        code, out, err = self.cli("batches", "alex", "--measure", "index", "--period-label", "p1", "--json")
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(result["measure"], "file_bytes")
        self.assertEqual(ids(result), [[GROWN, LOGIN]])
        self.assertIn("file_bytes", err)

    def test_cli_text(self):
        code, out, _ = self.cli("batches", "alex", "--measure", "index")
        self.assertEqual(code, 0)
        self.assertIn("1/1", out)
        self.assertIn(SHORT, out)

    def test_cli_default_measure_is_human(self):
        with mock.patch("lib.batches.subprocess.run",
                        return_value=subprocess.CompletedProcess([], 0, stdout=b"x" * 5, stderr=b"")) as run:
            code, out, err = self.cli("batches", "alex", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["measure"], "human")
        self.assertEqual(run.call_count, 3)

    def test_cli_human_without_normalize_is_1(self):
        err = b"error: argument command: invalid choice: 'normalize'"
        with mock.patch("lib.batches.subprocess.run",
                        return_value=subprocess.CompletedProcess([], 2, stdout=b"", stderr=err)):
            code, _, stderr = self.cli("batches", "alex")
        self.assertEqual(code, 1)
        self.assertIn("нет команды normalize", stderr)


if __name__ == "__main__":
    unittest.main()
