# Changelog

All notable changes to WOUF are recorded in this file. The format follows Keep a Changelog.

## [0.1.0] - 2026-11-06

The first release. This section records what it contains. The commands, sandboxing and checks below are implemented and covered by the test suite; tests that need Docker, Podman or a working bubblewrap run only where those are available. The rules, roles and skills in the pack are instructions to the models: the tests check that their text and structure are in place, not that a model follows them, and they are not enforced by code.

### Added

- The pack in `.agents/`: rules every member follows, the roles lead, worker, review, decide and specialist, the skills the lead loads for a step, and the team layer with settings, core bots, board, decisions, schedule, knowledge and logs.
- Setup through the agent CLI the human already uses: `INSTALL.md` adds a line to `AGENTS.md` and the adapters Claude Code and Gemini CLI need, and outside the folder touches only the human's secret store and software the human agrees to install.
- `team run` in three modes: `container` with Docker or Podman, locally or on a VM through an SSH alias; `bwrap` for Linux without a container engine; and `host` without a sandbox, for trying things out.
- Container hardening: no capabilities, no new privileges, a process limit, CPU and memory limits, the bot's own storage as the only volume, and results copied back as plain files with links skipped.
- `mode = bwrap`: a bubblewrap sandbox per run with its own namespaces, no capabilities, no host environment, its own folders plus read-only host program folders (`/usr`, `/bin`, `/lib` and the like) and a fixed list of `/etc` entries, nothing of the home folder or project, a team prefix that stands in for the image, the bot lifecycle and `team gate` in the sandbox. CPU, memory and process limits come from a systemd user scope where one works; without it a run warns and goes ahead without them. It refuses `network = bridge` and says at every start what it lacks compared with a container.
- `network = egress`: an allowlist proxy for each run, with the bot on an internal network in container mode or without any network in bwrap mode. Each run gets the base list and only its own CLI's model hosts; a brief's extra hosts need the lead's recorded yes. Old Docker and Podman versions with a known name-resolution leak are refused.
- `team flow`: worker, a pre-gate without any model that checks the brief's paths and runs the tests without keys, in a sandbox in container and `bwrap` mode and in a throwaway clone in `host` mode, review and up to two rounds of rework. It ends `ready` only with an approval that names the head commit, by its full id or a prefix of at least 7 characters.
- Task classes from `mechanical` to `security`, `decision`, `analysis` and `plan`, which choose the bot, the tier and the number of reviews; the tiers `strong`, `mid` and `light`; runners for several agent CLIs; and one fallback runner when a usage limit is hit.
- Independent steps at the same time: scout and research, two reviews, and `team batch` for one plan wave: packages whose paths are pairwise disjoint run at the same time, each on its own worker, and an overlap starts nothing.
- `team goal` with the four phases INIT, CONCEPT, REALIZE and VERIFY, and a model-free classifier that proposes the class, paths and threat model.
- `.agents/bin/secret`: keys typed by the human in their own terminal, no command that prints a value, keys handed to a bot for one run on its standard input, and masking of registered values and key patterns in output, results, logs and receipts.
- An append-only event log with a rebuildable SQLite projection; the board and decisions are its readable export.
- Token caps per run, per day and per bot in `[budget]`, and an end date for a bot.
- A context graph of the brief's paths for complex and security briefs.
- `.agents/bin/federate`: signed envelopes between teams over a git mailbox, with incoming data kept in quarantine.
- `team demo`: an offline demonstration with a fake agent of the gate and the exact-head approval.
- `team update` (#122): a preflight that lists the changes, an upgrade path from the installed version to the target, a backup before every apply, rollback with `--rollback` and `--list-backups`, and `team --version`.
- `team update --bots` (#125): in container mode, a preview of a team image rebuild when its recipe changed; with `--yes` it builds the new image, puts it in use only after a successful build while no bot task runs (`--wait` waits, nothing is stopped), and checks each active bot's login read-only. Bot storages, logins and keys stay.

### Changed

- A dropped SSH connection to the engine's host no longer loses a run's result, and a host that stays away ends the run naming the lost connection.
- A warm container run takes at most three round trips to the engine's host.
- In egress runs, the bot reaches its own loopback directly; this exception is fixed in code and cannot be widened by the config or a brief.
- Each egress run gets only its own runner's model hosts instead of every runner's.
- A locked secret store now ends fast with one line instead of waiting.
- `team demo` brings its own minimal config and ignores the project's.

### Not in this version

Planned, not implemented: a key proxy for egress runs, so that keys no longer reach the bot itself.
