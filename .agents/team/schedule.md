# Schedule

The lead's clock: reminders, deadlines and routines. The lead reads it at every session start (`.agents/skills/clock.md`) and reports what is due before anything else. Times in ISO format with time zone or as a cron line.

| When | What | Owner | Kind | Ending | Last run |
|---|---|---|---|---|---|
| | | | | | |

- **Kind**: `reminder` (tell the human), `deadline` (a task must be done by then), `routine` (work that runs by itself).
- **Ending**: for routines only, decided before the first run: `standing-yes` (safe and reversible, the human agreed once) or `draft` (stage and stop). There is no option that waits.
- **Last run**: date and outcome from the owner's log line.

Done reminders and deadlines move to the board or are deleted; the log keeps the record.
