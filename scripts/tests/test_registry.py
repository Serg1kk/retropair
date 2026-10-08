"""lib.registry: глобальный реестр workspaces в ~/.retropair."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from _util import TempHome

from lib import registry
from lib.version import __version__
from lib.workspace import SrError


class RegistryTest(TempHome, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.proj = self.home / "proj"
        self.proj.mkdir()

    def test_paths_follow_home(self):
        self.assertEqual(registry.sr_home(), self.home / ".retropair")
        self.assertEqual(registry.workspaces_path(), self.home / ".retropair" / "workspaces.json")

    def test_load_empty_when_missing(self):
        self.assertEqual(registry.load(), [])

    def test_load_not_a_list_code1(self):
        path = registry.workspaces_path()
        path.parent.mkdir(parents=True)
        path.write_text('{"a": 1}\n', encoding="utf-8")
        with self.assertRaises(SrError) as ctx:
            registry.load()
        self.assertEqual(ctx.exception.code, 1)

    def test_register_creates_file_and_plugin_state(self):
        entry = registry.register(self.proj, "Команда А", "team", ["team-a", "team-b"])
        self.assertEqual(entry, {
            "id": "komanda-a",
            "path": str(self.proj.resolve()),
            "title": "Команда А",
            "mode": "team",
            "groups": ["team-a", "team-b"],
            "last_run_at": None,
            "last_collect_at": None,
        })
        on_disk = json.loads(registry.workspaces_path().read_text(encoding="utf-8"))
        self.assertEqual(on_disk, [entry])
        state = json.loads((registry.sr_home() / "plugin-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state, {
            "formatVersion": 1,
            "pluginVersion": __version__,
            "storeKind": "global",
            "migrations": [],
            "lastSync": None,
        })

    def test_register_keeps_existing_plugin_state(self):
        home = registry.sr_home()
        home.mkdir()
        (home / "plugin-state.json").write_text('{"formatVersion": 1, "custom": true}\n', encoding="utf-8")
        registry.register(self.proj, "P", "self", [])
        state = json.loads((home / "plugin-state.json").read_text(encoding="utf-8"))
        self.assertTrue(state["custom"])

    def test_register_same_path_idempotent(self):
        first = registry.register(self.proj, "Proj", "team", ["a"])
        registry.update(first["id"], last_run_at="2026-09-27T10:00:00Z")
        again = registry.register(self.proj, "Proj renamed", "course", ["b"])
        self.assertEqual(len(registry.load()), 1)
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(again["title"], "Proj renamed")
        self.assertEqual(again["mode"], "course")
        self.assertEqual(again["groups"], ["b"])
        self.assertEqual(again["last_run_at"], "2026-09-27T10:00:00Z")

    def test_id_collision_with_other_path(self):
        other = self.home / "other"
        other.mkdir()
        third = self.home / "third"
        third.mkdir()
        a = registry.register(self.proj, "Same", "team", [])
        b = registry.register(other, "Same", "team", [])
        c = registry.register(third, "Same", "team", [])
        self.assertEqual((a["id"], b["id"], c["id"]), ("same", "same-2", "same-3"))

    def test_update_known_field(self):
        entry = registry.register(self.proj, "P", "team", [])
        updated = registry.update(entry["id"], last_collect_at="2026-09-27T11:00:00Z", groups=["x"])
        self.assertEqual(updated["last_collect_at"], "2026-09-27T11:00:00Z")
        self.assertEqual(updated["groups"], ["x"])
        self.assertEqual(registry.load()[0], updated)

    def test_update_unknown_field_code1(self):
        entry = registry.register(self.proj, "P", "team", [])
        for bad in ({"colour": "red"}, {"id": "other"}):
            with self.assertRaises(SrError) as ctx:
                registry.update(entry["id"], **bad)
            self.assertEqual(ctx.exception.code, 1)

    def test_update_unknown_id_code1(self):
        with self.assertRaises(SrError) as ctx:
            registry.update("nope", title="x")
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("nope", ctx.exception.message)

    def test_remove(self):
        entry = registry.register(self.proj, "P", "team", [])
        registry.remove(entry["id"])
        self.assertEqual(registry.load(), [])
        with self.assertRaises(SrError) as ctx:
            registry.remove(entry["id"])
        self.assertEqual(ctx.exception.code, 1)

    def test_find_by_path(self):
        self.assertIsNone(registry.find_by_path(self.proj))
        entry = registry.register(self.proj, "P", "team", [])
        self.assertEqual(registry.find_by_path(self.proj / "."), entry)
        self.assertIsNone(registry.find_by_path(Path("/home/user/elsewhere")))

    def test_slugify(self):
        self.assertEqual(registry.slugify("Команда А"), "komanda-a")
        self.assertEqual(registry.slugify("Щука и ёж"), "schuka-i-ezh")
        self.assertEqual(registry.slugify("  My Team #1! "), "my-team-1")
        self.assertEqual(registry.slugify("---"), "workspace")
        self.assertEqual(registry.slugify(""), "workspace")


if __name__ == "__main__":
    unittest.main()
