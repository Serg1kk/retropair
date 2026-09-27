"""Санитайзер: правила секретов и персональных данных, ALLOWLIST по значению целиком, имена по
целому слову, JSON-режим без поломки структуры, UUID и служебные ключи не режутся, отчёт.

Токены-образцы и домашние пути собираются конкатенацией в рантайме: в исходнике их литералов нет.
"""

from __future__ import annotations

import copy
import json
import re
import unittest

import _util  # noqa: F401 — кладёт scripts/ в sys.path
from _util import SCRIPTS

from lib import cc_jsonl, sanitize
from lib.workspace import SrError

S = sanitize.Sanitizer(names=["Иван Петров", "Иван", "Петров"])
PLAIN = sanitize.Sanitizer()
UUID = "11111111-2222-3333-4444-555555555555"
TS = "2026-09-27T10:15:30.123Z"
HOME_MAC = "/" + "Users/alice/x"
MAIL = "bob" + "@" + "mail-host.org"


class Allowlist(unittest.TestCase):
    def test_allowlist_whole_value(self):
        out, hits = S.sanitize_text('PASSWORD="test-prod-2024"')
        self.assertNotIn("test-prod-2024", out)
        self.assertIn("[SECRET_ASSIGNMENT]", out)
        self.assertEqual(hits, {"code_secret": 1})

    def test_allowlist_keeps_placeholders(self):
        for s in ('PASSWORD="test"', 'TOKEN="${TOKEN}"', 'API_KEY="<your-key>"', 'password = "{password}"',
                  "password=changeme", 'secret: "{{ vault_secret }}"', 'token_type="bearer"'):
            out, hits = S.sanitize_text(s)
            self.assertEqual(out, s)
            self.assertEqual(hits, {})

    def test_word_inside_value_does_not_whitelist(self):
        out, _ = PLAIN.sanitize_text('export DB_PASSWORD="prod-test-2026"')
        self.assertNotIn("prod-test-2026", out)

    def test_key_with_test_inside_is_cut(self):
        key = "sk-" + "proj-" + "abcTESTabc-test-0123456789abcdef"
        out, hits = PLAIN.sanitize_text(f"key {key} end")
        self.assertNotIn(key, out)
        self.assertEqual(hits, {"openai_key": 1})

    def test_conn_string_on_example_domain_is_cut(self):
        out, hits = PLAIN.sanitize_text("postgres://u:pass1234" + "@" + "db.example.com/app")
        self.assertNotIn("pass1234", out)
        self.assertEqual(hits, {"conn_string": 1})

    def test_env_reference_kept(self):
        for s in ("API_TOKEN=process.env.API_TOKEN", 'SECRET=os.environ["SECRET"]'):
            out, hits = PLAIN.sanitize_text(s)
            self.assertEqual(out, s)
            self.assertEqual(hits, {})


class Rules(unittest.TestCase):
    CASES = {
        "aws_key": ("AKIA" + "ABCDEFGHIJKLMNOP", "[AWS_KEY]"),
        "github_token": ("ghp" + "_" + "a" * 36, "[GITHUB_TOKEN]"),
        "github_pat": ("github" + "_pat_" + "B" * 30, "[GITHUB_TOKEN]"),
        "anthropic_key": ("sk-" + "ant-" + "api03-AbCdEf123456", "[API_KEY]"),
        "openai_key": ("sk-" + "proj-" + "Q" * 24, "[API_KEY]"),
        "stripe_key": ("sk_" + "live_" + "Z" * 16, "[API_KEY]"),
        "slack_token": ("xox" + "b-" + "1234567890-abcdef", "[SLACK_TOKEN]"),
        "google_key": ("AI" + "za" + "K" * 35, "[API_KEY]"),
        "jwt": ("ey" + "JhbGciOiJIUzI1.ey" + "JzdWIiOiIxMjM0.SflKxwRJSMeKKF2QT4", "[JWT]"),
        "private_key": ("-----BEGIN RSA " + "PRIVATE KEY-----\nMIIabc\n-----END RSA "
                        + "PRIVATE KEY-----", "[PRIVATE_KEY]"),
        "code_secret": ('db_password = "hunter22"', "[SECRET_ASSIGNMENT]"),
        "env_secret": ("export API_TOKEN=hunter22", "[SECRET_ASSIGNMENT]"),
        "conn_string": ("mysql://root:hunter22@db1/app", "[CONNECTION_STRING]"),
        "email": ("bob.smith" + "@" + "mail-host.org", "[EMAIL]"),
        "phone": ("+375 29 123-45-67", "[PHONE]"),
        "passport_by_ru": ("4510 123456", "[DOC_ID]"),
        "card": ("4111-1111-1111-1111", "[CARD]"),
        "iban": ("DE89370400440532013000", "[IBAN]"),
        "home_path": (HOME_MAC, "[USER]"),
        "home_path_win": ("C:\\Users\\alice\\proj", "[USER]"),
        "home_path_munged": ("-Users-alice-proj", "[USER]"),
        "internal_host": ("http://build.corp/job/1", "[INTERNAL_URL]"),
        "private_ip": ("192.168.1.10", "[PRIVATE_IP]"),
        "password_text": ("Пароль: " + "Qw7" + "zx9Lm", "[PASSWORD]"),
        "totp_secret": ("2FA: " + "k3m" + "q7p2", "[OTP_SECRET]"),
        "base32_secret": ("JBSW" + "Y3DP" + "EHPK" + "3PXP", "[OTP_SECRET]"),
        "json_secret": ('{"password": "' + "hun" + 'ter22"}', "[SECRET_ASSIGNMENT]"),
        "password_field": ("document.querySelector('#password').value = '" + "hun" + "ter22'",
                           "[PASSWORD]"),
    }

    def test_each_rule_fires(self):
        for rule, (sample, marker) in self.CASES.items():
            with self.subTest(rule=rule):
                out, hits = PLAIN.sanitize_text(f"см. {sample} тут")
                self.assertIn(marker, out)
                self.assertIn(rule, hits)

    def test_home_paths_keep_prefix(self):
        self.assertEqual(PLAIN.sanitize_text(HOME_MAC)[0], "/" + "Users/[USER]/x")
        self.assertEqual(PLAIN.sanitize_text("/home/alice/x")[0], "/home/[USER]/x")
        self.assertEqual(PLAIN.sanitize_text("-Users-alice-proj")[0], "-Users-[USER]-proj")

    def test_windows_home_escaped_and_without_drive(self):
        # Windows-путь в сыром виде, в JSON-строке (\\), дважды экранированный (\\\\), без буквы
        # диска и с прямыми слэшами: логин → [USER], префикс как был.
        bs = "\\"
        cases = [
            ("C:" + bs + "Users" + bs + "alice" + bs + "x", "C:" + bs + "Users" + bs + "[USER]" + bs + "x"),
            ("C:" + bs * 2 + "Users" + bs * 2 + "alice" + bs * 2 + "x",
             "C:" + bs * 2 + "Users" + bs * 2 + "[USER]" + bs * 2 + "x"),
            ("C:" + bs * 4 + "Users" + bs * 4 + "alice", "C:" + bs * 4 + "Users" + bs * 4 + "[USER]"),
            (".." + bs * 2 + "Users" + bs * 2 + "alice", ".." + bs * 2 + "Users" + bs * 2 + "[USER]"),
            ("D:/" + "Users/alice", "D:/" + "Users/[USER]"),
        ]
        for raw, want in cases:
            with self.subTest(raw=raw):
                out, hits = PLAIN.sanitize_text(f"cwd {raw} ok")
                self.assertEqual(out, f"cwd {want} ok")
                self.assertEqual(sum(hits.values()), 1, hits)
                self.assertNotIn("alice", out)

    def test_home_login_any_letters_space_mixed_slashes_wsl(self):
        # Логин — любые символы до разделителя: кириллица, пробел внутри (Windows), смешанные
        # слэши, WSL `/mnt/c/users/<логин>` в любом регистре; префикс остаётся как был.
        bs = "\\"
        cases = [
            ("C:" + bs + "Users" + bs + "Иван" + bs + "proj", "C:" + bs + "Users" + bs + "[USER]" + bs + "proj"),
            ("C:" + bs + "Users" + bs + "Ivan Petrov" + bs + "proj",
             "C:" + bs + "Users" + bs + "[USER]" + bs + "proj"),
            ("C:" + bs * 2 + "Users" + bs * 2 + "Иван Петров" + bs * 2 + "p",
             "C:" + bs * 2 + "Users" + bs * 2 + "[USER]" + bs * 2 + "p"),
            ("C:" + bs + "Users" + "/alice/proj", "C:" + bs + "Users" + "/[USER]/proj"),
            ("C:/" + "Users" + bs + "alice" + bs + "proj", "C:/" + "Users" + bs + "[USER]" + bs + "proj"),
            ("c:/" + "users/ivanp/proj", "c:/" + "users/[USER]/proj"),
            ("/mnt/c/" + "users/ivanp/x", "/mnt/c/" + "users/[USER]/x"),
            ("/MNT/D/" + "Users/Ivan Petrov/x", "/MNT/D/" + "Users/[USER]/x"),
            ("/" + "Users/Иван/x", "/" + "Users/[USER]/x"),
            ("/home/иван/x", "/home/[USER]/x"),
        ]
        for raw, want in cases:
            with self.subTest(raw=raw):
                out, hits = PLAIN.sanitize_text(f"cwd {raw} ok")
                self.assertEqual(out, f"cwd {want} ok")
                self.assertEqual(sum(hits.values()), 1, hits)
                self.assertEqual(PLAIN.sanitize_text(out), (out, {}), "повторный проход")

    def test_home_login_without_separator_takes_one_word(self):
        # Логин без следующего разделителя — одно слово, прочий текст строки не съедается.
        bs = "\\"
        out, _ = PLAIN.sanitize_text("cd C:" + bs + "Users" + bs + "alice and go /tmp")
        self.assertEqual(out, "cd C:" + bs + "Users" + bs + "[USER] and go /tmp")
        out, _ = PLAIN.sanitize_text("see (/" + "Users/alice), then")
        self.assertEqual(out, "see (/" + "Users/[USER]), then")

    def test_url_users_route_untouched(self):
        for text in ("GET /api/users/42", "https://x.test/api/users/42/edit", "users/42"):
            with self.subTest(text=text):
                self.assertEqual(PLAIN.sanitize_text(text), (text, {}))

    def test_windows_home_idempotent(self):
        masked = "C:" + "\\" + "Users" + "\\" + "[USER]" + "\\" + "x"
        self.assertEqual(PLAIN.sanitize_text(masked), (masked, {}))

    def test_windows_home_in_json_obj(self):
        raw = json.dumps({"cwd": "C:" + "\\" + "Users" + "\\" + "alice" + "\\" + "proj"})
        out, hits = PLAIN.sanitize_text(raw)
        self.assertNotIn("alice", out)
        self.assertEqual(json.loads(out)["cwd"], "C:" + "\\" + "Users" + "\\" + "[USER]" + "\\" + "proj")
        self.assertEqual(hits, {"home_path_win": 1})

    def test_munged_linux_home(self):
        # Linux-форма папки сессий: -home-<логин>-…; внутри слова («my-home-page») не режется.
        self.assertEqual(PLAIN.sanitize_text("projects/-home-jdoe-proj")[0],
                         "projects/-home-[USER]-proj")
        self.assertEqual(PLAIN.sanitize_text("-home-jdoe-proj")[1], {"home_path_munged": 1})
        self.assertEqual(PLAIN.sanitize_text("my-home-page-v2"), ("my-home-page-v2", {}))

    def test_rules_list_complete(self):
        self.assertEqual({r for r, _, _ in sanitize.RULES}, set(self.CASES))

    def test_clean_text_untouched(self):
        s = "Обычный текст: функция add(a, b) и 3 теста, версия 1.2.3."
        self.assertEqual(PLAIN.sanitize_text(s), (s, {}))


class Uuid(unittest.TestCase):
    def test_uuid_not_phone(self):
        out, hits = PLAIN.sanitize_text(f"id {UUID}")
        self.assertEqual(out, f"id {UUID}")
        self.assertNotIn("[PHONE]", out)
        self.assertEqual(hits, {})

    def test_uuid_not_card(self):
        u = "12345678-1234-1234-1234-123456789012"
        out, hits = PLAIN.sanitize_text(f"файл {u}.jsonl")
        self.assertEqual(out, f"файл {u}.jsonl")
        self.assertEqual(hits, {})

    def test_phone_next_to_uuid_still_cut(self):
        out, hits = PLAIN.sanitize_text(f"{UUID} звони +375 29 123-45-67")
        self.assertIn(UUID, out)
        self.assertEqual(hits, {"phone": 1})


class Names(unittest.TestCase):
    def test_names_whole_word(self):
        out, hits = S.sanitize_text("Иван сказал; Иванов не трогаем; петров тоже имя")
        self.assertIn("Иванов", out)
        self.assertEqual(hits["names"], 2)
        self.assertEqual(out, "[NAME] сказал; Иванов не трогаем; [NAME] тоже имя")

    def test_name_inside_word_on_left_untouched(self):
        self.assertEqual(S.sanitize_text("ДеИван и ИванПетров"), ("ДеИван и ИванПетров", {}))

    def test_underscore_is_word_boundary(self):
        # Клоны вида <хэндл>__<репо> в путях логов: `_` — разделитель, а не часть имени.
        s = sanitize.Sanitizer(names=["alice"])
        out, hits = s.sanitize_text("alice__repo x_alice_y Петров_2 malice")
        self.assertEqual(out, "[NAME]__repo x_[NAME]_y Петров_2 malice")
        self.assertEqual(hits, {"names": 2})
        self.assertEqual(S.sanitize_text("Петров_2")[0], "[NAME]_2")

    def test_full_name_before_parts(self):
        out, hits = S.sanitize_text("пишет Иван Петров.")
        self.assertEqual(out, "пишет [NAME].")
        self.assertEqual(hits, {"names": 1})

    def test_blank_names_ignored(self):
        s = sanitize.Sanitizer(names=["", "  ", "Ли"])
        self.assertEqual(s.sanitize_text("Ли и Лиза"), ("[NAME] и Лиза", {"names": 1}))

    def test_names_from_people(self):
        self.assertEqual(sanitize.names_from_people([{"name": "Анна Ли"}]), ["Анна Ли", "Анна"])

    def test_short_full_name_skipped(self):
        # Карточка self по умолчанию: name "Я" — иначе каждое слово «я» в логе стало бы [NAME].
        self.assertEqual(sanitize.names_from_people([{"name": "Я"}, {"name": "Ли"}]), [])
        out, hits = sanitize.Sanitizer(names=sanitize.names_from_people([{"name": "Я"}])).sanitize_text(
            "я сделал, а ты?")
        self.assertEqual(out, "я сделал, а ты?")

    def test_names_from_people_dedup_and_skip_nameless(self):
        cards = [{"name": "Анна Ли"}, {"id": "default"}, {"name": "анна"}, {"name": "Bob O'Neil"}]
        self.assertEqual(sanitize.names_from_people(cards),
                         ["Анна Ли", "Анна", "Bob O'Neil", "Bob", "O'Neil"])

    def test_names_from_people_bad_type(self):
        with self.assertRaises(SrError) as cm:
            sanitize.names_from_people([{"name": 42}])
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("42", cm.exception.message)


class EmailBeforeNames(unittest.TestCase):
    def test_email_with_name_inside_is_email(self):
        mail = "ivanov" + "@" + "corp.com"
        out, hits = sanitize.Sanitizer(names=["Ivanov"]).sanitize_text(f"пиши на {mail}, Ivanov")
        self.assertEqual(out, "пиши на [EMAIL], [NAME]")
        self.assertEqual(hits, {"email": 1, "names": 1})


# Выдуманные пароль и секрет TOTP: собираются в рантайме, литералов в исходнике нет.
PWD = "Xy7" + "pass" + "9Q"
B32 = "JBSW" + "Y3DP" + "EHPK" + "3PXP"
B32_SPACED = " ".join(B32[i:i + 4] for i in range(0, 16, 4))
B32_DASHED = "-".join(B32[i:i + 4] for i in range(0, 16, 4))
B32_LOWER_SPACED = B32_SPACED.lower()
KEY_HEAD = "a1bc" + "de2f"


class PasswordTotpText(unittest.TestCase):
    """A01: пароль и секрет 2FA/TOTP в свободном тексте ответа агента — фразой, таблицей, списком."""

    def assertClean(self, out: str, *values: str) -> None:
        for v in values:
            self.assertNotIn(v, out)
            self.assertNotIn(v.lower(), out.lower())

    def test_login_password_2fa_phrase(self):
        text = f"Готово. Логин: {MAIL}, пароль: {PWD}, 2FA-секрет: {B32}."
        out, hits = PLAIN.sanitize_text(text)
        self.assertClean(out, PWD, B32, MAIL)
        self.assertEqual(out, "Готово. Логин: [EMAIL], пароль: [PASSWORD], 2FA-секрет: [OTP_SECRET].")
        self.assertEqual(hits, {"email": 1, "password_text": 1, "totp_secret": 1})

    def test_markdown_table(self):
        text = (f"| Поле | Значение |\n|---|---|\n| Логин | {MAIL} |\n| Пароль | {PWD} |\n"
                f"| 2FA | {B32_SPACED} |\n| **Password** | `{PWD}` |\n")
        out, hits = PLAIN.sanitize_text(text)
        self.assertClean(out, PWD, B32_SPACED, "EHPK")
        self.assertIn("| Пароль | [PASSWORD] |", out)
        self.assertIn("| 2FA | [OTP_SECRET] |", out)
        self.assertIn("| **Password** | `[PASSWORD]` |", out)
        self.assertEqual(hits.get("password_text"), 2)
        self.assertEqual(hits.get("totp_secret"), 1)

    def test_markdown_list(self):
        text = (f"- Email: {MAIL}\n- Password: {PWD}\n- Authenticator Key: {B32_LOWER_SPACED}\n"
                f"* **Пароль:** {PWD}\n")
        out, hits = PLAIN.sanitize_text(text)
        self.assertClean(out, PWD, B32_LOWER_SPACED, "ehpk")
        self.assertIn("- Password: [PASSWORD]\n", out)
        self.assertIn("- Authenticator Key: [OTP_SECRET]\n", out)
        self.assertIn("* **Пароль:** [PASSWORD]\n", out)
        self.assertEqual(hits, {"email": 1, "password_text": 2, "totp_secret": 1})

    def test_bug_shape_code_block_in_agent_reply(self):
        # Форма утечки A01: строки учёток через два пробела в блоке кода, ключ обрезан «...».
        text = (f"Нашёл в логах:\n\n```\nUsername: {MAIL}  Password: {PWD}  Instance: acme.io ...\n"
                f"Password: {PWD}  Authenticator Key: {KEY_HEAD}...\n```\n")
        out, hits = PLAIN.sanitize_text(text)
        self.assertClean(out, PWD, KEY_HEAD)
        self.assertIn("Password: [PASSWORD]  Authenticator Key: [OTP_SECRET]...", out)
        self.assertIn("Instance: acme.io", out)
        self.assertEqual(hits, {"email": 1, "password_text": 2, "totp_secret": 1})

    def test_password_labels_and_separators(self):
        for label in ("пароль", "Пароль", "ПАРОЛЬ", "password", "Password", "PASSWORD", "pass",
                      "pwd", "passwd", "db_password", "паролем"):
            for sep in (": ", ":", " — ", " - ", " | ", "： ", "—", " |"):
                text = f"{label}{sep}{PWD}"
                with self.subTest(text=text):
                    out, hits = PLAIN.sanitize_text(text)
                    self.assertEqual(out, f"{label}{sep}[PASSWORD]")
                    self.assertEqual(sum(hits.values()), 1, hits)

    def test_password_after_space_ru(self):
        out, hits = PLAIN.sanitize_text(f"пароль {PWD} от админки")
        self.assertEqual(out, "пароль [PASSWORD] от админки")
        self.assertEqual(hits, {"password_text": 1})

    def test_totp_labels(self):
        for label in ("2FA", "2fa", "TOTP", "OTP", "MFA", "authenticator", "Authenticator Key",
                      "2FA secret", "TOTP seed", "2FA-секрет", "2FA код", "секрет", "Secret",
                      "secret key", "Secret Key", "ключ аутентификатора", "Ключ аутентификатора",
                      "MFA code", "otp_secret"):
            for value in (B32, B32_SPACED, B32_DASHED, B32_LOWER_SPACED, "k3mq7p2x"):
                text = f"{label}: {value}"
                with self.subTest(text=text):
                    out, hits = PLAIN.sanitize_text(text)
                    self.assertEqual(out, f"{label}: [OTP_SECRET]")
                    self.assertEqual(sum(hits.values()), 1, hits)

    def test_base32_without_label(self):
        for value in (B32, B32_SPACED, B32_DASHED, B32 + "ABCDEFGH23", B32 + "====",
                      B32 + "QRST" + "UVWX" + "Y234" + "567A"):
            with self.subTest(value=value):
                out, hits = PLAIN.sanitize_text(f"введи {value} в приложение")
                self.assertEqual(out, "введи [OTP_SECRET] в приложение")
                self.assertEqual(hits, {"base32_secret": 1})

    def test_sanitize_obj_assistant_row(self):
        row = {"type": "assistant", "uuid": UUID, "sessionId": UUID, "timestamp": TS,
               "message": {"id": "msg_01", "role": "assistant", "content": [
                   {"type": "text", "text": f"| Логин | {MAIL} |\n| Пароль | {PWD} |\n| 2FA | {B32_SPACED} |"},
                   {"type": "text", "text": f"- Password: {PWD}\n- TOTP: {B32}"}]}}
        line = json.dumps(row, ensure_ascii=False)
        out, hits = PLAIN.sanitize_obj(json.loads(line))
        dumped = json.dumps(out, ensure_ascii=False)
        self.assertClean(dumped, PWD, B32, B32_SPACED, MAIL)
        self.assertEqual(out["uuid"], UUID)
        self.assertEqual(out["message"]["content"][1]["text"], "- Password: [PASSWORD]\n- TOTP: [OTP_SECRET]")
        self.assertEqual(hits, {"email": 1, "password_text": 2, "totp_secret": 2})
        # тот же ряд сырой строкой JSONL через sanitize_text: JSON остаётся валидным
        raw_out, _ = PLAIN.sanitize_text(line)
        self.assertClean(raw_out, PWD, B32)
        json.loads(raw_out)

    def test_idempotent(self):
        text = f"Пароль: {PWD}\n| 2FA | {B32_SPACED} |\nсекрет {B32}\npassword {PWD}"
        once, _ = PLAIN.sanitize_text(text)
        self.assertEqual(PLAIN.sanitize_text(once), (once, {}))

    def test_negatives_untouched(self):
        hex40 = "0123456789abcdef" * 2 + "01234567"
        cases = [
            "Обычный текст ответа: всё собрано, 3 теста зелёные.",
            "пароль не нужен", "пароль будет в письме", "Пароль: не нужен", "Password reset link sent.",
            "password: ${PASS}", "password: <your-password>", "Пароль: <пароль>", "password: required",
            "PASSWORD=${DB_PASS}", "Пароль: [PASSWORD]",
            f"id {UUID} и {UUID.upper()}", f"commit {hex40}", f"sha {hex40.upper()}",
            "MAX_RETRY_COUNT DEFAULT_TIMEOUT ABCDEFGHIJKLMNOPQRSTUVWX ABCD EFGH IJKL MNOP",
            "2FA: включена", "TOTP: enabled for all users", "OTP: пришёл по SMS",
            "pwd: /tmp/work", "$ pwd\n/tmp/work", "bypass: true", "compass: north",
            "CamelCaseIdentifierABCDEFGH234 и prefix_ABCDEFGH23456789XYZ",
            "otpauth-style", "Секреты не найдены.", "секретов нет",
            # шаблоны grep и фразы из живых логов: не таблица и не значение
            'grep -iE "token|secret|password|api_key|apikey" .env', 'grep -E "password: *[a-z0-9]"',
            "а пароль — отдельно, через менеджер", "Пароль: отдельно",
            "self.first_pass = compute_all(x)", "Пароль: {PWD}", "| 2FA | {B32_SPACED} |",
            "data:image/png;base64,iVBOR+" + B32 + "/xyz", "blob/" + B32 + "=/next",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(PLAIN.sanitize_text(text), (text, {}))


# Выдуманный пароль B01; обратная косая + кавычка — экранированная кавычка JSON внутри строки.
PWV = "Qz4" + "nR8" + "wLp2" + "xT6mK9"
BQ = "\\" + '"'
BBQ = "\\" * 3 + '"'


class JsonPasswordField(unittest.TestCase):
    """B01: пароль в JSON (обычном и экранированном в строке команды) и во вводе в поле пароля."""

    def assertCut(self, text: str, want: str, rule: str, n: int = 1, extra: dict | None = None) -> str:
        out, hits = PLAIN.sanitize_text(text)
        self.assertNotIn(PWV, out)
        self.assertEqual(out, want)
        self.assertEqual(hits, {rule: n, **(extra or {})})
        self.assertEqual(PLAIN.sanitize_text(out), (out, {}), "повторный проход")
        return out

    def test_bug_shape_escaped_json_in_command(self):
        # Случай 1: curl -d '{\"email\":\"…\",\"password\":\"…\"}' в строке команды.
        head = "curl -d '{" + BQ + "email" + BQ + ":" + BQ + "a@example.com" + BQ + "," + BQ + "password" + BQ + ":" + BQ
        want = ("curl -d '{" + BQ + "email" + BQ + ":" + BQ + "[EMAIL]" + BQ + "," + BQ + "password" + BQ + ":"
                + BQ + "[SECRET_ASSIGNMENT]" + BQ + "}'")
        self.assertCut(head + PWV + BQ + "}'", want, "json_secret", extra={"email": 1})

    def test_plain_json_in_command_stays_json(self):
        # Случай 2: обычный JSON — ключ в кавычках перед `:`; ключ и кавычки остаются, JSON валиден.
        text = """curl -d '{"email":"a@example.com","password":\"""" + PWV + """\"}'"""
        out = self.assertCut(text, """curl -d '{"email":"[EMAIL]","password":"[SECRET_ASSIGNMENT]"}'""",
                             "json_secret", extra={"email": 1})
        self.assertEqual(json.loads(out[len("curl -d '"):-1])["password"], "[SECRET_ASSIGNMENT]")

    def test_json_key_variants(self):
        for key in ("password", "passwd", "secret", "token", "api_key", "db_password", "Password", "API_TOKEN"):
            for tpl, want in (('{"K": "V"}', '{"K": "[SECRET_ASSIGNMENT]"}'),
                              ('{"K" : "V"}', '{"K" : "[SECRET_ASSIGNMENT]"}'),
                              ('{"K":"V","x":1}', '{"K":"[SECRET_ASSIGNMENT]","x":1}'),
                              ("{'K': 'V'}", "{'K': '[SECRET_ASSIGNMENT]'}"),
                              ("cfg['K'] = 'V'", "cfg['K'] = '[SECRET_ASSIGNMENT]'"),
                              ("{" + BBQ + "K" + BBQ + ":" + BBQ + "V" + BBQ + "}",
                               "{" + BBQ + "K" + BBQ + ":" + BBQ + "[SECRET_ASSIGNMENT]" + BBQ + "}"),
                              ("{&#34;K&#34;:&#34;V&#34;}", "{&#34;K&#34;:&#34;[SECRET_ASSIGNMENT]&#34;}"),
                              ("{" + "\\" + "&#34;K" + "\\" + "&#34;:" + "\\" + "&#34;V" + "\\" + "&#34;}",
                               "{" + "\\" + "&#34;K" + "\\" + "&#34;:" + "\\" + "&#34;[SECRET_ASSIGNMENT]"
                               + "\\" + "&#34;}")):
                text = tpl.replace("K", key).replace("V", PWV)
                with self.subTest(text=text):
                    self.assertCut(text, want.replace("K", key), "json_secret")

    def test_value_with_backslash_escape(self):
        val = "ab" + "\\" + "u0026" + PWV
        self.assertCut('{"password": "' + val + '"}', '{"password": "[SECRET_ASSIGNMENT]"}', "json_secret")

    def test_jsonl_tool_use_row(self):
        cmd = """curl -s -d '{"email":"a@example.com","password":\"""" + PWV + """\"}' http://x.test/login"""
        row = {"type": "assistant", "uuid": UUID, "timestamp": TS, "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": cmd}}]}}
        line = json.dumps(row)
        out, hits = PLAIN.sanitize_obj(json.loads(line))
        got = out["message"]["content"][0]["input"]["command"]
        self.assertNotIn(PWV, got)
        self.assertIn('"password":"[SECRET_ASSIGNMENT]"', got)
        self.assertEqual(hits, {"json_secret": 1, "email": 1})
        raw_out, raw_hits = PLAIN.sanitize_text(line)  # сырая строка JSONL: `\"password\":\"…\"`
        self.assertNotIn(PWV, raw_out)
        self.assertEqual(json.loads(raw_out)["message"]["content"][0]["input"]["command"], got)
        self.assertEqual(raw_hits, hits)

    def test_js_bug_shapes(self):
        # Случаи 3 и 4: присваивание значения полю, найденному по селектору пароля.
        self.assertCut("document.querySelector('[name=" + BQ + "password" + BQ + "]').value = '" + PWV + "'",
                       "document.querySelector('[name=" + BQ + "password" + BQ + "]').value = '[PASSWORD]'",
                       "password_field")
        self.assertCut('document.querySelector(\'input[type=password]\').value="' + PWV + '"',
                       'document.querySelector(\'input[type=password]\').value="[PASSWORD]"', "password_field")

    def test_js_selectors_and_inputs(self):
        selectors = ("document.querySelector('[name=password]')", "document.querySelector('input[type=password]')",
                     "$('#password')", "document.querySelector('#pwd')", "page.getByLabel('Password')",
                     "page.locator('#user-passwd')", "document.getElementById('password')",
                     "driver.find_element(By.NAME, \"password\")", "page.getByRole('textbox', { name: 'Password' })")
        ops = (".value = 'V'", '.value="V"', ".fill('V')", ".type('V')", ".send_keys('V')",
               ".clear().send_keys(\"V\")", ".fill(`V`)")
        for sel in selectors:
            for op in ops:
                text = sel + op.replace("V", PWV) + ";\nnext()"
                with self.subTest(text=text):
                    self.assertCut(text, sel + op.replace("V", "[PASSWORD]") + ";\nnext()", "password_field")

    def test_js_selector_as_argument(self):
        for sel in ("'#password'", '"input[name=password]"', "'[type=password]'"):
            text = "await page.fill(" + sel + ", '" + PWV + "')"
            with self.subTest(text=text):
                self.assertCut(text, "await page.fill(" + sel + ", '[PASSWORD]')", "password_field")

    def test_js_password_variable(self):
        # Форма из живого лога: селектор в переменной, значение — строкой позже или через helper.
        bind = "const pass = document.querySelector('input[name=" + BQ + "password" + BQ + "]');" + "\\n  "
        cases = [
            (bind + "pass.value = '@V@';", bind + "pass.value = '[PASSWORD]';"),
            (bind + "setVal(pass, '@V@');", bind + "setVal(pass, '[PASSWORD]');"),
            ('passwordInput.fill("@V@")', 'passwordInput.fill("[PASSWORD]")'),
            ('self.password_field.send_keys("@V@")', 'self.password_field.send_keys("[PASSWORD]")'),
            ("newPassword.value = '@V@'", "newPassword.value = '[PASSWORD]'"),
            ("PWD_INPUT.type('@V@')", "PWD_INPUT.type('[PASSWORD]')"),
            ("typeInto(pwdField, '@V@')", "typeInto(pwdField, '[PASSWORD]')"),
        ]
        for raw, want in cases:
            text = raw.replace("@V@", PWV)
            with self.subTest(text=text):
                self.assertCut(text, want, "password_field")

    def test_negatives_untouched(self):
        cases = [
            'payload = {"email": EMAIL, "password": os.environ["N8N_OWNER_PASSWORD"]}',
            "{" + BQ + "password" + BQ + ":" + BQ + "$N8N_OWNER_PASSWORD" + BQ + "}",
            '-d "{\\"password\\":\\"$N8N_OWNER_PASSWORD\\"}"', 'echo "$N8N_OWNER_PASSWORD"',
            '{"password": process.env.ADMIN_PASS}', '{"password": "${ADMIN_PASS}"}', '{"token": "{{ token }}"}',
            '{"password": ""}', '{"password": null}', '{"password": "<your-password>"}', "{'secret': None}",
            '{"password": "[PASSWORD]"}', '{"max_tokens": 4096}', '"password_text": 2,',
            'data["password"] == other', 'os.environ["DB_PASSWORD"]',
            "document.querySelector('#password').focus();",
            "const pass = document.querySelector('input[name=password]');",
            "document.querySelector('#password'); document.querySelector('#email').value = 'alice'",
            "document.querySelector('#email').value = 'alice'",
            "document.querySelector('#password').setAttribute('type', 'text')",
            "pass.value = process.env.ADMIN_PASS", "pass.fill(password)", "pass.value = ''",
            "bypass.value = 'north'", "compass.value = 'north'", "passenger.value = 'Bob'",
            "passport.fill('AB12345')", "[password reset](https://x.test/reset)",
            'grep -iE "token|secret|password|api_key|apikey" .env',
            # формы из живых логов: метка источника, выражение n8n, адрес у ключа *_url
            '{"api_token": "[REDACTED:project-api-key]"}', '{"accessToken": "={{ $env.service_api_token }}"}',
            '{"token_url": "https://auth.example.com/oauth/token"}',
            # формы из живых логов: перечень селекторов в markdown и в коде через перевод строки,
            # заглушки значения, описание под ключом secret_*
            "селекторы (`[name=password]`, `input[type=password]`, `#pwd`) и ввод",
            "selectors = (\"page.getByLabel('Password')\",\n    \"driver.find_element(By.ID, 'x')\")",
            '{"password": "..."}', '{"password": "…"}', '{"password": "' + "*" * 12 + '"}',
            '{"secret_handling": "does not print keys to logs"}',
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(PLAIN.sanitize_text(text), (text, {}))

    def test_lowercase_dollar_and_url_with_query_are_values(self):
        # `$VAR` живёт только в верхнем регистре; URL с запросом или логином — не «просто адрес».
        for val in ("$ecret" + "99x", "$" + PWV, "https://x.test/cb?token=" + PWV, "[" + PWV + "]"):
            text = '{"password": "' + val + '"}'
            with self.subTest(text=text):
                self.assertCut(text, '{"password": "[SECRET_ASSIGNMENT]"}', "json_secret")


class Fingerprint(unittest.TestCase):
    def test_same_config_same_fp_order_insensitive(self):
        a = sanitize.Sanitizer(names=["Acme", "Globex"], extra_patterns=["A-\\d+", "B-\\d+"],
                               local_users=["jdoe", "jd2"])
        b = sanitize.Sanitizer(names=["Globex", "Acme"], extra_patterns=["B-\\d+", "A-\\d+"],
                               local_users=["jd2", "jdoe"])
        self.assertEqual(sanitize.fingerprint(a), sanitize.fingerprint(b))
        self.assertRegex(sanitize.fingerprint(a), r"^[0-9a-f]{64}$")

    def test_fp_changes_with_names_patterns_users_and_version(self):
        base = sanitize.fingerprint(sanitize.Sanitizer(names=["Acme"]))
        variants = [
            sanitize.Sanitizer(names=["Acme", "Globex"]),
            sanitize.Sanitizer(names=["Acme"], extra_patterns=["X-\\d+"]),
            sanitize.Sanitizer(names=["Acme"], local_users=["jdoe"]),
        ]
        for s in variants:
            self.assertNotEqual(sanitize.fingerprint(s), base)
        from unittest import mock
        with mock.patch.object(sanitize, "RULES_VERSION", sanitize.RULES_VERSION + 1):
            self.assertNotEqual(sanitize.fingerprint(sanitize.Sanitizer(names=["Acme"])), base)

    def test_sanitize_is_idempotent_on_marks(self):
        s = sanitize.Sanitizer(names=["Acme"], extra_patterns=["INT-\\d+"], local_users=["jdoe"])
        once, _ = s.sanitize_text(f"Acme {MAIL} {HOME_MAC} INT-7 jdoe -Users-jdoe-p")
        self.assertEqual(s.sanitize_text(once), (once, {}))


class Extra(unittest.TestCase):
    def test_extra_patterns(self):
        s = sanitize.Sanitizer(extra_patterns=[r"ACME-\d+"])
        self.assertEqual(s.sanitize_text("тикет ACME-4411 и ACME-7"),
                         ("тикет [REDACTED] и [REDACTED]", {"extra": 2}))

    def test_bad_regex(self):
        with self.assertRaises(SrError) as cm:
            sanitize.Sanitizer(extra_patterns=["(unclosed"])
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("(unclosed", cm.exception.message)


class JsonMode(unittest.TestCase):
    def test_json_stays_json(self):
        obj = {"message": {"content": f'export DB_PASSWORD="prod-x-99" и {HOME_MAC}'}}
        out, hits = S.sanitize_obj(obj)
        json.dumps(out)
        self.assertNotIn("prod-x-99", out["message"]["content"])
        self.assertNotIn("alice", out["message"]["content"])
        self.assertEqual(hits, {"code_secret": 1, "home_path": 1})

    def test_structural_keys_untouched(self):
        obj = {"type": "user", "uuid": UUID, "parentUuid": UUID, "sessionId": UUID,
               "timestamp": TS, "requestId": "req_4111111111111111",
               "message": {"id": "msg_01", "model": "m", "stop_reason": "end_turn",
                           "content": [{"type": "tool_result", "tool_use_id": "toolu_1",
                                        "content": f"cwd {HOME_MAC}"}]},
               "agentId": "a1", "agentType": "general-purpose", "subtype": "x",
               "toolUseId": "t", "session_id": UUID}
        out, hits = PLAIN.sanitize_obj(obj)
        for k in ("type", "uuid", "parentUuid", "sessionId", "timestamp", "requestId",
                  "agentId", "agentType", "subtype", "toolUseId", "session_id"):
            self.assertEqual(out[k], obj[k], k)
        self.assertEqual(out["message"]["id"], "msg_01")
        self.assertEqual(out["message"]["content"][0]["tool_use_id"], "toolu_1")
        self.assertEqual(out["message"]["content"][0]["content"], "cwd /" + "Users/[USER]/x")
        self.assertEqual(hits, {"home_path": 1})

    def test_structural_key_value_skipped_even_if_secretlike(self):
        out, hits = PLAIN.sanitize_obj({"agentType": MAIL, "cwd": HOME_MAC})
        self.assertEqual(out["agentType"], MAIL)
        self.assertEqual(out["cwd"], "/" + "Users/[USER]/x")
        self.assertEqual(hits, {"home_path": 1})

    def test_keys_not_touched_and_input_not_mutated(self):
        obj = {HOME_MAC: ["a", {"k": MAIL}, 3, None, True, 1.5]}
        before = copy.deepcopy(obj)
        out, hits = PLAIN.sanitize_obj(obj)
        self.assertEqual(obj, before)
        self.assertEqual(list(out), [HOME_MAC])
        self.assertEqual(out[HOME_MAC], ["a", {"k": "[EMAIL]"}, 3, None, True, 1.5])
        self.assertEqual(hits, {"email": 1})

    def test_names_in_obj(self):
        out, hits = S.sanitize_obj({"a": "Иван", "b": ["Петров здесь"]})
        self.assertEqual(out, {"a": "[NAME]", "b": ["[NAME] здесь"]})
        self.assertEqual(hits, {"names": 2})

    def test_compact_snapshot_accepts_sanitize_obj(self):
        main = _util.FIXTURES / "logs" / "claude-main.jsonl"
        text, hits, _ = cc_jsonl.compact_snapshot(main, PLAIN.sanitize_obj)
        rows = [json.loads(line) for line in text.splitlines()]
        self.assertTrue(rows)
        self.assertTrue(all(isinstance(v, int) for v in hits.values()))
        raw_uuids = set()
        for line in main.read_text(encoding="utf-8").splitlines():
            try:
                raw_uuids.add(json.loads(line).get("uuid"))
            except (ValueError, AttributeError):
                continue  # в фикстуре нарочно есть битые строки
        self.assertTrue({r["uuid"] for r in rows if r.get("isSidechain") is not True} <= raw_uuids)


class Report(unittest.TestCase):
    def test_report_shape(self):
        rep = sanitize.report("a.jsonl", {"names": 3, "email": 2, "phone": 1})
        self.assertEqual(rep, {"file": "a.jsonl", "rules_hit": {"email": 2, "phone": 1},
                               "renamed": 3})

    def test_report_without_names(self):
        self.assertEqual(sanitize.report("b.md", {}), {"file": "b.md", "rules_hit": {}, "renamed": 0})


class LocalUser(unittest.TestCase):
    def test_login_replaced_whole_word(self):
        s = sanitize.Sanitizer(local_users=["jdoe"])
        out, hits = s.sanitize_text("drwxr-xr-x  3 jdoe  staff  96 x; JDOE; ajdoe; jdoex")
        self.assertEqual(out, "drwxr-xr-x  3 [USER]  staff  96 x; [USER]; ajdoe; jdoex")
        self.assertEqual(hits["local_user"], 2)

    def test_login_underscore_is_boundary(self):
        s = sanitize.Sanitizer(local_users=["jdoe"])
        self.assertEqual(s.sanitize_text("jdoe__repo x_jdoe"), ("[USER]__repo x_[USER]", {"local_user": 2}))

    def test_local_user_names_filters_generic_and_short(self):
        from unittest import mock
        with mock.patch("getpass.getuser", return_value="root"), \
                mock.patch("pathlib.Path.home", return_value=sanitize.Path("/home/jdoe")):
            self.assertEqual(sanitize.local_user_names(), ["jdoe"])
        with mock.patch("getpass.getuser", return_value="al"), \
                mock.patch("pathlib.Path.home", return_value=sanitize.Path("/home/user")):
            self.assertEqual(sanitize.local_user_names(), [])

    def test_structural_keys_untouched(self):
        out, _ = sanitize.Sanitizer(local_users=["jdoe"]).sanitize_obj({"agentId": "jdoe", "text": "jdoe"})
        self.assertEqual(out, {"agentId": "jdoe", "text": "[USER]"})


class Performance(unittest.TestCase):
    """Длинные токены без разделителей (base64, хэши, минифицированный код) — линейно, не квадратично."""

    def test_long_tokens_linear(self):
        import time

        s = sanitize.Sanitizer()
        for text in ("a" * 20000, "a-" * 10000, "a." * 10000, "1" * 20000, "ab_c.d-" * 3000):
            started = time.perf_counter()
            s.sanitize_text(text)
            self.assertLess(time.perf_counter() - started, 1.0, text[:12])

    def test_long_tokens_linear_password_totp_base32(self):
        # Новые правила A01: метки подряд, длинные base32-цепочки, группы через пробел и дефис.
        import time

        s = sanitize.Sanitizer()
        for text in ("A" * 20000, "A2" * 10000, "ABCD " * 4000, "ABCD-" * 4000, "AB2D " * 4000 + "x",
                     "A" * 20000 + "a", "пароль " * 3000, "password:" * 2000, "pass " * 4000,
                     "2FA " * 5000, "2FA: " * 4000, "db_" * 6000 + "password", "a_" * 8000 + "x",
                     "Пароль: " + "x" * 20000, "секрет: " + "abcd " * 4000, "| Пароль | " * 2000):
            started = time.perf_counter()
            s.sanitize_text(text)
            self.assertLess(time.perf_counter() - started, 1.0, text[:12])

    def test_long_tokens_linear_json_password_field(self):
        # Правила B01: ключи и кавычки подряд, незакрытое значение, селекторы и переменные пароля подряд.
        import time

        s = sanitize.Sanitizer()
        bq = "\\" + '"'
        for text in ('"password":"' * 3000, '"token": "' + "a" * 20000, "password" + bq + ":" + bq * 5000,
                     "{&#34;password&#34;:&#34;" + "a&" * 10000, '"password":"' + "\\u" * 10000,
                     "\\" * 20000, "#password" * 3000, "[name=password]" * 2000, "#password " + "a" * 20000,
                     "getByLabel('Password'" * 2000, "[" * 20000, "pass.value = '" * 3000,
                     "setVal(pass, '" * 3000, "(pass, " * 4000, "userPass" * 3000, "a_" * 8000 + "Password",
                     "document.querySelector('#password')" + " x" * 10000, "#password" + "\\" * 20000,
                     "#password" + "\\x" * 10000, "pass.value = '" + "\\" * 20000, '"password":"' + "a\\" * 10000,
                     "ById" * 5000, "getBy" * 5000, "#" + "a" * 20000 + "pwd", "[password" * 5000,
                     "$('#password').value = '" + "a" * 20000):
            started = time.perf_counter()
            s.sanitize_text(text)
            self.assertLess(time.perf_counter() - started, 1.0, text[:16])

    def test_prefixed_secret_still_caught(self):
        s = sanitize.Sanitizer()
        for text in ('self.db_password = "hunter22"', "export MY_API_TOKEN=abcd1234", 'x.y-secret: "zzzz9999"'):
            out, hits = s.sanitize_text(text)
            self.assertIn("[SECRET_ASSIGNMENT]", out, text)


class Cleanliness(unittest.TestCase):
    def test_no_private_words_in_module(self):
        text = (SCRIPTS / "lib" / "sanitize.py").read_text(encoding="utf-8")
        for word in ("студ" + "ент", "кур" + "с", "мод" + "ул", "опер" + "атор"):
            self.assertIsNone(re.search(r"(?<!\w)" + word, text, re.IGNORECASE), word)
        self.assertNotIn("/" + "Users/", text)


if __name__ == "__main__":
    unittest.main()
