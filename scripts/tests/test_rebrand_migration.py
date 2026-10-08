"""Rebrand migration preserves historical bytes and refuses ambiguous state."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from _util import run_sr
from lib.workspace import SrError, find_workspace, ws_paths

class Migration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        source = Path(__file__).parent / "fixtures/workspace-min/.sessionretro"
        if not source.exists():
            source = source.with_name(".retropair")
        shutil.copytree(source, self.root / ".sessionretro")
        self.old = self.root / ".sessionretro"
        (self.old / "runs").mkdir(exist_ok=True)
        (self.old / "runs/history.json").write_text('{"run_id":"old-id", "approved":true}')
        (self.old / "logs").mkdir(exist_ok=True)
        (self.old / "logs/redacted.md").write_text("[REDACTED] synthetic log")
        self.before = self.digests(self.old)
    def tearDown(self):
        self.tmp.cleanup()
    def digests(self, path):
        return {str(p.relative_to(path)):hashlib.sha256(p.read_bytes()).hexdigest()
                for p in path.rglob("*") if p.is_file() and not p.name.endswith(".lock")}
    def cli(self, *args):
        return run_sr(*args, "--workspace", str(self.root), home=self.home, cwd=self.root)
    def test_preview_has_no_side_effects(self):
        result = self.cli("migrate", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "ready")
        self.assertEqual(self.digests(self.old), self.before)
        self.assertFalse((self.root / ".retropair").exists())
        self.assertEqual(list(self.home.iterdir()), [])
    def test_apply_preserves_bytes_and_is_idempotent(self):
        result = self.cli("migrate", "--apply", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.old.exists())
        self.assertEqual(self.digests(self.root / ".retropair"), self.before)
        result = self.cli("migrate", "--apply", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "already_current")
    def test_conflict_never_overwrites(self):
        (self.root / ".retropair").mkdir()
        result = self.cli("migrate", "--apply")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.digests(self.old), self.before)
        with self.assertRaises(SrError):
            ws_paths(self.root)
    def test_existing_commands_require_migration(self):
        result = self.cli("people", "list")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("migrate", result.stderr)
        self.assertFalse((self.root / ".retropair").exists())
    def test_init_cannot_shadow_old_workspace(self):
        result = self.cli("init", "--mode", "self", "--path", str(self.root))
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.root / ".retropair").exists())
    def test_global_preview_and_apply(self):
        old = self.home / ".sessionretro"
        old.mkdir()
        payload = '[{"id":"stable", "path":"/synthetic/project"}]'
        (old / "workspaces.json").write_text(payload)
        result = self.cli("migrate", "--global", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.home / ".retropair/workspaces.json").read_text(), payload)
        self.assertFalse(old.exists())
    def test_symlink_refused(self):
        outside = self.root / "external"
        self.old.rename(outside)
        self.old.symlink_to(outside, target_is_directory=True)
        result = self.cli("migrate", "--apply")
        self.assertEqual(result.returncode, 1)
        self.assertTrue(self.old.is_symlink())
    def test_active_collection_blocks_migration(self):
        from lib.lock import collect_lock
        with collect_lock(self.old, wait=0):
            result = self.cli("migrate", "--apply")
        self.assertEqual(result.returncode, 1)
        self.assertTrue(self.old.is_dir())
    def test_historical_absolute_log_path_resolves(self):
        from lib.collector_check import _log_path
        result = self.cli("migrate", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.root / ".retropair"
        path, error = _log_path(str(self.old / "logs/redacted.md"), state)
        self.assertIsNone(error)
        self.assertEqual(path, state / "logs/redacted.md")
        self.assertTrue(path.is_file())
    def test_new_entrypoint(self):
        import subprocess, sys
        p = Path(__file__).parents[1] / "rp.py"
        result = subprocess.run([sys.executable, str(p), "--version"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        from lib.version import __version__
        self.assertIn(f"retropair {__version__}", result.stdout)

class LegacyAgents(unittest.TestCase):
    def test_customized_old_agent_is_the_selected_target(self):
        from lib import agents
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / ".claude/agents/sessionretro-log-judge.md"
            old.parent.mkdir(parents=True)
            old.write_text("custom user instructions")
            self.assertEqual(agents.target(root, "judge"), old)
    def test_both_agent_names_fail_explicitly(self):
        from lib import agents
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / ".claude/agents"
            folder.mkdir(parents=True)
            for prefix in ("retropair", "sessionretro"):
                (folder / f"{prefix}-log-judge.md").write_text("custom")
            with self.assertRaises(SrError):
                agents.target(root, "judge")

class MigrationPrivacy(Migration):
    def test_migration_preserves_ignore_and_protects_new_state(self):
        import subprocess
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True)
        ignore = self.root / ".gitignore"
        ignore.write_text("# custom\nprivate.txt\n.sessionretro/logs/\n")
        result = self.cli("migrate", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("# custom\nprivate.txt\n.sessionretro/logs/\n", ignore.read_text())
        for name in (".retropair/logs/redacted.md", ".retropair/reports/index.html",
                     ".retropair/runs/pending/input.json", ".retropair-migration.json",
                     ".retropair-migration.lock"):
            check = subprocess.run(["git", "-C", str(self.root), "check-ignore", name], capture_output=True)
            self.assertEqual(check.returncode, 0, name)
    def test_local_validation_works_with_legacy_global_store(self):
        (self.home / ".sessionretro").mkdir()
        result = self.cli("migrate", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.cli("validate")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.home / ".retropair").exists())

class MigrationFailures(unittest.TestCase):
    setUp = Migration.setUp
    tearDown = Migration.tearDown
    cli = Migration.cli
    digests = Migration.digests
    def test_bad_primary_json_is_not_moved(self):
        (self.old / "config.json").write_text("bad JSON")
        result = self.cli("migrate", "--apply")
        self.assertEqual(result.returncode, 1)
        self.assertTrue(self.old.is_dir())
        self.assertFalse((self.root / ".retropair").exists())
    def test_missing_primary_file_is_not_moved(self):
        (self.old / "config.json").unlink()
        self.assertEqual(self.cli("migrate", "--apply").returncode, 1)
        self.assertTrue(self.old.is_dir())
    def test_nested_symlink_is_not_followed(self):
        (self.old / "outside").symlink_to(self.home, target_is_directory=True)
        self.assertEqual(self.cli("migrate", "--apply").returncode, 1)
        self.assertTrue(self.old.is_dir())
    def test_global_invalid_registry_is_not_moved(self):
        old = self.home / ".sessionretro"
        old.mkdir()
        (old / "workspaces.json").write_text("{}")
        self.assertEqual(self.cli("migrate", "--global", "--apply").returncode, 1)
        self.assertTrue(old.is_dir())
    def test_absent_store_is_noop(self):
        shutil.rmtree(self.old)
        result = self.cli("migrate", "--apply", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'absent')
        self.assertFalse((self.root / '.retropair-migration.json').exists())
    def test_verification_failure_rolls_back(self):
        from lib import cmd_migrate
        original = cmd_migrate._inventory
        def altered(path):
            value = original(path)
            return {} if Path(path).name == '.retropair' else value
        with patch.object(cmd_migrate, '_inventory', side_effect=altered):
            with self.assertRaises(SrError):
                cmd_migrate.migrate(self.root, apply=True)
        self.assertTrue(self.old.is_dir())
        self.assertFalse((self.root / '.retropair').exists())
        self.assertEqual(self.digests(self.old), self.before)
    def test_private_audit_mode_and_custom_ignore_mirror(self):
        (self.root / '.gitignore').write_text('.sessionretro/custom-private/\n')
        self.assertEqual(self.cli('migrate', '--apply').returncode, 0)
        audit = self.root / '.retropair-migration.json'
        self.assertEqual(audit.stat().st_mode & 0o777, 0o600)
        self.assertIn('.retropair/custom-private/', (self.root/'.gitignore').read_text())
    def test_hook_ignores_old_new_conflict(self):
        import importlib.util
        path = Path(__file__).parents[1]/'hook_session_end.py'
        spec = importlib.util.spec_from_file_location('rebrand_hook',path)
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        (self.root/'.retropair').mkdir()
        (self.root/'.retropair/config.json').write_text('{}')
        self.assertIsNone(hook._find_root(self.root))
        with patch.dict(os.environ, {'HOME':str(self.home)}):
            (self.home/'.sessionretro').mkdir()
            with self.assertRaises(hook.RegistryError):
                hook._registered(self.root)
