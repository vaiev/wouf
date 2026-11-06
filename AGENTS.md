# AGENTS.md

Instructions for AI agents (Claude Code, Codex, Grok Build, Antigravity) working on this repository. The pack these agents give to other teams lives in `.agents/`; this file is about changing the repository itself.

## Language

Code, comments, docs, commit messages and PRs: English.

## Workflow

- TDD: add or update a failing test first, then the change. Run the full suite before every commit.
- Test command: `python3 scripts/test-parallel.py`
- Small, focused commits with conventional prefixes (`feat:`, `fix:`, `docs:`, `test:`, `chore:`).
- Work on a branch and open a PR. Never push to `main` directly. PRs merge only after Review Bot approves.

## Guardrails

- Never commit secrets, private keys, tokens, `.env` files, IP addresses or host names of private machines.
- The pack stays generic: no organisation-specific values, no pinned model versions (tiers `strong`, `mid` and `light`, see `.agents/skills/dispatch.md`; only records of a real team, `docs/examples/` and the README's "Built with WOUF", name models). Keep it small: merge before you add.
- `START.md` is loaded every session: keep it under 700 words.
- Every skill keeps the six sections of `.agents/skills/_template.md`, in order, and is named in `.agents/roles/lead.md`.
- Do not change repository visibility, publish releases, or post outside the PR without owner approval.
- No force pushes or history rewrites on shared branches.

## Layout

- `INSTALL.md`: the setup the user's agent CLI follows
- `.agents/`: the pack (`START.md`, `rules.md`, `roles/`, `skills/`) and its catalogs (`bots/`, `connectors/`: examples, off by default)
- `.agents/bin/team`, `.agents/bin/log`, `.agents/bin/secret`, `.agents/bin/federate`, `.agents/bin/project`, `.agents/bin/guard`: helpers (standard-library Python, Python 3.9+; tested in `tests/test_team_cli.py`, `tests/test_log.py`, `tests/test_secret.py`, `tests/test_federate.py`, `tests/test_project_phases.py`, `tests/test_guard.py`; container tests need Docker or Podman)
- `.agents/team/`: the team layer users copy as defaults (config, core bots, empty board); keep it free of example data
- `docs/`: roadmap and design notes for the repository
- `tests/`: test suite
- `scripts/pre-gate.sh`: this repository's fast check of an agent branch before review (helpers parse, suite passes); `team flow` runs it only where `[flow] gate` names it (the shipped `.agents/team/config.ini` leaves `gate` empty: Paths only)
