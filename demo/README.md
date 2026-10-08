# Demo workspace

A ready-made RetroPair workspace for trying the reports before you point the plugin at your own logs.

## What it is

Anonymized data of a real learning group: 15 participants, 43 review runs (3 of them retracted),
five periods (P2–P6) and 40 approved reviews. The names on the person cards are fictional. Every text
passed the sanitizer: names and handles of real people, e-mails, links, home paths and secrets are
replaced, a mention of another participant reads «участник», and a line the privacy checks still
flagged is replaced with `[скрыто в демо]`. Commit hashes, session ids and log file names that could
lead back to the source repositories are replaced with synthetic ones (`session-NNN.md`), and paths
inside a participant's repository start at `<repo>/`. The texts are in Russian (workspace language `ru`,
mode `course`).

## How to open

From the plugin root:

```bash
cp -R demo/workspace /tmp/retropair-demo-ws
python3 scripts/rp.py --workspace /tmp/retropair-demo-ws report --group --all-personal --reviewer --out-dir /tmp/retropair-demo
```

Then open `group.html`, `reviewer.html` and `personal/*.html` from `/tmp/retropair-demo` in a browser.
Use the copied workspace and a temporary output folder outside the plugin: CLI commands update
workspace version metadata, so do not run them against the shipped source.

## What is inside

All state lives in `workspace/.retropair/`:

- `config.json`: workspace settings (mode `course`, language `ru`);
- `people/`: 15 person cards with different feedback settings, plus `_default.yaml`;
- `runs/process-reviews.json`: the run tracker with axis scores and their quotes, levels,
  recommendations and how each was followed later;
- `reviews/`: review texts, one per person and run;
- `logs-index.json`: the log index with session keys, states and mechanical metrics only.

No raw session logs are included.

Plugin overview: [README.md](../README.md#demo); in Russian: [README.ru.md](../README.ru.md#демо).
