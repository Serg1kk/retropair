"""Отметка `plugin-state.json`: сдвиг на новую версию плагина при любой команде `sr.py`."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _util
from _util import TempHome, copy_fixture, run_sr

from lib import plugin_state, registry, registry_cmds
from lib.version import __version__

ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _stamp(version, **extra) -> dict:
    return {"formatVersion": 1, "pluginVersion": version, "storeKind": "workspace",
            "migrations": [], "lastSync": None, **extra}


class SemverKeyTest(unittest.TestCase):
    def test_order(self):
        chain = ["0.1.0-dev", "0.1.0", "0.1.9", "0.1.10", "0.2.0", "1.0.0-alpha",
                 "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta", "1.0.0-beta.2",
                 "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0"]
        keys = [plugin_state._semver_key(v) for v in chain]
        self.assertNotIn(None, keys)
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len(set(keys)), len(keys))

    def test_build_metadata_ignored(self):
        self.assertEqual(plugin_state._semver_key("0.1.0+abc"), plugin_state._semver_key("0.1.0"))

    def test_unparsable(self):
        for bad in ("0.1", "v0.1.0", "0.1.0-", "0.1.0-a..b", "01.1.0", "", " 0.1.0", None, 1, [0, 1, 0]):
            with self.subTest(value=bad):
                self.assertIsNone(plugin_state._semver_key(bad))

    def test_current_version_parses(self):
        self.assertIsNotNone(plugin_state._semver_key(__version__))


class SyncStampTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="sr-pstate-")
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "plugin-state.json"

    def put(self, data) -> bytes:
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=2)
        self.path.write_text(text, encoding="utf-8")
        return self.path.read_bytes()

    def test_absent_creates_nothing(self):
        self.assertEqual(plugin_state.sync_stamp(self.path, "workspace"), "absent")
        self.assertFalse(self.path.exists())

    def test_old_version_synced_keeps_unknown_keys(self):
        self.put(_stamp("0.1.0-dev", custom={"a": 1}, migrations=["m1"]))
        self.assertEqual(plugin_state.sync_stamp(self.path, "workspace"), "synced")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["pluginVersion"], __version__)
        self.assertRegex(data["lastSync"], ISO_Z)
        self.assertEqual(data["custom"], {"a": 1})
        self.assertEqual(data["migrations"], ["m1"])
        self.assertEqual(data["formatVersion"], 1)
        self.assertEqual(data["storeKind"], "workspace")

    def test_equal_version_is_current_and_untouched(self):
        before = self.put(_stamp(__version__))
        self.assertEqual(plugin_state.sync_stamp(self.path, "workspace"), "current")
        self.assertEqual(self.path.read_bytes(), before)

    def test_newer_stamp_untouched(self):
        before = self.put(_stamp("99.0.0"))
        self.assertEqual(plugin_state.sync_stamp(self.path, "workspace"), "newer")
        self.assertEqual(self.path.read_bytes(), before)

    def test_corrupt_variants_untouched(self):
        cases = {
            "broken_json": "{broken",
            "not_object": [1, 2],
            "no_version": {"formatVersion": 1, "storeKind": "workspace"},
            "bad_version": _stamp("latest"),
            "number_version": _stamp(1),
            "other_store_kind": _stamp("0.1.0-dev", storeKind="global"),
        }
        for name, data in cases.items():
            with self.subTest(case=name):
                before = self.put(data)
                self.assertEqual(plugin_state.sync_stamp(self.path, "workspace"), "corrupt")
                self.assertEqual(self.path.read_bytes(), before)

    def test_missing_store_kind_is_not_corrupt(self):
        self.put({"formatVersion": 1, "pluginVersion": "0.1.0-dev"})
        self.assertEqual(plugin_state.sync_stamp(self.path, "global"), "synced")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertNotIn("storeKind", data)

    def test_unknown_store_kind_argument_raises(self):
        with self.assertRaises(ValueError):
            plugin_state.sync_stamp(self.path, "project")


class DispatchSyncTest(TempHome, unittest.TestCase):
    """`dispatch` синхронизирует отметки до handler и не роняет команду на сбоях."""

    def setUp(self) -> None:
        super().setUp()
        self.calls: list[str] = []
        self.gstate = registry.sr_home() / "plugin-state.json"
        self.gstate.parent.mkdir(parents=True)
        self.cwd = tempfile.TemporaryDirectory(prefix="sr-cwd-")
        self.addCleanup(self.cwd.cleanup)
        patcher = mock.patch.object(Path, "cwd", return_value=Path(self.cwd.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def dispatch(self) -> tuple[int, str]:
        def handler(args):
            self.calls.append(json.loads(self.gstate.read_text(encoding="utf-8"))["pluginVersion"])
            return 0
        args = argparse.Namespace(command="x", workspace=None, func=handler)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = registry_cmds.dispatch(args)
        return rc, err.getvalue()

    def test_synced_before_handler(self):
        self.gstate.write_text(json.dumps(_stamp("0.1.0-dev", storeKind="global")), encoding="utf-8")
        rc, err = self.dispatch()
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.calls, [__version__])

    def test_oserror_in_sync_warns_and_continues(self):
        self.gstate.write_text(json.dumps(_stamp("0.1.0-dev", storeKind="global")), encoding="utf-8")
        with mock.patch.object(plugin_state, "write_json_atomic", side_effect=PermissionError("нет прав")):
            rc, err = self.dispatch()
        self.assertEqual(rc, 0)
        self.assertEqual(self.calls, ["0.1.0-dev"])
        lines = err.splitlines()
        self.assertEqual(len(lines), 1, err)
        self.assertTrue(lines[0].startswith("retropair: предупреждение: plugin-state.json "), err)
        self.assertIn(str(self.gstate), lines[0])
        self.assertIn("нет прав", lines[0])
        self.assertTrue(lines[0].endswith(", не изменён"), err)


class CliSyncTest(TempHome, unittest.TestCase):
    """Реальный `sr.py` с временным HOME."""

    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="sr-pstate-cli-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.gstate = self.home / ".retropair" / "plugin-state.json"
        self.gstate.parent.mkdir(parents=True)

    def test_global_stamp_synced_by_command_without_workspace(self):
        self.gstate.write_text(json.dumps(_stamp("0.1.0-dev", storeKind="global")), encoding="utf-8")
        res = run_sr("workspaces", "list", home=self.home, cwd=self.tmp)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stderr, "")
        data = json.loads(self.gstate.read_text(encoding="utf-8"))
        self.assertEqual(data["pluginVersion"], __version__)
        self.assertRegex(data["lastSync"], ISO_Z)

    def test_workspace_stamp_synced_by_doctor(self):
        ws = copy_fixture("workspace-min", self.tmp)
        wstate = ws / ".retropair" / "plugin-state.json"
        self.assertEqual(json.loads(wstate.read_text(encoding="utf-8"))["pluginVersion"], "0.1.0-dev")
        res = run_sr("doctor", "--workspace", str(ws), home=self.home, cwd=self.tmp)
        self.assertNotIn("предупреждение: plugin-state.json", res.stderr)
        data = json.loads(wstate.read_text(encoding="utf-8"))
        self.assertEqual(data["pluginVersion"], __version__)
        self.assertRegex(data["lastSync"], ISO_Z)
        self.assertEqual(data["storeKind"], "workspace")

    def test_corrupt_global_stamp_warns_and_command_succeeds(self):
        self.gstate.write_text("{broken", encoding="utf-8")
        before = self.gstate.read_bytes()
        res = run_sr("workspaces", "list", home=self.home, cwd=self.tmp)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"retropair: предупреждение: plugin-state.json {self.gstate}: битый, не изменён",
                      res.stderr)
        self.assertEqual(self.gstate.read_bytes(), before)

    def test_newer_workspace_stamp_warns_with_version(self):
        ws = copy_fixture("workspace-min", self.tmp)
        wstate = ws / ".retropair" / "plugin-state.json"
        wstate.write_text(json.dumps(_stamp("99.0.0")), encoding="utf-8")
        before = wstate.read_bytes()
        res = run_sr("people", "list", "--workspace", str(ws), home=self.home, cwd=self.tmp)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"plugin-state.json {wstate.resolve()}: новее плагина (99.0.0), не изменён",
                      res.stderr)
        self.assertEqual(wstate.read_bytes(), before)

    def test_bad_explicit_workspace_skipped_silently_by_sync(self):
        res = run_sr("people", "list", "--workspace", str(self.tmp / "nope"), home=self.home, cwd=self.tmp)
        self.assertEqual(res.returncode, 2)
        self.assertNotIn("предупреждение", res.stderr)
        self.assertIn("workspace не найден", res.stderr)


if __name__ == "__main__":
    unittest.main()
