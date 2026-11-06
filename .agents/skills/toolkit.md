# Skill: Improve the toolkit

## When to use

When the same steps or lookups come up a second time, a bot needs a service (GitHub, a browser, documentation, a database), a skill or connector has done its job, a bot proposes a lesson, and once a week.

## Required inputs and access

- Connectors: `.agents/connectors/`, `team/connectors/`, each bot's `connectors:` line
- Skills and lessons: `team/tools.md`, `team/skills/`, `skills/_template.md`, `team lesson list`
- The evidence: tasks or log lines where the need showed up (`team meta`, `team stats --transcripts <dir>`)

Approval: proposing needs none. A connector, or anything needing a new key, host or permission, needs the human. Activating a skill or accepting a lesson is WOUF's call after review. Turning things off is always allowed.

## Sequence of work

1. **Connector** ("WOUF, I need GitHub access"): find it in `.agents/connectors/` or write `team/connectors/<name>.md` in that format. Tell the human what it gives which bot, the access and the key. On yes: add it to that bot's `connectors:` line, the human stores the key (`skills/secrets.md`). Only that bot gets it, next run. Off: remove it from the line when it is not needed.
2. **Skill**: `team/skills/<name>.md` from the template, `status: proposed`, a row in `team/tools.md`, at least two tasks that needed it; prefer extending a skill. Review checks it; `trial` for named tasks (their ids in its row's Used by cell: only their runs get it); `active` after two clean uses (sent into every run). A skill is staged only when it has one `team/tools.md` row and its file's `status:` does not differ from it; `proposed`, `disabled` and `retired` never are.
3. **Lessons**: bots' knowledge comes back as proposals; `team lesson list`, then `team lesson accept L-n` or leave it. Your own: `team lesson add --title T --topic <file> "body"`. Accepted lessons are found by `team ask`.
4. **Retire**: a skill done or unused for 30 days, a connector no bot listed for 30 days: delete the file; git keeps it.
5. Every change: one row in `team/tools.md`, one log line.

## How to validate the result

- Every connector a bot lists exists and its keys are stored (`team doctor`).
- Every file in `team/skills/` has a row in `team/tools.md` with the same status.

## What to return

- The proposal to the human, or the changed declarations, `team/tools.md`, `team/skills/` and lessons; weekly: what to switch off

## What requires approval

Turning on a connector, anything needing a new key, host or permission, and any skill that lets a bot act outside its role.
