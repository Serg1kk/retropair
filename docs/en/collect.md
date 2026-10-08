# Collecting logs: where RetroPair gets sessions and what it cleans out

The assessment of how you work with an agent is built on session logs. Before assessing anything,
RetroPair gathers those logs in one place: it finds them where Claude Code and SpecStory write
them, cleans out secrets and personal data, and puts sanitized copies into the project workspace.
One command does all of it:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rp.py" collect
```

In Claude Code you can also just ask ("collect my session logs"): the `collect` skill takes over.
Collection runs locally and nothing goes over the network (except `git pull` if you added a `git`
source yourself).

Collection is safe to repeat: the original logs are never changed, copies that have not changed
are not rewritten, and you can run the command as many times as you like.

The command output is in Russian in version 0.2; the examples on this page show it as printed, and
the text explains each line.

## Where the logs come from

The command learns where to look from the `sources` field in `.retropair/config.json`. After
`init` it already holds two sources, the Claude Code sessions and the SpecStory exports of this
project:

```json
"sources": [
  {"type": "specstory", "path": ".specstory/history"},
  {"type": "claude-jsonl", "project": "auto"}
]
```

| Type | What it reads | Settings |
|---|---|---|
| `claude-jsonl` | Claude Code sessions: `*.jsonl` files in `~/.claude/projects/<project folder>/`, together with subagent sessions | `"project": "auto"` for the current project, or an absolute path to another project |
| `specstory` | SpecStory markdown exports, every `*.md` in the folder and its subfolders | `"path"`: a folder relative to the project root |
| `dir` | any folder with logs: `.md`, `.markdown`, `.txt`, `.jsonl` (fits a manual export) | `"path"`: relative or absolute |
| `git` | a repository with another person's logs: the plugin clones it into `~/.retropair/clones/` and runs `git pull` on the next collection | `"url"`, optionally `"branch"`; team and course modes only |

Sources can also go into a person card (`sources:` in `.retropair/people/<id>.yaml`); logs
from them count as that person's logs. The owner of a source in `config.json` is determined like
this: the `"person": "<id>"` field if it is there; otherwise the only card in the workspace (in self
mode that is you). If there are several cards and `person` is not set, collection stops with an
error and asks you to set it.

The `person` field is set only by editing `config.json` by hand. In `setup-apply` answers a source
with `person` is rejected (code `2`): there a person's source goes into `people[].sources`, and
`setup-apply` writes it to `config.json` with `person` itself.

**Team and course: two shared sources after `init`.** The sources above have no `person`. While
there is one card, they count as its logs; as soon as there are two or more cards, `collect` stops
with code `1` on the first of them ("источник 1 конфига без person, а карточек 2", that is, config
source 1 has no person while there are 2 cards). Before collecting, remove these sources from
`config.json` or give each of them a `"person"`. In team and course the `/retropair:setup`
skill does not add them by default (the "people's sources only" option) and gives advice about such
sources when there are two or more cards.

An example for a team where everyone has their own log repository:

```yaml
# .retropair/people/alex.yaml
sources:
  - type: git
    url: git@github.com:example/alex-logs.git
```

A source whose folder does not exist (SpecStory is not installed yet, Claude Code has not been run
in the project) is not an error: it is skipped with a line in the warnings. The `codex-jsonl` and
`cursor-jsonl` types are not supported in version 0.2; Codex and Cursor logs are collected through
SpecStory.

**Consent.** A person's logs are collected only as long as they do not object. If the card has
`consent:` → `logs: false`, `collect` reads none of their sources (neither from the card nor from
`config.json` with their `person`) and writes to the warnings "`<id>`: нет согласия на логи
(consent.logs: false), источники пропущены" (no consent to logs, sources skipped). Snapshots
collected earlier stay in `logs/<id>/`, but in the inventory they become excluded
(`excluded_by: consent`), they go into no batches and no assessment, and `rp.py batches <id>`
refuses with "нет согласия на логи" (no consent to logs). The plugin does not delete the files
themselves: the warning gives their count, and you can delete the folder by hand. Set
`logs: true` back and those sessions become new again; exclusions set by the assessment stay. A
card without a `consent` block (usual in self mode) is collected as normal; a value other than
`true`/`false` is an error. How many cards lack consent is shown by `rp.py doctor` (the
`people_without_consent` line).

**Claude Code keeps its logs for a limited time** (about 30 days by default, the
`cleanupPeriodDays` setting in `~/.claude/settings.json`; `rp.py doctor` shows the current value).
The copy made by `collect` stays in the workspace after Claude Code deletes the original. So it is
worth collecting regularly; the simplest way is to turn on the hook (below).

Ready recipes for specific environments and agents (JetBrains, VS Code, Cursor, Codex, terminal
agents, manual export) are in the
[environment guides](../../references/collector-instructions/README.md) (Russian).

## SpecStory

[SpecStory](https://specstory.com) is a separate program that exports sessions of various agents
(Claude Code, Codex, Cursor, Antigravity) as markdown files into the project's
`.specstory/history/` folder. RetroPair works without it: for Claude Code the `claude-jsonl`
source is enough. SpecStory is needed for other agents and for those who want to read their
sessions in a readable form.

If `specstory` is among the sources and the program is installed (version 2.6.0 or newer),
`collect` runs `specstory sync` in the project root before collecting. Which agents to export is
set by `hook.providers` in the config or by the `--provider` flag; the default is `claude`:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rp.py" collect --provider claude --provider codex
```

No SpecStory, a version older than 2.6.0 or a failed export does not stop collection: the summary
has a `specstory: …` line with the reason, and the files already there are collected.

**SpecStory without the cloud.** By default SpecStory can sync sessions to its cloud and send
analytics. For logs that contain your code this is unnecessary. A ready config with the cloud,
analytics, version checks and telemetry turned off and the built-in secret redaction turned on is
[specstory-config.toml](../../references/collector-instructions/specstory-config.toml). Where to
put it is described in the guide for your environment.

## The `collect` command

```bash
cd ~/work/my-app
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rp.py" collect
```

```text
collect: 2 записей в инвентаре (/home/user/work/my-app/.retropair/logs-index.json)
новых: 2 · выросших: 0 · без изменений: 0 · дублей: 0 · пропавших: 0 · исключённых: 0
specstory: specstory sync claude: ок
санитайзер:
- ai-logs/2026-09-01_10-00-00Z-fix-login-bug.md: home_path 3; имён заменено 0
- ai-logs/cc-scrollback.txt: code_secret 1, home_path 1; имён заменено 0
*.orig найдено (не взяты): 1 · context-report (не взяты): 0 · слаг-файлов SpecStory: 1
предупреждения:
- источник 3 конфига: папки нет, источник пропущен (claude-jsonl: ~/.claude/projects/-home-user-work-my-app)
last_snapshot_at: 2026-09-27T16:58:34Z
```

What the summary shows:

- **the first two lines**: how many sessions are in the inventory and in which status (new, grown,
  unchanged, duplicates, gone, excluded; see "Session statuses" below);
- **specstory**: whether the SpecStory export went through;
- **санитайзер** (sanitizer): for every file where something was cleaned out, which rules fired
  and how many times, and how many names were replaced. Files with no hits are not shown. Paths
  here, in the warnings and in the `.retropair/` files are written without the login: inside the
  project relative to its root, inside the home folder as `~/…`. In a Claude Code project folder
  name the login is replaced too: `-~-` in a path like `~/.claude/projects/-~-Documents-my-app/`
  stands in place of `-Users-<login>-`;
- **`*.orig`, context-report, slug files**: counters of files worth knowing about (below);
- **предупреждения** (warnings): what was skipped and why; collection still succeeded;
- **last_snapshot_at**: when the inventory was updated.

Flags:

| Flag | What it does |
|---|---|
| `--provider <name>` | which agent `specstory sync` exports; can be repeated |
| `--json` | the summary as one JSON object, for scripts |
| `--quiet` | print nothing on success (this is how the hook runs collection); errors still go to stderr |
| `--replace <name>` | overwrite the snapshot `<name>` in `logs/<id>/` even if the new one is shorter or does not look like the existing one (needed after cleaning the original by hand); can be repeated |
| `--workspace <folder>` | the project, if it is not the current folder |

Exit codes: `0` collection succeeded (warnings are not errors), `1` the data did not add up (a
broken pattern in `sanitize.extra_patterns`, a source without `person` with several cards, a `git`
error), `2` a wrong call, for example no workspace. No workspace yet: create one with the
`/retropair:setup` skill or the `init` command (see [workspace.md](workspace.md)).

Two collections running at once in one project do not get in each other's way: while collection
runs, it holds the lock `.retropair/collect.lock`. A manual `collect`, `inventory` and session
marks wait for the lock for up to 300 seconds (after the sanitizer rules change, collection
re-cleans every snapshot, which can take a while); if the wait takes longer than a second, stderr
gets one line, "сбор логов уже идёт (collect.lock), жду до 300 с…" (collection already running,
waiting up to 300 s). If the lock is not released in time, the exit code is `1` with the path of
the lock file; repeat the command later.

The hook's background collection (`--quiet`) does not wait: several terminals of one project
often close almost at the same time. If the lock is taken, it sets the flag
`.retropair/collect.again`, writes "сбор уже идёт; повтор запрошен" (collection already
running; repeat requested) to `collect.log` and exits with code `0`. The running collection, when
it finishes its pass, sees the flag, removes it and makes one more pass, so it also picks up the
sessions that just closed. However many sessions close during collection, there are at most two
passes in a row; if the flag keeps coming back, after three repeats collection stops with a line in
the warnings, and the flag is left for the next collection.

A repeated collection is fast: an original that has not changed since last time is not parsed or
cleaned again.

## What appears in the workspace

```text
.retropair/
├── logs/<person id>/
│   ├── <session key>.md       # sanitized copy of a markdown log (SpecStory, export)
│   ├── <session key>.jsonl    # sanitized copy of a Claude Code session
│   └── .meta.json             # where each snapshot came from and what the sanitizer cleaned
├── logs-index.json            # inventory of collected sessions with statuses
└── collect.log                # output of background collections (written by the hook)
```

- A copy is named by the session key (its id if the log has one), not by the original file name.
- A copy of a Claude Code session is compact: only the messages, the agent's replies and tool calls
  of the main session and the subagents. The model's hidden reasoning does not go into the copy
  (only its length stays), and long tool output is cut to 4000 characters with a mark.
- If an original became shorter than the existing copy (it was overwritten or truncated), the copy
  is not overwritten: the warnings get a line "снапшот … не перезаписан" (snapshot … not
  overwritten) with a ready command. If you shortened the original on purpose, for example cut out
  a turn with private text, repeat collection with `--replace <snapshot name>`: the copy is
  overwritten, and what you cut out leaves it too.
- The same session from two sources gives one copy from the fuller file and a "дубль ключа"
  (duplicate key) line in the warnings. If a session has an id (Claude Code sessions, SpecStory
  exports), the same id always means the same session. The fuller copy is the one with more turns
  of the main conversation, and on a tie the one with more turns overall: for example, a Claude
  Code session together with its subagents is fuller than a copy of the main file alone. Such a
  copy replaces the existing one under the same name, and an assessed session does not become new.
  The "дубль ключа" line appears once; after that there is a counter of known source duplicates in
  the summary until the duplicate file changes.
- A different session under the same key (for example, two manual exports with the same beginning
  that have no id, so the key is computed from the first messages): the existing copy is not
  overwritten, the new one is saved next to it with an `_<8 characters>` suffix in the name, and the
  warnings get a "другая сессия" (different session) line.
- If collection in versions 0.1.2–0.1.4 already put a suffixed copy of a Claude Code session next
  to a copy of the same session, the next collection merges them: the fuller one stays under the
  plain name, the extra file is deleted (it is a copy; the original log is not touched), and the
  warnings get a line "склеена копия той же сессии" (merged a copy of the same session).

**Should you commit `logs/`.** In self mode, for yourself, do not commit `.retropair/logs/`:
even sanitized, it is your conversation with the agent (more in [workspace.md](workspace.md),
"What to commit to git"). If a reviewer reads the logs (team and course modes), you can hand them
the folder `.retropair/logs/<your id>/`: the snapshots in it are already sanitized, and the
paths in `.meta.json` are written without the login. Look through the snapshots before you hand
them over: only you can spot a client name in free text.

## Session statuses

Every session in the `logs-index.json` inventory has a status. The assessment needs it so as not
to go over the same thing twice and to notice that an old session has something new.

| Status | In the summary | What it means |
|---|---|---|
| `new` | новых | the session has not been assessed yet. It stays new on every collection until the assessment takes it |
| `grown` | выросших | the session was assessed, and new turns appeared in it afterwards; the assessment gets only the continuation |
| `unchanged` | без изменений | the session is assessed, no new turns |
| `duplicate` | дублей | the same session as another entry (for example, a SpecStory export and the Claude Code JSONL of one session); one of them goes into the assessment, preferably the JSONL, which has more data |
| `gone` | пропавших | the copy was deleted from `.retropair/logs/` by hand; the entry stays in the inventory for history |
| `excluded` | исключённых | the assessment decided not to take the session (for example, it has a couple of messages). If the session grows later, it becomes new again |

"Grown" counts only turns of the main conversation. If the file changed but no turns were added
(a re-export, different blank lines), the session stays "unchanged". New subagent turns without new
messages in the main conversation are "unchanged" too. For a grown session the assessment reads the
continuation from the first new message of the main conversation; subagent turns that fall between
the old and the new part go into the continuation as context.

To recompute the inventory from the copies already there, without touching the sources:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rp.py" inventory
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rp.py" inventory --json
```

## The sanitizer: what gets cleaned out

A session log is not only your conversation with the agent but also everything the agent read:
code, configs, command output. So every log passes the built-in sanitizer on its way to
`.retropair/logs/`. It works offline and deterministically: the same log is always cleaned the
same way. It does not touch the original files; only the copy changes.

What it finds is replaced with a label in square brackets. The rule names are the same as in the
`collect` summary.

| Rule | What it catches | Label |
|---|---|---|
| `aws_key` | AWS access keys | `[AWS_KEY]` |
| `github_token`, `github_pat` | GitHub tokens | `[GITHUB_TOKEN]` |
| `anthropic_key`, `openai_key`, `stripe_key`, `google_key` | Anthropic, OpenAI, Stripe, Google API keys | `[API_KEY]` |
| `slack_token` | Slack tokens | `[SLACK_TOKEN]` |
| `jwt` | JWT tokens | `[JWT]` |
| `private_key` | private keys (`-----BEGIN … PRIVATE KEY-----`) as a whole | `[PRIVATE_KEY]` |
| `json_secret` | a password, token or key in JSON and dictionaries when the key is quoted: `"password": "…"`, escaped JSON inside a command string `\"password\":\"…\"`, `'api_key': '…'`, `cfg["token"] = "…"`; only the value is replaced and the JSON stays valid. References (`$VAR`, `${VAR}`, `os.environ[…]`, `process.env.…`) and empty values are left alone | `[SECRET_ASSIGNMENT]` |
| `code_secret` | a password, token or key in an assignment with quotes: `DB_PASSWORD = "…"`, `api_key: '…'` | `[SECRET_ASSIGNMENT]` |
| `env_secret` | the same without quotes: `export API_TOKEN=…` | `[SECRET_ASSIGNMENT]` |
| `password_field` | a string typed into a password field (browser form autofill, UI tests): after the field selector (`[name=password]`, `input[type=password]`, `#pwd`, `getByLabel('Password')`) in the same statement, `.value = '…'`, `.fill('…')`, `.type('…')`, `send_keys('…')`; `page.fill('#password', '…')`; a field variable, `pass.value = '…'`, `setVal(pass, '…')` | `[PASSWORD]` |
| `conn_string` | connection strings with a password: `postgres://user:password@host` | `[CONNECTION_STRING]` |
| `password_text` | a password after a label in plain text, a list or a table (a "Password" cell with the value in the next one): `Password: …`, `- pwd — …`, `password Abc123` (after a space only a value with a letter and a digit); the labels are password, passwd, pass, pwd and the Russian пароль; the label can follow an `\n`/`\t` escape in printed source code (`"…\nPassword: …"`) and in grep output | `[PASSWORD]` |
| `password_column` | a password in a column under a header row: CSV, TSV, markdown and psql tables in command or file output: `username,password`, and a line below `alice,…`; including with grep line numbers and paths. Only the password column cell changes; phrases with spaces, numbers, `PASS`/`FAIL`, placeholders and values shorter than 4 characters stay | `[PASSWORD]` |
| `totp_secret` | a 2FA/TOTP secret after a label: `2FA: …`, `Authenticator Key: …` (and the Russian equivalent); the labels are 2FA, TOTP, OTP, MFA, authenticator, secret (and the Russian секрет); a value with a digit, base32 groups separated by spaces or hyphens are taken whole | `[OTP_SECRET]` |
| `base32_secret` | an authenticator key without a label: a standalone upper-case base32 token of 16 characters or more, solid or in groups of 4 (`ABCD EFGH …`), with a letter and a digit 2–7 | `[OTP_SECRET]` |
| `email` | e-mail addresses | `[EMAIL]` |
| `phone` | phone numbers | `[PHONE]` |
| `passport_by_ru` | passport numbers in the `1234 567890` format | `[DOC_ID]` |
| `card` | bank card numbers | `[CARD]` |
| `iban` | IBAN account numbers | `[IBAN]` |
| `home_path` | the login in macOS and Linux home paths: `/home/alice/…` → `/home/[USER]/…`; the login is any characters up to `/`, a space or a quote, Cyrillic included | `[USER]` |
| `home_path_win` | the same for Windows in any case: `C:\Users\alice\…`, escaped slashes (`\\`), mixed and forward slashes after the drive letter, WSL paths under `/mnt/c/`; a login with a space is taken whole | `[USER]` |
| `home_path_munged` | the login in Claude Code session folder names: `-Users-alice-…` | `[USER]` |
| `internal_host` | internal site addresses: `https://….internal`, `.local`, `.corp`, `.intranet`, `.lan` | `[INTERNAL_URL]` |
| `private_ip` | private IP addresses: `10.…`, `192.168.…`, `172.16–31.…` | `[PRIVATE_IP]` |
| `names` | people's names and your own names (below) | `[NAME]` |
| `local_user` | your login and home folder name where they appear outside paths: `ls -l` output, the terminal prompt | `[USER]` |
| `extra` | your own patterns (below) | `[REDACTED]` |

Placeholders from documentation are not cut out: `PASSWORD="test"`, `${TOKEN}`, `<your-key>`,
`process.env.API_KEY` stay as they are. But the placeholder has to be the whole value:
`PASSWORD="test-prod-2026"` is cut out. Words after a label that do not look like a value stay
too: "no password needed", `Password: required`, "2FA: enabled". Session and message ids (UUIDs)
are not mistaken for phone or card numbers.

**Names.** A regular expression cannot tell a surname from an ordinary word, so the sanitizer takes
names from two places:

- from person cards (`name` in `.retropair/people/*.yaml`): the whole name and every word of
  three letters or more (shorter ones are not taken: otherwise a one-letter name in the `me` card
  would cut out every occurrence of that letter as a word);
- from `sanitize.names` in `config.json`: put there everything of yours that must not get into the
  logs: your company, clients, colleagues, internal systems.

Names are matched as whole words, case-insensitively. A word boundary is any character other than a
letter or a digit, `_` included: a name is found in `alice_notes` and in `repo__alice`, but not
inside `malice`.

**Your own patterns.** `sanitize.extra_patterns` holds regular expressions (Python syntax);
everything they match is replaced with `[REDACTED]`. For example, internal tracker ticket numbers:

```json
"sanitize": {
  "names": ["Acme", "Project Falcon"],
  "extra_patterns": ["INT-\\d+", "falcon-[a-z]+\\.acme\\.io"]
}
```

A broken pattern stops collection with code `1` and shows which pattern is broken.

**Changed the settings: the copies are re-cleaned.** Added a name to `sanitize.names`, a new card or
a pattern to `extra_patterns`: just run `collect` again. Every copy remembers a fingerprint of the
rules it was cleaned with; if the fingerprint differs, the copy is cleaned again, from the original,
or, if the original is gone (Claude Code JSONL files are deleted after 30 days), the copy itself.

The automation catches known formats, but only you can spot a client name in free text. Before you
hand logs to anyone, look through at least one copy in `.retropair/logs/` yourself.

## `*.orig` files, slug files and context-report

- **`*.orig`**: raw copies left over after a log was cleaned by hand (some editors and scripts leave
  them this way). They contain what was cleaned out, so RetroPair does not take them and never
  writes them itself, it only counts them in the summary. Do not commit such files or hand them to
  anyone.
- **SpecStory slug files**: exports named like `2026-09-01_10-00-00Z-fix-login-bug.md`: SpecStory
  names the file after the beginning of the first prompt. They are collected as normal logs, but
  the copy in `.retropair/logs/` is named by the session key, so the prompt text does not get
  into its name.
- **`context-report*`**: service reports about the context; they are not collected, only counted.

## The hook: collection after every session

So that you do not have to run collection by hand, the plugin has a SessionEnd hook: when a Claude
Code session ends, it starts the same `collect` in the background and exits at once. The session
does not wait for collection, and the hook writes nothing to the Claude Code window.

- The hook works only in projects with `.retropair/config.json` (in the current folder or
  above). In other projects it finishes in milliseconds and does nothing.
- Collection runs only for workspaces from your registry `~/.retropair/workspaces.json` (written
  by `/retropair:setup`): the path in the registry matches the project root. A cloned repository
  of someone else with a committed `.retropair/config.json` does not start collection: otherwise
  a foreign config could collect your logs from other projects. The hook then writes the line
  `workspace не зарегистрирован … запустите /retropair:setup` (workspace not registered … run
  /retropair:setup) to stderr and exits without writing anything to the project. To collect logs
  in that project, run `/retropair:setup` in it.
- Whether it is on is decided by `hook.enabled` in `config.json`. After `init` the hook is on. You
  can turn it on or off in `/retropair:setup` or right in the config:

  ```json
  "hook": {"enabled": false, "providers": ["claude"]}
  ```

- `hook.providers`: which agents SpecStory exports during background collection.
- The output of background collections and their errors go to `.retropair/collect.log`: a line
  `<time> hook session=<id>` before each run, then whatever collection printed. The file trims
  itself when it grows past 1 MB (the last 200 KB stay).

If fresh logs do not show up after a session, look into `collect.log` first.

## Manual mode

The hook is optional. You can turn it off (`"enabled": false`) and collect when you need to:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rp.py" collect
```

If your agent does not write logs by itself, save the session by hand (in Claude Code with the
`/export` command, in other agents by exporting the conversation to markdown) into a project
folder, for example `ai-logs/`, and add it as a source:

```json
{"type": "dir", "path": "ai-logs"}
```

How exactly to export from your environment is described in the
[environment guides](../../references/collector-instructions/README.md) (Russian).

To see how the plugin read a single log, without a workspace and without writing anything:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rp.py" normalize ai-logs/session.md --stats
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rp.py" normalize ai-logs/session.md --tools-map
```

`--stats` shows the format, the number of turns and metrics, `--tools-map` which tools the agent
called turn by turn. The full list of flags is `normalize --help`.

## Windows

Collection is not supported on Windows in version 0.2: the file locking that `collect` relies on
does not work there, and the command stops with a clear error. There is no SpecStory for Windows
either. The path for Windows is a manual export of sessions with `/export` and collection on a
machine with macOS or Linux; details in
[windows.md](../../references/collector-instructions/windows.md) (Russian).

## More

- [Environment and agent guides](../../references/collector-instructions/README.md) (Russian)
- [Workspace: where RetroPair keeps its data](workspace.md)
- [Privacy](privacy.md)
