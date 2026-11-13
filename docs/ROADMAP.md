# Roadmap

This page says what version 0.2.0 contains, what is being built now, and what may come after. Version 0.2.0 is the latest release, dated 2026-11-12 (`.agents/VERSION`, `CHANGELOG.md`); it follows 0.1.0, dated 2026-11-06.

Each item names where it lives in the code as `file::symbol`. `teamlib/run.py::mask_stream` means the function `mask_stream` in `.agents/bin/teamlib/run.py`; `bin/` means `.agents/bin/`. The rules, roles and skills are instructions to the models: the code does not enforce them.

## Now: what 0.2.0 contains

Everything 0.1.0 contained, listed here, and what 0.2.0 adds ("New in 0.2.0", below).

### The team

- A team in a folder: lead, worker, review, decide, and for flows scout, bugfixer and planner (`.agents/team/bots/`). Fresh setup, update and remove follow one tested path in `INSTALL.md` (`scripts/install-smoke.sh`).
- One team image, own storage per bot, a container only while a bot works; nothing of the host mounted; remote engines on VMs and servers through an SSH alias (`teamlib/engine.py::bot_command`, `teamlib/engine.py::where`). `team bot reset` and `remove` stop, and remove nothing, when a bot's storage cannot be archived (`teamlib/engine.py::archive`).
- Example bots and connectors, off until turned on (`.agents/bots/`, `.agents/connectors/`); `team doctor` for setup problems (`teamlib/status.py::cmd_doctor`).
- Any agent CLI; keys passed per run, never stored with the bots; subscription logins per bot with `auth = login` (`teamlib/engine.py::uses_login`); tier, model and effort per run (`teamlib/run.py::prepare_model`); a `pass` secret backend (`bin/secret::read_pass`).
- Masking of registered values and key patterns in output, results, logs and run records, including long private-key bodies in streamed output (`teamlib/core.py::mask_secrets`, `teamlib/run.py::mask_stream`). The known gaps are listed in `README.md` under "Limits".
- Flows: `team flow` from a brief to `ready`, `blocked`, `limited` or a question: task classes, a pre-gate without a model, review on the exact head, bugfixer for `[bug]` findings (`teamlib/flow.py::do_flow`, `teamlib/flow.py::review_verdict`). `bin/team` is a launcher of command modules (`bin/team::registry`).
- A review delivered by hand, outside a flow, can be proposed for merge too, but only with an approval of the exact head of the branch (`teamlib/state.py::do_merge`).
- Logs with status, timeline and search (`bin/log`). Export and import of a team (`teamlib/porting.py::cmd_export`, `teamlib/porting.py::cmd_import`): import applies only data; settings, `provision.sh` and connectors are quarantined in `team/import/`; with `--with-data` a snapshot of the event log, flows, goals, the lead's briefs and the practice files comes along, which import applies only to a team without events. A guard hook for the lead's own session (`bin/guard::check_hook`); `--repo` on another repository and at any commit (`teamlib/run.py::split_repo`).
- An optional egress allowlist for bot containers: a proxy limits outbound hosts and model API paths, and each run gets only its own CLI's model hosts (`teamlib/engine.py::egress_prepare`). Keys still reach the bot; there is no key proxy yet.
- `mode = bwrap` on Linux without a container engine: a bubblewrap sandbox per run (`teamlib/bwrap.py::box_argv`), a team prefix for the image (`teamlib/bwrap.py::ensure_prefix`), the gate in the sandbox (`teamlib/bwrap.py::gate_run`). CPU, memory and process limits come from a systemd user scope where one works (`teamlib/bwrap.py::limit_argv`, `teamlib/bwrap.py::scope_usable`); without it a run warns and goes on without them. `team doctor` refuses a bwrap setup that cannot run a bot (`teamlib/status.py::bwrap_doctor`). It is weaker than a container (`docs/DESIGN.md` §2).
- `team update`: a preflight that lists the changes, an upgrade path from the installed version to the target, a backup before every apply, rollback, and `team --version` (`teamlib/update.py::do_update`, `teamlib/update.py::upgrade_path`, `teamlib/update.py::make_backup`, `teamlib/update.py::restore_backup`). The rules for release entries are in `.agents/upgrade/README.md`.
- `team update --bots` in container mode: a preview of a team image rebuild when its recipe changed; with `--yes` it builds under a temporary tag, puts the image in use only after a successful build while no bot task runs, and checks each active bot's login read-only; storages, logins and keys stay (`teamlib/update.py::refresh_bots`, `teamlib/update.py::refresh_image`) (T-0108).
- The redesigned wouf.dev website (`site/`).
- `team demo`: an offline demonstration with a fake agent of the gate and the exact-head approval (`teamlib/demo.py::do_demo`); since 0.1.0 it ends with a receipt (below).
- The test suite runs in parallel processes (`scripts/test-parallel.py::main`).

### A: speed core

- A boot in one call (`team context`, `teamlib/status.py::cmd_context`) and a lean pack: skills point at the commands, `team skill NAME` prints one skill's "Sequence of work" (`teamlib/help.py::do_skill`), `team brief new` a brief skeleton (`teamlib/help.py::do_brief`).
- Unchanged run-record indexes stay warm (`bin/log::index`); one SSH round trip per container pipeline (`teamlib/engine.py::shell`); a warm gate cache, which `team gate --fresh-cache` empties (`teamlib/gate.py::fresh_cache`).

### B: state engine

- State in an append-only event log (`bin/log::event_append`), a SQLite cache and Markdown projections: `team task`, `team question`, `team decision`, `team schedule`, `team merge propose` (`teamlib/state.py::do_task`, `do_question`, `do_decision`, `do_schedule`, `do_merge`). Hand edits of the board are found and offered for import (`team state`, `teamlib/state.py::do_state`).
- Knowledge: `team ask` with quoted, cited hits (SQLite FTS5), lessons proposed by bots and accepted by the lead (`teamlib/ask.py::do_ask`, `teamlib/ask.py::do_lesson`), optional project sources; a context graph per bot task (`teamlib/graph.py::graph`).

### C: project operating system

- `team stats` with token numbers and the lead's share (`teamlib/stats.py::cmd_stats`); the tiers `strong`, `mid` and `light` (`teamlib/engine.py::tier_model`); usage limits, waited out or handed to a fallback runner (`teamlib/run.py::limit_hit`, `teamlib/flow.py::flow_next`); one capped `LOGIN.md` per bot task (`teamlib/login.py::build`).
- Parallel stages in a flow; `team batch` runs one plan wave on several workers, with one plan schema for planner and batch, the repository and base kept, a stopped wave resumed, and an overlap of paths refused before anything starts (`teamlib/batch.py::run_batch`, `teamlib/batch.py::first_overlap`); `team meta` for repeated step chains (`teamlib/meta.py::cmd_meta`).
- Goals through four phases with `team goal` (`teamlib/goal.py::do_goal`): INIT with a classifier without a model (`teamlib/classify.py::classify`), CONCEPT as a class `plan` task that can be resumed (`teamlib/goal.py::goal_concept`), REALIZE through `team batch` (`teamlib/goal.py::goal_realize`), VERIFY with verdicts and merge proposals, which reports a head or a package count that does not match (`teamlib/goal.py::goal_verify`).
- Session hooks at zero cost (`team context --claude`, `team session end`, `teamlib/session.py::cmd_session`), staged runs (`--staged`, `team apply`, `teamlib/staged.py::do_apply`), token caps per run and per day (`teamlib/engine.py::budget_caps`), and an end date for a bot (`teamlib/core.py::until_days`).
- Only the team skills marked active, or on trial for this task, go into a sandbox (`teamlib/run.py::staged_team_skills`).

### D: companion and showcase

- The companion cockpit, an optional catalog tool (`catalog/tools/companion/companion.py`), on `team snapshot --json` and `team events` (`teamlib/snapshot.py::do_snapshot`, `teamlib/snapshot.py::do_events`), with owner actions behind a launch token. `bin/project` turns a plain goal into a brief and task waves (`bin/project::cmd_brief`, `bin/project::cmd_deps`).
- A worked example of one real task, from brief to approval (`docs/examples/T-0001.md`).

### E: A2A bridge

- Signed envelopes between two leads (`bin/federate::cmd_pack`), a git mailbox transport (`bin/federate send`, `pull`: `bin/federate::cmd_send`, `bin/federate::cmd_pull`) and the inbox (`bin/federate receive`, `bin/federate::cmd_receive`). A received task, question or decision becomes an event in the team's own state, under the same task-id rules as `team task` (`bin/federate::apply`, `bin/federate::check_task`). See `docs/FEDERATION.md`.

## New in 0.2.0

Not in 0.1.0; `CHANGELOG.md` lists them under "[0.2.0] - 2026-11-12".

- Change receipts (`docs/RECEIPTS.md`): R1 the format and signature (`teamlib/receipt.py::check_receipt`, `teamlib/receipt.py::sign`); R2 and R2.1 the evidence each flow step and `gate.json` keep (`teamlib/evidence.py::never_ran`, `teamlib/evidence.py::egress_summary`); R3 `team receipt make` and `team receipt resign`, with the file in `team/receipts/` and a git note in `refs/notes/wouf-receipts` (`teamlib/attest.py::build`, `teamlib/attest.py::store`, `teamlib/attest.py::resign`); R4 `team verify`, offline (`teamlib/verify.py::do_verify`); R5 `team receipt export --in-toto` (`teamlib/intoto.py::cmd_export`). The lead makes a receipt after a landing (`skills/review-merge.md`); nothing makes one by itself.
- `team demo` ends with a receipt made, signed with a throwaway key, verified offline and a tampered copy refused (`teamlib/demo.py::receipt_step`).
- Task classes from the config: every `[class.<name>]` with a `tier` is a class, and `steps` and `rounds` set its flow and its review rounds (`teamlib/core.py::known_classes`, `teamlib/flow.py::class_table`, `teamlib/flow.py::flow_next`).
- `[egress] private`: a narrow `host:port` exception for a model server on your own computer (`teamlib/engine.py::egress_prepare`, `catalog/tools/egress-proxy/egress_proxy.py::parse_private`).
- `team flow TASK --close --reason TEXT`: an end event for a flow that landed another way (`teamlib/flowend.py::close`).
- New task and goal ids keep the team's zero padding (`teamlib/core.py::next_task_id`).

## In progress

- `team update` in real use (T-0107). The command, the upgrade path and the backups are in 0.1.0 (above). The release entries are 0.1.0 (`.agents/upgrade/0.1.0.md`) and 0.2.0 (`.agents/upgrade/0.2.0.md`, from 0.1.0, no steps), so no upgrade step from one release to the next has run yet.

## After 0.2.0

Plans, not promises. Nothing here has a date.

- A key proxy for egress runs, so that keys no longer reach the bot itself (`docs/specs/EGRESS-AND-KEY-PROXY.md`).
- Verified runner commands for each agent CLI version, and a local-model runner that works out of the box. Today the catalog has a helper that checks a local OpenAI-compatible server and prints a runner section (`catalog/tools/local-runner/`).
- A sandbox without a container engine on macOS (Seatbelt), as `mode = bwrap` is on Linux.
- Connector wiring for Codex CLI (its MCP settings live in its own config file).
- Moving the remaining useful parts of earlier skills and guardrails projects here, then archiving them.
- One source for tiers: today a run takes the tier from the command, then the bot's declaration, then `[tiers]` in `config.ini` (`teamlib/run.py::prepare_model`).
