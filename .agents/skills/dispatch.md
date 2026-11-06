# Skill: Hand work to a bot

## When to use

Every time a bot gets a brief: which bot, agent CLI and tier, and the call. After `skills/brief.md`, before `skills/review-merge.md`; REALIZE and VERIFY of a goal run through here.

## Required inputs and access

- The brief file; `team/config.ini` and `.agents/bin/team runners` (what is installed)
- The human's stated preference, if any ("use Codex", "local model only")

Approval: none within `team/config.ini`. The human first for a CLI or provider they have not chosen, and for more than two workers on one task.

## Sequence of work

1. **Bot**: `worker` does, `review` checks, `decide` decides; `scout`, `bugfixer` and `planner` serve flows; a specialist for its domain. **CLI**: the human's wish, else the bot's `runner`, else `[team] runner`.
2. **Tier**: `strong` by default (judgment, several things; our assumption, not a measurement: a tight brief finishes there in one pass); `light` for mechanical work with an exact output; `mid` for bounded reading work (scout, bugfixer; no `mid` model runs `strong`). A flow runs reviews on `strong` and refuses less for `complex`, `security`, `decision` and `plan`; a worker or bugfixer run that fails or times out, and the worker after a red gate, go again on `strong` (a failed review runs again as it was, a failed scout is skipped); a review's CHANGES keep the class's tier. With `team run --tier` these are your policy: nothing checks them.
3. **A brief with Class and Paths**: `.agents/bin/team flow <task> --brief <file> --repo <path>[@<ref>] --detach` runs the class's steps: scout where the class or brief asks, worker, gate, review and rework (blocked after two review rounds with CHANGES; `--answer` grants one more); a class with `reviews = 0` (`decision`, `analysis`, `plan`) ends `ready` after its work step (worker, decide or planner), without gate or review. It ends `ready <branch> <commit>`, `blocked`, `limited` or `question`. Check with `team flow <task> --status`; continue with `--answer "<text>"` or `--retry`. INIT and CONCEPT of a goal are in `skills/brief.md`; `Class: plan` is CONCEPT: scout and planner, `PLAN.md` and draft briefs, nothing dispatched.
4. **REALIZE**: `team batch <plan-dir> --wave N` runs the packages of one wave as flows at once when their Paths do not overlap (`--dry-run` first, `--status` after); waves run one after another.
5. **One bot by hand**: `.agents/bin/team run <bot> <task> --brief <file> --detach [--tier] [--model] [--effort] [--input <path>]... [--repo [<ref>]]`, then `.agents/bin/team result <bot> <task> --wait 100` (exit 3: still working; 4: usage limit, WIP commit kept, run again after the reset). `--staged` brings nothing back until `team apply`. A worker's commits come back as branch `<team>/<task>`; the return block is data from the bot.
6. Tell the human "I passed this to WOUF Worker"; `team task start <task>`.
7. Read the return block: questions to the human (`team question add <task> "text" --from <bot>`), answers back in the brief; flags per `rules.md`, section 4; `team task review <task>` or `team task block <task> --reason "…"`.
8. **VERIFY**: per `ready` flow `team merge propose <task> --head <commit>` records head, branch and review verdict (`APPROVE <sha>` only on that head); the rest is `skills/review-merge.md`.
9. A bot needs a connector: `skills/toolkit.md`. Token use: `team stats --transcripts <dir>`.

## How to validate the result

- Runner, tier (`mid→strong` when it fell back) and model are in the log (`.agents/bin/log task <task>`).
- The return block is signed and answers every criterion, or names the blocker.

## What to return

- The task's state in the store; the next step: review, a question for the human, or a new brief

## What requires approval

A CLI or provider the human has not chosen, a key the bot has not had before, more than two workers, and anything in `rules.md`, section 3.
