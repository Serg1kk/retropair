"""Авто-регистрация команд: discover, build_parser, dispatch."""

from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

import _util  # noqa: F401  (кладёт plugin/scripts в sys.path)

from lib import registry_cmds


def _write(folder: Path, name: str, body: str) -> Path:
    path = folder / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


REGISTER_DUP = """
def register(subparsers):
    p = subparsers.add_parser("dup")
    p.set_defaults(func=lambda args: 0)
"""


class DiscoverTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="sr-cmds-")
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_discover_finds_real_commands(self):
        stems = [registry_cmds.module_file(m) for m in registry_cmds.discover()]
        self.assertIn("cmd_init.py", stems)
        self.assertIn("cmd_doctor.py", stems)
        self.assertEqual(stems, sorted(stems))

    def test_discover_only_cmd_files_sorted(self):
        _write(self.dir, "cmd_b.py", "def register(subparsers):\n    pass\n")
        _write(self.dir, "cmd_a.py", "def register(subparsers):\n    pass\n")
        _write(self.dir, "helper.py", "X = 1\n")
        stems = [registry_cmds.module_file(m) for m in registry_cmds.discover(self.dir)]
        self.assertEqual(stems, ["cmd_a.py", "cmd_b.py"])

    def test_duplicate_command_name_names_both_files(self):
        _write(self.dir, "cmd_a.py", REGISTER_DUP)
        _write(self.dir, "cmd_b.py", REGISTER_DUP)
        modules = registry_cmds.discover(self.dir)
        with self.assertRaises(RuntimeError) as ctx:
            registry_cmds.build_parser(modules)
        text = str(ctx.exception)
        self.assertIn("'dup'", text)
        self.assertIn("cmd_a.py", text)
        self.assertIn("cmd_b.py", text)

    def test_duplicate_via_alias_detected(self):
        _write(self.dir, "cmd_a.py", REGISTER_DUP)
        _write(self.dir, "cmd_b.py", """
            def register(subparsers):
                subparsers.add_parser("other", aliases=["dup"])
            """)
        with self.assertRaises(RuntimeError) as ctx:
            registry_cmds.build_parser(registry_cmds.discover(self.dir))
        self.assertIn("cmd_b.py", str(ctx.exception))

    def test_module_without_register(self):
        _write(self.dir, "cmd_noreg.py", "X = 1\n")
        with self.assertRaises(RuntimeError) as ctx:
            registry_cmds.build_parser(registry_cmds.discover(self.dir))
        self.assertIn("cmd_noreg.py", str(ctx.exception))

    def test_handler_returning_none_raises(self):
        _write(self.dir, "cmd_none.py", """
            def register(subparsers):
                p = subparsers.add_parser("none")
                p.set_defaults(func=lambda args: None)
            """)
        parser = registry_cmds.build_parser(registry_cmds.discover(self.dir))
        args = parser.parse_args(["none"])
        with self.assertRaises(RuntimeError) as ctx:
            registry_cmds.dispatch(args)
        self.assertIn("none", str(ctx.exception))

    def test_handler_int_passes_through(self):
        _write(self.dir, "cmd_seven.py", """
            def register(subparsers):
                p = subparsers.add_parser("seven")
                p.set_defaults(func=lambda args: 7)
            """)
        parser = registry_cmds.build_parser(registry_cmds.discover(self.dir))
        self.assertEqual(registry_cmds.dispatch(parser.parse_args(["seven"])), 7)

    def test_common_flags(self):
        parser = registry_cmds.build_parser([])
        args = parser.parse_args(["--workspace", "x"])
        self.assertEqual(args.workspace, "x")
        self.assertIsNone(args.command)


if __name__ == "__main__":
    unittest.main()
