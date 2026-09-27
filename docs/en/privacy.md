# Privacy: what stays with you and where things go

Agent session logs are your conversation, your code and everything the agent read: configs,
command output, sometimes keys. This page is about where sessionretro keeps that data, what the
models see, what goes over the network and what the automation does not catch.

## In short

- Everything runs on your machine: python scripts, state in folders of your project and your home
  directory. The plugin has no server, no account and no telemetry.
- Models are called through your Claude Code, under your account and its terms. They get sanitized
  copies of the logs, not the originals.
- Every log passes the sanitizer before it is copied. The plugin never modifies your original logs.
- The plugin sends nothing by itself: not reports, not proposals to the authors, not logs.
- The sanitizer works by rules, not by meaning. What it misses is listed at the end of this page.

## Where the data lives

| Folder | What is there | Who sees it |
|---|---|---|
| `.sessionretro/` in the project | config, person cards, sanitized log copies, run history, review texts, reports | you and whoever shares the repository with you |
| `~/.sessionretro/` | the registry of your workspaces, shared defaults, the page of all workspaces | only you |
| `~/.sessionretro/clones/` | clones of git repositories you listed as log sources (team and course modes) | only you |

Do not commit `.sessionretro/logs/`, `runs/pending/`, `reports/`, `collect.log` and
`collect.lock`; `setup` offers to add them to `.gitignore`. Person cards in team and course modes
hold names and profiles: commit them to a shared repository only with the people's consent.

The SessionEnd hook runs collection only in workspaces from your registry
`~/.sessionretro/workspaces.json`, that is, the ones your own `/sessionretro:setup` created. Someone
else's repository with a committed `.sessionretro/config.json` does not start collection: the hook
prints to stderr that the workspace is not registered and exits.

## What the models see

Three Claude Code agents do the assessment: the collector, the judge and the text critic. They run
in your Claude Code session like any other agent: whatever they read goes to the model through
your Claude Code under its rules. The plugin has no channel of its own.

- **Collectors** read the sanitized copies in `.sessionretro/logs/`. They do not get the person
  card.
- **The judge** gets the collectors' facts, paths to the log copies to verify quotes, open
  recommendations from earlier periods and a short slice of the card: role, grade, experience,
  stack, how to write the text. The name and the text formatting are not part of the slice.
- **No agent gets** past scores and levels, the reviewer's notes about the person, or logs and
  texts of other people.

You choose the models: the "Models" question in `/sessionretro:setup` or `models` in
`config.json` ([customize.md](customize.md)).

## What goes over the network

The plugin's scripts do not go online except in two cases you turn on yourself:

- `git clone` and `git pull` of repositories you listed as log sources;
- `specstory sync`, if SpecStory is one of the sources. SpecStory is a separate program with its
  own settings; the plugin only calls the export into the project's `.specstory/history`.

Reports are HTML files with no external scripts or fonts; they open without a network, and you
send them yourself. Proposals to the plugin authors are written to `.sessionretro/improvements/`
and leave only when you open an issue yourself.

## The sanitizer

The sanitizer works offline and deterministically. It replaces with labels:

- keys and tokens (AWS, GitHub, Anthropic, OpenAI, Stripe, Google, Slack, JWT, private keys),
  passwords in assignments and connection strings;
- e-mails, phone numbers, passport, card and IBAN numbers;
- the login in macOS, Linux and Windows home paths and in Claude Code session folder names;
- internal hosts and private IP addresses;
- names from person cards and everything you list in `sanitize.names`;
- your own patterns from `sanitize.extra_patterns`.

The full table of rules and labels is in Russian in [collect.md](../ru/collect.md). Paths in the
`.meta.json` file next to the copies are written without the login.

Before a personal report is written, a separate check looks for names and ids of other people in
the workspace; if it finds any, the file is not written. This check skips the workspace title, and
the title is shown in every personal file: do not put names in it. If the title contains words from
person cards, `report` prints a warning with the ids of those cards and still writes the file.
`--anon` builds a copy of the reports with codes and made-up names; there the title is anonymized
and checked together with the rest of the text.

## What the automation does not catch

The honest limits of version 0.1:

- **Names of people mentioned inside a log.** The sanitizer only knows the names from person cards
  and from `sanitize.names`. A colleague, a client or a fellow group member named in the conversation but not
  listed there stays in the copy as is. If the judge notices signs of unsanitized data in a log, it
  may raise the "Unredacted input" flag, which the reviewer sees, but that does not replace the
  cleanup. Add everyone who must not appear in the logs to `sanitize.names` and run `collect`
  again: the copies are re-cleaned.
- **Short name words.** From a card name the sanitizer takes the whole name and every word of three
  letters or more. A one- or two-letter name in free text is not removed.
- **Names in free text.** A regular expression cannot tell a client, project or city name from an
  ordinary word. Before you hand logs to anyone, look through at least one copy in
  `.sessionretro/logs/` yourself.
- **Extra replacements.** The phone, card and passport rules sometimes fire on numbers in tool
  output. That spoils a number in the copy but exposes nothing.
- **Windows.** Collection is not supported on Windows in 0.1; the path is a manual export and
  collection on macOS or Linux.

## How long logs live

Claude Code deletes its session logs older than `cleanupPeriodDays` days (30 by default). The
SessionEnd hook takes a sanitized copy after every session, and `setup` offers to raise the limit
to 90 days. Copies in `.sessionretro/logs/` live until you delete them. You can delete a copy at
any time: it drops out of the next assessment, and the session stays in the inventory with the
status `gone`.

## If something leaked

Open an issue with the "Privacy" template in the plugin repository: what kind of data (path, name,
key, e-mail) and at which step (collection, sanitizer, text to the person, HTML report). **Without
the data itself**: do not attach logs, names or keys, whole or in fragments. Such issues are
handled first.

## More

- [How the assessment works](how-it-works.md)
- [Customizing: person cards, the text template and project agents](customize.md)
- In Russian: [collection and the sanitizer](../ru/collect.md), [where state is kept](../ru/workspace.md), [reports](../ru/reports.md), [FAQ](../ru/faq.md)
