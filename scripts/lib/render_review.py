"""Текст ревью: сборка отправляемого блока по шаблону, запреты текста, файл ревью, approve.

Судья отдаёт части текста без разметки (`feedback_parts`), скрипт собирает из них блок для
человека по шаблону `feedback.<lang>.md` и проверяет запреты. Шаблон выбирается так:
`--template` → `feedback.template` карточки (`default` → шаблон плагина, `workspace` →
`.retropair/templates/feedback.md`, иное имя файла → `.retropair/templates/<имя>`) →
`templates/feedback.<lang>.md` плагина. Нет файла — явная ошибка, тихой подмены нет.

Файл `reviews/<person>/<run_id>.md` читает отчёт, поэтому его форма фиксирована:

    ---
    person_id / run_id / period / status / lang / template
    ---
    <!-- service -->
    служебная шапка (ревьюеру)
    ---
    отправляемый блок (человеку)
    ---
    ## Досье …  (ревьюеру)

Формат пункта действия, строки критерия и `{{opening}}` — по шапке-комментарию шаблона:
поля судьи идут как есть (точка в конце добавляется, если её нет), строка покрытия уже лежит
в `feedback_parts.opening` (её пишет судья), рендер её не дублирует и флаги карточки
`opening_summary`/`mark_sections` не трактует: они меняют то, что судья пишет в `opening`.
`short` или `practices: false` при рычагах — ошибка, а не обрезка.

Внутри отправляемого блока голой линейки, заголовков `#` и code-fence нет: по ним парсер
отчёта режет файл. Статус: режим `self` → `approved`; `team`/`course` при `reviewer_gate`
→ `draft` (текст уходит только после чтения ревьюером), без гейта → `approved`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import datetime
from pathlib import Path

from lib import i18n, marks, people, text_rules, tracker
from lib.config import load_config
from lib.version import plugin_root
from lib.workspace import SrError, ws_paths

ASSESS = ("cli", "assess")
PLACEHOLDERS = ("prefix", "opening", "actions", "criteria", "closing")
REVIEWED = "reviewed"
MAX_ACTIONS = {"short": 2, "detailed": 3}

_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")
_ONLY_PLACEHOLDER_RE = re.compile(r"^\s*\{\{\s*(\w+)\s*\}\}\s*$")
_LEFT_RE = re.compile(r"\{\{.*?\}\}")
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_LEADING_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
_HR_RE = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")  # линейка, по которой парсер отчёта режет файл
_END_MARKS = ".!?…"
_CLOSE_MARKS = "»\"”)"
_SENTENCE_SPLIT_RE = re.compile(r"[.!?…]+(?:\s+|$)")
_ACTION_FIELDS = ("place", "date", "quote", "cost", "try")


# --- строки ------------------------------------------------------------------


def _t(section, key: str, lang: str) -> str:
    try:
        return i18n.t(section, key, lang)
    except KeyError as err:
        raise SrError(f"{err.args[0]} (строки интерфейса references/i18n.json)", 1) from err


def verdict_text(verdict: str, lang: str) -> str:
    """Строка-вердикт критерия из секции `verdicts`; неизвестный ключ → `SrError(1)`."""
    return _t("verdicts", verdict, lang)


def _date_words(value, lang: str) -> str | None:
    """`2026-09-02…` → «2 сентября» / «September 2»; не дата → None."""
    m = _DATE_RE.match(value) if isinstance(value, str) else None
    if not m:
        return None
    months = _t(ASSESS, "months", lang).split(",")
    month, day = int(m.group(2)), int(m.group(3))
    if not 1 <= month <= len(months):
        raise SrError(f"дата {value!r}: месяца {month} нет", 1)
    return f"{day} {months[month - 1]}" if lang == "ru" else f"{months[month - 1]} {day}"


def _upper_first(text: str) -> str:
    return text[:1].upper() + text[1:]


def _end(text: str) -> str:
    """Поле без своей конечной точки получает точку."""
    text = text.strip()
    if not text:
        return text
    if text[-1] in _END_MARKS or (text[-1] in _CLOSE_MARKS and len(text) > 1 and text[-2] in _END_MARKS):
        return text
    return text + "."


prose_outside_quotes = text_rules.prose_outside_quotes


# --- шаблон ------------------------------------------------------------------


def _plain_name(name: str) -> bool:
    return bool(name) and "/" not in name and "\\" not in name and ".." not in name


def resolve_template(root: Path, card: dict, lang: str, explicit: str | None) -> Path:
    """Файл шаблона текста: `explicit` → шаблон карточки → шаблон плагина.

    Явный файл не найден → `SrError(2)`; имя шаблона в карточке с разделителем пути или `..`
    → `SrError(2)`; шаблон карточки или плагина не найден → `SrError(1)` с путём.
    """
    if explicit is not None:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.is_file():
            raise SrError(f"шаблон текста ревью не найден: нет файла ({path})", 2)
        return path
    name = (card.get("feedback") or {}).get("template") or "default"
    if name != "default":
        if not _plain_name(name):
            raise SrError(
                f"feedback.template карточки {card.get('id')!r} = {name!r}: нужно default, workspace "
                f"или имя файла без разделителей пути и ..",
                2,
            )
        folder = ws_paths(root)["templates"]
        path = folder / ("feedback.md" if name == "workspace" else name)
        if not path.is_file():
            raise SrError(
                f"шаблон текста ревью из карточки {card.get('id')!r} ({name}) не найден ({path})", 1
            )
        return path
    path = plugin_root() / "templates" / f"feedback.{lang}.md"
    if not path.is_file():
        raise SrError(
            f"шаблон текста ревью не найден: шаблон ставится вместе с промптами оценки ({path})", 1
        )
    return path


# --- сборка отправляемого блока ----------------------------------------------


def _session_status(session: dict) -> str:
    return session.get("status") or REVIEWED


def _status_name(status: str, lang: str) -> str:
    return _t("session_statuses", status, lang)


def _title(run: dict, lang: str) -> str:
    period = run.get("period") or {}
    if period.get("label"):
        return _t(ASSESS, "sent_title", lang).format(period=period["label"])
    d_from, d_to = _date_words(period.get("from"), lang), _date_words(period.get("to"), lang)
    if d_from or d_to:
        return _t(ASSESS, "sent_title_dates", lang).format(**{"from": d_from or d_to, "to": d_to or d_from})
    return _t(ASSESS, "sent_title_plain", lang)


def _quote(text: str, lang: str) -> str:
    return f"«{text}»" if lang == "ru" else f'"{text}"'


def _action_line(i: int, action: dict, lang: str) -> str:
    """`**<n>.** <date>, <place>: «<quote>»; <cost>. Стоит попробовать: <try>. Соседний рычаг: <lever>.`

    Поля как есть; поле без конечной точки получает точку; `date: null` → «<date>, » нет,
    первая буква `place` заглавная; пустой `levers` → хвоста нет.
    """
    ordinals = _t(ASSESS, "ordinals", lang).split(",")
    head = ordinals[i] if i < len(ordinals) else str(i + 1)
    place = (action.get("place") or "").strip()
    date = _date_words(action.get("date"), lang)
    lead = f"{date}, {place}" if date else _upper_first(place)
    quote = (action.get("quote") or "").strip()
    piece = f"{lead}: {_quote(quote, lang)}" if quote else lead
    cost = (action.get("cost") or "").strip()
    piece = f"{piece}; {_end(cost)}" if cost else _end(piece)
    parts = [f"**{head}.**", piece]
    tried = (action.get("try") or "").strip()
    if tried:
        parts.append(f"{_t(ASSESS, 'try_label', lang)}: {_end(tried)}")
    lever_label = _t(ASSESS, "lever_label", lang)
    for lever in action.get("levers") or []:
        if isinstance(lever, str) and lever.strip():
            parts.append(f"{lever_label}: {_end(lever.strip())}")
    return " ".join(parts)


def _criterion_line(crit: dict, lang: str) -> str:
    """`**<axis name>.** <verdict> <evidence>`; пустой `evidence` → строка кончается вердиктом."""
    name = _t("axes", str(crit.get("axis")), lang)
    line = f"**{name}.** {verdict_text(str(crit.get('verdict')), lang)}"
    evidence = (crit.get("evidence") or "").strip()
    return f"{line} {evidence}" if evidence else line


def render_sent(parts: dict, run: dict, card: dict, lang: str, template_text: str) -> str:
    """Отправляемый блок: заголовок с периодом, затем шаблон с подставленными частями.

    Шапка-комментарий шаблона вырезается; строка, где стоит один пустой плейсхолдер (пустой
    `prefix`, `closing: null`), удаляется вместе с пустой строкой после неё. `{{opening}}` —
    `feedback_parts.opening` как есть. Рычаги выводятся всегда: `short`/`practices: false`
    с рычагами ловит `check_forbidden`, молча их не срезаем. Неизвестный плейсхолдер остаётся
    как есть и ловится `check_forbidden`.
    """
    fb = card.get("feedback") or {}
    actions = "\n\n".join(_action_line(i, a, lang)
                          for i, a in enumerate(parts.get("actions") or []) if isinstance(a, dict))
    criteria = "\n\n".join(_criterion_line(c, lang)
                           for c in parts.get("criteria") or [] if isinstance(c, dict))
    values = {
        "prefix": (fb.get("prefix") or "").strip(),
        "opening": (parts.get("opening") or "").strip(),
        "actions": actions,
        "criteria": criteria,
        "closing": (parts.get("closing") or "").strip(),
    }
    out: list[str] = []
    drop_blank = False
    for line in _COMMENT_RE.sub("", template_text).strip("\n").splitlines():
        only = _ONLY_PLACEHOLDER_RE.match(line)
        if only and only.group(1) in values and not values[only.group(1)]:
            drop_blank = True
            continue
        if drop_blank and not line.strip():
            drop_blank = False
            continue
        drop_blank = False
        out.append(_PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1), m.group(0)), line))
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    return f"**{_title(run, lang)}**\n\n{body}"


# --- запреты -----------------------------------------------------------------


def _sentences(text: str) -> int:
    return len([s for s in _SENTENCE_SPLIT_RE.split(prose_outside_quotes(text)) if s.strip()])


def _field_breaks(parts: dict) -> list[str]:
    """Поля `feedback_parts` с переносом строки: абзац пишется одной строкой."""
    fields = [("opening", parts.get("opening")), ("closing", parts.get("closing"))]
    for i, action in enumerate(parts.get("actions") or []):
        if isinstance(action, dict):
            fields += [(f"actions[{i}].{k}", action.get(k)) for k in _ACTION_FIELDS]
            fields += [(f"actions[{i}].levers[{j}]", lever) for j, lever in enumerate(action.get("levers") or [])]
    for i, crit in enumerate(parts.get("criteria") or []):
        if isinstance(crit, dict):
            fields.append((f"criteria[{i}].evidence", crit.get("evidence")))
    return [f"перенос строки внутри поля feedback_parts.{name}: абзац пишется одной строкой"
            for name, value in fields if isinstance(value, str) and ("\n" in value or "\r" in value)]


def check_forbidden(text: str, parts: dict, card: dict, lang: str) -> list[str]:
    """Нарушения запретов отправляемого блока (пусто — можно писать).

    По строкам всего текста: заголовки `#`, голая линейка, code-fence, неподставленный
    `{{…}}`. По прозе (без содержимого кавычек и без значений полей `quote`) — условие 9
    «Проверок скриптом» (`text_rules.prose_problems`): баллы и шкала, уровни, идентификаторы,
    длинное тире, сравнения, обращение против `address`, маркеры старой формы, `>` вне цитат.
    По частям: пустое вступление, перенос строки в поле, число пунктов (`short` ≤ 2,
    `detailed` ≤ 3), рычаги при `short` или `practices: false`, в `short` у критерия не больше
    одной фразы после вердикта.
    """
    problems: list[str] = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            problems.append(f"заголовок через решётку в тексте: {line.strip()!r}")
        if _HR_RE.match(line):
            problems.append(f"голая линейка {line.strip()!r}: по ней режется файл ревью")
        if line.lstrip().startswith("```"):
            problems.append("code-fence в тексте: по нему парсер отчёта теряет линейки")
    for left in _LEFT_RE.findall(text):
        problems.append(f"неподставленный плейсхолдер {left}")

    prose = text
    for action in parts.get("actions") or []:
        quote = action.get("quote") if isinstance(action, dict) else None
        if isinstance(quote, str) and quote.strip():
            prose = prose.replace(quote, " ")
    fb = card.get("feedback") or {}
    problems += text_rules.prose_problems(prose_outside_quotes(prose), fb.get("address"))

    opening = parts.get("opening")
    if not isinstance(opening, str) or not opening.strip():
        problems.append("пустое вступление feedback_parts.opening: строка покрытия обязательна")
    problems += _field_breaks(parts)
    length = fb.get("length") or "detailed"
    actions = [a for a in parts.get("actions") or [] if isinstance(a, dict)]
    limit = MAX_ACTIONS.get(length, MAX_ACTIONS["detailed"])
    if len(actions) > limit:
        problems.append(f"length: {length}: пунктов {len(actions)}, нужно не больше {limit}")
    levers = sum(len(a.get("levers") or []) for a in actions if isinstance(a.get("levers"), list))
    if levers and length == "short":
        problems.append(f"length: short: рычагов {levers}, нужно 0 (рендер их не срезает)")
    if levers and fb.get("practices") is False:
        problems.append(f"practices: false: рычагов {levers}, нужно 0 (рендер их не срезает)")
    if length == "short":
        for crit in parts.get("criteria") or []:
            if isinstance(crit, dict) and _sentences(crit.get("evidence") or "") > 1:
                axis = crit.get("axis")
                problems.append(
                    f"length: short: у критерия {axis} ({_t('axes', str(axis), lang)}) больше одной фразы после вердикта"
                )
    return problems


# --- файл ревью --------------------------------------------------------------


def status_for(cfg: dict) -> str:
    """`self` → approved; `team`/`course` с `reviewer_gate` → draft, без гейта → approved."""
    if cfg.get("mode") != "self" and cfg.get("reviewer_gate"):
        return "draft"
    return "approved"


def _period_value(run: dict) -> str:
    period = run.get("period") or {}
    if period.get("label"):
        return str(period["label"])
    dates = [str(period[k])[:10] for k in ("from", "to") if isinstance(period.get(k), str)]
    return "..".join(dates) if dates else str(run.get("run_id"))


def _inside(path: Path, state_dir: Path) -> str | None:
    """Путь `path` от `state_dir` в posix-виде; вне него → None (сверка и как есть, и по resolve)."""
    from lib.workspace import historical_path
    path = historical_path(path, state_dir)
    for p, base in ((path, state_dir), (path.resolve(), state_dir.resolve())):
        try:
            return p.relative_to(base).as_posix()
        except ValueError:
            continue
    return None


def shown_path(path: str, state_dir: Path | None) -> str:
    """Путь сессии для файла ревью: без абсолютных путей машины (файл пересылают).

    Абсолютный внутри `.retropair/` → относительный от неё (`logs/<id>/<file>`);
    абсолютный вне workspace (или workspace неизвестен) → только имя файла; относительный
    (так пишет судья: от `.retropair/`) и пустой — как есть. Трекер не переписывается.
    """
    if not path or not Path(path).is_absolute():
        return path
    rel = _inside(Path(path), state_dir) if state_dir is not None else None
    return rel if rel is not None else Path(path).name


def _log_path(path: str, state_dir: Path | None) -> str:
    """Аргумент `sr.py normalize` из корня проекта: путь внутри workspace → `.retropair/<rel>`,
    абсолютный вне workspace → только имя файла (адрес машины в файл ревью не пишется)."""
    if not path:
        return path
    p = Path(path)
    if p.is_absolute():
        rel = _inside(p, state_dir) if state_dir is not None else None
        return f".retropair/{rel}" if rel is not None else p.name
    return f".retropair/{path}"


def _service(run: dict, card: dict, cfg: dict, lang: str, template_name: str,
             state_dir: Path | None = None) -> list[str]:
    none = _t(ASSESS, "none", lang)
    sessions = [s for s in run.get("sessions_reviewed") or [] if isinstance(s, dict)]
    reviewed = [s for s in sessions if _session_status(s) == REVIEWED]
    left = []
    for s in sessions:
        if _session_status(s) != REVIEWED:
            note = f": {s['status_note']}" if s.get("status_note") else ""
            left.append(f"`{shown_path(str(s.get('path') or ''), state_dir) or s.get('session_id')}` ({_status_name(_session_status(s), lang)}{note})")
    left += [f"`{sid}` ({_t(ASSESS, 'service_skipped_known', lang)})" for sid in run.get("sessions_skipped_known") or []]
    period = run.get("period") or {}
    dates = "..".join(str(period[k])[:10] for k in ("from", "to") if isinstance(period.get(k), str))
    period_text = ", ".join(x for x in (period.get("label"), dates) if x) or none
    revision = _t(ASSESS, "service_revision", lang).format(n=run.get("revision"))
    sessions_value = _t(ASSESS, "service_sessions_value", lang).format(
        n=len(reviewed), total=run.get("sessions_total") or len(sessions), batches=run.get("batches") or 1)
    warnings = ", ".join(str(w) for w in run.get("warnings") or []) or none
    length = (card.get("feedback") or {}).get("length")
    return [
        "<!-- service -->",
        f"*{_t(ASSESS, 'service_note', lang)}*",
        "",
        f"**{_t(ASSESS, 'service_run', lang)}:** `{run.get('run_id')}` · {revision} · {run.get('reviewer')}",
        f"**{_t(ASSESS, 'service_sessions', lang)}:** {sessions_value}",
        f"**{_t(ASSESS, 'service_period', lang)}:** {period_text}",
        f"**{_t(ASSESS, 'service_left_out', lang)}:** {'; '.join(left) or none}",
        f"**{_t(ASSESS, 'service_warnings', lang)}:** {warnings}",
        f"**{_t(ASSESS, 'service_template', lang)}:** {template_name} · "
        f"**{_t(ASSESS, 'service_length', lang)}:** {length} · mode: {cfg.get('mode')}",
    ]


def _dossier(run: dict, lang: str, state_dir: Path | None = None) -> list[str]:
    none = _t(ASSESS, "none", lang)
    cols = _t(ASSESS, "dossier_columns", lang).split(",")
    lines = [f"## {_t(ASSESS, 'dossier_title', lang)}", "",
             "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    slices = []
    for s in run.get("sessions_reviewed") or []:
        if not isinstance(s, dict):
            continue
        path = str(s.get("path") or "")
        m = _LEADING_DATE_RE.match(Path(path).name)
        from_turn = s.get("reviewed_from_turn")
        lines.append(f"| {m.group(1) if m else ''} | `{shown_path(path, state_dir)}` | "
                     f"`{s.get('session_id') or ''}` | "
                     f"{_status_name(_session_status(s), lang)} | {from_turn if from_turn is not None else 0} |")
        if _session_status(s) == REVIEWED and path:
            tail = f" --from-turn {from_turn}" if from_turn is not None else ""
            slices.append(f"- `sr.py normalize {_log_path(path, state_dir)}{tail}`")
    lines += ["", f"**{_t(ASSESS, 'dossier_slices', lang)}:**"] + (slices or [none])
    recs = [f"- `{r.get('id')}` ({r.get('axis')}): {r.get('text')}"
            for r in run.get("recommendations") or [] if isinstance(r, dict)]
    lines += ["", f"**{_t(ASSESS, 'dossier_recs', lang)}:**"] + (recs or [none])
    prev = []
    for v in run.get("prev_recommendations") or []:
        if isinstance(v, dict):
            note = f" {v['note']}" if v.get("note") else ""
            prev.append(f"- `{v.get('id')}`: {_end(_t('rec_verdicts', str(v.get('verdict')), lang))}{note}")
    lines += ["", f"**{_t(ASSESS, 'dossier_prev', lang)}:**"] + (prev or [none])
    return lines


def build_file(run: dict, card: dict, cfg: dict, sent: str, lang: str, template_name: str,
               state_dir: Path | None = None) -> str:
    """Файл ревью: frontmatter, служебный блок, `---`, отправляемый блок, `---`, досье.

    Пути сессий идут через `shown_path` от `state_dir` (`.retropair/` workspace): абсолютные
    пути машины в файл, который пересылают, не попадают.
    """
    front = ["---", f"person_id: {run.get('person_id')}", f"run_id: {run.get('run_id')}",
             f"period: {_period_value(run)}", f"status: {status_for(cfg)}", f"lang: {lang}",
             f"template: {template_name}", "---"]
    lines = (front + _service(run, card, cfg, lang, template_name, state_dir)
             + ["", "---", "", sent.strip(), "", "---", ""])
    return "\n".join(lines + _dossier(run, lang, state_dir)) + "\n"


def review_path(root: Path, person_id: str, run_id: str) -> Path:
    return ws_paths(root)["reviews"] / person_id / f"{run_id}.md"


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mask = os.umask(0)
        os.umask(mask)
        mode = 0o666 & ~mask
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _pick_run(tr: dict, person_id: str, run_id: str | None) -> dict:
    """Живой прогон человека: названный или последний; иначе `SrError(1)`."""
    if run_id is None:
        runs = tracker.live_runs(tr, person_id)
        if not runs:
            raise SrError(f"у {person_id} нет живых прогонов в трекере", 1)
        return runs[-1]
    run = tracker.find_run(tr, run_id)
    if run.get("person_id") != person_id:
        raise SrError(f"прогон {run_id} принадлежит {run.get('person_id')!r}, а не {person_id!r}", 1)
    if not tracker.is_live(run):
        raise SrError(f"прогон {run_id} отозван: {(run.get('retracted') or {}).get('why')}", 1)
    return run


def write_review(root: Path, judge_path, person_id: str, run_id: str | None = None,
                 template: str | None = None, now: datetime | None = None) -> Path:
    """Собрать текст ревью прогона и записать `reviews/<person>/<run_id>.md`.

    Порядок: карточка (нет → 2) → файл судьи (нет → 2) → прогон (`run_id` или последний живой;
    нет → 1) → sha256 файла = `run.source.sha256` (иначе 1) → `feedback_parts` → шаблон →
    сборка → запреты (нарушение → 1, ничего не пишется) → `feedback_draft` и `review_status`
    в прогон → файл.
    """
    root = Path(root)
    card = people.load_person(root, person_id)
    cfg = load_config(root)
    lang = people.feedback_language(card, cfg)
    judge = Path(judge_path)
    if not judge.is_file():
        raise SrError(f"нет файла с выходом судьи ({judge})", 2)
    run = _pick_run(tracker.load(root), person_id, run_id)
    rid = run["run_id"]
    raw = judge.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    expected = (run.get("source") or {}).get("sha256")
    if sha != expected:
        raise SrError(
            f"sha256 выхода судьи {sha} не совпал с записанным в прогон {rid} ({expected}): "
            f"текст собирается только из того файла, что прошёл append ({judge})",
            1,
        )
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise SrError(f"выход судьи не разбирается как JSON: {err} ({judge})", 1) from err
    parts = (obj.get("data") or {}).get("feedback_parts") if isinstance(obj, dict) else None
    if not isinstance(parts, dict):
        raise SrError(f"в выходе судьи нет data.feedback_parts: тексту не из чего собираться ({judge})", 1)
    tpl = resolve_template(root, card, lang, template)
    sent = render_sent(parts, run, card, lang, tpl.read_text(encoding="utf-8"))
    problems = check_forbidden(sent, parts, card, lang)
    if problems:
        raise SrError(
            f"текст ревью {rid} не записан, нарушены запреты ({len(problems)}):\n  " + "\n  ".join(problems), 1
        )
    template_name = tpl.name if template is not None else (card.get("feedback") or {}).get("template") or "default"
    status = status_for(cfg)
    text = build_file(run, card, cfg, sent, lang, template_name, ws_paths(root)["state_dir"])
    tracker.update_run(root, rid, now=now, feedback_draft=sent, review_status=status)
    path = review_path(root, person_id, rid)
    _write_text_atomic(path, text)
    return path


def _frontmatter_status(lines: list[str], path: Path) -> int:
    """Индекс строки `status:` во frontmatter; нет frontmatter или строки → `SrError(1)`."""
    if not lines or lines[0].strip() != "---":
        raise SrError(f"файл ревью без frontmatter: статус менять негде ({path})", 1)
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            break
        if lines[i].startswith("status:"):
            return i
    raise SrError(f"во frontmatter файла ревью нет строки status ({path})", 1)


def approve(root: Path, person_id: str, run_id: str | None = None, now: datetime | None = None) -> dict:
    """Черновик → одобрено: `status` во frontmatter, `review_status` и `approved_at` прогона.

    Без `run_id` — последний живой прогон человека. Нет карточки → 2; нет прогона, файла,
    frontmatter → 1; уже одобрено и там и там → `changed: False`, ничего не пишется.
    """
    root = Path(root)
    people.load_person(root, person_id)
    run = _pick_run(tracker.load(root), person_id, run_id)
    rid = run["run_id"]
    path = review_path(root, person_id, rid)
    if not path.is_file():
        raise SrError(f"нет файла ревью прогона {rid}: сначала render-review ({path})", 1)
    lines = path.read_text(encoding="utf-8").split("\n")
    idx = _frontmatter_status(lines, path)
    current = lines[idx].split(":", 1)[1].strip()
    if current not in ("draft", "approved"):
        raise SrError(f"статус {current!r} во frontmatter не из draft|approved ({path})", 1)
    if current == "approved" and run.get("review_status") == "approved":
        return {"run_id": rid, "status": "approved", "changed": False}
    # approved_at — момент выдачи рекомендаций прогона при гейте ревьюера (open-recs, страж append)
    tracker.update_run(root, rid, now=now, review_status="approved", approved_at=marks.iso_utc(now))
    if current != "approved":
        lines[idx] = "status: approved"
        _write_text_atomic(path, "\n".join(lines))
    return {"run_id": rid, "status": "approved", "changed": True}
