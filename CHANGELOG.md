# Changelog

Формат — [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/), версии — [SemVer](https://semver.org/lang/ru/).

## [Unreleased]

## [0.1.1] — 2026-09-28

### Fixed

- A01: санитайзер вырезает пароли и секреты 2FA/TOTP в свободном тексте (метки, таблицы, списки, base32).
- A02: `append` записывает `last_run_at` в реестр workspace, `doctor` показывает время последнего прогона.
- A04: в режиме run два прогона за один день получают разные метки; повтор метки — только через `--replace`.

## [0.1.0] — 2026-09-28

- Каркас плагина: CLI `sr.py` (`init`, `people`, `doctor`), схемы, шаблоны.

- Сбор логов: `sr.py collect` (санитайзированные снимки сессий Claude Code, SpecStory, папок и git в `.sessionretro/logs/`, сводка), `sr.py inventory` (перечень сессий со статусами), `sr.py normalize` (разбор одного лога), хук SessionEnd для фонового сбора, скилл `collect`, инструкции по связкам среды и агента, страница `docs/ru/collect.md`.

- Оценка: промпты коллектора (v3), судьи (v13) и критика, референсы осей, антипаттернов, приёмов, контракта текста и датасета формулировок, схемы выхода коллектора (v2) и судьи (v3), имена в `i18n.json`, шаблоны текста на двух языках и болванки проектных агентов.

- Движок оценки и скилл `review`: трекер прогонов v2 (`append`, `validate`, `retract`, `compare`, `open-recs`, `mark-logs`, `verify`), уровень по формуле (`level`), нарезка на пачки (`batches`), проверка выхода коллектора (`check-collector`), текст ревью по шаблону с проверкой запретов (`render-review`), одобрение черновика (`approve`), самопроверка (`self-test`); страницы `docs/ru/how-it-works.md` и `docs/ru/review.md`.

- Отчёты `sr.py report` и скилл `/sessionretro:report`: отчёт ревьюера, групповой (team, course), личные файлы с проверкой на чужие имена, страница всех workspace, обезличивание `--anon`; строка `reports_built_at` в `doctor`; страница `docs/ru/reports.md`.

- Настройка `/sessionretro:setup`: диалог из 11 вопросов и `sr.py setup-apply --answers` / `setup-state`; проектные агенты с моделью и effort (`agents write|check`); предложения авторам плагина без автоотправки (`improve add|list|pack|sent|sync`); новые строки `doctor`; страницы `docs/ru/setup.md`, `customize.md`, `feedback.md`.

- Приватность: санитайзер вычищает логин в домашних путях с любыми символами (кириллица, пробел в логине Windows), в экранированных и смешанных Windows-путях и в WSL; `_` — граница слова для имён и хэндлов, в том числе в `--anon`; заголовок workspace не считается чужим именем в личных отчётах, но `report` предупреждает, если в нём есть слова из карточек; хук SessionEnd запускает сбор только в workspace из реестра `~/.sessionretro/workspaces.json`.

- Демо-workspace `demo/`: обезличенная учебная группа с вымышленными именами — 15 человек, пять периодов, одобренные тексты; команда сборки отчётов в `demo/README.md`.

- Документация: README на английском и русском, `ROADMAP.md`, индекс `docs/README.md`, страницы `docs/ru/privacy.md` и `docs/ru/faq.md`, английские страницы `docs/en/how-it-works.md`, `privacy.md`, `customize.md`.
