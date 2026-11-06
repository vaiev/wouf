# Hub CLI

Hub coordinates agent work and searches shared notes, decisions and documents using
SQLite WAL and FTS5, with optional sqlite-vec/fastembed semantic search.

## Install and run standalone

Needs Python 3.9+ with SQLite FTS5 (standard). Copy this folder anywhere and use it in place:

```sh
cp -r catalog/tools/hub ~/tools/hub
export HUB_HOME=~/tools/hub          # optional; the wrapper defaults to its own folder
export HUB_BOT=worker                # your identity for claims and handoffs
~/tools/hub/hub brief
~/tools/hub/hub status
```

Put a symlink to `hub` on your `PATH` if you like; the wrapper resolves it. Optional semantic search needs `pip install sqlite-vec fastembed` (use a virtualenv at `$HUB_HOME/.venv`, which the wrapper prefers); without them the hub uses full-text search only (`HUB_EMBED=none` forces that).

`hub.py` defaults to `$HUB_HOME/hub.db` (or the standard user install directory when
`HUB_HOME` is unset). The wrapper resolves symlinks and uses its directory as the default
home. Embedding model cache and interpreter paths are configurable. No agent identity is assumed.

`kb-sources.sh` reads optional trusted `$HUB_KB_CONFIG` or
`$HUB_HOME/kb-sources.conf`. Configure `KB_ORG`, newline-separated `KB_REPOS`,
`KB_SRC`, and newline-separated `KB_DOCS`. Empty lists ingest no new sources.

Ingest chunks Markdown by heading, deduplicates content, redacts recognizable sensitive
patterns and skips unchanged files. Search and ask cover notes, decisions and documents.
Refresh reports missing files without removing their existing indexed content.

Tests use temporary databases. From the repository root:

```sh
python3 -m unittest discover -s catalog/tools/hub
```

The semantic-search test is skipped when `sqlite-vec` and `fastembed` are not installed.

`HUB_BOT` and `--bot` identities are unauthenticated. Claims and ownership checks
are coordination only, not security. Work update/release and handoff done require an
identity; handoff completion checks the recipient. `--force` overrides ownership and
stores the actor, previous owner, operation and target in `audit_events`.

All supplied stored text is checked for recognizable secrets and IP addresses. Ingest
redacts document content and refuses sensitive source paths, URLs and projects, including
URL credentials and credential query parameters. New database files use mode `0600`.
