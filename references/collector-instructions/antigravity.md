# Antigravity

Сначала главное: **автоматическая выгрузка работает только для консольной версии
Antigravity (`agy` в терминале)**. Если вы работаете в приложении Antigravity IDE,
SpecStory ваших сессий не увидит — это проверено на живых данных IDE: он честно ищет
каталог CLI-версии и не находит его. Для IDE идите сразу в раздел «Ручной путь» внизу.

Дальше — путь для CLI-версии. Целиком на Antigravity он не прогонялся: механизм —
выгрузка, редакция и вычистка — прогнан на другом агенте, а режим `antigravity` заявлен
вендором SpecStory. Если что-то не сойдётся, откройте issue в репозитории плагина.

## Шаг 1. Поставьте SpecStory

```bash
brew tap specstoryai/tap
brew install specstory
specstory version
```

Если `brew install` ругается на Command Line Tools, возьмите готовый бинарь:

```bash
curl -fsSL -o specstory.tar.gz \
  https://github.com/specstoryai/getspecstory/releases/download/v2.6.0/SpecStoryCLI_Darwin_arm64.tar.gz
tar -xzf specstory.tar.gz && chmod +x specstory
mkdir -p ~/.local/bin && mv specstory ~/.local/bin/
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
export PATH="$HOME/.local/bin:$PATH"
specstory version
```

Последние строки кладут бинарь в постоянное место и добавляют его в PATH — после
этого `specstory` доступна из любого терминала, как при установке через brew.

Для Intel замените `arm64` на `x86_64`, для Linux возьмите `SpecStoryCLI_Linux_arm64.tar.gz`
или `SpecStoryCLI_Linux_x86_64.tar.gz` из того же релиза. Архив `specstory.tar.gz`
после распаковки можно удалить. Под Windows SpecStory нет — см. [windows.md](windows.md).

## Шаг 2. Отключите сеть инструмента

Тот же конфиг лежит рядом: [specstory-config.toml](specstory-config.toml).

```bash
mkdir -p ~/.specstory/cli
cat > ~/.specstory/cli/config.toml <<'EOF'
# SpecStory без сети. В аккаунт не входить, specstory login не запускать.

[cloud_sync]
enabled = false

[analytics]
enabled = false

[version_check]
enabled = false

[telemetry]
prompts = false

[redaction]
enabled = true
EOF
```

По умолчанию SpecStory при каждом запуске проверяет обновления и отправляет анонимную
аналитику, а при входе в аккаунт синхронизирует сессии в своё облако. Конфиг выключает
всё это разом. В аккаунт не входите и `specstory login` не запускайте.

## Шаг 3. Проверьте до начала работы

```bash
cd путь/к/проекту
specstory sync antigravity
ls -lt .specstory/history/ | head -5
```

Должны появиться markdown-файлы с датами. Если пусто, поработайте с агентом пять минут
и повторите. Если и тогда пусто, откройте issue в репозитории плагина и приложите вывод
команды `specstory sync antigravity` целиком.

## Шаг 4. Подключите источник

Один раз в корне проекта создайте workspace, если его ещё нет:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rp.py" init --mode self
```

В `.retropair/config.json` добавьте источник SpecStory и укажите, какого агента
выгружать:

```json
"sources": [
  {"type": "specstory", "path": ".specstory/history"}
],
"hook": {"enabled": false, "providers": ["antigravity"]}
```

## Шаг 5. Соберите и вычистите

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rp.py" collect
```

Команда сама запускает `specstory sync antigravity` в корне проекта, затем копирует каждую
выгрузку снимком в `.retropair/logs/me/` и по дороге вычищает его встроенным санитайзером.

Встроенная редакция SpecStory (`[REDACTED:…]`) ловит форматы секретов, но пропускает почту,
телефоны, фамилии, адреса, строки подключения к базе и пароли, похожие на человеческие
слова. И ещё: **имя файла** выгрузки собирается из первого сообщения сессии до всякой
редакции. Санитайзер `collect` дочищает содержимое, а снимок называет по ключу сессии —
кусок промпта из имени в `logs/` не попадает. Исходные файлы не меняются, `*.orig` плагин
не пишет.

Имена собственные регулярным выражением не ловятся. Впишите свои названия и фамилии
в `.retropair/config.json`:

```json
"sanitize": {"names": ["ООО Ромашка", "Иванов"], "extra_patterns": []}
```

Метрик заполнения контекстного окна по сессиям Antigravity нет: их даёт только источник
`claude-jsonl`.

## Шаг 6. Посмотрите своими глазами

```bash
grep -rn "REDACTED\|SECRET_ASSIGNMENT\|API_KEY\|EMAIL\|PHONE\|NAME" .retropair/logs | head
```

Затем откройте один снимок целиком и пролистайте: автоматика ловит форматы, а название
клиента в свободном тексте увидите только вы. В выгрузку попадает содержимое файлов,
которые агент читал, то есть ваш код.

Самопроверка перед первым использованием: положите в промпт агенту заведомую строку
`TEST_EMAIL = "a@example.com"`, выполните шаг 5 и убедитесь, что в снимке стоит `[EMAIL]`.

## Если логи смотрит другой человек

Ревьюер собирает логи из вашего приватного репозитория. Отдавайте уже вычищенные снимки,
а не сырую выгрузку:

```bash
git add .retropair/logs
git commit -m "логи сессий"
git push origin
```

Если после отправки поработаете с агентом ещё, повторите шаги 5, 6 и отправку.

## Ручной путь: Antigravity IDE и всё, что не сработало

Для приложения Antigravity IDE это основной путь: выгрузите переписку с агентом
из интерфейса вручную и сохраните в файл `.md` в папке `ai-logs/` проекта, одна сессия —
один файл, например `ai-logs/session-2026-01-15.md`. Роли размечайте заголовками,
а дату — строкой `Date:`:

```markdown
Date: 2026-01-15

## User
текст запроса

## Assistant
ответ агента
```

Подключите папку источником и соберите как обычно (шаги 5 и 6):

```json
"sources": [
  {"type": "dir", "path": "ai-logs"}
]
```

Санитайзер `collect` вычистит снимки, сами файлы в `ai-logs/` останутся как есть —
в общий репозиторий их не кладите. Это не хуже автоматической выгрузки, просто дольше.
