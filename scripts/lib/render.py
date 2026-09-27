"""Сборка HTML отчётов и страницы workspaces, атомарная запись файла.

- `render_html(payload, i18n)` — самодостаточный HTML отчёта: шаблон `report_assets/report.html`,
  стили и скрипт встраиваются, данные — JSON в `<script type="application/json" id="data">`.
- `render_index(workspaces, lang=…, generated_at=…, i18n=…)` — страница со списком workspaces
  из реестра и ссылками `file://` на собранные отчёты.
- `write_html_atomic(path, html)` — запись через временный файл и `os.replace`.

Плейсхолдеры шаблона подставляются одним проходом: текст, пришедший из данных или заголовка,
повторно не сканируется, поэтому `__TITLE__` в тексте ревью так и останется текстом.
"""

from __future__ import annotations

import html
import json
import os
import re
import stat
import tempfile
from pathlib import Path

from lib.config import load_config
from lib.report_data import MODES
from lib.workspace import STATE_DIR, SrError, _current_umask

ASSETS = Path(__file__).resolve().parent / "report_assets"
LANGS = ("ru", "en")
REPORT_PLACEHOLDERS = ("LANG", "MODE", "TITLE", "CSS", "JS", "DATA_JSON")
INDEX_PLACEHOLDERS = ("LANG", "TITLE", "BODY")
_PLACEHOLDER_RE = re.compile(r"__([A-Z][A-Z_]*[A-Z])__")

# Подписи режимов workspace на странице workspaces (ключи — литералы, их проверяет тест).
_WS_MODE_KEYS = {"self": "idx_mode_self", "team": "idx_mode_team", "course": "idx_mode_course"}


# --- шаблоны ------------------------------------------------------------------------------

def _asset(name: str) -> str:
    path = ASSETS / name
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as err:
        raise SrError(f"файл шаблона отчёта не читается: {err} ({path})", 1) from err


def _fill(template: str, values: dict[str, str], name: str) -> str:
    """Подставляет `__KEY__` одним проходом; каждый плейсхолдер в шаблоне ровно один раз."""
    for key in values:
        count = template.count(f"__{key}__")
        if count != 1:
            raise SrError(
                f"шаблон {name}: плейсхолдер __{key}__ встречается {count} раз, нужен 1 "
                f"({ASSETS / name})", 1)

    def sub(match: re.Match) -> str:
        key = match.group(1)
        return values[key] if key in values else match.group(0)

    return _PLACEHOLDER_RE.sub(sub, template)


def _embed_json(value) -> str:
    """JSON для `<script type="application/json">`: `<` → `\\u003c`, тег не закрыть из данных."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


# --- отчёт ------------------------------------------------------------------------------

def render_html(payload: dict, i18n: dict) -> str:
    """HTML отчёта: в копию payload добавляется `i18n`, всё встраивается в один файл.

    `meta.lang` не из ru/en или `meta.mode` не из режимов отчёта → `SrError(2)`.
    Payload вызывающего не меняется.
    """
    meta = payload.get("meta") if isinstance(payload, dict) else None
    if not isinstance(meta, dict):
        raise SrError("payload отчёта без meta", 1)
    lang, mode, title = meta.get("lang"), meta.get("mode"), meta.get("title")
    if lang not in LANGS:
        raise SrError(f"язык отчёта {lang!r} не поддерживается, ожидается один из {LANGS}", 2)
    if mode not in MODES:
        raise SrError(f"режим отчёта {mode!r} неизвестен, ожидается один из {MODES}", 2)
    if not isinstance(title, str):
        raise SrError(f"заголовок отчёта не строка: {type(title).__name__}", 1)

    css, js = _asset("report.css"), _asset("report.js")
    if "</style" in css.lower():
        raise SrError(f"report.css содержит </style: стиль закроется раньше ({ASSETS / 'report.css'})", 1)
    if "</script" in js.lower():
        raise SrError(f"report.js содержит </script: скрипт закроется раньше ({ASSETS / 'report.js'})", 1)

    data = {**payload, "i18n": i18n}
    return _fill(_asset("report.html"), {
        "LANG": lang,
        "MODE": mode,
        "TITLE": html.escape(title),
        "CSS": css,
        "JS": js,
        "DATA_JSON": _embed_json(data),
    }, "report.html")


# --- страница workspaces ------------------------------------------------------------------

def _strings(i18n: dict, lang: str):
    section = i18n.get("report") if isinstance(i18n, dict) else None
    if not isinstance(section, dict):
        raise SrError("в словаре интерфейса нет секции report", 1)

    def s(key: str) -> str:
        entry = section.get(key)
        text = entry.get(lang) if isinstance(entry, dict) else None
        if not text:
            raise SrError(f"в словаре интерфейса нет строки report.{key} для языка {lang}", 1)
        return html.escape(text)

    return s


def _config_groups(root: Path) -> list[str] | None:
    """Группы из `<root>/.sessionretro/config.json`; конфиг не читается или не по схеме → None."""
    try:
        cfg = load_config(root)
    except (SrError, OSError, UnicodeDecodeError):
        return None
    groups = cfg.get("workspace", {}).get("groups")
    return [str(g) for g in groups] if isinstance(groups, list) else None


def _groups_cell(groups: list, s) -> str:
    if not groups:
        return f'<span class="muted">{s("idx_no_groups")}</span>'
    chips = "".join(f'<span class="chip">{html.escape(str(g))}</span>' for g in groups)
    return f'<div class="chips">{chips}</div>'


def _link(path: Path, label: str) -> str:
    return f'<li><a href="{html.escape(path.as_uri(), quote=True)}">{label}</a></li>'


def _off(label: str, s) -> str:
    return f'<li class="off">{label}: {s("idx_not_built")}</li>'


def _reports_cell(root: Path, s) -> str:
    reports = root / STATE_DIR / "reports"
    items = []
    for name, key in (("group.html", "idx_report_group"), ("reviewer.html", "idx_report_reviewer")):
        label = s(key)
        target = reports / name
        items.append(_link(target, label) if target.is_file() else _off(label, s))
    personal = reports / "personal"
    count = len(list(personal.glob("*.html"))) if personal.is_dir() else 0
    label = s("idx_report_personal")
    items.append(_link(personal, f"{label} ({count})") if count else _off(label, s))
    return f'<ul class="links">{"".join(items)}</ul>'


def _row(entry: dict, s) -> str:
    ws_id = entry.get("id")
    raw_path = entry.get("path")
    if not isinstance(ws_id, str) or not ws_id:
        raise SrError(f"реестр workspaces: запись без id ({entry!r})", 1)
    if not isinstance(raw_path, str) or not raw_path or not Path(raw_path).is_absolute():
        raise SrError(f"реестр workspaces: у записи нет абсолютного пути (id: {ws_id}, path: {raw_path!r})", 1)
    root = Path(raw_path)
    title = entry.get("title") or ws_id
    mode = entry.get("mode")
    mode_label = s(_WS_MODE_KEYS[mode]) if mode in _WS_MODE_KEYS else html.escape(str(mode))
    registry_groups = entry.get("groups") if isinstance(entry.get("groups"), list) else []

    if not root.is_dir():
        groups_html = _groups_cell(registry_groups, s)
        reports_html = f'<span class="chip warn">{s("idx_folder_missing")}</span>'
    else:
        groups = _config_groups(root)
        if groups is None:
            groups_html = (_groups_cell(registry_groups, s)
                           + f'<span class="note">{s("idx_groups_from_registry")}</span>')
        else:
            groups_html = _groups_cell(groups, s)
        reports_html = _reports_cell(root, s)

    last = entry.get("last_run_at")
    last_html = (f'<span class="tnum">{html.escape(str(last)[:16].replace("T", " "))}</span>'
                 if last else f'<span class="muted">{s("idx_never")}</span>')
    return (
        "<tr>"
        f'<td class="ws" data-label="{s("idx_col_workspace")}"><b>{html.escape(str(title))}</b>'
        f'<span class="id">{html.escape(ws_id)}</span>'
        f'<div class="path">{html.escape(raw_path)}</div></td>'
        f'<td data-label="{s("idx_col_mode")}"><span class="chip mode">{mode_label}</span></td>'
        f'<td data-label="{s("idx_col_groups")}">{groups_html}</td>'
        f'<td data-label="{s("idx_col_last_run")}">{last_html}</td>'
        f'<td data-label="{s("idx_col_reports")}">{reports_html}</td>'
        "</tr>"
    )


def render_index(workspaces: list[dict], *, lang: str, generated_at: str, i18n: dict) -> str:
    """Страница workspaces из реестра: название, режим, группы, последний прогон, ссылки.

    Группы берутся из конфига workspace; конфиг не читается — из реестра с пометкой.
    Есть файл отчёта — ссылка `file://`, нет — «не собран»; нет папки — «папка не найдена».
    Запись реестра без id или абсолютного пути → `SrError(1)`.
    """
    if lang not in LANGS:
        raise SrError(f"язык страницы {lang!r} не поддерживается, ожидается один из {LANGS}", 2)
    s = _strings(i18n, lang)
    head = (
        "<thead><tr>"
        f'<th scope="col">{s("idx_col_workspace")}</th>'
        f'<th scope="col">{s("idx_col_mode")}</th>'
        f'<th scope="col">{s("idx_col_groups")}</th>'
        f'<th scope="col">{s("idx_col_last_run")}</th>'
        f'<th scope="col">{s("idx_col_reports")}</th>'
        "</tr></thead>"
    )
    if workspaces:
        rows = "".join(_row(entry, s) for entry in workspaces)
        table = f'<div class="tbl-scroll"><table>{head}<tbody>{rows}</tbody></table></div>'
    else:
        table = f'<p class="empty">{s("idx_empty")}</p>'
    meta = f'{s("idx_generated")} <span class="tnum">{html.escape(generated_at)}</span>' \
        if generated_at else ""
    body = (
        f'<h1>{s("idx_title")}</h1>'
        f'<p class="meta">{meta}</p>'
        f'<section class="card">{table}</section>'
    )
    return _fill(_asset("index.html"), {
        "LANG": lang,
        "TITLE": s("idx_title"),
        "BODY": body,
    }, "index.html")


# --- запись -------------------------------------------------------------------------------

def write_html_atomic(path: Path, html_text: str, *, min_bytes: int = 2000) -> int:
    """Пишет HTML через временный файл в той же папке и `os.replace`; возвращает размер в байтах.

    Меньше `min_bytes` байт → `SrError(1)`, файл не трогается (страница workspaces
    вызывает с `min_bytes=0`). Права — как у существующего файла или `0o666 & ~umask`.
    """
    path = Path(path)
    data = html_text.encode("utf-8")
    if len(data) < min_bytes:
        raise SrError(
            f"HTML подозрительно мал: {len(data)} байт при минимуме {min_bytes}, файл не записан ({path})", 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = 0o666 & ~_current_umask()
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
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
    return len(data)
