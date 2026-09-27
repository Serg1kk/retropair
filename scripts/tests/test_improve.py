"""Предложения движку: `lib.improve` (add, leaks, list_items, pack, gh_command, mark_sent, sync,
doctor_lines) и команда `improve add|list|pack|sent|sync`.

Workspace — копия фикстуры `workspace-min` во временной папке, HOME подменён (`TempHome` /
`run_sr(home=…)`). Пути и адреса, которые ищет проверка утечек, собраны склейкой: гейт чистоты
сканирует и этот файл.
"""

from __future__ import annotations

import argparse
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _util import TempHome, copy_fixture, run_sr

from lib import cmd_improve, improve
from lib.version import __version__
from lib.workspace import SrError, ws_paths

MAC_PATH = "/Us" + "ers/x"
HOME_PATH = "/home/" + "bob/"
MAIL = "a" + "@" + "b.io"
NAMES = ["Alex Demo", "Alex", "Demo"]
FIELDS = ("ID", "Компонент", "Симптом и контекст", "Локальный обход", "Предложение",
          "Версия плагина", "Дата", "Статус")


def add(root: Path, component: str = "скилл review", symptom: str = "пачка не собралась",
        **kw) -> Path:
    return improve.add(root, component, symptom, kw.pop("workaround", "собрал вручную"),
                       kw.pop("proposal", "причина — x; подход — y; критерий успеха — z"), **kw)


def fields(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^- \*\*(.+?):\*\* (.*)$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def write_pending(root: Path, text: str, ident: str = "imp-20260927-manual-abcd") -> Path:
    folder = ws_paths(root)["improvements"]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{ident}.md"
    path.write_text(
        f"# {ident}\n\n- **ID:** {ident}\n- **Компонент:** collect\n"
        f"- **Симптом и контекст:** {text}\n- **Локальный обход:** нет\n"
        f"- **Предложение:** нет\n- **Версия плагина:** 0.1.0\n- **Дата:** 2026-09-27\n"
        f"- **Статус:** pending\n", encoding="utf-8")
    return path


class Base(TempHome, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="sr-improve-")
        self.root = copy_fixture("workspace-min", Path(self._tmp.name))
        self.folder = ws_paths(self.root)["improvements"]

    def tearDown(self):
        self._tmp.cleanup()
        super().tearDown()

    def args(self, **kw) -> argparse.Namespace:
        return argparse.Namespace(workspace=str(self.root), **kw)


class Add(Base):
    def test_file_name_fields_status(self):
        path = add(self.root, today="2026-09-27")
        self.assertEqual(path.parent, self.folder)
        self.assertRegex(path.stem, "^" + improve.ID_RE + "$")
        self.assertTrue(path.stem.startswith("imp-20260927-"))
        got = fields(path)
        for name in FIELDS:
            self.assertIn(name, got)
        self.assertEqual(got["ID"], path.stem)
        self.assertEqual(got["Статус"], "pending")
        self.assertEqual(got["Версия плагина"], __version__)
        self.assertEqual(got["Дата"], "2026-09-27")
        self.assertEqual(path.read_text(encoding="utf-8").splitlines()[0], f"# {path.stem}")

    def test_slug_from_component_and_explicit(self):
        self.assertIn("-skill-review-", add(self.root, component="skill review").stem)
        self.assertIn("-batch-fail-", add(self.root, slug="batch-fail").stem)

    def test_bad_slug_is_call_error(self):
        with self.assertRaises(SrError) as err:
            add(self.root, slug="Bad Slug")
        self.assertEqual(err.exception.code, 2)
        self.assertIn("Bad Slug", err.exception.message)

    def test_empty_field_is_call_error(self):
        with self.assertRaises(SrError) as err:
            add(self.root, symptom="  ")
        self.assertEqual(err.exception.code, 2)

    def test_sanitized_on_write(self):
        path = add(self.root, symptom=f"Alex Demo написал {MAIL} из /home/user/x")
        text = path.read_text(encoding="utf-8")
        self.assertIn("[NAME]", text)
        self.assertIn("[EMAIL]", text)
        self.assertNotIn("Alex", text)
        self.assertNotIn(MAIL, text)
        self.assertNotIn("/home/user", text)
        self.assertEqual(improve.leaks(text, NAMES), [])

    def test_cli_add_rc0(self):
        res = run_sr("improve", "add", "--component", "collect", "--symptom", "s",
                     "--workaround", "w", "--proposal", "p", home=self.home, cwd=self.root)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(len(list(self.folder.glob("imp-*.md"))), 1)


LOGIN = "qw" + "login7"


class SlugPrivacy(Base):
    """I1: логин, имя и id карточки не уходят в ID через слаг."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(improve, "local_user_names", return_value=[LOGIN])
        patcher.start()
        self.addCleanup(patcher.stop)
        from lib.people import add_person
        add_person(self.root, "rbt", "Robert Demo", "default")

    def test_login_in_component_not_in_id(self):
        path = add(self.root, component=f"{LOGIN} collect")
        self.assertNotIn(LOGIN, path.name)
        self.assertNotIn(LOGIN, path.read_text(encoding="utf-8"))
        self.assertIn("-collect-", path.stem)
        improve.pack(self.root)

    def test_name_in_component_not_in_id(self):
        path = add(self.root, component="Alex flow")
        self.assertNotIn("alex", path.name.lower())
        self.assertIn("-flow-", path.stem)
        improve.pack(self.root)

    def test_card_id_in_component_not_in_id(self):
        path = add(self.root, component="rbt collect")
        self.assertNotIn("rbt", path.name)
        self.assertIn("-collect-", path.stem)
        self.assertEqual(improve.leaks(path.read_text(encoding="utf-8"),
                                       improve.private_words(self.root)), [])

    def test_explicit_slug_with_leak_code1(self):
        for slug in (f"{LOGIN}-collect", "alex-flow", "rbt-flow"):
            with self.subTest(slug=slug), self.assertRaises(SrError) as err:
                add(self.root, slug=slug)
            self.assertEqual(err.exception.code, 1)
        self.assertFalse(self.folder.exists() and list(self.folder.glob("imp-*.md")))

    def test_private_words(self):
        words = improve.private_words(self.root)
        self.assertIn(LOGIN, words)
        self.assertIn("rbt", words)
        self.assertIn("Alex Demo", words)
        self.assertNotIn("_default", words)
        self.assertNotIn("me", words)

    def test_pack_catches_login_and_card_id(self):
        for bad in (LOGIN, "rbt"):
            with self.subTest(bad=bad):
                path = write_pending(self.root, f"сломалось у {bad}")
                with self.assertRaises(SrError) as err:
                    improve.pack(self.root)
                self.assertEqual(err.exception.code, 1)
                self.assertIn(bad, err.exception.message)
                path.unlink()


class Leaks(unittest.TestCase):
    def test_four_findings(self):
        text = f"путь {MAC_PATH}, дом {HOME_PATH}, почта {MAIL}, автор Alex Demo"
        self.assertEqual(len(improve.leaks(text, NAMES)), 4)

    def test_clean_text(self):
        self.assertEqual(improve.leaks("~/x и [NAME], [EMAIL]", NAMES), [])


class ListItems(Base):
    def test_two_pending(self):
        a, b = add(self.root), add(self.root)
        items = improve.list_items(self.root)
        self.assertEqual(sorted(i["id"] for i in items), sorted([a.stem, b.stem]))
        self.assertEqual({i["status"] for i in items}, {"pending"})
        self.assertEqual({i["component"] for i in items}, {"скилл review"})

    def test_empty_without_folder(self):
        self.assertEqual(improve.list_items(self.root), [])

    def test_broken_file_is_error(self):
        self.folder.mkdir(parents=True)
        (self.folder / "imp-20260927-x-abcd.md").write_text("# пусто\n", encoding="utf-8")
        with self.assertRaises(SrError) as err:
            improve.list_items(self.root)
        self.assertEqual(err.exception.code, 1)
        self.assertIn("imp-20260927-x-abcd.md", err.exception.message)

    def test_cli_json(self):
        add(self.root)
        res = run_sr("improve", "list", "--json", home=self.home, cwd=self.root)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(json.loads(res.stdout)[0]["status"], "pending")


class Pack(Base):
    def test_pack_contains_both_and_prints_command(self):
        a, b = add(self.root), add(self.root)
        with mock.patch("subprocess.run") as run, mock.patch("subprocess.Popen") as popen, \
                mock.patch("sys.stdout", new_callable=lambda: __import__("io").StringIO()) as out:
            rc = cmd_improve.handle_pack(self.args(out=None))
        self.assertEqual(rc, 0)
        run.assert_not_called()
        popen.assert_not_called()
        packs = list(self.folder.glob("pack-*.md"))
        self.assertEqual(len(packs), 1)
        self.assertRegex(packs[0].name, r"^pack-\d{8}-[0-9a-f]{4}\.md$")
        text = packs[0].read_text(encoding="utf-8")
        self.assertIn(a.stem, text)
        self.assertIn(b.stem, text)
        self.assertEqual(improve.leaks(text, NAMES), [])
        stdout = out.getvalue()
        self.assertIn(text, stdout)
        gh = [line for line in stdout.splitlines() if line.startswith("gh issue create")]
        self.assertEqual(len(gh), 1)
        self.assertTrue(gh[0].startswith("gh issue create --repo Serg1kk/sessionretro"))
        self.assertIn(str(packs[0]), gh[0])
        self.assertIn("предложений 2", gh[0])

    def test_only_pending(self):
        a, b = add(self.root), add(self.root)
        improve.mark_sent(self.root, [a.stem])
        _, text = improve.pack(self.root)
        self.assertIn(b.stem, text)
        self.assertNotIn(a.stem, text)

    def test_out_path(self):
        add(self.root)
        out = Path(self._tmp.name) / "p.md"
        path, text = improve.pack(self.root, out=out)
        self.assertEqual(path, out)
        self.assertEqual(out.read_text(encoding="utf-8"), text)

    def test_leak_in_manual_file_blocks(self):
        for bad in (MAC_PATH, "Alex Demo"):
            with self.subTest(bad=bad):
                path = write_pending(self.root, f"сломалось у {bad}")
                with self.assertRaises(SrError) as err:
                    improve.pack(self.root)
                self.assertEqual(err.exception.code, 1)
                self.assertEqual(list(self.folder.glob("pack-*.md")), [])
                path.unlink()

    def test_bad_repository_no_orphan_file(self):
        add(self.root)
        with mock.patch.object(improve, "_manifest_repository", return_value="o/r"), \
                mock.patch("sys.stdout", new_callable=lambda: __import__("io").StringIO()):
            with self.assertRaises(SrError) as err:
                cmd_improve.handle_pack(self.args(out=None))
        self.assertEqual(err.exception.code, 1)
        self.assertEqual(list(self.folder.glob("pack-*.md")), [])

    def test_no_pending(self):
        with self.assertRaises(SrError) as err:
            improve.pack(self.root)
        self.assertEqual(err.exception.code, 1)
        self.assertIn("pending", err.exception.message)
        res = run_sr("improve", "pack", home=self.home, cwd=self.root)
        self.assertEqual(res.returncode, 1)


class GhCommand(unittest.TestCase):
    def test_repo_forms(self):
        for repo in ("https://github.com/o/r", "https://github.com/o/r.git"):
            with self.subTest(repo=repo):
                cmd = improve.gh_command(Path("/home/user/p.md"), 3, "0.1.1", repository=repo)
                self.assertEqual(cmd, "gh issue create --repo o/r --label proposal "
                                      "--title 'sessionretro 0.1.1: предложений 3' "
                                      "--body-file /home/user/p.md")

    def test_path_quoted_for_shell(self):
        import shlex
        path = Path('/home/user/a "b" $HOME/p.md')
        cmd = improve.gh_command(path, 1, "0.1.0", repository="https://github.com/o/r")
        self.assertEqual(shlex.split(cmd)[-1], str(path))

    def test_bad_repo(self):
        for repo in ("https://gitlab.com/o/r", "o/r", "", {"url": "https://github.com/o/r"}):
            with self.subTest(repo=repo), self.assertRaises(SrError) as err:
                improve.gh_command(Path("p.md"), 1, "0.1.0", repository=repo)
            self.assertEqual(err.exception.code, 1)

    def test_default_from_manifest(self):
        cmd = improve.gh_command(Path("p.md"), 1, "0.1.0")
        self.assertTrue(cmd.startswith("gh issue create --repo Serg1kk/sessionretro "))


class SentSync(Base):
    def changelog(self, body: str) -> Path:
        path = Path(self._tmp.name) / "CHANGELOG.md"
        path.write_text(body, encoding="utf-8")
        return path

    def test_sent(self):
        a = add(self.root)
        self.assertEqual(improve.mark_sent(self.root, [a.stem]), [a.stem])
        self.assertEqual(fields(a)["Статус"], "sent")

    def test_sent_unknown_id(self):
        with self.assertRaises(SrError) as err:
            improve.mark_sent(self.root, ["imp-20260927-none-abcd"])
        self.assertEqual(err.exception.code, 1)
        with self.assertRaises(SrError) as err:
            improve.mark_sent(self.root, ["not-an-id"])
        self.assertEqual(err.exception.code, 2)

    def test_sync_implemented_and_archived(self):
        a, b = add(self.root), add(self.root)
        improve.mark_sent(self.root, [a.stem])
        log = self.changelog(f"# Changelog\n\n## [Unreleased]\n\n- внедрено: {b.stem}\n\n"
                             f"## [0.1.1] — 2026-10-01\n\n- Внедрено : {a.stem}\n\n"
                             f"## [0.1.0]\n\n- упомянут {b.stem}\n")
        self.assertEqual(improve.sync(self.root, log), [a.stem])
        archived = self.folder / "archive" / a.name
        self.assertTrue(archived.is_file())
        self.assertFalse(a.exists())
        self.assertEqual(fields(archived)["Статус"], "implemented (0.1.1)")
        self.assertEqual([i["id"] for i in improve.list_items(self.root)], [b.stem])

    def test_sync_missing_changelog(self):
        with self.assertRaises(SrError) as err:
            improve.sync(self.root, Path(self._tmp.name) / "nope.md")
        self.assertEqual(err.exception.code, 2)

    def test_cli_sent_and_sync(self):
        a = add(self.root)
        res = run_sr("improve", "sent", a.stem, home=self.home, cwd=self.root)
        self.assertEqual(res.returncode, 0, res.stderr)
        log = self.changelog(f"## [0.2.0]\n\n- внедрено: {a.stem}\n")
        res = run_sr("improve", "sync", "--changelog", str(log), home=self.home, cwd=self.root)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(a.stem, res.stdout)
        self.assertTrue((self.folder / "archive" / a.name).is_file())


class Doctor(Base):
    def test_doctor_lines(self):
        self.assertEqual(improve.doctor_lines(None), [])
        self.assertEqual(cmd_improve.doctor_lines(None), [])
        self.assertEqual(improve.doctor_lines(self.root), [("improvements_pending", 0)])
        add(self.root)
        self.assertEqual(improve.doctor_lines(self.root), [("improvements_pending", 1)])

    def test_doctor_json(self):
        add(self.root)
        res = run_sr("doctor", "--json", home=self.home, cwd=self.root)
        data = json.loads(res.stdout)
        self.assertEqual(data["improvements_pending"], 1, res.stderr)


if __name__ == "__main__":
    unittest.main()
