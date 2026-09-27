"""`lib/marks.py`: индекс логов, поиск записи, пометки reviewed/excluded, откат, покрытие."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import _util  # noqa: F401 — кладёт scripts/ в sys.path
from _assess import ASSESS, make_ws
from _util import SCRIPTS, TempHome

from lib import i18n, marks
from lib.lock import file_lock
from lib.workspace import SrError, ws_paths

NOW = "2026-09-10T12:00:00Z"
RUN = "demo-team_alex_p2_20260910T120000Z"
NEWER = "demo-team_alex_p3_20260920T120000Z"

LOGIN = "3f9a2c71-8d4e-4b6a-9c15-2e7f0a4b8d31"
FIX_ALEX = "b61d0e48-27c3-4f95-a8e1-6d0c93b2f574"
GROWN = "5c2e8f19-a4b7-4d20-8e63-f1b09c7d2a46"
SHORT = "e07a4d93-6f18-4c5b-b2d7-8a3e10f4c965"
FIX_SAM = "d8a35f06-b9e2-4c71-a4d8-5e2f07c3b196"
GONE_SAM = "2b7e91c4-e8d3-4a09-b6f5-7c1d2e4a8f30"

CHILD = textwrap.dedent(
    """
    import sys, time
    sys.path.insert(0, sys.argv[1])
    from pathlib import Path
    from lib.lock import file_lock
    with file_lock(Path(sys.argv[2]), timeout=5):
        print("locked", flush=True)
        time.sleep(3)
    """
)


def fixture_index() -> dict:
    return json.loads((ASSESS / "logs-index.json").read_text(encoding="utf-8"))


def entry_by_sid(index: dict, sid: str) -> dict:
    return next(e for e in index["logs"] if e["session_id"] == sid)


class ExcludeReasonsTest(unittest.TestCase):
    def test_reasons_are_session_statuses(self):
        self.assertEqual(marks.EXCLUDE_REASONS, (
            "empty_transcript", "transcript_too_short", "unreadable_input", "agent_subtranscript",
            "duplicate_export", "other_period", "not_batched", "excluded_other",
        ))
        statuses = i18n.keys("session_statuses")
        for reason in marks.EXCLUDE_REASONS:
            self.assertIn(reason, statuses)
        self.assertEqual(marks.DEFAULT_EXCLUDE_REASON, "not_batched")


class IsoTest(unittest.TestCase):
    def test_iso_utc_formats_aware_and_naive(self):
        at = datetime(2026, 9, 10, 15, 0, 0, tzinfo=timezone(timedelta(hours=3)))
        self.assertEqual(marks.iso_utc(at), "2026-09-10T12:00:00Z")
        self.assertEqual(marks.iso_utc(datetime(2026, 9, 10, 12, 0, 0, 999)), NOW)

    def test_iso_utc_default_is_now(self):
        self.assertRegex(marks.iso_utc(), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


class IndexIoTest(TempHome, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.root = make_ws(self.home)
        self.path = ws_paths(self.root)["logs_index"]

    def test_load_missing_is_none(self):
        self.path.unlink()
        self.assertIsNone(marks.load_index(self.root))

    def test_load_broken_json_code1(self):
        self.path.write_text("{oops", encoding="utf-8")
        with self.assertRaises(SrError) as ctx:
            marks.load_index(self.root)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn(str(self.path), ctx.exception.message)

    def test_load_wrong_shape_code1(self):
        self.path.write_text('{"schema_version": 1, "logs": {}}', encoding="utf-8")
        with self.assertRaises(SrError) as ctx:
            marks.load_index(self.root)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("logs", ctx.exception.message)

    def test_save_sets_updated_at_and_roundtrips(self):
        index = marks.load_index(self.root)
        marks.save_index(self.root, index, now=datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc))
        again = marks.load_index(self.root)
        self.assertEqual(again["updated_at"], NOW)
        self.assertEqual(len(again["logs"]), 7)

    def test_save_inside_index_lock_does_not_deadlock(self):
        with marks.index_lock(self.root):
            index = marks.load_index(self.root)
            marks.save_index(self.root, index, timeout=0.5)
        with marks.index_lock(self.root, timeout=0.5):
            pass

    def test_index_lock_uses_collect_lock(self):
        # тот же lock-файл, что у сбора логов: `file_lock(<.sessionretro>/collect)` → `collect.lock`
        with marks.index_lock(self.root) as lock_path:
            self.assertEqual(Path(lock_path), ws_paths(self.root)["state_dir"] / "collect.lock")
            with marks.index_lock(self.root) as inner:
                self.assertEqual(Path(inner), Path(lock_path))

    def test_save_when_collect_lock_held_elsewhere_code1(self):
        index = marks.load_index(self.root)
        before = self.path.read_bytes()
        with file_lock(ws_paths(self.root)["state_dir"] / "collect"):
            with self.assertRaises(SrError) as ctx:
                marks.save_index(self.root, index, timeout=0.3)
        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(self.path.read_bytes(), before)

    def test_save_when_collect_lock_held_by_other_process_code1(self):
        index = marks.load_index(self.root)
        target = ws_paths(self.root)["state_dir"] / "collect"
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        child = subprocess.Popen(
            [sys.executable, "-c", CHILD, str(SCRIPTS), str(target)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )
        try:
            self.assertEqual(child.stdout.readline().strip(), "locked")
            with self.assertRaises(SrError) as ctx:
                marks.save_index(self.root, index, timeout=0.3)
            self.assertEqual(ctx.exception.code, 1)
        finally:
            child.kill()
            child.wait(timeout=10)
            child.stdout.close()
            child.stderr.close()


class FindEntryTest(unittest.TestCase):
    def setUp(self):
        self.index = fixture_index()

    def test_by_session_id(self):
        self.assertEqual(marks.find_entry(self.index, "alex", LOGIN)["session_id"], LOGIN)

    def test_by_unique_prefix(self):
        self.assertEqual(marks.find_entry(self.index, "alex", "3f9a2c71")["session_id"], LOGIN)
        self.assertEqual(marks.find_entry(self.index, "alex", "3f9a2c71-8d4e")["session_id"], LOGIN)

    def test_prefix_shorter_than_8_is_not_used(self):
        self.assertIsNone(marks.find_entry(self.index, "alex", "3f9a2c7"))

    def test_ambiguous_prefix_code1_lists_candidates(self):
        twin = copy.deepcopy(entry_by_sid(self.index, LOGIN))
        twin["session_id"] = "3f9a2c71-0000-0000-0000-000000000000"
        twin["key"] = "twin"
        twin["path"] = "logs/alex/twin.md"
        self.index["logs"].append(twin)
        with self.assertRaises(SrError) as ctx:
            marks.find_entry(self.index, "alex", "3f9a2c71")
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn(LOGIN, ctx.exception.message)
        self.assertIn("twin", ctx.exception.message)
        self.assertIn("alex", ctx.exception.message)

    def test_by_key_and_path(self):
        self.assertEqual(
            marks.find_entry(self.index, "alex", "2026-09-04_16-00-00Z-quick-question")["session_id"], SHORT)
        self.assertEqual(
            marks.find_entry(self.index, "alex", "logs/alex/2026-09-04_16-00-00Z-quick-question.md")["session_id"],
            SHORT)

    def test_same_key_and_tail_resolve_to_own_person(self):
        for ref in ("2026-09-08_14-00-00Z-fix-tests", "2026-09-08_14-00-00Z-fix-tests.md"):
            self.assertEqual(marks.find_entry(self.index, "alex", ref)["session_id"], FIX_ALEX, ref)
            self.assertEqual(marks.find_entry(self.index, "sam", ref)["session_id"], FIX_SAM, ref)

    def test_longer_path_resolves_by_reverse_tail(self):
        ref = "/home/user/demo/.sessionretro/logs/alex/2026-09-08_14-00-00Z-fix-tests.md"
        self.assertEqual(marks.find_entry(self.index, "alex", ref)["session_id"], FIX_ALEX)

    def test_tail_respects_path_boundary(self):
        self.assertIsNone(marks.find_entry(self.index, "alex", "tests.md"))

    def test_other_person_session_not_found(self):
        self.assertIsNone(marks.find_entry(self.index, "alex", FIX_SAM))
        self.assertIsNone(marks.find_entry(self.index, "nobody", LOGIN))

    def test_gone_is_skipped(self):
        self.assertIsNone(marks.find_entry(self.index, "sam", GONE_SAM))

    def test_gone_does_not_shadow_live(self):
        live = entry_by_sid(self.index, LOGIN)
        ghost = copy.deepcopy(live)
        ghost["state"] = "gone"
        ghost["key"] = "old-key"
        self.index["logs"].insert(0, ghost)
        for ref in (LOGIN, live["path"], "3f9a2c71"):
            self.assertIs(marks.find_entry(self.index, "alex", ref), live, ref)

    def test_duplicate_is_skipped(self):
        live = entry_by_sid(self.index, LOGIN)
        dup = copy.deepcopy(live)
        dup["key"] = live["key"] + "#dup:x"
        dup["duplicate_of"] = live["key"]
        self.index["logs"].append(dup)
        self.assertIs(marks.find_entry(self.index, "alex", LOGIN), live)

    def test_ambiguous_exact_match_code1(self):
        twin = copy.deepcopy(entry_by_sid(self.index, LOGIN))
        twin["key"] = "twin"
        twin["path"] = "logs/alex/twin.md"
        self.index["logs"].append(twin)
        with self.assertRaises(SrError) as ctx:
            marks.find_entry(self.index, "alex", LOGIN)
        self.assertEqual(ctx.exception.code, 1)

    def test_empty_ref_code2(self):
        with self.assertRaises(SrError) as ctx:
            marks.find_entry(self.index, "alex", "  ")
        self.assertEqual(ctx.exception.code, 2)


class MarkTest(unittest.TestCase):
    def setUp(self):
        self.index = fixture_index()

    def assert_not_both(self, entry):
        self.assertFalse(entry.get("reviewed") and entry.get("excluded"), entry)

    def test_mark_reviewed_new(self):
        entry = entry_by_sid(self.index, LOGIN)
        mark = marks.mark_reviewed(entry, RUN, NOW)
        self.assertTrue(entry["reviewed"])
        self.assertEqual(entry["reviewed_in_run"], RUN)
        self.assertEqual(entry["reviewed_at"], NOW)
        self.assertEqual(entry["reviewed_turns_total"], 24)
        self.assertEqual(entry["reviewed_bytes"], 18400)
        self.assertEqual(entry["state"], "unchanged")
        self.assertEqual(mark["kind"], "reviewed")
        self.assertEqual(mark["key"], entry["key"])
        self.assertEqual(mark["session_id"], LOGIN)
        self.assertEqual(mark["path"], entry["path"])
        self.assertEqual(mark["marked"], {"reviewed_turns_total": 24, "reviewed_bytes": 18400})
        self.assertEqual(mark["prev"]["state"], "new")
        self.assertFalse(mark["prev"]["reviewed"])

    def test_mark_reviewed_grown_drops_review_from_turn(self):
        entry = entry_by_sid(self.index, GROWN)
        mark = marks.mark_reviewed(entry, RUN, NOW)
        self.assertNotIn("review_from_turn", entry)
        self.assertEqual(entry["reviewed_turns_total"], 18)
        self.assertEqual(entry["state"], "unchanged")
        self.assertEqual(mark["prev"]["review_from_turn"], 10)
        self.assertEqual(mark["prev"]["reviewed_turns_total"], 10)

    def test_mark_excluded(self):
        entry = entry_by_sid(self.index, SHORT)
        mark = marks.mark_excluded(entry, RUN, "transcript_too_short", "две реплики", NOW)
        self.assertTrue(entry["excluded"])
        self.assertFalse(entry["reviewed"])
        self.assertEqual(entry["excluded_in_run"], RUN)
        self.assertEqual(entry["excluded_at"], NOW)
        self.assertEqual(entry["excluded_reason"], "transcript_too_short")
        self.assertEqual(entry["excluded_note"], "две реплики")
        self.assertEqual(entry["excluded_turns_total"], 4)
        self.assertEqual(entry["state"], "excluded")
        self.assertEqual(mark["kind"], "excluded")
        self.assertEqual(mark["reason"], "transcript_too_short")
        self.assertEqual(mark["note"], "две реплики")
        self.assertEqual(mark["prev"]["state"], "new")

    def test_mark_excluded_default_reason(self):
        entry = entry_by_sid(self.index, SHORT)
        mark = marks.mark_excluded(entry, RUN, None, None, NOW)
        self.assertEqual(entry["excluded_reason"], "not_batched")
        self.assertEqual(mark["reason"], "not_batched")
        self.assertIsNone(entry["excluded_note"])

    def test_mark_excluded_unknown_reason_code2_untouched(self):
        entry = entry_by_sid(self.index, SHORT)
        before = copy.deepcopy(entry)
        with self.assertRaises(SrError) as ctx:
            marks.mark_excluded(entry, RUN, "bored", None, NOW)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("bored", ctx.exception.message)
        self.assertEqual(entry, before)

    def test_reviewed_then_excluded_then_reviewed_never_both(self):
        entry = entry_by_sid(self.index, GROWN)
        marks.mark_reviewed(entry, RUN, NOW)
        self.assert_not_both(entry)
        marks.mark_excluded(entry, NEWER, "other_period", None, NOW)
        self.assert_not_both(entry)
        self.assertFalse(entry["reviewed"])
        self.assertIsNone(entry["reviewed_in_run"])
        self.assertIsNone(entry["reviewed_at"])
        self.assertIsNone(entry["reviewed_turns_total"])
        self.assertEqual(entry["state"], "excluded")
        marks.mark_reviewed(entry, RUN, NOW)
        self.assert_not_both(entry)
        # формат сбора: у неисключённой записи полей исключения нет вовсе
        for field in marks.EXCLUDED_FIELDS:
            self.assertNotIn(field, entry)
        self.assertEqual(entry["state"], "unchanged")


class UnmarkTest(unittest.TestCase):
    def setUp(self):
        self.index = fixture_index()

    def test_unmark_reviewed_restores_prev(self):
        entry = entry_by_sid(self.index, GROWN)
        before = copy.deepcopy(entry)
        mark = marks.mark_reviewed(entry, RUN, NOW)
        self.assertEqual(marks.unmark(entry, mark, set()), "restored")
        self.assertEqual(entry, before)

    def test_unmark_excluded_restores_prev(self):
        entry = entry_by_sid(self.index, LOGIN)
        before = copy.deepcopy(entry)
        mark = marks.mark_excluded(entry, RUN, "duplicate_export", "копия", NOW)
        self.assertEqual(marks.unmark(entry, mark, set()), "restored")
        self.assertEqual(entry, before)

    def test_unmark_kept_when_newer_run_marked(self):
        entry = entry_by_sid(self.index, LOGIN)
        mark = marks.mark_reviewed(entry, RUN, NOW)
        marks.mark_reviewed(entry, NEWER, "2026-09-20T12:00:00Z")
        snapshot = copy.deepcopy(entry)
        self.assertEqual(marks.unmark(entry, mark, {NEWER}), "kept_newer")
        self.assertEqual(entry, snapshot)

    def test_unmark_kept_when_newer_run_excluded(self):
        entry = entry_by_sid(self.index, LOGIN)
        mark = marks.mark_reviewed(entry, RUN, NOW)
        marks.mark_excluded(entry, NEWER, "other_period", None, "2026-09-20T12:00:00Z")
        self.assertEqual(marks.unmark(entry, mark, {NEWER}), "kept_newer")
        self.assertTrue(entry["excluded"])

    def test_unmark_foreign_entry_code1(self):
        entry = entry_by_sid(self.index, LOGIN)
        mark = marks.mark_reviewed(entry, RUN, NOW)
        other = entry_by_sid(self.index, SHORT)
        with self.assertRaises(SrError) as ctx:
            marks.unmark(other, mark, set())
        self.assertEqual(ctx.exception.code, 1)


class CoverageTest(unittest.TestCase):
    def setUp(self):
        self.index = fixture_index()
        self.run = {
            "run_id": RUN, "person_id": "alex",
            "sessions_reviewed": [
                {"session_id": LOGIN, "path": "logs/alex/a.md", "status": "reviewed"},
                {"session_id": FIX_ALEX, "path": "logs/alex/b.md", "status": "reviewed"},
                {"session_id": SHORT, "path": "logs/alex/c.md", "status": "transcript_too_short"},
            ],
            "logs_index_marks": [],
        }

    def mark_all(self):
        for sid in (LOGIN, FIX_ALEX):
            self.run["logs_index_marks"].append(
                marks.mark_reviewed(entry_by_sid(self.index, sid), RUN, NOW))
        self.run["logs_index_marks"].append(marks.mark_excluded(
            entry_by_sid(self.index, SHORT), RUN, "transcript_too_short", None, NOW))

    def test_full_coverage(self):
        self.mark_all()
        self.assertEqual(marks.coverage(self.run, self.index), (3, 3))

    def test_partial_coverage(self):
        self.run["logs_index_marks"].append(
            marks.mark_reviewed(entry_by_sid(self.index, LOGIN), RUN, NOW))
        self.assertEqual(marks.coverage(self.run, self.index), (1, 3))

    def test_mark_lost_in_index_not_counted(self):
        self.mark_all()
        entry = entry_by_sid(self.index, FIX_ALEX)
        entry["reviewed"] = False
        entry["reviewed_in_run"] = None
        self.assertEqual(marks.coverage(self.run, self.index), (2, 3))

    def test_newer_run_mark_still_counts(self):
        self.mark_all()
        marks.mark_reviewed(entry_by_sid(self.index, LOGIN), NEWER, "2026-09-20T12:00:00Z")
        self.assertEqual(marks.coverage(self.run, self.index), (3, 3))

    def test_other_period_not_expected(self):
        """Сессия чужого периода запись индекса не закрывает, поэтому и в ожидаемое покрытие не входит."""
        self.mark_all()
        self.run["sessions_reviewed"].append({"session_id": "x", "path": "logs/alex/x.md", "status": "other_period"})
        self.assertEqual(marks.coverage(self.run, self.index), (3, 3))

    def test_no_index(self):
        self.mark_all()
        self.assertEqual(marks.coverage(self.run, None), (0, 3))


if __name__ == "__main__":
    unittest.main()
