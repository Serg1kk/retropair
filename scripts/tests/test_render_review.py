"""`lib/render_review.py` и команда `render-review`: сборка текста ревью по шаблону, запреты
текста, выбор шаблона, файл ревью (frontmatter, служебный блок, линейки, досье)."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from unittest import mock

import _util  # noqa: F401 — кладёт scripts/ в sys.path
from _assess import TEMPLATE_EN, TEMPLATE_RU, payload
from test_tracker import NOW, RUN_P1, TrackerCase

from lib import i18n, render_review, tracker
from lib.workspace import SrError

HR_RE = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")


def card(address: str = "ты", length: str = "detailed", **feedback) -> dict:
    """Эффективная карточка в минимальном виде: только поля, которые читает рендер."""
    fb = {"address": address, "length": length, "tone": "direct", "opening_summary": False,
          "mark_sections": False, "practices": True, "skip_basics": [], "boundaries": [], "avoid": [],
          "value_focus": "finding", "show_dynamics": True, "channel": "file", "template": "default",
          "prefix": ""}
    fb.update(feedback)
    return {"id": "alex", "name": "Alex Demo", "group": "team-a", "feedback": fb}


def parts(**patch) -> dict:
    obj = copy.deepcopy(payload()["data"]["feedback_parts"])
    obj.update(copy.deepcopy(patch))
    return obj


def run_stub(**patch) -> dict:
    run = {
        "run_id": RUN_P1, "person_id": "alex",
        "period": {"label": "p1", "from": "2026-09-01T11:30:00Z", "to": "2026-09-02T10:00:00Z", "anchor": None},
        "sessions_reviewed": copy.deepcopy(payload()["data"]["session_scope"]["sessions"]),
        "sessions_skipped_known": [],
    }
    run.update(patch)
    return run


def parse_review(text: str) -> dict:
    """Разбор файла ревью по правилам парсера отчёта: frontmatter, две линейки вне code-fence."""
    lines = text.splitlines()
    assert lines[0] == "---", lines[0]
    end = lines.index("---", 1)
    meta = dict(line.split(": ", 1) for line in lines[1:end])
    body = lines[end + 1:]
    rules, fence = [], False
    for i, line in enumerate(body):
        if line.lstrip().startswith("```"):
            fence = not fence
            continue
        if not fence and HR_RE.match(line):
            rules.append(i)
    assert len(rules) >= 2, rules
    return {"meta": meta, "service": "\n".join(body[:rules[0]]),
            "sent": "\n".join(body[rules[0] + 1:rules[1]]).strip(),
            "dossier": "\n".join(body[rules[1] + 1:]).strip()}


LEVER_RU = "Соседний рычаг"
TRY_RU = "Стоит попробовать"


class RenderSentTest(TrackerCase):
    """Формат по шапке настоящего шаблона `templates/feedback.<lang>.md`."""

    def render(self, p=None, c=None, lang="ru", run=None, template=TEMPLATE_RU) -> str:
        return render_review.render_sent(p or parts(), run or run_stub(), c or card(), lang,
                                         Path(template).read_text(encoding="utf-8"))

    def test_all_placeholders_filled_and_header_comment_dropped(self):
        text = self.render()
        self.assertNotIn("{{", text)
        self.assertNotIn("<!--", text)
        self.assertIn("**Если читать быстро: что попробовать иначе.**", text)
        self.assertIn("**Дальше по восьми критериям, на чём это стоит.**", text)
        self.assertIn("**Первое.**", text)
        self.assertIn("**Второе.**", text)
        self.assertNotIn("**Третье.**", text)

    def test_action_line_exactly_by_template_header(self):
        first = next(line for line in self.render().splitlines() if line.startswith("**Первое.**"))
        self.assertEqual(first, (
            "**Первое.** 2 сентября, сессия про форму входа, ближе к концу: «ок, мержим»; дифф на четыре "
            "файла ушёл в ветку без запуска тестов, и сломанная валидация всплыла бы уже у коллег. "
            "Стоит попробовать: перед словом «мержим» запускать тесты и открывать хотя бы один изменённый "
            "файл. Соседний рычаг: попросить агента показать команду проверки и её вывод. Соседний рычаг: "
            "держать короткий список того, что смотреть в диффе."))

    def test_action_line_en_by_template_header(self):
        text = self.render(lang="en", template=TEMPLATE_EN)
        first = next(line for line in text.splitlines() if line.startswith("**First.**"))
        self.assertTrue(first.startswith('**First.** September 2, сессия про форму входа, ближе к концу: "ок, мержим"; '),
                        first)
        self.assertEqual(first.count("Worth trying: "), 1)
        self.assertEqual(first.count("A related lever: "), 2)

    def test_fields_as_is_with_final_period_once(self):
        p = parts()
        p["actions"][0]["cost"] = "Postgres упал на миграции."
        p["actions"][0]["try"] = "API-ключ держать в переменной окружения"
        line = next(x for x in self.render(p).splitlines() if x.startswith("**Первое.**"))
        self.assertIn("«ок, мержим»; Postgres упал на миграции. Стоит попробовать: API-ключ держать", line)
        self.assertNotIn("..", line)

    def test_try_label_not_deduplicated_silently(self):
        p = parts()
        p["actions"][0]["try"] = "стоит попробовать запускать тесты"
        line = next(x for x in self.render(p).splitlines() if x.startswith("**Первое.**"))
        self.assertIn("Стоит попробовать: стоит попробовать запускать тесты.", line)

    def test_null_date_capitalises_place(self):
        p = parts()
        p["actions"][0]["date"] = None
        p["actions"][0]["place"] = "сессия про форму входа"
        line = next(x for x in self.render(p).splitlines() if x.startswith("**Первое.**"))
        self.assertTrue(line.startswith("**Первое.** Сессия про форму входа: «ок, мержим»"), line)

    def test_empty_levers_no_tail(self):
        line = next(x for x in self.render().splitlines() if x.startswith("**Второе.**"))
        self.assertNotIn(LEVER_RU, line)
        self.assertTrue(line.endswith("смотреть результат после первой."), line)

    def test_opening_is_the_field_as_is_without_extra_coverage_line(self):
        blocks = self.render().split("\n\n")
        self.assertEqual(blocks[1], parts()["opening"])
        self.assertFalse(hasattr(render_review, "coverage_line"))

    def test_card_opening_flags_do_not_change_render(self):
        plain = self.render()
        self.assertEqual(self.render(c=card(opening_summary=True, mark_sections=True)), plain)

    def test_eight_criteria_with_axis_names_and_verdicts_ru(self):
        text = self.render()
        for crit in parts()["criteria"]:
            name = i18n.t("axes", crit["axis"], "ru")
            verdict = i18n.t("verdicts", crit["verdict"], "ru")
            self.assertIn(f"**{name}.** {verdict} {crit['evidence']}", text.splitlines())

    def test_eight_criteria_en(self):
        text = self.render(lang="en", template=TEMPLATE_EN)
        self.assertIn("**First.**", text)
        self.assertIn("September 2", text)
        self.assertIn('"ок, мержим"', text)  # цитата дословна, кавычки языка текста
        for crit in parts()["criteria"]:
            self.assertIn(f"**{i18n.t('axes', crit['axis'], 'en')}.** "
                          f"{i18n.t('verdicts', crit['verdict'], 'en')}", text)

    def test_empty_evidence_line_ends_with_verdict(self):
        p = parts()
        p["criteria"][4]["evidence"] = ""
        name = i18n.t("axes", "context_engineering", "ru")
        self.assertIn(f"**{name}.** {i18n.t('verdicts', 'not_visible', 'ru')}", self.render(p).splitlines())

    def test_verdicts_only_from_verdicts_section(self):
        for key in ("second_place_above", "third_place_above"):
            self.assertEqual(render_review.verdict_text(key, "ru"), i18n.t("verdicts", key, "ru"))
            self.assertNotIn(f"verdict_{key}", i18n.keys(("cli", "assess")))
        with self.assertRaises(SrError) as ctx:
            render_review.verdict_text("great", "ru")
        self.assertEqual(ctx.exception.code, 1)

    def test_prefix_and_empty_prefix_line_removed(self):
        blocks = self.render(c=card(prefix="Привет!")).split("\n\n")
        self.assertEqual(blocks[0], "**Разбор сессий, период «p1»**")  # заголовок с периодом над блоком
        self.assertEqual(blocks[1], "Привет!")
        text = self.render()
        self.assertEqual(text.split("\n\n")[1], parts()["opening"])
        self.assertNotIn("\n\n\n", text)

    def test_closing_null_removed_and_present_kept(self):
        self.assertFalse(self.render().endswith("\n\n"))
        self.assertTrue(self.render().endswith(parts()["criteria"][-1]["evidence"]))
        text = self.render(parts(closing="Коммит перед рефакторингом 1 сентября держит откат дешёвым."))
        self.assertTrue(text.rstrip().endswith("держит откат дешёвым."))

    def test_short_keeps_levers_visible_no_silent_trim(self):
        text = self.render(c=card(length="short"))
        self.assertIn(LEVER_RU, text)

    def test_paragraph_is_one_line(self):
        for line in self.render().splitlines():
            self.assertFalse(line.startswith(" "), line)


class ForbiddenTest(TrackerCase):
    def problems(self, extra: str, c=None, p=None, lang="ru") -> list[str]:
        base = render_review.render_sent(p or parts(), run_stub(), c or card(), lang,
                                         TEMPLATE_RU.read_text(encoding="utf-8"))
        return render_review.check_forbidden(base + "\n\n" + extra, p or parts(), c or card(), lang)

    def assertBanned(self, extra: str, needle: str | None = None, **kw):
        found = self.problems(extra, **kw)
        self.assertTrue(found, f"не пойман запрет: {extra!r}")
        if needle:
            self.assertTrue(any(needle in f for f in found), found)

    def test_clean_text_passes(self):
        self.assertEqual(self.problems(""), [])

    def test_score_out_of_ten(self):
        self.assertBanned("Здесь 7 из 10.")

    def test_score_word(self):
        self.assertBanned("Это на высокий балл.")

    def test_score_slash(self):
        self.assertBanned("Проверка тянет на 3/4.")

    def test_level_name_ru(self):
        self.assertBanned("Работа на уровне: Оркестратор.")

    def test_level_key(self):
        self.assertBanned("Это confident_operator.")

    def test_level_word_en_with_level(self):
        self.assertBanned("This is a competent level of work.", lang="en")

    def test_snake_case_in_prose(self):
        self.assertBanned("Слабее всего task_framing.", "task_framing")

    def test_rec_id(self):
        self.assertBanned("Смотри REC-alex-001.", "REC-")

    def test_session_hex_id(self):
        self.assertBanned("Это было в сессии a1b2c3d4e5.", "a1b2c3d4e5")

    def test_em_dash_outside_quote(self):
        self.assertBanned("Тесты не запускались — и это заметно.", "—")

    def test_comparison_with_others(self):
        self.assertBanned("Здесь лучше, чем другие.")

    def test_hash_heading(self):
        self.assertBanned("# X")

    def test_bare_rule(self):
        self.assertBanned("---")

    def test_code_fence(self):
        self.assertBanned("```")

    def test_you_formal_with_informal_address(self):
        self.assertBanned("Попробуйте, как вам удобнее.", c=card(address="ты"))

    def test_you_informal_with_formal_address(self):
        found = self.problems("", c=card(address="вы"))
        self.assertEqual(found, [])
        self.assertTrue(self.problems("Твой коммит на месте.", c=card(address="вы")))

    def test_quote_may_carry_ids_digits_and_dash(self):
        p = parts()
        p["actions"][0]["quote"] = "запусти run_tests, коммит a1b2c3d4e5 — готово"
        self.assertEqual(self.problems("", p=p), [])

    def test_quote_with_inner_guillemets_is_exempt_as_field(self):
        # вложенные «…» ломают поиск по кавычкам: цитата исключается по значению поля quote
        p = parts()
        p["actions"][0]["quote"] = "ответ «да» — и коммит a1b2c3d4e5"
        self.assertEqual(self.problems("", p=p), [])

    def test_quote_in_guillemets_in_prose_is_fine(self):
        self.assertEqual(self.problems("Фраза «прогони pytest — и 1/4 готово» стоит в логе."), [])

    def test_ru_ambiguous_level_names_as_speech_allowed(self):
        self.assertEqual(self.problems("В логах мало данных о проверке. Навык развивается от сессии к сессии."), [])

    def test_ru_ambiguous_level_names_next_to_level_banned(self):
        self.assertBanned("Уровень: мало данных.", "имя уровня")
        self.assertBanned("Твой уровень развивается.", "имя уровня")

    def test_en_developing_as_verb_allowed(self):
        self.assertEqual(self.problems("You spent a while developing the API.", lang="en",
                                       c=card(address="you")), [])

    def test_left_placeholder(self):
        self.assertBanned("{{foo}}", "{{")

    def test_short_three_actions(self):
        p = parts()
        p["actions"].append(copy.deepcopy(p["actions"][1]))
        p["actions"][2]["levers"] = []
        self.assertTrue(self.problems("", p=p, c=card(length="short")))

    def test_short_two_sentences_in_criterion(self):
        p = parts()
        p["criteria"][1]["evidence"] = "Цель названа сразу. Границы тоже."
        found = self.problems("", p=p, c=card(length="short"))
        self.assertTrue(any("task_framing" in f or "Постановка" in f for f in found), found)

    def test_short_one_sentence_ok(self):
        p = parts()
        for a in p["actions"]:
            a["levers"] = []
        self.assertEqual(self.problems("", p=p, c=card(length="short")), [])
        self.assertEqual(self.problems("", p=p, c=card(practices=False)), [])

    def test_detailed_four_actions(self):
        p = parts()
        p["actions"] += copy.deepcopy(p["actions"])
        self.assertTrue(self.problems("", p=p))

    def test_short_with_levers_is_error_naming_condition(self):
        found = self.problems("", c=card(length="short"))
        self.assertTrue(any("short" in f and "рычаг" in f for f in found), found)

    def test_practices_false_with_levers_is_error(self):
        found = self.problems("", c=card(practices=False))
        self.assertTrue(any("practices: false" in f and "рычаг" in f for f in found), found)

    def test_old_form_markers(self):
        self.assertBanned("**Приёмы под то, что стоит подтянуть.**", "старой формы")
        self.assertBanned("**Рекомендация (попробовать):** запускать тесты.", "старой формы")

    def test_angle_bracket_outside_quotes(self):
        self.assertBanned("> цитатный блок", "«>»")

    def test_angle_bracket_inside_quotes_allowed(self):
        self.assertEqual(self.problems("В логе стоит «exit code > 0»."), [])

    def test_empty_opening(self):
        found = self.problems("", p=parts(opening="  "))
        self.assertTrue(any("opening" in f for f in found), found)

    def test_line_break_inside_field(self):
        p = parts()
        p["actions"][0]["cost"] = "дифф ушёл без тестов\nи сломал валидацию"
        found = self.problems("", p=p)
        self.assertTrue(any("actions[0].cost" in f and "перенос" in f for f in found), found)


class TemplateTest(TrackerCase):
    def test_explicit_template(self):
        path = render_review.resolve_template(self.root, card(), "ru", str(TEMPLATE_RU))
        self.assertEqual(path, TEMPLATE_RU)

    def test_explicit_missing_is_2(self):
        with self.assertRaises(SrError) as ctx:
            render_review.resolve_template(self.root, card(), "ru", str(self.root / "nope.md"))
        self.assertEqual(ctx.exception.code, 2)

    def test_card_workspace_template(self):
        target = self.paths["templates"] / "feedback.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{{prefix}}", encoding="utf-8")
        self.assertEqual(render_review.resolve_template(self.root, card(template="workspace"), "ru", None), target)

    def test_card_named_template(self):
        target = self.paths["templates"] / "mine.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{{prefix}}", encoding="utf-8")
        self.assertEqual(render_review.resolve_template(self.root, card(template="mine.md"), "ru", None), target)

    def test_card_named_missing_is_1_with_path(self):
        with self.assertRaises(SrError) as ctx:
            render_review.resolve_template(self.root, card(template="mine.md"), "ru", None)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("mine.md", ctx.exception.message)

    def test_card_name_with_separators_is_2(self):
        for bad in ("../x", "a/b.md", "a\\b.md", ".."):
            with self.assertRaises(SrError) as ctx:
                render_review.resolve_template(self.root, card(template=bad), "ru", None)
            self.assertEqual(ctx.exception.code, 2, bad)

    def test_plugin_template_missing_is_1_with_path(self):
        fake = self.root / "fake-plugin"
        with mock.patch.object(render_review, "plugin_root", return_value=fake):
            with self.assertRaises(SrError) as ctx:
                render_review.resolve_template(self.root, card(), "ru", None)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn(str(fake / "templates" / "feedback.ru.md"), ctx.exception.message)
        self.assertIn("промптами оценки", ctx.exception.message)

    def test_plugin_template_default(self):
        fake = self.root / "fake-plugin"
        (fake / "templates").mkdir(parents=True)
        (fake / "templates" / "feedback.en.md").write_text("{{prefix}}", encoding="utf-8")
        with mock.patch.object(render_review, "plugin_root", return_value=fake):
            path = render_review.resolve_template(self.root, card(), "en", None)
        self.assertEqual(path, fake / "templates" / "feedback.en.md")


class WriteReviewTest(TrackerCase):
    def setUp(self) -> None:
        super().setUp()
        self.append_p1()
        self.judge = self.paths["pending"] / "alex" / "judge.json"

    def set_config(self, **fields) -> None:
        cfg = json.loads(self.paths["config"].read_text(encoding="utf-8"))
        cfg.update(fields)
        self.paths["config"].write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    def write_review(self, **kw) -> Path:
        kw.setdefault("template", str(TEMPLATE_RU))
        return render_review.write_review(self.root, self.judge, "alex", now=NOW, **kw)

    def test_file_path_and_frontmatter(self):
        path = self.write_review()
        self.assertEqual(path, self.paths["reviews"] / "alex" / f"{RUN_P1}.md")
        doc = parse_review(path.read_text(encoding="utf-8"))
        self.assertEqual(list(doc["meta"]), ["person_id", "run_id", "period", "status", "lang", "template"])
        self.assertEqual(doc["meta"]["person_id"], "alex")
        self.assertEqual(doc["meta"]["run_id"], RUN_P1)
        self.assertEqual(doc["meta"]["period"], "p1")
        self.assertEqual(doc["meta"]["lang"], "ru")
        self.assertEqual(doc["meta"]["template"], "feedback.ru.md")

    def test_service_block_rules_and_dossier(self):
        text = self.write_review().read_text(encoding="utf-8")
        doc = parse_review(text)
        self.assertTrue(doc["service"].lstrip().startswith("<!-- service -->"), doc["service"])
        self.assertIn(RUN_P1, doc["service"])
        self.assertIn("**Первое.**", doc["sent"])
        self.assertNotIn(RUN_P1, doc["sent"])
        dossier_title = i18n.t(("cli", "assess"), "dossier_title", "ru")
        self.assertTrue(doc["dossier"].startswith(f"## {dossier_title}"), doc["dossier"][:80])
        self.assertIn("REC-alex-001", doc["dossier"])
        self.assertIn("verification", doc["dossier"])
        self.assertIn("5c2e8f19-a4b7-4d20-8e63-f1b09c7d2a46", doc["dossier"])
        self.assertIn("normalize", doc["dossier"])
        self.assertIn("--from-turn 10", doc["dossier"])  # grown-сессия с хода 10

    def test_run_gets_draft_and_feedback_draft(self):
        self.write_review()
        run = tracker.find_run(tracker.load(self.root), RUN_P1)
        doc = parse_review((self.paths["reviews"] / "alex" / f"{RUN_P1}.md").read_text(encoding="utf-8"))
        self.assertEqual(run["review_status"], "draft")
        self.assertEqual(run["feedback_draft"], doc["sent"])
        self.assertEqual(doc["meta"]["status"], "draft")
        self.assertEqual(tracker.validate_file(self.root), [])

    def test_draft_with_nested_quote_passes_validate(self):
        obj = payload()
        obj["data"]["feedback_parts"]["actions"][0]["quote"] = "ответ «да» — и готово, коммит a1b2c3d4e5"
        self.append(obj, now=NOW, period_label="p1", replace=RUN_P1)
        self.write_review()
        self.assertEqual(tracker.validate_file(self.root), [])

    def test_self_mode_is_approved(self):
        self.set_config(mode="self")
        doc = parse_review(self.write_review().read_text(encoding="utf-8"))
        self.assertEqual(doc["meta"]["status"], "approved")
        self.assertEqual(tracker.find_run(tracker.load(self.root), RUN_P1)["review_status"], "approved")

    def test_team_without_gate_is_approved(self):
        self.set_config(reviewer_gate=False)
        doc = parse_review(self.write_review().read_text(encoding="utf-8"))
        self.assertEqual(doc["meta"]["status"], "approved")

    def test_sha_mismatch_is_1(self):
        self.judge.write_text(self.judge.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with self.assertRaises(SrError) as ctx:
            self.write_review()
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("sha256", ctx.exception.message)
        self.assertFalse((self.paths["reviews"] / "alex" / f"{RUN_P1}.md").exists())

    def bad_judge(self, field: str, text: str) -> Path:
        """Выход судьи с запрещённой фразой; sha прогона подменён, чтобы дойти до проверки запретов.

        `append` такой текст не пропустил бы (балльная лексика), поэтому прогон записан по
        чистому файлу, а рендер получает испорченный.
        """
        obj = payload()
        obj["data"]["feedback_parts"][field] = text
        path = self.write(obj, name="judge-bad.json")
        doc = self.tracker_doc()
        doc["runs"][0]["source"]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.save_doc(doc)
        return path

    def test_violation_is_1_and_no_file(self):
        path = self.bad_judge("closing", "По сравнению с другими, это 7 из 10")
        with self.assertRaises(SrError) as ctx:
            render_review.write_review(self.root, path, "alex", template=str(TEMPLATE_RU), now=NOW)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("7 из 10", ctx.exception.message)
        self.assertFalse((self.paths["reviews"] / "alex").exists())
        self.assertIsNone(tracker.find_run(tracker.load(self.root), RUN_P1)["feedback_draft"])

    def test_unknown_run_is_1(self):
        with self.assertRaises(SrError) as ctx:
            self.write_review(run_id="nope")
        self.assertEqual(ctx.exception.code, 1)

    def test_run_of_other_person_is_1(self):
        with self.assertRaises(SrError) as ctx:
            render_review.write_review(self.root, self.judge, "sam", run_id=RUN_P1,
                                       template=str(TEMPLATE_RU), now=NOW)
        self.assertEqual(ctx.exception.code, 1)

    def test_no_live_run_is_1(self):
        with self.assertRaises(SrError) as ctx:
            render_review.write_review(self.root, self.judge, "kim", template=str(TEMPLATE_RU), now=NOW)
        self.assertEqual(ctx.exception.code, 1)

    def test_missing_judge_file_is_2(self):
        with self.assertRaises(SrError) as ctx:
            render_review.write_review(self.root, self.root / "nope.json", "alex",
                                       template=str(TEMPLATE_RU), now=NOW)
        self.assertEqual(ctx.exception.code, 2)

    def test_rerender_overwrites(self):
        first = self.write_review().read_text(encoding="utf-8")
        second = self.write_review().read_text(encoding="utf-8")
        self.assertEqual(first, second)

    def test_no_user_paths_in_file(self):
        text = self.write_review().read_text(encoding="utf-8")
        self.assertNotIn(str(self.root), text)

    def test_cli_render_review(self):
        code, out, err = self.cli("render-review", str(self.judge), "--person", "alex",
                                  "--template", str(TEMPLATE_RU))
        self.assertEqual(code, 0, err)
        self.assertIn(f"{RUN_P1}.md", out)
        self.assertTrue((self.paths["reviews"] / "alex" / f"{RUN_P1}.md").is_file())

    def test_cli_render_review_with_run_and_bad_template_name(self):
        path = self.paths["people"] / "alex.yaml"
        path.write_text(path.read_text(encoding="utf-8").replace("feedback:\n", "feedback:\n  template: ../x\n"),
                        encoding="utf-8")
        code, _, err = self.cli("render-review", str(self.judge), "--person", "alex", "--run", RUN_P1)
        self.assertEqual(code, 2, err)

    def test_cli_violation_code_1(self):
        path = self.bad_judge("opening", "Ты работаешь лучше, чем другие.")
        code, _, err = self.cli("render-review", str(path), "--person", "alex", "--template", str(TEMPLATE_RU))
        self.assertEqual(code, 1, err)
        self.assertIn("чем другие", err)
