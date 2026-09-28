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


class CollectorCase(unittest.TestCase):
    def setUp(self) -> None:
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
        code, results = collector_check.check_files(
            [self.put_raw(self.single("ок, мержим", 3, str(log)), outside)])
        self.assertEqual(code, 1)
        self.assertTrue(any("ход 2" in p for p in results[0]["problems"]), results)

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


if __name__ == "__main__":
    unittest.main()
