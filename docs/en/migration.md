# Moving from SessionRetro to RetroPair

RetroPair 0.2.0 continues the existing product history. The four skills are now
`/retropair:setup`, `/retropair:collect`, `/retropair:review` and `/retropair:report`.
The preferred CLI entrypoint is `scripts/rp.py`; `scripts/sr.py` remains supported.
Persisted assessment fields, recommendation IDs and period IDs keep their contracts.

## Switch the installed plugin

Finish any running review or collection first. Disable collection in the old workspace
(`hook.enabled: false` in `.sessionretro/config.json`) and stop the old Claude Code session.
Keep a backup of the workspace and customized project agents before migrating.
Then replace the old marketplace/plugin registration in Claude Code:

```text
/plugin uninstall sessionretro@sessionretro
/plugin marketplace remove sessionretro
/plugin marketplace add Serg1kk/retropair
/plugin install retropair@retropair
/reload-plugins
```

The distribution repository remains private; your GitHub account needs access. These are
manual migration instructions, not a claim that an existing-install upgrade has been tested
in your Claude Code runtime. Removing the plugin does not remove your workspace data.
If you also registered a manual SessionEnd hook in Claude settings, remove that old
registration before enabling the new one. Keep one active RetroPair collection hook.

## Preview and migrate state

Use the installed plugin root in these commands and replace `/path/to/project` with the
project root, not a raw log directory. Preview is the default and does not move files:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rp.py" --workspace /path/to/project migrate
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rp.py" --workspace /path/to/project migrate --apply
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rp.py" --workspace /path/to/project migrate --global
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rp.py" --workspace /path/to/project migrate --global --apply
```

Project migration moves `.sessionretro/` to `.retropair/`; global migration moves
`~/.sessionretro/` to `~/.retropair/`. Existing state files keep their contents: profiles,
assessments, historical scores, review approvals and retractions, reviewer notes,
recommendations and verdicts, inventories, sanitized snapshots, custom templates,
language, model/effort preferences and hook settings. Migration does not rescore anything,
append assessment runs or rewrite original coding-agent logs.

When both old and new directories exist, migration refuses to overwrite or merge them.
Keep both intact, compare their contents and resolve the conflict before retrying. A completed
migration can be run again without duplicating history. Do not point collection at both copies.
The directory move does not sanitize manually added files; inspect your workspace before
sharing it. Raw source logs stay at their original locations and are not copied by migration.

After migration run `/retropair:setup` to check registration, agents, hook settings and
`.gitignore`, then run `doctor` and rebuild reports:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rp.py" --workspace /path/to/project doctor
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rp.py" --workspace /path/to/project report
```

Check that previous periods, approvals and recommendations appear before collecting a new
period. Project `migrate --apply` preserves the old `.gitignore` rules and adds the new
standard protections, equivalents of custom legacy rules, and rules for its audit/lock
files before moving state. The audit `.retropair-migration.json` records file names, sizes
and hashes, not file contents, with owner-only permissions (`0600`). Preview does not edit
`.gitignore`. Keep legacy protections while an old workspace or backup exists.

## Customized project agents

New workspaces use `.claude/agents/retropair-log-collector.md`,
`retropair-log-judge.md` and `retropair-feedback-critic.md`. Keep old customized
`sessionretro-*.md` files until you have verified agent synchronization. Do not use
`agents write --force` to resolve a migration warning without reviewing and backing up
customizations: that flag explicitly overwrites agent prompts. See [customize.md](customize.md).

## Site and future commands

The canonical product URL is [retropair.dev](https://retropair.dev). Website implementation,
including dedicated documentation/privacy/pilot pages, is a separate task; the local
[documentation](../README.md) remains the available reference.
`/retropair:retro` is a planned shortcut, not an available skill. It must never implicitly
approve team/course reviews.
