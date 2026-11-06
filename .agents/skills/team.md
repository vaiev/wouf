# Skill: Manage the team

## When to use

When a bot should be added, changed, stopped, reset or retired; when a bot goes off track; when bots need a tool; when the human wants to share the team, move it, or exchange work with a peer lead. Not for cutting a large goal into tasks: that is a `plan` (`skills/brief.md`).

## Required inputs and access

- `team/bots/*.md`, the example bots in `.agents/bots/`, `roles/`, `team/config.ini`
- `.agents/bin/team` (bot, install, export, import) and `.agents/bin/federate`
- Evidence for a change: tasks, log lines (`team meta` finds repeated step chains), the human's request

Approval: adding, activating or retiring a bot, and applying anything from another team, need the human's yes. Stopping, resetting and inspecting a bot, and adding a tool to the image, do not.

## Sequence of work

1. **Add a bot** only for recurring work in one domain (three tasks in two weeks, or the human asks): `team bot add <example>` (research, writer, tester, ops) or `team/bots/<id>.md` from `roles/specialist.md`; it starts `proposed`. Tell the human in five lines why, what it may do, what it costs. On yes: `status: active`, `team bot create <id>`; a runner with `auth = login`: the human runs `.agents/bin/team bot login <id>` once. `team doctor` checks.
2. **Change a bot**: edit its declaration; it applies on the next run.
3. **Off track**: `team bot stop <id>` at once, list its files with `team bot inspect <id> tasks/<task>` (names only), read `team result <id> <task>` and `log task <task>`, then `team bot reset <id>` and tell the human. A gate went wrong too: `team gate --fresh-cache`.
4. **Tools for all bots**: `team install -- <command>` adds it to `team/provision.sh`; `container` rebuilds the image at once, `bwrap` rebuilds the prefix with `team image`, `host` installs nothing (install it on this computer).
5. **Retire** (idle 30 days or its work moved): on yes, `team bot remove <id>`. Core bots (worker, review, decide, bugfixer, scout, planner) are not retired.
6. **Share or move**: `team export [<id>...]` (never secrets; `--with-data` only when the human asks). Elsewhere: `INSTALL.md` steps 1 to 3, then `team import <file>` lists; on the human's yes `--apply` writes data only and keeps settings, `provision.sh` and connectors in `team/import/` for the human to copy by hand; then `skills/setup.md`. With data, events, flows, goals and practice apply only to a team without events; storage, logins and keys never move: reactivate, create and sign in the bots again.
7. **A peer lead**: `.agents/bin/federate pull` fetches signed envelopes into quarantine; `.agents/bin/federate receive <file>` shows them, `--apply` after the human's yes. `federate status` lists peers.

## How to validate the result

- Every `active` bot has a declaration (`team status` lists them with their last run; it does not check storage), storage from `team bot create <id>`, its key or login (`team doctor`) and an owner sentence no other bot has.
- Exports contain no secrets (the export refuses files that look like they do).

## What to return

- The proposal or the change, in five plain lines to the human; one log line

## What requires approval

Adding, activating and retiring bots, exporting with data, importing a team or applying a peer's envelopes.
