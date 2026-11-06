# Skill: Keep the clock

## When to use

At session start (`team context` lists what is due), when the human mentions a date, a deadline, "remind me" or "every Monday", and when a routine reports back.

## Required inputs and access

- The store's schedule (exported to `team/schedule.md`), the current date and time with zone (ask the system, never guess)
- A scheduler if the human has one (cron, systemd timer, the CLI's own); otherwise items come up at the next session

Approval: none for reminders and deadlines. A routine that acts while the human is away needs its ending agreed first.

## Sequence of work

1. Report what is due or overdue first, in at most five lines.
2. New entry: `team schedule add <when> "what" --owner <id> --kind reminder|deadline|routine [--ending standing-yes|draft]`, with an absolute ISO time and zone or a cron line, never "next Friday". Read it back in one line.
3. Routines: the slowest cadence that works, an event before a schedule, a cheap change-check (hash, count, timestamp) before real work, the ending decided before the first run, tier `light` unless they need judgment.
4. With a scheduler: register `.agents/bin/team run <bot> <routine> --brief <file> --tier light`. Without one: say so; it runs at the next session after its time.
5. Done: `team schedule done S-n`. A missed deadline: `team task block <task> --reason "deadline missed"`.
6. Weekly: routines that were "no change" almost every time, or whose output nobody read: propose to slow or remove them.

## How to validate the result

- Every entry has an absolute time or a cron line, an owner, a kind, and a routine its ending.
- Nothing due went unreported.

## What to return

- The due list (or "nothing due"), the entries changed, one log line

## What requires approval

A routine that acts while the human is away, one that costs money per run, and deleting a deadline the human set.
