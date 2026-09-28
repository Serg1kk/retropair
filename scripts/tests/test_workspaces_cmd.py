"""Команда `workspaces`: список и удаление записей глобального реестра (m13)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from _util import TempHome, run_sr

from lib import registry


class WorkspacesCmdTest(TempHome, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="sr-ws-")
        self.base = Path(self._tmp.name).resolve()
        # живой проект: есть .sessionretro/config.json
        self.alive = self.base / "alive"
        (self.alive / ".sessionretro").mkdir(parents=True)
        (self.alive / ".sessionretro" / "config.json").write_text("{}", encoding="utf-8")
        (self.alive / "main.py").write_text("print(1)\n", encoding="utf-8")
        # песочница, которую удалили с диска
        self.gone = self.base / "gone"

    def tearDown(self) -> None:
        self._tmp.cleanup()
        super().tearDown()

    def sr(self, *args: str):
        return run_sr(*args, home=self.home, cwd=self.base)

    def two_entries(self) -> tuple[dict, dict]:
        alive = registry.register(self.alive, "Alive App", "self", ["me"])
        gone = registry.register(self.gone, "Sandbox", "team", ["team-a"])
        return alive, gone

    def ids(self) -> list[str]:
        return [e["id"] for e in registry.load()]

    # list

    def test_list_empty_registry(self) -> None:
        res = self.sr("workspaces", "list")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("реестр пуст", res.stdout)

    def test_list_two_entries_marks_missing_folder(self) -> None:
        alive, gone = self.two_entries()
        res = self.sr("workspaces", "list")
        self.assertEqual(res.returncode, 0, res.stderr)
        lines = res.stdout.strip().splitlines()
        alive_line = next(line for line in lines if line.startswith(alive["id"]))
        gone_line = next(line for line in lines if line.startswith(gone["id"]))
        self.assertIn(str(self.alive), alive_line)
        self.assertIn("Alive App", alive_line)
        self.assertIn("self", alive_line)
        self.assertNotIn("папки нет", alive_line)
        self.assertIn(str(self.gone), gone_line)
        self.assertIn("team", gone_line)
        self.assertIn("папки нет", gone_line)

    def test_list_json(self) -> None:
        alive, gone = self.two_entries()
        res = self.sr("workspaces", "list", "--json")
        self.assertEqual(res.returncode, 0, res.stderr)
        data = json.loads(res.stdout)
        by_id = {e["id"]: e for e in data}
        self.assertEqual(set(by_id), {alive["id"], gone["id"]})
        self.assertEqual(by_id[alive["id"]]["path"], str(self.alive))
        self.assertEqual(by_id[alive["id"]]["title"], "Alive App")
        self.assertEqual(by_id[alive["id"]]["mode"], "self")
        self.assertFalse(by_id[alive["id"]]["missing"])
        self.assertTrue(by_id[gone["id"]]["missing"])

    def test_list_json_empty(self) -> None:
        res = self.sr("workspaces", "list", "--json")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(json.loads(res.stdout), [])

    # remove

    def test_remove_existing_keeps_project_files(self) -> None:
        alive, gone = self.two_entries()
        res = self.sr("workspaces", "remove", alive["id"])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.ids(), [gone["id"]])
        self.assertIn(alive["id"], res.stdout)
        self.assertIn("report --index", res.stdout)
        self.assertTrue((self.alive / ".sessionretro" / "config.json").is_file())
        self.assertTrue((self.alive / "main.py").is_file())

    def test_remove_unknown_id(self) -> None:
        alive, gone = self.two_entries()
        res = self.sr("workspaces", "remove", "nope")
        self.assertEqual(res.returncode, 1)
        self.assertIn("nope", res.stderr)
        self.assertIn(alive["id"], res.stderr)
        self.assertIn(gone["id"], res.stderr)
        self.assertEqual(sorted(self.ids()), sorted([alive["id"], gone["id"]]))

    def test_remove_missing_only_entries_without_folder(self) -> None:
        alive, gone = self.two_entries()
        res = self.sr("workspaces", "remove", "--missing")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.ids(), [alive["id"]])
        self.assertIn(gone["id"], res.stdout)
        self.assertNotIn(alive["id"], res.stdout)

    def test_remove_missing_nothing_to_remove(self) -> None:
        registry.register(self.alive, "Alive App", "self", ["me"])
        res = self.sr("workspaces", "remove", "--missing")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("нечего убирать", res.stdout)
        self.assertEqual(len(self.ids()), 1)

    def test_remove_id_and_missing_is_usage_error(self) -> None:
        alive, _ = self.two_entries()
        res = self.sr("workspaces", "remove", alive["id"], "--missing")
        self.assertEqual(res.returncode, 2)
        self.assertEqual(len(self.ids()), 2)

    def test_remove_without_arguments_is_usage_error(self) -> None:
        self.two_entries()
        res = self.sr("workspaces", "remove")
        self.assertEqual(res.returncode, 2)
        self.assertEqual(len(self.ids()), 2)

    def test_workspaces_without_action_is_usage_error(self) -> None:
        res = self.sr("workspaces")
        self.assertEqual(res.returncode, 2)

    # сценарий на реальном init

    def test_init_then_remove_drops_id_from_registry(self) -> None:
        proj = self.base / "sandbox"
        proj.mkdir()
        res = run_sr("init", "--mode", "self", "--title", "Sandbox Demo", home=self.home, cwd=proj)
        self.assertEqual(res.returncode, 0, res.stderr)
        ws_id = registry.find_by_path(proj)["id"]
        self.assertIn(ws_id, self.ids())
        res = self.sr("workspaces", "remove", ws_id)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn(ws_id, self.ids())
        self.assertIsNone(registry.find_by_path(proj))
        self.assertTrue((proj / ".sessionretro" / "config.json").is_file())


if __name__ == "__main__":
    unittest.main()
