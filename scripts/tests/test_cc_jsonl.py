"""Парсер нативного JSONL Claude Code: люди и вставки, склейка ответов по message.id без задвоения
usage, сабагенты, компакты, пропуски, компактный снапшот."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

import _util  # noqa: F401 — кладёт scripts/ в sys.path
from _util import SCRIPTS, run_sr

from lib import cc_jsonl, normalize
from lib.workspace import SrError

F = _util.FIXTURES / "logs"
MAIN = F / "claude-main.jsonl"
COMPACT = F / "claude-compact.jsonl"
SID = "aaaaaaaa-0000-0000-0000-000000000001"


def read(name: str) -> str:
    return (F / name).read_text(encoding="utf-8")


def line(**kw) -> str:
    return json.dumps(kw, ensure_ascii=False)


def human(text: str, uuid: str, ts: str = "2026-09-20T11:00:00.000Z") -> str:
    return line(type="user", uuid=uuid, sessionId=SID, isSidechain=False, timestamp=ts,
                origin={"kind": "human"}, message={"role": "user", "content": text})


def said(text: str, uuid: str, ts: str, mid: str | None = None, **extra) -> str:
    """Строка ответа агента с одним текстовым блоком."""
    return line(type="assistant", uuid=uuid, sessionId=SID, timestamp=ts, **extra,
                message={"id": mid or f"msg-{uuid}", "model": "m", "role": "assistant",
                         "content": [{"type": "text", "text": text}]})


def called(name: str, tool_id: str, uuid: str, ts: str, **extra) -> str:
    """Строка ответа агента с вызовом инструмента."""
    return line(type="assistant", uuid=uuid, sessionId=SID, timestamp=ts, **extra,
                message={"id": f"msg-{uuid}", "model": "m", "role": "assistant",
                         "content": [{"type": "tool_use", "id": tool_id, "name": name,
                                      "input": {"command": "ls"}}]})


def result(tool_id: str, uuid: str, ts: str, out: str = "ok", **extra) -> str:
    """Строка ответа инструмента."""
    return line(type="user", uuid=uuid, sessionId=SID, timestamp=ts, **extra,
                message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id,
                                                      "content": out}]})


def view(turns: list[dict]) -> list[tuple]:
    return [(t["index"], t["role"], t["text"], t["sidechain"]) for t in turns]


def copy_main(tmp: Path) -> Path:
    """Копия главной фикстуры с папкой сабагентов во временной папке."""
    shutil.copy(MAIN, tmp / MAIN.name)
    shutil.copytree(F / "claude-main", tmp / "claude-main")
    return tmp / MAIN.name


def snapshot_doc(main: Path) -> dict:
    text, _, _ = cc_jsonl.compact_snapshot(main, lambda o: (o, {}))
    return cc_jsonl.parse(text)


class Detect(unittest.TestCase):
    def test_main_fixture_starts_without_session_id(self):
        first = json.loads(read("claude-main.jsonl").splitlines()[0])
        self.assertNotIn("sessionId", first)
        self.assertTrue(cc_jsonl.looks_like(read("claude-main.jsonl")))

    def test_detect_format(self):
        for name in ("claude-main.jsonl", "claude-compact.jsonl"):
            with self.subTest(fixture=name):
                self.assertEqual(normalize.detect_format(read(name)), "claude-jsonl")

    def test_registered_first(self):
        self.assertEqual(normalize.FORMATS[0][0], "claude-jsonl")
        self.assertIn("claude-jsonl", normalize.format_names())

    def test_other_fixtures_not_taken(self):
        for p in sorted(F.iterdir()):
            if p.is_file() and p.suffix != ".jsonl":
                with self.subTest(fixture=p.name):
                    self.assertFalse(cc_jsonl.looks_like(p.read_text(encoding="utf-8")))
                    self.assertNotEqual(normalize.detect_format(p.read_text(encoding="utf-8")),
                                        "claude-jsonl")

    def test_first_line_must_have_type(self):
        text = line(uuid="u1", sessionId=SID) + "\n" + human("привет", "u2")
        self.assertFalse(cc_jsonl.looks_like(text))

    def test_needs_session_id_or_uuid(self):
        self.assertFalse(cc_jsonl.looks_like(line(type="mode", mode="x") + "\n" + line(type="x")))
        self.assertTrue(cc_jsonl.looks_like(line(type="mode") + "\n" + line(type="x", uuid="u")))

    def test_leading_blank_lines_and_garbage(self):
        self.assertTrue(cc_jsonl.looks_like("\n\n" + human("привет", "u1")))
        self.assertFalse(cc_jsonl.looks_like("не json\n" + human("привет", "u1")))
        self.assertFalse(cc_jsonl.looks_like("[1, 2]\n"))
        self.assertFalse(cc_jsonl.looks_like(""))


class MainSession(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = normalize.parse_file(MAIN)
        cls.m = cls.main["mechanical_metrics"]

    def tools(self, name=None, sidechain=None):
        return [tool for t in self.main["turns"] for tool in t["tools"]
                if (name is None or tool["name"] == name)
                and (sidechain is None or t["sidechain"] == sidechain)]

    def test_document_header(self):
        d = self.main
        self.assertEqual(d["format"], "claude-jsonl")
        self.assertEqual(d["session_id"], SID)
        self.assertEqual(d["collector"], "Claude Code 2.1.250")
        self.assertEqual(d["started_at"], "2026-09-20T10:00:00.123Z")
        self.assertEqual(d["ended_at"], "2026-09-20T10:01:55.123Z")
        self.assertEqual(d["started_at_source"], "record_ts")
        self.assertEqual(d["models"], ["claude-opus-5", "claude-haiku-4-5"])   # без <synthetic>
        self.assertEqual(d["path"], str(MAIN))

    def test_index_dense(self):
        self.assertEqual([t["index"] for t in self.main["turns"]],
                         list(range(self.main["turns_total"])))

    def test_usage_not_doubled(self):
        self.assertEqual(self.m["context_peak_tokens"], 1000)      # 10 + 900 + 90 на ответ
        self.assertAlmostEqual(self.m["cache_share"], 0.9, places=3)

    def test_humans(self):
        self.assertEqual(self.main["user_turns"], 5)   # peer, meta, task-notification, tool_result — не люди
        humans = [t["text"] for t in self.main["turns"] if t["role"] == "user" and not t["synthetic"]]
        self.assertEqual(humans, ["Добавь функцию сложения в calc.py и тест к ней", "ок",
                                  "Теперь добавь вычитание", "Сначала покажи план, без правок",
                                  "Хорошо, делай по плану"])

    def test_synthetic_reasons(self):
        reasons = [t["synthetic_reason"] for t in self.main["turns"]
                   if t["role"] == "user" and t["synthetic"]]
        self.assertEqual(reasons, ["system_injection", "slash_command", "agent_message",
                                   "system_injection", "agent_message"])

    def test_slash_command(self):
        slash = [t for t in self.main["turns"] if "<command-name>/review" in t["text"]]
        self.assertEqual(len(slash), 1)
        self.assertEqual(slash[0]["synthetic_reason"], "slash_command")

    def test_assistant_lines_glued_by_message_id(self):
        first = next(t for t in self.main["turns"] if t["role"] == "assistant")
        # ответ из трёх строк и два следующих ответа до реплики человека — один ход агента
        self.assertEqual(first["text"], "Сейчас сделаю.\n\nЗапускаю тесты.\n\nГотово: функция и тест добавлены.")
        self.assertEqual([t["name"] for t in first["tools"]], ["Write", "Bash"])
        self.assertEqual(first["ts"], "2026-09-20T10:00:20.123Z")     # конец хода агента
        self.assertEqual(first["model"], "claude-opus-5")

    def test_thinking_not_in_text(self):
        self.assertFalse(any("Нужно создать файл" in t["text"] for t in self.main["turns"]))

    def test_tool_types(self):
        types = {tool["name"]: tool["type"] for tool in self.tools()}
        self.assertEqual(types, {"Write": "write", "Bash": "shell", "Read": "read",
                                 "mcp__x__y": "mcp", "Edit": "write", "Agent": "tool",
                                 "Glob": "read"})

    def test_bash_command_and_verification(self):
        bash = self.tools("Bash")
        self.assertEqual(len(bash), 1)
        self.assertEqual(bash[0]["command"], "pytest -q")
        self.assertGreaterEqual(self.m["verification_commands"], 1)
        self.assertTrue(self.m["commands_available"])
        self.assertEqual(self.m["shell_tool_calls"], 1)
        self.assertEqual(self.m["write_tool_calls"], 2)
        self.assertEqual(self.m["tool_calls"], 7)

    def test_tool_results_attached(self):
        bash = self.tools("Bash")[0]
        self.assertEqual(bash["output_chars"], 5000)
        self.assertTrue(bash["output_head"].startswith("1 passed in 0.02s"))
        self.assertEqual(len(bash["output_head"]), 600)
        read_tool = self.tools("Read")[0]
        self.assertEqual(read_tool["output_head"], "def add(a, b):\n    return a + b\n")
        self.assertTrue(self.tools("mcp__x__y")[0]["is_error"])
        self.assertFalse(bash["is_error"])

    def test_interrupts(self):
        self.assertGreaterEqual(self.m["interrupts"], 2)              # прерывание + отказ
        self.assertEqual(self.m["interrupts"], 2)
        plan = next(t for t in self.main["turns"] if t["text"].startswith("План:"))
        self.assertTrue(plan["interrupted"])
        self.assertFalse(any("[Request interrupted" in t["text"] for t in self.main["turns"]))

    def test_skips_counted(self):
        self.assertEqual(self.m["unparsed_lines"], 1)
        self.assertEqual(self.m["unknown_types"], {"brand-new": 1})

    def test_span_and_approvals(self):
        self.assertTrue(self.m["timestamps_available"])
        self.assertEqual(self.m["session_span_seconds"], 115.0)
        self.assertEqual(self.m["approvals"], 1)
        self.assertEqual(self.m["instant_approvals"], 1)             # «ок» через 4 с после Write
        self.assertEqual(self.m["speed_accept_count"], 1)

    def test_subagents_sidechain(self):
        side = [t for t in self.main["turns"] if t["sidechain"]]
        self.assertTrue(side and all(t["role"] == "assistant" for t in side))
        self.assertEqual(self.m["subagent_files"], 1)
        self.assertEqual(self.main["sidechain_turns"], len(side))
        # ходы по времени: заголовок сабагента сразу за ходом агента, который его запустил
        # (его конец дописан позже, но номер хода — по первой строке); заголовок несёт
        # agent_type из .meta.json
        first_side = self.main["turns"].index(side[0])
        self.assertEqual([t["name"] for t in self.main["turns"][first_side - 1]["tools"]], ["Agent"])
        self.assertIn("Всё сделано.", self.main["turns"][first_side - 1]["text"])
        self.assertTrue(all(t["sidechain"] for t in self.main["turns"][first_side:]))
        self.assertIn("Explore", side[0]["text"])
        self.assertIn("a1", side[0]["text"])
        self.assertEqual(self.tools("Glob", sidechain=True)[0]["output_head"], "tests/test_calc.py")

    def test_compactions_zero(self):
        self.assertEqual(self.m["compactions"], 0)

    def test_tool_denials(self):
        self.assertEqual(self.m["tool_denials"], {"user-rejected": 1})

    def test_all_metric_keys(self):
        self.assertEqual(set(self.m), set(normalize.metrics([])) | set(normalize.EXTRA_METRIC_KEYS))
        for key in normalize.EXTRA_METRIC_KEYS:
            self.assertIsNotNone(self.m[key], key)


class WithoutPath(unittest.TestCase):
    def test_no_path_no_subagents(self):
        doc = cc_jsonl.parse(read("claude-main.jsonl"))
        self.assertEqual(doc["sidechain_turns"], 0)
        self.assertEqual(doc["mechanical_metrics"]["subagent_files"], 0)
        self.assertEqual(doc["user_turns"], 5)

    def test_index_stable_when_log_grows(self):
        text = read("claude-main.jsonl")
        before = cc_jsonl.parse(text)["turns"]
        after = cc_jsonl.parse(text + human("ещё вопрос", "u-tail", "2026-09-20T10:05:00.000Z") + "\n")["turns"]
        self.assertEqual(len(after), len(before) + 1)
        self.assertEqual([(t["index"], t["text"]) for t in before],
                         [(t["index"], t["text"]) for t in after[:len(before)]])

    def test_session_id_does_not_depend_on_file_name(self):
        text = read("claude-compact.jsonl")
        a = normalize.parse(text, path=Path("/home/user/x/one.jsonl"))
        b = normalize.parse(text, path=Path("/home/user/y/renamed.jsonl"))
        self.assertEqual(a["session_id"], "aaaaaaaa-0000-0000-0000-000000000002")
        self.assertEqual(a["session_id"], b["session_id"])

    def test_no_tool_output_and_max_turn_chars(self):
        doc = cc_jsonl.parse(read("claude-main.jsonl"), keep_tool_output=False, max_turn_chars=5)
        tools = [tool for t in doc["turns"] for tool in t["tools"]]
        self.assertTrue(all(tool["output_head"] is None for tool in tools))
        self.assertEqual(next(tool for tool in tools if tool["name"] == "Bash")["output_chars"], 5000)
        first = doc["turns"][0]
        self.assertTrue(first["text"].startswith("Добав\n…[обрезано, всего "), first["text"])
        self.assertEqual(first["chars"], len("Добавь функцию сложения в calc.py и тест к ней"))


class Robustness(unittest.TestCase):
    def test_blank_lines_silent_non_objects_counted(self):
        text = "\n".join([human("первый", "u1"), "", "   ", "[1, 2]", "42", "не json",
                          line(sessionId=SID, uuid="u9"), human("второй", "u2")])
        doc = cc_jsonl.parse(text)
        self.assertEqual(doc["user_turns"], 2)
        # не объект, не JSON и объект без type — нераспознанные строки
        self.assertEqual(doc["mechanical_metrics"]["unparsed_lines"], 4)
        self.assertEqual(doc["mechanical_metrics"]["unknown_types"], {})

    def test_unicode_line_separators_inside_strings(self):
        # json.dumps(ensure_ascii=False) оставляет U+2028/U+2029/U+0085 как есть; строки делит только \n
        text = "\n".join([human("до после и\x85ещё", "u1"), human("второй", "u2")])
        doc = cc_jsonl.parse(text)
        self.assertEqual(doc["mechanical_metrics"]["unparsed_lines"], 0)
        self.assertEqual(doc["user_turns"], 2)

    def test_snapshot_escapes_unicode_line_separators(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "s.jsonl"
            src.write_text(human("до после\x85", "u1") + "\n", encoding="utf-8")
            text, _, _ = cc_jsonl.compact_snapshot(src, lambda o: (o, {}))
        self.assertEqual(len(text.splitlines()), 1)
        self.assertEqual(json.loads(text.splitlines()[0])["message"]["content"], "до после\x85")

    def test_known_meta_types_skipped_silently(self):
        metas = ["last-prompt", "mode", "permission-mode", "custom-title", "ai-title", "summary",
                 "file-history-snapshot", "queue-operation", "attachment", "artifact-link"]
        text = "\n".join([human("первый", "u1")] + [line(type=t, sessionId=SID) for t in metas])
        m = cc_jsonl.parse(text)["mechanical_metrics"]
        self.assertEqual((m["unparsed_lines"], m["unknown_types"]), (0, {}))

    def test_duplicate_uuid_counted_once(self):
        row = human("повтор", "u1")
        doc = cc_jsonl.parse(row + "\n" + row + "\n")
        self.assertEqual(doc["user_turns"], 1)

    def test_pasted_content_without_origin_is_human(self):
        row = line(type="user", uuid="u1", sessionId=SID, timestamp="2026-09-20T11:00:00.000Z",
                   message={"role": "user", "content": "<pasted_content id=1>текст</pasted_content>"})
        doc = cc_jsonl.parse(row)
        self.assertEqual(doc["user_turns"], 1)

    def test_tag_without_origin_is_synthetic(self):
        row = line(type="user", uuid="u1", sessionId=SID, timestamp="2026-09-20T11:00:00.000Z",
                   message={"role": "user", "content": "<bash-input>ls</bash-input>"})
        turn = cc_jsonl.parse(row)["turns"][0]
        self.assertEqual((turn["synthetic"], turn["synthetic_reason"]), (True, "system_injection"))

    def test_image_only_human(self):
        row = line(type="user", uuid="u1", sessionId=SID, origin={"kind": "human"},
                   timestamp="2026-09-20T11:00:00.000Z",
                   message={"role": "user", "content": [{"type": "image", "source": {"data": "AAAA"}}]})
        turn = cc_jsonl.parse(row)["turns"][0]
        self.assertEqual(turn["text"], "[image]")

    def test_usage_from_last_line_of_response(self):
        def asst(uuid, mid, u, block):
            return line(type="assistant", uuid=uuid, sessionId=SID, timestamp="2026-09-20T11:00:01.000Z",
                        message={"id": mid, "model": "m", "content": [block],
                                 "usage": dict(zip(("input_tokens", "cache_read_input_tokens",
                                                    "cache_creation_input_tokens", "output_tokens"), u))})
        text = "\n".join([
            human("начни", "u0"),
            asst("u1", "A", (10, 900, 90, 1), {"type": "text", "text": "раз"}),
            asst("u2", "A", (10, 900, 90, 7), {"type": "text", "text": "два"}),
            asst("u3", "B", (100, 0, 0, 3), {"type": "text", "text": "три"}),
        ])
        m = cc_jsonl.parse(text)["mechanical_metrics"]
        self.assertEqual(m["context_peak_tokens"], 1000)
        self.assertEqual(m["cache_share"], round(900 / 1100, 3))     # по строкам было бы 1800/2100

    def test_no_usage_means_none(self):
        m = cc_jsonl.parse(human("один", "u1"))["mechanical_metrics"]
        self.assertIsNone(m["context_peak_tokens"])
        self.assertIsNone(m["cache_share"])


class Compact(unittest.TestCase):
    def test_compact_marker(self):
        doc = cc_jsonl.parse(read("claude-compact.jsonl"), path=str(COMPACT))
        self.assertEqual(doc["mechanical_metrics"]["compactions"], 1)
        marker = [t for t in doc["turns"] if t.get("synthetic_reason") == "compact"]
        self.assertEqual(len(marker), 1)
        self.assertEqual((marker[0]["role"], marker[0]["text"], marker[0]["tools"]),
                         ("system", "compact", []))

    def test_compact_summary_not_human(self):
        doc = cc_jsonl.parse(read("claude-compact.jsonl"))
        self.assertEqual(doc["user_turns"], 4)
        summary = [t for t in doc["turns"] if t["synthetic_reason"] == "compact_summary"]
        self.assertEqual(len(summary), 1)


class Snapshot(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = normalize.parse_file(MAIN)
        cls.text, cls.hits, cls.sha = cc_jsonl.compact_snapshot(MAIN, lambda o: (o, {}))
        cls.rows = [json.loads(ln) for ln in cls.text.splitlines()]

    def test_snapshot_roundtrip(self):
        snap = cc_jsonl.parse(self.text)                     # без path: сабагенты уже внутри
        self.assertEqual([t["text"] for t in snap["turns"]], [t["text"] for t in self.main["turns"]])
        self.assertEqual([t["tools"] for t in snap["turns"]], [t["tools"] for t in self.main["turns"]])
        skip = {"unparsed_lines", "unknown_types"}
        self.assertEqual({k: v for k, v in snap["mechanical_metrics"].items() if k not in skip},
                         {k: v for k, v in self.main["mechanical_metrics"].items() if k not in skip})
        self.assertEqual(snap["session_id"], self.main["session_id"])
        self.assertEqual(normalize.detect_format(self.text), "claude-jsonl")

    def test_only_conversation_lines_subagents_last(self):
        self.assertEqual({r["type"] for r in self.rows}, {"user", "assistant", "system"})
        side = [i for i, r in enumerate(self.rows) if r.get("isSidechain")]
        self.assertEqual(side, list(range(len(self.rows) - 3, len(self.rows))))
        for i in side:
            self.assertEqual(self.rows[i][cc_jsonl.SUBAGENTS_INLINE_KEY], "a1")
            self.assertEqual(self.rows[i]["agentType"], "Explore")

    def test_thinking_compacted(self):
        blocks = [b for r in self.rows if r["type"] == "assistant"
                  for b in r["message"]["content"] if b.get("type") == "thinking"]
        self.assertEqual(blocks, [{"type": "thinking", "chars": len("Нужно создать файл и тест.")}])

    def test_long_tool_result_truncated(self):
        contents = [b["content"] for r in self.rows if r["type"] == "user"
                    and isinstance(r["message"]["content"], list)
                    for b in r["message"]["content"] if b.get("type") == "tool_result"]
        long = [c for c in contents if isinstance(c, str) and "обрезано" in c]
        self.assertEqual(long, ["1 passed in 0.02s\n" + "." * 3982 + "…[обрезано 1000]"])
        self.assertFalse(any("toolUseResult" in r for r in self.rows))

    def test_sanitize_gets_whole_lines_hits_summed(self):
        seen = []

        def stub(obj):
            seen.append(obj)
            text = json.dumps(obj, ensure_ascii=False).replace("calc.py", "[FILE]")
            return json.loads(text), {"file": 1}

        text, hits, _ = cc_jsonl.compact_snapshot(MAIN, stub)
        self.assertEqual(len(seen), len(text.splitlines()))
        self.assertTrue(all("type" in o and "uuid" in o for o in seen))
        self.assertEqual(hits, {"file": len(seen)})
        self.assertNotIn("calc.py", text)

    def test_source_sha256(self):
        h = hashlib.sha256()
        h.update(MAIN.read_bytes())
        h.update((F / "claude-main" / "subagents" / "agent-a1.jsonl").read_bytes())
        self.assertEqual(self.sha, h.hexdigest())

    def test_sha_changes_with_subagent_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            shutil.copy(MAIN, tmp / MAIN.name)
            shutil.copytree(F / "claude-main", tmp / "claude-main")
            _, _, sha1 = cc_jsonl.compact_snapshot(tmp / MAIN.name, lambda o: (o, {}))
            self.assertEqual(sha1, self.sha)
            sub = tmp / "claude-main" / "subagents" / "agent-a1.jsonl"
            sub.write_text(sub.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            _, _, sha2 = cc_jsonl.compact_snapshot(tmp / MAIN.name, lambda o: (o, {}))
            self.assertNotEqual(sha1, sha2)

    def test_missing_file(self):
        with self.assertRaises(SrError) as cm:
            cc_jsonl.compact_snapshot(F / "nope.jsonl", lambda o: (o, {}))
        self.assertIn("nope.jsonl", str(cm.exception))

    def test_sanitize_must_return_pair(self):
        with self.assertRaises(SrError):
            cc_jsonl.compact_snapshot(MAIN, lambda o: o)


class TimeOrder(unittest.TestCase):
    """Ходы основной ветки и сабагентов идут по времени: у выросшей сессии первые N ходов
    сохраняют номера, и разбор снапшота совпадает с разбором исходника."""

    TAIL = "\n".join([
        human("Теперь добавь умножение", "t-h1", "2026-09-20T10:03:00.000Z"),
        said("Умножение добавлено.", "t-a1", "2026-09-20T10:03:10.000Z"),
    ]) + "\n"

    def test_grown_main_keeps_old_turns(self):
        with tempfile.TemporaryDirectory() as tmp:
            main = copy_main(Path(tmp))
            before = normalize.parse_file(main)["turns"]
            with open(main, "a", encoding="utf-8") as fh:
                fh.write("\n" + self.TAIL)
            after = normalize.parse_file(main)["turns"]
            snap = snapshot_doc(main)["turns"]
        self.assertTrue(any(t["sidechain"] for t in before))
        self.assertEqual(view(after[:len(before)]), view(before))
        self.assertEqual([(t["role"], t["text"], t["sidechain"]) for t in after[len(before):]],
                         [("user", "Теперь добавь умножение", False),
                          ("assistant", "Умножение добавлено.", False)])
        self.assertEqual(view(snap), view(after))

    def test_interleaved_background_subagent(self):
        main_rows = [
            human("начни", "m1", "2026-09-20T12:00:00.000Z"),
            called("Agent", "tA", "m2", "2026-09-20T12:00:01.000Z"),
            result("tA", "m3", "2026-09-20T12:00:02.000Z", "запущен в фоне"),
            said("Сабагент работает, жду.", "m4", "2026-09-20T12:00:03.000Z"),
            human("как дела?", "m5", "2026-09-20T12:00:06.000Z"),
            said("Ещё ищет.", "m6", "2026-09-20T12:00:07.000Z"),
        ]
        side_rows = [
            line(type="user", uuid="s1", sessionId=SID, timestamp="2026-09-20T12:00:01.500Z",
                 message={"role": "user", "content": "найди тесты"}),
            said("Ищу.", "s2", "2026-09-20T12:00:04.000Z"),
            said("Нашёл два файла.", "s3", "2026-09-20T12:00:08.000Z"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            main = Path(tmp) / "s.jsonl"
            main.write_text("\n".join(main_rows) + "\n", encoding="utf-8")
            subs = Path(tmp) / "s" / "subagents"
            subs.mkdir(parents=True)
            (subs / "agent-b7.jsonl").write_text("\n".join(side_rows) + "\n", encoding="utf-8")
            doc = normalize.parse_file(main)
            snap = snapshot_doc(main)
        self.assertEqual([(t["role"], t["text"], t["sidechain"]) for t in doc["turns"]], [
            ("user", "начни", False),
            ("assistant", "Сабагент работает, жду.", False),
            ("assistant", "subagent b7", True),
            ("assistant", "найди тесты", True),
            ("assistant", "Ищу.\n\nНашёл два файла.", True),
            ("user", "как дела?", False),
            ("assistant", "Ещё ищет.", False),
        ])
        self.assertEqual(view(snap["turns"]), view(doc["turns"]))
        self.assertEqual(snap["mechanical_metrics"], doc["mechanical_metrics"])

    def test_equal_time_main_first_and_no_ts_stays_with_neighbour(self):
        t = "2026-09-20T12:00:00.000Z"
        main_rows = [human("раз", "m1", t), said("ответ", "m2", "2026-09-20T12:00:05.000Z")]
        side_rows = [
            line(type="user", uuid="s1", sessionId=SID, timestamp=t,
                 message={"role": "user", "content": "задача"}),
            said("без времени", "s2", ""),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            main = Path(tmp) / "s.jsonl"
            main.write_text("\n".join(main_rows) + "\n", encoding="utf-8")
            subs = Path(tmp) / "s" / "subagents"
            subs.mkdir(parents=True)
            (subs / "agent-c1.jsonl").write_text("\n".join(side_rows) + "\n", encoding="utf-8")
            doc = normalize.parse_file(main)
            snap = snapshot_doc(main)
        self.assertEqual([t["text"] for t in doc["turns"]],
                         ["раз", "subagent c1", "задача", "без времени", "ответ"])
        self.assertEqual(view(snap["turns"]), view(doc["turns"]))


class InlineAndFolder(unittest.TestCase):
    """В главном файле есть inline-sidechain, и рядом папка `subagents/`: исходник и снапшот
    читают обе части одним правилом, дубли по uuid не считаются."""

    def test_same_turns_from_source_and_snapshot(self):
        sub_text = (F / "claude-main" / "subagents" / "agent-a1.jsonl").read_text(encoding="utf-8")
        inline = [json.loads(ln) for ln in sub_text.splitlines() if ln.strip()]
        b2 = sub_text.replace('"a1"', '"b2"').replace("-8000-0000000000", "-8000-0000000001")
        with tempfile.TemporaryDirectory() as tmp:
            main = copy_main(Path(tmp))
            # строки a1 лежат и в главном файле, и в папке: считаются один раз
            with open(main, "a", encoding="utf-8") as fh:
                fh.write("\n" + "\n".join(json.dumps(r, ensure_ascii=False) for r in inline) + "\n")
            (Path(tmp) / "claude-main" / "subagents" / "agent-b2.jsonl").write_text(b2, encoding="utf-8")
            doc = normalize.parse_file(main)
            snap = snapshot_doc(main)
        heads = [t["text"] for t in doc["turns"] if t["text"].startswith("subagent ")]
        self.assertEqual(heads, ["subagent a1 · Explore", "subagent b2"])
        self.assertEqual(doc["mechanical_metrics"]["subagent_files"], 2)
        self.assertEqual(doc["sidechain_turns"], 6)
        self.assertEqual(view(snap["turns"]), view(doc["turns"]))
        skip = {"unparsed_lines", "unknown_types"}
        self.assertEqual({k: v for k, v in snap["mechanical_metrics"].items() if k not in skip},
                         {k: v for k, v in doc["mechanical_metrics"].items() if k not in skip})


class ToolDenials(unittest.TestCase):
    """`interrupts` — только отказ человека без последующего прерывания того же хода;
    все виды отказов — в `tool_denials`."""

    def rows(self) -> list[str]:
        def ts(s: int) -> str:
            return f"2026-09-20T13:00:{s:02d}.000Z"
        return [
            human("поправь файл", "d1", ts(0)),
            called("Edit", "t1", "d2", ts(1)),
            result("t1", "d3", ts(2), "The user doesn't want to proceed", toolDenialKind="user-rejected"),
            called("Bash", "t2", "d4", ts(3)),
            result("t2", "d5", ts(4), "denied by rule", toolDenialKind="permission-rule"),
            called("Write", "t3", "d6", ts(5)),
            result("t3", "d7", ts(6), "blocked", toolDenialKind="automode-blocked"),
            said("Не получилось, нужны права.", "d8", ts(7)),
            human("попробуй ещё", "d9", ts(10)),
            called("Edit", "t4", "d10", ts(11)),
            result("t4", "d11", ts(12), "The user doesn't want to proceed", toolDenialKind="user-rejected"),
            line(type="user", uuid="d12", sessionId=SID, timestamp=ts(13),
                 message={"role": "user", "content": [{"type": "text",
                                                       "text": "[Request interrupted by user]"}]}),
            human("стоп, сделаю сам", "d13", ts(20)),
        ]

    def test_interrupts_and_denials(self):
        m = cc_jsonl.parse("\n".join(self.rows()) + "\n")["mechanical_metrics"]
        # отказ человека без прерывания (+1) и прерванный ход (+1); второй отказ человека
        # сразу перед прерыванием — то же событие, rule и automode — конфигурация
        self.assertEqual(m["interrupts"], 2)
        self.assertEqual(m["tool_denials"], {"user-rejected": 2, "permission-rule": 1,
                                             "automode-blocked": 1})

    def test_no_denials_is_empty_dict(self):
        m = cc_jsonl.parse(human("один", "u1"))["mechanical_metrics"]
        self.assertEqual(m["tool_denials"], {})

    def test_key_registered(self):
        self.assertIn("tool_denials", normalize.EXTRA_METRIC_KEYS)


class Cli(unittest.TestCase):
    def test_normalize_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = run_sr("normalize", str(MAIN), "--stats", home=Path(tmp), cwd=Path(tmp))
        self.assertEqual(res.returncode, 0, res.stderr)
        doc = json.loads(res.stdout)
        self.assertNotIn("turns", doc)
        self.assertEqual(doc["format"], "claude-jsonl")
        self.assertEqual(doc["mechanical_metrics"]["subagent_files"], 1)


class Cleanliness(unittest.TestCase):
    def test_no_private_words_in_module(self):
        text = (SCRIPTS / "lib" / "cc_jsonl.py").read_text(encoding="utf-8")
        for word in ["студ" + "ент", "мод" + "ул", "кур" + "с", "опер" + "атор"]:
            with self.subTest(word=word):
                self.assertIsNone(re.search(word, text, re.I))


if __name__ == "__main__":
    unittest.main()
