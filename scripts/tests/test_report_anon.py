"""Обезличивание и гейт чужих имён (`lib/anon.py`)."""

from __future__ import annotations

import re
import unittest

import _util  # noqa: F401  (кладёт scripts/ в sys.path)
import report_fixture
from lib import anon

ANNA = {"id": "anna", "name": "Анна Вершинина"}
BORIS = {"id": "boris", "name": "Борис Каменев"}
VERA = {"id": "vera", "name": "Вера Ольховская"}
ROSTER = [VERA, ANNA, BORIS]


def _keys(value) -> list:
    """Все ключи dict при рекурсивном обходе, в порядке обхода."""
    out = []
    if isinstance(value, dict):
        for k, v in value.items():
            out.append(k)
            out.extend(_keys(v))
    elif isinstance(value, list):
        for v in value:
            out.extend(_keys(v))
    return out


def _fixture_words() -> set[str]:
    words = set()
    for pid, name in report_fixture.NAMES + [report_fixture.EXTRA]:
        words.add(pid.casefold())
        words.update(w.casefold() for w in name.split())
    return words


class ScrubContactsTest(unittest.TestCase):
    def test_five_kinds_are_replaced(self):
        email = "someone" + "@" + "example.org"
        users_path = "/" + "Users" + "/alice/proj/notes.md"
        text = (f"почта {email}, репо https://github.com/fake-owner/repo/tree/main, "
                f"пиши @fake_handle, файл {users_path} и /home/user/proj/tests")
        out = anon.scrub_contacts(text)
        self.assertIn("[email]", out)
        self.assertIn("[ссылка]", out)
        self.assertIn("[хэндл]", out)
        self.assertIn("~/proj/notes.md", out)
        self.assertIn("~/proj/tests", out)
        for leaked in (email, "github.com", "fake-owner", "@fake_handle", "alice",
                       "/" + "Users" + "/", "/home/"):
            self.assertNotIn(leaked, out)

    def test_home_prefix_at_end_or_before_space(self):
        home = "/" + "home" + "/user"
        users = "/" + "Users" + "/alice"
        out = anon.scrub_contacts(f"лог в {home} и {users} ({home}), путь {home}/x; конец {home}")
        self.assertNotIn("/home/", out)
        self.assertNotIn("user", out)
        self.assertNotIn("alice", out)
        self.assertIn("~/x", out)
        self.assertIn("(~)", out)
        self.assertTrue(out.endswith("конец ~"), out)

    def test_plain_text_untouched(self):
        text = "Проверил результат сам, 3 сессии, ход 5."
        self.assertEqual(anon.scrub_contacts(text), text)


class AnonymizerTest(unittest.TestCase):
    def test_id_map_by_sorted_id_and_deterministic(self):
        a = anon.Anonymizer(ROSTER)
        b = anon.Anonymizer(list(reversed(ROSTER)))
        self.assertEqual(a.id_map, {"anna": "p01", "boris": "p02", "vera": "p03"})
        self.assertEqual(a.id_map, b.id_map)
        self.assertEqual(a.name_map, b.name_map)
        self.assertEqual(sorted(a.name_map), ["anna", "boris", "vera"])
        self.assertEqual(len(set(a.name_map.values())), 3)

    def test_plain_ids_keep_p_prefix(self):
        a = anon.Anonymizer(ROSTER)
        self.assertEqual(a.code_prefix, "p")
        self.assertEqual(sorted(a.id_map.values()), ["p01", "p02", "p03"])

    def test_code_prefix_avoids_ids_that_look_like_codes(self):
        roster = [{"id": "p02", "name": "Анна Вершинина"}, {"id": "p01", "name": "Борис Каменев"},
                  {"id": "p03", "name": "Вера Ольховская"}]
        a = anon.Anonymizer(roster)
        self.assertNotEqual(a.code_prefix, "p")
        ids = {p["id"].casefold() for p in roster}
        self.assertFalse(ids & {c.casefold() for c in a.id_map.values()}, a.id_map)
        for pid in ids:
            self.assertNotRegex(pid, rf"(?i)^{re.escape(a.code_prefix)}\d+$")
        for code in a.id_map.values():
            self.assertRegex(code, rf"^{re.escape(a.code_prefix)}\d{{2}}$")
        # замена id → код не каскадирует: у каждого человека свой код
        out = a.text("p01, p02 и p03; REC-p02-001")
        self.assertEqual(out, f"{a.id_map['p01']}, {a.id_map['p02']} и {a.id_map['p03']}; "
                              f"REC-{a.id_map['p02']}-001")

    def test_code_prefix_skips_every_taken_letter(self):
        roster = [{"id": "p01", "name": "Анна Вершинина"}, {"id": "X7", "name": "Борис Каменев"},
                  {"id": "team-q01", "name": "Вера Ольховская"}]
        a = anon.Anonymizer(roster)
        self.assertNotIn(a.code_prefix, ("p", "x", "q"))
        self.assertEqual(a.code_prefix, anon.CODE_PREFIXES[3])

    def test_code_prefix_list_exhausted_raises(self):
        roster = [{"id": f"{prefix}01", "name": ""} for prefix in anon.CODE_PREFIXES]
        with self.assertRaisesRegex(ValueError, "префикс"):
            anon.Anonymizer(roster)

    def test_text_leaves_no_ids_words_or_stems(self):
        a = anon.Anonymizer(ROSTER)
        text = ("Анна Вершинина написала. С Анной Вершининой и Борису Каменеву, "
                "про Веру Ольховскую. anna, REC-anna-001, demo-team_boris_P1_20260118T100000Z, "
                "/home/user/proj/vera/tests, @fake-handle-anna")
        out = a.text(text)
        low = out.casefold()
        for p in ROSTER:
            self.assertIsNone(re.search(rf"(?<![a-z0-9]){p['id']}(?![a-z0-9])", low), out)
            for word in p["name"].split():
                self.assertNotIn(word[:4].casefold(), low, out)
        self.assertIn("p01", out)
        self.assertIn("REC-p01-001", out)
        self.assertIn(a.name_map["anna"], out)

    def test_deep_keeps_schema_keys_and_enum_values(self):
        a = anon.Anonymizer(ROSTER)
        payload = {
            "meta": {"mode": "personal", "lang": "ru", "title": "Отчёт: Анна Вершинина"},
            "people": [{"id": "anna", "name": "Анна Вершинина", "points": [
                {"date": "2026-01-18", "level": "competent", "text": "С Анной всё ровно",
                 "axes": {"verification": {"ie": False, "reason": None}},
                 "transitions": {"verification": "up"}}],
                "recs": [{"axis": "verification", "status": "open", "text": "Борис поможет"}]}],
        }
        out = a.deep(payload)
        self.assertEqual(_keys(out), _keys(payload))
        person = out["people"][0]
        self.assertEqual(person["id"], "p01")
        self.assertEqual(person["name"], a.name_map["anna"])
        self.assertEqual(person["points"][0]["level"], "competent")
        self.assertEqual(person["points"][0]["date"], "2026-01-18")
        self.assertEqual(person["points"][0]["transitions"], {"verification": "up"})
        self.assertEqual(person["recs"][0]["status"], "open")
        self.assertNotIn("Борис", person["recs"][0]["text"])
        self.assertNotIn("Анн", out["meta"]["title"])
        self.assertEqual(out["meta"]["mode"], "personal")

    def test_lang_picks_latin_or_cyrillic(self):
        en = anon.Anonymizer(ROSTER, lang="en")
        ru = anon.Anonymizer(ROSTER, lang="ru")
        for name in en.name_map.values():
            self.assertRegex(name, r"^[A-Za-z]+ [A-Za-z]+$")
        for name in ru.name_map.values():
            self.assertRegex(name, r"^[А-ЯЁа-яё]+ [А-ЯЁа-яё]+$")

    def test_unknown_lang_raises(self):
        with self.assertRaises(ValueError):
            anon.Anonymizer(ROSTER, lang="de")

    def test_fictional_names_never_match_fixture_roster(self):
        roster = [{"id": pid, "name": name}
                  for pid, name in report_fixture.NAMES + [report_fixture.EXTRA]]
        words = _fixture_words()
        for lang in ("ru", "en"):
            a = anon.Anonymizer(roster, lang=lang)
            self.assertEqual(len(set(a.name_map.values())), len(roster))
            for pid, fake in a.name_map.items():
                for word in fake.split():
                    self.assertNotIn(word.casefold(), words, f"{lang}: {pid} → {fake}")
            # вымышленное имя переживает собственную замену (ростер его не ловит)
            for fake in a.name_map.values():
                self.assertEqual(a.text(fake), fake)

    def test_word_lists_do_not_intersect_fixture_names(self):
        words = _fixture_words()
        for lst in (anon.FIRST_RU_M, anon.FIRST_RU_F, anon.LAST_RU, anon.FIRST_EN, anon.LAST_EN):
            self.assertFalse({w.casefold() for w in lst} & words)

    def test_extra_names_are_replaced(self):
        a = anon.Anonymizer([ANNA], extra_names=["Тимур Засекин"])
        out = a.text("Тимуру и Засекиной передай")
        self.assertNotIn("Тимур", out)
        self.assertNotIn("Засекин", out)
        self.assertIn("[имя]", out)

    def test_shared_name_words_become_placeholder(self):
        twin = {"id": "anna-k", "name": "Анна Каменева"}
        roster = [ANNA, twin, BORIS]
        a = anon.Anonymizer(roster)
        out = a.text("Анна Каменева и Анне Вершининой, Борис Каменев, Каменевым")
        self.assertNotIn("Анн", out)
        self.assertNotIn("Каменев", out)
        # общее слово не превращается в вымышленное имя одного из тёзок
        first_last = {pid: n.split() for pid, n in a.name_map.items()}
        self.assertNotIn(first_last["anna"][0], out)
        self.assertNotIn(first_last["anna-k"][0], out)
        self.assertIn(first_last["anna"][1], out)  # Вершининой — только у anna
        self.assertIn(first_last["boris"][0], out)  # Борис — только у boris
        self.assertIn("[имя]", out)

    def test_payload_names_by_id(self):
        twin = {"id": "anna-k", "name": "Анна Каменева"}
        roster = [ANNA, twin, BORIS]
        a = anon.Anonymizer(roster)
        payload = {"meta": {"title": "Отчёт"},
                   "people": [{"id": pid, "name": n} for pid, n in
                              (("anna", "Анна Вершинина"), ("anna-k", "Анна Каменева"),
                               ("boris", "Борис Каменев"))]}
        out = a.payload(payload)
        self.assertEqual([p["id"] for p in out["people"]], ["p01", "p02", "p03"])
        self.assertEqual([p["name"] for p in out["people"]],
                         [a.name_map["anna"], a.name_map["anna-k"], a.name_map["boris"]])
        self.assertEqual(payload["people"][0]["id"], "anna", "payload не меняется на месте")

    def test_short_id_only_as_whole_value(self):
        a = anon.Anonymizer([{"id": "me", "name": "Me"}], lang="en")
        self.assertEqual(a.text("show me the tests"), "show me the tests")
        self.assertEqual(a.deep({"id": "me"}), {"id": "p01"})

    def test_underscore_is_boundary_like_roster_gate(self):
        # Замены с той же `_`-границей, что у гейта ростера: `alice__repo` и `max_tokens`
        # заменяются, и гейт после замены чист (иначе `--anon` отказывал бы на таком тексте).
        alice = {"id": "p7x", "name": "Alice Smith"}
        mx = {"id": "u42", "name": "Max Power"}
        a = anon.Anonymizer([alice, mx], extra_names=["Bobbins"], lang="en")
        text = "clone alice__repo, set max_tokens, x_smith_y, clone_bobbins__x, Alice"
        out = a.deep({"meta": {"title": "Group · alice_team"}, "people": [{"text": text}]})
        low = anon.gate_text(out, out["meta"]["title"]).casefold()
        for token in ("alice", "smith", "max", "bobbins"):
            self.assertNotIn(token, low, low)
        self.assertEqual(anon.personal_gate(anon.gate_text(out, out["meta"]["title"]),
                                            anon.roster_gate_patterns([alice, mx], ["Bobbins"])), [])
        first = a.name_map["p7x"].split()[0]
        self.assertIn(f"clone {first}__repo", out["people"][0]["text"])


class PersonalGateTest(unittest.TestCase):
    def test_foreign_name_in_case_is_found_own_is_not(self):
        pats = anon.personal_gate_patterns(ANNA, [BORIS, VERA])
        self.assertTrue(all(isinstance(l, str) and isinstance(p, re.Pattern) for l, p in pats))
        self.assertEqual(anon.personal_gate("Анной Вершининой всё сделано, anna", pats), [])
        hits = anon.personal_gate("Спроси у Бориса", pats)
        self.assertEqual(len(hits), 1)
        self.assertIn("boris", hits[0])
        self.assertIn("Бориса", hits[0])
        self.assertTrue(anon.personal_gate("как у Ольховской", pats))
        self.assertTrue(anon.personal_gate("demo-team_vera_P1", pats))

    def test_namesake_matches_whole_word_only(self):
        target = {"id": "sasha", "name": "Александр Ветров"}
        other = {"id": "alex", "name": "Александра Кленова"}
        pats = anon.personal_gate_patterns(target, [other])
        self.assertEqual(anon.personal_gate("Александр молодец", pats), [])
        self.assertTrue(anon.personal_gate("и Александра тоже", pats))
        twin = {"id": "anna-k", "name": "Анна Каменева"}
        pats = anon.personal_gate_patterns(ANNA, [twin])
        self.assertEqual(anon.personal_gate("Анна, Анной", pats), [])
        self.assertTrue(anon.personal_gate("с Каменевой", pats))

    def test_names_on_iy_are_found_in_every_case(self):
        other = {"id": "leo", "name": "Леонтий Дмитриев"}
        pats = anon.personal_gate_patterns(ANNA, [other])
        for form in ("Леонтий", "Леонтия", "Леонтию", "Леонтием", "Леонтии"):
            self.assertTrue(anon.personal_gate(f"спроси: {form}", pats), form)
        extra = anon.personal_gate_patterns(ANNA, [], extra_names=["Евгений"])
        for form in ("Евгения", "Евгению", "Евгением", "Евгении"):
            self.assertTrue(anon.personal_gate(f"у {form}", extra), form)
        text = anon.Anonymizer([ANNA, other]).text("ответ Леонтию и Леонтием")
        self.assertNotIn("Леонти", text)

    def test_extra_names_join_gate(self):
        pats = anon.personal_gate_patterns(ANNA, [], extra_names=["Тимур Засекин"])
        self.assertTrue(anon.personal_gate("Засекиным", pats))

    def test_gate_text_nested_payload_keeps_cyrillic(self):
        payload = {"meta": {"mode": "personal", "lang": "ru"},
                   "people": [{"id": "anna", "name": "Анна Вершинина", "points": [
                       {"review": {"sent_md": "Как у Каменева, только лучше."}, "sessions": 3}]}]}
        text = anon.gate_text(payload, "Личный отчёт")
        self.assertIn("Каменева", text)
        self.assertIn("Личный отчёт", text)
        self.assertNotIn("\\u", text)
        self.assertNotIn("sent_md", text)
        self.assertNotIn("personal", text)
        pats = anon.personal_gate_patterns(ANNA, [BORIS])
        self.assertTrue(anon.personal_gate(text, pats))

    def test_gate_text_unescapes_html(self):
        from lib import md
        other = {"id": "kob", "name": "Kate O'Brien"}
        payload = {"people": [{"points": [{"review": {"sent_html": md.md_to_html("Как `O'Brien`")}}]}]}
        text = anon.gate_text(payload, "Отчёт")
        self.assertIn("O'Brien", text)
        self.assertTrue(anon.personal_gate(text, anon.personal_gate_patterns(ANNA, [other])))

    def test_roster_gate_finds_any_roster_token(self):
        pats = anon.roster_gate_patterns([ANNA, BORIS], extra_names=["Тимур Засекин"])
        self.assertEqual(anon.personal_gate("p01 и Савелий Титов, [имя]", pats), [])
        for text in ("Анной", "boris", "Засекиным", "REC-anna-001"):
            self.assertTrue(anon.personal_gate(text, pats), text)

    def test_roster_gate_underscore_is_boundary(self):
        alice = {"id": "p7x", "name": "Alice Smith"}
        pats = anon.roster_gate_patterns([alice], extra_names=["Bobbins"])
        for text in ("alice__repo", "x_alice_y", "Smith_2", "clone_bobbins__x"):
            self.assertTrue(anon.personal_gate(text, pats), text)
        self.assertEqual(anon.personal_gate("malice and blacksmith", pats), [])

    def test_personal_gate_name_keeps_word_boundary(self):
        # `_`-граница в личном гейте только у id: имя «Max» не ловится в `max_tokens`.
        kim = {"id": "kim", "name": "Kim"}
        mx = {"id": "u42", "name": "Max"}
        pats = anon.personal_gate_patterns(kim, [mx])
        self.assertEqual(anon.personal_gate("set max_tokens to 10", pats), [])
        self.assertTrue(anon.personal_gate("ask Max", pats))
        self.assertTrue(anon.personal_gate("REC_u42_1", pats))

    def test_gate_text_skips_workspace_title_only_on_request(self):
        # Личный гейт без --anon: заголовок workspace — не текст о человеке, его слово, совпавшее
        # со словом имени, не чужое имя. В остальном тексте то же слово по-прежнему ловится.
        payload = {"meta": {"title": "Group · Demo team", "workspace_title": "Demo team"},
                   "people": [{"points": [{"review": {"sent_md": "all fine"}}]}]}
        text = anon.gate_text(payload, "Group · Demo team", skip_workspace_title=True)
        self.assertNotIn("Demo", text)
        self.assertIn("Group", text)
        alex = {"id": "alex", "name": "Alex Demo"}
        sam = {"id": "sam", "name": "Sam"}
        pats = anon.personal_gate_patterns(sam, [alex])
        self.assertEqual(anon.personal_gate(text, pats), [])
        for quote in ("как у Demo в прошлый раз", "спроси Alex"):
            payload["people"][0]["points"][0]["review"]["sent_md"] = quote
            self.assertTrue(anon.personal_gate(
                anon.gate_text(payload, "Personal · Sam", skip_workspace_title=True), pats), quote)

    def test_gate_text_keeps_workspace_title_by_default(self):
        # Гейт обезличенного файла (--anon) видит заголовок как обычный текст.
        payload = {"meta": {"title": "Group · alice_team", "workspace_title": "alice_team"},
                   "people": []}
        text = anon.gate_text(payload, "Group · alice_team")
        self.assertIn("alice_team", text)
        alice = {"id": "p7x", "name": "Alice Smith"}
        self.assertTrue(anon.personal_gate(text, anon.roster_gate_patterns([alice])))

    def test_title_name_owners(self):
        self.assertEqual(anon.title_name_owners("Команда Анны", [BORIS, ANNA, VERA]), ["anna"])
        self.assertEqual(anon.title_name_owners("anna и Каменев", [BORIS, ANNA, VERA]),
                         ["anna", "boris"])
        self.assertEqual(anon.title_name_owners("Demo team", ROSTER), [])
        self.assertEqual(anon.title_name_owners("", ROSTER), [])

    def test_short_id_does_not_hit_schema(self):
        lev = {"id": "lev", "name": "Лев Гринёв"}
        pats = anon.personal_gate_patterns(ANNA, [lev])
        payload = {"level": "competent", "status": "open",
                   "people": [{"text": "next level, open questions", "level": "developing"}]}
        self.assertEqual(anon.personal_gate(anon.gate_text(payload, "Отчёт"), pats), [])
        self.assertTrue(anon.personal_gate("REC-lev-001", pats))


if __name__ == "__main__":
    unittest.main()
