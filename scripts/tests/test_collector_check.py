"""`lib/collector_check.py` и команда `check-collector`: выход коллектора до передачи судье —
поля оценки на любой глубине, схема `collector`, смысловые сходимости, коды по файлам."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _util  # noqa: F401 — кладёт scripts/ в sys.path
from _assess import ASSESS

import sr
from lib import collector_check, schema
from lib.workspace import SrError

AXES = ["verification", "task_framing", "ownership", "self_correction", "context_engineering",
        "decomposition", "trust_calibration", "reversibility"]


def batch() -> dict:
    return json.loads((ASSESS / "batch-ok.json").read_text(encoding="utf-8"))


QUOTE_BLOCKS = ("observations", "antipattern_candidates", "gating_candidates", "rec_signals",
                "outcome_signals")


def heading_log(turns: list[tuple[str, str]]) -> str:
    """Синтетический лог формата md-headings: `## User` / `## Assistant` по ходу."""
    parts = ["# Экспорт сессии", ""]
    for role, text in turns:
        parts += [f"## {'User' if role == 'user' else 'Assistant'}", "", text, ""]
    return "\n".join(parts)


def batch_logs(obj: dict) -> dict[str, str]:
    """Логи под цитаты пачки: по сессии из `sessions[]`, на которую ссылаются цитаты, лог из
    `turns_total` ходов; процитированные ходы — реплики человека с текстом цитат, прочие — агент."""
    quoted: dict[str, dict[int, list[str]]] = {}
    for block in QUOTE_BLOCKS:
        for item in obj.get(block) or []:
            if item.get("turn_index") is None:
                continue
            texts = quoted.setdefault(item["session_id"], {}).setdefault(item["turn_index"], [])
            if item["quote"] not in texts:
                texts.append(item["quote"])
    logs = {}
    for s in obj["sessions"]:
        if s["session_id"] not in quoted:
            continue
        at = quoted[s["session_id"]]
        turns = [("user", "\n\n".join(at[i])) if i in at else ("assistant", f"ответ агента на ходу {i}")
                 for i in range(s["turns_total"])]
        logs[s["path"]] = heading_log(turns)
    return logs


def make_state(root: Path) -> Path:
    """Минимальный workspace: `<root>/.sessionretro/config.json`; вернуть папку состояния."""
    state = root / ".sessionretro"
    state.mkdir(parents=True, exist_ok=True)
    (state / "config.json").write_text("{}", encoding="utf-8")
    return state


class CollectorCase(_util.TempHome, unittest.TestCase):
    """HOME временный: `sr.main` сдвигает глобальную отметку `plugin-state.json`."""

    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="sr-collector-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def problems(self, obj) -> list[str]:
        return collector_check.check_batch(obj)

    def assertProblem(self, obj, needle: str) -> None:
        found = self.problems(obj)
        self.assertTrue(any(needle in p for p in found), f"нет «{needle}» в {found}")

    def put(self, name: str, obj) -> str:
        """Файл пачки в `runs/pending/alex/` workspace; логи под её цитаты — рядом, по
        относительным путям `sessions[].path`, как их кладёт сбор."""
        state = make_state(self.dir)
        if isinstance(obj, dict) and isinstance(obj.get("sessions"), list):
            for rel, text in batch_logs(obj).items():
                log = state / rel
                log.parent.mkdir(parents=True, exist_ok=True)
                log.write_text(text, encoding="utf-8")
        path = state / "runs" / "pending" / "alex" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
        path.write_text(text, encoding="utf-8")
        return str(path)


class CheckBatchTest(CollectorCase):
    def test_valid_batch_passes(self):
        self.assertEqual(self.problems(batch()), [])

    def test_forbidden_set_is_exact(self):
        self.assertEqual(collector_check.FORBIDDEN, {
            "score", "scores", "axes", "overall_level", "level", "feedback_draft", "feedback_parts",
            "recommendations", "insufficient_evidence", "gating_triggered", "verdict"})

    def test_each_forbidden_key_rejected_deep_inside(self):
        for key in sorted(collector_check.FORBIDDEN):
            with self.subTest(key=key):
                obj = batch()
                obj["observations"][1][key] = 3
                found = self.problems(obj)
                self.assertTrue(any(f"$.observations[1].{key}" in p and "не ставит" in p for p in found),
                                found)

    def test_forbidden_key_on_top_level_rejected(self):
        obj = batch()
        obj["axes"] = [{"id": "verification", "score": 2}]
        found = self.problems(obj)
        self.assertTrue(any(p.startswith("$.axes:") for p in found), found)
        self.assertTrue(any("$.axes[0].score" in p for p in found), found)

    def test_observation_without_session_id(self):
        obj = batch()
        del obj["observations"][0]["session_id"]
        self.assertProblem(obj, "session_id")

    def test_observation_with_null_session_id(self):
        obj = batch()
        obj["observations"][2]["session_id"] = None
        self.assertProblem(obj, "$.observations[2]: наблюдение без session_id")

    def test_session_status_reviewed_rejected(self):
        obj = batch()
        obj["sessions"][0]["status"] = "reviewed"
        self.assertProblem(obj, "$.sessions[0].status")

    def test_axes_unobserved_required(self):
        obj = batch()
        del obj["axes_unobserved"]
        self.assertProblem(obj, "axes_unobserved")

    def test_axis_not_covered(self):
        obj = batch()
        obj["observations"] = [o for o in obj["observations"] if o["axis"] != "ownership"]
        self.assertProblem(obj, "не покрыты")
        self.assertProblem(obj, "ownership")

    def test_axis_in_both_lists(self):
        obj = batch()
        obj["axes_unobserved"].append({"axis": "verification", "reason": "behaviour_absent", "note": None})
        self.assertProblem(obj, "одновременно")

    def test_axis_listed_twice(self):
        obj = batch()
        obj["axes_unobserved"].append({"axis": "context_engineering", "reason": "short_log", "note": None})
        self.assertProblem(obj, "дважды")

    def test_index_greater_than_total(self):
        obj = batch()
        obj["batch"]["index"] = 3
        obj["batch"]["total"] = 2
        self.assertProblem(obj, "index 3 больше total 2")

    def test_old_of_field_rejected_by_real_schema(self):
        obj = batch()
        obj["batch"]["of"] = obj["batch"].pop("total")
        self.assertProblem(obj, "total")
        self.assertProblem(obj, "'of'")

    def test_period_is_object_with_anchor(self):
        obj = batch()
        obj["batch"]["period"] = "p1"
        self.assertProblem(obj, "$.batch.period")

    def test_success_with_ok_false(self):
        obj = batch()
        obj["status"]["ok"] = False
        self.assertProblem(obj, "не сходится")

    def test_failure_code_with_ok_true(self):
        obj = batch()
        obj["status"]["code"] = "empty_transcript"
        self.assertProblem(obj, "не сходится")

    def test_unobserved_without_reason(self):
        obj = batch()
        del obj["axes_unobserved"][0]["reason"]
        self.assertProblem(obj, "reason")

    def test_warnings_codes_or_check_lines_only(self):
        """A12: в `metadata.warnings` — коды коллектора или строки `check-collector: …`."""
        obj = batch()
        obj["metadata"]["warnings"] = ["sessions_excluded", "check-collector: цитата перенесена: x"]
        self.assertEqual(self.problems(obj), [])
        for bad in ("что-то ещё", "check-collector:", "sessions_excluded и ещё"):
            with self.subTest(bad=bad):
                obj["metadata"]["warnings"] = [bad]
                self.assertProblem(obj, "$.metadata.warnings[0]")

    def test_stub_rejected(self):
        found = self.problems({"stub": True})
        self.assertEqual(len(found), 1)
        self.assertIn("стаб не заполнен", found[0])

    def test_not_an_object(self):
        self.assertProblem([1, 2], "не объект")

    def test_wrong_block_types_do_not_crash(self):
        obj = batch()
        obj["observations"] = {"axis": "verification"}
        obj["batch"] = [1]
        found = self.problems(obj)
        self.assertTrue(any("$.observations" in p for p in found), found)
        self.assertTrue(any("$.batch" in p for p in found), found)

    def test_missing_schema_is_error_1_with_path(self):
        missing = self.dir / "nope" / "collector.json"
        real = schema.schema_path
        with mock.patch.object(schema, "schema_path",
                               side_effect=lambda name: missing if name == "collector" else real(name)):
            with self.assertRaises(SrError) as ctx:
                self.problems(batch())
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn(str(missing), ctx.exception.message)


class CheckFilesTest(CollectorCase):
    def test_all_valid_is_0(self):
        code, results = collector_check.check_files([self.put("b1.json", batch()), self.put("b2.json", batch())])
        self.assertEqual(code, 0)
        self.assertEqual([r["problems"] for r in results], [[], []])

    def test_one_bad_is_1(self):
        bad = batch()
        bad["observations"][0]["score"] = 2
        code, results = collector_check.check_files([self.put("ok.json", batch()), self.put("bad.json", bad)])
        self.assertEqual(code, 1)
        self.assertEqual(results[0]["problems"], [])
        self.assertTrue(results[1]["problems"])
        self.assertTrue(results[1]["path"].endswith("bad.json"))

    def test_only_missing_is_2(self):
        code, results = collector_check.check_files([str(self.dir / "a.json"), str(self.dir / "b.json")])
        self.assertEqual(code, 2)
        self.assertEqual([r["problems"] for r in results], [["файла нет"], ["файла нет"]])

    def test_missing_and_bad_is_1(self):
        bad = batch()
        bad["level"] = "competent"
        code, _ = collector_check.check_files([str(self.dir / "a.json"), self.put("bad.json", bad)])
        self.assertEqual(code, 1)

    def test_broken_json_is_1(self):
        code, results = collector_check.check_files([self.put("broken.json", "{oops")])
        self.assertEqual(code, 1)
        self.assertIn("не разбирается", results[0]["problems"][0])

    def test_problems_capped_with_total(self):
        bad = batch()
        for obs in bad["observations"]:
            obs["score"] = 1
            obs["verdict"] = "x"
            obs["level"] = "x"
            obs["scores"] = []
        code, results = collector_check.check_files([self.put("bad.json", bad)])
        self.assertEqual(code, 1)
        self.assertLessEqual(len(results[0]["problems"]), collector_check.MAX_PROBLEMS)
        self.assertGreater(results[0]["problems_total"], collector_check.MAX_PROBLEMS)


class CheckCollectorCliTest(CollectorCase):
    def cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sr.main(["check-collector", *args])
        return code, out.getvalue(), err.getvalue()

    def test_cli_ok_prints_json(self):
        code, out, _ = self.cli(self.put("b1.json", batch()))
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["checked"], 1)
        self.assertEqual(report["with_problems"], 0)
        self.assertEqual(report["batches"][0]["index"], 1)
        self.assertEqual(report["batches"][0]["total"], 1)

    def test_cli_bad_is_1(self):
        bad = copy.deepcopy(batch())
        bad["feedback_draft"] = "текст"
        code, out, _ = self.cli(self.put("bad.json", bad))
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["with_problems"], 1)

    def test_cli_missing_is_2(self):
        code, out, _ = self.cli(str(self.dir / "none.json"))
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["missing_files"], 1)


SID = "s-quotes"
# 0 человек · 1 агент · 2 человек · 3 агент · 4 вставка харнесса · 5 человек
ROLE_LOG = [
    ("user", "сделай форму входа: email и пароль, без новых зависимостей"),
    ("assistant", "Готово, форма на месте.\n\nПроверьте вручную вход с пустым паролем, я не запускал."),
    ("user", "ок, мержим"),
    ("assistant", "Смержил в main, ветку удалил."),
    ("user", "<system-reminder>Напоминание харнесса про задачи.</system-reminder>"),
    ("user", "стоп, откатываем, пробуем через адаптер"),
]


def role_doc(turns=ROLE_LOG) -> dict:
    from lib import normalize
    return normalize.parse(heading_log(turns))


def quoted(quote: str, turn: int | None, *, block: str = "observations", sid: str | None = SID) -> dict:
    """Пачка из фикстуры, где в блоке `block` ровно одна запись с этой цитатой."""
    obj = batch()
    for name in QUOTE_BLOCKS:
        obj[name] = []
    item = {"session_id": sid, "turn_index": turn, "quote": quote}
    if block == "observations":
        item.update(axis="verification", polarity="negative", reading="приёмка без проверки")
    elif block == "rec_signals":
        item.update(rec_id="REC-alex-001", direction="contradicts")
    elif block == "gating_candidates":
        item.update(trigger="G1")
    elif block == "outcome_signals":
        item.update(direction="done")
    elif block == "antipattern_candidates":
        item.update(id="blind_accept")
    obj[block] = [item]
    observed = {"verification"} if block == "observations" else set()
    obj["axes_unobserved"] = [{"axis": a, "reason": "behaviour_absent", "note": None}
                              for a in AXES if a not in observed]
    return obj


class QuoteAttributionTest(unittest.TestCase):
    """Регрессия A08: цитата коллектора сверяется с разметкой ролей нормализованного лога —
    текст агента под видом реплики человека и сдвиг номера хода ловятся до судьи."""

    def attribution(self, obj, docs=None) -> list[str]:
        return collector_check.attribution_problems(obj, {SID: role_doc()} if docs is None else docs)

    def assertOne(self, found: list[str], *needles: str) -> None:
        self.assertEqual(len(found), 1, found)
        for needle in needles:
            self.assertIn(needle, found[0])

    def test_human_quote_at_its_turn_passes(self):
        self.assertEqual(self.attribution(quoted("без новых зависимостей", 0)), [])

    def test_agent_text_cited_as_human_rejected(self):
        found = self.attribution(quoted("Проверьте вручную вход с пустым паролем", 1))
        self.assertOne(found, "$.observations[0]", SID, "ход 1", "текст агента")

    def test_agent_text_under_human_turn_index_rejected(self):
        found = self.attribution(quoted("Проверьте вручную вход с пустым паролем", 2))
        self.assertOne(found, "$.observations[0]", "ход 2", "текст агента", "ход 1")

    def test_turn_shifted_to_agent_turn_rejected(self):
        found = self.attribution(quoted("ок, мержим", 3))
        self.assertOne(found, "ход 3", "реплика человека", "ход 2")

    def test_turn_shifted_between_human_turns_rejected(self):
        found = self.attribution(quoted("стоп, откатываем", 2))
        self.assertOne(found, "ход 2", "ход 5")

    def test_harness_insertion_rejected(self):
        found = self.attribution(quoted("Напоминание харнесса про задачи", 4))
        self.assertOne(found, "ход 4", "вставка харнесса", "system_injection")

    def test_quote_absent_from_log_rejected(self):
        found = self.attribution(quoted("этого текста в логе нет", 2))
        self.assertOne(found, "ход 2", "не найдена")

    def test_turn_beyond_log_rejected(self):
        found = self.attribution(quoted("ок, мержим", 40))
        self.assertOne(found, "ход 40", "нет в логе", "ход 2")

    def test_every_quote_block_checked(self):
        for block in QUOTE_BLOCKS:
            with self.subTest(block=block):
                found = self.attribution(quoted("Смержил в main", 3, block=block))
                self.assertOne(found, f"$.{block}[0]", "текст агента")

    def test_null_turn_without_session_is_context_report_number(self):
        self.assertEqual(self.attribution(quoted("пик окна 81%", None, sid=None)), [])

    def test_whitespace_case_and_markup_tolerated(self):
        self.assertEqual(self.attribution(quoted("  Сделай   форму входа:\nEMAIL и **пароль**", 0)), [])

    def test_ellipsis_joined_fragments_of_one_turn_pass(self):
        self.assertEqual(self.attribution(quoted("сделай форму входа … без новых зависимостей", 0)), [])

    def test_fragment_from_agent_turn_rejected(self):
        found = self.attribution(quoted("ок, мержим … Смержил в main", 2))
        self.assertOne(found, "ход 2")

    def test_session_without_log_rejected(self):
        found = self.attribution(quoted("ок, мержим", 2, sid="s-other"))
        self.assertOne(found, "s-other")


class RealLogAttributionTest(unittest.TestCase):
    """Разметка ролей настоящего парсера на фикстуре нативного JSONL Claude Code."""

    def setUp(self) -> None:
        from _util import FIXTURES
        from lib import normalize
        self.doc = normalize.parse_file(FIXTURES / "logs" / "claude-main.jsonl")
        self.sid = self.doc["session_id"]

    def check(self, quote: str, turn: int) -> list[str]:
        return collector_check.attribution_problems(quoted(quote, turn, sid=self.sid), {self.sid: self.doc})

    def test_roles_of_real_parser(self):
        self.assertEqual(self.check("Добавь функцию сложения в calc.py", 0), [])
        self.assertEqual(self.check("pytest -q", 1), [], "команда инструмента — факт лога")
        self.assertEqual(self.check("1 passed", 1), [], "вывод инструмента — факт лога")
        self.assertIn("текст агента", " ".join(self.check("функция и тест добавлены", 1)))
        self.assertIn("вставка харнесса", " ".join(self.check("/review", 4)))
        self.assertIn("текст агента", " ".join(self.check("find tests", 16)), "промпт субагента")


class CheckFilesAttributionTest(CollectorCase):
    def write_log(self, rel: str, turns=ROLE_LOG) -> Path:
        path = make_state(self.dir) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(heading_log(turns), encoding="utf-8")
        return path

    def single(self, quote: str, turn: int, path: str) -> dict:
        obj = quoted(quote, turn)
        obj["sessions"] = [{"session_id": SID, "path": path, "turns_total": 6, "user_turns": 3,
                            "reviewed_from_turn": None, "state": "new", "status": "read",
                            "status_note": None}]
        return obj

    def put_raw(self, obj: dict, where: Path) -> str:
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        return str(where)

    def batch_file(self, obj: dict) -> str:
        return self.put_raw(obj, make_state(self.dir) / "runs" / "pending" / "alex" / "batch-01.json")

    def test_agent_quote_fails_check_files(self):
        self.write_log("logs/alex/s.md")
        code, results = collector_check.check_files(
            [self.batch_file(self.single("Проверьте вручную вход с пустым паролем", 1, "logs/alex/s.md"))])
        self.assertEqual(code, 1)
        self.assertTrue(any("текст агента" in p and SID in p for p in results[0]["problems"]), results)

    def test_human_quote_passes_check_files(self):
        self.write_log("logs/alex/s.md")
        code, results = collector_check.check_files(
            [self.batch_file(self.single("ок, мержим", 2, "logs/alex/s.md"))])
        self.assertEqual((code, results[0]["problems"]), (0, []))

    def test_absolute_path_needs_no_workspace(self):
        log = self.write_log("logs/alex/s.md")
        outside = self.dir / "elsewhere" / "batch-01.json"
        path = self.put_raw(self.single("ок, мержим", 3, str(log)), outside)
        code, results = collector_check.check_files([path])
        # лог найден по абсолютному пути: сдвиг хода 3 → 2 восстановлен по нему (A12)
        self.assertEqual((code, results[0]["problems"]), (0, []))
        self.assertTrue(any("ход 3 → 2" in ln for ln in results[0]["repaired"]), results)
        self.assertEqual(json.loads(Path(path).read_text(encoding="utf-8"))["observations"][0]["turn_index"], 2)

    def test_missing_log_is_named(self):
        make_state(self.dir)
        code, results = collector_check.check_files(
            [self.batch_file(self.single("ок, мержим", 2, "logs/alex/none.md"))])
        self.assertEqual(code, 1)
        self.assertTrue(any("none.md" in p and "не сверить" in p for p in results[0]["problems"]), results)

    def test_relative_path_without_workspace_is_named(self):
        outside = self.dir / "elsewhere" / "batch-01.json"
        code, results = collector_check.check_files(
            [self.put_raw(self.single("ок, мержим", 2, "logs/alex/s.md"), outside)])
        self.assertEqual(code, 1)
        self.assertTrue(any("workspace" in p and "logs/alex/s.md" in p for p in results[0]["problems"]),
                        results)

    def test_cli_reports_agent_quote(self):
        self.write_log("logs/alex/s.md")
        path = self.batch_file(self.single("Смержил в main", 3, "logs/alex/s.md"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = sr.main(["check-collector", path])
        self.assertEqual(code, 1)
        self.assertIn("текст агента", out.getvalue())


# ── A12: ремонт адреса и склейки, отбрасывание неремонтируемых ──────────────────
# Длинная многотемная реплика человека (ход 13), на которой коллектор склеивает куски без «…»
# и сдвигает номер хода. 0..14: чётные — человек (кроме 12, 14), нечётные — агент.
LONG = ("смотри, тут несколько вещей сразу. во-первых, форма входа падает на пустом пароле, "
        "и я хочу, чтобы ты сначала написал тест, который это воспроизводит. во-вторых, "
        "не трогай конфиг сборки, там всё настроено под старый сервер. в-третьих, после правки "
        "запусти весь набор тестов и покажи мне вывод целиком, а не пересказ.")
AGENT_12 = "Посмотрел логи сервера: ошибок авторизации нет, есть таймауты базы."
LONG_LOG = [
    ("user", "сделай форму входа: email и пароль, без новых зависимостей"),
    ("assistant", "Готово, форма на месте."),
    ("user", "покажи, что поменялось"),
    ("assistant", "Поменялись два файла: форма и её стили."),
    ("user", "дальше"),
    ("assistant", "Добавил валидацию email."),
    ("user", "проверь ещё мобильную вёрстку"),
    ("assistant", "На узком экране кнопка уезжает, поправил отступы."),
    ("user", "ок, мержим"),
    ("assistant", "Смержил в main."),
    ("user", "ок, мержим"),
    ("user", "хорошо, теперь посмотри на логи сервера за вчера"),
    ("assistant", AGENT_12),
    ("user", LONG),
    ("assistant", "Принял, начинаю с теста на пустой пароль."),
]
PIECE_1 = "сначала написал тест, который это воспроизводит."
PIECE_2 = "запусти весь набор тестов и покажи мне вывод целиком"
GLUED = f"{PIECE_1} {PIECE_2}"          # два куска одной реплики без «…»


def long_doc() -> dict:
    return role_doc(LONG_LOG)


class QuoteRepairTest(unittest.TestCase):
    """`repair_quotes`: адрес и склейку, которые однозначно восстанавливаются по логу, скрипт
    чинит сам со строкой в `metadata.warnings`; текст агента и неоднозначность не чинятся."""

    def repair(self, obj) -> tuple[list[str], list[str]]:
        docs = {SID: long_doc()}
        lines = collector_check.repair_quotes(obj, docs)
        return lines, collector_check.attribution_problems(obj, docs)

    def test_glued_pieces_rewritten_with_ellipsis(self):
        obj = quoted(GLUED, 13)
        lines, problems = self.repair(obj)
        self.assertEqual(problems, [])
        self.assertEqual(obj["observations"][0]["quote"], f"{PIECE_1} … {PIECE_2}")
        self.assertEqual(obj["observations"][0]["turn_index"], 13)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("«…»", lines[0])
        self.assertIn("$.observations[0]", lines[0])
        self.assertIn(SID, lines[0])

    def test_turn_shifted_by_two_readdressed(self):
        obj = quoted("не трогай конфиг сборки, там всё настроено под старый сервер", 11)
        lines, problems = self.repair(obj)
        self.assertEqual(problems, [])
        self.assertEqual(obj["observations"][0]["turn_index"], 13)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("ход 11 → 13", lines[0])
        self.assertIn("перенесена", lines[0])

    def test_glued_and_shifted_both_repaired(self):
        obj = quoted(GLUED, 11)
        lines, problems = self.repair(obj)
        self.assertEqual(problems, [])
        self.assertEqual((obj["observations"][0]["turn_index"], obj["observations"][0]["quote"]),
                         (13, f"{PIECE_1} … {PIECE_2}"))
        self.assertIn("ход 11 → 13", " ".join(lines))

    def test_every_quote_block_repaired(self):
        for block in QUOTE_BLOCKS:
            with self.subTest(block=block):
                obj = quoted(GLUED, 11, block=block)
                lines, problems = self.repair(obj)
                self.assertEqual(problems, [])
                self.assertEqual(obj[block][0]["turn_index"], 13)
                self.assertIn(f"$.{block}[0]", lines[0])

    def test_piece_from_agent_text_rejected(self):
        obj = quoted(f"{PIECE_2} {AGENT_12}", 13)
        lines, problems = self.repair(obj)
        self.assertEqual(lines, [])
        self.assertEqual(obj["observations"][0]["quote"], f"{PIECE_2} {AGENT_12}")
        self.assertEqual(len(problems), 1, problems)

    def test_agent_text_never_readdressed(self):
        obj = quoted(AGENT_12, 13)
        lines, problems = self.repair(obj)
        self.assertEqual((lines, obj["observations"][0]["turn_index"]), ([], 13))
        self.assertIn("текст агента", problems[0])

    def test_short_pieces_rejected(self):
        """Минимум куска — 3 слова и 15 знаков: из двухсловных обрывков длинной реплики можно
        собрать фразу, которой человек не говорил («тут несколько» + «форма входа падает»)."""
        self.assertEqual((collector_check.MIN_PIECE_WORDS, collector_check.MIN_PIECE_CHARS), (3, 15))
        for quote in ("тут несколько форма входа падает", "форма входа падает на пустом пароле тест"):
            with self.subTest(quote=quote):
                obj = quoted(quote, 13)
                lines, problems = self.repair(obj)
                self.assertEqual(lines, [])
                self.assertEqual(obj["observations"][0]["quote"], quote)
                self.assertEqual(len(problems), 1, problems)

    def test_pieces_out_of_order_rejected(self):
        obj = quoted(f"{PIECE_2} {PIECE_1}", 13)
        lines, problems = self.repair(obj)
        self.assertEqual(lines, [])
        self.assertEqual(len(problems), 1, problems)

    def test_ambiguous_turn_not_readdressed(self):
        """«ок, мержим» — в ходах 8 и 10: какой из них имел в виду коллектор, по логу не решить."""
        obj = quoted("ок, мержим", 9)
        lines, problems = self.repair(obj)
        self.assertEqual((lines, obj["observations"][0]["turn_index"]), ([], 9))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("ход 8", problems[0])
        self.assertIn("ход 10", problems[0])

    def test_harness_text_not_readdressed(self):
        obj = quoted("Напоминание харнесса про задачи", 2)
        lines = collector_check.repair_quotes(obj, {SID: role_doc()})
        self.assertEqual((lines, obj["observations"][0]["turn_index"]), ([], 2))

    def test_valid_quote_untouched(self):
        obj = quoted(f"{PIECE_1} … {PIECE_2}", 13)
        self.assertEqual(self.repair(obj), ([], []))

    def test_tool_output_readdressed(self):
        from _util import FIXTURES
        from lib import normalize
        doc = normalize.parse_file(FIXTURES / "logs" / "claude-main.jsonl")
        obj = quoted("1 passed", 0, sid=doc["session_id"])
        lines = collector_check.repair_quotes(obj, {doc["session_id"]: doc})
        self.assertEqual(obj["observations"][0]["turn_index"], 1, lines)
        self.assertIn("ход 0 → 1", lines[0])


def two_axes() -> dict:
    """verification — верная цитата хода 13; ownership — текст агента хода 12 (не чинится)."""
    obj = quoted(f"{PIECE_1} … {PIECE_2}", 13)
    obj["observations"].append({"axis": "ownership", "polarity": "negative", "session_id": SID,
                                "turn_index": 12, "quote": AGENT_12, "reading": "x"})
    obj["axes_unobserved"] = [u for u in obj["axes_unobserved"] if u["axis"] != "ownership"]
    return obj


class DropInvalidTest(unittest.TestCase):
    """`drop_unverified` (`--drop-invalid`): после исчерпания перезапусков неремонтируемые
    записи убираются явно, ось без наблюдений уходит в `axes_unobserved` с `quote_unverified`."""

    def test_unrepairable_dropped_and_axis_unobserved(self):
        obj = two_axes()
        docs = {SID: long_doc()}
        lines = collector_check.drop_unverified(obj, docs)
        self.assertEqual([o["axis"] for o in obj["observations"]], ["verification"])
        moved = [u for u in obj["axes_unobserved"] if u["axis"] == "ownership"]
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0]["reason"], "quote_unverified")
        self.assertTrue(any("отброшено" in ln and "$.observations[1]" in ln and "ход 12" in ln
                            for ln in lines), lines)
        self.assertTrue(any("ownership" in ln and "quote_unverified" in ln for ln in lines), lines)
        self.assertEqual(collector_check.attribution_problems(obj, docs), [])
        self.assertEqual(collector_check.sense_problems(obj), [])


class RepairFilesTest(CollectorCase):
    """`check_files` чинит по умолчанию и пишет файл атомарно; `--drop-invalid` отбрасывает."""

    def batch_file(self, obj: dict) -> str:
        state = make_state(self.dir)
        log = state / "logs" / "alex" / "long.md"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(heading_log(LONG_LOG), encoding="utf-8")
        obj["sessions"] = [{"session_id": SID, "path": "logs/alex/long.md", "turns_total": len(LONG_LOG),
                            "user_turns": 9, "reviewed_from_turn": None, "state": "new", "status": "read",
                            "status_note": None}]
        path = state / "runs" / "pending" / "alex" / "batch-01.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def reread(self, path: str) -> dict:
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def test_repair_written_and_schema_clean(self):
        path = self.batch_file(quoted(GLUED, 11))
        code, results = collector_check.check_files([path])
        self.assertEqual((code, results[0]["problems"]), (0, []))
        saved = self.reread(path)
        self.assertEqual((saved["observations"][0]["turn_index"], saved["observations"][0]["quote"]),
                         (13, f"{PIECE_1} … {PIECE_2}"))
        warns = saved["metadata"]["warnings"]
        self.assertTrue(any(w.startswith("check-collector: ") and "ход 11 → 13" in w for w in warns), warns)
        self.assertEqual(collector_check.check_batch(saved, make_state(self.dir), quotes=True), [])
        self.assertTrue(results[0]["written"])
        self.assertEqual(len(results[0]["repaired"]), 2, results[0]["repaired"])
        # второй проход ничего не чинит и файл не трогает
        code, again = collector_check.check_files([path])
        self.assertEqual((code, again[0]["repaired"], again[0]["written"]), (0, [], False))

    def test_unrepairable_without_drop_is_1_and_file_kept(self):
        obj = quoted(AGENT_12, 13)
        path = self.batch_file(obj)
        before = Path(path).read_text(encoding="utf-8")
        code, results = collector_check.check_files([path])
        self.assertEqual(code, 1)
        self.assertTrue(any("текст агента" in p for p in results[0]["problems"]), results)
        self.assertEqual(Path(path).read_text(encoding="utf-8"), before)

    def test_drop_invalid_removes_record_and_passes(self):
        obj = two_axes()
        path = self.batch_file(obj)
        code, results = collector_check.check_files([path], drop_invalid=True)
        self.assertEqual((code, results[0]["problems"]), (0, []))
        saved = self.reread(path)
        self.assertEqual([o["axis"] for o in saved["observations"]], ["verification"])
        self.assertIn({"axis": "ownership", "reason": "quote_unverified"},
                      [{k: u[k] for k in ("axis", "reason")} for u in saved["axes_unobserved"]])
        self.assertTrue(any("отброшено" in w for w in saved["metadata"]["warnings"]))
        self.assertEqual(len(results[0]["dropped"]), 2, results[0]["dropped"])
        self.assertEqual(schema.validate(saved, "collector"), [])

    def test_drop_leaving_no_observations_fails_batch(self):
        path = self.batch_file(quoted(AGENT_12, 13))
        before = Path(path).read_text(encoding="utf-8")
        code, results = collector_check.check_files([path], drop_invalid=True)
        self.assertEqual(code, 1)
        self.assertTrue(any("не осталось ни одного наблюдения" in p for p in results[0]["problems"]),
                        results)
        self.assertEqual(Path(path).read_text(encoding="utf-8"), before)

    def test_missing_log_never_dropped(self):
        obj = quoted("ок, мержим", 8)
        path = self.batch_file(obj)
        saved = self.reread(path)
        saved["sessions"][0]["path"] = "logs/alex/none.md"
        Path(path).write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
        code, results = collector_check.check_files([path], drop_invalid=True)
        self.assertEqual(code, 1)
        self.assertEqual(results[0]["dropped"], [])
        self.assertTrue(any("none.md" in p for p in results[0]["problems"]), results)

    def cli(self, *args: str) -> tuple[int, dict]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = sr.main(["check-collector", *args])
        return code, json.loads(out.getvalue())

    def test_cli_names_repair(self):
        code, report = self.cli(self.batch_file(quoted(GLUED, 11)))
        self.assertEqual(code, 0)
        self.assertEqual(report["repaired"], 2)
        self.assertTrue(any("ход 11 → 13" in ln for ln in report["batches"][0]["repaired"]), report)

    def test_cli_drop_invalid_flag(self):
        code, report = self.cli("--drop-invalid", self.batch_file(two_axes()))
        self.assertEqual(code, 0)
        self.assertEqual(report["dropped"], 2)
        self.assertTrue(any("отброшено" in ln for ln in report["batches"][0]["dropped"]), report)


if __name__ == "__main__":
    unittest.main()
