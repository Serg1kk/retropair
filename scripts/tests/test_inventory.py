"""Инвентарь логов (`lib/inventory.py`) и `sr.py inventory`: состояния merge, рост только по ходам,
reviewed/excluded взаимоисключающие, дубли с приоритетом `claude-jsonl`, контракт `mark`,
`period_guess`, скан снапшотов с сайдкаром `.meta.json`, фикстура `logs-index.sample.json`.

Все пути — во временных папках; HOME подменён (TempHome).
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import unittest.mock
from pathlib import Path

import _util  # noqa: F401 — кладёт scripts/ в sys.path
from _util import FIXTURES, SCRIPTS, TempHome, copy_fixture, run_sr

from lib import cmd_inventory, inventory, lock, normalize
from lib.workspace import SrError

# отдельный процесс держит замок `argv[2]` `argv[3]` секунд (как хук с долгим collect)
HOLDER = textwrap.dedent(
    """
    import sys, time
    sys.path.insert(0, sys.argv[1])
    from pathlib import Path
    from lib.lock import file_lock
    with file_lock(Path(sys.argv[2]), timeout=5):
        print("locked", flush=True)
        time.sleep(float(sys.argv[3]))
    """
)

NOW = "2026-09-27T12:00:00Z"
LOGS = FIXTURES / "logs"
SAMPLE = FIXTURES / "logs-index.sample.json"
STATES = {"new", "grown", "unchanged", "duplicate", "gone", "excluded"}
EXCLUDED_FIELDS = ("excluded", "excluded_in_run", "excluded_at", "excluded_reason",
                   "excluded_note", "excluded_turns_total", "excluded_bytes")


def entry(**kw) -> dict:
    key = kw.pop("key", "k")
    person = kw.pop("person_id", "me")
    rec = {
        "person_id": person,
        "source": {"type": "specstory", "id": "specstory:0"},
        "session_id": None,
        "key": key,
        "path": kw.pop("path", f"logs/{person}/{key}.md"),
        "source_path": "/home/user/demo/.specstory/history/x.md",
        "format": "specstory",
        "sha": "0" * 64,
        "bytes": 1000,
        "turns_total": 10,
        "user_turns": 5,
        "log_started_at": "2026-09-01T10:00:00Z",
        "log_ended_at": "2026-09-01T11:00:00Z",
        "started_at_source": "session_header",
        "collector": "SpecStory",
        "models": [],
        "period_guess": None,
        "period_guess_source": "none",
        "metrics": {},
        "sanitize": {"rules_hit": {}, "renamed": 0},
    }
    rec.update(kw)
    return rec


def idx(*records) -> dict:
    return {"schema_version": 1, "workspace": "ws", "updated_at": "2026-09-01T00:00:00Z",
            "logs": list(records), "warnings": []}


def by_key(index: dict) -> dict:
    return {r["key"]: r for r in index["logs"]}


class MergeStatesTest(unittest.TestCase):
    def test_new_without_prev(self):
        out = inventory.merge(None, [entry(key="k")], "ws", NOW)
        rec = out["logs"][0]
        self.assertEqual(rec["state"], "new")
        self.assertFalse(rec["reviewed"])
        self.assertIsNone(rec["reviewed_turns_total"])
        self.assertEqual(out["schema_version"], 1)
        self.assertEqual(out["workspace"], "ws")
        self.assertEqual(out["updated_at"], NOW)
        self.assertEqual(out["warnings"], [])

    def test_not_reviewed_prev_stays_new(self):
        out = inventory.merge(idx(entry(key="k", state="new", reviewed=False)),
                              [entry(key="k", turns_total=14)], "ws", NOW)
        self.assertEqual(out["logs"][0]["state"], "new")

    def test_bytes_growth_is_not_grown(self):        # баг 3
        prev = idx(entry(key="k", reviewed=True, reviewed_turns_total=10, reviewed_bytes=1000))
        out = inventory.merge(prev, [entry(key="k", turns_total=10, bytes=1200)], "ws", NOW)
        self.assertEqual(out["logs"][0]["state"], "unchanged")
        self.assertIsNone(out["logs"][0].get("review_from_turn"))

    def test_turn_growth_is_grown(self):
        prev = idx(entry(key="k", reviewed=True, reviewed_in_run="r1", reviewed_at="2026-09-02",
                         reviewed_turns_total=10, reviewed_bytes=1000))
        out = inventory.merge(prev, [entry(key="k", turns_total=14, bytes=1400)], "ws", NOW)
        rec = out["logs"][0]
        self.assertEqual(rec["state"], "grown")
        self.assertEqual(rec["review_from_turn"], 10)
        self.assertTrue(rec["reviewed"])
        self.assertEqual(rec["reviewed_in_run"], "r1")
        self.assertEqual(rec["reviewed_turns_total"], 10)

    def test_review_from_turn_not_carried_to_unchanged(self):
        prev = idx(entry(key="k", reviewed=True, reviewed_turns_total=14, reviewed_bytes=1400,
                         review_from_turn=10, state="grown"))
        out = inventory.merge(prev, [entry(key="k", turns_total=14)], "ws", NOW)
        self.assertEqual(out["logs"][0]["state"], "unchanged")
        self.assertNotIn("review_from_turn", out["logs"][0])

    def test_excluded_stays_on_bytes(self):          # баг 3, ветка excluded
        prev = idx(entry(key="k", reviewed=False, excluded=True, excluded_in_run="r1",
                         excluded_at="2026-09-02", excluded_reason="short", excluded_note=None,
                         excluded_turns_total=10, excluded_bytes=1000))
        out = inventory.merge(prev, [entry(key="k", turns_total=10, bytes=1500)], "ws", NOW)
        rec = out["logs"][0]
        self.assertEqual(rec["state"], "excluded")
        self.assertTrue(rec["excluded"])
        self.assertEqual(rec["excluded_reason"], "short")
        self.assertFalse(rec["reviewed"])

    def test_excluded_grown_by_turns_becomes_new(self):
        prev = idx(entry(key="k", reviewed=False, excluded=True, excluded_in_run="r1",
                         excluded_at="2026-09-02", excluded_reason="short",
                         excluded_turns_total=10, excluded_bytes=1000))
        out = inventory.merge(prev, [entry(key="k", turns_total=12)], "ws", NOW)
        rec = out["logs"][0]
        self.assertEqual(rec["state"], "new")
        for field in EXCLUDED_FIELDS:
            self.assertNotIn(field, rec)

    def test_merge_never_both(self):
        prev = idx(entry(key="k", reviewed=True, reviewed_at="2026-09-02", reviewed_turns_total=10,
                         excluded=True, excluded_at="2026-09-01", excluded_turns_total=10))
        out = inventory.merge(prev, [entry(key="k")], "ws", NOW)
        rec = out["logs"][0]
        self.assertFalse(rec.get("reviewed") and rec.get("excluded"))
        self.assertTrue(rec["reviewed"])                 # reviewed позже
        self.assertEqual(rec["state"], "unchanged")
        self.assertTrue(out["warnings"])
        self.assertIn("k", out["warnings"][0])

    def test_merge_both_later_excluded_wins(self):
        prev = idx(entry(key="k", reviewed=True, reviewed_at="2026-09-01", reviewed_turns_total=10,
                         excluded=True, excluded_at="2026-09-03", excluded_turns_total=10))
        out = inventory.merge(prev, [entry(key="k")], "ws", NOW)
        rec = out["logs"][0]
        self.assertEqual(rec["state"], "excluded")
        self.assertFalse(rec["reviewed"])
        self.assertIsNone(rec["reviewed_at"])
        self.assertEqual(len(out["warnings"]), 1)

    def test_gone(self):
        prev = idx(entry(key="k", reviewed=True, reviewed_turns_total=10, state="unchanged"),
                   entry(key="other", state="new", reviewed=False))
        out = inventory.merge(prev, [entry(key="other")], "ws", NOW)
        recs = by_key(out)
        self.assertEqual(recs["k"]["state"], "gone")
        self.assertTrue(recs["k"]["reviewed"])           # история оценки сохраняется
        self.assertEqual(recs["other"]["state"], "new")
        self.assertEqual(len(out["logs"]), 2)

    def test_gone_never_both(self):
        prev = idx(entry(key="k", reviewed=True, reviewed_at="2026-09-02",
                         excluded=True, excluded_at="2026-09-01"))
        out = inventory.merge(prev, [], "ws", NOW)
        rec = out["logs"][0]
        self.assertEqual(rec["state"], "gone")
        self.assertFalse(rec.get("reviewed") and rec.get("excluded"))
        self.assertTrue(out["warnings"])

    def test_same_key_other_person_is_separate(self):
        prev = idx(entry(key="k", person_id="alex", reviewed=True, reviewed_turns_total=10))
        out = inventory.merge(prev, [entry(key="k", person_id="sam")], "ws", NOW)
        states = {(r["person_id"], r["state"]) for r in out["logs"]}
        self.assertEqual(states, {("sam", "new"), ("alex", "gone")})

    def test_no_consent_excludes_new_and_reviewed(self):     # B02
        reviewed = inventory.mark(entry(key="r"), "reviewed", "r1", "2026-09-02T00:00:00Z")
        reviewed["state"] = "unchanged"
        out = inventory.merge(idx(reviewed), [entry(key="r"), entry(key="n"),
                                              entry(key="x", person_id="sam")],
                              "ws", NOW, no_consent={"me"})
        recs = by_key(out)
        for key in ("r", "n"):
            self.assertEqual(recs[key]["state"], "excluded")
            self.assertEqual((recs[key]["excluded_reason"], recs[key]["excluded_by"]), ("consent", "consent"))
            self.assertIs(recs[key]["reviewed"], False)
            self.assertEqual(recs[key]["excluded_at"], NOW)
        self.assertEqual(recs["x"]["state"], "new")
        self.assertEqual(out["warnings"], [inventory.consent_warning("me", 2)])
        later = inventory.merge(out, [entry(key="r", turns_total=14)], "ws", "2026-09-28T00:00:00Z",
                                no_consent={"me"})
        self.assertEqual(by_key(later)["r"]["state"], "excluded")
        self.assertEqual(by_key(later)["r"]["excluded_at"], NOW)      # дата отзыва не сдвигается
        back = inventory.merge(later, [entry(key="r", turns_total=14)], "ws", NOW)
        self.assertEqual(by_key(back)["r"]["state"], "new")
        self.assertNotIn("excluded_by", by_key(back)["r"])
        remarked = inventory.mark(by_key(later)["r"], "excluded", "r2", NOW, reason="other_period")
        self.assertNotIn("excluded_by", remarked)

    def test_no_consent_keeps_assessment_exclusion(self):     # B02
        marked = inventory.mark(entry(key="k"), "excluded", "r1", NOW, reason="other_period")
        out = inventory.merge(idx(marked), [entry(key="k")], "ws", NOW, no_consent={"me"})
        rec = by_key(out)["k"]
        self.assertEqual((rec["state"], rec["excluded_reason"], rec["excluded_in_run"]),
                         ("excluded", "other_period", "r1"))
        self.assertNotIn("excluded_by", rec)
        back = inventory.merge(out, [entry(key="k")], "ws", NOW)
        self.assertEqual((by_key(back)["k"]["state"], by_key(back)["k"]["excluded_reason"]),
                         ("excluded", "other_period"))

    def test_duplicate_state(self):
        a = entry(key="s1", path="logs/me/a.md", turns_total=30)
        b = entry(key="s1", path="logs/me/b.md", turns_total=12)
        out = inventory.merge(None, [a, b], "ws", NOW)
        states = sorted(r["state"] for r in out["logs"])
        self.assertEqual(states, ["duplicate", "new"])
        dup = next(r for r in out["logs"] if r["state"] == "duplicate")
        self.assertEqual(dup["duplicate_of"], "logs/me/a.md")
        self.assertFalse(dup["reviewed"])


class CollapseTest(unittest.TestCase):
    def test_jsonl_wins_duplicate(self):             # q-04
        a = entry(key="s1", path="logs/me/s1.md", format="specstory", turns_total=30)
        b = entry(key="s1", path="logs/me/s1.jsonl", format="claude-jsonl", turns_total=12)
        res = {e["format"]: e for e in inventory.collapse_duplicates([a, b])}
        self.assertEqual(res["specstory"]["duplicate_of"], b["path"])
        self.assertEqual(res["specstory"]["key"], "s1#dup:logs/me/s1.md")
        self.assertEqual(res["claude-jsonl"]["key"], "s1")
        self.assertNotIn("duplicate_of", res["claude-jsonl"])

    def test_same_format_turns_then_bytes_then_path(self):
        a = entry(key="s", path="logs/me/a.md", turns_total=10, bytes=100)
        b = entry(key="s", path="logs/me/b.md", turns_total=10, bytes=200)
        c = entry(key="s", path="logs/me/c.md", turns_total=9, bytes=900)
        out = inventory.collapse_duplicates([a, b, c])
        winner = next(e for e in out if e["key"] == "s")
        self.assertEqual(winner["path"], "logs/me/b.md")
        d = entry(key="t", path="logs/me/d.md")
        e = entry(key="t", path="logs/me/e.md")
        out = inventory.collapse_duplicates([e, d])
        self.assertEqual(next(x for x in out if x["key"] == "t")["path"], "logs/me/d.md")

    def test_collapse_is_idempotent(self):
        a = entry(key="s1", path="logs/me/a.md", turns_total=30)
        b = entry(key="s1", path="logs/me/b.md", turns_total=12)
        once = inventory.collapse_duplicates([a, b])
        self.assertEqual(inventory.collapse_duplicates(once), once)

    def test_inputs_not_mutated(self):
        a = entry(key="s1", path="logs/me/a.md", turns_total=30)
        b = entry(key="s1", path="logs/me/b.md", turns_total=12)
        inventory.collapse_duplicates([a, b])
        self.assertEqual(b["key"], "s1")
        self.assertNotIn("duplicate_of", b)


class MarkTest(unittest.TestCase):
    def test_mark_exclusive(self):                   # баг 4
        e = inventory.mark(entry(), "excluded", "r1", NOW, reason="x")
        e = inventory.mark(e, "reviewed", "r2", NOW)
        self.assertTrue(e["reviewed"])
        self.assertNotIn("excluded", e)
        e = inventory.mark(e, "excluded", "r3", NOW, reason="y")
        self.assertFalse(e.get("reviewed"))

    def test_mark_reviewed_fields(self):
        src = entry(turns_total=14, bytes=1400, review_from_turn=10, excluded=True,
                    excluded_in_run="r0", excluded_at="2026-09-01", excluded_reason="short",
                    excluded_note="n", excluded_turns_total=3, excluded_bytes=30)
        e = inventory.mark(src, "reviewed", "r2", "2026-09-05T00:00:00Z")
        self.assertIs(e["reviewed"], True)
        self.assertEqual(e["reviewed_in_run"], "r2")
        self.assertEqual(e["reviewed_at"], "2026-09-05T00:00:00Z")
        self.assertEqual(e["reviewed_turns_total"], 14)
        self.assertEqual(e["reviewed_bytes"], 1400)
        self.assertNotIn("review_from_turn", e)
        for field in EXCLUDED_FIELDS:
            self.assertNotIn(field, e)
        self.assertIn("excluded", src)                   # вход не изменён

    def test_mark_excluded_fields(self):
        src = entry(turns_total=7, bytes=700, reviewed=True, reviewed_in_run="r1",
                    reviewed_at="2026-09-02", reviewed_turns_total=5, reviewed_bytes=500,
                    review_from_turn=5)
        e = inventory.mark(src, "excluded", "r3", "2026-09-06T00:00:00Z", reason="subagent",
                           note="транскрипт сабагента")
        self.assertIs(e["excluded"], True)
        self.assertEqual(e["excluded_in_run"], "r3")
        self.assertEqual(e["excluded_at"], "2026-09-06T00:00:00Z")
        self.assertEqual(e["excluded_reason"], "subagent")
        self.assertEqual(e["excluded_note"], "транскрипт сабагента")
        self.assertEqual(e["excluded_turns_total"], 7)
        self.assertEqual(e["excluded_bytes"], 700)
        self.assertIs(e["reviewed"], False)
        for field in ("reviewed_in_run", "reviewed_at", "reviewed_turns_total", "reviewed_bytes"):
            self.assertIsNone(e[field])
        self.assertNotIn("review_from_turn", e)

    def test_mark_excluded_note_optional(self):
        e = inventory.mark(entry(), "excluded", "r1", NOW, reason="x")
        self.assertIsNone(e["excluded_note"])

    def test_mark_unknown_flag(self):
        with self.assertRaises(SrError) as ctx:
            inventory.mark(entry(), "done", "r1", NOW)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("done", ctx.exception.message)

    def test_mark_excluded_requires_reason(self):
        with self.assertRaises(SrError) as ctx:
            inventory.mark(entry(), "excluded", "r1", NOW)
        self.assertEqual(ctx.exception.code, 2)

    def test_mark_rejects_unknown_extra(self):
        with self.assertRaises(SrError) as ctx:
            inventory.mark(entry(), "reviewed", "r1", NOW, reason="x")
        self.assertEqual(ctx.exception.code, 2)
        with self.assertRaises(SrError):
            inventory.mark(entry(), "excluded", "r1", NOW, reason="x", why="y")

    def test_mark_then_merge(self):
        rec = inventory.merge(None, [entry(key="k", turns_total=10)], "ws", NOW)["logs"][0]
        marked = inventory.mark(rec, "reviewed", "r1", NOW)
        out = inventory.merge(idx(marked), [entry(key="k", turns_total=10, bytes=5000)], "ws", NOW)
        self.assertEqual(out["logs"][0]["state"], "unchanged")
        out = inventory.merge(idx(marked), [entry(key="k", turns_total=13)], "ws", NOW)
        self.assertEqual(out["logs"][0]["state"], "grown")
        self.assertEqual(out["logs"][0]["review_from_turn"], 10)


class KeysAndPeriodTest(unittest.TestCase):
    def test_session_key_order(self):
        self.assertEqual(inventory.session_key({"session_id": "abc", "started_at": "t"}, "a.md"), "abc")
        self.assertEqual(inventory.session_key({"session_id": None, "started_at": "2026-09-01T10:00:00Z"},
                                               "a.md"), "ts:2026-09-01T10:00:00Z")
        self.assertEqual(inventory.session_key({"session_id": None, "started_at": None}, "x/a.md"),
                         "path:x/a.md")

    def test_snapshot_name(self):
        self.assertEqual(inventory.snapshot_name("abc-1", "claude-jsonl"), "abc-1.jsonl")
        self.assertEqual(inventory.snapshot_name("ts:2026-09-01T10:00:00Z", "specstory"),
                         "ts_2026-09-01T10_00_00Z.md")
        self.assertEqual(inventory.snapshot_name("path:a b/c.md", "md-headings"), "path_a_b_c.md.md")

    def test_period_guess_dates(self):
        cfg = {"period": {"mode": "dates", "windows": [
            {"label": "M1", "from": "2026-09-01", "to": "2026-09-14"},
            {"label": "M2", "from": "2026-09-15", "to": None},
        ]}}
        self.assertEqual(inventory.period_guess(cfg, "2026-09-14T23:00:00Z"), ("M1", "date_window"))
        self.assertEqual(inventory.period_guess(cfg, "2026-09-15T00:10:00Z"), ("M2", "date_window"))
        self.assertEqual(inventory.period_guess(cfg, "2026-08-20T10:00:00Z"), (None, "unknown"))
        self.assertEqual(inventory.period_guess(cfg, None), (None, "unknown"))

    def test_period_guess_run_and_labels(self):
        for mode in ("run", "labels"):
            cfg = {"period": {"mode": mode}}
            self.assertEqual(inventory.period_guess(cfg, "2026-09-01T10:00:00Z"), (None, "none"))

    def test_period_guess_bad_mode(self):
        with self.assertRaises(SrError):
            inventory.period_guess({"period": {"mode": "weeks"}}, "2026-09-01")

    def test_validate_period_ok(self):
        for cfg in ({}, {"period": {"mode": "run"}}, {"period": {"mode": "labels"}},
                    {"period": {"mode": "dates", "windows": [{"label": "M1", "from": "2026-09-01"}]}}):
            inventory.validate_period(cfg)

    def test_validate_period_errors_name_the_field(self):
        cases = [
            ({"period": "run"}, "period"),
            ({"period": {"mode": "weeks"}}, "period.mode"),
            ({"period": {"mode": "dates", "windows": "M1"}}, "period.windows"),
            ({"period": {"mode": "dates", "windows": ["M1"]}}, "period.windows[0]"),
            ({"period": {"mode": "dates", "windows": [{"from": "2026-09-01"}]}},
             "period.windows[0].label"),
            ({"period": {"mode": "dates", "windows": [{"label": "M1"}, {"label": " "}]}},
             "period.windows[1].label"),
            ({"period": {"mode": "dates", "windows": [{"label": "M1", "from": 20260901}]}},
             "period.windows[0].from"),
        ]
        for cfg, field in cases:
            with self.subTest(field=field):
                with self.assertRaises(SrError) as cm:
                    inventory.validate_period(cfg)
                self.assertEqual(cm.exception.code, 1)
                self.assertIn(field, cm.exception.message)


def _ws(tmp: Path) -> Path:
    root = tmp / "proj"
    (root / ".sessionretro").mkdir(parents=True)
    return root


CFG = {"workspace": {"id": "ws"}, "period": {"mode": "run"}}


class ScanTest(TempHome, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="sr-inv-")
        self.root = _ws(Path(self._tmp.name))
        self.me = self.root / ".sessionretro" / "logs" / "me"
        self.me.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()
        super().tearDown()

    def test_scan_empty_without_logs_dir(self):
        shutil.rmtree(self.root / ".sessionretro" / "logs")
        entries, junk = inventory.scan(self.root, CFG)
        self.assertEqual(entries, [])
        self.assertEqual(junk["unparsed"], [])

    def test_scan_real_snapshots(self):
        md_doc = normalize.parse_file(LOGS / "specstory-claude.md")
        md_name = inventory.snapshot_name(md_doc["session_id"], "specstory")
        shutil.copy(LOGS / "specstory-claude.md", self.me / md_name)
        js_doc = normalize.parse_file(LOGS / "claude-main.jsonl")
        js_name = inventory.snapshot_name(js_doc["session_id"], "claude-jsonl")
        shutil.copy(LOGS / "claude-main.jsonl", self.me / js_name)
        inventory.save_meta(self.me, {md_name: {
            "source": {"type": "specstory", "id": "specstory:0"},
            "source_path": "/home/user/demo/.specstory/history/2026-09-01_10-00-00Z-add.md",
            "source_sha256": "a" * 64,
            "sanitize": {"rules_hit": {"email": 1}, "renamed": 2},
            "key": "src-key",
            "session_id": "src-session",
            "log_started_at": "2026-09-01T09:00:00Z",
            "log_ended_at": "2026-09-01T12:00:00Z",
            "started_at_source": "file_name",
            "unparsed_lines": 3,
            "unknown_types": {"odd": 1},
        }})
        entries, junk = inventory.scan(self.root, CFG)
        self.assertEqual(junk["unparsed"], [])
        recs = {e["format"]: e for e in entries}
        self.assertEqual(set(recs), {"specstory", "claude-jsonl"})

        md = recs["specstory"]
        self.assertEqual(md["person_id"], "me")
        self.assertEqual(md["path"], f"logs/me/{md_name}")
        self.assertEqual(md["source"], {"type": "specstory", "id": "specstory:0"})
        self.assertTrue(md["source_path"].startswith("/home/user/"))
        self.assertEqual(md["sanitize"], {"rules_hit": {"email": 1}, "renamed": 2})
        self.assertEqual(md["key"], "src-key")
        self.assertEqual(md["session_id"], "src-session")
        self.assertEqual(md["log_started_at"], "2026-09-01T09:00:00Z")
        self.assertEqual(md["log_ended_at"], "2026-09-01T12:00:00Z")
        self.assertEqual(md["started_at_source"], "file_name")
        self.assertEqual(md["metrics"]["unparsed_lines"], 3)
        self.assertEqual(md["metrics"]["unknown_types"], {"odd": 1})
        self.assertEqual(md["sha"], hashlib.sha256((self.me / md_name).read_bytes()).hexdigest())
        self.assertEqual(md["turns_total"], md_doc["turns_total"])
        self.assertEqual(md["user_turns"], md_doc["user_turns"])
        self.assertEqual(md["bytes"], md_doc["bytes"])
        self.assertEqual(md["collector"], md_doc["collector"])
        self.assertEqual(md["models"], md_doc["models"])
        self.assertEqual(md["period_guess"], None)
        self.assertEqual(md["period_guess_source"], "none")

        js = recs["claude-jsonl"]                        # без сайдкара — из разбора снапшота
        self.assertEqual(js["key"], js_doc["session_id"])
        self.assertEqual(js["session_id"], js_doc["session_id"])
        self.assertEqual(js["log_started_at"], js_doc["started_at"])
        self.assertIsNone(js["source"])
        self.assertIsNone(js["source_path"])
        self.assertIsNone(js["sanitize"])
        self.assertEqual(js["metrics"]["unparsed_lines"], js_doc["mechanical_metrics"]["unparsed_lines"])
        for field in ("person_id", "source", "session_id", "key", "path", "source_path", "format",
                      "sha", "bytes", "turns_total", "user_turns", "log_started_at", "log_ended_at",
                      "collector", "models", "period_guess", "period_guess_source", "metrics",
                      "sanitize"):
            self.assertIn(field, js)

    def test_scan_period_from_sidecar_start(self):
        shutil.copy(LOGS / "specstory-claude.md", self.me / "a.md")
        inventory.save_meta(self.me, {"a.md": {"log_started_at": "2026-09-20T10:00:00Z"}})
        cfg = {"period": {"mode": "dates", "windows": [{"label": "W2", "from": "2026-09-15", "to": None}]}}
        entries, _ = inventory.scan(self.root, cfg)
        self.assertEqual((entries[0]["period_guess"], entries[0]["period_guess_source"]),
                         ("W2", "date_window"))

    def test_scan_key_fallback_to_path(self):
        (self.me / "hand.md").write_text("## User\n\nпривет\n\n## Assistant\n\nпривет\n",
                                         encoding="utf-8")
        real = normalize.parse_file

        def no_ids(path, *a, **kw):
            doc = real(path, *a, **kw)
            doc["session_id"] = None
            doc["started_at"] = None
            return doc

        with unittest.mock.patch("lib.inventory.normalize.parse_file", side_effect=no_ids):
            entries, _ = inventory.scan(self.root, CFG)
        self.assertEqual(entries[0]["key"], "path:logs/me/hand.md")

    def test_scan_zero_turns_goes_to_junk(self):
        (self.me / "empty.md").write_text("\n", encoding="utf-8")
        (self.me / "notes.txt").write_text("не лог\n", encoding="utf-8")   # не .md/.jsonl
        entries, junk = inventory.scan(self.root, CFG)
        self.assertEqual(entries, [])
        self.assertEqual([j["path"] for j in junk["unparsed"]], ["logs/me/empty.md"])
        self.assertIn("0", junk["unparsed"][0]["error"])

    def test_meta_roundtrip_and_broken(self):
        self.assertEqual(inventory.load_meta(self.me), {})
        inventory.save_meta(self.me, {"a.md": {"key": "k"}})
        self.assertEqual(inventory.load_meta(self.me), {"a.md": {"key": "k"}})
        self.assertTrue((self.me / inventory.META_FILE).is_file())
        (self.me / inventory.META_FILE).write_text("[1]", encoding="utf-8")
        with self.assertRaises(SrError):
            inventory.load_meta(self.me)

    def test_index_roundtrip_and_broken(self):
        self.assertIsNone(inventory.load_index(self.root))
        inventory.save_index(self.root, idx(entry()))
        self.assertEqual(inventory.load_index(self.root)["logs"][0]["key"], "k")
        path = self.root / ".sessionretro" / "logs-index.json"
        path.write_text('{"schema_version": 7, "logs": []}', encoding="utf-8")
        with self.assertRaises(SrError) as ctx:
            inventory.load_index(self.root)
        self.assertIn("schema_version", ctx.exception.message)

    def test_rebuild_writes_index_and_warnings(self):
        shutil.copy(LOGS / "specstory-claude.md", self.me / "a.md")
        (self.me / "empty.md").write_text("\n", encoding="utf-8")
        index = inventory.rebuild(self.root, CFG, now=NOW, warnings=["из collect"])
        self.assertEqual(index["updated_at"], NOW)
        self.assertEqual([r["state"] for r in index["logs"]], ["new"])
        self.assertIn("из collect", index["warnings"])
        self.assertTrue(any("empty.md" in w for w in index["warnings"]))
        self.assertEqual(inventory.load_index(self.root), index)
        # пометка + повтор → unchanged; дописанный ход → grown
        index["logs"][0] = inventory.mark(index["logs"][0], "reviewed", "r1", NOW)
        inventory.save_index(self.root, index)
        again = inventory.rebuild(self.root, CFG, now=NOW)
        self.assertEqual(again["logs"][0]["state"], "unchanged")
        self.assertFalse(any("из collect" in w for w in again["warnings"]))
        (self.me / "a.md").unlink()
        gone = inventory.rebuild(self.root, CFG, now=NOW)
        self.assertEqual(gone["logs"][0]["state"], "gone")

    def test_rebuild_bad_period_is_config_error_not_broken_snapshot(self):
        shutil.copy(LOGS / "specstory-claude.md", self.me / "a.md")
        cfg = {**CFG, "period": {"mode": "dates", "windows": [{"from": "2026-01-01"}]}}
        with unittest.mock.patch.object(inventory, "scan", wraps=inventory.scan) as scan:
            with self.assertRaises(SrError) as cm:
                inventory.rebuild(self.root, cfg, now=NOW)
        scan.assert_not_called()
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("period.windows[0].label", cm.exception.message)
        self.assertIsNone(inventory.load_index(self.root))

    def test_state_labels_cover_states(self):
        self.assertEqual([s for s, _ in inventory.STATE_LABELS], list(inventory.STATES))

    def test_rebuild_default_now_is_utc_z(self):
        index = inventory.rebuild(self.root, CFG)
        self.assertRegex(index["updated_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_summary(self):
        index = idx(entry(key="a", state="new"), entry(key="b", state="new"),
                    entry(key="c", state="gone"))
        s = inventory.summary(index)
        self.assertEqual(s["new"], 2)
        self.assertEqual(s["gone"], 1)
        self.assertEqual(set(s), STATES)


class CliTest(TempHome, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="sr-inv-cli-")
        self.ws = copy_fixture("workspace-min", Path(self._tmp.name))
        self.alex = self.ws / ".sessionretro" / "logs" / "alex"
        self.alex.mkdir(parents=True)
        shutil.copy(LOGS / "specstory-claude.md", self.alex / "a.md")

    def tearDown(self):
        self._tmp.cleanup()
        super().tearDown()

    def test_cli_inventory_json(self):
        res = run_sr("inventory", "--json", home=self.home, cwd=self.ws)
        self.assertEqual(res.returncode, 0, res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(len(data["logs"]), 1)
        self.assertEqual(data["logs"][0]["state"], "new")
        self.assertEqual(data["logs"][0]["person_id"], "alex")
        self.assertEqual(data["workspace"], "demo-team")
        self.assertTrue((self.ws / ".sessionretro" / "logs-index.json").is_file())
        self.assertTrue((self.ws / ".sessionretro" / "collect.lock").is_file())

    def test_cli_inventory_text(self):
        (self.alex / "empty.md").write_text("\n", encoding="utf-8")
        res = run_sr("inventory", home=self.home, cwd=self.ws)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("новых: 1", res.stdout)
        self.assertIn("empty.md", res.stdout)

    def test_cli_without_workspace(self):
        res = run_sr("inventory", home=self.home, cwd=Path(self._tmp.name))
        self.assertEqual(res.returncode, 2)
        self.assertIn("workspace", res.stderr)

    # ── B05: сбор держит collect.lock дольше 10 с ─────────────────────────────

    def test_cli_inventory_waits_for_running_collect(self):
        target = self.ws / ".sessionretro" / "collect"
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        child = subprocess.Popen([sys.executable, "-c", HOLDER, str(SCRIPTS), str(target), "2.5"],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        try:
            self.assertEqual(child.stdout.readline().strip(), "locked")
            start = time.monotonic()
            res = run_sr("inventory", "--json", home=self.home, cwd=self.ws)
            waited = time.monotonic() - start
        finally:
            child.wait(timeout=10)
            child.stdout.close()
            child.stderr.close()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertGreaterEqual(waited, 1.5)
        lines = res.stderr.splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("сбор логов уже идёт (collect.lock), жду до 300 с", lines[0])
        self.assertEqual(len(json.loads(res.stdout)["logs"]), 1)

    def test_inventory_lock_timeout_is_code1_with_path(self):
        args = argparse.Namespace(workspace=str(self.ws), json=True)
        target = self.ws / ".sessionretro" / "collect"
        with lock.file_lock(target), \
                unittest.mock.patch.object(lock, "COLLECT_WAIT_SECONDS", 1), \
                contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SrError) as ctx:
                cmd_inventory.handle(args)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn(str(self.ws / ".sessionretro" / "collect.lock"), ctx.exception.message)
        self.assertIn("1 с", ctx.exception.message)
        self.assertFalse((self.ws / ".sessionretro" / "logs-index.json").exists())


class SampleFixtureTest(unittest.TestCase):
    def test_sample_fixture_valid(self):
        data = json.loads(SAMPLE.read_text(encoding="utf-8"))
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["workspace"], "demo-team")
        self.assertIsInstance(data["warnings"], list)
        logs = data["logs"]
        self.assertEqual({r["state"] for r in logs}, STATES)
        self.assertEqual({r["person_id"] for r in logs}, {"alex", "sam"})
        for person in ("alex", "sam"):
            self.assertEqual({r["state"] for r in logs if r["person_id"] == person}, STATES)
        keys = [(r["person_id"], r["key"]) for r in logs]
        self.assertEqual(len(keys), len(set(keys)))
        text = SAMPLE.read_text(encoding="utf-8")
        self.assertNotIn("/" + "Users/", text)
        for r in logs:
            self.assertFalse(r.get("reviewed") and r.get("excluded"), r["key"])
            self.assertTrue(r["path"].startswith(f"logs/{r['person_id']}/"), r["path"])
            self.assertTrue(r["source_path"].startswith("/home/user/"), r["source_path"])
            for field in ("person_id", "source", "session_id", "key", "path", "source_path",
                          "format", "sha", "bytes", "turns_total", "user_turns", "log_started_at",
                          "log_ended_at", "collector", "models", "period_guess",
                          "period_guess_source", "metrics", "sanitize", "state", "reviewed"):
                self.assertIn(field, r, f"{r['key']}: нет {field}")
            if r["state"] == "grown":
                self.assertEqual(r["review_from_turn"], r["reviewed_turns_total"])
                self.assertGreater(r["turns_total"], r["reviewed_turns_total"])
            else:
                self.assertNotIn("review_from_turn", r)
            if r["state"] == "duplicate":
                self.assertIn(r["duplicate_of"], {x["path"] for x in logs})
                self.assertIn("#dup:", r["key"])
            if r["state"] == "excluded":
                self.assertTrue(r["excluded"])
                self.assertTrue(r["excluded_reason"])

    def test_sample_is_merge_stable(self):
        # индекс, пересчитанный по тем же записям (кроме gone), даёт те же состояния
        data = json.loads(SAMPLE.read_text(encoding="utf-8"))
        merge_fields = {"state", "reviewed", "reviewed_in_run", "reviewed_at", "reviewed_turns_total",
                        "reviewed_bytes", "review_from_turn", *EXCLUDED_FIELDS}
        fresh = [{k: v for k, v in r.items() if k not in merge_fields}
                 for r in data["logs"] if r["state"] != "gone"]
        out = inventory.merge(data, fresh, data["workspace"], NOW)
        before = {(r["person_id"], r["key"]): r["state"] for r in data["logs"]}
        after = {(r["person_id"], r["key"]): r["state"] for r in out["logs"]}
        self.assertEqual(before, after)
        self.assertEqual(out["warnings"], [])


if __name__ == "__main__":
    unittest.main()
