# Вход агентов оценки: коллектор, судья, критик

judge_input_version: v2

> Контракт между скиллом `review` и тремя агентами плагина (`log-collector`, `log-judge`,
> `feedback-critic`): что скилл кладёт в задание агенту, куда агент пишет выход и что
> проверяет скрипт после него. Задание передаётся текстом, одним блоком JSON с полями ниже.
> Правка этого файла бампит строку версии выше и строки версий агентов, которые его читают.

Если в пути виден литерал `${CLAUDE_PLUGIN_ROOT}`, подставь `plugin_root` из входа.

Хост не подставляет переменную в тело агента, поэтому все пути вида `${CLAUDE_PLUGIN_ROOT}/…`
в промптах и референсах агент читает от абсолютного `plugin_root`.

**Обязательного поля нет во входе → агент ничего не пишет в `output_path` и первой строкой
ответа называет недостающее поле.** Значение по умолчанию он не придумывает: вход без поля
это ошибка скилла, и молча её закрыть значит спрятать её.

## Вход коллектора и судьи

| Поле | Кому | Тип | Что это |
|---|---|---|---|
| `plugin_root` | оба | строка | Абсолютный путь к корню плагина. От него читаются референсы, схемы и `rp.py` |
| `workspace_root` | оба | строка | Абсолютный путь к папке `.retropair/` проекта. Агент из неё только читает; пишет он в один файл, `output_path` |
| `workspace_id` | оба | строка | id workspace из его `config.json`. Коллектор копирует его в `batch.workspace` |
| `person_id` | оба | строка | id карточки участника (`[a-z0-9-]+`). Из него собирается id рекомендации `REC-<person_id>-<NNN>` |
| `period{label,from,to,anchor}` | оба | объект | `label` имя периода, обязательно; `from`, `to` календарные края `YYYY-MM-DD` или `null`; `anchor` опорная точка периода (дата встречи, тег релиза) или `null`. Коллектор копирует объект в `batch.period` как есть |
| `run_id` | оба | строка | id прогона. Агент называет его в сводке ответа; в JSON выхода его нет |
| `lang` | оба | `ru` или `en` | Язык служебных строк (`reading`, `note`, `batch_note`) и текста адресату. Цитаты остаются на языке лога |
| `output_path` | оба | строка | Абсолютный путь к файлу-стабу в `<workspace_root>/runs/pending/`, созданному скиллом. Агент читает стаб, затем перезаписывает его целиком одним JSON по своей схеме. Других файлов не создаёт и не правит |
| `log_paths[]` | оба | массив объектов | По элементу на сессию: `path` абсолютный путь к санитайзированной копии лога в `<workspace_root>/logs/<person_id>/`; `session_id` id сессии или `null`; `state` `new` или `grown`; `from_turn` номер хода, с которого читать хвост (обязателен при `grown`), иначе `null`. Коллектору подаются логи его пачки целыми сессиями; судье все логи прогона, только для точечной сверки цитат |
| `batch{index,total}` | коллектор | объект | `index` номер пачки с единицы, `total` сколько пачек у участника за период. Копируются в `batch.index` и `batch.total` |
| `collector_outputs[]` | судья | массив строк | Абсолютные пути к JSON коллекторов этого прогона, прошедшим проверку скриптом, по одному на пачку, в порядке `batch.index` |
| `excluded_sessions[]` | судья | массив объектов | Сессии, которые скилл в пачки не отправлял: `{session_id, path, status, status_note}` со статусом `duplicate_export`, `other_period`, `not_batched` или `transcript_too_short` (короче порога до нарезки). Судья переносит их в `session_scope.sessions[]` с тем же статусом. `other_period` запись индекса не закрывает: сессию оценит прогон её периода. Таких нет → пустой массив |
| `sessions_total` | судья | целое | Сколько сессий у участника за период по инвентарю, включая не поданные в прогон. Идёт в `metadata.sessions_total` |
| `card_slice` | судья | строка или `null` | Срез карточки участника, 5–8 строк: `profile` (`role`, `grade`, `years`, `stack`, `tools`, `constraints`) и `feedback` без `channel`, `template`, `prefix`. Коллектору не подаётся: профиль тянет подгонять чтение под грейд. Карточки нет → `null`, судья ставит `no_person_profile` в предупреждения. Скилл `review` без карточки прогон не начинает: `append` пишет прогон только человеку с карточкой |
| `task_summary` | оба | строка | Нейтральная однострочная сводка, о чём была работа, со слов пользователя, без критериев приёмки; либо «нет» |
| `open_recs[]{id,text,period,issued_at,evaluable}` | оба | массив объектов | Открытые рекомендации прошлых периодов из трекера: `id` вида `REC-<person_id>-<NNN>`, `text` само действие, `period` `label` периода, где она выдана, `issued_at` когда человек её получил (время прогона; при гейте ревьюера время approve), `evaluable` есть ли среди сессий этого прогона хоть одна, законченная после `issued_at` (считает `open-recs --period-label`, не модель). `evaluable: false` → судья ставит `not_applicable_yet`, коллектор сигналов по ней не собирает. Только `open` из `open-recs`: рекомендации неодобренного черновика (`pending_approval`) человек не получал, они не подаются. Коллектор приносит по ним `rec_signals`, вердикт ставит судья. Прошлых нет → пустой массив |
| `context_report` | оба, необязательно | строка или `null` | Путь к сводке по управлению окном (ходы, пик заполнения, компакты). Сбор логов такой сводки не строит, скилл подаёт `null` → предупреждение `no_context_report` |
| `repo_path` | оба, необязательно | строка или `null` | Корень репозитория участника, если он доступен (в режиме self это сам проект). `null` → в репозиторий не ходить; где это объясняет отсутствие следов, причина `no_repo_access` |
| `show_scores` | скрипт | логическое | Показывать ли баллы и уровень в личном отчёте и в сводке режима self (по умолчанию `true`; `false` — только динамика). Агентам ничего не меняет: судья ставит баллы всегда и в текст адресату не пишет никогда |

## Выход коллектора и судьи

- **JSON в `output_path`, перезаписью файла.** Коллектор пишет по схеме
  `${CLAUDE_PLUGIN_ROOT}/references/schemas/collector.json`, судья по схеме
  `${CLAUDE_PLUGIN_ROOT}/references/schemas/judge.json`. Без markdown-обёртки, без преамбулы.
- **Текстом в ответ не больше десяти строк сводки:** `run_id`, `output_path`, что прочитано
  и что исключено, сколько материала собрано или осей оценено, предупреждения. JSON в ответе
  не повторяется: скрипт читает файл, а не ответ.
- Записать файл не удалось → первой строкой ответа причина и путь. Молча отдать JSON текстом
  вместо файла нельзя: скилл его не ждёт.

## Вход критика

| Поле | Тип | Что это |
|---|---|---|
| `plugin_root` | строка | Абсолютный путь к корню плагина |
| `lang` | `ru` или `en` | Язык ответа критика |
| `draft_path` | строка | Абсолютный путь к тексту адресату, собранному рендером из `feedback_parts` по шаблону: только отправляемый блок, без служебной части и досье |
| `card_slice` | строка или `null` | Тот же срез карточки, что у судьи: критик читает текст глазами этого человека |
| `output_path` | строка | Абсолютный путь к файлу-стабу `.md` в `<workspace_root>/runs/pending/`. Критик перезаписывает его своей таблицей замечаний |

**Выход критика:** таблица замечаний markdown в `output_path` (формат в теле агента: по строке на
часть текста, колонки «№ · Вердикт · Что мешает · Тон · Комментарий одной фразой», под таблицей
три строки: худшая часть, лучшая часть, чего не хватило). Текстом в ответ не больше десяти строк.

## Проверки после агента

Скрипт после коллектора и после судьи валидирует файл по схеме и проверяет условия, которые
схема не выражает. Список этих условий один, он в
`${CLAUDE_PLUGIN_ROOT}/references/output-contract.md`, раздел «Проверки скриптом»; здесь он не
повторяется. Нарушение → явная ошибка с названием условия, поля и индекса, прогон по участнику
останавливается; тихой починки нет.

## Пример входа (синтетический)

Коллектор, вторая пачка из двух:

```json
{
  "plugin_root": "/home/user/.claude/plugins/retropair",
  "workspace_root": "/home/user/work/shop-api/.retropair",
  "workspace_id": "shop-api",
  "person_id": "dev-a",
  "period": {"label": "sprint-14", "from": "2026-03-02", "to": "2026-03-13", "anchor": null},
  "run_id": "shop-api_sprint-14_20260314T101500",
  "lang": "ru",
  "output_path": "/home/user/work/shop-api/.retropair/runs/pending/dev-a.collector.2.json",
  "log_paths": [
    {"path": "/home/user/work/shop-api/.retropair/logs/dev-a/2026-03-10_s-7f3a.md", "session_id": "s-7f3a", "state": "new", "from_turn": null},
    {"path": "/home/user/work/shop-api/.retropair/logs/dev-a/2026-03-04_s-1c9e.md", "session_id": "s-1c9e", "state": "grown", "from_turn": 38}
  ],
  "batch": {"index": 2, "total": 2},
  "task_summary": "перенос выгрузки отчётов на новую очередь",
  "open_recs": [{"id": "REC-dev-a-003", "text": "перед приёмкой диффа просить агента перечислить изменённые файлы", "period": "sprint-13", "issued_at": "2026-03-01T17:40:00Z", "evaluable": true}],
  "context_report": null,
  "repo_path": "/home/user/work/shop-api",
  "show_scores": false
}
```

Судья получает те же общие поля без `batch`, плюс:

```json
{
  "output_path": "/home/user/work/shop-api/.retropair/runs/pending/dev-a.judge.json",
  "collector_outputs": [
    "/home/user/work/shop-api/.retropair/runs/pending/dev-a.collector.1.json",
    "/home/user/work/shop-api/.retropair/runs/pending/dev-a.collector.2.json"
  ],
  "excluded_sessions": [{"session_id": "s-44b0", "path": "/home/user/work/shop-api/.retropair/logs/dev-a/2026-03-05_s-44b0.md", "status": "transcript_too_short", "status_note": null}],
  "sessions_total": 9,
  "card_slice": "profile: backend developer, senior, 9 лет; стек Python, Postgres; инструменты Claude Code\nfeedback: address ты; length detailed; tone direct; practices true; value_focus finding; show_dynamics true"
}
```

Критик:

```json
{
  "plugin_root": "/home/user/.claude/plugins/retropair",
  "lang": "ru",
  "draft_path": "/home/user/work/shop-api/.retropair/runs/pending/dev-a.draft.md",
  "card_slice": "profile: backend developer, senior, 9 лет; стек Python, Postgres; инструменты Claude Code\nfeedback: address ты; length detailed; tone direct",
  "output_path": "/home/user/work/shop-api/.retropair/runs/pending/dev-a.critic.md"
}
```
