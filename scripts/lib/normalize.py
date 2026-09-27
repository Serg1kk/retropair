"""Нормализатор лога сессии: markdown/текст лога → документ с `turns[]` и механическими метриками.

Индекс хода считается от начала файла: у выросшей сессии первые N ходов сохраняют номера,
дописанный хвост получает новые. На индекс ссылаются цитаты оценки, поэтому он стабилен.

Форматы (детект по содержимому, расширение не смотрится; порядок проверки — `FORMATS`):

- `jsonl-export` — экспорт native JSONL заголовками с временем (`## user — <ISO>`,
  `### Tool call: <имя> — <ISO>`), шапка `# codex|claude|… — <ISO>` + «Offline export of … JSONL records»;
- `cc-scrollback` — очищенный скроллбек терминала Claude Code (`❯` человек, `●` агент, `⎿` результат);
- `cursor-export` — выгрузка чата Cursor (`**User**` / `**Cursor**` отдельными строками);
- `md-headings` — ручной экспорт заголовками (`## User` / `## Assistant`);
- `md-bold-roles` — экспорт с ролями жирным (`**Имя:**` / `**Claude:**`);
- `specstory` — SpecStory markdown (`_**User (…)**_` / `_**Agent (…)**_`), в том числе вариант
  с ролями `_**Человек**_` / `_**Агент**_` и блоками `<details><summary>Инструмент:`; он же фоллбек.

Не всякий ход с role: user написан человеком: вставки харнесса (текст скилла, сводка компакта,
разворот слэш-команды, вывод локальной команды, системная вставка) помечаются
`synthetic: true` с причиной и не попадают в `user_turns`.

«Не измерено» — всегда `None`, а не 0: ноль читается как «такого поведения не было».
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from lib.workspace import SrError

# Метрики сверх базовых 19. Их считает только парсер нативного JSONL Claude Code; у остальных
# форматов в `mechanical_metrics` они есть и равны None.
EXTRA_METRIC_KEYS = ("unparsed_lines", "unknown_types", "context_peak_tokens", "cache_share",
                     "compactions", "subagent_files", "tool_denials")

TRUNCATED_NOTE = "\n…[обрезано, всего {chars} знаков]"

# ── SpecStory ────────────────────────────────────────────────────────────────

SESSION_RE = re.compile(
    r"<!--\s*(?P<collector>[A-Za-z0-9 ._-]+?)\s+Session\s+(?P<sid>[0-9a-fA-F-]{8,})\s*"
    r"(?:\((?P<ts>[^)]*)\))?\s*-->"
)
# `# Сессия 2026-09-12T09:53:26.399Z` — шапка экспорта с русскими ролями.
HEADING_TS_RE = re.compile(
    r"^#\s+(?:Сессия\s+)?(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?)\s*$", re.M)
# Суффикс роли обязателен: SpecStory помечает ходы внутри субагента как
# `_**Agent - sidechain (…)**_`. Без суффикса такой заголовок не матчится, и транскрипт
# субагента молча вклеивается в предыдущий ход.
TURN_RE = re.compile(
    r"^_\*\*(?P<role>User|Agent|Assistant|System|Человек|Агент)"
    r"(?P<suffix>\s*-\s*[A-Za-z][\w -]*?)?"
    r"(?:\s*\((?P<meta>[^)]*)\))?\*\*_\s*$",
    re.M,
)
SIDECHAIN_SUFFIX_RE = re.compile(r"sidechain|subagent", re.I)
TOOL_BLOCK_RE = re.compile(
    r'<tool-use\s+data-tool-type="(?P<ttype>[^"]*)"\s+data-tool-name="(?P<tname>[^"]*)"\s*>'
    r"(?P<body>.*?)</tool-use>",
    re.S,
)
SUMMARY_RE = re.compile(r"<summary>(?P<s>.*?)</summary>", re.S)
# Экспорт с русскими ролями: вызов `<details><summary>Инструмент: <b>Bash</b></summary>`,
# затем описание и забор с командой; вывод — отдельным блоком `Результат:`, приклеивается
# к последнему вызову.
RU_TOOL_BLOCK_RE = re.compile(
    r"<details>\s*<summary>\s*Инструмент:\s*(?:<b>)?(?P<tname>[^<\n]+?)(?:</b>)?\s*</summary>"
    r"(?P<body>.*?)</details>",
    re.S,
)
RU_RESULT_BLOCK_RE = re.compile(
    r"<details>\s*<summary>\s*Результат:[^<\n]*</summary>(?P<body>.*?)</details>", re.S)
RU_SHELL_NAMES = {"Bash", "BashOutput", "PowerShell"}
RU_READ_NAMES = {"Read", "Glob", "Grep", "LS"}
BACKTICK_CMD_RE = re.compile(r"^`(?P<cmd>[^`\n]{2,})`\s*$", re.M)
# Команду каждый коллектор кладёт по-своему: Claude Code — в бэктиках после </summary>,
# Codex — в бэктиках внутри summary, Cursor — открытым текстом после «Run command:» внутри
# summary, с HTML-сущностями и переносами строк.
SUMMARY_BACKTICK_CMD_RE = re.compile(r"`(?P<cmd>[^`]{2,})`", re.S)
SUMMARY_RUN_CMD_RE = re.compile(r"\b(?:Run command|Command|Terminal)\s*:\s*(?P<cmd>.+)",
                                re.S | re.I)
FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*\n(?P<out>.*?)```", re.S)
TS_IN_META_RE = re.compile(r"(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}Z?)")
INTERRUPT_RE = re.compile(r"\[Request interrupted[^\]]*\]")

# ── Классы проверки ──────────────────────────────────────────────────────────

# Команды, которые считаются проверкой результата. Список намеренно широкий: промах в сторону
# «нашли проверку» дешевле, чем в сторону «проверки не было».
VERIFY_CMD_PATTERNS = [
    r"\bpytest\b", r"\bunittest\b", r"\bnpm (run )?test\b", r"\byarn test\b", r"\bpnpm test\b",
    r"\bjest\b", r"\bvitest\b", r"\bmocha\b", r"\bplaywright test\b", r"\bcypress run\b",
    r"\bgo test\b", r"\bcargo test\b",
    # Maven и Gradle зовут с флагами, а на Windows ещё и через переменную с путём к
    # исполняемому файлу (`& $maven.FullName test`).
    r"\b(?:mvn|mvnw(?:\.cmd)?|maven[\w.]*)\b[^\n]{0,80}?\b(?:test|verify)\b",
    r"\bgradlew?(?:\.bat)?\b[^\n]{0,80}?\b(?:test|check)\b",
    r"\bdotnet test\b", r"\brspec\b", r"\bphpunit\b",
    r"\bnpm run build\b", r"\byarn build\b", r"\bpnpm build\b", r"\bmake (test|check|build)\b",
    r"\btsc\b", r"\bruff\b", r"\bflake8\b", r"\bmypy\b", r"\beslint\b", r"\bbandit\b",
    r"\bcurl\b", r"\bhttpie\b", r"\bhttp [A-Z]+ ", r"\bnode --check\b", r"\bpython -m py_compile\b",
    r"\bdocker compose (up|run)\b", r"\balembic (upgrade|check)\b",
]
VERIFY_CMD_RE = re.compile("|".join(VERIFY_CMD_PATTERNS), re.I)

# Проверка — не только тестовый фреймворк: поднять сервис и посмотреть на него тоже
# наблюдение результата. `git status` / `git diff` сюда не входят: срабатывают у всех и всегда
# и ничего не говорят о том, работает ли код.
RUNTIME_CMD_PATTERNS = [
    r"@modelcontextprotocol/inspector", r"\bmcp[- ]inspector\b", r"\bmcp dev\b",
    r"\buvicorn\b", r"\bgunicorn\b", r"\bflask run\b", r"\bfastapi dev\b",
    r"\bnpm run (dev|start|serve)\b", r"\byarn (dev|start)\b", r"\bpnpm (dev|start)\b",
    r"\buv run\b", r"\bpython -m http\.server\b", r"\bstreamlit run\b",
    r"\bdocker compose (ps|logs|exec)\b", r"\bdocker (ps|logs|exec)\b",
    r"\bpsql\b", r"\bredis-cli\b", r"\bmongosh\b", r"\bsqlite3\b",
]
RUNTIME_CMD_RE = re.compile("|".join(RUNTIME_CMD_PATTERNS), re.I)

# Разовый скрипт вместо теста (`python3 -c "…"`): частая форма проверки там, где тестового
# фреймворка нет. Класс отдельный, чтобы его можно было взвесить иначе, чем прогон тестов.
ADHOC_RUN_PATTERNS = [
    r"\bpython3?\s+-c\b", r"\bpython3?\s+<<", r"\bnode\s+(-e|--eval)\b",
    r"\bdeno eval\b", r"\bruby -e\b", r"\bphp -r\b",
]
ADHOC_RUN_RE = re.compile("|".join(ADHOC_RUN_PATTERNS), re.I)

# Инструменты, которыми смотрят на живую систему: браузер и удалённые MCP-серверы.
UI_TOOL_RE = re.compile(r"browser|computer|playwright|chrome|screenshot|snapshot", re.I)
MCP_TOOL_RE = re.compile(r"^mcp__(?P<server>[^_]+(?:[-_][^_]+)*?)__", re.I)
# Серверы поиска и документации: обращение к ним — ресёрч, а не проверка своего кода.
MCP_RESEARCH_SERVERS = {"exa", "context7", "perplexity", "brave", "brave-search", "fetch",
                        "tavily", "firecrawl", "ref", "deepwiki"}
RESEARCH_TOOL_RE = re.compile(r"^(web_search|web_fetch|WebSearch|WebFetch)$")

# Короткие подтверждения: сигнал к speed_accept, но сами по себе не приговор.
APPROVE_WORDS = {
    "ок", "окей", "ok", "okay", "да", "ага", "угу", "давай", "го", "поехали", "продолжай",
    "yes", "y", "go", "go ahead", "continue", "proceed", "lgtm", "ship it", "мерж", "мержим",
    "апрув", "approve", "approved", "принято", "хорошо", "супер", "отлично", "норм", "sure",
    "next", "дальше", "делай", "sounds good", "+", "++", "ага давай",
}
# Запись определяется прежде всего по data-tool-type="write"/"edit" (SpecStory проставляет его
# у всех коллекторов); список имён — запасной путь.
WRITE_TOOL_TYPES = {"write", "edit"}
TOOLS_WRITING = {"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch", "edit_file",
                 "search_replace", "create_file", "write_file"}
SHELL_TOOL_TYPES = {"shell", "terminal", "command", "bash"}
# Codex через SpecStory: `data-tool-type="unknown"` с таким именем — шелл-вызов всегда, команда
# лежит в `tools.exec_command({cmd:"…"})` или в JSON `{"cmd": "…"}`.
EXEC_TOOL_NAMES = {"exec", "exec_command", "shell", "local_shell"}
# Правка файлов патчем через шелл (`apply_patch <<'PATCH' …`) — запись.
APPLY_PATCH_RE = re.compile(r"^\s*apply_patch\b")

# ── Вставки харнесса ─────────────────────────────────────────────────────────

SYNTHETIC_PATTERNS = [
    ("skill_injection", re.compile(r"^Base directory for this skill:", re.M)),
    ("compact_summary", re.compile(
        r"^(This session is being continued from a previous conversation"
        r"|Caveat: The messages below were generated by the user while running local commands)",
        re.M)),
    ("slash_command", re.compile(r"<command-(?:name|message)>")),
    ("local_command_output", re.compile(r"<local-command-(?:stdout|stderr)>")),
    ("system_injection", re.compile(
        r"<system-reminder>|<task-notification>|<tool_use_error>|<function_results>")),
    # JetBrains/Codex складывает system+developer+environment в сообщение с ролью user:
    # каталог плагинов, файл правил в `<INSTRUCTIONS>`, `<environment_context>`. Это текст
    # вендора целиком.
    ("vendor_context", re.compile(
        r"^<recommended_plugins>|^<INSTRUCTIONS>|^<environment_context>", re.M)),
    ("harness_notice", re.compile(r"^\[Request interrupted[^\]]*\]\s*$")),
    # Отчёты и сообщения субагентов приезжают в ход с ролью user, но их пишет харнесс.
    ("agent_message", re.compile(
        r"<teammate-message\b"
        r"|^Another Claude session sent a message:"
        r"|^\[SYSTEM NOTIFICATION - NOT USER INPUT\]"
        r"|<cross-session-message\b", re.M)),
]


def synthetic_reason(text: str) -> str | None:
    """Почему ход с role: user на самом деле вставка харнесса, а не реплика человека."""
    t = (text or "").strip()
    if not t:
        return None
    for reason, pat in SYNTHETIC_PATTERNS:
        if pat.search(t):
            return reason
    return None


def is_write_tool(tool: dict) -> bool:
    return ((tool.get("type") or "").lower() in WRITE_TOOL_TYPES
            or tool.get("name") in TOOLS_WRITING
            or bool(APPLY_PATCH_RE.match(tool.get("command") or "")))


def is_exec_call(tool_type: str | None, tool_name: str | None) -> bool:
    return (tool_type or "").lower() == "unknown" and (tool_name or "").lower() in EXEC_TOOL_NAMES


def is_shell_tool(tool: dict) -> bool:
    return ((tool.get("type") or "").lower() in SHELL_TOOL_TYPES
            or is_exec_call(tool.get("type"), tool.get("name")))


def extract_command(summary: str | None, after_summary: str, exec_call: bool = False) -> str | None:
    """Команда инструмента SpecStory. Для вызова exec (`exec_call`) сначала ищется
    `exec_command({cmd:"…"})` / `{"cmd": "…"}`. Дальше summary приоритетнее: там коллектор
    команду и объявляет, а после `</summary>` бэктик может оказаться куском вывода."""
    if exec_call:
        cmd = exec_call_command(f"{summary or ''}\n{after_summary}")
        if cmd:
            return cmd
    if summary:
        m = SUMMARY_RUN_CMD_RE.search(summary)         # Cursor
        if m:
            return html.unescape(m.group("cmd")).strip() or None
        m = SUMMARY_BACKTICK_CMD_RE.search(summary)    # Codex
        if m:
            return html.unescape(m.group("cmd")).strip() or None
    m = BACKTICK_CMD_RE.search(after_summary)          # Claude Code
    return m.group("cmd").strip() if m else None


def _truncate(prose: str, max_turn_chars: int) -> str:
    chars = len(prose)
    if max_turn_chars and chars > max_turn_chars:
        return prose[:max_turn_chars] + TRUNCATED_NOTE.format(chars=chars)
    return prose


def _turn(index: int, role: str, text: str, chars: int, *, tools=None, ts=None, model=None,
          interrupted=False, sidechain=False, syn: str | None = None) -> dict:
    return {"index": index, "role": role, "model": model, "ts": ts, "text": text,
            "chars": chars, "tools": tools if tools is not None else [],
            "interrupted": interrupted, "sidechain": sidechain,
            "synthetic": bool(syn), "synthetic_reason": syn}


def _content_id(prefix: str, seed: str) -> str | None:
    return (prefix + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]) if seed else None


SEED_TURNS = 3
SEED_TURN_CHARS = 200
TRUNCATED_NOTE_RE = re.compile(r"\n…\[обрезано, всего \d+ знаков\]$")


def _content_seed(head: str, turns: list[dict], started_at: str | None = None) -> str:
    """Seed ключа для форматов без идентификатора в файле: `head` (первая реплика и то, что
    формат к ней добавляет), первые три хода (роль и начало текста) и время старта, если оно взято
    из содержимого. Разные сессии с одинаковой первой репликой получают разные ключи, а дописанный
    после третьего хода хвост ключ не меняет. Время выгрузки и дата из имени файла сюда не идут:
    повторная выгрузка или переименование — та же сессия."""
    parts = [head] if head else []
    for t in turns[:SEED_TURNS]:
        text = re.sub(r"\s+", " ", TRUNCATED_NOTE_RE.sub("", t["text"] or "")).strip()
        parts.append(t["role"] + ":" + text[:SEED_TURN_CHARS])
    if started_at:
        parts.append(started_at)
    return " | ".join(parts)


def _document(fmt: str, turns: list[dict], text: str, *, collector, session_id, started_at,
              started_at_source, ended_at=None, models=None) -> dict:
    doc = {
        "format": fmt,
        "started_at_source": started_at_source,
        "collector": collector,
        "session_id": session_id,
        "started_at": started_at,
        "ended_at": ended_at,
        "turns_total": len(turns),
        # user_turns — реплики человека; вставки харнесса считаются отдельно
        "user_turns": sum(1 for t in turns if t["role"] == "user" and not t["synthetic"]),
        "synthetic_turns": sum(1 for t in turns if t["synthetic"]),
        "assistant_turns": sum(1 for t in turns if t["role"] == "assistant"),
        # сколько ходов прошло внутри субагентов: факт делегирования, не оценка
        "sidechain_turns": sum(1 for t in turns if t.get("sidechain")),
        "models": models or [],
        "bytes": len(text.encode("utf-8")),
        "turns": turns,
    }
    doc["mechanical_metrics"] = metrics(turns)
    return doc


# ── cc-scrollback: скроллбек терминала Claude Code ───────────────────────────
# Очищенный скроллбек TUI (`/export`), а не структурированный лог:
#   * роли — символ в первой колонке: `❯` человек, `●` агент, `⎿` результат инструмента;
#     продолжение блока отбито двумя пробелами, вывод инструмента — четырьмя и глубже;
#   * таймстемпов нет: `timestamps_available: false`, быстрые апрувы не измерены (None);
#   * вызовы свёрнуты («ran 5 shell commands (ctrl+o to expand)»): свёрнутая активность
#     отдаётся шелл-вызовами без команды, и `metrics()` честно ставит
#     `commands_available: false` и `verification_commands: None`;
#   * `/export` выгружает разговор целиком, поэтому ключ сессии производится из первой
#     содержательной реплики и первых трёх ходов (`_content_seed`), а не из имени файла:
#     повторные выгрузки сводятся в одну запись.
CC_BANNER_RE = re.compile(r"Claude Code v(?P<ver>\d+(?:\.\d+)+)")
CC_MODEL_RE = re.compile(r"^[^\w\n]*(?P<model>[A-Za-z][\w.\- ]{1,30}?)\s+·\s+Claude\s+\w+\s*$", re.M)
CC_CWD_RE = re.compile(r"^\s*▘+\s*▝+\s+(?P<cwd>\S.*?)\s*$", re.M)
CC_USER_RE = re.compile(r"^❯\s?(?P<text>.*)$")
CC_AGENT_RE = re.compile(r"^●\s?(?P<text>.*)$")
CC_RESULT_RE = re.compile(r"^\s*⎿\s?(?P<text>.*)$")
# `Update(README.md)`, `Write(CLAUDE.md)`, `Fetch(https://…)`, `Skill(…)`
CC_TOOL_CALL_RE = re.compile(r"^(?P<name>[A-Z][A-Za-z_]{1,20})\((?P<arg>.*)\)$")
CC_COLLAPSED_RE = re.compile(r"\(ctrl\+o to expand\)$")
# «… +117 lines (ctrl+o to expand)» — обрезанный показ вывода, а не вызов инструмента.
CC_MORE_LINES_RE = re.compile(r"^…\s*\+\d+\s+lines")
CC_INTERRUPT_RE = re.compile(r"^Interrupted\b")
CC_COMMIT_RE = re.compile(r"\bCommitted\s+(?P<sha>[0-9a-f]{7,40})\b")
CC_AGG_PATTERNS = [
    ("shell", "Bash", re.compile(r"\bran (?P<n>\d+) shell commands?\b", re.I)),
    ("read", "Read", re.compile(r"\bread (?P<n>\d+) files?\b", re.I)),
    ("read", "Glob", re.compile(r"\blisted (?P<n>\d+) director(?:y|ies)\b", re.I)),
    ("read", "Grep", re.compile(r"\bsearched for (?P<n>\d+) patterns?\b", re.I)),
    ("write", "Edit", re.compile(r"\bmade (?P<n>\d+) scratchpad edits?\b", re.I)),
]
# Слэш-команда, набранная человеком (`/clear`, `/export`): управление инструментом, а не
# постановка задачи. Разворота слэш-команды в этом формате нет, поэтому пост-проход
# `slash_command_expansion` здесь не запускается.
CC_SLASH_RE = re.compile(r"^/[A-Za-z][\w-]*(?:\s+\S+){0,3}$")
CC_WRITE_NAMES = TOOLS_WRITING | {"Update", "Create", "Patch"}
CC_READ_NAMES = {"Read", "Glob", "Grep", "Search", "List"}
CC_FILENAME_TS_RE = re.compile(
    r"(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})[-_](?P<h>\d{2})(?P<mi>\d{2})(?P<s>\d{2})")


def looks_like_cc_scrollback(text: str) -> bool:
    if CC_BANNER_RE.search(text[:4000]):
        return True
    return bool(re.search(r"^❯ ", text, re.M) and re.search(r"^● ", text, re.M))


def _cc_agg_tools(line: str) -> list[dict]:
    """Свёрнутая строка активности → вызовы за ней. Команды в ней нет по построению."""
    tools = []
    for ttype, name, pat in CC_AGG_PATTERNS:
        m = pat.search(line)
        if not m:
            continue
        for _ in range(min(int(m.group("n")), 50)):
            tools.append({"name": name, "type": ttype, "command": None,
                          "output_head": None, "output_chars": 0, "collapsed": True})
    for m in CC_COMMIT_RE.finditer(line):
        # Тип не shell, хотя коммит делается шеллом: иначе единственный вызов с командой
        # поднял бы `commands_available`, и скрытые проверки превратились бы в ноль.
        tools.append({"name": "git-commit", "type": "vcs", "command": f"git commit {m.group('sha')}",
                      "output_head": None, "output_chars": 0, "collapsed": True})
    return tools


def parse_cc_scrollback(text: str, keep_tool_output: bool = True, max_turn_chars: int = 0,
                        path=None) -> dict:
    raw = [ln.rstrip() for ln in text.splitlines()]
    ver = CC_BANNER_RE.search(text)
    collector = f"Claude Code {ver.group('ver')}" if ver else "Claude Code"
    head = text[:3000]
    models = []
    mm = CC_MODEL_RE.search(head)
    if mm:
        models.append(mm.group("model").strip())
    cwd_m = CC_CWD_RE.search(head)
    cwd = cwd_m.group("cwd") if cwd_m else None

    blocks: list[dict] = []
    cur: dict | None = None
    in_result = False
    started = False

    def open_block(role):
        nonlocal cur, in_result
        cur = {"role": role, "lines": [], "tools": [], "interrupted": False}
        blocks.append(cur)
        in_result = False

    def add_tool_line(body: str) -> bool:
        m = CC_TOOL_CALL_RE.match(body)
        if not m:
            return False
        name = m.group("name")
        # правку файла TUI рисует как `Update(file)`
        ttype = ("write" if name in CC_WRITE_NAMES
                 else "read" if name in CC_READ_NAMES else "other")
        cur["tools"].append({"name": name, "type": ttype, "command": m.group("arg")[:200] or None,
                             "output_head": None, "output_chars": 0})
        return True

    for ln in raw:
        um = CC_USER_RE.match(ln)
        am = CC_AGENT_RE.match(ln)
        if um is not None:
            started = True
            open_block("user")
            cur["lines"].append(um.group("text").strip())
            continue
        if am is not None:
            started = True
            body = am.group("text").strip()
            # подряд идущие `●` — один ход агента между двумя репликами человека
            if cur is None or cur["role"] != "assistant":
                open_block("assistant")
            in_result = False
            if not add_tool_line(body):
                cur["lines"].append(body)
            continue
        if not started or cur is None:
            continue                                   # шапка до первой реплики
        rm = CC_RESULT_RE.match(ln)
        collapsed = (CC_COLLAPSED_RE.search(ln.strip())
                     and not CC_MORE_LINES_RE.match(ln.strip()))
        # Работа агента начинается до первой строки `●`: сразу под промптом TUI рисует
        # свёрнутую активность и результаты `⎿` с тем же отступом, что у переноса промпта.
        # Без этой развилки они приклеились бы к реплике человека.
        if cur["role"] == "user" and (rm is not None or collapsed):
            open_block("assistant")
        if rm is not None:
            body = rm.group("text").strip()
            in_result = True
            if CC_INTERRUPT_RE.match(body):
                cur["interrupted"] = True
            if cur["tools"]:
                cur["tools"][-1]["output_head"] = (cur["tools"][-1]["output_head"] or body)[:600]
            cur["tools"].extend(_cc_agg_tools(body))
            cur["lines"].append(body)
            continue
        indent = len(ln) - len(ln.lstrip(" "))
        stripped = ln.strip()
        if not stripped:
            cur["lines"].append("")
            continue
        if in_result and indent >= 4:
            # тело результата инструмента в прозу хода не идёт
            if cur["tools"]:
                cur["tools"][-1]["output_chars"] += len(stripped) + 1
            continue
        in_result = False
        if collapsed:
            cur["tools"].extend(_cc_agg_tools(stripped))
        cur["lines"].append(stripped)

    turns = []
    for i, b in enumerate(blocks):
        prose = "\n".join(b["lines"]).strip()
        prose = re.sub(r"\n{3,}", "\n\n", prose)
        syn = None
        if b["role"] == "user":
            # слэш ищем в первой строке: TUI дописывает под командой свой ответ
            first_line = next((ln for ln in prose.splitlines() if ln.strip()), "")
            syn = "slash_command" if CC_SLASH_RE.match(first_line.strip()) else synthetic_reason(prose)
        chars = len(prose)
        turns.append(_turn(i, b["role"], _truncate(prose, max_turn_chars), chars,
                           tools=b["tools"], interrupted=b["interrupted"], syn=syn))

    first_real = next((t["text"] for t in turns if t["role"] == "user" and not t["synthetic"]), None)
    seed = first_real or " | ".join(t["text"] for t in turns if t["role"] == "user")
    seed = re.sub(r"\s+", " ", f"{cwd or ''}|{seed}").strip()[:400]

    started_at = None
    if path is not None:
        fm = CC_FILENAME_TS_RE.search(Path(path).name)
        if fm:
            started_at = (f"{fm.group('y')}-{fm.group('mo')}-{fm.group('d')} "
                          f"{fm.group('h')}:{fm.group('mi')}:{fm.group('s')}")
    # время из имени файла — момент выгрузки, а не старта сессии
    return _document("cc-scrollback", turns, text, collector=collector,
                     session_id=_content_id("ccsb-", _content_seed(seed, turns)),
                     started_at=started_at,
                     started_at_source="filename_export" if started_at else None, models=models)


# ── cursor-export: выгрузка чата Cursor ──────────────────────────────────────
CURSOR_BANNER_RE = re.compile(
    r"^_Exported on\s+(?P<when>.+?)\s+from\s+Cursor\s*(?:\((?P<ver>[^)]*)\))?_\s*$", re.M
)
# Роль стоит отдельной строкой целиком: жирный `**User**` посреди прозы встречается в любом отчёте.
CURSOR_TURN_RE = re.compile(r"^\*\*(?P<role>User|Cursor|Assistant|System)\*\*\s*$", re.M)
CURSOR_TITLE_RE = re.compile(r"\A\s*#\s+(?P<title>.+?)\s*$", re.M)
CURSOR_WHEN_RE = re.compile(
    r"(?P<mo>\d{1,2})/(?P<d>\d{1,2})/(?P<y>\d{4})\s+at\s+"
    r"(?P<h>\d{1,2}):(?P<mi>\d{2}):(?P<s>\d{2})"
)


def looks_like_cursor_export(text: str) -> bool:
    if CURSOR_BANNER_RE.search(text[:4000]):
        return True
    roles = [m.group("role") for m in CURSOR_TURN_RE.finditer(text)]
    return roles.count("User") >= 2 and roles.count("Cursor") >= 2


def parse_cursor_export(text: str, keep_tool_output: bool = True, max_turn_chars: int = 0,
                        path=None) -> dict:
    """`**User**` / `**Cursor**` через `---`. Вызовы инструментов в формате не размечены:
    `tools` пуст, и `metrics()` ставит `commands_available: false` — это факт о выгрузке,
    а не о человеке. Единственная дата в файле — момент выгрузки."""
    marks = list(CURSOR_TURN_RE.finditer(text))
    turns = []
    for i, mark in enumerate(marks):
        role_raw = mark.group("role")
        role = "user" if role_raw == "User" else ("system" if role_raw == "System" else "assistant")
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[mark.end():end]
        # `---` здесь разделитель ходов, а не линейка внутри реплики
        prose = re.sub(r"^\s*---\s*$", "", body, flags=re.M)
        prose = re.sub(r"\n{3,}", "\n\n", prose).strip()
        syn = None
        if role == "user":
            first_line = next((ln for ln in prose.splitlines() if ln.strip()), "")
            syn = "slash_command" if CC_SLASH_RE.match(first_line.strip()) else synthetic_reason(prose)
        chars = len(prose)
        turns.append(_turn(i, role, _truncate(prose, max_turn_chars), chars,
                           interrupted=bool(INTERRUPT_RE.search(body)), syn=syn))

    tm = CURSOR_TITLE_RE.search(text)
    title = tm.group("title") if tm else ""
    first_real = next((t["text"] for t in turns if t["role"] == "user" and not t["synthetic"]), None)
    seed = first_real or " | ".join(t["text"] for t in turns if t["role"] == "user")
    seed = re.sub(r"\s+", " ", f"{title}|{seed}").strip()[:400]

    started_at = None
    bm = CURSOR_BANNER_RE.search(text)
    if bm:
        wm = CURSOR_WHEN_RE.search(bm.group("when"))
        if wm:
            started_at = (f"{wm.group('y')}-{int(wm.group('mo')):02d}-{int(wm.group('d')):02d} "
                          f"{int(wm.group('h')):02d}:{wm.group('mi')}:{wm.group('s')}")
    return _document("cursor-export", turns, text, collector="Cursor",
                     session_id=_content_id("curex-", _content_seed(seed, turns)),
                     started_at=started_at,
                     started_at_source="export_banner" if started_at else None)


# ── md-headings: ручной экспорт заголовками ──────────────────────────────────
# Заголовок роли терпит значок перед именем (👤 / 🤖) и хвост с временем (`· 19:23`).
MDEX_TURN_RE = re.compile(
    r"^#{2,4}\s*(?:[^\w\s#]+\s*)?(?P<role>User|Human|Пользователь|Assistant|Agent|Claude|Ассистент|"
    r"Thinking|Reasoning|Размышления|System)\s*(?::|·[^\n]*)?\s*$",
    re.M | re.I,
)
MDEX_USER_ROLES = {"user", "human", "пользователь"}
MDEX_THINK_ROLES = {"thinking", "reasoning", "размышления"}
MDEX_DATE_RE = re.compile(r"^\**Date:?\**\s*:?\s*(?P<d>\d{4}-\d{2}-\d{2})", re.M | re.I)
MDEX_FILE_DATE_RE = re.compile(r"(?P<d>\d{4}-\d{2}-\d{2})")


def looks_like_heading_export(text: str) -> bool:
    roles = [m.group("role").lower() for m in MDEX_TURN_RE.finditer(text)]
    if not roles:
        return False
    has_user = any(r in MDEX_USER_ROLES for r in roles)
    has_assistant = any(r not in MDEX_USER_ROLES and r not in MDEX_THINK_ROLES for r in roles)
    return has_user and has_assistant


def parse_heading_export(text: str, keep_tool_output: bool = True, max_turn_chars: int = 0,
                         path=None) -> dict:
    """`## User` / `## Assistant`: ручной путь для связок без автоматической выгрузки.

    Вызовы инструментов не размечены (`tools` пуст, `commands_available: false`). Блоки
    размышлений приклеиваются к следующему ответу, иначе `assistant_turns` удваивается.
    """
    marks = list(MDEX_TURN_RE.finditer(text))
    raw = []
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = re.sub(r"^\s*---\s*$", "", text[mark.end():end], flags=re.M)
        raw.append((mark.group("role").lower(), re.sub(r"\n{3,}", "\n\n", body).strip()))

    turns, pending_think = [], []
    for role_raw, prose in raw:
        if role_raw in MDEX_THINK_ROLES:
            if prose:
                pending_think.append(prose)
            continue
        role = "user" if role_raw in MDEX_USER_ROLES else (
            "system" if role_raw == "system" else "assistant")
        if role == "assistant" and pending_think:
            prose = "\n\n".join(pending_think + ([prose] if prose else [])).strip()
            pending_think = []
        syn = synthetic_reason(prose) if role == "user" else None
        chars = len(prose)
        turns.append(_turn(len(turns), role, _truncate(prose, max_turn_chars), chars,
                           interrupted=bool(INTERRUPT_RE.search(prose)), syn=syn))
    if pending_think:      # размышления в самом конце, ответа за ними не последовало
        joined = "\n\n".join(pending_think).strip()
        turns.append(_turn(len(turns), "assistant", joined, len(joined)))

    first_real = next((t["text"] for t in turns if t["role"] == "user" and not t["synthetic"]), None)
    seed = re.sub(r"\s+", " ", first_real or "").strip()[:400]

    dm = MDEX_DATE_RE.search(text)
    started_at, src = (dm.group("d"), "header_date") if dm else (None, None)
    if not started_at and path is not None:
        fm = MDEX_FILE_DATE_RE.search(Path(str(path)).name)
        started_at, src = (fm.group("d"), "filename") if fm else (None, None)
    # дата суток без времени: часов в таком экспорте нет ни у одного хода; в ключ идёт только
    # дата из шапки, дата из имени файла — нет
    header_date = started_at if src == "header_date" else None
    return _document("md-headings", turns, text, collector="manual-export",
                     session_id=_content_id("mdex-", _content_seed(seed, turns, header_date)),
                     started_at=started_at,
                     started_at_source=src)


# ── md-bold-roles: экспорт с ролями жирным ───────────────────────────────────
# Реплики человека дословно, ответы агента прозой, вызовы инструментов одной строкой в цитате
# (`> Bash(command=…)` в бэктиках), выводы выброшены. Имя человека произвольное, поэтому роль
# опознаётся по повторяемости: маркер — имя, стоящее на отдельной строке хотя бы дважды,
# и среди таких имён обязано быть известное имя агента.
MDBOLD_ROLE_RE = re.compile(r"^\*\*(?P<role>[A-Za-zА-Яа-яЁё][\w.\- ]{0,30}?):\*\*\s*$", re.M)
MDBOLD_ASSISTANT_NAMES = {"claude", "assistant", "agent", "ai", "codex", "cursor", "copilot",
                          "gemini", "model", "bot", "ассистент", "агент"}
MDBOLD_TOOL_RE = re.compile(r"^>\s*`(?P<name>[A-Z][A-Za-z_]{1,30})\((?P<args>.*)\)`\s*$", re.M)
MDBOLD_CMD_RE = re.compile(
    r"\bcommand=(?P<cmd>.*?)(?:,\s*(?:description|timeout|run_in_background)=|$)", re.S)
MDBOLD_DATE_RE = re.compile(r"\b(?P<d>\d{4}-\d{2}-\d{2})\b")
MDBOLD_SHELL_NAMES = {"Bash", "Shell", "Terminal", "bash", "shell"}
MDBOLD_WRITE_NAMES = TOOLS_WRITING | {"Update", "Create", "Patch"}


def _mdbold_roles(text: str) -> tuple[set, set]:
    counts: dict = {}
    for m in MDBOLD_ROLE_RE.finditer(text):
        r = m.group("role").strip().lower()
        counts[r] = counts.get(r, 0) + 1
    roles = {r for r, n in counts.items() if n >= 2}
    assistants = {r for r in roles if r in MDBOLD_ASSISTANT_NAMES}
    return assistants, roles - assistants


def looks_like_bold_export(text: str) -> bool:
    assistants, users = _mdbold_roles(text)
    return bool(assistants) and bool(users)


def _mdbold_tools(body: str) -> list[dict]:
    """Строки `> Name(args)` в бэктиках → вызовы. Команда шелла — из `command=`."""
    tools = []
    for tm in MDBOLD_TOOL_RE.finditer(body):
        name, args = tm.group("name"), tm.group("args")
        if name in MDBOLD_SHELL_NAMES:
            ttype = "shell"
            cm = MDBOLD_CMD_RE.search(args)
            command = html.unescape(cm.group("cmd")).strip() if cm else None
        elif name in MDBOLD_WRITE_NAMES:
            ttype, command = "write", None
        else:
            ttype, command = "other", None
        tools.append({"name": name, "type": ttype, "command": command or None,
                      "output_head": None, "output_chars": 0, "collapsed": True})
    return tools


def _mdbold_unfence(prose: str) -> str:
    """Реплика человека завёрнута в забор из 3+ бэктиков: это упаковка, а не текст."""
    lines = prose.strip().splitlines()
    if len(lines) >= 2 and re.fullmatch(r"`{3,}\s*", lines[0]) and re.fullmatch(r"`{3,}\s*", lines[-1]):
        return "\n".join(lines[1:-1]).strip()
    return prose.strip()


def parse_bold_export(text: str, keep_tool_output: bool = True, max_turn_chars: int = 0,
                      path=None) -> dict:
    assistants, users = _mdbold_roles(text)
    known = assistants | users
    marks = [m for m in MDBOLD_ROLE_RE.finditer(text) if m.group("role").strip().lower() in known]
    turns = []
    for i, mark in enumerate(marks):
        role = "assistant" if mark.group("role").strip().lower() in assistants else "user"
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[mark.end():end]
        tools = _mdbold_tools(body) if role == "assistant" else []
        prose = MDBOLD_TOOL_RE.sub("", body)
        prose = re.sub(r"^\s*---\s*$", "", prose, flags=re.M)
        prose = re.sub(r"\n{3,}", "\n\n", prose).strip()
        if role == "user":
            prose = _mdbold_unfence(prose)
        syn = synthetic_reason(prose) if role == "user" else None
        chars = len(prose)
        turns.append(_turn(len(turns), role, _truncate(prose, max_turn_chars), chars, tools=tools,
                           interrupted=bool(INTERRUPT_RE.search(prose)), syn=syn))

    first_real = next((t["text"] for t in turns if t["role"] == "user" and not t["synthetic"]), None)
    seed = re.sub(r"\s+", " ", first_real or "").strip()[:400]

    head = "\n".join(text.splitlines()[:15])
    dm = MDBOLD_DATE_RE.search(head)
    started_at, src = (dm.group("d"), "header_date") if dm else (None, None)
    if not started_at and path is not None:
        fm = MDEX_FILE_DATE_RE.search(Path(str(path)).name)
        started_at, src = (fm.group("d"), "filename") if fm else (None, None)
    # первая дата в шапке такого экспорта — обычно момент сборки файла, а не старт: в ключ не идёт
    return _document("md-bold-roles", turns, text, collector="manual-export",
                     session_id=_content_id("mdbold-", _content_seed(seed, turns)),
                     started_at=started_at,
                     started_at_source=src)


# ── jsonl-export: экспорт native JSONL заголовками с временем ────────────────
# У каждого заголовка есть время (`## user — 2026-09-22T17:12:02.491Z`), вызовы и ответы
# инструментов размечены своими заголовками третьего уровня с Call ID. Детектор стоит первым:
# в выводе инструментов внутри такого файла встречаются `❯` и `●` скроллбека.
JEX_TS = r"\d{4}-\d{2}-\d{2}T[\d:]{8}(?:\.\d+)?Z?"
JEX_BANNER_RE = re.compile(
    rf"^#\s*(?P<tool>codex|claude|cursor|copilot|gemini)\s*[—–-]\s*{JEX_TS}\s*$", re.M | re.I)
JEX_NOTE_RE = re.compile(r"Offline (?:export|snapshot) of \w+ JSONL records", re.I)
JEX_FENCE_OPEN_RE = re.compile(r"^(?P<f>`{3,})(?P<tail>.*)$")
JEX_MARK_RE = re.compile(
    rf"^##\s*(?P<role>user|assistant)\s*[—–-]\s*(?P<rts>{JEX_TS})\s*$"
    rf"|^###\s*Tool call(?:\s*:\s*(?P<tname>[^\n—–]+?))?\s*[—–-]\s*(?P<cts>{JEX_TS})\s*$"
    rf"|^###\s*Tool result\s*[—–-]\s*(?P<xts>{JEX_TS})\s*$"
    rf"|^###\s*Agent message\s*[—–-]\s*(?P<ats>{JEX_TS})\s*$", re.I)
# Команда шелла лежит либо в вызове Codex (`tools.exec_command({cmd:"…"})`), либо в
# JSON-аргументах (`"command": "…"` / `"command": ["bash","-lc","…"]`).
JEX_EXEC_CMD_RE = re.compile(r'\bcmd\s*:\s*"(?P<cmd>(?:[^"\\]|\\.)*)"')
JEX_JSON_CMD_RE = re.compile(
    r'"(?:command|cmd)"\s*:\s*(?P<val>"(?:[^"\\]|\\.)*"|\[[^\]]*\])')
JEX_SHELL_NAMES = {"exec", "exec_command", "shell", "bash", "local_shell", "run_terminal_cmd",
                   "container.exec", "functions.exec"}


def _jex_unfence(body: str) -> str:
    """Первый забор из тела блока: вокруг него служебное (`Call ID: …`)."""
    m = re.search(r"^(?P<f>`{3,})[^\n]*\n(?P<inner>.*?)\n(?P=f)\s*$", body, re.S | re.M)
    return (m.group("inner") if m else body).strip()


def exec_call_command(text: str) -> str | None:
    """Команда из вызова exec: `exec_command({cmd:"…"})` или JSON `{"cmd"|"command": …}`."""
    m = JEX_EXEC_CMD_RE.search(text)
    if m:
        try:
            return json.loads('"' + m.group("cmd") + '"').strip() or None
        except ValueError:
            return m.group("cmd").strip() or None
    m = JEX_JSON_CMD_RE.search(text)
    if m:
        try:
            val = json.loads(m.group("val"))
        except ValueError:
            return None
        if isinstance(val, list):
            val = " ".join(str(x) for x in val)
        return str(val).strip() or None
    return None


def _jex_tool_command(name: str, body: str) -> str | None:
    return exec_call_command(_jex_unfence(body))


def _jex_tool_type(name: str) -> str:
    low = (name or "").lower()
    if low in JEX_SHELL_NAMES:
        return "shell"
    if name in TOOLS_WRITING or low in {"write", "edit", "update"}:
        return "write"
    if MCP_TOOL_RE.match(name or ""):
        return "mcp"
    return "tool"


def looks_like_jsonl_export(text: str) -> bool:
    """Шапка экспорта плюс его оговорка про опущенное — вместе они не встречаются ни в одном
    другом формате, поэтому детектор может стоять первым."""
    head = text[:4000]
    return bool(JEX_BANNER_RE.search(head)) and bool(JEX_NOTE_RE.search(head))


def _jex_marks(text: str) -> tuple[list[str], list[tuple[int, re.Match]]]:
    """Маркеры вне заборов: внутри заборов лежит вывод инструментов, где встречаются
    и `## user`, и `### Tool call` — своим же логом, зачитанным в контекст."""
    lines = text.split("\n")
    fence: str | None = None
    marks: list[tuple[int, re.Match]] = []
    for i, ln in enumerate(lines):
        fm = JEX_FENCE_OPEN_RE.match(ln.strip())
        if fence is not None:
            if fm and len(fm.group("f")) >= len(fence) and not fm.group("tail").strip():
                fence = None
            continue
        if fm:
            fence = fm.group("f")
            continue
        m = JEX_MARK_RE.match(ln)
        if m:
            marks.append((i, m))
    return lines, marks


def parse_jsonl_export(text: str, keep_tool_output: bool = True, max_turn_chars: int = 0,
                       path=None) -> dict:
    lines, marks = _jex_marks(text)
    banner = JEX_BANNER_RE.search(text[:4000])
    tool_name = banner.group("tool").lower() if banner else "unknown"

    turns: list[dict] = []

    def open_assistant(ts: str | None) -> dict:
        """Вызов инструмента без предшествующей прозы — действие агента, а не человека."""
        if turns and turns[-1]["role"] == "assistant":
            return turns[-1]
        turns.append(_turn(len(turns), "assistant", "", 0, ts=ts))
        return turns[-1]

    def add_turn(role: str, ts: str | None, prose: str, syn: str | None) -> None:
        turns.append(_turn(len(turns), role, _truncate(prose, max_turn_chars), len(prose), ts=ts,
                           interrupted=bool(INTERRUPT_RE.search(prose)), syn=syn))

    for pos, (line_no, m) in enumerate(marks):
        end = marks[pos + 1][0] if pos + 1 < len(marks) else len(lines)
        body = "\n".join(lines[line_no + 1:end])
        if m.group("role"):
            role = m.group("role").lower()
            prose = _jex_unfence(body)
            add_turn(role, m.group("rts"), prose, synthetic_reason(prose) if role == "user" else None)
        elif m.group("ats") is not None:
            # межагентное сообщение приезжает в слот человека, но написано не человеком
            add_turn("user", m.group("ats"), _jex_unfence(body), "agent_message")
        elif m.group("cts") is not None:
            name = (m.group("tname") or tool_name or "tool").strip()
            turn = open_assistant(m.group("cts"))
            turn["tools"].append({"name": name, "type": _jex_tool_type(name),
                                  "command": _jex_tool_command(name, body),
                                  "output_head": None, "output_chars": 0})
        else:
            out = _jex_unfence(body)
            last = next((t for t in reversed(turns) if t["tools"]), None)
            if last is not None:
                tool = last["tools"][-1]
                tool["output_chars"] = len(out)
                tool["output_head"] = out[:400] or None

    # Идентификатор сеется шапкой (время старта уникально и переживает переименование), а не
    # первой репликой: у JetBrains/Codex первая реплика — служебная вставка, одинаковая во всех
    # сессиях проекта, и разные разговоры склеивались бы в один.
    seed = banner.group(0).strip() if banner else ""
    if not seed:
        seed_turn = next((t for t in turns if t["role"] == "user" and not t["synthetic"]), None)
        seed = re.sub(r"\s+", " ", (seed_turn or {}).get("text", "")).strip()[:400]

    stamps = [t["ts"] for t in turns if t.get("ts")]
    return _document("jsonl-export", turns, text, collector=f"{tool_name}-jsonl-export",
                     session_id=_content_id("jex-", seed),
                     started_at=stamps[0] if stamps else None,
                     started_at_source="turn_ts" if stamps else None,
                     ended_at=stamps[-1] if stamps else None)


# ── specstory (фоллбек) ──────────────────────────────────────────────────────


def parse_specstory(text: str, keep_tool_output: bool = True, max_turn_chars: int = 0,
                    path=None) -> dict:
    session_id = collector = started_at = None
    m = SESSION_RE.search(text)
    if m:
        session_id = m.group("sid")
        collector = (m.group("collector") or "").strip()
        started_at = (m.group("ts") or "").strip() or None
    if not started_at:
        h = HEADING_TS_RE.search(text)
        started_at = h.group("ts") if h else None

    marks = list(TURN_RE.finditer(text))
    turns = []
    for i, mark in enumerate(marks):
        role_raw = mark.group("role").lower()
        role = ("user" if role_raw in ("user", "человек")
                else ("system" if role_raw == "system" else "assistant"))
        # Ход внутри субагента никогда не реплика человека, даже если заголовок скажет User.
        sidechain = bool(SIDECHAIN_SUFFIX_RE.search(mark.group("suffix") or ""))
        if sidechain and role == "user":
            role = "assistant"
        meta = (mark.group("meta") or "").strip()
        ts = model = None
        ts_m = TS_IN_META_RE.search(meta)
        if ts_m:
            ts = ts_m.group("ts")
            model = meta[: ts_m.start()].strip() or None
        elif meta:
            model = meta
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[mark.end():end]

        tools = []
        for tm in TOOL_BLOCK_RE.finditer(body):
            tbody = tm.group("body")
            sm = SUMMARY_RE.search(tbody)
            after_summary = tbody[sm.end():] if sm else tbody
            out_m = FENCE_RE.search(after_summary)
            output = (out_m.group("out") if out_m and keep_tool_output else "") or ""
            tools.append({
                "name": tm.group("tname"),
                "type": tm.group("ttype"),
                "command": extract_command(sm.group("s") if sm else None, after_summary,
                                           exec_call=is_exec_call(tm.group("ttype"),
                                                                  tm.group("tname"))),
                "output_head": output.strip()[:600] or None,
                "output_chars": len(output),
            })

        # Экспорт с русскими ролями: вызов и результат — отдельные <details> в порядке появления.
        ru_events = sorted(
            [(rm.start(), "call", rm) for rm in RU_TOOL_BLOCK_RE.finditer(body)]
            + [(rm.start(), "result", rm) for rm in RU_RESULT_BLOCK_RE.finditer(body)],
            key=lambda e: e[0])
        for _, kind, rm in ru_events:
            if kind == "call":
                tname = rm.group("tname").strip()
                fence = FENCE_RE.search(rm.group("body"))
                arg = fence.group("out").strip() if fence else None
                ttype = ("shell" if tname in RU_SHELL_NAMES
                         else "write" if tname in TOOLS_WRITING
                         else "read" if tname in RU_READ_NAMES else "tool")
                tools.append({"name": tname, "type": ttype,
                              "command": arg if ttype == "shell" else None,
                              "output_head": None, "output_chars": 0})
            elif tools:
                out_m = FENCE_RE.search(rm.group("body"))
                output = (out_m.group("out") if out_m and keep_tool_output else "") or ""
                tools[-1]["output_head"] = output.strip()[:600] or None
                tools[-1]["output_chars"] = len(output)

        prose = TOOL_BLOCK_RE.sub(" ", body)
        prose = RU_TOOL_BLOCK_RE.sub(" ", prose)
        prose = RU_RESULT_BLOCK_RE.sub(" ", prose)
        prose = re.sub(r"^\s*---\s*$", "", prose, flags=re.M)
        prose = re.sub(r"\n{3,}", "\n\n", prose).strip()
        # вставку харнесса ищем по полному тексту: маркер бывает и не в первой строке
        syn = synthetic_reason(prose) if role == "user" else None
        prose = _truncate(prose, max_turn_chars)
        turns.append(_turn(i, role, prose, len(prose), tools=tools, ts=ts, model=model,
                           interrupted=bool(INTERRUPT_RE.search(body)), sidechain=sidechain,
                           syn=syn))

    # Слэш-команда приезжает двумя ходами: `<command-name>/init</command-name>`, затем её
    # разворот — готовый промпт вендора. Второй ход ловится только позицией.
    for i, t in enumerate(turns):
        if t["synthetic_reason"] != "slash_command":
            continue
        nxt = turns[i + 1] if i + 1 < len(turns) else None
        if nxt and nxt["role"] == "user" and not nxt["synthetic"]:
            nxt["synthetic"] = True
            nxt["synthetic_reason"] = "slash_command_expansion"

    models = []
    for t in turns:
        if t["model"] and t["model"] not in models:
            models.append(t["model"])
    return _document("specstory", turns, text, collector=collector, session_id=session_id,
                     started_at=started_at,
                     started_at_source="log_header" if started_at else None,
                     ended_at=next((t["ts"] for t in reversed(turns) if t["ts"]), None),
                     models=models)


# ── Детект и разбор ──────────────────────────────────────────────────────────


def _no_specstory_turns(detector: Callable[[str], bool]) -> Callable[[str], bool]:
    """Инструментальные и ручные форматы проверяются, только если в тексте нет ходов SpecStory:
    `## User` и `**User**` встречаются и внутри SpecStory-логов."""
    return lambda text: not TURN_RE.search(text) and detector(text)


# (имя, детектор, парсер) в порядке проверки. Точка расширения: новый формат вставляется
# в нужную позицию. Все парсеры принимают (text, keep_tool_output, max_turn_chars, path).
# SpecStory последний и принимает всё — это фоллбек.
FORMATS: list[tuple[str, Callable[[str], bool], Callable[..., dict]]] = [
    ("jsonl-export", looks_like_jsonl_export, parse_jsonl_export),
    ("cc-scrollback", _no_specstory_turns(looks_like_cc_scrollback), parse_cc_scrollback),
    ("cursor-export", _no_specstory_turns(looks_like_cursor_export), parse_cursor_export),
    # ручные экспорты — после инструментальных: их маркеры встречаются внутри чужих форматов
    ("md-headings", _no_specstory_turns(looks_like_heading_export), parse_heading_export),
    ("md-bold-roles", _no_specstory_turns(looks_like_bold_export), parse_bold_export),
    ("specstory", lambda text: True, parse_specstory),
]


# Нативный JSONL Claude Code разбирает `lib/cc_jsonl.py`; импорт внутри обёрток, чтобы не было цикла
# (`cc_jsonl` импортирует `normalize` на верхнем уровне). Проверяется первым: JSON-строки
# не похожи ни на один markdown-формат.
def _cc_looks_like(text: str) -> bool:
    from lib import cc_jsonl
    return cc_jsonl.looks_like(text)


def _cc_parse(text: str, **kw) -> dict:
    from lib import cc_jsonl
    return cc_jsonl.parse(text, **kw)


FORMATS.insert(0, ("claude-jsonl", _cc_looks_like, _cc_parse))


def format_names() -> list[str]:
    return [name for name, _, _ in FORMATS]


def _pick(text: str):
    for name, detector, parser in FORMATS:
        if detector(text):
            return name, parser
    raise SrError(f"normalize: ни один детектор не принял текст (форматы: {', '.join(format_names())})", 1)


def detect_format(text: str) -> str:
    return _pick(text)[0]


def parse(text: str, keep_tool_output: bool = True, max_turn_chars: int = 0, path=None) -> dict:
    _, parser = _pick(text)
    doc = parser(text, keep_tool_output=keep_tool_output, max_turn_chars=max_turn_chars, path=path)
    mm = doc.setdefault("mechanical_metrics", {})
    for key in EXTRA_METRIC_KEYS:
        mm.setdefault(key, None)
    return doc


def parse_file(path: Path, keep_tool_output: bool = True, max_turn_chars: int = 0) -> dict:
    """Читает лог как UTF-8 (битые байты заменяются) и добавляет в документ `path`."""
    path = Path(path)
    if not path.is_file():
        raise SrError(f"normalize: нет файла ({path})", 2)
    doc = parse(path.read_text(encoding="utf-8", errors="replace"),
                keep_tool_output=keep_tool_output, max_turn_chars=max_turn_chars, path=path)
    doc["path"] = str(path)
    return doc


# ── Метрики ──────────────────────────────────────────────────────────────────


FRACTION_RE = re.compile(r"\.(\d+)")


def parse_ts(value: str | None) -> datetime | None:
    """ISO-время с `Z`, `±HH:MM`, дробными секундами любой длины, `T` или пробелом → aware
    datetime; время без зоны считается UTC; не разобралось → None."""
    if not value:
        return None
    s = value.strip()
    if s[-1:] in ("Z", "z"):
        s = s[:-1] + "+00:00"
    # fromisoformat в Python 3.10 понимает только 3 или 6 знаков дробной части
    s = FRACTION_RE.sub(lambda m: "." + (m.group(1) + "000000")[:6], s, count=1)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def seconds_between(a: str | None, b: str | None) -> float | None:
    pa, pb = parse_ts(a), parse_ts(b)
    if pa is None or pb is None:
        return None
    return (pb - pa).total_seconds()


def is_approve(text: str) -> bool:
    t = re.sub(r"[!.,)(\s]+", " ", (text or "").strip().lower()).strip()
    if not t or len(t) > 24:
        return False
    return t in APPROVE_WORDS or all(w in APPROVE_WORDS for w in t.split())


def verify_kind(tool: dict) -> str | None:
    """Класс проверки вызова: test_command, runtime_check, adhoc_run, ui_check, mcp_call."""
    name = tool.get("name") or ""
    cmd = tool.get("command") or ""
    if cmd and VERIFY_CMD_RE.search(cmd):
        return "test_command"
    if cmd and RUNTIME_CMD_RE.search(cmd):
        return "runtime_check"
    if cmd and ADHOC_RUN_RE.search(cmd):
        return "adhoc_run"
    if RESEARCH_TOOL_RE.match(name):
        return None                      # веб-поиск — ресёрч, не проверка
    if UI_TOOL_RE.search(name):
        return "ui_check"
    m = MCP_TOOL_RE.match(name)
    if m:
        if m.group("server").lower() in MCP_RESEARCH_SERVERS:
            return None
        return "mcp_call"
    return None


def metrics(turns: list[dict]) -> dict:
    """19 механических счётчиков. Всё, что не считается по имеющимся данным, — None.

    Нет таймстемпов — быстрые апрувы не измерены. Нет вызовов инструментов — счётчики записи
    и проверок не измерены.
    """
    have_ts = any(t.get("ts") for t in turns)
    speed_accept = instant_approvals = approvals = 0
    interrupts = sum(1 for t in turns if t.get("interrupted"))
    verify_calls = 0
    verify_examples = []
    verify_by_kind: dict = {}
    write_calls = tool_calls = shell_calls = shell_with_cmd = 0
    repeated = 0
    prev_user_norm = None

    for i, t in enumerate(turns):
        for tool in t.get("tools") or []:
            tool_calls += 1
            if is_write_tool(tool):
                write_calls += 1
            cmd = tool.get("command") or ""
            if is_shell_tool(tool):
                shell_calls += 1
                if cmd:
                    shell_with_cmd += 1
            kind = verify_kind(tool)
            if kind:
                verify_calls += 1
                verify_by_kind[kind] = verify_by_kind.get(kind, 0) + 1
                if len(verify_examples) < 12:
                    verify_examples.append({"turn_index": t["index"], "kind": kind,
                                            "tool": tool.get("name"), "command": cmd[:200] or None})
        # вставки харнесса человек не писал: в счётчики его поведения они не идут
        if t["role"] != "user" or t.get("synthetic"):
            continue
        if is_approve(t["text"]):
            approvals += 1
            prev = turns[i - 1] if i else None
            if prev and prev["role"] == "assistant":
                changed = any(is_write_tool(tool) for tool in prev.get("tools") or [])
                gap = seconds_between(prev.get("ts"), t.get("ts"))
                # без таймстемпов быстрый апрув не отличить от прочитанного: не считаем
                if gap is not None and gap <= 20:
                    instant_approvals += 1
                    if changed:
                        speed_accept += 1
        norm = re.sub(r"\W+", " ", (t["text"] or "").lower()).strip()[:160]
        if norm and prev_user_norm and norm == prev_user_norm:
            repeated += 1
        prev_user_norm = norm or prev_user_norm

    span = seconds_between(
        next((t["ts"] for t in turns if t.get("ts")), None),
        next((t["ts"] for t in reversed(turns) if t.get("ts")), None),
    )
    tools_visible = tool_calls > 0
    # Шелл-вызовы есть, а команды не видно ни у одного — формат прячет команды (свёрнутый
    # скроллбек, неизвестный коллектор). Тогда число проверок не измерено, даже если часть
    # проверок опознана по имени инструмента: заниженное число читалось бы как факт.
    # Сами опознанные примеры остаются в verification_examples.
    commands_visible = shell_calls == 0 or shell_with_cmd > 0
    verify_measurable = tools_visible and commands_visible
    return {
        "timestamps_available": have_ts,
        "tool_calls_available": tools_visible,
        "commands_available": tools_visible and commands_visible,
        "turns_total": len(turns),
        "user_turns": sum(1 for t in turns if t["role"] == "user" and not t.get("synthetic")),
        "synthetic_turns": sum(1 for t in turns if t.get("synthetic")),
        "sidechain_turns": sum(1 for t in turns if t.get("sidechain")),
        "tool_calls": tool_calls,
        "shell_tool_calls": shell_calls if tools_visible else None,
        "write_tool_calls": write_calls if tools_visible else None,
        "verification_commands": verify_calls if verify_measurable else None,
        "verification_by_kind": verify_by_kind if verify_measurable else None,
        "verification_examples": verify_examples,
        "approvals": approvals,
        "instant_approvals": instant_approvals if have_ts else None,
        "speed_accept_count": speed_accept if have_ts else None,
        "interrupts": interrupts,
        "repeated_prompts": repeated,
        "session_span_seconds": span,
    }
