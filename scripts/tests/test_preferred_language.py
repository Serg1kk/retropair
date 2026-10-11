"""Preferred Language: override, fallback, CLI and real review artifacts."""
import json
import tempfile
import unittest
from pathlib import Path
import _util
import test_render_review as rr
from lib import config, people, render_review, tracker
from lib.workspace import SrError, ws_paths

class LanguageTest(_util.TempHome, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _util.copy_fixture("workspace-min", Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()
        super().tearDown()

    def test_preference_overrides_both_workspace_languages(self):
        for preferred, default in [("en", "ru"), ("ru", "en")]:
            with self.subTest(preferred=preferred):
                self.assertEqual(people.feedback_language({"profile": {"preferred_language": preferred}}, {"lang": default}), preferred)

    def test_old_empty_and_null_profiles_use_workspace(self):
        for card in [{}, {"profile": None}, {"profile": {}}, {"profile": {"preferred_language": None}}]:
            for lang in ["ru", "en"]:
                self.assertEqual(people.feedback_language(card, {"lang": lang}), lang)

    def test_invalid_effective_language_is_explicit_error(self):
        for card, cfg in [({"profile": {"preferred_language": "fr"}}, {"lang": "ru"}), ({}, {"lang": "fr"})]:
            with self.assertRaises(SrError):
                people.feedback_language(card, cfg)

    def test_review_skill_passes_effective_language_per_person(self):
        skill = (_util.PLUGIN / "skills/review/SKILL.md").read_text()
        self.assertIn('people language <person> --json', skill)
        self.assertIn('отдельно для каждого человека', skill)
        self.assertIn('во входах обоих агентов и критика', skill)
        self.assertIn('новые рекомендации', skill)
        orchestration = (_util.PLUGIN / "skills/review/references/orchestration.md").read_text()
        self.assertIn('`profile.preferred_language` → `config.lang`', orchestration)

    def test_invalid_preferences_fail_in_schema(self):
        path = ws_paths(self.root)["people"] / "alex.yaml"
        raw = path.read_text()
        for value in ['fr', 'English', '""', '7']:
            path.write_text(raw.replace("profile:", f"profile:\n  preferred_language: {value}"))
            with self.assertRaises(SrError):
                people.load_person(self.root, "alex")

    def test_card_roundtrip_context_and_cli(self):
        path = ws_paths(self.root)["people"] / "alex.yaml"
        path.write_text(path.read_text().replace("profile:", "profile:\n  preferred_language: en"))
        card = people.load_person(self.root, "alex")
        self.assertEqual(card["profile"]["preferred_language"], "en")
        self.assertIn("preferred_language: en", "\n".join(people.person_context(card)))
        result = _util.run_sr("people", "language", "alex", "--json", home=self.home, cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"lang": "en", "source": "profile.preferred_language", "workspace_lang": "ru"})
        fallback = _util.run_sr("people", "language", "sam", "--json", home=self.home, cwd=self.root)
        self.assertEqual(fallback.returncode, 0, fallback.stderr)
        self.assertEqual(json.loads(fallback.stdout), {"lang": "ru", "source": "config.lang", "workspace_lang": "ru"})
        plain = _util.run_sr("people", "language", "alex", home=self.home, cwd=self.root)
        self.assertEqual(plain.stdout.strip(), "en")
        missing = _util.run_sr("people", "language", "nobody", "--json", home=self.home, cwd=self.root)
        self.assertEqual(missing.returncode, 2)

    def test_template_optional_language_is_valid(self):
        people.add_person(self.root, "new", "Demo", "team-a")
        card = people.load_person(self.root, "new")
        self.assertIsNone(card["profile"]["preferred_language"])
        self.assertEqual(people.feedback_language(card, config.load_config(self.root)), "ru")

class RenderLanguageTest(rr.TrackerCase):
    def setUp(self):
        super().setUp()
        self.append_p1()
        self.judge = self.paths["pending"] / "alex" / "judge.json"

    set_config = rr.WriteReviewTest.set_config

    def test_en_preference_selects_real_en_template_preserves_scores_quotes_and_approval(self):
        path = self.paths["people"] / "alex.yaml"
        path.write_text(path.read_text().replace("profile:", "profile:\n  preferred_language: en"))
        before = tracker.find_run(tracker.load(self.root), rr.RUN_P1)
        doc = rr.parse_review(render_review.write_review(self.root, self.judge, "alex", now=rr.NOW).read_text())
        self.assertEqual(doc["meta"]["lang"], "en")
        self.assertEqual(doc["meta"]["status"], "draft")
        self.assertIn("**First.**", doc["sent"])
        self.assertIn('"ок, мержим"', doc["sent"])
        self.assertEqual(tracker.find_run(tracker.load(self.root), rr.RUN_P1)["axes"], before["axes"])
        self.assertEqual(json.loads(self.paths["config"].read_text())["lang"], "ru")

    def test_ru_preference_wins_over_en_default(self):
        self.set_config(lang="en")
        path = self.paths["people"] / "alex.yaml"
        path.write_text(path.read_text().replace("profile:", "profile:\n  preferred_language: ru"))
        doc = rr.parse_review(render_review.write_review(self.root, self.judge, "alex", now=rr.NOW).read_text())
        self.assertEqual(doc["meta"]["lang"], "ru")
        self.assertIn("**Первое.**", doc["sent"])
