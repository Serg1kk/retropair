"""Каркас плагина: манифесты, версии, скиллы, агенты, хук, i18n, общие файлы документации."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

import _util
from _util import PLUGIN

from lib.version import __version__

SKILLS = {"setup", "collect", "review", "report"}
AGENTS = {
    "log-collector": ("sonnet", "medium"),
    "log-judge": ("opus", "high"),
    "feedback-critic": ("sonnet", "medium"),
}
# версия берётся из lib/version.py: тесты не держат литерал и переживают бамп релиза
VERSION = __version__
I18N_SECTIONS = [
    "axes", "levels", "verdicts", "rec_verdicts", "rec_statuses",
    "session_statuses", "insufficient_reasons", "antipatterns", "flags", "gates",
    "review_statuses", "report", "cli",
]
CLI_SUBSECTIONS = ["assess", "setup"]
AXES = [
    "verification", "task_framing", "ownership", "self_correction",
    "context_engineering", "decomposition", "trust_calibration", "reversibility",
]
SHARED_DOCS = ["README.md", "README.ru.md", "CHANGELOG.md", "TESTING.md"]


def read_json(rel: str) -> dict:
    return json.loads((PLUGIN / rel).read_text(encoding="utf-8"))


def frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise AssertionError(f"frontmatter не начинается с '---' ({path})")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise AssertionError(f"frontmatter не закрыт ({path})")
    fields: dict[str, str] = {}
    for line in text[4:end].splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    return fields


class ManifestTest(unittest.TestCase):
    def test_plugin_json_fields(self):
        data = read_json(".claude-plugin/plugin.json")
        self.assertEqual(data["name"], "sessionretro")
        self.assertEqual(data["version"], VERSION)
        self.assertEqual(data["license"], "MIT")
        self.assertEqual(data["author"], {"name": "ProdfeatAI"})
        self.assertEqual(data["repository"], "https://github.com/Serg1kk/sessionretro")

    def test_manifest_format_is_indent2_with_newline(self):
        for rel in (".claude-plugin/plugin.json", ".claude-plugin/marketplace.json"):
            raw = (PLUGIN / rel).read_text(encoding="utf-8")
            expected = json.dumps(json.loads(raw), indent=2, ensure_ascii=False) + "\n"
            self.assertEqual(raw, expected, rel)
            self.assertIn(f'"version": "{VERSION}"', raw, rel)

    def test_marketplace_points_to_self(self):
        data = read_json(".claude-plugin/marketplace.json")
        self.assertEqual(data["name"], "sessionretro")
        self.assertEqual(len(data["plugins"]), 1)
        self.assertEqual(data["plugins"][0]["name"], "sessionretro")
        self.assertEqual(data["plugins"][0]["source"], "./")
        plugin_desc = read_json(".claude-plugin/plugin.json")["description"]
        self.assertEqual(data["metadata"]["description"], plugin_desc)
        self.assertEqual(data["plugins"][0]["description"], plugin_desc)

    def test_versions_match_in_four_places(self):
        from lib import version

        plugin = read_json(".claude-plugin/plugin.json")
        market = read_json(".claude-plugin/marketplace.json")
        found = [
            plugin["version"],
            market["metadata"]["version"],
            market["plugins"][0]["version"],
            version.__version__,
        ]
        self.assertEqual(found, [VERSION] * 4)

    def test_plugin_root_helper(self):
        from lib import version

        self.assertEqual(version.plugin_root(), PLUGIN)

    def test_four_skills_three_agents(self):
        skill_dirs = {p.parent.name for p in (PLUGIN / "skills").glob("*/SKILL.md")}
        self.assertEqual(skill_dirs, SKILLS)
        for name in SKILLS:
            fm = frontmatter(PLUGIN / "skills" / name / "SKILL.md")
            self.assertEqual(fm.get("name"), name)
            self.assertTrue(fm.get("description"), name)
            self.assertEqual(set(fm), {"name", "description"}, name)
        agent_files = {p.stem for p in (PLUGIN / "agents").glob("*.md")}
        self.assertEqual(agent_files, set(AGENTS))
        for name in AGENTS:
            fm = frontmatter(PLUGIN / "agents" / f"{name}.md")
            self.assertEqual(fm.get("name"), name)
            self.assertTrue(fm.get("description"), name)

    def test_skill_description_shape(self):
        for name in SKILLS:
            desc = frontmatter(PLUGIN / "skills" / name / "SKILL.md")["description"]
            self.assertTrue(desc.startswith('"') and desc.endswith('"'), name)
            self.assertIn("Use when", desc, name)
            self.assertIn("Триггеры:", desc, name)

    def test_agents_have_model_effort(self):
        for name, (model, effort) in AGENTS.items():
            fm = frontmatter(PLUGIN / "agents" / f"{name}.md")
            self.assertEqual(fm.get("model"), model, name)
            self.assertEqual(fm.get("effort"), effort, name)

    def test_hooks_json_session_end(self):
        data = read_json("hooks/hooks.json")
        entries = data["hooks"]["SessionEnd"]
        self.assertEqual(len(entries), 1)
        hooks = entries[0]["hooks"]
        self.assertEqual(len(hooks), 1)
        hook = hooks[0]
        self.assertEqual(hook["type"], "command")
        self.assertIn("${CLAUDE_PLUGIN_ROOT}/scripts/hook_session_end.py", hook["command"])
        self.assertEqual(hook["timeout"], 10)

    def test_i18n_sections(self):
        data = read_json("references/i18n.json")
        self.assertEqual(list(data), I18N_SECTIONS)

        def check_section(path: str, section: dict) -> None:
            keys = list(section)
            self.assertTrue(keys, path)
            for key in keys:
                value = section[key]
                self.assertIsInstance(value, dict, f"{path}.{key}")
                self.assertTrue(value.get("ru"), f"{path}.{key}.ru")
                self.assertTrue(value.get("en"), f"{path}.{key}.en")

        for name in I18N_SECTIONS:
            if name == "cli":
                self.assertEqual(list(data["cli"]), CLI_SUBSECTIONS)
                for sub in CLI_SUBSECTIONS:
                    check_section(f"cli.{sub}", data["cli"][sub])
            else:
                check_section(name, data[name])
        self.assertEqual(list(data["axes"]), AXES)

    def test_i18n_has_no_service_keys(self):
        found = []

        def walk(path: str, node) -> None:
            if not isinstance(node, dict):
                return
            for key, value in node.items():
                if key.startswith("_"):
                    found.append(f"{path}.{key}".lstrip("."))
                walk(f"{path}.{key}", value)

        walk("", read_json("references/i18n.json"))
        self.assertEqual(found, [])

    def test_i18n_one_entry_per_line(self):
        lines = (PLUGIN / "references/i18n.json").read_text(encoding="utf-8").splitlines()
        for line in lines:
            if '"ru"' in line:
                self.assertRegex(line, r'^\s+"[a-z0-9_]+": \{"ru": ".+", "en": ".+"\},?$', line)

    def test_shared_docs_have_no_block_markers(self):
        for rel in SHARED_DOCS:
            text = (PLUGIN / rel).read_text(encoding="utf-8")
            with self.subTest(file=rel):
                self.assertNotIn("<!-- feature-", text)
                self.assertNotIn("<!-- /feature-", text)
                self.assertNotIn("\n\n\n", text)
                self.assertTrue(text.endswith("\n") and not text.endswith("\n\n"), rel)

    def test_changelog_unreleased_is_first_section(self):
        text = (PLUGIN / "CHANGELOG.md").read_text(encoding="utf-8")
        sections = [line for line in text.splitlines() if line.startswith("## ")]
        self.assertTrue(sections)
        self.assertEqual(sections[0], "## [Unreleased]")

    def test_license_mit(self):
        text = (PLUGIN / "LICENSE").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("MIT License"))
        self.assertIn("Copyright (c) 2026 ProdfeatAI", text)

    def test_issue_templates(self):
        for name in ("proposal", "bug", "privacy"):
            fm = frontmatter(PLUGIN / ".github/ISSUE_TEMPLATE" / f"{name}.md")
            self.assertEqual({"name", "about", "labels"} - set(fm), set(), name)

class TempHomeTest(_util.TempHome, unittest.TestCase):
    def test_home_is_temporary(self):
        self.assertEqual(Path(os.environ["HOME"]), self.home)
        self.assertTrue(self.home.is_dir())
        self.assertEqual(Path.home(), self.home)


if __name__ == "__main__":
    unittest.main()
