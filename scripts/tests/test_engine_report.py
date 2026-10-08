"""Стык движка оценки с отчётами: `sr.py report` на трекере и ревью, которые записал движок
(`fixtures/process-reviews.sample.json`, `fixtures/reviews/`), собирает все файлы без
предупреждений и ошибок."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import _util  # noqa: F401 — кладёт scripts/ в sys.path
from _assess import make_ws
from _util import FIXTURES, TempHome, run_sr

from lib.workspace import ws_paths

SAMPLE = FIXTURES / "process-reviews.sample.json"
REVIEWS = FIXTURES / "reviews"
# Основные тесты берут заголовок без слов имён; заголовок фикстуры «Demo team», который делит слово
# с карточкой alex («Alex Demo»), — `TitleCollisionTest` ниже.
TITLE = "Retro workspace"


class ReportOnEngineOutputTest(TempHome, unittest.TestCase):
    title: str | None = TITLE
    # Ожидаемое предупреждение о словах карточек в заголовке workspace (личные файлы без --anon).
    title_warning: str | None = None

    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory(prefix="sr-engine-report-")
        self.addCleanup(tmp.cleanup)
        self.root = make_ws(Path(tmp.name))  # карточки alex, sam, kim
        paths = ws_paths(self.root)
        if self.title is not None:
            cfg = json.loads(paths["config"].read_text(encoding="utf-8"))
            cfg["workspace"]["title"] = self.title
            paths["config"].write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        paths["runs"].mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SAMPLE, paths["runs"] / "process-reviews.json")
        shutil.copytree(REVIEWS, paths["reviews"])
        self.reports = paths["reports"]
        doc = json.loads(SAMPLE.read_text(encoding="utf-8"))
        self.people = sorted({r["person_id"] for r in doc["runs"]})

    def report(self, *args: str) -> list[dict]:
        done = run_sr("--workspace", str(self.root), "report", *args, "--json",
                      home=self.home, cwd=self.root)
        self.assertEqual(done.returncode, 0, done.stderr)
        lines = done.stderr.strip().splitlines()
        if self.title_warning is not None:
            lines = [ln for ln in lines if not ln.startswith(self.title_warning)]
        self.assertEqual(lines, [], "report на выходе движка дал предупреждения")
        self.last_stderr = done.stderr
        items = json.loads(done.stdout)
        for item in items:
            path = Path(item["path"])
            self.assertTrue(path.is_file(), item)
            self.assertGreater(path.stat().st_size, 0, item)
            self.assertEqual(item["bytes"], path.stat().st_size, item)
        return items

    def test_every_mode_builds_without_warnings(self):
        items = self.report("--group", "--reviewer", "--all-personal", "--index")
        kinds = [i["kind"] for i in items]
        self.assertEqual(kinds, ["group", "reviewer", *["personal"] * len(self.people), "index"])
        self.assertEqual(sorted(i["person"] for i in items if i["kind"] == "personal"), self.people)
        reports = self.reports.resolve()
        for item in items[:-1]:
            where = reports / "personal" if item["kind"] == "personal" else reports
            self.assertEqual(Path(item["path"]).resolve().parent, where, item)
        self.assertEqual(Path(items[-1]["path"]).resolve(),
                         (self.home / ".retropair" / "reports" / "index.html").resolve())

    def test_default_targets_and_single_personal(self):
        default = self.report()
        self.assertEqual({i["kind"] for i in default}, {"group", "reviewer", "personal", "index"})
        one = self.report("--personal", self.people[0])
        self.assertEqual([(i["kind"], i["person"]) for i in one], [("personal", self.people[0])])

    def test_anon_builds_without_warnings(self):
        items = self.report("--group", "--reviewer", "--all-personal", "--anon")
        self.assertEqual(len(items), 2 + len(self.people))


class TitleCollisionTest(ReportOnEngineOutputTest):
    """Слово заголовка workspace, совпавшее со словом имени человека («Demo team» и карточка
    «Alex Demo»), не блокирует личные файлы: личный гейт без `--anon` заголовок не проверяет,
    а `report` предупреждает по id карточки; с `--anon` заголовок обезличен и настоящих токенов
    в нём нет. Все личные файлы пишутся (код 0). Базовые тесты класса — на том же заголовке."""

    title = None  # заголовок фикстуры «Demo team», карточка «Alex Demo»
    title_warning = "предупреждение: workspace title contains name tokens of: alex "

    def test_title_word_equal_to_name_does_not_block_personal(self):
        items = self.report("--all-personal")
        self.assertEqual(sorted(i["person"] for i in items), self.people)
        self.assertEqual(len(self.people), 3)
        self.assertIn(self.title_warning, self.last_stderr)
        self.assertNotIn("Demo", self.last_stderr)
        anon_items = self.report("--all-personal", "--anon")
        self.assertEqual(len(anon_items), len(self.people))
        self.assertEqual(self.last_stderr.strip(), "")
        for item in anon_items:
            text = Path(item["path"]).read_text(encoding="utf-8")
            data = json.loads(text.split('id="data">', 1)[1].split("</script>", 1)[0])
            data.pop("i18n", None)
            self.assertNotIn("Demo", json.dumps(data, ensure_ascii=False), item["path"])

if __name__ == "__main__":
    unittest.main()
