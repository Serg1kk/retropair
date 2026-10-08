# VS Code плюс Codex

Механика выгрузки и вычистки прогнана на Claude Code; для Codex SpecStory устроен так же,
и отдельно проверено, что чужие сессии из общего каталога он не подхватывает. То, что
расширение для VS Code пишет в тот же каталог, что и консольный Codex, видно прямо в файлах
сессий: там записана поверхность, с которой сессия запущена.

Файлы сессий Codex RetroPair 0.2 напрямую не читает, поэтому понадобится SpecStory:
он разворачивает их в markdown, а `collect` собирает уже его.

## Куда пишутся ваши логи

В домашний каталог, в `~/.codex/sessions/ГОД/МЕСЯЦ/ДЕНЬ/rollout-*.jsonl`, одним файлом
на сессию, разложенным по датам. Консольный Codex и расширение VS Code пишут туда же.

## Шаг 1. Проверьте до начала работы

```bash
ls -lt ~/.codex/sessions/$(date +%Y/%m/%d)/ | head -5
```

Должны быть файлы за сегодня. Если сегодня ещё не работали с агентом, посмотрите общий список:

```bash
ls -lt ~/.codex/sessions/*/*/*/ | head -5
```

Пусто после реальной работы с агентом — значит, у вас нестандартная сборка,
см. раздел «Если что-то не сходится».

## Шаг 2. Поставьте SpecStory

Один раз.

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

Для Intel замените `arm64` на `x86_64`, для Linux возьмите Linux-архив из того же релиза.
Архив `specstory.tar.gz` после распаковки можно удалить. Под Windows SpecStory нет —
см. [windows.md](windows.md).

## Шаг 3. Отключите сеть инструмента

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

По умолчанию SpecStory проверяет обновления и отправляет анонимную аналитику, а при
входе в аккаунт синхронизирует сессии в облако. Конфиг выключает всё это разом.
В аккаунт не входите, `specstory login` не запускайте.

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
"hook": {"enabled": false, "providers": ["codex"]}
```

## Шаг 5. Соберите и вычистите

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rp.py" collect
```

Команда сама запускает `specstory sync codex` в корне проекта, затем копирует каждую
выгрузку снимком в `.retropair/logs/me/` и по дороге вычищает его встроенным
санитайзером. Разово другого агента можно выгрузить флагом: `collect --provider codex`.

SpecStory берёт только сессии этого проекта: в чужом проекте он не находит ничего
и просит открыть Codex именно здесь. Секреты известных форматов он вырезает сам
(`[REDACTED:…]`), но пропускает почту, телефоны, фамилии, адреса, строки подключения
и пароли, похожие на человеческие слова. И ещё: **имя файла** выгрузки собирается
из первого сообщения сессии до всякой редакции. Санитайзер `collect` дочищает
содержимое, а снимок называет по ключу сессии — кусок промпта из имени в `logs/`
не попадает. Исходные файлы не меняются, `*.orig` плагин не пишет.

Имена собственные регулярным выражением не ловятся. Впишите свои названия и фамилии
в `.retropair/config.json`:

```json
"sanitize": {"names": ["ООО Ромашка", "Иванов"], "extra_patterns": []}
```

## Шаг 6. Посмотрите своими глазами

```bash
grep -rn "REDACTED\|SECRET_ASSIGNMENT\|API_KEY\|EMAIL\|PHONE\|NAME" .retropair/logs | head
```

Затем откройте один снимок и пролистайте. В лог попадает содержимое файлов, которые агент
читал, то есть ваш код.

Самопроверка перед первым использованием: положите в промпт агенту заведомую строку
`TEST_EMAIL = "a@example.com"`, выполните шаг 5 и убедитесь, что в снимке на её месте
стоит `[EMAIL]`.

Честное ограничение Codex: в лог попадает переписка, но не каждый шаг агента — по логу
видно, что вы просили и что агент отвечал, но не все его действия.

## Если логи смотрит другой человек

Ревьюер собирает логи из вашего приватного репозитория. Отдавайте уже вычищенные снимки,
а не сырую выгрузку `.specstory/history`:

```bash
git add .retropair/logs
git commit -m "логи сессий"
git push origin
```

Если после отправки поработаете с агентом ещё, повторите шаги 5, 6 и отправку.

## Если что-то не сходится

В сводке `collect` строка `specstory sync codex` с «no sessions», или сам SpecStory советует
открыть Codex в этом проекте. Значит, в этом конкретном репозитории вы с Codex ещё
не работали: сессии привязываются к рабочей папке.

В сводке «specstory не найден, sync пропущен». SpecStory не в PATH того терминала, где
запускается `collect`: повторите шаг 2 и проверьте `specstory version`.

Проверка из первого шага пуста после реальной работы. Откройте issue в репозитории
плагина и приложите вывод обеих команд.
