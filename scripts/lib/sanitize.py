"""Санитайзер логов: вырезает секреты и персональные данные до того, как лог попадёт в снапшот.

Детерминированный, офлайн, только stdlib. Два режима:

- `Sanitizer.sanitize_text(text)` — по тексту целиком (markdown, txt, сырой JSONL);
- `Sanitizer.sanitize_obj(obj)` — рекурсивно по строковым значениям JSON-объекта: ключи не
  трогаются, структура остаётся валидным JSON, значения служебных ключей (`STRUCTURAL_KEYS`:
  идентификаторы, время, типы) не санитайзятся вовсе.

Порядок: правила `RULES` до `email` включительно (секреты, строки подключения, почта) → колонка
пароля под строкой заголовка (`password_column`: CSV/TSV, таблица) → имена людей → остальные правила `RULES` → логин ОС (`local_user`) → пользовательские `extra_patterns`.
`email` раньше имён: иначе адрес, в котором есть имя из списка, стал бы `[NAME]@<домен>` с
живым доменом; секреты раньше `email`: пароль в `postgres://user:pass@host` — строка подключения. Оба режима возвращают пару `(результат, hits)`, где
`hits = {правило: число замен}`; замены имён считаются как `names`, пользовательских шаблонов —
как `extra`.

Санитайзер идемпотентен на своих метках: повторный проход по очищенному тексту ничего не меняет.
`fingerprint(sanitizer)` — отпечаток набора правил: collect хранит его рядом со снапшотом и
перечищает снапшот, когда отпечаток меняется (новое имя, шаблон, версия правил).

ALLOWLIST (заглушки из документации, служебные значения вроде `bearer`, ссылки на переменные
окружения) сравнивается со **значением секрета целиком** (`fullmatch`): у правил с группой `val`
— с этой группой, у остальных — со всем совпадением. Слово-заглушка внутри значения
(`"prod-test-2026"`) значение не спасает. После ALLOWLIST у части правил есть проверка `CHECKS`
(в секрете 2FA есть цифра, base32 — от 16 символов с буквой и цифрой 2–7 и т. п.).

UUID (`8-4-4-4-12` hex) не режутся числовыми правилами `phone`, `card`, `passport_by_ru`: в логах
агентов это идентификаторы сессий и сообщений, а не телефоны.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import re
from pathlib import Path
from typing import Iterable

from .workspace import SrError

# Версия правил: поднимается при любом изменении поведения санитайзера, которое меняет вывод
# (порядок правил, новые метки, проверки `CHECKS`). Входит в `fingerprint` вместе с самими шаблонами.
RULES_VERSION = 7

NAME_MARK = "[NAME]"
EXTRA_MARK = "[REDACTED]"
NAMES_KEY = "names"
EXTRA_KEY = "extra"
USER_MARK = "[USER]"
LOCAL_USER_KEY = "local_user"
# Логины, совпадающие с обычными словами логов: их замена испортила бы текст.
GENERIC_LOGINS = frozenset({"user", "users", "admin", "root", "ubuntu", "runner", "home", "dev",
                            "test", "guest", "default", "developer", "vagrant", "docker"})

# Символ логина в домашнем пути: всё, кроме разделителей пути, пробелов, кавычек, скобок и
# запрещённых в именах Windows символов. `[` не входит — маска `[USER]` повторно не меняется.
_LOGIN_CH = r"""[^\\/\s"'<>|:*?()\[\],;`]"""
_LOGIN_WIN = rf"(?:{_LOGIN_CH}+(?: {_LOGIN_CH}+)*(?=[\\/])|{_LOGIN_CH}+)"

_SECRET_WORDS = (r"(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
                 r"private[_-]?key|credential")

# ── пароль и секрет 2FA/TOTP в свободном тексте (ответ агента, таблица, список) ──
# Метка с начала токена (lookbehind — линейное время, как у `code_secret`); префикс через `_.-`
# разрешён (`db_password:`), середина слова — нет (`bypass:`). Сразу после escape `\n`, `\r`, `\t`
# текстом (исходник с f-строкой `"…\nPassword: …"` в выводе grep/cat/Read) — тоже начало токена.
_LABEL_START = r"(?:(?<![\w.\-])|(?<=\\[nrt]))(?:[\w.\-]*[_.\-])?"
_PASS_LABEL = r"(?:парол[ьяеюи]\w{0,3}|password|passwd|passcode|pswd|pass|pwd)(?:_\w*)?"
_TOTP_LABEL = (r"(?:2fa|totp|otp|mfa|authenticator|аутентификатор\w*|ключ\w*[ \t]+аутентификатора"
               r"|secret|секрет\w*)(?:[ \t_\-]{1,2}(?:secret|key|seed|code|ключ|секрет|код)\w*){0,2}")
# Разделитель: `:` (и полноширинный), тире; дефис — только в пробелах (`password-reset` не метка);
# ячейка таблицы `|` — с пробелом хотя бы с одной стороны (`secret|password|api_key` в шаблоне grep
# — не таблица). `=` — забота `env_secret`/`code_secret` (в коде `first_pass = f()` не пароль).
# Вокруг — markdown-выделение `**`/`_` и обратная кавычка; после разделителя пробел — ещё и escape
# `\t` текстом. Второй пробельный отрезок — только после `*`/`_`: два `*` подряд квадратичны.
_SEP_WS = r"(?:[ \t]|\\{1,4}t)*"
_LABEL_SEP = (r"[*_]{0,3}(?:[ \t]*(?:[:：]|[—–])|[ \t]+-(?=[ \t])|[ \t]*\|(?=[ \t])|[ \t]+\|)"
              + _SEP_WS + r"(?:[*_]{1,3}" + _SEP_WS + r")?`?")
# Значение: до пробела, `|`, обратной кавычки, `"` и `\` (граница строки JSON); не с `[` (метка
# санитайзера), не с `/` и `~` (путь, `pwd: /tmp`), не с `*` (шаблон `password: *[a-z]`);
# хвостовая пунктуация предложения не входит. Минимум 4 символа.
_TEXT_VALUE = r"""[^\s|`"\\\[/~*][^\s|`"\\]{2,}[^\s|`"\\,.;:!?)\]*]"""
# Base32 группами по 4 через пробел или дефис (так ключ показывают приложения), от 16 символов.
_B32_GROUPS = r"[A-Z2-7]{4}(?:[ \-][A-Z2-7]{4}){3,}(?:[ \-][A-Z2-7]{1,3})?"

# ── пароль в JSON и во вводе в поле пароля (JS, Playwright, Selenium) ──
# Кавычка строкового литерала: обычная, экранированная любое число раз (`\"`, `\\\"` — JSON внутри
# строки команды) и HTML-сущностью (`&#34;`, `&quot;` — markdown-выгрузки логов). В JS ещё и `` ` ``.
_QUOTE = r"""\\*(?:["']|&#3[49];|&#x2[27];|&quot;|&apos;)"""
_JS_QUOTE = r"""\\*(?:["'`]|&#3[49];|&#x2[27];|&quot;|&apos;)"""
# Символ значения в кавычках: всё, кроме кавычек и перевода строки; `&` — если это не кавычка-сущность;
# обратные косые — если за ними не кавычка и не `\n`/`\r` (escape `\u0026` внутри значения допустим).
_QCHAR = r"""(?:[^"'`\n\\&]|&(?!#3[49];|#x2[27];|quot;|apos;)|\\+(?![\\"'`&nr]))"""
_PW_WORD = r"(?:password|passwd|pwd)"
# Селектор поля пароля: атрибут в `[...]`, `#id`, локатор Playwright `getBy…(…)`, Selenium/DOM по id и name.
_PW_SELECTOR = (r"(?:\[[^\]\n]{0,60}" + _PW_WORD + r"[^\]\n]{0,20}\]"
                r"|(?<![&\w])#[\w\-]{0,40}" + _PW_WORD + r"[\w\-]{0,40}"
                r"|getBy\w{1,20}\([^)\n]{0,60}?" + _PW_WORD + r"[^)\n]{0,40}\)"
                r"|(?:ById|ByName|_by_id|_by_name|By\.(?:ID|NAME))\W{0,8}[\w\-]{0,40}" + _PW_WORD + r"[\w\-]{0,40})")
# Ввод строки в поле: `.value = '…'`, `.fill('…')`, `.type(…)`, `send_keys(…)` и т. п.
_PW_INPUT = (r"(?:\.\s*value\s*=\s*|\.\s*(?:fill|type|sendKeys|send_keys|setValue|pressSequentially|val)"
             r"\s*\(\s*)" + _JS_QUOTE)
# Переменная с полем пароля (с учётом регистра): `pass`, `passwordInput`, `newPassword`, `pwd_field`,
# `PASSWORD`; не `bypass`, `compass`, `passenger`, `passport` (перед меткой — `_`/`$` или camelCase).
_PW_IDENT = (r"(?-i:(?<![\w$])(?:[\w$]*[_$]|[\w$]*[a-z0-9](?=P))?"
             r"(?:[Pp]ass(?:word|wd)?|PASS(?:WORD|WD)?|[Pp]wd|PWD)(?![a-z])[\w$]*)")

# (имя правила, регулярка, подстановка). Группа `val` — значение секрета для ALLOWLIST.
RULES: list[tuple[str, str, str]] = [
    # ── секреты ────────────────────────────────────────────────────────────────
    ("aws_key", r"\b(?:AKIA|ASIA|AROA|ABIA|ACCA)[A-Z0-9]{16}\b", "[AWS_KEY]"),
    ("github_token", r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b", "[GITHUB_TOKEN]"),
    ("github_pat", r"\bgithub_pat_[A-Za-z0-9_]{20,}\b", "[GITHUB_TOKEN]"),
    ("anthropic_key", r"\bsk-ant-[A-Za-z0-9_-]{10,}\b", "[API_KEY]"),
    ("openai_key", r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b", "[API_KEY]"),
    ("stripe_key", r"\bsk_(?:live|test)_[A-Za-z0-9]{10,}\b", "[API_KEY]"),
    ("slack_token", r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b", "[SLACK_TOKEN]"),
    ("google_key", r"\bAIza[A-Za-z0-9_-]{35}\b", "[API_KEY]"),
    ("jwt", r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b", "[JWT]"),
    ("private_key", r"-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z ]*PRIVATE KEY-----",
     "[PRIVATE_KEY]"),
    # Ключ в кавычках — JSON и словари: "password": "…", \"password\":\"…\" в строке команды,
    # 'api_key': '…', cfg["token"] = "…", &#34;secret&#34;:&#34;…&#34;. Заменяется только значение: ключ
    # и кавычки остаются, JSON (и сырой JSONL) остаётся валидным. Значение без кавычек (null, число,
    # os.environ[…]) не трогается.
    ("json_secret", r"(?i)(?P<pfx>(?<![A-Za-z0-9_.\-])[A-Za-z0-9_.\-]*" + _SECRET_WORDS
     + r"|conn[_-]?str)[A-Za-z0-9_]*" + _QUOTE + r"[ \t]*\]?[ \t]*[:=][ \t]*" + _QUOTE + r")(?P<val>"
     + _QCHAR + r"{4,})(?=" + _QUOTE + r")",
     r"\g<pfx>[SECRET_ASSIGNMENT]"),
    # Присваивание в кавычках: DB_PASSWORD = "hunter2", api_key: 'abc', self.token="xyz".
    # Префикс до ключевого слова обязателен: без него правило не видит DB_PASSWORD.
    # Lookbehind: матч начинается только с начала токена, иначе длинный токен без разделителей
    # проверяется с каждой позиции (квадратично: 20 000 символов — 15 с).
    ("code_secret", r"(?i)(?<![A-Za-z0-9_.\-])[A-Za-z0-9_.\-]*" + _SECRET_WORDS + r"|conn[_-]?str)"
     r"""[A-Za-z0-9_]*\s*[:=]\s*\\*["'](?P<val>[^"'\n\\]{4,})\\*["']""",
     "[SECRET_ASSIGNMENT]"),
    # То же без кавычек: DB_PASSWORD=hunter2, export API_TOKEN=abc.
    ("env_secret", r"(?i)(?<![A-Za-z0-9_.\-])[A-Za-z0-9_.\-]*" + _SECRET_WORDS + r")"
     r"""[A-Za-z0-9_]*\s*=\s*(?P<val>[^\s"'\n,;)]{4,})""",
     "[SECRET_ASSIGNMENT]"),
    # Ввод пароля в поле формы (автозаполнение в браузере, тесты UI), ключевого слова рядом со
    # значением нет: (1) селектор пароля и в той же инструкции (до `;`/перевода строки, ≤ 200 символов)
    # ввод строки; (2) селектор строкой-аргументом и значение следующим аргументом —
    # `page.fill('#password', '…')` (кавычки `'`/`"`, в одной строке: перечень селекторов в markdown
    # через `` ` `` — не ввод); (3) переменная поля пароля: `pass.value = '…'`, `setVal(pass, '…')`.
    ("password_field", r"(?i)(?P<pfx>" + _PW_SELECTOR + r"(?:(?:[^;\n\\]|\\(?![nr])){0,200}?" + _PW_INPUT
     + r"|" + _QUOTE + r"?[ \t]*,[ \t]*" + _QUOTE + r")|" + _PW_IDENT + r"\s*" + _PW_INPUT
     + r"|\([ \t]*" + _PW_IDENT + r"[ \t]*,[ \t]*" + _JS_QUOTE + r")(?P<val>" + _QCHAR + r"{1,200})(?="
     + _JS_QUOTE + r")",
     r"\g<pfx>[PASSWORD]"),
    # Строка подключения с паролем: postgres://user:pass@host.
    ("conn_string", r"(?i)\b[a-z][a-z0-9+.\-]*://[^\s:@/]+:(?P<val>[^\s:@/]+)@[^\s/\"']+",
     "[CONNECTION_STRING]"),
    # Пароль после метки: «Пароль: …», `| Password | … |`, `- pwd — …`; после «пароль» и пробела
    # — только значение с буквой и цифрой (`CHECKS`), иначе «пароль будет в письме» стало бы меткой.
    ("password_text", r"(?i)(?P<pfx>" + _LABEL_START + _PASS_LABEL + r"(?!\w)(?:(?P<sp>[ \t]+)"
     r"(?=[^\s\[/~])|" + _LABEL_SEP + r"))(?P<val>" + _TEXT_VALUE + r")",
     r"\g<pfx>[PASSWORD]"),
    # Секрет 2FA/TOTP после метки: «2FA: …», «Authenticator Key: …», «ключ аутентификатора — …»;
    # значение — base32 группами (любой регистр) или один токен; в нём должна быть цифра (`CHECKS`).
    ("totp_secret", r"(?i)(?P<pfx>" + _LABEL_START + _TOTP_LABEL + r"(?!\w)" + _LABEL_SEP
     + r")(?P<val>" + _B32_GROUPS + r"(?![\w\-])|" + _TEXT_VALUE + r")",
     r"\g<pfx>[OTP_SECRET]"),
    # Base32-ключ аутентификатора без метки: целый токен в верхнем регистре от 16 значимых символов
    # (сплошной или группами по 4), с буквой и цифрой 2–7 (`CHECKS`); не кусок base64 или пути.
    ("base32_secret", r"(?<![\w.\-+/=])(?:[A-Z2-7]{16,}={0,6}|" + _B32_GROUPS + r")(?![\w\-+/=])",
     "[OTP_SECRET]"),

    # ── персональные данные ────────────────────────────────────────────────────
    # После escape `\n`/`\r`/`\t` текстом адрес начинается за буквой escape: иначе `\nbob@…` стал бы
    # `\[EMAIL]` — escape и разделитель строк пропали бы.
    ("email", r"(?:(?<=\\[nrt])|(?<!\\)\b|(?<=\\)(?![nrt]))[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b", "[EMAIL]"),
    # Хвостовой lookahead пропускает точку конца предложения, но не десятичное продолжение.
    ("phone", r"(?<![\w.])(?:\+?\d{1,3}[ \-]?)?\(?\d{2,4}\)?[ \-]?\d{3}[ \-]?\d{2}[ \-]?\d{2}"
     r"(?!\w|\.\d)", "[PHONE]"),
    ("passport_by_ru", r"\b\d{4}[ \-]?\d{6}\b", "[DOC_ID]"),
    ("card", r"\b(?:\d{4}[ \-]?){3}\d{4}\b", "[CARD]"),
    ("iban", r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b", "[IBAN]"),

    # ── имя пользователя ОС в путях (логин часто совпадает с фамилией) ─────────
    # Логин — любые символы (кириллица, `@`, `%`…) до разделителя пути, пробела, кавычки или
    # скобки. macOS/Linux: каталог `Users` или `home` в корне плюс логин, с учётом регистра (маршрут
    # URL `/api/users/42` — не домашний каталог); после буквы диска (`C:` и прямые слэши) и после
    # `/mnt/<диск>/` (WSL) путь Windows — его ловит `home_path_win`.
    ("home_path", r"(?<![A-Za-z]:)(?<!/[Mm][Nn][Tt]/[A-Za-z])(?P<pfx>/(?:Users|home)/)" + _LOGIN_CH + "+",
     r"\g<pfx>[USER]"),
    # Windows: `C:\Users\<логин>` сырой и экранированный в JSON-строках (`\\`, `\\\\`), с буквой
    # диска и без (`..\\..\\Users\\<логин>`), смешанные слэши (`C:\Users/<логин>`, `C:/Users\<логин>`),
    # диск с прямыми слэшами и WSL `/mnt/c/users/<логин>` в любом регистре. Логин Windows может содержать пробел
    # (`Ivan Petrov`): слова через пробел берутся, если за ними идёт разделитель пути, иначе — одно слово.
    ("home_path_win", r"(?i)(?P<pfx>\\+Users[\\/]+|/+Users\\+|(?<![a-z0-9])[a-z]:/+Users/+"
     r"|/mnt/[a-z]/+Users/+)" + _LOGIN_WIN, r"\g<pfx>[USER]"),
    # Свёрнутая форма путей в папках сессий агента: -Users-<логин>-…
    # macOS `-Users-<логин>-…` и Linux `-home-<логин>-…`; не внутри слова («my-home-page»).
    ("home_path_munged", r"(?<![A-Za-z0-9])(?P<pfx>-(?:Users|home)-)[A-Za-z0-9_.]+(?=-)",
     r"\g<pfx>[USER]"),

    # ── внутренняя инфраструктура ──────────────────────────────────────────────
    ("internal_host", r"(?i)\bhttps?://[a-z0-9.\-]+\.(?:internal|local|corp|intranet|lan)\b"
     r"[^\s\"'<>]*", "[INTERNAL_URL]"),
    ("private_ip", r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b",
     "[PRIVATE_IP]"),
]

# Значения, похожие на секрет, но им не являющиеся. Сравнение — fullmatch по значению.
ALLOWLIST = re.compile(
    r"(?i)(?:example|test|fake|dummy|sample|placeholder|your[_-]?key|changeme|x{3,}"
    r"|bearer|basic|digest|none|null|true|false|string|number|boolean|required|optional"
    r"|env|secret_name|redacted"
    r"|(?:process\.env|os\.environ|os\.getenv)(?:[.\[(].*)?"
    # Ссылка `$VAR` — только имя в верхнем регистре: `$ecret99` может быть паролем. Метка
    # санитайзера или источника (`[PASSWORD]`, `[REDACTED:api-key]`) — повторный проход её не трогает.
    r"|(?-i:\$[A-Z_][A-Z0-9_]*|\[[A-Z][A-Z0-9_]*(?::[\w.\- ]{1,40})?\])"
    r"|<[^>]{1,40}>|\$\{[^}]{1,40}\}|=?\{\{[^}]{1,60}\}\}|\{[^{}]{1,40}\}|…|\.{3}|\*{3,})"
)


def _check_password(m: re.Match) -> bool:
    """Значение из одних русских букв — слово фразы («а пароль — отдельно»), не пароль. После
    «пароль» и пробела (без `:`) — только значение с буквой и цифрой; англ. метки так не берутся."""
    val = m.group("val")
    if re.fullmatch(r"[А-Яа-яЁё\-]+", val):
        return False
    if m.group("sp") is None:
        return True
    return ("парол" in m.group("pfx").casefold() and any(c.isdigit() for c in val)
            and any(c.isalpha() for c in val))


def _check_totp(m: re.Match) -> bool:
    """В секрете 2FA есть цифра: иначе «2FA: включена», «TOTP: enabled» стали бы метками."""
    return any(c.isdigit() for c in m.group("val"))


def _check_base32(m: re.Match) -> bool:
    """Base32-блок: от 16 значимых символов, есть и буква, и цифра 2–7 (не КАПС-слова и не числа)."""
    compact = re.sub(r"[ \-=]", "", m.group(0))
    return (len(compact) >= 16 and re.search(r"[A-Z]", compact) is not None
            and re.search(r"[2-7]", compact) is not None)


def _check_json(m: re.Match) -> bool:
    """URL без запроса и логина у ключа вроде `token_url` — адрес, а не секрет; значение из четырёх
    и более слов (`"secret_handling": "не выводит ключи в лог"`) — описание, а не секрет."""
    val = m.group("val")
    return re.fullmatch(r"(?i)https?://[^\s?#@]*", val) is None and val.count(" ") < 3


# Доп. проверка совпадения по правилу (после ALLOWLIST): `False` — совпадение остаётся как есть.
# Меняете проверку — поднимите `RULES_VERSION`.
CHECKS = {"password_text": _check_password, "totp_secret": _check_totp,
          "base32_secret": _check_base32, "json_secret": _check_json}

# ── колонка пароля под строкой заголовка: CSV/TSV, markdown- и psql-таблица в выводе инструмента ──
# Метки рядом со значением нет: заголовок `username,password` — строкой выше. Проход после секретов
# и `email`: строки (перевод строки или его escape текстом, в том числе `\\n` сырого JSONL) делятся
# на ячейки разделителем заголовка; в строках ниже с тем же числом ячеек значение колонки пароля
# меняется на `[PASSWORD]`. Блок кончается первой строкой с другим числом ячеек (пустой строкой).
PASSWORD_COLUMN_KEY = "password_column"
PASSWORD_MARK = "[PASSWORD]"
_COL_HINT = re.compile(r"(?i)pass|pwd|парол")
_ROW_SPLIT = re.compile(r"(\r?\n|(?:\\{1,4}r)?\\{1,4}n)")
_CELL_SPLITS = tuple(re.compile(p) for p in (r"(,)", r"(;)", r"(\|)", r"(\t)", r"(\\{1,4}t)"))
_SPACED_SPLITS = frozenset({r"(\|)", r"(\t)", r"(\\{1,4}t)"})  # в имени колонки можно пробел
# Префикс первой ячейки: номер строки Read (`     1→`) или путь и номер строки grep (`a.csv:1:`).
_ROW_PREFIX = r"(?:[ \t]*\d{1,7}→|[^\s:,;|]{1,200}?:\d{1,7}[:\-])?"
_CELL_QUOTE = r"""(?:\\*["'`]|\*{1,2})?"""
_COL_NAME = r"[ \t]*" + _CELL_QUOTE + r"(?P<name>[^\W\d][\w.\- ]{0,39}?)" + _CELL_QUOTE + r"[ \t]*"
_COL_NAME_FIRST = re.compile(_ROW_PREFIX + _COL_NAME)
_COL_NAME_REST = re.compile(_COL_NAME)
_COL_LABEL = re.compile(r"(?i)(?:[\w.\-]*[_.\- ])?(?:парол[ьяеюи]\w{0,3}|password|passwd|passcode|pswd"
                        r"|pass|pwd)(?:[_ \-]\w*)?")
# Значение — ячейка целиком (в кавычках или без): фраза с пробелами — не пароль.
_COL_VALUE = r"""[ \t]*\\*["'`]?)(?P<val>[^\s"'`\\|,;]+)(?P<post>\\*["'`]?[ \t]*)"""
_COL_VALUE_FIRST = re.compile(r"(?P<pre>" + _ROW_PREFIX + _COL_VALUE)
_COL_VALUE_REST = re.compile(r"(?P<pre>" + _COL_VALUE)
# Не пароль: итог теста в колонке `pass`, числа, короче 4 символов.
_COL_SKIP = re.compile(r"(?i)pass(?:ed)?|fail(?:ed)?|ok|yes|no|skip(?:ped)?|n/a|[\d.,%+\-]+")
# Линейка под заголовком: `|---|---|`, `-----+-----`.
_RULER = re.compile(r"[ \t|:+=\-]*-[ \t|:+=\-]*")


def _column_header(row: str) -> tuple[re.Pattern, int, list[int]] | None:
    """Строка — заголовок с колонкой пароля: (разделитель, число ячеек, номера колонок пароля)."""
    for split in _CELL_SPLITS:
        cells = split.split(row)[0::2]
        if len(cells) < 2:
            continue
        cols: list[int] = []
        names = 0
        for k, cell in enumerate(cells):
            if not cell.strip():
                if split.pattern == r"(\|)" and k in (0, len(cells) - 1):
                    continue  # края markdown-таблицы
                break
            m = (_COL_NAME_FIRST if k == 0 else _COL_NAME_REST).fullmatch(cell)
            if m is None:
                break
            name = m.group("name").strip()
            if " " in name and split.pattern not in _SPACED_SPLITS:
                break  # «Enter username, password» — фраза, а не CSV
            names += 1
            if _COL_LABEL.fullmatch(name):
                cols.append(k)
        else:
            if cols and names >= 2:
                return split, len(cells), cols
    return None


def _mask_columns(pieces: list[str], cols: list[int], hits: dict[str, int]) -> str | None:
    changed = False
    for k in cols:
        cell = pieces[2 * k]
        m = (_COL_VALUE_FIRST if k == 0 else _COL_VALUE_REST).fullmatch(cell)
        if m is None:
            continue
        val = m.group("val")
        if len(val) < 4 or ALLOWLIST.fullmatch(val) or _COL_SKIP.fullmatch(val):
            continue
        pieces[2 * k] = m.group("pre") + PASSWORD_MARK + m.group("post")
        _bump(hits, PASSWORD_COLUMN_KEY)
        changed = True
    return "".join(pieces) if changed else None


def _password_columns(text: str, hits: dict[str, int]) -> str:
    if _COL_HINT.search(text) is None:
        return text
    parts = _ROW_SPLIT.split(text)
    rows = parts[0::2]
    changed = False
    i = 0
    while i < len(rows):
        header = _column_header(rows[i]) if _COL_HINT.search(rows[i]) else None
        i += 1
        if header is None:
            continue
        split, n, cols = header
        if i < len(rows) and _RULER.fullmatch(rows[i]):
            i += 1
        while i < len(rows):
            pieces = split.split(rows[i])
            if len(pieces) != 2 * n - 1:
                break
            new = _mask_columns(pieces, cols, hits)
            if new is not None:
                rows[i] = new
                changed = True
            i += 1
    if not changed:
        return text
    parts[0::2] = rows
    return "".join(parts)

UUID_RE = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
                     r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}(?![0-9A-Fa-f])")
UUID_GUARDED = frozenset({"phone", "card", "passport_by_ru"})

# Значения этих ключей (на любом уровне JSON) — идентификаторы, время и типы: не санитайзятся.
STRUCTURAL_KEYS = frozenset({
    "sessionId", "session_id", "uuid", "parentUuid", "timestamp", "requestId", "agentId",
    "agentType", "tool_use_id", "toolUseId", "id", "type", "model", "subtype", "stop_reason",
})

# Граница имени и логина: соседний символ не буква и не цифра. `_` — разделитель, а не часть слова:
# клоны вида `<хэндл>__<репо>` и `x_<логин>_y` в путях логов.
_BOUND_L = r"(?<![^\W_])"
_BOUND_R = r"(?![^\W_])"

_COMPILED = [(rule, re.compile(pattern), repl) for rule, pattern, repl in RULES]
# Секреты и `email` применяются до имён (см. описание файла), остальные правила — после.
_EMAIL_AT = next(i for i, (rule, _, _) in enumerate(RULES) if rule == "email")
FIRST_RULES = frozenset(rule for rule, _, _ in RULES[:_EMAIL_AT + 1])
_COMPILED_FIRST = _COMPILED[:_EMAIL_AT + 1]
_COMPILED_REST = _COMPILED[_EMAIL_AT + 1:]


def _bump(hits: dict[str, int], key: str, n: int = 1) -> None:
    hits[key] = hits.get(key, 0) + n


def _in_uuid(spans: list[tuple[int, int]], start: int, end: int) -> bool:
    return any(s < end and start < e for s, e in spans)


class Sanitizer:
    """Набор правил с именами людей и пользовательскими шаблонами; строится один раз на прогон."""

    def __init__(self, names: Iterable[str] = (), extra_patterns: Iterable[str] = (),
                 local_users: Iterable[str] = ()) -> None:
        clean: list[str] = []
        seen: set[str] = set()
        for name in names:
            if not isinstance(name, str):
                raise SrError(f"sanitize: имя не строка (получено {name!r}, "
                              f"тип {type(name).__name__})", 1)
            name = name.strip()
            if name and name.casefold() not in seen:
                seen.add(name.casefold())
                clean.append(name)
        clean.sort(key=len, reverse=True)  # длинные раньше коротких: «Иван Петров» до «Иван»
        self.names = clean
        self._names_re = (re.compile(_BOUND_L + "(?:" + "|".join(re.escape(n) for n in clean)
                                     + ")" + _BOUND_R, re.IGNORECASE) if clean else None)
        users = sorted({u for u in local_users if isinstance(u, str) and u}, key=len, reverse=True)
        self.local_users = users
        self._users_re = (re.compile(_BOUND_L + "(?:" + "|".join(re.escape(u) for u in users)
                                     + ")" + _BOUND_R, re.IGNORECASE) if users else None)
        self._extra: list[re.Pattern] = []
        self.extra_patterns: list[str] = []
        for pat in extra_patterns:
            try:
                self._extra.append(re.compile(pat))
            except (re.error, TypeError) as e:
                raise SrError(f"sanitize: битый шаблон в extra_patterns ({pat!r}): {e}", 1) from e
            self.extra_patterns.append(pat)

    def sanitize_text(self, text: str) -> tuple[str, dict[str, int]]:
        """Возвращает (очищенный текст, {правило: число замен})."""
        if not isinstance(text, str):
            raise SrError(f"sanitize: ожидался текст, получен {type(text).__name__}", 1)
        hits: dict[str, int] = {}
        text = self._apply(text, hits)
        return text, hits

    def sanitize_obj(self, obj: object) -> tuple[object, dict[str, int]]:
        """Рекурсивно по строковым значениям; возвращает новый объект и hits. Вход не меняется."""
        hits: dict[str, int] = {}
        return self._walk(obj, hits), hits

    # ── внутреннее ────────────────────────────────────────────────────────────

    def _walk(self, obj: object, hits: dict[str, int]) -> object:
        if isinstance(obj, str):
            return self._apply(obj, hits)
        if isinstance(obj, dict):
            return {k: (v if k in STRUCTURAL_KEYS else self._walk(v, hits)) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self._walk(v, hits) for v in obj]
        return obj

    def _apply(self, text: str, hits: dict[str, int]) -> str:
        for rule, pattern, repl in _COMPILED_FIRST:
            text = self._apply_rule(rule, pattern, repl, text, hits)
        text = _password_columns(text, hits)
        if self._names_re is not None:
            text, n = self._names_re.subn(NAME_MARK, text)
            if n:
                _bump(hits, NAMES_KEY, n)
        for rule, pattern, repl in _COMPILED_REST:
            text = self._apply_rule(rule, pattern, repl, text, hits)
        if self._users_re is not None:  # после home_path: сначала целые пути, потом голый логин
            text, n = self._users_re.subn(USER_MARK, text)
            if n:
                _bump(hits, LOCAL_USER_KEY, n)
        for pattern in self._extra:
            text, n = pattern.subn(EXTRA_MARK, text)
            if n:
                _bump(hits, EXTRA_KEY, n)
        return text

    @staticmethod
    def _apply_rule(rule: str, pattern: re.Pattern, repl: str, text: str,
                    hits: dict[str, int]) -> str:
        spans = ([m.span() for m in UUID_RE.finditer(text)] if rule in UUID_GUARDED else [])
        check = CHECKS.get(rule)

        def _sub(m: re.Match) -> str:
            if spans and _in_uuid(spans, m.start(), m.end()):
                return m.group(0)
            value = m.group("val") if "val" in pattern.groupindex else m.group(0)
            if value is not None and ALLOWLIST.fullmatch(value):
                return m.group(0)
            if check is not None and not check(m):
                return m.group(0)
            _bump(hits, rule)
            return m.expand(repl) if "\\" in repl else repl

        return pattern.sub(_sub, text)


def fingerprint(sanitizer: Sanitizer) -> str:
    """sha256 набора правил: `RULES_VERSION`, шаблоны `RULES`/`ALLOWLIST`/`STRUCTURAL_KEYS`, `CHECKS`,
    отсортированные `names`, `extra_patterns`, `local_users`. Порядок в конфиге не влияет."""
    payload = {
        "rules_version": RULES_VERSION,
        "rules": [[rule, pattern, repl] for rule, pattern, repl in RULES],
        "first": sorted(FIRST_RULES),
        "checks": sorted(CHECKS),
        "columns": [_ROW_SPLIT.pattern, [c.pattern for c in _CELL_SPLITS], _COL_NAME_FIRST.pattern,
                    _COL_LABEL.pattern, _COL_VALUE_FIRST.pattern, _COL_SKIP.pattern, _RULER.pattern],
        "allowlist": ALLOWLIST.pattern,
        "structural": sorted(STRUCTURAL_KEYS),
        "names": sorted(n.casefold() for n in sanitizer.names),
        "extra_patterns": sorted(sanitizer.extra_patterns),
        "local_users": sorted(u.casefold() for u in sanitizer.local_users),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def names_from_people(cards: list[dict]) -> list[str]:
    """Имена для санитайзера из карточек людей: `name` целиком и каждое слово — если в них ≥ 3 букв,
    без дублей (без учёта регистра). Карточка без `name` (или с пустым) пропускается. Короткие имена
    («Я» в карточке self, «Ли») не берутся: иначе заменялось бы каждое такое слово в тексте."""
    out: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        if value.casefold() not in seen:
            seen.add(value.casefold())
            out.append(value)

    for i, card in enumerate(cards):
        if not isinstance(card, dict):
            raise SrError(f"sanitize: карточка #{i} не отображение (тип {type(card).__name__})", 1)
        name = card.get("name")
        if name is None:
            continue
        if not isinstance(name, str):
            raise SrError(f"sanitize: name в карточке #{i} не строка (в карточке: {name!r}, "
                          f"тип {type(name).__name__})", 1)
        name = " ".join(name.split())
        if not name:
            continue
        if sum(c.isalpha() for c in name) >= 3:
            add(name)
        for word in name.split(" "):
            if sum(c.isalpha() for c in word) >= 3:
                add(word)
    return out


def local_user_names() -> list[str]:
    """Логин ОС и имя домашней папки — они попадают в логи вне путей (`ls -l`, приглашение шелла).

    Берутся, если в них ≥ 3 символов и это не обычное слово (`GENERIC_LOGINS`)."""
    out: list[str] = []
    candidates = []
    try:
        candidates.append(getpass.getuser())
    except (KeyError, OSError):  # нет записи пользователя (контейнер) — остаётся имя папки
        pass
    candidates.append(Path.home().name)
    for value in candidates:
        if (value and len(value) >= 3 and value.casefold() not in GENERIC_LOGINS
                and value.casefold() not in {o.casefold() for o in out}):
            out.append(value)
    return out


def report(file: str, hits: dict[str, int]) -> dict:
    """Отчёт санитайзера по файлу: `{file, rules_hit{правило: n} без имён, renamed}`."""
    return {
        "file": file,
        "rules_hit": {k: v for k, v in hits.items() if k != NAMES_KEY},
        "renamed": hits.get(NAMES_KEY, 0),
    }
