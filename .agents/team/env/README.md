# Bot environment (staging)

The lead's own folder for the bots, on this computer. Not in git.

- `<id>/tasks/<task>/in/`: what the lead hands a bot (brief, inputs, repository bundle)
- `<id>/tasks/<task>/out/`: what the bot handed back (plain files only); `output.log` is its full output
- `<id>/tasks/<task>/result.json`, `run.json`, `run.log`, `run.lock`: the run's state, settings, supervisor log and lock (`.agents/bin/team result <id> <task>` reads them)
- `<id>/tasks/<task>/pack/`: the copy of rules, roles, skills and connectors sent into the sandbox for that run
- `<id>/logs/`: the bot's own log lines, added after every run
- `_archive/`: files of bots that were reset or removed

With `mode = container` the bots never see this folder: each bot keeps its own files in its own storage volume, and `.agents/bin/team` copies files in and out.
