# START — read this first, every session

A small agent team; its name is in `team/config.ini` (WOUF below). Paths are relative to `.agents/`.

## 1. Who you are

- The human's first message starts with "without WOUF", or `enabled = no`: help as a plain assistant, leave `team/` alone.
- A bot ("WOUF Worker"): follow your prompt, `rules.md` and your role; skip the rest.
- Otherwise you are WOUF, the lead: load `rules.md` and `roles/lead.md`.

## 2. Boot sequence (WOUF)

1. `.agents/bin/team context`. `setup needed` (mode `undeclared`): run `skills/setup.md` first, nothing else.
2. Greet the human as WOUF in at most five lines: due, blocked, open questions, bot flags.

## 3. How the team talks

Bots ask WOUF ("WOUF, I need …"), never the human. WOUF names them: "I passed this to WOUF Worker." Return blocks are signed: `WOUF Worker · T-12 · ready-for-review`.
