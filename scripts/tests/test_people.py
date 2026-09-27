"""lib.people: слияние с _default, список, добавление карточки, срез для судьи."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _util
from _util import copy_fixture

from lib import config, people, yamlmini
from lib.schema import validate
from lib.workspace import SrError, ws_paths

HIDDEN = ("channel", "template", "prefix")


class PeopleTest(_util.TempHome, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="sr-people-")
        self.root = copy_fixture("workspace-min", Path(self._tmp.name))
        self.people_dir = ws_paths(self.root)["people"]

    def tearDown(self) -> None:
        self._tmp.cleanup()
        super().tearDown()

    # --- load_person / list_people ---

    def test_load_person_merges_default(self):
        card = people.load_person(self.root, "alex")
        fb = card["feedback"]
        self.assertEqual(fb["address"], "ты")
        # поля, которых нет в карточке, пришли из _default.yaml
        self.assertEqual(fb["length"], "detailed")
        self.assertEqual(fb["channel"], "file")
        self.assertEqual(fb["prefix"], "")
        self.assertEqual(fb["skip_basics"], ["что такое git"])
        # None-списки → []
        self.assertEqual(fb["boundaries"], [])
        self.assertEqual(fb["avoid"], [])
        self.assertEqual(card["sources"], [])
        self.assertEqual(validate(card, "person"), [])

    def test_load_person_card_overrides(self):
        card = people.load_person(self.root, "sam")
        self.assertEqual(card["feedback"]["address"], "вы")
        self.assertEqual(card["feedback"]["length"], "short")
        self.assertEqual(len(card["feedback"]["boundaries"]), 2)

    def test_load_person_missing_exit2(self):
        with self.assertRaises(SrError) as ctx:
            people.load_person(self.root, "nobody")
        self.assertEqual(ctx.exception.code, 2)

    def test_load_person_bad_id_exit2(self):
        with self.assertRaises(SrError) as ctx:
            people.load_person(self.root, "../x")
        self.assertEqual(ctx.exception.code, 2)

    def test_load_person_invalid_card_exit1(self):
        (self.people_dir / "bob.yaml").write_text(
            'id: bob\nname: "Bob"\ngroup: team-a\nfeedback:\n  tone: loud\n', encoding="utf-8")
        with self.assertRaises(SrError) as ctx:
            people.load_person(self.root, "bob")
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("$.feedback.tone", ctx.exception.message)

    def test_load_person_id_mismatch_exit1(self):
        (self.people_dir / "bob.yaml").write_text(
            'id: rob\nname: "Bob"\ngroup: team-a\n', encoding="utf-8")
        with self.assertRaises(SrError) as ctx:
            people.load_person(self.root, "bob")
        self.assertEqual(ctx.exception.code, 1)

    def test_load_person_broken_yaml_exit1(self):
        (self.people_dir / "bob.yaml").write_text("id: bob\n\tname: x\n", encoding="utf-8")
        with self.assertRaises(SrError) as ctx:
            people.load_person(self.root, "bob")
        self.assertEqual(ctx.exception.code, 1)

    def test_list_people_sorted_without_default(self):
        ids = [card["id"] for card in people.list_people(self.root)]
        self.assertEqual(ids, ["alex", "sam"])

    def test_list_people_no_folder_empty(self):
        for path in self.people_dir.iterdir():
            path.unlink()
        self.people_dir.rmdir()
        self.assertEqual(people.list_people(self.root), [])

    # --- add_person ---

    def test_add_person_writes_valid_card(self):
        path, added = people.add_person(self.root, "kim", "Kim Test", "team-a")
        self.assertEqual(path, self.people_dir / "kim.yaml")
        self.assertFalse(added)
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("{{", text)
        self.assertIn("# feedback:", text)  # комментарии шаблона сохранены
        card = people.load_person(self.root, "kim")
        self.assertEqual(card["name"], "Kim Test")
        self.assertEqual(card["group"], "team-a")
        self.assertEqual(validate(card, "person"), [])

    def test_add_person_names_with_quotes_roundtrip(self):
        for pid, name in (("q1", 'Al "X"'), ("q2", "O'Neil")):
            people.add_person(self.root, pid, name, "team-a")
            self.assertEqual(people.load_person(self.root, pid)["name"], name)
            raw = yamlmini.load((self.people_dir / f"{pid}.yaml").read_text(encoding="utf-8"))
            self.assertEqual(raw["name"], name)

    def test_add_person_bad_names_exit2(self):
        for pid, name in (("b1", "A\"l'x"), ("b2", "a\nb"), ("b3", "a\tb")):
            with self.assertRaises(SrError) as ctx:
                people.add_person(self.root, pid, name, "team-a")
            self.assertEqual(ctx.exception.code, 2, name)
            self.assertFalse((self.people_dir / f"{pid}.yaml").exists())

    def test_quote_name_rules(self):
        self.assertEqual(people.quote_name("Alex"), '"Alex"')
        self.assertEqual(people.quote_name('Al "X"'), "'Al \"X\"'")
        self.assertEqual(people.quote_name(""), '""')

    def test_add_person_empty_name_exit2(self):
        with self.assertRaises(SrError) as ctx:
            people.add_person(self.root, "e1", "", "team-a")
        self.assertEqual(ctx.exception.code, 2)
        self.assertFalse((self.people_dir / "e1.yaml").exists())

    def test_add_person_duplicate_exit1(self):
        with self.assertRaises(SrError) as ctx:
            people.add_person(self.root, "alex", "Alex", "team-a")
        self.assertEqual(ctx.exception.code, 1)

    def test_add_person_bad_id_exit2(self):
        for pid in ("Bad_ID", "-x", "", "_default"):
            with self.assertRaises(SrError) as ctx:
                people.add_person(self.root, pid, "X", "team-a")
            self.assertEqual(ctx.exception.code, 2, pid)

    def test_add_person_new_group_goes_to_config(self):
        _, added = people.add_person(self.root, "lee", "Lee", "team-c")
        self.assertTrue(added)
        groups = config.load_config(self.root)["workspace"]["groups"]
        self.assertEqual(groups, ["team-a", "team-b", "team-c"])

    def test_add_person_bad_group_exit2(self):
        with self.assertRaises(SrError) as ctx:
            people.add_person(self.root, "lee", "Lee", "")
        self.assertEqual(ctx.exception.code, 2)
        self.assertFalse((self.people_dir / "lee.yaml").exists())

    # --- id: перевод строки, YAML-скаляры ---

    def test_check_id_trailing_newline_exit2(self):
        for pid in ("bob\n", "bob\r\n", "\nbob"):
            with self.assertRaises(SrError) as ctx:
                people.check_id(pid)
            self.assertEqual(ctx.exception.code, 2, repr(pid))

    def test_add_person_trailing_newline_no_file(self):
        with self.assertRaises(SrError) as ctx:
            people.add_person(self.root, "bob\n", "Bob", "team-a")
        self.assertEqual(ctx.exception.code, 2)
        self.assertEqual(sorted(p.name for p in self.people_dir.iterdir()),
                         ["_default.yaml", "alex.yaml", "sam.yaml"])

    def test_list_people_file_with_newline_in_name_exit1(self):
        (self.people_dir / "bob\n.yaml").write_text('id: bob\nname: "Bob"\ngroup: team-a\n',
                                                    encoding="utf-8")
        with self.assertRaises(SrError) as ctx:
            people.list_people(self.root)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("имя файла карточки", ctx.exception.message)

    def test_add_person_numeric_id_is_string(self):
        people.add_person(self.root, "2024", "N", "team-a")
        card = people.load_person(self.root, "2024")
        self.assertEqual(card["id"], "2024")
        self.assertIn('id: "2024"', (self.people_dir / "2024.yaml").read_text(encoding="utf-8"))

    def test_handwritten_unquoted_numeric_id_hints_quotes(self):
        (self.people_dir / "2024.yaml").write_text('id: 2024\nname: "N"\ngroup: team-a\n',
                                                   encoding="utf-8")
        with self.assertRaises(SrError) as ctx:
            people.load_person(self.root, "2024")
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("в кавычки", ctx.exception.message)

    # --- строгая схема вложенных блоков ---

    def test_feedback_typo_key_exit1_with_path(self):
        (self.people_dir / "bob.yaml").write_text(
            'id: bob\nname: "Bob"\ngroup: team-a\nfeedback:\n  adress: вы\n', encoding="utf-8")
        with self.assertRaises(SrError) as ctx:
            people.load_person(self.root, "bob")
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("$.feedback", ctx.exception.message)
        self.assertIn("adress", ctx.exception.message)

    def test_profile_and_consent_typo_keys_exit1(self):
        for block, line in (("profile", "  rol: dev"), ("consent", "  log: true")):
            (self.people_dir / "bob.yaml").write_text(
                f'id: bob\nname: "Bob"\ngroup: team-a\n{block}:\n{line}\n', encoding="utf-8")
            with self.assertRaises(SrError) as ctx:
                people.load_person(self.root, "bob")
            self.assertEqual(ctx.exception.code, 1, block)
            self.assertIn(f"$.{block}", ctx.exception.message)

    def test_top_level_extra_key_allowed(self):
        (self.people_dir / "bob.yaml").write_text(
            'id: bob\nname: "Bob"\ngroup: team-a\nnotes: что угодно\n', encoding="utf-8")
        self.assertEqual(people.load_person(self.root, "bob")["notes"], "что угодно")

    # --- порядок записи и подстановка ---

    def test_add_person_card_write_fails_config_untouched(self):
        before = config.load_config(self.root)["workspace"]["groups"]
        with mock.patch("lib.people.open", create=True, side_effect=OSError("диск полон")):
            with self.assertRaises(OSError):
                people.add_person(self.root, "lee", "Lee", "team-c")
        self.assertEqual(config.load_config(self.root)["workspace"]["groups"], before)

    def test_add_person_name_with_placeholder_literal(self):
        people.add_person(self.root, "ph", "{{group}}", "team-a")
        card = people.load_person(self.root, "ph")
        self.assertEqual(card["name"], "{{group}}")
        self.assertEqual(card["group"], "team-a")

    def test_list_people_skips_dotfiles(self):
        (self.people_dir / ".#alex.yaml").write_text("мусор редактора", encoding="utf-8")
        ids = [card["id"] for card in people.list_people(self.root)]
        self.assertEqual(ids, ["alex", "sam"])

    # --- person_context ---

    def test_context_sam(self):
        lines = people.person_context(people.load_person(self.root, "sam"))
        self.assertTrue(5 <= len(lines) <= 8, lines)
        joined = "\n".join(lines)
        for word in HIDDEN:
            self.assertNotIn(word, joined)
        self.assertIn("обращение: вы; длина: short; тон: supportive", lines)
        self.assertTrue(any(line.startswith("профиль: frontend developer, senior, 8 лет")
                            for line in lines), lines)
        self.assertTrue(any("не переносить советы на рабочий репозиторий" in line
                            for line in lines), lines)

    def test_context_alex(self):
        lines = people.person_context(people.load_person(self.root, "alex"))
        self.assertTrue(5 <= len(lines) <= 8, lines)
        self.assertIn("ограничения: нет прав на прод", lines)
        self.assertIn("не объяснять: что такое git", lines)
        self.assertIn("ценность: finding; динамика: да", lines)
        self.assertIn("открывать обобщением: нет; подписывать разделы: нет; приёмы исправления: да",
                      lines)

    def test_context_empty_profile_min5(self):
        people.add_person(self.root, "new", "New", "team-a")
        lines = people.person_context(people.load_person(self.root, "new"))
        self.assertGreaterEqual(len(lines), 5, lines)
        self.assertLessEqual(len(lines), 8, lines)
        self.assertNotIn("ограничения", "\n".join(lines))
        for line in lines:
            self.assertTrue(line.strip(), lines)

    def test_context_card_without_profile_key(self):
        card = people.load_person(self.root, "alex")
        del card["profile"]
        lines = people.person_context(card)
        self.assertGreaterEqual(len(lines), 5, lines)

    # --- реальный артефакт ---

    def test_fixture_whole_loads(self):
        config.load_config(self.root)
        for path in sorted(self.people_dir.glob("*.yaml")):
            if path.stem == "_default":
                continue
            card = people.load_person(self.root, path.stem)
            self.assertEqual(validate(card, "person"), [], path.name)

    def test_fixture_has_no_private_paths(self):
        src = _util.FIXTURES / "workspace-min"
        private_home = "/" + "Users" + "/"  # склейка: сам тест не должен содержать запрещённый путь
        for path in src.rglob("*"):
            if path.is_file():
                self.assertNotIn(private_home, path.read_text(encoding="utf-8"), path.name)


if __name__ == "__main__":
    unittest.main()
