"""Команда collect: снапшоты, санитайзер, идемпотентность, рост, усыхание, сводка, doctor, реестр."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _util import FIXTURES, TempHome, copy_fixture, run_sr

from lib import cmd_collect, inventory, lock, normalize, registry, sanitize

LOGS = FIXTURES / "logs"
NOW = "2026-09-27T10:00:00Z"
LATER = "2026-09-27T11:00:00Z"
JSONL_SESSION = "aaaaaaaa-0000-0000-0000-000000000001"
SPECSTORY_SESSION = "11111111-2222-3333-4444-555555555555"
SECRET_SESSION = "99999999-8888-7777-6666-555555555555"

ME_CARD = 'id: me\nname: "Test Person"\ngroup: default\n'

NEW_TURN = (
    "\n\n---\n\n_**User (2026-09-01 10:10:00Z)**_\n\nИ ещё добавь умножение.\n\n---\n\n"
    "_**Agent (claude-opus-5 2026-09-01 10:10:05Z)**_\n\nУмножение добавлено.\n"
)


def secret_log() -> str:
    """specstory-лог со своим session id и секретом, именем, названием компании и тикетом."""
    text = (LOGS / "specstory-claude.md").read_text(encoding="utf-8")
    text = text.replace(SPECSTORY_SESSION, SECRET_SESSION)
    return text.replace(
        "Добавь функцию сложения",
        'Test Person из Acme просит: export DB_PASSWORD="prod-x-99", тикет INT-42. Добавь функцию сложения',
        1,
    )


NEW_TURN_2 = (
    "\n\n---\n\n_**User (2026-09-01 10:20:00Z)**_\n\nТеперь деление.\n\n---\n\n"
    "_**Agent (claude-opus-5 2026-09-01 10:20:05Z)**_\n\nДеление добавлено.\n"
)
OTHER_ASK = "Совсем другая просьба: сделай парсер"


def other_session_log() -> str:
    """Другая сессия с тем же session id (ключом), что у specstory-claude.md: другое начало, больше ходов."""
    text = (LOGS / "specstory-claude.md").read_text(encoding="utf-8")
    return text.replace("Добавь функцию сложения", OTHER_ASK, 1) + NEW_TURN


def has_word(text: str, word: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text, re.IGNORECASE) is not None


def tree_sha(folder: Path) -> dict:
    return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob("*")) if p.is_file()}


class CollectBase(TempHome, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="sr-collect-")
        self.tmp = Path(self._tmp.name).resolve()
        self.root = copy_fixture("workspace-min", self.tmp)
        state = self.root / ".sessionretro"
        people = state / "people"
        (people / "alex.yaml").unlink()
        (people / "sam.yaml").unlink()
        (people / "me.yaml").write_text(ME_CARD, encoding="utf-8")

        self.src = self.tmp / "src"
        self.src.mkdir()
        shutil.copy(LOGS / "specstory-claude.md", self.src / "specstory-claude.md")
        shutil.copy(LOGS / "cc-scrollback.txt", self.src / "cc-scrollback.txt")
        shutil.copy(LOGS / "md-headings.md", self.src / "md-headings.md")
        (self.src / "secret.md").write_text(secret_log(), encoding="utf-8")

        # абсолютный путь внутри ~/.claude/projects берётся как есть
        self.cc = self.home / ".claude" / "projects" / "-home-user-proj"
        self.cc.mkdir(parents=True)
        shutil.copy(LOGS / "claude-main.jsonl", self.cc / "claude-main.jsonl")
        shutil.copytree(LOGS / "claude-main", self.cc / "claude-main")

        self.cfg_path = state / "config.json"
        cfg = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        cfg["mode"] = "self"
        cfg["workspace"]["groups"] = ["default"]
        cfg["sources"] = [
            {"type": "dir", "path": str(self.src), "person": "me"},
            {"type": "claude-jsonl", "project": str(self.cc), "person": "me"},
        ]
        cfg["sanitize"] = {"names": ["Acme"], "extra_patterns": ["INT-\\d+"]}
        self.write_cfg(cfg)
        self.me = state / "logs" / "me"

    def tearDown(self) -> None:
        self._tmp.cleanup()
        super().tearDown()

    def write_cfg(self, cfg: dict) -> None:
        self.cfg = cfg
        self.cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    def collect(self, now: str = NOW, providers=None, replace=None) -> dict:
        return cmd_collect.collect(self.root, providers, now=now, replace=replace)

    def index(self) -> dict:
        return inventory.load_index(self.root)

    def rec(self, session_id: str, fmt: str | None = None) -> dict:
        found = [r for r in self.index()["logs"]
                 if r["session_id"] == session_id and (fmt is None or r["format"] == fmt)]
        self.assertEqual(len(found), 1, found)
        return found[0]

    def mark_all_reviewed(self) -> None:
        idx = self.index()
        idx["logs"] = [inventory.mark(r, "reviewed", "r1", NOW) for r in idx["logs"]]
        inventory.save_index(self.root, idx)

    def snapshots(self) -> list[Path]:
        return sorted(p for p in self.me.iterdir() if p.suffix in (".md", ".jsonl"))

    def mtimes(self) -> dict:
        return {p.name: p.stat().st_mtime_ns for p in self.snapshots()}


class FirstCollectTest(CollectBase):
    def test_snapshots_meta_index_all_new(self):
        out = self.collect()
        names = [p.name for p in self.snapshots()]
        self.assertEqual(len(names), 5, names)
        self.assertTrue(any(n.endswith(".jsonl") for n in names))
        self.assertTrue(any(n.endswith(".md") for n in names))
        self.assertIn(f"{JSONL_SESSION}.jsonl", names)
        self.assertIn(f"{SPECSTORY_SESSION}.md", names)
        meta = inventory.load_meta(self.me)
        self.assertEqual(sorted(meta), sorted(names))
        idx = self.index()
        self.assertEqual(len(idx["logs"]), 5)
        self.assertTrue(all(r["state"] == "new" for r in idx["logs"]))
        self.assertEqual(out["counts"]["new"], 5)
        self.assertEqual(sum(out["counts"].values()), 5)
        self.assertEqual(out["last_snapshot_at"], NOW)
        self.assertEqual(idx["updated_at"], NOW)

    def test_summary_shape(self):
        out = self.collect()
        for key in ("counts", "sanitized", "orig", "slug_named", "context_report", "warnings",
                    "specstory", "last_snapshot_at"):
            self.assertIn(key, out)
        self.assertEqual(set(out["counts"]), set(inventory.STATES))
        self.assertEqual(out["specstory"], [])       # specstory-источника нет — sync не зовётся
        self.assertEqual(out["orig"], 0)

    def test_jsonl_record_from_source(self):
        self.collect()
        rec = self.rec(JSONL_SESSION)
        self.assertEqual(rec["format"], "claude-jsonl")
        self.assertEqual(rec["key"], JSONL_SESSION)
        self.assertEqual(rec["source"], {"type": "claude-jsonl", "id": "config#2"})
        self.assertEqual(rec["source_path"], "~/.claude/projects/-home-user-proj/claude-main.jsonl")
        self.assertEqual(rec["metrics"]["unparsed_lines"], 1)     # из разбора исходника
        self.assertEqual(rec["metrics"]["subagent_files"], 1)     # сабагенты внутри снапшота
        snap = (self.me / f"{JSONL_SESSION}.jsonl").read_text(encoding="utf-8")
        for line in snap.splitlines():
            json.loads(line)

    def test_sanitizer_secret_names_extra(self):
        out = self.collect()
        snap = (self.me / f"{SECRET_SESSION}.md").read_text(encoding="utf-8")
        for leaked in ("prod-x-99", "Test Person", "Acme", "INT-42"):
            self.assertNotIn(leaked, snap)
        self.assertIn("[NAME]", snap)
        self.assertIn("[REDACTED]", snap)
        self.assertIn("[SECRET_ASSIGNMENT]", snap)
        reports = [r for r in out["sanitized"] if r["file"].endswith("secret.md")]
        self.assertEqual(len(reports), 1, out["sanitized"])
        rep = reports[0]
        self.assertGreaterEqual(rep["renamed"], 2)
        self.assertGreaterEqual(rep["rules_hit"].get("extra", 0), 1)
        self.assertNotIn("names", rep["rules_hit"])
        self.assertTrue(set(rep["rules_hit"]) & {"code_secret", "env_secret"}, rep)
        meta = inventory.load_meta(self.me)[f"{SECRET_SESSION}.md"]
        self.assertGreaterEqual(meta["sanitize"]["renamed"], 2)
        self.assertEqual(self.rec(SECRET_SESSION)["sanitize"], meta["sanitize"])

    def test_local_user_login_removed(self):
        from unittest import mock
        (self.src / "login.md").write_text(
            (FIXTURES / "logs" / "specstory-claude.md").read_text(encoding="utf-8")
            .replace("11111111-2222-3333-4444-555555555555", "99999999-2222-3333-4444-555555555555")
            + "\n-rw-r--r--  1 tuserx  staff  556 README.md\n", encoding="utf-8")
        with mock.patch("lib.sanitize.local_user_names", return_value=["tuserx"]):
            self.collect()
        snap = (self.me / "99999999-2222-3333-4444-555555555555.md").read_text(encoding="utf-8")
        self.assertNotIn("tuserx", snap)
        self.assertIn("[USER]", snap)

    def test_sources_never_change(self):
        before = (tree_sha(self.src), tree_sha(self.cc))
        self.collect()
        self.collect(LATER)
        self.assertEqual((tree_sha(self.src), tree_sha(self.cc)), before)
        self.assertFalse(list(self.src.rglob("*.orig")))

    def test_unparsable_source_warns(self):
        (self.src / "garbage.txt").write_text("hello world\n", encoding="utf-8")
        out = self.collect()
        self.assertTrue(any("garbage.txt" in w for w in out["warnings"]), out["warnings"])
        self.assertEqual(len(self.snapshots()), 5)

    def test_junk_counted(self):
        (self.src / "a.md.orig").write_text("x", encoding="utf-8")
        (self.src / "context-report.md").write_text("x", encoding="utf-8")
        shutil.copy(LOGS / "specstory-codex-exec.md", self.src / "2026-09-02_09-00-00Z-codex.md")
        out = self.collect()
        self.assertEqual((out["orig"], out["context_report"], out["slug_named"]), (1, 1, 1))
        names = [p.name for p in self.snapshots()]
        self.assertIn("22222222-3333-4444-5555-666666666666.md", names)
        self.assertFalse(any("codex" in n for n in names))       # слаг в logs/ не попадает


class RepeatTest(CollectBase):
    def test_repeat_after_review_unchanged_no_rewrite(self):
        self.collect()
        self.mark_all_reviewed()
        before = self.mtimes()
        out = self.collect(LATER)
        self.assertEqual(out["counts"]["unchanged"], 5, out)
        self.assertEqual(self.mtimes(), before)
        self.assertEqual(out["sanitized"], [])        # ничего не переписано — нечего отчитывать

    def test_repeat_without_mark_stays_new(self):
        self.collect()
        out = self.collect(LATER)
        self.assertEqual(out["counts"]["new"], 5)

    def test_new_turn_grown(self):
        self.collect()
        self.mark_all_reviewed()
        prev = self.rec(SPECSTORY_SESSION)["turns_total"]
        with open(self.src / "specstory-claude.md", "a", encoding="utf-8") as fh:
            fh.write(NEW_TURN)
        out = self.collect(LATER)
        rec = self.rec(SPECSTORY_SESSION)
        self.assertEqual(rec["state"], "grown")
        self.assertEqual(rec["review_from_turn"], prev)
        self.assertEqual(rec["turns_total"], prev + 2)
        self.assertEqual(out["counts"]["grown"], 1)
        self.assertEqual(out["counts"]["unchanged"], 4)

    def test_blank_tail_unchanged(self):
        self.collect()
        self.mark_all_reviewed()
        with open(self.src / "specstory-claude.md", "a", encoding="utf-8") as fh:
            fh.write("\n\n\n")
        with open(self.cc / "claude-main.jsonl", "a", encoding="utf-8") as fh:
            fh.write("\n\n")
        out = self.collect(LATER)
        self.assertEqual(out["counts"]["unchanged"], 5, out)
        self.assertEqual(self.rec(SPECSTORY_SESSION).get("review_from_turn"), None)

    def test_shrunk_source_not_rewritten(self):
        self.collect()
        snap = self.me / f"{SPECSTORY_SESSION}.md"
        before = snap.read_bytes()
        src = self.src / "specstory-claude.md"
        text = src.read_text(encoding="utf-8")
        src.write_text(text[: text.index("_**User (2026-09-01 10:04:00Z)**_")], encoding="utf-8")
        out = self.collect(LATER)
        self.assertEqual(snap.read_bytes(), before)
        self.assertTrue(any(snap.name in w and "меньше" in w for w in out["warnings"]),
                        out["warnings"])
        self.assertEqual(self.rec(SPECSTORY_SESSION)["turns_total"], 8)

    def test_new_subagent_file_rewrites_jsonl(self):
        self.collect()
        snap = self.me / f"{JSONL_SESSION}.jsonl"
        old_sha = inventory.load_meta(self.me)[snap.name]["source_sha256"]
        subs = self.cc / "claude-main" / "subagents"
        text = (subs / "agent-a1.jsonl").read_text(encoding="utf-8")
        # свои uuid: одинаковые строки парсер считает копиями и не учитывает дважды
        text = text.replace('"a1"', '"b2"').replace("-8000-0000000000", "-8000-0000000001")
        (subs / "agent-b2.jsonl").write_text(text, encoding="utf-8")
        self.collect(LATER)
        self.assertIn('"b2"', snap.read_text(encoding="utf-8"))
        self.assertNotEqual(inventory.load_meta(self.me)[snap.name]["source_sha256"], old_sha)
        self.assertEqual(self.rec(JSONL_SESSION)["metrics"]["subagent_files"], 2)

    def test_same_log_in_two_sources_one_snapshot(self):
        dup = self.tmp / "src2"
        dup.mkdir()
        shutil.copy(LOGS / "specstory-claude.md", dup / "copy.md")
        cfg = dict(self.cfg)
        cfg["sources"] = [*cfg["sources"], {"type": "dir", "path": str(dup), "person": "me"}]
        self.write_cfg(cfg)
        out = self.collect()
        self.assertEqual(len(self.snapshots()), 5)
        dups = [w for w in out["warnings"] if w.startswith(f"дубль ключа {SPECSTORY_SESSION}")]
        self.assertEqual(len(dups), 1, out["warnings"])
        self.assertIn("copy.md", dups[0])
        self.assertIn("specstory-claude.md", dups[0])
        meta = inventory.load_meta(self.me)[f"{SPECSTORY_SESSION}.md"]
        self.assertEqual(meta["source_path"], str(self.src / "specstory-claude.md"))
        before = self.mtimes()
        self.collect(LATER)
        self.assertEqual(self.mtimes(), before)


class SanitizerChangeTest(CollectBase):
    """I1: смена правил санитайзера доходит до готовых снапшотов, даже если исходника уже нет."""

    def snapshot_texts(self) -> dict:
        return {p.name: p.read_text(encoding="utf-8") for p in self.snapshots()}

    def add_name(self, name: str) -> None:
        cfg = json.loads(json.dumps(self.cfg))
        cfg["sanitize"]["names"].append(name)
        self.write_cfg(cfg)

    def test_meta_has_sanitizer_fp(self):
        self.collect()
        fps = {e["sanitizer_fp"] for e in inventory.load_meta(self.me).values()}
        self.assertEqual(len(fps), 1, fps)
        self.assertRegex(fps.pop(), r"^[0-9a-f]{64}$")

    def test_added_name_reaches_snapshots(self):
        self.collect()
        self.assertTrue(any(has_word(t, "calc") for t in self.snapshot_texts().values()))
        self.add_name("calc")
        self.collect(LATER)
        for name, text in self.snapshot_texts().items():
            with self.subTest(snapshot=name):
                self.assertFalse(has_word(text, "calc"))

    def test_added_name_reaches_snapshots_when_sources_gone(self):
        self.collect()
        shutil.rmtree(self.src)
        shutil.rmtree(self.cc)
        shutil.rmtree(self.cc.with_suffix(""), ignore_errors=True)
        self.add_name("calc")
        out = self.collect(LATER)
        texts = self.snapshot_texts()
        self.assertEqual(len(texts), 5)
        for name, text in texts.items():
            with self.subTest(snapshot=name):
                self.assertFalse(has_word(text, "calc"))
        for line in texts[f"{JSONL_SESSION}.jsonl"].splitlines():
            json.loads(line)
        self.assertTrue(any(r["renamed"] for r in out["sanitized"]), out["sanitized"])
        self.assertEqual(out["counts"]["gone"], 0)
        fp = {e["sanitizer_fp"] for e in inventory.load_meta(self.me).values()}
        self.assertEqual(len(fp), 1)
        before = self.mtimes()
        self.collect("2026-09-27T12:00:00Z")      # отпечаток совпал — второй раз не перечищается
        self.assertEqual(self.mtimes(), before)


# A01: пароль и секрет 2FA в ответе агента; значения выдуманные и собираются в рантайме.
A01_SESSION = "99999999-2222-3333-4444-a01a01a01a01"
A01_PWD = "Xy7" + "pass" + "9Q"
A01_B32 = "JBSW" + "Y3DP" + "EHPK" + "3PXP"
A01_NEW_RULES = {"password_text", "totp_secret", "base32_secret"}


class PasswordLeakResanitizeTest(CollectBase):
    """A01: снапшот, собранный правилами до `password_text`/`totp_secret`, перечищается после
    подъёма `RULES_VERSION` — и из исходника, и на месте, когда исходника уже нет."""

    def setUp(self) -> None:
        super().setUp()
        text = (LOGS / "specstory-claude.md").read_text(encoding="utf-8").replace(SPECSTORY_SESSION, A01_SESSION)
        text = text.replace("Готово, тест зелёный.",
                            f"Готово.\n\n| Поле | Значение |\n|---|---|\n| Пароль | {A01_PWD} |\n"
                            f"| 2FA | {A01_B32} |\n\n- Password: {A01_PWD}", 1)
        self.assertIn(A01_PWD, text)
        (self.src / "a01.md").write_text(text, encoding="utf-8")
        self.snap = self.me / f"{A01_SESSION}.md"

    def collect_with_old_rules(self) -> None:
        old = [r for r in sanitize._COMPILED_FIRST if r[0] not in A01_NEW_RULES]
        with mock.patch.object(sanitize, "_COMPILED_FIRST", old), \
                mock.patch.object(sanitize, "RULES_VERSION", sanitize.RULES_VERSION - 1):
            self.collect()
        leaked = self.snap.read_text(encoding="utf-8")
        self.assertIn(A01_PWD, leaked)                # утечка воспроизведена старыми правилами
        self.assertIn(A01_B32, leaked)

    def assert_cleaned(self, out: dict) -> None:
        text = self.snap.read_text(encoding="utf-8")
        self.assertNotIn(A01_PWD, text)
        self.assertNotIn(A01_B32, text)
        self.assertIn("| Пароль | [PASSWORD] |", text)
        self.assertIn("| 2FA | [OTP_SECRET] |", text)
        self.assertIn("- Password: [PASSWORD]", text)
        meta = inventory.load_meta(self.me)[self.snap.name]
        self.assertEqual({e["sanitizer_fp"] for e in inventory.load_meta(self.me).values()}, {meta["sanitizer_fp"]})
        self.assertGreaterEqual(meta["sanitize"]["rules_hit"].get("password_text", 0), 2)
        self.assertGreaterEqual(meta["sanitize"]["rules_hit"].get("totp_secret", 0), 1)
        reps = [r for r in out["sanitized"] if r["rules_hit"].get("password_text")]
        self.assertTrue(reps, out["sanitized"])

    def test_old_fp_snapshot_resanitized_from_source(self):
        self.collect_with_old_rules()
        old_fp = inventory.load_meta(self.me)[self.snap.name]["sanitizer_fp"]
        out = self.collect(LATER)
        self.assert_cleaned(out)
        self.assertNotEqual(inventory.load_meta(self.me)[self.snap.name]["sanitizer_fp"], old_fp)

    def test_old_fp_snapshot_resanitized_when_source_gone(self):
        self.collect_with_old_rules()
        old_fp = inventory.load_meta(self.me)[self.snap.name]["sanitizer_fp"]
        shutil.rmtree(self.src)
        out = self.collect(LATER)
        self.assert_cleaned(out)
        self.assertNotEqual(inventory.load_meta(self.me)[self.snap.name]["sanitizer_fp"], old_fp)
        before = self.mtimes()
        self.collect("2026-09-27T12:00:00Z")      # отпечаток уже текущий — второй раз не трогается
        self.assertEqual(self.mtimes(), before)


class SkipUnchangedTest(CollectBase):
    """I5: неизменный исходник не разбирается и не санитайзится повторно; замок по режиму."""

    def source_paths(self) -> set[str]:
        return {str(p) for p in [*self.src.rglob("*"), *self.cc.rglob("*")]}

    def test_second_run_does_not_parse_or_sanitize_sources(self):
        self.collect()
        srcs = self.source_paths()
        with mock.patch.object(normalize, "parse_file", wraps=normalize.parse_file) as pf, \
                mock.patch.object(sanitize.Sanitizer, "sanitize_text", autospec=True,
                                  side_effect=sanitize.Sanitizer.sanitize_text) as st, \
                mock.patch.object(sanitize.Sanitizer, "sanitize_obj", autospec=True,
                                  side_effect=sanitize.Sanitizer.sanitize_obj) as so:
            out = self.collect(LATER)
        parsed = {str(c.args[0]) for c in pf.call_args_list}
        self.assertEqual(parsed & srcs, set())
        self.assertEqual((st.call_count, so.call_count), (0, 0))
        self.assertEqual(out["logs_indexed"], 5)

    def test_changed_source_only_is_parsed(self):
        self.collect()
        with open(self.src / "specstory-claude.md", "a", encoding="utf-8") as fh:
            fh.write(NEW_TURN)
        srcs = self.source_paths()
        with mock.patch.object(normalize, "parse_file", wraps=normalize.parse_file) as pf:
            self.collect(LATER)
        parsed = {str(c.args[0]) for c in pf.call_args_list} & srcs
        self.assertEqual(parsed, {str(self.src / "specstory-claude.md")})

    def args(self, quiet: bool) -> argparse.Namespace:
        return argparse.Namespace(quiet=quiet, json=False, provider=None, replace=None,
                                  workspace=str(self.root))

    def test_lock_timeout_by_mode(self):
        self.assertEqual((cmd_collect.LOCK_TIMEOUT, cmd_collect.LOCK_TIMEOUT_QUIET), (10, 120))
        seen = []
        with mock.patch.object(cmd_collect, "collect",
                               side_effect=lambda *a, **kw: seen.append(kw["lock_timeout"]) or {}), \
                mock.patch.object(cmd_collect, "format_text", return_value=""), \
                contextlib.redirect_stdout(io.StringIO()):
            cmd_collect.handle(self.args(quiet=True))
            cmd_collect.handle(self.args(quiet=False))
        self.assertEqual(seen, [120, 10])

    def test_busy_lock_quiet_exits_0_with_stderr_line(self):
        err = io.StringIO()
        with lock.file_lock(self.root / ".sessionretro" / "collect"), \
                mock.patch.object(cmd_collect, "LOCK_TIMEOUT_QUIET", 0.3), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()) as out:
            code = cmd_collect.handle(self.args(quiet=True))
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("сбор уже идёт", err.getvalue())
        self.assertIsNone(self.index())

    def test_busy_lock_interactive_is_error(self):
        with lock.file_lock(self.root / ".sessionretro" / "collect"), \
                mock.patch.object(cmd_collect, "LOCK_TIMEOUT", 0.3):
            with self.assertRaises(cmd_collect.SrError) as cm:
                cmd_collect.handle(self.args(quiet=False))
        self.assertEqual(cm.exception.code, 1)


class CollisionTest(CollectBase):
    """I2: разные сессии под одним ключом не затирают друг друга."""

    def spec_snapshots(self) -> list[Path]:
        return [p for p in self.snapshots() if p.name.startswith(SPECSTORY_SESSION)]

    def test_other_session_after_source_gone_does_not_overwrite(self):
        self.collect()
        snap = self.me / f"{SPECSTORY_SESSION}.md"
        before = snap.read_bytes()
        (self.src / "specstory-claude.md").unlink()
        (self.src / "other.md").write_text(other_session_log(), encoding="utf-8")
        out = self.collect(LATER)
        self.assertEqual(snap.read_bytes(), before)
        extra = [p for p in self.spec_snapshots() if p != snap]
        self.assertEqual(len(extra), 1, self.snapshots())
        self.assertRegex(extra[0].name, rf"^{SPECSTORY_SESSION}_[0-9a-f]{{8}}\.md$")
        self.assertIn(OTHER_ASK, extra[0].read_text(encoding="utf-8"))
        self.assertTrue(any("другая сессия" in w and extra[0].name in w for w in out["warnings"]),
                        out["warnings"])
        keys = {r["key"] for r in self.index()["logs"] if r["session_id"] == SPECSTORY_SESSION}
        self.assertEqual(len(keys), 2, keys)
        self.assertIn(SPECSTORY_SESSION, keys)
        self.assertEqual(out["counts"]["gone"], 0)

    def test_two_present_sources_different_sessions_both_kept(self):
        (self.src / "other.md").write_text(other_session_log(), encoding="utf-8")
        out = self.collect()
        snaps = self.spec_snapshots()
        self.assertEqual(len(snaps), 2, self.snapshots())
        texts = [p.read_text(encoding="utf-8") for p in snaps]
        self.assertEqual(sum(OTHER_ASK in t for t in texts), 1)
        self.assertEqual(sum("Добавь функцию сложения" in t for t in texts), 1)
        self.assertFalse(any(w.startswith(f"дубль ключа {SPECSTORY_SESSION}") for w in out["warnings"]),
                         out["warnings"])
        self.assertTrue(any("другая сессия" in w for w in out["warnings"]), out["warnings"])

    def test_suffixed_snapshot_stable_and_grows_in_place(self):
        (self.src / "other.md").write_text(other_session_log(), encoding="utf-8")
        self.collect()
        before = self.mtimes()
        self.collect(LATER)
        self.assertEqual(self.mtimes(), before)
        with open(self.src / "other.md", "a", encoding="utf-8") as fh:
            fh.write(NEW_TURN_2)
        self.collect("2026-09-27T12:00:00Z")
        snaps = self.spec_snapshots()
        self.assertEqual(len(snaps), 2, self.snapshots())
        grown = [p for p in snaps if "Теперь деление" in p.read_text(encoding="utf-8")]
        self.assertEqual(len(grown), 1)
        self.assertIn(OTHER_ASK, grown[0].read_text(encoding="utf-8"))


class ReplaceTest(CollectBase):
    """I3: `--replace <имя>` снимает защиту от усыхания; предупреждение даёт рецепт."""

    def shrink(self) -> Path:
        self.collect()
        src = self.src / "specstory-claude.md"
        text = src.read_text(encoding="utf-8")
        src.write_text(text[: text.index("_**User (2026-09-01 10:04:00Z)**_")], encoding="utf-8")
        return self.me / f"{SPECSTORY_SESSION}.md"

    def test_shrink_warning_has_recipe(self):
        snap = self.shrink()
        out = self.collect(LATER)
        self.assertTrue(any(f"--replace {snap.name}" in w for w in out["warnings"]), out["warnings"])

    def test_replace_overwrites_shrunk_snapshot(self):
        snap = self.shrink()
        out = self.collect(LATER, replace=[snap.name])
        self.assertNotIn("10:04:00Z", snap.read_text(encoding="utf-8"))
        self.assertLess(self.rec(SPECSTORY_SESSION)["turns_total"], 8)
        self.assertFalse(any("меньше" in w for w in out["warnings"]), out["warnings"])

    def test_replace_unknown_name_warns(self):
        self.collect()
        out = self.collect(LATER, replace=["nope.md"])
        self.assertTrue(any("--replace nope.md" in w for w in out["warnings"]), out["warnings"])

    def test_cli_replace_repeatable(self):
        snap = self.shrink()
        res = run_sr("collect", "--json", "--replace", snap.name, "--replace", "nope.md",
                     "--workspace", str(self.root), home=self.home, cwd=self.tmp)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("10:04:00Z", snap.read_text(encoding="utf-8"))


class PortablePathsTest(CollectBase):
    """I6: в `.meta.json` и `logs-index.json` нет абсолютных путей с домашней папкой."""

    def test_no_home_in_meta_and_index(self):
        inside = self.root / "ai-logs"
        inside.mkdir()
        shutil.copy(LOGS / "specstory-ru-roles.md", inside / "ru.md")
        cfg = json.loads(json.dumps(self.cfg))
        cfg["sources"] += [{"type": "dir", "path": "ai-logs", "person": "me"},
                           {"type": "claude-jsonl", "project": str(self.home / "nowhere"),
                            "person": "me"}]
        self.write_cfg(cfg)
        out = self.collect()
        raw = (self.me / ".meta.json").read_text(encoding="utf-8") + \
            (self.root / ".sessionretro" / "logs-index.json").read_text(encoding="utf-8")
        for home in {str(Path.home()), str(Path.home().resolve())}:
            self.assertNotIn(home, raw)
        meta = inventory.load_meta(self.me)
        paths = {e["source_path"] for e in meta.values()}
        self.assertIn("~/.claude/projects/-home-user-proj/claude-main.jsonl", paths)
        self.assertIn("ai-logs/ru.md", paths)
        self.assertTrue(any("~/.claude/projects/" in w for w in self.index()["warnings"]),
                        self.index()["warnings"])
        self.assertTrue(any(r["file"].startswith("~/") for r in out["sanitized"]), out["sanitized"])
        before = self.mtimes()                       # сравнение по meta работает с этой формой
        with mock.patch.object(normalize, "parse_file", wraps=normalize.parse_file) as pf:
            self.collect(LATER)
        self.assertEqual(self.mtimes(), before)
        self.assertFalse(any(str(c.args[0]).startswith(str(inside)) for c in pf.call_args_list))


class RegistryAndSpecstoryTest(CollectBase):
    def test_registered_last_collect_at(self):
        entry = registry.register(self.root, "Test", "self", ["default"])
        out = self.collect()
        self.assertEqual(registry.find_by_path(self.root)["last_collect_at"], NOW)
        self.assertEqual(entry["id"], registry.find_by_path(self.root)["id"])
        self.assertFalse(any("реестр" in w for w in out["warnings"]), out["warnings"])

    def test_unregistered_warns(self):
        out = self.collect()
        self.assertTrue(any("реестр" in w for w in out["warnings"]), out["warnings"])
        self.assertTrue(any("реестр" in w for w in self.index()["warnings"]))

    def test_specstory_missing_is_summary_line(self):
        hist = self.root / ".specstory" / "history"
        hist.mkdir(parents=True)
        cfg = dict(self.cfg)
        cfg["sources"] = [*cfg["sources"], {"type": "specstory", "path": ".specstory/history",
                                            "person": "me"}]
        self.write_cfg(cfg)
        with mock.patch("lib.specstory.shutil.which", return_value=None) as which:
            out = self.collect()
        self.assertTrue(which.called)
        self.assertEqual(out["specstory"], ["specstory не найден, sync пропущен"])
        self.assertEqual(out["counts"]["new"], 5)

    def test_specstory_providers_flag(self):
        cfg = dict(self.cfg)
        cfg["sources"] = [{"type": "specstory", "path": str(self.src), "person": "me"}]
        self.write_cfg(cfg)
        with mock.patch("lib.specstory.shutil.which", return_value=None):
            out = self.collect(providers=["codex", "claude"])
        self.assertEqual(len(out["specstory"]), 2)

    def test_no_specstory_source_no_probe(self):
        with mock.patch("lib.specstory.shutil.which", return_value=None) as which:
            self.collect()
        which.assert_not_called()


class DoctorLinesTest(CollectBase):
    def test_no_workspace(self):
        self.assertEqual(cmd_collect.doctor_lines(None),
                         [("last_snapshot_at", "нет workspace"), ("logs_indexed", "нет workspace")])

    def test_before_and_after_collect(self):
        self.assertEqual(cmd_collect.doctor_lines(self.root),
                         [("last_snapshot_at", "нет"), ("logs_indexed", 0)])
        self.collect()
        self.assertEqual(cmd_collect.doctor_lines(self.root),
                         [("last_snapshot_at", NOW), ("logs_indexed", 5)])


class CliTest(CollectBase):
    def sr(self, *args: str):
        return run_sr(*args, "--workspace", str(self.root), home=self.home, cwd=self.tmp)

    def test_json(self):
        res = self.sr("collect", "--json")
        self.assertEqual(res.returncode, 0, res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(data["counts"]["new"], 5)
        self.assertTrue(data["last_snapshot_at"].endswith("Z"))

    def test_quiet_prints_nothing(self):
        res = self.sr("collect", "--quiet")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, "")
        self.assertEqual(res.stderr, "")
        self.assertEqual(len(self.index()["logs"]), 5)

    def test_text_summary(self):
        res = self.sr("collect")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("новых: 5", res.stdout)
        self.assertIn("secret.md", res.stdout)
        self.assertIn("предупреждения", res.stdout)

    def test_error_to_stderr_code1(self):
        cfg = dict(self.cfg)
        cfg["sources"] = [{"type": "dir", "path": str(self.src)}, {"type": "dir", "path": "x"}]
        cfg["sources"][0].pop("person", None)
        (self.root / ".sessionretro" / "people" / "other.yaml").write_text(
            'id: other\nname: "Other One"\ngroup: default\n', encoding="utf-8")
        self.write_cfg(cfg)
        res = self.sr("collect", "--quiet")
        self.assertEqual(res.returncode, 1)
        self.assertEqual(res.stdout, "")
        self.assertIn("без person", res.stderr)

    def test_provider_flag_repeatable(self):
        res = self.sr("collect", "--json", "--provider", "claude", "--provider", "codex")
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_doctor_shows_lines(self):
        self.sr("collect", "--quiet")
        res = self.sr("doctor", "--json")
        self.assertEqual(res.returncode, 0, res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(data["logs_indexed"], 5)
        self.assertTrue(data["last_snapshot_at"].endswith("Z"))


if __name__ == "__main__":
    unittest.main()
