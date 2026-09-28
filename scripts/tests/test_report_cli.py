"""Команда `sr.py report`: цели по режиму, флаги, коды выхода, гейт, `--anon`, index, doctor."""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import _util
import report_fixture
from _util import run_sr
from lib import registry, render
from lib import report_data as rd
from lib.workspace import ws_paths

DATA_RE = re.compile(r'<script type="application/json" id="data">(.*?)</script>', re.S)
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)
LINE_RE = re.compile(r"^(.+\.html) · \d+(?:[.,]\d)? (?:Б|КБ|МБ)$")


def _data(html_text: str) -> dict:
    found = DATA_RE.findall(html_text)
    if len(found) != 1:
        raise AssertionError(f"ожидался один блок данных, найдено {len(found)}")
    return json.loads(found[0])


def _data_without_i18n(html_text: str) -> str:
    data = _data(html_text)
    data.pop("i18n", None)
    return json.dumps(data, ensure_ascii=False)


def _leak_patterns(people: list[tuple[str, str]]) -> list[tuple[str, re.Pattern]]:
    """Регексы с границами слов: id целиком, слова имени по основе (без последней буквы)."""
    out = []
    for pid, name in people:
        out.append((pid, re.compile(r"(?<![A-Za-z0-9])" + re.escape(pid) + r"(?![A-Za-z0-9])",
                                    re.I)))
        for word in name.split():
            stem = word[:-1] if len(word) >= 4 else word
            stem = re.sub("[её]", "[её]", re.escape(stem))
            out.append((word, re.compile(r"(?<!\w)" + stem + r"[а-яё]{0,3}(?!\w)", re.I)))
    return out


class _Base(_util.TempHome, unittest.TestCase):
    MODE = "team"

    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="sr-report-cli-")
        self.base = Path(self._tmp.name).resolve()
        self.ws = report_fixture.make_workspace(self.base / "ws", people=3, periods=3,
                                                mode=self.MODE)
        self.reports = ws_paths(self.ws)["reports"]
        self.index = self.home / ".sessionretro" / "reports" / "index.html"

    def tearDown(self) -> None:
        self._tmp.cleanup()
        super().tearDown()

    def sr(self, *args: str, cwd: Path | None = None):
        return run_sr(*args, home=self.home, cwd=cwd or self.ws)

    def ok(self, *args: str):
        res = self.sr("report", *args)
        self.assertEqual(res.returncode, 0, res.stderr)
        return res


class TargetsTest(_Base):
    def test_default_team_builds_group_reviewer_personal_and_index(self):
        res = self.ok()
        expected = [self.reports / "group.html", self.reports / "reviewer.html",
                    *(self.reports / "personal" / f"{pid}.html" for pid in ("anna", "boris", "vera")),
                    self.index]
        for path in expected:
            with self.subTest(path=path.name):
                self.assertTrue(path.is_file(), path)
        self.assertFalse((self.reports / "personal" / "nora.html").exists())
        lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
        self.assertEqual(len(lines), len(expected), res.stdout)
        for line, path in zip(lines, expected):
            m = LINE_RE.match(line)
            self.assertIsNotNone(m, line)
            self.assertEqual(Path(m.group(1)), path)
        self.assertIn("людей без прогонов: 1", res.stderr)

    def test_retracted_run_review_is_silent_and_counted_for_reviewer(self):
        # в фикстуре у отозванного прогона anna есть файл ревью: отзыв штатный, шума нет
        res = self.ok()
        self.assertNotIn("отозван", res.stderr)
        self.assertNotIn("не привязано", res.stderr)
        meta = _data((self.reports / "reviewer.html").read_text(encoding="utf-8"))["meta"]
        self.assertEqual((meta["reviews_unbound"], meta["reviews_of_retracted"]), (0, 1))

    def test_modes_of_files(self):
        self.ok()
        for name, mode in (("group.html", "group"), ("reviewer.html", "reviewer"),
                           ("personal/anna.html", "personal")):
            with self.subTest(name=name):
                data = _data((self.reports / name).read_text(encoding="utf-8"))
                self.assertEqual(data["meta"]["mode"], mode)
                self.assertEqual(data["meta"]["workspace_title"], "Demo team")

    def test_personal_one_person_only(self):
        self.ok("--personal", "boris")
        self.assertTrue((self.reports / "personal" / "boris.html").is_file())
        for name in ("group.html", "reviewer.html", "personal/anna.html"):
            self.assertFalse((self.reports / name).exists(), name)
        self.assertFalse(self.index.exists())

    def test_index_only(self):
        res = self.ok("--index")
        self.assertTrue(self.index.is_file())
        self.assertFalse(self.reports.exists())
        self.assertEqual(len(res.stdout.splitlines()), 1)

    def test_personal_unknown_id_exit2(self):
        res = self.sr("report", "--personal", "nobody")
        self.assertEqual(res.returncode, 2)
        self.assertIn("nobody", res.stderr)
        self.assertFalse(self.reports.exists())

    def test_personal_card_without_runs_exit1(self):
        res = self.sr("report", "--personal", "nora")
        self.assertEqual(res.returncode, 1)
        self.assertIn("nora", res.stderr)
        self.assertFalse((self.reports / "personal" / "nora.html").exists())

    def test_personal_and_all_personal_exclusive_exit2(self):
        res = self.sr("report", "--personal", "anna", "--all-personal")
        self.assertEqual(res.returncode, 2)
        self.assertFalse(self.reports.exists())

    def test_out_dir(self):
        out = self.base / "out dir"
        self.ok("--out-dir", str(out), "--reviewer", "--index")
        self.assertTrue((out / "reviewer.html").is_file())
        self.assertFalse(self.reports.exists())
        self.assertTrue(self.index.is_file())

    def test_json_output(self):
        res = self.ok("--json")
        items = json.loads(res.stdout)
        kinds = [i["kind"] for i in items]
        self.assertEqual(kinds, ["group", "reviewer", "personal", "personal", "personal", "index"])
        self.assertEqual([i["person"] for i in items if i["kind"] == "personal"],
                         ["anna", "boris", "vera"])
        self.assertIsNone(items[0]["person"])
        for item in items:
            with self.subTest(path=item["path"]):
                self.assertEqual(Path(item["path"]).stat().st_size, item["bytes"])

    def test_lang_en(self):
        self.ok("--lang", "en", "--reviewer")
        text = (self.reports / "reviewer.html").read_text(encoding="utf-8")
        self.assertIn('<html lang="en"', text)
        self.assertEqual(_data(text)["meta"]["lang"], "en")
        self.assertIn("Reviewer report", TITLE_RE.search(text).group(1))

    def test_default_lang_from_config(self):
        self.ok("--reviewer")
        text = (self.reports / "reviewer.html").read_text(encoding="utf-8")
        self.assertIn('<html lang="ru"', text)

    def test_show_scores_flag(self):
        self.ok("--personal", "anna")
        plain = _data_without_i18n((self.reports / "personal" / "anna.html").read_text("utf-8"))
        self.assertNotIn('"score"', plain)
        self.assertNotIn('"level"', plain)
        self.ok("--personal", "anna", "--show-scores")
        data = json.loads(_data_without_i18n(
            (self.reports / "personal" / "anna.html").read_text("utf-8")))
        self.assertTrue(data["meta"]["show_scores"])
        self.assertIn("score", data["people"][0]["points"][0]["axes"]["verification"])
        self.assertIn("level", data["people"][0]["points"][0])


class ErrorsTest(_Base):
    def test_no_tracker_exit1(self):
        (ws_paths(self.ws)["runs"] / "process-reviews.json").unlink()
        res = self.sr("report")
        self.assertEqual(res.returncode, 1)
        self.assertIn("process-reviews.json", res.stderr)
        self.assertFalse(self.reports.exists())

    def test_empty_runs_exit1(self):
        path = ws_paths(self.ws)["runs"] / "process-reviews.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["runs"] = []
        path.write_text(json.dumps(doc), encoding="utf-8")
        res = self.sr("report")
        self.assertEqual(res.returncode, 1)
        self.assertIn("нет прогонов", res.stderr)

    def test_tracker_v1_exit1(self):
        path = ws_paths(self.ws)["runs"] / "process-reviews.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["tracker_version"] = 1
        path.write_text(json.dumps(doc), encoding="utf-8")
        res = self.sr("report")
        self.assertEqual(res.returncode, 1)
        self.assertIn("tracker_version", res.stderr)

    def test_no_workspace_exit2(self):
        empty = self.base / "empty"
        empty.mkdir()
        res = self.sr("report", cwd=empty)
        self.assertEqual(res.returncode, 2)


DRAFT_REC_TEXT = "Текст рекомендации из неодобренного прогона, адресат его не получал."


class UnapprovedRunsTest(_Base):
    """team с гейтом ревьюера: в личный файл идут только одобренные прогоны."""

    def _tracker_path(self) -> Path:
        return ws_paths(self.ws)["runs"] / "process-reviews.json"

    def _plant_draft(self) -> None:
        path = self._tracker_path()
        doc = json.loads(path.read_text(encoding="utf-8"))
        run = [r for r in doc["runs"] if r["person_id"] == "anna"
               and r["period"]["label"] == "P3" and not r.get("retracted")][0]
        self.assertEqual(run["review_status"], "draft")
        run["recommendations"][0]["text"] = DRAFT_REC_TEXT
        run["prev_recommendations"] = [{"id": "REC-anna-002", "verdict": "followed",
                                        "evidence": None, "note": ""}]
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    def _all_drafts(self, pid: str) -> None:
        path = self._tracker_path()
        doc = json.loads(path.read_text(encoding="utf-8"))
        for run in doc["runs"]:
            if run["person_id"] == pid:
                run["review_status"] = "draft"
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    def test_personal_html_has_no_draft_rec_or_draft_point(self):
        self._plant_draft()
        self.ok()
        personal_text = (self.reports / "personal" / "anna.html").read_text(encoding="utf-8")
        self.assertNotIn(DRAFT_REC_TEXT, personal_text)
        data = _data(personal_text)
        person = data["people"][0]
        self.assertEqual([pt["period"] for pt in person["points"]], ["P1", "P2"])
        self.assertEqual({r["given_in"]: r["status"] for r in person["recs"]},
                         {"P1": "followed", "P2": "open"})
        self.assertEqual(data["meta"]["date_to"], person["points"][-1]["date"])
        reviewer = _data((self.reports / "reviewer.html").read_text(encoding="utf-8"))
        anna = [p for p in reviewer["people"] if p["id"] == "anna"][0]
        flagged = [r for r in anna["recs"] if r["text"] == DRAFT_REC_TEXT]
        self.assertEqual(len(flagged), 1)
        self.assertTrue(flagged[0]["pending_approval"])
        group_text = (self.reports / "group.html").read_text(encoding="utf-8")
        self.assertNotIn(DRAFT_REC_TEXT, group_text)

    def test_personal_without_approved_runs_exit1(self):
        self._all_drafts("boris")
        res = self.sr("report", "--personal", "boris")
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("boris", res.stderr)
        self.assertIn("одобренн", res.stderr)
        self.assertFalse((self.reports / "personal" / "boris.html").exists())

    def test_all_personal_skips_person_without_approved_runs(self):
        self._all_drafts("boris")
        res = self.ok("--all-personal")
        self.assertIn("boris", res.stderr)
        self.assertFalse((self.reports / "personal" / "boris.html").exists())
        for pid in ("anna", "vera"):
            self.assertTrue((self.reports / "personal" / f"{pid}.html").is_file(), pid)


class GateTest(_Base):
    def _plant_other_name(self) -> Path:
        """Подкидывает имя Бориса в отправляемый блок одобренного ревью Анны."""
        for path in sorted((ws_paths(self.ws)["reviews"] / "anna").glob("*.md")):
            text = path.read_text(encoding="utf-8")
            if "status: approved" in text and "period: P2" in text:
                head = "### Ревью по сессиям, P2"
                self.assertIn(head, text)
                path.write_text(text.replace(head, head + "\n\nКак и Борису, стоит начать с тестов.",
                                             1), encoding="utf-8")
                return path
        raise AssertionError("нет одобренного ревью P2 у anna")

    def test_foreign_name_in_sent_block_blocks_file(self):
        self.ok()
        target = self.reports / "personal" / "anna.html"
        self.assertTrue(target.is_file())
        self._plant_other_name()
        res = self.sr("report")
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("чужие токены", res.stderr)
        self.assertIn("anna.html", res.stderr)
        self.assertIn("boris", res.stderr)
        self.assertFalse(target.exists(), "старый личный файл должен быть удалён")
        for name in ("group.html", "reviewer.html", "personal/boris.html", "personal/vera.html"):
            self.assertTrue((self.reports / name).is_file(), name)
        self.assertTrue(self.index.is_file())
        self.assertNotIn("anna.html", res.stdout)

    def test_sanitize_names_are_gated(self):
        cfg_path = ws_paths(self.ws)["config"]
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["sanitize"]["names"] = ["Леонтий Заречный"]
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        path = self._plant_other_name()
        path.write_text(path.read_text(encoding="utf-8").replace("Как и Борису", "Как и Леонтий"),
                        encoding="utf-8")
        res = self.sr("report", "--personal", "anna")
        self.assertEqual(res.returncode, 1)
        self.assertIn("sanitize.names", res.stderr)


class AnonTest(_Base):
    PEOPLE = report_fixture.NAMES[:3] + [report_fixture.EXTRA]

    def test_anon_has_no_ids_names_contacts_paths(self):
        res = self.ok("--anon")
        personal = sorted(p.name for p in (self.reports / "personal").iterdir())
        self.assertEqual(len(personal), 3)
        for name in personal:
            self.assertRegex(name, r"^p\d{2}\.html$")
        for line in res.stdout.splitlines():
            self.assertNotRegex(line, r"anna|boris|vera")
        pats = _leak_patterns(self.PEOPLE)
        files = [self.reports / "group.html", self.reports / "reviewer.html",
                 *sorted((self.reports / "personal").glob("*.html"))]
        for path in files:
            text = path.read_text(encoding="utf-8")
            data = _data_without_i18n(text)
            title = TITLE_RE.search(text).group(1)
            for label, pat in pats:
                with self.subTest(file=path.name, token=label):
                    self.assertIsNone(pat.search(data), f"{label}: {pat.search(data)}")
                    self.assertIsNone(pat.search(title), title)
            for leak in ("@fake-handle", "github.com/fake", "/home/user"):
                with self.subTest(file=path.name, leak=leak):
                    self.assertNotIn(leak, text)

    def test_anon_personal_by_real_id(self):
        res = self.ok("--anon", "--personal", "boris", "--json")
        items = json.loads(res.stdout)
        self.assertEqual(len(items), 1)
        path = Path(items[0]["path"])
        self.assertRegex(path.name, r"^p\d{2}\.html$")
        self.assertEqual(items[0]["person"], path.stem)
        self.assertTrue(path.is_file())
        self.assertFalse((self.reports / "personal" / "boris.html").exists())


class AnonNamesakesTest(_Base):
    def test_namesakes_anon_all_personal(self):
        from lib import anon
        from lib.people import list_people
        card = ws_paths(self.ws)["people"] / "vera.yaml"
        card.write_text(card.read_text(encoding="utf-8").replace("Вера Ольховская", "Анна Каменева"),
                        encoding="utf-8")
        res = self.sr("report", "--anon", "--all-personal", "--reviewer", "--json")
        self.assertEqual(res.returncode, 0, res.stderr)
        cards = list_people(self.ws)
        a = anon.Anonymizer(cards, [], "ru")
        items = json.loads(res.stdout)
        by_code = {a.id_map[pid]: pid for pid in a.id_map}
        personal = [i for i in items if i["kind"] == "personal"]
        self.assertEqual(len(personal), 3)
        for item in personal:
            text = Path(item["path"]).read_text(encoding="utf-8")
            data = _data(text)
            expected = a.name_map[by_code[item["person"]]]
            self.assertEqual(data["people"][0]["name"], expected)
            self.assertIn(expected, data["meta"]["title"])
            self.assertIn(expected, TITLE_RE.search(text).group(1))
        reviewer = _data((self.reports / "reviewer.html").read_text(encoding="utf-8"))
        self.assertEqual(sorted(p["name"] for p in reviewer["people"]),
                         sorted(a.name_map[pid] for pid in ("anna", "boris", "vera")))


def _rename_ids(ws: Path, renames: dict[str, str]) -> None:
    """Переименовывает id людей в синтетическом workspace: карточки, папки ревью, трекер, тексты."""
    state = ws / ".sessionretro"
    pats = [(re.compile(r"(?<![A-Za-z0-9])" + re.escape(old) + r"(?![A-Za-z0-9])"), new)
            for old, new in renames.items()]

    def sub(text: str) -> str:
        for pat, new in pats:
            text = pat.sub(new, text)
        return text

    for path in sorted(state.rglob("*"), key=lambda p: -len(p.parts)):
        if path.is_file() and path.suffix in (".json", ".yaml", ".md"):
            path.write_text(sub(path.read_text(encoding="utf-8")), encoding="utf-8")
        new_name = sub(path.name)
        if new_name != path.name:
            path.rename(path.with_name(new_name))


class AnonCodeLikeIdsTest(_Base):
    """id людей сами вида `p01…`: коды `--anon` берут другую букву, group и reviewer пишутся."""

    RENAMES = {"anna": "p01", "boris": "p02", "vera": "p03"}

    def test_ids_like_codes_anon_writes_all_files(self):
        from lib import anon
        from lib.people import list_people
        _rename_ids(self.ws, self.RENAMES)
        out = self.base / "anon-out"
        res = self.sr("report", "--group", "--reviewer", "--all-personal", "--anon",
                      "--out-dir", str(out))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("чужие токены", res.stderr)
        a = anon.Anonymizer(list_people(self.ws), [], "ru")
        self.assertNotEqual(a.code_prefix, "p")
        personal = sorted(p.name for p in (out / "personal").iterdir())
        self.assertEqual(personal, sorted(f"{a.id_map[pid]}.html" for pid in self.RENAMES.values()))
        people = [(new, dict(report_fixture.NAMES)[old]) for old, new in self.RENAMES.items()]
        people.append(report_fixture.EXTRA)
        pats = _leak_patterns(people)
        files = [out / "group.html", out / "reviewer.html", *sorted((out / "personal").glob("*.html"))]
        self.assertEqual(len(files), 5)
        for path in files:
            data = _data_without_i18n(path.read_text(encoding="utf-8"))
            for label, pat in pats:
                with self.subTest(file=path.name, token=label):
                    self.assertIsNone(pat.search(data), f"{label}: {pat.search(data)}")


class AnonDemoWorkspaceTest(_util.TempHome, unittest.TestCase):
    """Демо-workspace плагина (id `p01…p15`): `--anon` пишет group и reviewer."""

    def test_demo_anon_group_and_reviewer(self):
        demo = _util.SCRIPTS.parent / "demo" / "workspace"
        with tempfile.TemporaryDirectory(prefix="sr-demo-anon-") as tmp:
            ws = Path(tmp) / "ws"
            shutil.copytree(demo, ws)
            out = Path(tmp) / "out"
            res = run_sr("--workspace", str(ws), "report", "--group", "--reviewer", "--anon",
                         "--out-dir", str(out), home=self.home, cwd=ws)
            self.assertEqual(res.returncode, 0, res.stderr)
            for name in ("group.html", "reviewer.html"):
                data = _data_without_i18n((out / name).read_text(encoding="utf-8"))
                with self.subTest(file=name):
                    self.assertIsNone(re.search(r"(?<![A-Za-z0-9])p\d{2}(?![A-Za-z0-9])", data))


class GateHelpersTest(unittest.TestCase):
    ANNA = {"id": "anna", "name": "Анна Вершинина"}
    BORIS = {"id": "boris", "name": "Борис Каменев"}

    def cards(self) -> dict:
        return {"anna": self.ANNA, "boris": self.BORIS}

    def test_personal_gate_under_anon_includes_own_real_tokens(self):
        from lib import anon, cmd_report
        a = anon.Anonymizer(list(self.cards().values()))
        payload = {"meta": {"title": "Отчёт"}, "people": [{"text": "С Анной всё ровно"}]}
        hits = cmd_report._personal_hits(payload, "anna", self.cards(), a, [])
        self.assertTrue(any("anna" in h for h in hits), hits)
        self.assertEqual(cmd_report._personal_hits(payload, "anna", self.cards(), None, []), [])

    def test_roster_gate_for_shared_files(self):
        from lib import cmd_report
        leak = {"meta": {"title": "Отчёт"}, "people": [{"text": "как у Бориса"}]}
        clean = {"meta": {"title": "Отчёт"}, "people": [{"text": "как у p02"}]}
        self.assertTrue(cmd_report._roster_hits(leak, self.cards(), []))
        self.assertEqual(cmd_report._roster_hits(clean, self.cards(), []), [])

    def test_roster_gate_catches_code_like_real_ids(self):
        from lib import anon, cmd_report
        cards = {"p01": {"id": "p01", "name": "Анна Вершинина"},
                 "p02": {"id": "p02", "name": "Борис Каменев"}}
        a = anon.Anonymizer(list(cards.values()))
        payload = a.payload({"meta": {"title": "Отчёт"}, "people": [{"id": "p01", "name": "x"}]})
        self.assertEqual(cmd_report._roster_hits(payload, cards, []), [])
        payload["people"][0]["text"] = "как у p02"  # настоящий id другого человека
        self.assertTrue(cmd_report._roster_hits(payload, cards, []))
        self.assertTrue(cmd_report._personal_hits(payload, "p01", cards, a, []))

    def test_workspace_title_is_gated_only_under_anon(self):
        from lib import anon, cmd_report
        payload = {"meta": {"title": "Отчёт", "workspace_title": "boris_team"},
                   "people": [{"text": "всё ровно"}]}
        self.assertTrue(cmd_report._roster_hits(payload, self.cards(), []))
        a = anon.Anonymizer(list(self.cards().values()))
        self.assertTrue(cmd_report._personal_hits(payload, "anna", self.cards(), a, []))
        self.assertEqual(cmd_report._personal_hits(payload, "anna", self.cards(), None, []), [])


class WorkspaceTitleTest(_Base):
    """Заголовок workspace: личный файл пишется с предупреждением, `--anon` его обезличивает."""

    def set_title(self, title: str) -> None:
        cfg_path = ws_paths(self.ws)["config"]
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["workspace"]["title"] = title
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    def test_personal_with_foreign_name_in_title_warns_by_id(self):
        self.set_title("Команда Анны")
        res = self.ok("--personal", "boris")
        self.assertTrue((self.reports / "personal" / "boris.html").is_file())
        self.assertIn("workspace title contains name tokens of: anna", res.stderr)
        self.assertNotIn("Анн", res.stderr)

    def test_personal_of_title_owner_does_not_warn(self):
        self.set_title("Команда Анны")
        res = self.ok("--personal", "anna")
        self.assertNotIn("workspace title contains", res.stderr)

    def test_anon_group_title_is_anonymized(self):
        self.set_title("команда_Вершининой")
        res = self.ok("--anon", "--group", "--all-personal")
        self.assertNotIn("workspace title contains", res.stderr)
        files = [self.reports / "group.html", *sorted((self.reports / "personal").glob("*.html"))]
        self.assertEqual(len(files), 4)
        for path in files:
            text = path.read_text(encoding="utf-8")
            with self.subTest(file=path.name):
                self.assertNotIn("Вершинин", _data_without_i18n(text))
                self.assertNotIn("Вершинин", TITLE_RE.search(text).group(1))


class AnonUnderscoreTest(_Base):
    """`--anon` заменяет хэндл в `alice__repo`, и гейт после замены чист (файл пишется)."""

    def test_handle_with_double_underscore_is_replaced(self):
        card = ws_paths(self.ws)["people"] / "alice.yaml"
        card.write_text(report_fixture._card("alice", "Alice Smith", "team-a"), encoding="utf-8")
        tracker = ws_paths(self.ws)["runs"] / "process-reviews.json"
        text = tracker.read_text(encoding="utf-8")
        self.assertIn("запусти тесты в", text)
        tracker.write_text(text.replace("запусти тесты в", "клонируй alice__repo, запусти тесты в"),
                           encoding="utf-8")
        res = self.ok("--anon", "--group", "--reviewer")
        for name in ("group.html", "reviewer.html"):
            data = _data_without_i18n((self.reports / name).read_text(encoding="utf-8"))
            with self.subTest(file=name):
                if name == "reviewer.html":  # цитаты доказательств есть только у ревьюера
                    self.assertIn("__repo", data)
                self.assertNotIn("alice", data.casefold())
        self.assertNotIn("чужие токены", res.stderr)


class CardValidationTest(_Base):
    def test_bad_show_dynamics_stops_before_any_file(self):
        card = ws_paths(self.ws)["people"] / "vera.yaml"
        card.write_text(card.read_text(encoding="utf-8") + "  show_dynamics: maybe\n",
                        encoding="utf-8")
        res = self.sr("report")
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("vera", res.stderr)
        self.assertFalse(self.reports.exists())
        self.assertFalse(self.index.exists())


class SelfModeTest(_Base):
    MODE = "self"

    def test_default_self_no_group(self):
        self.ok()
        self.assertFalse((self.reports / "group.html").exists())
        self.assertTrue((self.reports / "reviewer.html").is_file())
        self.assertTrue((self.reports / "personal" / "me.html").is_file())
        self.assertTrue(self.index.is_file())

    def test_group_only_in_self_exit1(self):
        res = self.sr("report", "--group")
        self.assertEqual(res.returncode, 1)
        self.assertIn("в режиме self групповой отчёт не собирается", res.stderr)
        self.assertFalse((self.reports / "group.html").exists())

    def test_group_only_in_self_json_prints_empty_list(self):
        res = self.sr("report", "--group", "--json")
        self.assertEqual(res.returncode, 1)
        self.assertEqual(json.loads(res.stdout), [])

    def test_group_with_other_target_in_self_exit0(self):
        res = self.sr("report", "--group", "--reviewer")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("в режиме self групповой отчёт не собирается", res.stderr)
        self.assertFalse((self.reports / "group.html").exists())
        self.assertTrue((self.reports / "reviewer.html").is_file())


class IndexTest(_Base):
    def test_index_lists_two_registered_workspaces(self):
        second = self.base / "ws-copy"
        shutil.copytree(self.ws, second)
        registry.register(self.ws, "Первый стенд", "team", ["team-a", "team-b"])
        registry.register(second, "Второй стенд", "course", ["team-a"])
        self.ok()
        text = self.index.read_text(encoding="utf-8")
        self.assertIn("Первый стенд", text)
        self.assertIn("Второй стенд", text)
        self.assertIn((self.reports / "reviewer.html").as_uri(), text)
        self.assertIn(str(second), text)
        self.assertEqual(text.count("<tr>"), 3)  # шапка + две строки


class DoctorTest(_Base):
    def test_doctor_shows_reports_built_at(self):
        before = json.loads(self.sr("doctor", "--json").stdout)
        self.assertEqual(before["reports_built_at"], "не собирались")
        self.ok("--reviewer")
        after = json.loads(self.sr("doctor", "--json").stdout)
        self.assertRegex(after["reports_built_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")

    def test_doctor_lines_none_root(self):
        from lib import cmd_report
        self.assertEqual(cmd_report.doctor_lines(None), [])


class RealisticSizeTest(_util.TempHome, unittest.TestCase):
    """20 × 6, досье ≈ 20 КБ: все файлы собираются; размеры — цифры для DoD."""

    def test_20x6_sizes(self):
        with tempfile.TemporaryDirectory(prefix="sr-report-20x6-") as tmp:
            ws = report_fixture.make_workspace(Path(tmp) / "ws", people=20, periods=6,
                                               mode="team", realistic=True)
            started = time.monotonic()
            res = run_sr("report", "--json", home=self.home, cwd=ws)
            elapsed = time.monotonic() - started
            self.assertEqual(res.returncode, 0, res.stderr)
            items = json.loads(res.stdout)
            self.assertEqual(sum(1 for i in items if i["kind"] == "personal"), 20)
            self.assertEqual({i["kind"] for i in items},
                             {"group", "reviewer", "personal", "index"})
            by_kind = {i["kind"]: i for i in items if i["kind"] != "personal"}
            personal = [i["bytes"] for i in items if i["kind"] == "personal"]

            model = rd.collect(ws, lambda _m: None)
            payload = rd.build_payload(model, mode="reviewer", lang="ru", title="x",
                                       quote_periods=None)
            unlimited = len(render.render_html(rd.attach_html(payload),
                                               rd.load_i18n()).encode("utf-8"))
            limited = by_kind["reviewer"]["bytes"]
            self.assertLess(limited, unlimited)
            print(f"\n[20×6] reviewer quote_periods=3: {limited / 1024:.0f} КБ; "
                  f"без лимита: {unlimited / 1024:.0f} КБ; group: "
                  f"{by_kind['group']['bytes'] / 1024:.0f} КБ; personal: "
                  f"{min(personal) / 1024:.0f}–{max(personal) / 1024:.0f} КБ; "
                  f"CLI {elapsed:.1f} с")


if __name__ == "__main__":
    unittest.main()
