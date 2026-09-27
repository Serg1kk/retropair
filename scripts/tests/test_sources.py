"""Источники логов (`lib/sources.py`) и SpecStory (`lib/specstory.py`): munge пути проекта,
владелец источника, разрешение файлов по типам, мусор и слаг-файлы, git на локальном bare-репо,
предупреждения вместо ошибок, specstory sync через подмену `shutil.which`/`subprocess.run`.

Все пути — во временных папках; HOME подменён (TempHome), сети нет.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _util  # noqa: F401 — кладёт scripts/ в sys.path
from _util import TempHome

from lib import sources, specstory
from lib.workspace import SrError

GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@example.com"]


def _cfg(mode: str = "self", srcs: list | None = None, providers: list | None = None) -> dict:
    return {
        "workspace": {"id": "ws", "title": "ws", "groups": ["default"]},
        "mode": mode,
        "sources": srcs or [],
        "hook": {"enabled": True, "providers": providers if providers is not None else ["claude"]},
    }


def _touch(path: Path, text: str = "x\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _git(*args: str, cwd: Path | None = None) -> None:
    res = subprocess.run(["git", *GIT_ID, *args], cwd=str(cwd) if cwd else None,
                         capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if res.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: код {res.returncode}, {res.stderr}")


class _Base(TempHome, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="sr-src-")
        self.tmp = Path(self._tmp.name).resolve()
        self.root = self.tmp / "proj"
        self.root.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()
        super().tearDown()

    def names(self, files) -> list[str]:
        return sorted(f.path.name for f in files)


class MungeTest(unittest.TestCase):
    def test_munge(self):
        self.assertEqual(sources.munge("/home/user/My Docs /My.proj_x"),
                         "-home-user-My-Docs--My-proj-x")

    def test_munge_cyrillic_one_to_one(self):
        self.assertEqual(sources.munge("/home/user/Проект"), "-home-user-------")

    def test_munge_accepts_path(self):
        self.assertEqual(sources.munge(Path("/a/b.c")), "-a-b-c")


class ClaudeProjectDirTest(_Base):
    def test_auto_uses_resolved_root(self):
        link = self.tmp / "link"
        link.symlink_to(self.root)
        got = sources.claude_project_dir(link, "auto")
        self.assertEqual(got, Path.home() / ".claude" / "projects" / sources.munge(self.root.resolve()))

    def test_absolute_project_is_munged(self):
        other = self.tmp / "other proj"
        got = sources.claude_project_dir(self.root, str(other))
        self.assertEqual(got, Path.home() / ".claude" / "projects" / sources.munge(other.resolve()))

    def test_absolute_projects_folder_taken_as_is(self):
        folder = Path.home() / ".claude" / "projects" / "-x-y"
        folder.mkdir(parents=True)
        self.assertEqual(sources.claude_project_dir(self.root, str(folder)), folder)

    def test_relative_project_error(self):
        with self.assertRaises(SrError) as cm:
            sources.claude_project_dir(self.root, "rel/path")
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("rel/path", cm.exception.message)


class OwnerTest(unittest.TestCase):
    def test_owner_single_card(self):
        self.assertEqual(sources.owner({"type": "dir", "path": "x"}, [{"id": "me"}], 0), "me")

    def test_owner_default_card_not_counted(self):
        people = [{"id": "_default"}, {"id": "me"}]
        self.assertEqual(sources.owner({"type": "dir", "path": "x"}, people, 1), "me")

    def test_owner_ambiguous(self):
        with self.assertRaises(SrError) as cm:
            sources.owner({"type": "dir", "path": "x"}, [{"id": "a"}, {"id": "b"}], 3)
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("3", cm.exception.message)
        self.assertIn("2", cm.exception.message)

    def test_owner_no_cards(self):
        with self.assertRaises(SrError) as cm:
            sources.owner({"type": "dir", "path": "x"}, [], 1)
        self.assertEqual(cm.exception.code, 1)

    def test_owner_explicit_person(self):
        people = [{"id": "a"}, {"id": "b"}]
        self.assertEqual(sources.owner({"type": "dir", "path": "x", "person": "b"}, people, 1), "b")

    def test_owner_unknown_person(self):
        with self.assertRaises(SrError) as cm:
            sources.owner({"type": "dir", "path": "x", "person": "zz"}, [{"id": "a"}], 2)
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("zz", cm.exception.message)


class ResolveTest(_Base):
    def test_specstory_recursive_md(self):
        hist = self.root / ".specstory" / "history"
        _touch(hist / "2026-09-01_10-00-00Z.md")
        _touch(hist / "sub" / "b.md")
        _touch(hist / "c.txt")
        cfg = _cfg(srcs=[{"type": "specstory", "path": ".specstory/history"}])
        files, warnings, junk = sources.resolve(self.root, cfg, [{"id": "me"}])
        self.assertEqual(self.names(files), ["2026-09-01_10-00-00Z.md", "b.md"])
        self.assertEqual(warnings, [])
        f = files[0]
        self.assertEqual((f.person_id, f.source_type, f.source_id), ("me", "specstory", "config#1"))
        self.assertTrue(f.path.is_absolute())

    def test_claude_auto(self):
        folder = Path.home() / ".claude" / "projects" / sources.munge(self.root.resolve())
        _touch(folder / "x.jsonl", "{}\n")
        _touch(folder / "x" / "subagents" / "agent-a1.jsonl", "{}\n")
        _touch(folder / "notes.md")
        cfg = _cfg(srcs=[{"type": "claude-jsonl", "project": "auto"}])
        files, warnings, _ = sources.resolve(self.root, cfg, [{"id": "me"}])
        self.assertEqual([f.path for f in files], [folder / "x.jsonl"])
        self.assertEqual(files[0].source_type, "claude-jsonl")
        self.assertEqual(warnings, [])

    def test_missing_dir_warns(self):
        cfg = _cfg(srcs=[
            {"type": "specstory", "path": ".specstory/history"},
            {"type": "claude-jsonl", "project": "auto"},
            {"type": "dir", "path": "nope"},
        ])
        files, warnings, _ = sources.resolve(self.root, cfg, [{"id": "me"}])
        self.assertEqual(files, [])
        self.assertEqual(len(warnings), 3)
        self.assertIn("источник 1", warnings[0])
        self.assertIn(".specstory/history", warnings[0])
        self.assertIn("источник 3", warnings[2])

    def test_unsupported_warns(self):
        cfg = _cfg(srcs=[{"type": "codex-jsonl"}, {"type": "cursor-jsonl"}])
        files, warnings, _ = sources.resolve(self.root, cfg, [{"id": "me"}])
        self.assertEqual(files, [])
        self.assertEqual(len(warnings), 2)
        self.assertIn("codex-jsonl", warnings[0])
        self.assertIn("не поддерживается в 0.1", warnings[0])

    def test_unknown_type_error(self):
        cfg = _cfg(srcs=[{"type": "ftp"}])
        with self.assertRaises(SrError) as cm:
            sources.resolve(self.root, cfg, [{"id": "me"}])
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("ftp", cm.exception.message)

    def test_missing_required_field_error(self):
        cfg = _cfg(srcs=[{"type": "dir"}])
        with self.assertRaises(SrError) as cm:
            sources.resolve(self.root, cfg, [{"id": "me"}])
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("path", cm.exception.message)

    def test_dir_filters(self):
        d = self.root / "logs"
        for name in ("a.md", "b.markdown", "c.txt", "d.jsonl", "sub/e.md"):
            _touch(d / name)
        _touch(d / "img.png")
        _touch(d / ".git" / "HEAD.md")
        _touch(d / "node_modules" / "pkg" / "README.md")
        cfg = _cfg(srcs=[{"type": "dir", "path": "logs"}])
        files, _, _ = sources.resolve(self.root, cfg, [{"id": "me"}])
        self.assertEqual(self.names(files), ["a.md", "b.markdown", "c.txt", "d.jsonl", "e.md"])

    def test_dir_skips_state_and_subagents(self):
        _touch(self.root / "s.jsonl")
        _touch(self.root / "s" / "subagents" / "agent-1.jsonl")
        _touch(self.root / ".sessionretro" / "logs" / "me" / "k.md")
        cfg = _cfg(srcs=[{"type": "dir", "path": "."}])
        files, _, _ = sources.resolve(self.root, cfg, [{"id": "me"}])
        self.assertEqual(self.names(files), ["s.jsonl"])

    def test_absolute_dir_path(self):
        outside = self.tmp / "outside"
        _touch(outside / "a.md")
        cfg = _cfg(srcs=[{"type": "dir", "path": str(outside)}])
        files, _, _ = sources.resolve(self.root, cfg, [{"id": "me"}])
        self.assertEqual([f.path for f in files], [outside / "a.md"])

    def test_junk_counted(self):
        hist = self.root / ".specstory" / "history"
        _touch(hist / "a.md.orig")
        _touch(hist / "context-report.md")
        _touch(hist / "context_report-2.md")
        _touch(hist / "2026-09-01_10-00-00Z-fix-bug.md")
        _touch(hist / "plain.md")
        cfg = _cfg(srcs=[{"type": "specstory", "path": ".specstory/history"}])
        files, _, junk = sources.resolve(self.root, cfg, [{"id": "me"}])
        self.assertEqual(self.names(files), ["2026-09-01_10-00-00Z-fix-bug.md", "plain.md"])
        self.assertEqual(junk, {"orig": 1, "context_report": 2, "slug_named": 1})

    def test_card_sources(self):
        _touch(self.root / "mine" / "a.md")
        _touch(self.root / "theirs" / "b.md")
        people = [
            {"id": "a", "sources": [{"type": "dir", "path": "mine"}]},
            {"id": "b", "sources": [{"type": "dir", "path": "theirs"}]},
        ]
        files, _, _ = sources.resolve(self.root, _cfg(mode="team"), people)
        got = sorted((f.person_id, f.source_id, f.path.name) for f in files)
        self.assertEqual(got, [("a", "person:a#1", "a.md"), ("b", "person:b#1", "b.md")])

    def test_config_source_ambiguous_owner(self):
        _touch(self.root / "logs" / "a.md")
        cfg = _cfg(mode="team", srcs=[{"type": "dir", "path": "logs"}])
        with self.assertRaises(SrError) as cm:
            sources.resolve(self.root, cfg, [{"id": "a"}, {"id": "b"}])
        self.assertEqual(cm.exception.code, 1)

    def test_git_in_self_mode_error(self):
        cfg = _cfg(mode="self", srcs=[{"type": "git", "url": "/nowhere.git"}])
        with self.assertRaises(SrError) as cm:
            sources.resolve(self.root, cfg, [{"id": "me"}])
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("self", cm.exception.message)

    def test_git_local_bare(self):
        work = self.tmp / "work"
        _git("init", "-q", "-b", "main", str(work))
        _touch(work / "logs" / "s.md", "# s\n")
        _touch(work / "src" / "main.md", "# code\n")
        _git("-C", str(work), "add", ".")
        _git("-C", str(work), "commit", "-q", "-m", "one")
        bare = self.tmp / "bare.git"
        _git("clone", "-q", "--bare", str(work), str(bare))
        people = [{"id": "p1", "sources": [{"type": "git", "url": str(bare)}]}]

        files, warnings, _ = sources.resolve(self.root, _cfg(mode="team"), people)
        clones = Path.home() / ".sessionretro" / "clones"
        dirs = list(clones.iterdir())
        self.assertEqual(len(dirs), 1)
        self.assertRegex(dirs[0].name, r"^ws-p1-[0-9a-f]{8}$")
        self.assertEqual(self.names(files), ["s.md"])
        self.assertEqual(files[0].source_type, "git")
        self.assertEqual(files[0].source_id, "person:p1#1")
        self.assertEqual(warnings, [])

        _touch(work / "logs" / "t.md", "# t\n")
        _git("-C", str(work), "add", ".")
        _git("-C", str(work), "commit", "-q", "-m", "two")
        _git("-C", str(work), "push", "-q", str(bare), "main")
        files, _, _ = sources.resolve(self.root, _cfg(mode="team"), people)
        self.assertEqual(self.names(files), ["s.md", "t.md"])
        self.assertEqual(len(list(clones.iterdir())), 1)

    def test_git_clone_error(self):
        people = [{"id": "p1", "sources": [{"type": "git", "url": str(self.tmp / "missing.git")}]}]
        with self.assertRaises(SrError) as cm:
            sources.resolve(self.root, _cfg(mode="course"), people)
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("git clone", cm.exception.message)
        self.assertIn("missing.git", cm.exception.message)
        self.assertIn("код", cm.exception.message)

    def test_git_url_starting_with_dash_rejected(self):
        people = [{"id": "p1", "sources": [{"type": "git", "url": "--upload-pack=touch x"}]}]
        with mock.patch("lib.sources.subprocess.run") as run:
            with self.assertRaises(SrError) as cm:
                sources.resolve(self.root, _cfg(mode="team"), people)
        run.assert_not_called()
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("--upload-pack", cm.exception.message)

    def test_git_clone_separates_url_with_double_dash(self):
        people = [{"id": "p1", "sources": [{"type": "git", "url": "https://example.com/r.git",
                                            "branch": "main"}]}]
        with mock.patch("lib.sources.subprocess.run", return_value=_done()) as run:
            sources.resolve(self.root, _cfg(mode="team"), people)
        argv = run.call_args[0][0]
        self.assertEqual(argv[:2], ["git", "clone"])
        i = argv.index("--")
        self.assertEqual(argv[i + 1], "https://example.com/r.git")
        self.assertEqual(len(argv), i + 3)          # после url — только папка клона
        self.assertLess(argv.index("--branch"), i)

    def test_log_globs_ported(self):
        self.assertIn("**/.specstory/history/**/*", sources.LOG_GLOBS)
        self.assertIn("**/ide-session*", sources.LOG_GLOBS)


def _done(stdout: str = "", code: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=stdout, stderr=stderr)


class SpecstoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="sr-spec-")
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_min_version(self):
        self.assertEqual(specstory.MIN_VERSION, (2, 6, 0))

    def test_specstory_absent(self):
        with mock.patch("lib.specstory.shutil.which", return_value=None), \
                mock.patch("lib.specstory.subprocess.run") as run:
            self.assertIsNone(specstory.version())
            line = specstory.sync(self.root, "claude")
        self.assertIn("specstory не найден", line)
        self.assertIn("sync пропущен", line)
        run.assert_not_called()

    def test_version_with_banner(self):
        with mock.patch("lib.specstory.shutil.which", return_value="/bin/specstory"), \
                mock.patch("lib.specstory.subprocess.run",
                           return_value=_done("Welcome!\n\n2.6.1 (SpecStory)\n")):
            self.assertEqual(specstory.version(), (2, 6, 1))

    def test_specstory_old(self):
        calls = []

        def fake(cmd, **kw):
            calls.append(cmd)
            return _done("2.5.1\n")

        with mock.patch("lib.specstory.shutil.which", return_value="/bin/specstory"), \
                mock.patch("lib.specstory.subprocess.run", side_effect=fake):
            line = specstory.sync(self.root, "claude")
        self.assertIn("2.5.1", line)
        self.assertIn("2.6.0", line)
        self.assertEqual(len(calls), 1)  # sync не запускался

    def test_sync_ok_runs_in_root(self):
        calls = []

        def fake(cmd, **kw):
            calls.append((cmd, kw))
            return _done("2.6.0\n") if "--version" in cmd else _done("ok")

        with mock.patch("lib.specstory.shutil.which", return_value="/bin/specstory"), \
                mock.patch("lib.specstory.subprocess.run", side_effect=fake):
            line = specstory.sync(self.root, "codex")
        cmd, kw = calls[-1]
        self.assertEqual(cmd[1:], ["sync", "codex"])
        self.assertEqual(Path(kw["cwd"]), self.root)
        self.assertEqual(kw["timeout"], 120)
        self.assertIs(kw["stdin"], subprocess.DEVNULL)
        self.assertIn("codex", line)
        self.assertIn("ок", line)

    def test_sync_nonzero(self):
        def fake(cmd, **kw):
            return _done("2.6.0\n") if "--version" in cmd else _done(code=3, stderr="a\nboom tail")

        with mock.patch("lib.specstory.shutil.which", return_value="/bin/specstory"), \
                mock.patch("lib.specstory.subprocess.run", side_effect=fake):
            line = specstory.sync(self.root, "claude")
        self.assertIn("код 3", line)
        self.assertIn("boom tail", line)

    def test_sync_timeout(self):
        def fake(cmd, **kw):
            if "--version" in cmd:
                return _done("2.6.0\n")
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])

        with mock.patch("lib.specstory.shutil.which", return_value="/bin/specstory"), \
                mock.patch("lib.specstory.subprocess.run", side_effect=fake):
            line = specstory.sync(self.root, "claude")
        self.assertEqual(line, "specstory sync claude: нет ответа за 120 с")

    def test_version_unrecognized(self):
        with mock.patch("lib.specstory.shutil.which", return_value="/bin/specstory"), \
                mock.patch("lib.specstory.subprocess.run", return_value=_done("hello\n")):
            self.assertIsNone(specstory.version())
            line = specstory.sync(self.root, "claude")
        self.assertIn("версия не распознана", line)
        self.assertIn("sync пропущен", line)

    def test_providers_order(self):
        cfg = _cfg(providers=["codex", "cursor"])
        self.assertEqual(specstory.providers(["gemini"], cfg), ["gemini"])
        self.assertEqual(specstory.providers(None, cfg), ["codex", "cursor"])
        self.assertEqual(specstory.providers([], cfg), ["codex", "cursor"])
        self.assertEqual(specstory.providers(None, _cfg(providers=[])), ["claude"])
        self.assertEqual(specstory.providers(None, {}), ["claude"])
        self.assertEqual(specstory.providers(["a", "a", "b"], cfg), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
