"""lib.workspace: поиск workspace, пути состояния, атомарная запись JSON."""

from __future__ import annotations

import json
import os
import stat
import unittest
from pathlib import Path

from _util import TempHome

from lib import workspace
from lib.workspace import STATE_DIR, SrError, find_workspace, read_json, write_json_atomic, ws_paths


def make_ws(root: Path) -> Path:
    state = root / STATE_DIR
    state.mkdir(parents=True)
    (state / "config.json").write_text("{}\n", encoding="utf-8")
    return root


class SrErrorTest(unittest.TestCase):
    def test_fields_and_default_code(self):
        err = SrError("что-то упало")
        self.assertEqual(err.message, "что-то упало")
        self.assertEqual(err.code, 1)
        self.assertEqual(str(err), "что-то упало")
        self.assertEqual(SrError("x", 2).code, 2)


class FindWorkspaceTest(TempHome, unittest.TestCase):
    def test_finds_upward_from_nested_dir(self):
        root = make_ws(self.home / "proj")
        nested = root / "src" / "deep"
        nested.mkdir(parents=True)
        self.assertEqual(find_workspace(start=nested), root.resolve())

    def test_explicit_state_dir(self):
        root = make_ws(self.home / "proj")
        self.assertEqual(find_workspace(explicit=str(root / STATE_DIR)), root.resolve())

    def test_explicit_root(self):
        root = make_ws(self.home / "proj")
        self.assertEqual(find_workspace(explicit=str(root)), root.resolve())

    def test_explicit_without_config_code2(self):
        empty = self.home / "empty"
        empty.mkdir()
        with self.assertRaises(SrError) as ctx:
            find_workspace(explicit=str(empty))
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn(str(empty), ctx.exception.message)

    def test_not_found_code2_with_start_path(self):
        start = self.home / "nowhere" / "sub"
        start.mkdir(parents=True)
        with self.assertRaises(SrError) as ctx:
            find_workspace(start=start)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn(str(start), ctx.exception.message)

    def test_global_dir_is_not_workspace(self):
        glob = self.home / STATE_DIR
        glob.mkdir()
        (glob / "config.json").write_text("{}\n", encoding="utf-8")
        start = self.home / "proj" / "sub"
        start.mkdir(parents=True)
        with self.assertRaises(SrError) as ctx:
            find_workspace(start=start)
        self.assertEqual(ctx.exception.code, 2)

    def test_explicit_global_dir_code2(self):
        glob = self.home / STATE_DIR
        glob.mkdir()
        (glob / "config.json").write_text("{}\n", encoding="utf-8")
        with self.assertRaises(SrError) as ctx:
            find_workspace(explicit=str(self.home))
        self.assertEqual(ctx.exception.code, 2)


class WsPathsTest(unittest.TestCase):
    def test_keys_and_locations(self):
        root = Path("/home/user/proj")
        paths = ws_paths(root)
        self.assertEqual(
            set(paths),
            {"state_dir", "config", "people", "logs", "logs_index", "runs", "pending",
             "reviews", "reports", "templates", "improvements", "state"},
        )
        state = root / STATE_DIR
        self.assertEqual(paths["state_dir"], state)
        self.assertEqual(paths["config"], state / "config.json")
        self.assertEqual(paths["people"], state / "people")
        self.assertEqual(paths["logs"], state / "logs")
        self.assertEqual(paths["logs_index"], state / "logs-index.json")
        self.assertEqual(paths["runs"], state / "runs")
        self.assertEqual(paths["pending"], state / "runs" / "pending")
        self.assertEqual(paths["reviews"], state / "reviews")
        self.assertEqual(paths["reports"], state / "reports")
        self.assertEqual(paths["templates"], state / "templates")
        self.assertEqual(paths["improvements"], state / "improvements")
        self.assertEqual(paths["state"], state / "plugin-state.json")


class JsonIoTest(TempHome, unittest.TestCase):
    def test_atomic_write_leaves_valid_json_and_no_tmp(self):
        target = self.home / "d" / "data.json"
        data = {"имя": "Алекс", "n": [1, 2]}
        write_json_atomic(target, data)
        text = target.read_text(encoding="utf-8")
        self.assertTrue(text.endswith("\n"))
        self.assertIn("Алекс", text)  # ensure_ascii=False
        self.assertEqual(json.loads(text), data)
        self.assertEqual(read_json(target), data)
        self.assertEqual([p.name for p in target.parent.iterdir()], ["data.json"])

    def test_atomic_write_overwrites(self):
        target = self.home / "data.json"
        write_json_atomic(target, {"a": 1})
        write_json_atomic(target, {"a": 2})
        self.assertEqual(read_json(target), {"a": 2})

    def test_atomic_write_new_file_mode_follows_umask(self):
        target = self.home / "new.json"
        old = os.umask(0o022)
        try:
            write_json_atomic(target, {"a": 1})
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)

    def test_atomic_write_keeps_existing_mode(self):
        target = self.home / "kept.json"
        target.write_text("{}\n", encoding="utf-8")
        os.chmod(target, 0o640)
        write_json_atomic(target, {"a": 1})
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)

    def test_broken_json_code1(self):
        target = self.home / "bad.json"
        target.write_text("{нет", encoding="utf-8")
        with self.assertRaises(SrError) as ctx:
            read_json(target)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("битый JSON", ctx.exception.message)
        self.assertIn(str(target), ctx.exception.message)

    def test_module_constant(self):
        self.assertEqual(workspace.STATE_DIR, ".sessionretro")


class PortablePathTest(TempHome, unittest.TestCase):
    """Пути без логина: внутри проекта — относительные, внутри HOME — `~/…`, прочие как есть."""

    def setUp(self):
        super().setUp()
        self.root = self.home / "work" / "proj"

    def test_portable_path(self):
        pp = workspace.portable_path
        self.assertEqual(pp(self.root / "ai-logs" / "a.md", self.root), "ai-logs/a.md")
        self.assertEqual(pp(self.root, self.root), ".")
        self.assertEqual(pp(self.home / ".claude" / "x.jsonl", self.root), "~/.claude/x.jsonl")
        self.assertEqual(pp(self.home, None), "~")
        self.assertEqual(pp(self.home.resolve() / "y", None), "~/y")
        outside = str(self.home) + "-other/z"            # префикс по строке, но не по пути
        self.assertEqual(pp(outside, self.root), outside)
        self.assertEqual(pp("/srv/logs/a.md", self.root), "/srv/logs/a.md")

    def test_portable_munged_home(self):
        # Папка сессий Claude Code: `~/.claude/projects/<cwd с '-' вместо не-букв>` несёт логин.
        import re
        munged = re.sub(r"[^A-Za-z0-9]", "-", str(self.home))
        path = self.home / ".claude" / "projects" / (munged + "-work-proj") / "s.jsonl"
        out = workspace.portable_path(path, self.root)
        self.assertEqual(out, "~/.claude/projects/-~-work-proj/s.jsonl")
        self.assertNotIn(self.home.name, out)
        self.assertEqual(workspace.portable_text(f"нет папки {path.parent}", None),
                         "нет папки ~/.claude/projects/-~-work-proj")

    def test_portable_text(self):
        pt = workspace.portable_text
        text = (f"папки нет ({self.home}/.claude/projects/-x), исходник {self.root}/a.md, "
                f"корень ({self.root}); чужое {self.home}-other/z")
        self.assertEqual(pt(text, self.root),
                         f"папки нет (~/.claude/projects/-x), исходник a.md, корень (.); "
                         f"чужое {self.home}-other/z")
        self.assertEqual(pt(f"{self.home}", None), "~")

    def test_write_text_atomic_keeps_newlines(self):
        target = self.home / "t" / "a.txt"
        workspace.write_text_atomic(target, "a\r\nb\n")
        self.assertEqual(target.read_bytes(), b"a\r\nb\n")


if __name__ == "__main__":
    unittest.main()
