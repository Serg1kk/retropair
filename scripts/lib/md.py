"""Файл ревью и минимальный markdown → HTML.

Формат файла `reviews/<id>/<run_id>.md`:

    ---
    person_id: …
    run_id: …
    period: …
    status: draft | approved
    lang: ru | en
    template: …
    ---
    <!-- service -->
    служебная шапка (в отправку не идёт)
    ---
    отправляемый блок (текст адресату)
    ---
    ## Досье …  (остаток файла, только для ревьюера)

Линейки `---` внутри code-fence не считаются. Файл без frontmatter, без маркера
`<!-- service -->` первой непустой строкой тела или без двух линеек не распознаётся:
весь текст уходит в досье, отправляемый блок пустой — адресату такое не попадает.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

from lib.workspace import SrError

STATUSES = ("draft", "approved")
SERVICE_MARK = "<!-- service -->"

_HR_RE = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")
_FM_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$")


def _strip_edges(lines: list[str]) -> list[str]:
    """Убрать пустые строки и линейки по краям блока."""
    a, b = 0, len(lines)
    while a < b and (not lines[a].strip() or _HR_RE.match(lines[a])):
        a += 1
    while b > a and (not lines[b - 1].strip() or _HR_RE.match(lines[b - 1])):
        b -= 1
    return lines[a:b]


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _frontmatter(lines: list[str]) -> tuple[dict | None, int]:
    """Frontmatter `---` … `---` в начале файла → (meta, индекс первой строки тела).

    Нет открывающей или закрывающей линейки → (None, 0).
    """
    if not lines or lines[0].strip() != "---":
        return None, 0
    meta: dict[str, str] = {}
    for i in range(1, len(lines)):
        line = lines[i]
        if line.strip() == "---":
            return meta, i + 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _FM_LINE_RE.match(line)
        if m:
            meta[m.group(1)] = _unquote(m.group(2))
    return None, 0


def _rules_outside_fence(lines: list[str]) -> list[int]:
    rules, fence = [], False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            fence = not fence
            continue
        if not fence and _HR_RE.match(line):
            rules.append(i)
    return rules


def _join(lines: list[str]) -> str:
    return "\n".join(_strip_edges(lines)).strip()


def parse_review(text: str) -> dict:
    """Разбор файла ревью: `{meta, service_md, sent_md, dossier_md, recognized, warning}`.

    `meta.status` вне `draft|approved` (или его нет) → считается draft, warning с исходным
    значением. Нет frontmatter, маркера `<!-- service -->` первой непустой строкой тела или
    двух линеек после него → recognized False, всё в досье.
    """
    text = (text or "").lstrip("﻿")
    lines = text.splitlines()
    warnings: list[str] = []
    meta, body_start = _frontmatter(lines)

    def result(recognized: bool, service: str = "", sent: str = "", dossier: str = "") -> dict:
        return {
            "meta": meta if meta is not None else {},
            "service_md": service,
            "sent_md": sent,
            "dossier_md": dossier,
            "recognized": recognized,
            "warning": "; ".join(warnings) if warnings else None,
        }

    if meta is not None:
        raw_status = meta.get("status")
        if raw_status not in STATUSES:
            warnings.append(f"status {raw_status!r} не из draft|approved, считается draft")
            meta["status"] = "draft"

    if meta is None:
        warnings.insert(0, "нет frontmatter (`---` в начале файла), весь текст в досье")
        return result(False, dossier=text.strip())

    body = lines[body_start:]
    first_line = next((ln.strip() for ln in body if ln.strip()), "")
    if first_line != SERVICE_MARK:
        warnings.insert(0, f"нет маркера {SERVICE_MARK} первой строкой после frontmatter; "
                           "весь текст в досье")
        return result(False, dossier=text.strip())
    rules = _rules_outside_fence(body)
    if len(rules) < 2:
        warnings.insert(0, f"после frontmatter нужно две линейки `---`, найдено {len(rules)}; "
                           "весь текст в досье")
        return result(False, dossier=text.strip())

    first, second = rules[0], rules[1]
    service_lines = [ln for ln in body[:first] if ln.strip() != SERVICE_MARK]
    sent = _join(body[first + 1:second])
    if not sent:
        warnings.insert(0, "отправляемый блок пустой, весь текст в досье")
        return result(False, dossier=text.strip())
    return result(True, service=_join(service_lines), sent=sent,
                  dossier=_join(body[second + 1:]))


# --- markdown → HTML ------------------------------------------------------------------

_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")
_LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_HEAD_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def _md_inline(text: str) -> str:
    parts = _INLINE_CODE_RE.split(text)
    out = []
    for i, part in enumerate(parts):
        if i % 2:
            out.append(f"<code>{html.escape(part)}</code>")
        else:
            s = html.escape(part, quote=False)
            s = _BOLD_RE.sub(r"<b>\1</b>", s)
            s = _ITALIC_RE.sub(r"<i>\1</i>", s)
            out.append(s)
    return "".join(out)


def _md_is_block_start(line: str) -> bool:
    s = line.lstrip()
    return bool(_HEAD_RE.match(line) or _HR_RE.match(line) or s.startswith("```")
                or s.startswith("|") or s.startswith(">") or _LIST_RE.match(line))


def _md_table(rows: list[str]) -> str:
    def split(row: str) -> list[str]:
        cells = re.split(r"(?<!\\)\|", row.strip().strip("|"))
        return [c.replace("\\|", "|").strip() for c in cells]

    cells = [split(r) for r in rows]
    header, body = None, cells
    if len(cells) >= 2 and all(re.match(r"^:?-+:?$", c) for c in cells[1] if c) and any(cells[1]):
        header, body = cells[0], cells[2:]
    out = '<div class="tbl"><table>'
    if header:
        out += "<thead><tr>" + "".join(f"<th>{_md_inline(c)}</th>" for c in header) + "</tr></thead>"
    out += "<tbody>" + "".join(
        "<tr>" + "".join(f"<td>{_md_inline(c)}</td>" for c in r) + "</tr>" for r in body
    )
    return out + "</tbody></table></div>"


def md_to_html(text: str) -> str:
    """Минимальный markdown → HTML: заголовки, абзацы, **жирный**, *курсив*, `код`,
    списки -/1., цитаты >, таблицы | a | b |, code-fence, линейки. HTML-комментарии
    выкидываются, остальной HTML экранируется. Ссылки не поддерживаются намеренно."""
    text = re.sub(r"<!--.*?-->", "", text or "", flags=re.S)
    lines = text.splitlines()
    out, i, n = [], 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if line.lstrip().startswith("```"):
            j, buf = i + 1, []
            while j < n and not lines[j].lstrip().startswith("```"):
                buf.append(lines[j])
                j += 1
            out.append("<pre><code>" + html.escape("\n".join(buf)) + "</code></pre>")
            i = j + 1
            continue
        m = _HEAD_RE.match(line)
        if m:
            level = len(m.group(1))
            tag = "h3" if level <= 2 else "h4" if level == 3 else "h5"
            out.append(f"<{tag}>{_md_inline(m.group(2).strip())}</{tag}>")
            i += 1
            continue
        if _HR_RE.match(line):
            out.append("<hr>")
            i += 1
            continue
        if line.lstrip().startswith("|"):
            j, rows = i, []
            while j < n and lines[j].lstrip().startswith("|"):
                rows.append(lines[j])
                j += 1
            out.append(_md_table(rows))
            i = j
            continue
        if line.lstrip().startswith(">"):
            j, buf = i, []
            while j < n and lines[j].lstrip().startswith(">"):
                buf.append(re.sub(r"^\s*>\s?", "", lines[j]))
                j += 1
            out.append("<blockquote>" + md_to_html("\n".join(buf)) + "</blockquote>")
            i = j
            continue
        lm = _LIST_RE.match(line)
        if lm:
            ordered = lm.group(2)[0].isdigit()
            items, j = [], i
            while j < n:
                lm2 = _LIST_RE.match(lines[j])
                if lm2 and lm2.group(2)[0].isdigit() == ordered:
                    items.append(lm2.group(3).strip())
                elif (items and lines[j].strip() and lines[j][:1] in (" ", "\t")
                      and not _md_is_block_start(lines[j].lstrip())):
                    items[-1] += " " + lines[j].strip()
                else:
                    break
                j += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{_md_inline(it)}</li>" for it in items) + f"</{tag}>")
            i = j
            continue
        j, buf = i, []
        while j < n and lines[j].strip() and (j == i or not _md_is_block_start(lines[j])):
            buf.append(lines[j].strip())
            j += 1
        out.append("<p>" + _md_inline(" ".join(buf)) + "</p>")
        i = j
    return "".join(out)


# --- папка ревью ----------------------------------------------------------------------

def load_reviews(reviews_dir: Path) -> tuple[dict[str, dict], list[str]]:
    """`reviews/<id>/*.md` → ({run_id: разбор + path + status}, предупреждения).

    Нет папки → `({}, [])`. Файл без `run_id` во frontmatter — предупреждение, в словарь
    не попадает. Нераспознанная структура или неизвестный status — предупреждение, файл
    в словаре (с пустым отправляемым блоком или как draft). Повтор `run_id` — предупреждение,
    остаётся первый по сортировке пути.
    """
    reviews_dir = Path(reviews_dir)
    found: dict[str, dict] = {}
    warnings: list[str] = []
    if not reviews_dir.is_dir():
        return found, warnings
    for path in sorted(reviews_dir.rglob("*.md")):
        rel = "reviews/" + path.relative_to(reviews_dir).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as err:
            raise SrError(f"файл ревью не читается: {err} ({path})", 1) from err
        parsed = parse_review(text)
        run_id = parsed["meta"].get("run_id")
        if not run_id:
            warnings.append(f"нет run_id во frontmatter, файл пропущен: {rel}")
            continue
        if parsed["warning"]:
            warnings.append(f"{rel}: {parsed['warning']}")
        if run_id in found:
            warnings.append(f"повтор run_id {run_id}: {rel} пропущен, взят {found[run_id]['path']}")
            continue
        parsed["path"] = rel
        parsed["status"] = parsed["meta"].get("status", "draft")
        found[run_id] = parsed
    return found, warnings
