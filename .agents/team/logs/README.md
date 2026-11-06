# Logs

One line per run, one file per member: the lead writes `logs/lead.jsonl`; each bot writes its own log in its sandbox, which comes back to `env/<id>/logs/` after every run. `bin/log` reads both. Append only. Logs are the receipts: what each member did, when, with which result. The board is the state.

Write and read them with `.agents/bin/log` (standard-library Python, no install):

```bash
.agents/bin/log add worker T-12 started "brief read"
.agents/bin/log add worker T-12 done "tests 14/14, 3 files" --ref "PR 7" --cost "12k tok"
.agents/bin/log status                 # where each member stands: its latest line
.agents/bin/log task T-12              # one task across all members, in order
.agents/bin/log find rollback invoice  # full-text search, newest first, 20 lines max
.agents/bin/log tail review -n 5
```

```text
2026-10-19T09:02 worker T-12 done | tests 14/14, 3 files | PR 7 | ~12k tok
2026-10-19T09:20 review T-12 approve | head 1a2b3c4, 0 blocking | PR 7 | ~6k tok
```

Outcomes: `started`, `done`, `blocked`, `asked` (waiting for an answer), `approve`, `changes`, `decided`, `no-change`, `failed`.

Why this shape:

- **Cheap.** Members never load a log file; they ask `bin/log` for the few lines they need. The search runs on a SQLite full-text index (`.index.sqlite`), rebuilt automatically when a log changes. It is a cache, not in git; delete it any time.
- **Traceable.** The `.jsonl` files are plain text in the project's git: the human can see what every member did and when, also after a failure, with any editor or `grep`.
- **Portable.** Any CLI, any model and any operating system with Python 3 can write and read them. Without Python, append the same JSON line by hand.
- **Safe.** `bin/log` refuses text that looks like a secret or a token. No tool output dumps, no transcripts; link the artifact with `--ref`.

When a log passes 500 lines, the lead moves the older lines to `logs/archive/<id>-<YYYY-MM>.jsonl`.
