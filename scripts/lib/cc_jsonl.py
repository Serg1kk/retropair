"""Нативный JSONL Claude Code (`~/.claude/projects/<папка>/<sessionId>.jsonl`) → документ формата
`claude-jsonl` с теми же ключами, что у `normalize.parse`, плюс шесть доп. метрик.

Устройство файла:

- строка = JSON-объект с `type`; реплики — `user` / `assistant` / `system`, остальное — метастроки
  (заголовки, режимы, снимки файлов, `attachment`), они пропускаются молча;
- один API-ответ записан несколькими строками `assistant` с одним `message.id` (по блоку на строку),
  `usage` на всех строках одинаковый, кроме растущего `output_tokens`: строки склеиваются, `usage`
  берётся с последней строки ответа — сумма по строкам задваивает токены;
- `user`-строка с блоками `tool_result` — ответ инструмента, а не человек; реплика человека несёт
  `origin.kind == "human"`, у старых логов без `origin` работает эвристика;
- сабагенты лежат рядом: `<stem>/subagents/agent-<id>.jsonl` + `.meta.json` (у старых версий —
  inline-строками с `isSidechain` в главном файле; читаются обе части, дубли по `uuid` не считаются);
  их ходы помечены `sidechain: true`.

Порядок ходов — по времени: записи основной ветки и сабагентов сливаются по `timestamp` (при
равенстве — основная ветка, затем сабагенты по первому появлению, внутри потока — порядок в файле;
строка без времени держится за соседом по своему потоку), заголовок сабагента встаёт перед его первой
записью. Номер хода — по его первой записи. Файлы только дописываются, у новых строк время больше
прежних, поэтому у выросшей сессии первые N ходов сохраняют номера.

Отказ в инструменте (`toolDenialKind`) — в `tool_denials{вид: n}`; в `interrupts` идёт только
`user-rejected` и только если тот же ход потом не прерван (иначе событие считалось бы дважды):
`permission-rule` и `automode-blocked` — конфигурация, а не поведение человека.

Ход агента — всё, что агент сделал между двумя репликами (человека или харнесса): несколько
API-ответов, их текст и вызовы. `ts` такого хода — время его последней строки, чтобы пауза до
следующей реплики человека считалась от конца работы агента.

Компактный снапшот (`compact_snapshot`) — строки `user/assistant/system` главной сессии, затем
сабагентов (с `agentId` и `agentType`), без дублей по `uuid`, без `thinking`-текста и с обрезанными
выводами. Разбор снапшота даёт те же ходы и метрики, что разбор исходника с папкой сабагентов.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from lib import normalize
from lib.workspace import SrError

FORMAT = "claude-jsonl"
SUBAGENTS_INLINE_KEY = "agentId"
AGENT_TYPE_KEY = "agentType"
CONVERSATION_TYPES = {"user", "assistant", "system"}
# Метастроки: пропуск без счёта. `summary` — тип старых версий, держится в списке известных.
KNOWN_META_TYPES = {
    "attachment", "last-prompt", "mode", "atis-latch", "permission-mode", "agent-name",
    "custom-title", "queue-operation", "ai-title", "file-history-snapshot", "file-history-delta",
    "relocated", "worktree-state", "frame-link", "cost-state", "pr-link", "bridge-session",
    "continued-in", "fork-context-ref", "summary",
}
KNOWN_META_PREFIXES = ("artifact-",)

SHELL_NAMES = {"Bash", "PowerShell"}
WRITE_NAMES = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
READ_NAMES = {"Read", "Grep", "Glob"}

# Не человек: `origin.kind` → причина вставки. Неизвестный вид — системная вставка.
ORIGIN_REASONS = {
    "peer": "agent_message",
    "coordinator": "agent_message",
    "task-notification": "system_injection",
    "auto-continuation": "system_injection",
}
INTERRUPT_PREFIX = "[Request interrupted"
PEER_PREFIX = "Another Claude session"

DETECT_LINES = 50
OUTPUT_HEAD = 600
TOOL_RESULT_LIMIT = 4000
CUT_NOTE = "…[обрезано {n}]"
CUT_NOTE_RE = re.compile(r"…\[обрезано (\d+)\]")
USER_REJECTED = "user-rejected"
TS_MIN = datetime.min.replace(tzinfo=timezone.utc)


# ── Детект ───────────────────────────────────────────────────────────────────


def _lines(text: str) -> list[str]:
    """Строки JSONL делятся только по `\n`: `str.splitlines` режет и по U+2028/U+2029/U+0085,
    которые JSON без экранирования оставляет внутри строк."""
    return text.split("\n")


def _dumps(obj) -> str:
    """JSON-строка снапшота: разделители строк Unicode экранируются, чтобы файл читался
    построчно любым способом."""
    return (json.dumps(obj, ensure_ascii=False)
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029").replace("\x85", "\\u0085"))


def _load(line: str):
    try:
        return json.loads(line)
    except ValueError:
        return None


def looks_like(text: str) -> bool:
    """Первая непустая строка — JSON-объект с `type`, и среди первых 50 непустых строк есть
    объект с `sessionId` или `uuid` (файл может начинаться со снимка файлов без них)."""
    lines = [ln for ln in text.split("\n", DETECT_LINES * 4)[:DETECT_LINES * 4] if ln.strip()]
    if not lines:
        return False
    first = _load(lines[0])
    if not isinstance(first, dict) or "type" not in first:
        return False
    for ln in lines[:DETECT_LINES]:
        obj = _load(ln)
        if isinstance(obj, dict) and ("sessionId" in obj or "uuid" in obj):
            return True
    return False


# ── Чтение строк ─────────────────────────────────────────────────────────────


def _is_meta_type(t: str) -> bool:
    return t in KNOWN_META_TYPES or t.startswith(KNOWN_META_PREFIXES)


class _Reader:
    """Разбирает строки в записи реплик и считает пропуски."""

    def __init__(self) -> None:
        self.unparsed = 0
        self.unknown: dict[str, int] = {}
        self.seen_uuids: set[str] = set()

    def records(self, text: str) -> list[dict]:
        out = []
        for raw in _lines(text):
            if not raw.strip():
                continue
            obj = _load(raw)
            if not isinstance(obj, dict) or not isinstance(obj.get("type"), str):
                self.unparsed += 1
                continue
            t = obj["type"]
            if t not in CONVERSATION_TYPES:
                if not _is_meta_type(t):
                    self.unknown[t] = self.unknown.get(t, 0) + 1
                continue
            # продолженные и форкнутые сессии копируют строки: дубль по uuid не считается
            uid = obj.get("uuid")
            if isinstance(uid, str):
                if uid in self.seen_uuids:
                    continue
                self.seen_uuids.add(uid)
            out.append(obj)
        return out


def subagents_dir(main: Path) -> Path:
    return Path(main).with_suffix("") / "subagents"


def _agent_id_from_name(path: Path) -> str:
    stem = path.stem
    return stem[len("agent-"):] if stem.startswith("agent-") else stem


def _subagent_files(main: Path) -> list[Path]:
    d = subagents_dir(main)
    return sorted(d.glob("agent-*.jsonl")) if d.is_dir() else []


def _read_meta(sub: Path) -> tuple[str | None, bool]:
    """`agentType` из `.meta.json` рядом с файлом сабагента; (тип, битый ли файл)."""
    meta = sub.with_suffix(".meta.json")
    if not meta.is_file():
        return None, False
    obj = _load(meta.read_text(encoding="utf-8", errors="replace"))
    if not isinstance(obj, dict):
        return None, True
    at = obj.get("agentType")
    return (at if isinstance(at, str) and at else None), False


def _load_subagents(main: Path, reader: _Reader) -> tuple[list[dict], dict[str, str]]:
    """Записи сабагентов из файлов: у каждой `agentId` (из строки или имени файла) и
    `agentType` из `.meta.json`, если он есть. Второе значение — {agentId файла: agentType}."""
    out = []
    types: dict[str, str] = {}
    for sub in _subagent_files(main):
        agent_type, broken = _read_meta(sub)
        if broken:
            reader.unparsed += 1
        agent_id = _agent_id_from_name(sub)
        if agent_type:
            types[agent_id] = agent_type
        for rec in reader.records(sub.read_text(encoding="utf-8", errors="replace")):
            rec.setdefault(SUBAGENTS_INLINE_KEY, agent_id)
            rec["isSidechain"] = True
            if agent_type and AGENT_TYPE_KEY not in rec:
                rec[AGENT_TYPE_KEY] = agent_type
            out.append(rec)
    return out, types


def _apply_types(records: list[dict], types: dict[str, str]) -> None:
    """`agentType` из `.meta.json` достаётся и inline-строкам того же сабагента: их копии из
    папки отброшены как дубли по `uuid`."""
    for rec in records:
        aid = rec.get(SUBAGENTS_INLINE_KEY)
        if rec.get("isSidechain") and isinstance(aid, str) and aid in types:
            rec.setdefault(AGENT_TYPE_KEY, types[aid])


# ── Содержимое строк ─────────────────────────────────────────────────────────


def _blocks(rec: dict) -> list:
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return content if isinstance(content, list) else []


def _user_text(blocks: list) -> str:
    parts = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text" and isinstance(b.get("text"), str):
            parts.append(b["text"])
        elif b.get("type") == "image":
            parts.append("[image]")
    return "\n".join(parts).strip()


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b["text"] for b in content
                         if isinstance(b, dict) and b.get("type") == "text"
                         and isinstance(b.get("text"), str))
    return ""


def _result_chars(out: str) -> int:
    """Длина вывода до обрезки: снапшот режет длинный вывод и пишет, сколько срезано."""
    notes = list(CUT_NOTE_RE.finditer(out))
    return len(out) - sum(len(m.group(0)) for m in notes) + sum(int(m.group(1)) for m in notes)


def tool_type(name: str) -> str:
    if name in SHELL_NAMES:
        return "shell"
    if name in WRITE_NAMES:
        return "write"
    if name in READ_NAMES:
        return "read"
    if name.startswith("mcp__"):
        return "mcp"
    return "tool"


def _user_reason(rec: dict, text: str) -> str | None:
    """Причина, по которой user-строка не реплика человека; None — человек."""
    if rec.get("isCompactSummary"):
        return "compact_summary"
    if "<command-name>" in text:
        return "slash_command"
    origin = rec.get("origin")
    kind = origin.get("kind") if isinstance(origin, dict) else None
    if kind == "human":
        return None
    if kind is not None:
        return ORIGIN_REASONS.get(kind, "system_injection")
    # логи без origin: эвристика
    if rec.get("isMeta"):
        return "system_injection"
    s = text.lstrip()
    if s.startswith(PEER_PREFIX):
        return "agent_message"
    if s.startswith("<pasted_content"):
        return None
    if s.startswith("<"):
        return normalize.synthetic_reason(s) or "system_injection"
    return normalize.synthetic_reason(s)


# ── Сборка ходов ─────────────────────────────────────────────────────────────


class _Stream:
    """Состояние одной ветки: основной или одного сабагента."""

    def __init__(self, sidechain: bool, agent_id: str | None = None,
                 agent_type: str | None = None, first_ts: str | None = None) -> None:
        self.sidechain = sidechain
        self.agent_id = agent_id
        self.agent_type = agent_type
        self.first_ts = first_ts
        self.started = False
        self.run: dict | None = None           # текущий ход агента
        self.last_agent: dict | None = None


def _streams(records: list[dict]) -> list[tuple[_Stream, list[dict]]]:
    """Основная ветка, затем сабагенты по первому появлению `agentId`."""
    main = [r for r in records if not r.get("isSidechain")]
    groups: dict[str, list[dict]] = {}
    for rec in records:
        if rec.get("isSidechain"):
            groups.setdefault(str(rec.get(SUBAGENTS_INLINE_KEY) or "?"), []).append(rec)
    out = [(_Stream(False), main)]
    for agent_id, recs in groups.items():
        agent_type = next((r[AGENT_TYPE_KEY] for r in recs
                           if isinstance(r.get(AGENT_TYPE_KEY), str)), None)
        first_ts = next((r.get("timestamp") for r in recs if r.get("timestamp")), None)
        out.append((_Stream(True, agent_id, agent_type, first_ts), recs))
    return out


def _ordered(streams: list[tuple[_Stream, list[dict]]]):
    """Записи всех веток по времени. Ключ — (время, ранг ветки, позиция в ветке); время внутри
    ветки не убывает (берётся максимум с начала ветки), поэтому порядок строк в файле сохраняется,
    а строка без времени идёт за своим соседом."""
    seqs = []
    for rank, (st, recs) in enumerate(streams):
        eff = TS_MIN
        seq = []
        for pos, rec in enumerate(recs):
            ts = rec.get("timestamp")
            dt = normalize.parse_ts(ts) if isinstance(ts, str) else None
            if dt is not None and dt > eff:
                eff = dt
            seq.append((eff, rank, pos, st, rec))
        seqs.append(seq)
    for _, _, _, st, rec in heapq.merge(*seqs, key=lambda x: (x[0], x[1], x[2])):
        yield st, rec


class _Builder:
    def __init__(self, keep_tool_output: bool) -> None:
        self.keep_tool_output = keep_tool_output
        self.items: list[dict] = []        # черновики ходов
        self.tools_by_id: dict[str, dict] = {}
        self.models: list[str] = []
        self.usage_main: dict[str, dict] = {}
        self.denials: list[tuple[str, dict | None]] = []   # (вид, ход агента)
        self.compactions = 0
        self.stamps: list[str] = []
        self.session_id: str | None = None
        self.version: str | None = None
        self._side_session_id: str | None = None
        self._side_version: str | None = None

    def _item(self, role: str, *, ts=None, sidechain=False, syn=None, text="") -> dict:
        item = {"role": role, "parts": [text] if text else [], "tools": [], "ts": ts,
                "model": None, "interrupted": False, "sidechain": sidechain, "syn": syn}
        self.items.append(item)
        return item

    def _note_common(self, rec: dict, sidechain: bool) -> None:
        """Время строки в охват сессии; `sessionId` и версия — с основной ветки, сабагенты —
        запасной источник."""
        ts = rec.get("timestamp")
        if isinstance(ts, str) and ts:
            self.stamps.append(ts)
        sid, ver = rec.get("sessionId"), rec.get("version")
        if sidechain:
            if self._side_session_id is None and isinstance(sid, str):
                self._side_session_id = sid
            if self._side_version is None and isinstance(ver, str):
                self._side_version = ver
            return
        if self.session_id is None and isinstance(sid, str):
            self.session_id = sid
        if self.version is None and isinstance(ver, str):
            self.version = ver

    def build(self, records: list[dict]) -> int:
        """Ходы всех веток по времени; возвращает число сабагентов."""
        streams = _streams(records)
        for st, rec in _ordered(streams):
            self.feed(st, rec)
        self.session_id = self.session_id or self._side_session_id
        self.version = self.version or self._side_version
        return len(streams) - 1

    def feed(self, st: _Stream, rec: dict) -> None:
        sidechain = st.sidechain
        if sidechain and not st.started:
            st.started = True
            head = f"subagent {st.agent_id}" + (f" · {st.agent_type}" if st.agent_type else "")
            self._item("assistant", ts=st.first_ts, sidechain=True, text=head)
        t = rec["type"]
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
        if t == "assistant":
            if msg.get("model") == "<synthetic>" or rec.get("isApiErrorMessage"):
                return                      # ошибка API харнесса: не ответ, usage нулевой
            self._note_common(rec, sidechain)
            if st.run is None:
                st.run = self._item("assistant", sidechain=sidechain)
            run = st.last_agent = st.run
            run["ts"] = rec.get("timestamp") or run["ts"]
            model = msg.get("model")
            if isinstance(model, str) and model:
                run["model"] = run["model"] or model
                if model not in self.models:
                    self.models.append(model)
            for b in _blocks(rec):
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text" and isinstance(b.get("text"), str) and b["text"].strip():
                    run["parts"].append(b["text"].strip())
                elif bt in ("tool_use", "server_tool_use"):
                    name = str(b.get("name") or "tool")
                    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                    cmd = inp.get("command") if tool_type(name) == "shell" else None
                    tool = {"name": name, "type": tool_type(name),
                            "command": cmd if isinstance(cmd, str) and cmd.strip() else None,
                            "output_head": None, "output_chars": 0, "is_error": False}
                    run["tools"].append(tool)
                    if isinstance(b.get("id"), str):
                        self.tools_by_id[b["id"]] = tool
            usage = msg.get("usage")
            if not sidechain and isinstance(usage, dict):
                key = msg.get("id") or rec.get("uuid")
                self.usage_main[str(key)] = usage     # последняя строка ответа побеждает
        elif t == "user":
            blocks = _blocks(rec)
            results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
            text = _user_text(blocks)
            kind = rec.get("toolDenialKind")
            if kind:
                self.denials.append((str(kind), st.run or st.last_agent))
            for b in results:
                tool = self.tools_by_id.get(b.get("tool_use_id"))
                if tool is None:
                    continue
                out = _result_text(b.get("content"))
                tool["output_chars"] = _result_chars(out)
                tool["output_head"] = (out[:OUTPUT_HEAD] or None) if self.keep_tool_output else None
                tool["is_error"] = bool(b.get("is_error"))
            if text.startswith(INTERRUPT_PREFIX):
                # прерывание — пометка хода агента, а не реплика
                self._note_common(rec, sidechain)
                if st.last_agent is not None:
                    st.last_agent["interrupted"] = True
                st.run = None
                return
            if results:
                self._note_common(rec, sidechain)
                return
            if not text:
                return
            self._note_common(rec, sidechain)
            st.run = None
            if sidechain:
                # в сабагенте человека нет: промпт задачи и сообщения пишет агент
                self._item("assistant", ts=rec.get("timestamp"), sidechain=True,
                           syn="agent_message", text=text)
            else:
                self._item("user", ts=rec.get("timestamp"), syn=_user_reason(rec, text), text=text)
        elif t == "system":
            if rec.get("subtype") != "compact_boundary":
                return
            self._note_common(rec, sidechain)
            st.run = None
            if not sidechain:
                self.compactions += 1
            self._item("system", ts=rec.get("timestamp"), sidechain=sidechain,
                       syn="compact", text="compact")

    def denial_counts(self) -> tuple[dict[str, int], int]:
        """({вид: n} по всем отказам, сколько из них — остановка агента человеком): отказ
        человека, за которым тот же ход прерван, — одно событие, его уже считает `interrupted`."""
        counts: dict[str, int] = {}
        stops = 0
        for kind, item in self.denials:
            counts[kind] = counts.get(kind, 0) + 1
            if kind == USER_REJECTED and not (item is not None and item["interrupted"]):
                stops += 1
        return counts, stops

    def turns(self, max_turn_chars: int) -> list[dict]:
        out = []
        for i, it in enumerate(self.items):
            text = "\n\n".join(it["parts"])
            out.append(normalize._turn(i, it["role"], normalize._truncate(text, max_turn_chars),
                                       len(text), tools=it["tools"], ts=it["ts"],
                                       model=it["model"], interrupted=it["interrupted"],
                                       sidechain=it["sidechain"], syn=it["syn"]))
        return out


def _span(stamps: list[str]) -> tuple[str | None, str | None]:
    parsed = [(normalize.parse_ts(s), s) for s in stamps]
    parsed = [(dt, s) for dt, s in parsed if dt is not None]
    if not parsed:
        return None, None
    return min(parsed)[1], max(parsed)[1]


def parse(text: str, path: str | Path | None = None, max_turn_chars: int = 0,
          keep_tool_output: bool = True) -> dict:
    """Документ `claude-jsonl`. `path` нужен только чтобы найти `<stem>/subagents/`: папка
    читается всегда, когда есть, вместе с inline-строками сабагентов главного файла; дубли по
    `uuid` отбрасываются (снапшот уже несёт сабагентов внутри, папки рядом с ним нет)."""
    reader = _Reader()
    records = reader.records(text)
    if path is not None:
        folder, types = _load_subagents(Path(path), reader)
        records += folder
        _apply_types(records, types)

    b = _Builder(keep_tool_output)
    b.build(records)
    turns = b.turns(max_turn_chars)

    started_at, ended_at = _span(b.stamps)
    doc = normalize._document(
        FORMAT, turns, text,
        collector=f"Claude Code {b.version}" if b.version else "Claude Code",
        session_id=b.session_id, started_at=started_at,
        started_at_source="record_ts" if started_at else None,
        ended_at=ended_at, models=b.models)

    mm = doc["mechanical_metrics"]
    denials, stops = b.denial_counts()
    mm["interrupts"] += stops                     # отказ человека — тоже остановка агента
    mm["tool_denials"] = denials
    # номер хода — по первой записи, а `ts` — по последней, поэтому длительность — по min/max
    # времени строк
    mm["session_span_seconds"] = normalize.seconds_between(started_at, ended_at)
    ctx = [int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0)
           + int(u.get("cache_creation_input_tokens") or 0) for u in b.usage_main.values()]
    reads = sum(int(u.get("cache_read_input_tokens") or 0) for u in b.usage_main.values())
    mm["unparsed_lines"] = reader.unparsed
    mm["unknown_types"] = dict(reader.unknown)
    mm["context_peak_tokens"] = max(ctx) if ctx else None
    mm["cache_share"] = round(reads / sum(ctx), 3) if ctx and sum(ctx) else None
    mm["compactions"] = b.compactions
    mm["subagent_files"] = len({str(r.get(SUBAGENTS_INLINE_KEY)) for r in records
                                if r.get("isSidechain") and r.get(SUBAGENTS_INLINE_KEY)})
    return doc


# ── Снапшот ──────────────────────────────────────────────────────────────────


def _cut(s: str) -> str:
    if len(s) <= TOOL_RESULT_LIMIT:
        return s
    return s[:TOOL_RESULT_LIMIT] + CUT_NOTE.format(n=len(s) - TOOL_RESULT_LIMIT)


def _compact_block(b):
    if not isinstance(b, dict):
        return b
    bt = b.get("type")
    if bt == "thinking":
        return {"type": "thinking", "chars": len(b.get("thinking") or "")}
    if bt == "redacted_thinking":
        return {"type": "redacted_thinking", "chars": len(b.get("data") or "")}
    if bt == "image":
        return {"type": "image"}              # base64 картинки не нужен разбору и тяжёл
    if bt == "tool_result":
        b = dict(b)
        c = b.get("content")
        if isinstance(c, str):
            b["content"] = _cut(c)
        elif isinstance(c, list):
            b["content"] = [_compact_block(x) if not (isinstance(x, dict) and x.get("type") == "text")
                            else {**x, "text": _cut(x.get("text") or "")} for x in c]
    return b


def _compact_line(rec: dict) -> dict:
    rec = dict(rec)
    rec.pop("toolUseResult", None)            # дубль tool_result целиком, без обрезки
    msg = rec.get("message")
    if isinstance(msg, dict) and isinstance(msg.get("content"), list):
        rec["message"] = {**msg, "content": [_compact_block(b) for b in msg["content"]]}
    return rec


def _conversation_lines(text: str) -> list[dict]:
    out = []
    for raw in _lines(text):
        obj = _load(raw) if raw.strip() else None
        if isinstance(obj, dict) and obj.get("type") in CONVERSATION_TYPES:
            out.append(obj)
    return out


def compact_snapshot(main: Path, sanitize_obj: Callable[[dict], tuple[dict, dict]]
                     ) -> tuple[str, dict, str]:
    """Компактный снапшот сессии: (текст — строки JSON через `\\n`, суммарные hits санитайзера,
    sha256 исходника = главный файл + файлы сабагентов по отсортированным именам).

    `sanitize_obj` получает строку целиком (объект) и возвращает (объект, {правило: n})."""
    main = Path(main)
    if not main.is_file():
        raise SrError(f"cc_jsonl: нет файла сессии для снапшота ({main})", 1)
    subs = _subagent_files(main)
    sha = hashlib.sha256()
    main_bytes = main.read_bytes()
    sha.update(main_bytes)
    # то же правило, что в `parse`: главный файл (с inline-сабагентами) плюс папка, дубль по uuid
    # не пишется
    seen: set[str] = set()

    def fresh(r: dict) -> bool:
        uid = r.get("uuid")
        if not isinstance(uid, str):
            return True
        if uid in seen:
            return False
        seen.add(uid)
        return True

    rows = [_compact_line(r) for r in _conversation_lines(main_bytes.decode("utf-8", errors="replace"))
            if fresh(r)]
    types: dict[str, str] = {}
    for sub in subs:
        data = sub.read_bytes()
        sha.update(data)
        agent_type, _ = _read_meta(sub)
        agent_id = _agent_id_from_name(sub)
        if agent_type:
            types[agent_id] = agent_type
        for r in _conversation_lines(data.decode("utf-8", errors="replace")):
            if not fresh(r):
                continue
            r = _compact_line(r)
            r.setdefault(SUBAGENTS_INLINE_KEY, agent_id)
            r["isSidechain"] = True
            if agent_type:
                r.setdefault(AGENT_TYPE_KEY, agent_type)
            rows.append(r)
    _apply_types(rows, types)

    hits: dict[str, int] = {}
    lines = []
    for r in rows:
        res = sanitize_obj(r)
        if not (isinstance(res, tuple) and len(res) == 2 and isinstance(res[0], dict)
                and isinstance(res[1], dict)):
            raise SrError(f"cc_jsonl: санитайзер вернул не пару (объект, hits) "
                          f"(тип {type(res).__name__}, файл {main})", 1)
        obj, h = res
        for rule, n in h.items():
            hits[rule] = hits.get(rule, 0) + int(n)
        lines.append(_dumps(obj))
    text = "\n".join(lines) + ("\n" if lines else "")
    return text, hits, sha.hexdigest()
