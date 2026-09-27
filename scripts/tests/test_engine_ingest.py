"""Стык движка оценки со сбором логов: общий замок индекса, пометки через `inventory.mark`,
движок на индексе, построенном настоящим `inventory`, замер человеческого слоя настоящей `normalize`."""

from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import _util  # noqa: F401 — кладёт scripts/ в sys.path
from _assess import make_ws, payload
from _util import FIXTURES, TempHome
from test_tracker import TrackerCase

from lib import batches, cmd_normalize, inventory, lock, marks, normalize, tracker
from lib.config import load_config
from lib.workspace import SrError, ws_paths

NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(days=1)
REBUILT_AT = "2026-09-21T09:00:00Z"
# логи фичи сбора: человек → файлы фикстуры `tests/fixtures/logs/`
LOGS = {
    "alex": ("specstory-claude.md", "md-headings.md", "claude-main.jsonl"),
    "sam": ("specstory-ru-roles.md",),
}


def collect_lock(root: Path):
    """Замок сбора логов ровно так, как его берут `collect` и `inventory`."""
    return lock.file_lock(ws_paths(root)["state_dir"] / "collect", timeout=5)


class SharedLockTest(TrackerCase):
    """Пока сбор держит свой замок, движок не пишет индекс (и наоборот)."""

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(marks, "INDEX_LOCK_TIMEOUT", 0.3)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_append_with_marks_waits_for_collect_lock_code1(self):
        before = self.paths["logs_index"].read_bytes()
        with collect_lock(self.root):
            message = self.rejected(payload(), period_label="p1")
        self.assertIn("collect.lock", message)
        self.assertFalse(tracker.tracker_path(self.root).exists(), "прогон записан мимо замка")
        self.assertEqual(self.paths["logs_index"].read_bytes(), before)

    def test_retract_waits_for_collect_lock_code1(self):
        self.append_p1()
        tr_before = tracker.tracker_path(self.root).read_bytes()
        index_before = self.paths["logs_index"].read_bytes()
        with collect_lock(self.root):
            with self.assertRaises(SrError) as ctx:
                tracker.retract(self.root, self.tracker_doc()["runs"][0]["run_id"], "ошибка", now=LATER)
        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(tracker.tracker_path(self.root).read_bytes(), tr_before)
        self.assertEqual(self.paths["logs_index"].read_bytes(), index_before)

    def test_collect_waits_for_engine_index_lock(self):
        with marks.index_lock(self.root):
            with self.assertRaises(lock.LockBusy):
                with lock.file_lock(self.paths["state_dir"] / "collect", timeout=0.3):
                    pass


class RealIndexCase(TempHome, unittest.TestCase):
    """Workspace, чей индекс построил `inventory.rebuild` по снапшотам из логов фичи сбора."""

    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory(prefix="sr-real-index-")
        self.addCleanup(tmp.cleanup)
        self.root = make_ws(Path(tmp.name))
        self.paths = ws_paths(self.root)
        self.paths["logs_index"].unlink()  # индекс фикстуры движка не нужен: строит настоящий инвентарь
        for person, names in LOGS.items():
            person_dir = self.paths["logs"] / person
            person_dir.mkdir(parents=True, exist_ok=True)
            for name in names:
                src = FIXTURES / "logs" / name
                doc = normalize.parse_file(src)
                key = inventory.session_key(doc, f"logs/{person}/{name}")
                shutil.copyfile(src, person_dir / inventory.snapshot_name(key, doc["format"]))
        self.cfg = load_config(self.root)
        with collect_lock(self.root):
            self.index = inventory.rebuild(self.root, self.cfg, now=REBUILT_AT)

    def alex(self) -> list[dict]:
        return [e for e in self.index["logs"] if e["person_id"] == "alex"]

    def judge(self) -> dict:
        """Выход судьи фикстуры с `session_scope` на настоящие записи `alex`: две оценены, одна мала."""
        first, second, third = sorted(self.alex(), key=lambda e: e["log_started_at"])
        obj = payload()
        obj["data"]["session_scope"]["sessions"] = [
            {"session_id": e["session_id"], "path": e["path"], "turns_total": e["turns_total"],
             "reviewed_from_turn": None, "state": e["state"], "status": status, "status_note": note}
            for e, status, note in ((first, "reviewed", None), (second, "reviewed", None),
                                    (third, "transcript_too_short", "мало реплик"))
        ]
        return obj

    def append(self) -> dict:
        path = self.paths["pending"] / "alex" / "judge.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.judge(), ensure_ascii=False), encoding="utf-8")
        return tracker.append(self.root, path, "alex", period_label="p1", now=NOW)

    def disk_index(self) -> dict:
        return inventory.load_index(self.root)


class RealIndexTest(RealIndexCase):
    def test_real_index_shape_matches_engine_fixture(self):
        fixture = json.loads((FIXTURES / "assess" / "logs-index.json").read_text(encoding="utf-8"))
        self.assertEqual(list(fixture), list(self.index))
        real_fields = set().union(*(e.keys() for e in self.index["logs"]))
        for entry in fixture["logs"]:
            extra = set(entry) - real_fields - set(inventory.EXCLUDED_FIELDS) - {"review_from_turn", "duplicate_of"}
            self.assertEqual(extra, set(), f"поля фикстуры движка, которых нет у сбора: {entry['key']}")
            self.assertEqual(set(entry["source"]), {"type", "id"})
            if entry["state"] != "grown":
                self.assertNotIn("review_from_turn", entry)
            if not entry.get("excluded"):
                self.assertFalse(set(entry) & set(inventory.EXCLUDED_FIELDS), entry["key"])
        self.assertEqual(marks.load_index(self.root), self.disk_index())

    def test_batches_on_real_index(self):
        result = batches.for_person(self.root, "alex", measure="index")
        taken = [s["key"] for b in result["batches"] for s in b["sessions"]]
        self.assertEqual(sorted(taken), sorted(e["key"] for e in self.alex()))
        self.assertEqual(result["short"], [])
        self.assertEqual(result["measure"], "file_bytes")

    def test_append_marks_via_inventory_and_retract_restores(self):
        before = copy.deepcopy(self.disk_index())
        result = self.append()
        self.assertEqual(result["logs_marked"], 3)
        self.assertEqual(result["logs_unmatched"], 0)
        self.assertEqual(result["coverage"], {"marked": 3, "expected": 3})
        run_id = result["run_id"]
        after = self.disk_index()  # schema_version на месте — индекс читается сбором
        marked = [e for e in after["logs"] if e["person_id"] == "alex"]
        reviewed = [e for e in marked if e.get("reviewed")]
        excluded = [e for e in marked if e.get("excluded")]
        self.assertEqual(len(reviewed), 2)
        self.assertEqual(len(excluded), 1)
        for entry in reviewed:
            original = next(e for e in before["logs"] if e["key"] == entry["key"])
            expect = inventory.mark(original, "reviewed", run_id, "2026-09-21T12:00:00Z")
            expect["state"] = "unchanged"
            self.assertEqual(entry, expect)
        x = excluded[0]
        original = next(e for e in before["logs"] if e["key"] == x["key"])
        expect = inventory.mark(original, "excluded", run_id, "2026-09-21T12:00:00Z",
                                reason="transcript_too_short", note="мало реплик")
        expect["state"] = "excluded"
        self.assertEqual(x, expect)

        # следующий сбор переносит пометки движка и считает состояния по ним
        with collect_lock(self.root):
            rebuilt = inventory.rebuild(self.root, self.cfg, now="2026-09-22T09:00:00Z")
        states = {e["key"]: e["state"] for e in rebuilt["logs"] if e["person_id"] == "alex"}
        self.assertEqual(sorted(states.values()), ["excluded", "unchanged", "unchanged"])
        self.assertEqual(rebuilt["warnings"], [])

        tracker.retract(self.root, run_id, "проверка отката", now=LATER)
        restored = self.disk_index()
        # откат возвращает записи ровно к виду до прогона, хотя между ними прошёл пересчёт индекса
        self.assertEqual(restored["logs"], before["logs"])
        with collect_lock(self.root):
            again = inventory.rebuild(self.root, self.cfg, now="2026-09-23T09:00:00Z")
        self.assertEqual({e["state"] for e in again["logs"]}, {"new"})

    def test_mark_logs_catch_up_on_real_index(self):
        saved = self.paths["logs_index"].read_bytes()
        self.paths["logs_index"].unlink()
        result = self.append()  # индекса нет: прогон пишется, пометок нет
        self.assertEqual(result["logs_unmatched"], 3)
        self.paths["logs_index"].write_bytes(saved)
        caught = tracker.mark_logs(self.root, result["run_id"], now=LATER)
        self.assertEqual(caught["marked"], 3)
        self.assertEqual(caught["coverage"], {"marked": 3, "expected": 3})
        forced = tracker.mark_logs(self.root, result["run_id"], force=True, now=LATER + timedelta(hours=1))
        self.assertEqual(forced["marked"], 3)
        index = self.disk_index()
        alex = [e for e in index["logs"] if e["person_id"] == "alex"]
        self.assertEqual(sorted(bool(e.get("reviewed")) for e in alex), [False, True, True])
        self.assertEqual([e["excluded_reason"] for e in alex if e.get("excluded")], ["transcript_too_short"])
        with collect_lock(self.root):
            rebuilt = inventory.rebuild(self.root, self.cfg, now="2026-09-23T09:00:00Z")
        self.assertEqual(rebuilt["warnings"], [])


class HumanMeasureTest(RealIndexCase):
    def test_normalize_has_flags_used_by_batches(self):
        import argparse

        parser = argparse.ArgumentParser()
        cmd_normalize.register(parser.add_subparsers())
        args = parser.parse_args(["normalize", "x.md", "--roles", "user", "--drop-synthetic",
                                  "--max-turn-chars", "4000"])
        self.assertEqual((args.roles, args.drop_synthetic, args.max_turn_chars), ("user", True, 4000))

    def test_human_measure_on_real_normalize(self):
        result = batches.for_person(self.root, "alex", measure="human")
        self.assertEqual(result["measure"], "human")
        sessions = [s for b in result["batches"] for s in b["sessions"]]
        self.assertEqual(len(sessions), len(self.alex()))
        by_key = {e["key"]: e for e in self.alex()}
        for session in sessions:
            self.assertGreater(session["bytes"], 0, session["key"])
        jsonl = next(s for s in sessions if by_key[s["key"]]["format"] == "claude-jsonl")
        # человеческий слой без трейсов инструментов и ответов агента легче файла
        self.assertLess(jsonl["bytes"], by_key[jsonl["key"]]["bytes"])


if __name__ == "__main__":
    unittest.main()
