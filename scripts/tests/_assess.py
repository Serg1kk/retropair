"""Хелперы тестов движка оценки: workspace с индексом логов, выход судьи.

Схемы коллектора и судьи — настоящие (`references/schemas/{collector,judge}.json`), шаблоны
текста — настоящие (`templates/feedback.{ru,en}.md`). Фикстуры лежат в `tests/fixtures/assess/`:

- `judge-alex-p1.json`, `judge-alex-p2.json` — валидные выходы судьи для `alex` (p2 отвечает на
  рекомендации p1 в `prev_recommendations`);
- `batch-ok.json` — валидный выход коллектора (одна пачка `alex`);
- `logs-index.json` — индекс логов в формате сбора (`inventory.merge`): `{schema_version: 1,
  workspace: "demo-team", updated_at, logs: [], warnings: []}`. Поля записи, по порядку:
  `person_id, source{type,id}, session_id, key, path` (относительно `.retropair/`,
  `logs/<person>/<key>.md`), `source_path, format, sha, bytes, turns_total, user_turns,
  log_started_at, log_ended_at, started_at_source, collector, models, period_guess,
  period_guess_source, metrics, sanitize, reviewed, reviewed_in_run, reviewed_at,
  reviewed_turns_total, reviewed_bytes, state`, у `grown` ещё `review_from_turn`; поля
  `excluded*` и `duplicate_of` появляются только у исключённых записей и дублей. Записи: `alex` — две `new`, одна `grown`
  (`reviewed_turns_total: 10`, `review_from_turn: 10`), одна короткая (`user_turns: 2`), и у
  одной из его записей тот же хвост пути, что у записи `sam`; `sam` — две `new`, одна `gone`.
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

from _util import FIXTURES, PLUGIN, copy_fixture

ASSESS = FIXTURES / "assess"
TEMPLATE_RU = PLUGIN / "templates" / "feedback.ru.md"
TEMPLATE_EN = PLUGIN / "templates" / "feedback.en.md"

KIM_CARD = """id: "kim"                   # вымышленный участник демо-команды
name: "Kim Sample"
group: team-a
profile:
  role: data engineer
  grade: junior
  years: 1
  stack: Python, Airflow
  tools: Claude Code
  constraints: ""
feedback:
  address: ты
  opening_summary: true
  mark_sections: true
  prefix: "Привет!"
sources:
consent:
  logs: true
  date: 2026-09-01
"""


def make_ws(tmp: Path) -> Path:
    """Копия `workspace-min` + `logs-index.json` из фикстуры + карточка `kim`; вернуть корень."""
    root = copy_fixture("workspace-min", Path(tmp))
    state = root / ".retropair"
    shutil.copyfile(ASSESS / "logs-index.json", state / "logs-index.json")
    (state / "people" / "kim.yaml").write_text(KIM_CARD, encoding="utf-8")
    return root


def payload(name: str = "judge-alex-p1.json", **patch) -> dict:
    """Глубокая копия выхода судьи из фикстуры; `patch` заменяет ключи в `data`."""
    path = ASSESS / name
    if not path.is_file():
        raise FileNotFoundError(f"retropair: фикстура судьи не найдена ({path})")
    obj = json.loads(path.read_text(encoding="utf-8"))
    for key, value in patch.items():
        obj["data"][key] = copy.deepcopy(value)
    return obj
