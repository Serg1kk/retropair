"""lib.config: загрузка, дефолт из шаблона, сохранение с валидацией."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import _util
from _util import copy_fixture

from lib import config
from lib.schema import validate
from lib.workspace import SrError, ws_paths


class ConfigTest(_util.TempHome, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="sr-config-")
        self.root = copy_fixture("workspace-min", Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()
        super().tearDown()

    def cfg_path(self) -> Path:
        return ws_paths(self.root)["config"]

    def test_fixture_loads(self):
        cfg = config.load_config(self.root)
        self.assertEqual(cfg["mode"], "team")
        self.assertEqual(cfg["workspace"]["id"], "demo-team")
        self.assertEqual(cfg["workspace"]["groups"], ["team-a", "team-b"])

    def test_broken_mode_exit1_with_field_path(self):
        data = json.loads(self.cfg_path().read_text(encoding="utf-8"))
        data["mode"] = "solo"
        self.cfg_path().write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(SrError) as ctx:
            config.load_config(self.root)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("$.mode", ctx.exception.message)
        self.assertIn("config.json не проходит схему", ctx.exception.message)

    def test_broken_json_exit1(self):
        self.cfg_path().write_text("{", encoding="utf-8")
        with self.assertRaises(SrError) as ctx:
            config.load_config(self.root)
        self.assertEqual(ctx.exception.code, 1)

    def test_not_object_exit1(self):
        self.cfg_path().write_text("[]", encoding="utf-8")
        with self.assertRaises(SrError) as ctx:
            config.load_config(self.root)
        self.assertEqual(ctx.exception.code, 1)

    def test_missing_config_exit2(self):
        self.cfg_path().unlink()
        with self.assertRaises(SrError) as ctx:
            config.load_config(self.root)
        self.assertEqual(ctx.exception.code, 2)

    def test_default_config_valid(self):
        cfg = config.default_config(ws_id="my-proj", title="My proj", mode="self", lang="en")
        self.assertEqual(validate(cfg, "config"), [])
        self.assertEqual(cfg["workspace"]["id"], "my-proj")
        self.assertEqual(cfg["workspace"]["title"], "My proj")
        self.assertEqual(cfg["mode"], "self")
        self.assertEqual(cfg["lang"], "en")
        self.assertNotIn("{{", json.dumps(cfg))

    def test_default_config_keyword_only(self):
        with self.assertRaises(TypeError):
            config.default_config("x", "X", "self", "ru")  # type: ignore[misc]

    def test_save_config_roundtrip(self):
        cfg = config.load_config(self.root)
        cfg["workspace"]["groups"].append("team-c")
        config.save_config(self.root, cfg)
        self.assertEqual(config.load_config(self.root)["workspace"]["groups"],
                         ["team-a", "team-b", "team-c"])

    def test_save_config_invalid_not_written(self):
        before = self.cfg_path().read_text(encoding="utf-8")
        cfg = config.load_config(self.root)
        cfg["lang"] = "de"
        with self.assertRaises(SrError) as ctx:
            config.save_config(self.root, cfg)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("$.lang", ctx.exception.message)
        self.assertEqual(self.cfg_path().read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main()
