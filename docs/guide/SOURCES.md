# Sources

Every factual claim in this guide and in the changelog, grouped by document and heading. Each entry names the repository source and the test that asserts it, as a file and a stable symbol in it, `file::symbol`, not a line number. The symbol is a function, class, method or constant in Python (`Class.method` for a test), the heading in Markdown (`README.md::Limits`), the section and key in `config.ini` (`[environment] network`; for a key documented only in a comment, the section and key the comment names, such as `[egress] allow`), a function in a shell script and a job in a workflow file. A key of `[runner.claude]` stands for the key every `[runner.<name>]` section has, as the comment above `[runner.claude]` describes; `[class.standard]` is the example in the task-class comment of `[flow]`. Every cited symbol was checked to exist, and to back its claim, in the tree of the commit that adds this file. "Test not found" means the claim is a rule or an instruction to the models: it is described, not enforced by code. "Skipped without an engine" or "skipped without bubblewrap" means the test runs only where Docker, Podman or a working bubblewrap is available, and proves nothing on a computer without them.

## How to read the evidence

- Implemented behaviour: source and test.
- Rules and roles: the rule or role file, and "test not found" unless a test pins its text; a pinned text is still an instruction, not a guarantee.
- Plans: labelled as plans, with the source that says so.

## Overview

### What WOUF is

- An agent team in a folder, set up with the CLI you use; lead, worker, reviewer, decider: `README.md::WOUF`. Test not found (description).
- Not a product or a platform; plain instructions plus helper scripts; used with the agent CLI you already have: `README.md::WOUF`, `README.md::Set it up (technical)`; helpers are standard-library Python: `INSTALL.md::3. Check the requirements`, `AGENTS.md::Layout`. Test: `tests/test_pack.py::OpenSourceFilesTest.test_helpers_are_executable` (helpers are executable Python scripts).
- WOUF is only the example name: `README.md::WOUF`, `.agents/team/config.ini::[team] name`. Test: `tests/test_team_cli.py::RunPlanTest.test_renamed_team` (a renamed team, "Atlas Worker").

### A plain folder

- Rules, roles, skills, team layer and helpers and their places: `README.md::Make it yours`, `AGENTS.md::Layout`. Tests: `tests/test_pack.py::PackStructureTest.test_every_skill_has_the_required_sections`, `tests/test_pack.py::PackStructureTest.test_the_lead_role_names_every_skill`, `tests/test_pack.py::PackStructureTest.test_files_named_in_the_pack_exist`.
- Board, decisions, schedule and knowledge are files: `README.md::How it works`, `.agents/rules.md::7. State lives in the event log; the store is its cache, board and decisions are its readable export`. Test: `tests/test_pack.py::LeanPackTest.test_rules_section_7_is_the_owners_sentence` (rule text only).
- An update replaces the pack and keeps `.agents/team/`: `README.md::Make it yours`, `INSTALL.md::Update`. Test: `tests/test_install_smoke.py::InstallSmokeHostTest.test_d10_update_removes_retired_files_and_preserves_nested_content` (the Update block of `INSTALL.md` run on a folder: retired pack files removed and the team's `config.ini` unchanged); needs git and bash, `tests/test_install_smoke.py::InstallSmokeHostTest.setUpClass`.

### A team that works by a method

- One lead that does not do the work; brief; fresh review; decide; human yes for irreversible steps: `.agents/rules.md::1. Roles stay separate`, `.agents/rules.md::3. Reversible is free, irreversible asks`, `.agents/roles/lead.md::Role: Lead (WOUF)`. Test: `tests/test_pack.py::LeanPackTest.test_the_hard_limits_are_unchanged` pins the text of rules 3 and 8; enforcement test not found (instructions).

### Where the safety comes from

- Rules are instructions; Docker, Podman and bubblewrap stage inputs and a repository copy into a separate workspace; host mode has no sandbox and can access the host with the user's permissions: `README.md::Limits`, `.agents/bin/teamlib/engine.py::inbound`, `.agents/bin/teamlib/bwrap.py::stage`, `.agents/bin/teamlib/run.py::stage_run`, `.agents/bin/teamlib/run.py::attach`. Tests: `tests/test_engine_calls.py::EngineCallsTest.test_one_tar_brings_the_inputs_and_the_pack_and_the_bundle_travels_once`, `tests/test_bwrap_isolation.py::BoxArgvTest.test_exactly_the_mount_table_is_bound_and_nothing_else`, `tests/test_team_cli.py::RunPlanTest.test_host_mode_uses_the_safe_command`. Plain files back: `.agents/bin/teamlib/core.py::copy_regular`; tests: see Safety, Bot output is data.

### Try it without a model

- `team demo` is offline with a fake agent and shows the gate and the exact-head approval: `README.md::WOUF`, `.agents/bin/teamlib/demo.py::exercise`. Test: `tests/test_demo.py::DemoTest.test_cli_json_repeat_cleanup_and_text` (zero model calls, gate refuses outside Paths, stale approval refused, folder unchanged, under ten seconds). The "no network calls" wording is asserted as output text, not measured.

### Status

- Version 0.1.0, dated 2026-11-06, guide describes `main`: `CHANGELOG.md::[0.1.0] - 2026-11-06`, `.agents/VERSION`. Publishing a release needs owner approval: `AGENTS.md::Guardrails`. Test not found.

## Method

### One lead

- The lead talks to the human, briefs, dispatches, relays and reports, never does the work: `.agents/roles/lead.md::Role: Lead (WOUF)`, `.agents/rules.md::1. Roles stay separate`, `README.md::How it works`. Test not found (role).
- Bots talk to the lead only; questions collected and asked once: `.agents/rules.md::1. Roles stay separate`, `README.md::How it works`. Test: `tests/test_live_state.py::RunEventsTest.test_a_bot_question_is_asked_by_the_bot_and_reaches_the_board_and_context` (a worker's question reaches the lead's context framed as data from the bot); that bots never address the human is test not found (rule).

### Roles

- Five roles in `.agents/roles/`: `README.md::Make it yours`; `.agents/roles/worker.md::Role: Worker`, `.agents/roles/review.md::Role: Review`, `.agents/roles/decide.md::Role: Decide`. Test: `tests/test_pack.py::PackStructureTest.test_files_named_in_the_pack_exist` (files named in the pack exist).
- Nobody grades their own work: `.agents/rules.md::1. Roles stay separate`, `.agents/roles/review.md::You ask WOUF (and stop)`. Test not found (rule).
- Scout, bugfixer and planner serve flows; planner drafts a plan for the lead: `.agents/skills/dispatch.md::Sequence of work`, `README.md::Use it`. Test: `tests/test_flow_parts.py::FlowNextTest.test_start_with_or_without_scout`, `tests/test_flow_parts.py::FlowNextTest.test_changes_go_to_the_worker_and_bugs_to_the_bugfixer`.
- Example bots research, writer, tester, ops: `README.md::Use it`, `.agents/bots/writer.md::id: writer`. Test: `tests/test_pack.py::PackStructureTest.test_files_named_in_the_pack_exist`.

### The brief

- Brief fields and `team brief new` with the next free id: `.agents/skills/brief.md::Sequence of work`. Test: `tests/test_help.py::BriefNewTest.test_the_skeleton_takes_the_next_free_id_from_the_store`.
- `Class:` picks bot, tier and reviews; `mechanical` light with one review, `security` strong with two: `.agents/bin/teamlib/flow.py::CLASS_DEFAULTS`, `.agents/team/config.ini::[class.standard]`. Test: `tests/test_flow_parts.py::ClassTest.test_class_defaults_keep_their_tiers`.
- Pre-gate without a model: a file outside Paths turns it red; a failing gate command turns it red; a red gate goes back once, a second blocks: `.agents/bin/teamlib/gate.py::cmd_gate`, `README.md::How it works`. Tests: `tests/test_flow_parts.py::ScopeTest.test_globs`, `tests/test_team_cli_gate.py::GateTest.test_red_when_the_command_fails`, `tests/test_flow_parts.py::FlowNextTest.test_a_red_gate_goes_back_once_on_strong`.
- The gate command gets no keys in every mode: `.agents/bin/teamlib/gate.py::gate_host` (host: a scratch clone with an environment of its own, no sandbox). It runs in a sandbox only in container mode (`.agents/bin/teamlib/gate.py::gate_execute`, `--network` from `gate_network`, default `none`) and `bwrap` mode (`.agents/bin/teamlib/gate.py::gate_execute`, `.agents/bin/teamlib/bwrap.py::gate_run`); host mode takes the `gate_host` branch in `.agents/bin/teamlib/gate.py::gate_execute`, as the docstring of `.agents/bin/teamlib/gate.py::cmd_gate` says. The comment on `.agents/team/config.ini::[flow] gate` says "in a sandbox" without naming the mode; the guide follows the code. Tests: `tests/test_team_cli_gate.py::GateTest.test_green_on_the_branch_head_without_keys` (host mode, no key reaches the gate), `tests/test_gate_cache.py::GateCacheTest.test_engine_budget_and_mount_policy` (container argv, stand-in engine), `tests/test_bwrap_gate.py::BwrapGateTest.test_no_key_host_environment_or_network` (bwrap: no key, no host environment, only loopback; skipped without bubblewrap, the `skipUnless` on `tests/test_bwrap_gate.py::BwrapGateTest`), `tests/test_bwrap_gate.py::GateSetup.test_bridge_is_refused` (bwrap refuses `gate_network = bridge`). No test asserts that the host-mode gate is unfenced; that follows from the code.

### Fresh, independent review

- Review in a fresh context and own sandbox, against the brief not the summary: `.agents/roles/review.md::Role: Review`, `.agents/roles/review.md::You check`, `.agents/rules.md::1. Roles stay separate`. Test not found for the fresh context (role instruction); that `team flow` runs a separate review step: `tests/test_flow_parts.py::FlowNextTest.test_the_happy_path`.
- The intended rule: an approval names the exact commit it checked, and a new commit needs a new review: `.agents/roles/review.md::Return block`, `.agents/rules.md::8. Quality` (instructions; test not found for the role text).
- What `team flow` implements: the named commit must be 7 to 40 hexadecimal characters and the head's id must start with it, so a matching prefix of at least 7 characters counts as an approval of the head; another commit, a prefix of fewer than 7 characters or no commit is `stale`, no approval: `.agents/bin/teamlib/flow.py::review_verdict`. Tests: `tests/test_flow_parts.py::ReviewVerdictTest.test_approve_on_the_head` (a 12-character prefix of the head is accepted), `tests/test_flow_parts.py::ReviewVerdictTest.test_approve_on_another_commit_is_no_approve` (another commit is stale), `tests/test_flow_parts.py::ReviewVerdictTest.test_a_short_or_missing_commit_is_no_approve` (5 characters and a missing commit are stale), `tests/test_flow_parts.py::FlowParallelNextTest.test_both_must_approve_the_same_head`. The guide describes the accepted prefix rather than saying a shortened commit is refused.

### Decide

- Decide bot picks one option with risk and next step: `.agents/roles/decide.md::Role: Decide`, `.agents/roles/decide.md::How you decide`. Test not found (role).
- `autonomy` ask by default, auto decides reversible things and reports: `.agents/team/config.ini::[team] autonomy`, `.agents/rules.md::3. Reversible is free, irreversible asks`. Test: `tests/test_pack.py::LeanPackTest.test_the_hard_limits_are_unchanged` (text pinned); behaviour test not found (instruction). A decide bot's decision is not the lead's yes for an egress host: `tests/test_egress_run.py::AllowlistTest.test_a_decide_bots_decision_or_a_pending_one_is_not_the_leads_yes`.
- Always the human's yes: send, publish, deploy, purchase, delete or overwrite outside a sandbox, merge, new access, money: `.agents/rules.md::3. Reversible is free, irreversible asks`. Test: `tests/test_pack.py::LeanPackTest.test_the_hard_limits_are_unchanged` (text pinned); enforcement test not found (rule).

### Speak up

- Bots under `Flags`, the lead to the human: `.agents/rules.md::4. Speak up`. Test not found (rule).

### A whole task on its own

- `team flow` stages and endings, up to two rework rounds: `README.md::How it works`, `.agents/skills/dispatch.md::Sequence of work`. Tests: `tests/test_flow_parts.py::FlowNextTest.test_the_happy_path`, `tests/test_flow_parts.py::FlowNextTest.test_two_rounds_without_approve_block`, `tests/test_team_cli_gate.py::FlowTest.test_ready_after_one_approve`.
- Usage limit: wait, or one switch to a configured fallback: `.agents/team/config.ini::[flow] fallback.<runner>`. Tests: `tests/test_flow_parts.py::FlowNextTest.test_a_limit_switches_to_the_fallback_runner_once`, `tests/test_flow_parts.py::FlowNextTest.test_a_limit_without_fallback_waits_and_then_runs_again`.
- Four phases INIT, CONCEPT, REALIZE, VERIFY; leaving a phase needs a yes: `.agents/skills/brief.md::Sequence of work`. Tests: `tests/test_pack.py::LeanPackTest.test_brief_and_dispatch_describe_the_four_phases` (text), `tests/test_goal.py::ClassifierTest.test_twenty_fixture_goals_at_least_eighteen_match` (classifier), `tests/test_project_phases.py::ProjectTest.test_brief_from_a_plain_goal`.

## Safety

### How `.agents/` is built

- Pack, team layer, helpers, examples off by default: `README.md::Make it yours`, `AGENTS.md::Layout`. Test: `tests/test_pack.py::PackStructureTest.test_files_named_in_the_pack_exist`.
- A connector reaches only the bot that lists it: `README.md::Use it`. Test: `tests/test_team_cli.py::RunPlanTest.test_connectors_are_wired_only_for_bots_that_list_them`; real container check `tests/test_team_cli_export.py::ContainerRunTest.test_4_connectors_reach_only_the_bot_that_lists_them`, skipped without an engine.
- Setup changes only your folder, apart from your own secret store and software you agree to install: `INSTALL.md::Install: set up the agent team in this folder`, `README.md::Set it up (technical)`. Tests: `tests/test_install_smoke.py::InstallSmokeHostTest.test_d06_secret_store_and_approved_install_exceptions_disclosed` (the two exceptions are named in `INSTALL.md` and the setup skill; text only), `tests/test_install_smoke.py::InstallSmokeContainerTest.test_install_in_fresh_containers` (install in fresh containers, skipped without an engine). No test checks the human's home folder after setup; test not found for that.

### Sandbox modes

- `mode` values and `undeclared` start: `.agents/team/config.ini::[environment] mode`. Test: `tests/test_bwrap_run.py::BwrapPlanTest.test_the_plan_is_a_boxed_one` (bwrap plan), `tests/test_team_cli.py::RunPlanTest.test_host_mode_uses_the_safe_command` (host plan).
- Docker, Podman and bubblewrap stage inputs and a repository copy into a separate workspace; host mode runs without a sandbox under the user's permissions: `.agents/bin/teamlib/engine.py::inbound`, `.agents/bin/teamlib/bwrap.py::stage`, `.agents/bin/teamlib/run.py::stage_run`, `.agents/bin/teamlib/run.py::attach`. Tests: `tests/test_engine_calls.py::EngineCallsTest.test_one_tar_brings_the_inputs_and_the_pack_and_the_bundle_travels_once`, `tests/test_bwrap_isolation.py::BoxArgvTest.test_exactly_the_mount_table_is_bound_and_nothing_else`, `tests/test_team_cli.py::RunPlanTest.test_host_mode_uses_the_safe_command`.
- Container: own storage as the only volume, original project and home folders not mounted as such, CPU and memory limits: `.agents/bin/teamlib/engine.py::box_command`, `.agents/team/config.ini::[environment] mode`. Tests: `tests/test_engine_calls.py::EngineCallsTest.test_one_tar_brings_the_inputs_and_the_pack_and_the_bundle_travels_once` (one tar brings the inputs in), `tests/test_team_cli_export.py::ContainerRunTest.test_1_run_in_the_sandbox`, `tests/test_team_cli_export.py::ContainerRunTest.test_2_nothing_runs_while_idle_and_storage_persists` (no container while idle, storage persists), both skipped without an engine.
- Container hardening: `--cap-drop ALL`, `no-new-privileges`, `--pids-limit 1024`: `.agents/bin/teamlib/core.py::HARDEN`. Test: `tests/test_team_cli_export.py::ContainerRunTest.test_8_the_container_has_no_capabilities_and_no_way_to_gain_them`, skipped without an engine.
- Time limit stops a bot: `.agents/team/config.ini::[environment] timeout_minutes`. Test: `tests/test_team_cli_export.py::ContainerRunTest.test_6_the_time_limit_keeps_what_was_done`, skipped without an engine.
- Docker or Podman, locally or through an SSH alias: `.agents/team/config.ini::[environment] engine`, `.agents/team/config.ini::[environment] docker_host`, `README.md::Set it up (technical)`. Test: `tests/test_engine_calls.py::EngineCallsTest.test_a_warm_container_run_takes_at_most_three_round_trips`.
- Bubblewrap: own namespaces, no capabilities, cleared environment: `.agents/bin/teamlib/bwrap.py::FLAGS`. Test: `tests/test_bwrap_isolation.py::BoxArgvTest.test_the_sandbox_always_unshares_everything_and_never_shares_the_network` (the unshare, cleared-environment and session flags in the argv, no `--share-net`), runs everywhere; no capabilities in a real sandbox: `tests/test_bwrap_isolation.py::BwrapIsolationTest.test_the_bot_has_no_capabilities_and_cannot_gain_any`, skipped without bubblewrap.
- Bubblewrap mounts: the run's own paths (storage, inputs, outbox, team prefix, its own `passwd` and `group`), plus, read-only, the host program folders `/usr`, `/bin`, `/sbin`, `/lib`, `/lib32`, `/lib64`, `/libx32` and the `/etc` entries `ssl`, `ca-certificates`, `pki`, `alternatives`, `ld.so.*`, `localtime`, `nsswitch.conf`, `hosts`, where they exist; private `/tmp`, `/dev` and `/proc`: `.agents/bin/teamlib/bwrap.py::HOST_DIRS`, `.agents/bin/teamlib/bwrap.py::HOST_ETC`, `.agents/bin/teamlib/bwrap.py::BINDS`, `.agents/bin/teamlib/bwrap.py::PRIVATE`, `.agents/bin/teamlib/bwrap.py::host_mounts`, `.agents/bin/teamlib/bwrap.py::box_argv`. Test: `tests/test_bwrap_isolation.py::BoxArgvTest.test_exactly_the_mount_table_is_bound_and_nothing_else` (the argv's mount table equals the host mounts of `tests/test_bwrap_isolation.py::HOST_DIRS` and `tests/test_bwrap_isolation.py::HOST_ETC` plus the run's own and private mounts), runs everywhere.
- No home, project or runtime folder is a mount source: only the fixed host list and the run's own `BINDS` are mounted, and an unknown bind is refused (`.agents/bin/teamlib/bwrap.py::BINDS`, `.agents/bin/teamlib/bwrap.py::run_mounts`). Tests that run everywhere: `tests/test_bwrap_isolation.py::BoxArgvTest.test_no_home_project_or_runtime_folder_is_ever_a_source` (no source under `/home`, `/root`, `/run/user`, `/var`, the home folder or the working folder), `tests/test_bwrap_isolation.py::BoxArgvTest.test_bridge_and_unknown_networks_and_binds_are_refused` (a `home` bind is refused).
- The start message says "only its own folders" (`.agents/bin/teamlib/bwrap.py::WEAKER`, said once per run in `.agents/bin/teamlib/bwrap.py::stage`), which leaves out the read-only host mounts above; the guide follows the mount table. Test: `tests/test_bwrap_run.py::BwrapRunTest.test_a_run_returns_its_result_and_commits` (the message appears once in a real run), skipped without bubblewrap (`tests/test_bwrap_run.py::BwrapRunTest`). Real sandbox tests of the mounts, also skipped without bubblewrap: `tests/test_bwrap_isolation.py::BwrapIsolationTest.test_a_project_folder_and_home_with_ssh_key_cannot_be_found_or_read`, `tests/test_bwrap_isolation.py::BwrapIsolationTest.test_a_host_environment_variable_does_not_reach_the_sandbox`, `tests/test_bwrap_isolation.py::BwrapIsolationTest.test_the_bot_has_no_capabilities_and_cannot_gain_any`, `tests/test_bwrap_isolation.py::BwrapIsolationTest.test_no_network_but_own_loopback_and_no_host_socket_is_reachable`, `tests/test_bwrap_isolation.py::BwrapIsolationTest.test_only_bot_storage_is_writable_and_nothing_else_of_the_host_changes`.
- Team prefix stands in for the image: `.agents/bin/teamlib/bwrap.py::module docstring`, `.agents/team/config.ini::[environment] mode`. Test: `tests/test_bwrap_prefix.py::PrefixRecipeTest.test_a_bot_sandbox_gets_the_prefix_read_only_and_first_on_path`.
- Bubblewrap is weaker: no system call filter, said at every start: `.agents/bin/teamlib/bwrap.py::WEAKER`, `.agents/bin/teamlib/bwrap.py::stage`. Test: `tests/test_bwrap_run.py::BwrapRunTest.test_a_run_returns_its_result_and_commits`, skipped without bubblewrap. Root installs skipped: `INSTALL.md::3. Check the requirements`, `.agents/team/config.ini::[environment] mode`. Test: `tests/test_bwrap_prefix.py::PrefixRecipeTest.test_setup_is_skipped_and_the_lines_come_from_runner_and_provision` (setup that needs root is skipped).
- CPU, memory and process limits in `bwrap` mode only where a systemd user scope works: `scope_usable` starts a trial scope (`.agents/bin/teamlib/bwrap.py::scope_usable`); `limit_argv` then wraps the sandbox in `systemd-run --user --scope` with `MemoryMax` from `[environment] memory`, `CPUQuota` from `cpus` and `TasksMax=1024`, and otherwise says "no CPU, memory or process limit on this computer" and runs without them (`.agents/bin/teamlib/bwrap.py::limit_argv`); a bot run applies it in `.agents/bin/teamlib/bwrap.py::launch`, the gate in `.agents/bin/teamlib/bwrap.py::gate_check` (the limits) and `.agents/bin/teamlib/bwrap.py::gate_run`; `team doctor` checks the scope in `.agents/bin/teamlib/status.py::bwrap_doctor`; described in `.agents/team/config.ini::[environment] mode`, `INSTALL.md::3. Check the requirements`, `docs/DESIGN.md::2. Two places, one boundary`. Tests, all with a stand-in `systemd-run` and running everywhere: `tests/test_bwrap_doctor.py::BwrapDoctorTest.test_scope_limits_and_fallback` (the scope with the limits; with no usable scope, the command unchanged and the warning), `tests/test_bwrap_doctor.py::BwrapDoctorTest.test_bot_and_gate_use_scopes_with_configured_limits` (a bot run and the gate use the configured limits), `tests/test_bwrap_doctor.py::BwrapDoctorTest.test_doctor_reports_bwrap_requirements` (doctor names systemd). They check the command line; that systemd enforces the limits: test not found.
- Host mode: no sandbox, runs in `.agents/team/env/<id>/` with the CLI's safe defaults: `.agents/team/config.ini::[environment] mode`, `.agents/team/config.ini::[runner.claude] host_command`, `.agents/bin/teamlib/engine.py::bot_command`, `README.md::Where the bots run`. Tests: `tests/test_team_cli.py::RunPlanTest.test_host_mode_uses_the_safe_command`, `tests/test_team_cli.py::RunPlanTest.test_the_shipped_claude_runner_keeps_its_permission_checks`.

### Network and egress

- `bridge` default, open, reaches local network and metadata service: `.agents/team/config.ini::[environment] network`, `README.md::Limits`. Test not found for the shipped default value (test fixtures write their own config).
- `none` for local models in the container: `.agents/team/config.ini::[environment] network`. Container mode passes it to the engine as `--network`: `.agents/bin/teamlib/engine.py::box_command`. Test: `tests/test_egress_run.py::BridgeTest.test_none` (the spec's network is `none`, stand-in engine). bwrap: no network with `none`: `--unshare-all` without `--share-net`, `.agents/bin/teamlib/bwrap.py::FLAGS`, `.agents/bin/teamlib/bwrap.py::box_argv`. Test: `tests/test_bwrap_isolation.py::BwrapIsolationTest.test_no_network_but_own_loopback_and_no_host_socket_is_reachable`, skipped without bubblewrap (`tests/test_bwrap_isolation.py::BwrapIsolationTest`).
- `host` mode does not apply `network`, `none` included: the host run starts the CLI as a plain process with this computer's environment and network (`.agents/bin/teamlib/run.py::attach`, its branch for a mode that is neither `container` nor `bwrap`: `sh -c` with `os.environ`, no sandbox and no network option); the only host-mode network check refuses `egress` (`.agents/bin/teamlib/flow.py::prepare_egress`). Test for `egress`: `tests/test_egress_run.py::RefusalTest.test_host_mode_refuses_egress`. Test not found for `none` in host mode; the claim follows from the code, which has no network fence on the host path.
- `egress` in container mode: internal network for the bot, proxy on both networks: `.agents/team/config.ini::[egress]`. Test: `tests/test_egress_run.py::TopologyTest.test_two_networks_the_proxy_on_both_and_the_bot_on_the_internal_one_only`, checked against the commands sent to a stand-in engine, not a live network.
- `egress` in bwrap mode: the bot's sandbox never gets `--share-net` (`.agents/bin/teamlib/bwrap.py::FLAGS`, `.agents/bin/teamlib/bwrap.py::box_argv`); the proxy runs in a sandbox of its own with `--share-net` and listens on a Unix socket in the control folder (`.agents/bin/teamlib/bwrap.py::proxy_argv`); a relay inside the bot takes `127.0.0.1:3128` to that socket (`.agents/bin/teamlib/bwrap.py::RELAY`, started by `.agents/bin/teamlib/bwrap.py::egress_launch`). Tests: `tests/test_bwrap_egress.py::HandOffTest.test_the_bot_gets_the_relay_on_its_loopback_and_loopback_only_around_it` (the bot's proxy is the relay on its loopback; runs everywhere), `tests/test_bwrap_egress.py::BwrapEgressTest.test_the_bot_reaches_its_list_through_the_proxy_and_nothing_else` (a real run: the listed host through the proxy, unlisted hosts refused, direct addresses, the host's loopback and DNS fail), skipped without bubblewrap (`tests/test_bwrap_egress.py::BwrapEgressTest`, as `tests/test_bwrap_egress.py::module docstring` says).
- Base list plus the run's own CLI hosts; brief hosts need the lead's recorded yes: `.agents/team/config.ini::[egress] allow`. Tests: `tests/test_egress_run.py::RunnerNeedsTest.test_a_claude_run_and_a_codex_run_on_one_config_each_get_their_own_hosts`, `tests/test_egress_run.py::AllowlistTest.test_a_host_outside_the_base_list_without_a_lead_decision_refuses_the_run`, `tests/test_egress_run.py::AllowlistTest.test_a_recorded_lead_decision_lets_the_host_through_for_that_task_only`.
- Loopback goes around the proxy, directly, and only loopback; fixed in code, not widened by the config or a brief: `.agents/bin/teamlib/engine.py::EGRESS_ENV` (`NO_PROXY` is `localhost,127.0.0.1,::1`), `.agents/bin/teamlib/bwrap.py::BOT_PROXY` (bwrap mode takes the same variables, with the relay as the proxy). Tests: `tests/test_egress_run.py::LoopbackTest.test_the_bot_gets_no_proxy_for_its_loopback_only_next_to_the_four_proxy_variables`, `tests/test_egress_run.py::LoopbackTest.test_the_loopback_exception_is_fixed_in_code_and_does_not_widen` (config and brief cannot widen it), `tests/test_egress_run.py::LoopbackTest.test_urllib_reaches_loopback_directly_and_an_outside_host_through_the_proxy` (loopback reached directly, outside hosts through the proxy; a local stand-in proxy, not a live container), `tests/test_bwrap_egress.py::HandOffTest.test_the_bot_gets_the_relay_on_its_loopback_and_loopback_only_around_it` (bwrap uses the same list; runs everywhere). It is the bot's own loopback, not the host's: `tests/test_bwrap_egress.py::BwrapEgressTest.test_the_bot_reaches_its_list_through_the_proxy_and_nothing_else` (a service on the host's loopback is not reached, loopback inside the sandbox is), skipped without bubblewrap (`tests/test_bwrap_egress.py::BwrapEgressTest`). For container mode, test not found against a live container; it follows from the bot's own network namespace on the internal network.
- The proxy refuses private, loopback and link-local addresses: `catalog/tools/egress-proxy/egress_proxy.py::_refusal`. Test: `catalog/tools/egress-proxy/test_egress_proxy.py::ConnectTest.test_names_that_resolve_to_refused_addresses`, run by `tests/test_catalog.py::CatalogToolTests.test_tool_tests_pass`.
- Old Docker and Podman refused: `.agents/bin/teamlib/engine.py::AARDVARK_MIN`, `.agents/bin/teamlib/engine.py::egress_engine_check`, `.agents/team/config.ini::[egress]`. Tests: `tests/test_egress_run.py::RefusalTest.test_an_old_aardvark_dns_or_cni_is_refused`, `tests/test_egress_run.py::DockerTest.test_a_docker_without_the_cve_2024_29018_fix_is_refused`.
- bwrap refuses `bridge`: `.agents/bin/teamlib/bwrap.py::box_argv` (the sandbox's argv), `.agents/bin/teamlib/bwrap.py::check` (before a run is staged), with the reason in `.agents/bin/teamlib/bwrap.py::BRIDGE`. Tests: `tests/test_bwrap_isolation.py::BoxArgvTest.test_bridge_and_unknown_networks_and_binds_are_refused`, `tests/test_bwrap_run.py::BwrapPlanTest.test_bridge_is_refused_before_anything_is_staged`, `tests/test_bwrap_egress.py::HandOffTest.test_bridge_stays_refused_and_egress_is_not`, all running everywhere. Host refuses `egress`: `.agents/bin/teamlib/flow.py::prepare_egress`. Test: `tests/test_egress_run.py::RefusalTest.test_host_mode_refuses_egress`.

### Keys

- Keys out of repository, chat and bot storage; typed in your own terminal; no command prints a value: `.agents/bin/secret::module docstring`, `.agents/rules.md::5. Secrets`, `README.md::Limits`. Tests: `tests/test_secret.py::SecretTest.test_set_refuses_without_a_terminal`, `tests/test_secret.py::SecretTest.test_there_is_no_get`.
- Keychain or Secret Service, else an owner-only file store refused inside a git repository: `.agents/bin/secret::module docstring`. Tests: `tests/test_secret.py::SecretTest.test_set_then_run_injects_and_masks` (modes 0600 and 0700), `tests/test_secret.py::SecretTest.test_refuses_a_store_inside_a_git_repository`.
- A key for one run on standard input, never in an argument or environment of the container command: `.agents/team/config.ini::[runner.claude] auth`, `docs/DESIGN.md::6. Keys are typed once and never stored with the bots`. Test: `tests/test_engine_calls.py::EngineCallsTest.test_keys_reach_the_bot_only_as_lines_on_its_stdin`; bwrap: `tests/test_bwrap_isolation.py::BwrapIsolationTest.test_a_key_on_stdin_reaches_the_script_and_no_command_line`, skipped without bubblewrap.
- Keys in what a bot hands back are masked: `.agents/bin/teamlib/core.py::mask_secrets`. Tests: `tests/test_engine_calls.py::EngineCallsTest.test_results_are_masked_straight_into_out_and_receipt_values_never_reach_the_host_index`, `tests/test_team_cli.py::WayBackTest.test_a_key_split_over_output_lines_is_masked_whole`, `tests/test_secret_sinks.py::HandlerAndBinaryTest.test_binary_files_are_listed_and_only_their_secrets_are_masked`.

### Bot output is data

- Output framed as data from the bot, not instructions; the lead's role says so: `.agents/bin/teamlib/run.py::report` (a run's output), `.agents/bin/teamlib/status.py::FROM_BOT` (receipts and questions in the lead's context), `.agents/roles/lead.md::Never`, `README.md::Limits`. Tests: `tests/test_team.py::FirstRunDocsTest.test_bot_output_is_data_for_the_lead` (role text), `tests/test_live_state.py::RunEventsTest.test_a_bot_question_is_asked_by_the_bot_and_reaches_the_board_and_context`. That the lead obeys the framing: test not found (it is a model).
- Plain files only; links skipped and listed: `.agents/bin/teamlib/core.py::copy_regular`, `README.md::Limits`. Tests: `tests/test_team_cli.py::WayBackTest.test_a_linked_folder_is_not_followed`, `tests/test_engine_calls.py::EngineCallsTest.test_results_are_masked_straight_into_out_and_receipt_values_never_reach_the_host_index`; real container `tests/test_team_cli_export.py::ContainerRunTest.test_9_links_and_forged_receipts_do_not_come_back`, skipped without an engine.

## Getting started

### What you need

- Agent CLI, Python 3.9+, git, Docker or Podman, bubblewrap on Linux: `README.md::Set it up (technical)`, `INSTALL.md::3. Check the requirements`. Test not found (requirements text).
- The setup names what is missing and installs it after a yes: `README.md::Quickstart (no technical background needed)`, `INSTALL.md::Install: set up the agent team in this folder`. Test not found (setup instruction).

### Set it up

- The sentence to paste: `README.md::Quickstart (no technical background needed)`. Steps of `INSTALL.md`, no overwriting, one round of questions, doctor and smoke test, the key typed by the human: `README.md::Set it up (technical)`, `INSTALL.md::Install: set up the agent team in this folder`, `INSTALL.md::4. Run the setup`, `.agents/skills/setup.md::Sequence of work`. Tests: `tests/test_install_smoke.py::InstallSmokeContainerTest.test_install_in_fresh_containers` (skipped without an engine), `tests/test_install_smoke.py::InstallSmokeHostTest.test_unchanged_guide_passes` (the install smoke script on this folder: setup, bot create, doctor and a signed stand-in run; needs git and bash).

### `AGENTS.md` and adapters

- The `AGENTS.md` line; `CLAUDE.md` and `GEMINI.md` with `@AGENTS.md`; permissions and optional hooks for Claude Code; other CLIs read `AGENTS.md` directly: `INSTALL.md::2. Adapters for the CLI`. Script: `scripts/install-smoke.sh::adapters`. Tests: `tests/test_install_smoke.py::InstallSmokeContainerTest.test_install_in_fresh_containers` (skipped without an engine), `tests/test_install_smoke.py::InstallSmokeHostTest.test_wrong_session_hook_fails_the_script` (the script fails on a wrong session hook; needs git and bash, `tests/test_install_smoke.py::InstallSmokeHostTest.setUpClass`).

### Runners: which CLI runs the bots

- `[runner.<name>]` sections, keys and the shipped `claude`, `codex`, `agy` and empty `local`: `.agents/team/config.ini::[runner.claude]`, `.agents/team/config.ini::[runner.codex]`, `.agents/team/config.ini::[runner.agy]`, `.agents/team/config.ini::[runner.local]`. Tests: `tests/test_team_cli.py::RunPlanTest.test_strong_tier_and_first_installed_runner_by_default`, `tests/test_team_cli.py::RunPlanTest.test_the_shipped_claude_runner_keeps_its_permission_checks`.
- Choose the CLI for the team, a bot or a run: `.agents/skills/dispatch.md::Sequence of work`, `README.md::Use it`. Tests: `tests/test_team_cli.py::RunPlanTest.test_light_tier_and_cli_default_model` (a run), `tests/test_team_cli.py::RunPlanTest.test_the_human_or_the_session_can_choose_the_runner` (the session or human); per bot: test not found.
- Commands follow each CLI's documentation; check them: `README.md::Limits`. Test not found.

### Tiers: which model

- `strong`, `mid`, `light` and their use; empty means the CLI default; mid falls back to strong: `.agents/team/config.ini::[tiers]`, `.agents/team/config.ini::[runner.claude] model_flag`. Tests: `tests/test_team_cli.py::RunPlanTest.test_light_tier_and_cli_default_model`, `tests/test_flow_parts.py::ClassTest.test_a_class_may_run_on_mid`, `tests/test_flow_parts.py::FlowNextTest.test_a_missed_mid_run_goes_again_on_strong`.
- `--model` for one run: `.agents/team/config.ini::[runner.claude] model_flag`. Test: `tests/test_team_cli.py::RunPlanTest.test_effort_and_model_per_run`.
- Review, decision, plan, complex and security stay strong: `.agents/skills/dispatch.md::Sequence of work`. Tests: `tests/test_flow_parts.py::ClassTest.test_complex_security_decision_and_plan_never_run_below_strong`, `tests/test_flow_parts.py::FlowNextTest.test_the_tier_of_each_step`.

### First steps

- `team demo`, talk by name, "without WOUF", settings and bots: `README.md::WOUF`, `README.md::Use it`, `INSTALL.md::5. Hand over`. Test: `tests/test_demo.py::DemoTest.test_cli_json_repeat_cleanup_and_text`; the rest test not found (instructions).

## Limits

### Rules are instructions, not enforcement

- `README.md::Limits`, `SECURITY.md::Scope`. Enforced checks: gate `tests/test_flow_parts.py::ScopeTest.test_globs`, head approval `tests/test_flow_parts.py::ReviewVerdictTest.test_approve_on_another_commit_is_no_approve`; the network setting is enforced only in container and `bwrap` mode (see Safety, Network and egress).

### Host mode has no sandbox

- `.agents/team/config.ini::[environment] mode`, `README.md::Where the bots run`, `README.md::Limits`. Test: `tests/test_team_cli.py::RunPlanTest.test_host_mode_uses_the_safe_command`.
- Network not taken away by `none` in host mode: see Safety, Network and egress (test not found; from the code).
- Host-mode gate in a throwaway clone, not a sandbox: see Method, The brief.

### Bubblewrap is weaker than a container

- No system call filter: `.agents/bin/teamlib/bwrap.py::WEAKER`, `INSTALL.md::3. Check the requirements`; root installs skipped: `tests/test_bwrap_prefix.py::PrefixRecipeTest.test_setup_is_skipped_and_the_lines_come_from_runner_and_provision`. CPU, memory and process limits only with a working systemd user scope, else a warning: see Safety, Sandbox modes (tests with a stand-in `systemd-run`; enforcement by systemd: test not found).

### The default network is open

- `README.md::Limits`, `.agents/team/config.ini::[environment] network`. Test not found for the default value.
- An allowed host can still carry data; over `CONNECT` the proxy sees only the host: `docs/DESIGN.md::2. Two places, one boundary`. Test not found (design note).
- Keys still reach the bot; no key proxy yet: `.agents/team/config.ini::[egress]`, `docs/DESIGN.md::2. Two places, one boundary`. Test: `tests/test_egress_run.py::TopologyTest.test_keys_reach_the_bot_as_today_and_never_the_proxy`.
- Host lists unverified until a trial: `.agents/team/config.ini::[egress]`. Test not found (instruction).
- Private and loopback addresses refused: `catalog/tools/local-runner/README.md::Ollama setup`, `catalog/tools/egress-proxy/egress_proxy.py::_refusal`. Test: `catalog/tools/egress-proxy/test_egress_proxy.py::ConnectTest.test_names_that_resolve_to_refused_addresses`.
- Version requirements: `.agents/team/config.ini::[egress]`. Test: `tests/test_egress_run.py::RefusalTest.test_an_old_aardvark_dns_or_cni_is_refused`, `tests/test_egress_run.py::VersionTest.test_moby_versions`.

### Secret masking has gaps

- Masking is not a barrier; the store is readable by your own user: `README.md::Limits`. Test not found (limitation).
- Known gaps: `README.md::Limits`, `docs/DESIGN.md::6. Keys are typed once and never stored with the bots`. Tests: `tests/test_secret_sinks.py::HandlerAndBinaryTest.test_binary_files_are_listed_and_only_their_secrets_are_masked` (binary files: only registered values are masked), `tests/test_team_cli.py::HostRunTest.test_a_key_block_longer_than_the_hold_limit_is_masked_in_the_bot_output` and `tests/test_stream_masking.py::StreamMaskingTest.test_long_raw_private_keys_hide_every_body_line` (a private-key block longer than the stream buffer still stays masked: these test what is closed, not the gaps). The gaps for registered values split across lines or longer than the buffer, for `/` in a path and for the human's own files are documented, not tested.

### The lead reads what bots write

- `README.md::Limits`. Test not found (limitation).

### What the tests cover

- Tested with Docker and a stand-in CLI; container tests run when an engine is reachable: `README.md::Limits`, `README.md::Develop`, `CONTRIBUTING.md::Checks`. Skip conditions: `tests/test_team_cli_export.py::ContainerRunTest`, `tests/test_bwrap_isolation.py::BwrapIsolationTest`, `tests/test_bwrap_egress.py::BwrapEgressTest`.

### Version 0.1.0

- Dated 2026-11-06, the first release, guide describes `main`: see Overview, Status; `CHANGELOG.md::[0.1.0] - 2026-11-06`. Test not found.

## Examples

### Project management for a non-technical project

- Default `autonomy = ask`: `.agents/team/config.ini::[team] autonomy`. Test not found for the shipped default.
- Under `ask`, routine reversible work goes ahead and other decisions go to the human; send, publish, delete outside a sandbox, new access and paid steps go to the human in every setting: `.agents/rules.md::3. Reversible is free, irreversible asks`. Test: `tests/test_pack.py::LeanPackTest.test_the_hard_limits_are_unchanged` (text pinned); behaviour test not found (instruction).
- Brief to worker, review, board as a file: `README.md::How it works`. Tests: `tests/test_flow_parts.py::FlowNextTest.test_the_happy_path`.
- Sending is left to the human by rule; nothing in the pack stops a bot that has a way to send: `.agents/rules.md::3. Reversible is free, irreversible asks`, `.agents/rules.md::2. Each bot stays in its sandbox` (sandboxes hold no accounts). Test: `tests/test_pack.py::LeanPackTest.test_the_hard_limits_are_unchanged` pins the rule text only; enforcement test not found (instruction).
- Reminders, and that they come up at the next session without a scheduler: `README.md::Use it`, `.agents/skills/clock.md::Required inputs and access`. Test not found (skill).
- Example writer bot: `.agents/bots/writer.md::id: writer`, `README.md::Use it`. Test: `tests/test_pack.py::PackStructureTest.test_files_named_in_the_pack_exist`.

### A coding co-worker

- `gate` in `[flow]` runs the tests without keys, in a sandbox in container mode: `.agents/team/config.ini::[flow] gate`, `.agents/bin/teamlib/gate.py::gate_execute`. Tests: `tests/test_team_cli_gate.py::GateTest.test_green_on_the_branch_head_without_keys`, `tests/test_gate_cache.py::GateCacheTest.test_engine_budget_and_mount_policy`; see Method, The brief.
- Flow, Paths, rework, exact-head approval, branch back: `README.md::How it works`. Tests: `tests/test_flow_parts.py::ScopeTest.test_globs` (path globs), `tests/test_flow_parts.py::ReviewVerdictTest.test_approve_on_the_head` (approval on the head), `tests/test_flow_parts.py::FlowNextTest.test_a_red_gate_goes_back_once_on_strong` (a red gate goes back once).
- `team merge propose` records the proposal and never merges: `.agents/bin/teamlib/state.py::USAGE`. Test: `tests/test_state_cmd.py::MergeTest.test_propose_records_head_branch_and_verdict_and_never_merges` (branches unchanged, "nothing merged").
- Main only with review, green CI and the human's yes is a rule, not branch protection; the repository host must enforce it: `.agents/rules.md::8. Quality`. Test: `tests/test_pack.py::LeanPackTest.test_the_hard_limits_are_unchanged` pins the rule text only; enforcement test not found (instruction).
- Two reviews for `security`, reviewer on another runner: `.agents/team/config.ini::[class.standard]`. Tests: `tests/test_flow_parts.py::FlowNextTest.test_two_reviews_with_their_own_runners`, `tests/test_flow_parallel.py::RunFlowListTest.test_two_reviews_run_at_once_and_both_approve`.
- Tester bot and browser connector after a yes: `README.md::Use it`. Test: `tests/test_team_cli.py::RunPlanTest.test_connectors_are_wired_only_for_bots_that_list_them`.

### In a company

- Bots on a VM through an SSH alias: `.agents/team/config.ini::[environment] docker_host`. Test: `tests/test_engine_calls.py::EngineCallsTest.test_a_warm_container_run_takes_at_most_three_round_trips`.
- Under `ask` and `auto`, irreversible, outward, paid and new-access steps wait for a person by rule; the company enforces it through the access it grants: `.agents/rules.md::3. Reversible is free, irreversible asks`, `.agents/rules.md::2. Each bot stays in its sandbox`. Test: `tests/test_pack.py::LeanPackTest.test_the_hard_limits_are_unchanged` pins the rule text only; enforcement test not found (instruction).
- Egress after a trial, own CLI hosts: see Safety, Network and egress.
- Keys typed by the owner; connectors per bot: `.agents/rules.md::5. Secrets`, `README.md::Use it`. Tests: `tests/test_secret.py::SecretTest.test_set_refuses_without_a_terminal`, `tests/test_team_cli.py::RunPlanTest.test_connectors_are_wired_only_for_bots_that_list_them`.
- Export without keys, without data unless asked: `README.md::Use it`, `.agents/skills/team.md::Sequence of work`. Tests: `tests/test_team_cli_export.py::ExportImportTest.test_export_has_declarations_and_no_data_by_default`, `tests/test_team_cli_export.py::ExportImportTest.test_export_with_data_and_refusal_of_secrets`.
- Token caps per run, per day and per bot: `.agents/team/config.ini::[budget]` (`per_run`, `per_day`, `per_day.<bot>`). Tests: `tests/test_budget.py::BudgetRunTest.test_at_per_day_the_run_is_skipped`, `tests/test_budget.py::BudgetRunTest.test_a_bots_own_cap`, `tests/test_budget.py::BudgetRunTest.test_per_run_marks_the_receipt_and_the_result`.

### An authority, such as the Swiss federal administration

- This is a setup description. Data protection under the Swiss nDSG and sovereignty are the organisation's goals and assessment; no repository source claims WOUF ensures them, and this guide makes no such claim.
- No classification feature: none found in `.agents/`; what bots see is what the lead stages: `.agents/rules.md::2. Each bot stays in its sandbox`, `README.md::How it works`. Test not found.
- Where bots run, `docker_host`, cloud CLI needs network to its model API, empty `local` runner: `.agents/team/config.ini::[environment] docker_host`, `.agents/team/config.ini::[runner.local]`, `README.md::Limits`, `.agents/team/config.ini::[egress]`. Test: `tests/test_engine_calls.py::EngineCallsTest.test_a_warm_container_run_takes_at_most_three_round_trips`; local runner: test not found in the pack.
- Container mode, `none` or `egress`, `bridge` open, private addresses refused: see Safety and Limits.
- The egress allowlist is not the whole list: each run adds its own CLI's model API and login hosts, built in (`.agents/bin/teamlib/engine.py::EGRESS_NEEDS`) or from `needs.<runner>`, which replaces the built-in list (`.agents/team/config.ini::[egress] allow`, `.agents/bin/teamlib/flow.py::prepare_egress`); the run's list is shown at start (`.agents/bin/teamlib/flow.py::prepare_egress`). Tests: `tests/test_egress_run.py::RunnerNeedsTest.test_a_claude_run_and_a_codex_run_on_one_config_each_get_their_own_hosts` (claude and codex runs each get their own hosts on top of `allow`), `tests/test_egress_run.py::RunnerNeedsTest.test_needs_of_a_runner_replace_its_built_in_list` (`needs.<runner>` replaces the built-in list), `tests/test_egress_run.py::RunnerNeedsTest.allow_of` (the run's list at start).
- Under `ask`, routine reversible work goes ahead; other decisions and the always-human steps go to a person, by rule; these are instructions, enforced only by the accounts, keys, destinations and permissions the organisation grants: `.agents/rules.md::3. Reversible is free, irreversible asks`. Test: `tests/test_pack.py::LeanPackTest.test_the_hard_limits_are_unchanged` pins the rule text only; enforcement test not found (instruction).
- A bot given a key can leak it: `README.md::Limits`. Test not found (limitation).
- The review bot checks against the brief only: `.agents/roles/review.md::Role: Review`, `.agents/roles/review.md::You check`. Test not found (role).

## Contribute

### Contribute (introduction)

- Pack small on purpose; every line costs context; a good change is clearer, shorter or safer: `CONTRIBUTING.md::Contributing`. Test not found (statement of intent).
- Public repository at github.com/vaiev/wouf: `SECURITY.md::Reporting a vulnerability`. Test not found.

### Ways to help

- Real failures, example bots or connectors, fixes for contradictions and outdated runner commands: `CONTRIBUTING.md::What helps most`. Test not found (invitation).
- Security issues privately, not in a public issue: `SECURITY.md::Reporting a vulnerability`. Test: `tests/test_pack.py::OpenSourceFilesTest.test_project_files_exist` (SECURITY.md names private vulnerability reporting).

### Rules for changes

- Test first: `CONTRIBUTING.md::Rules for changes`, `AGENTS.md::Workflow`. Test not found (procedural rule, not enforced).
- A new skill copies the template, keeps its six sections in order and is named in the lead role: `CONTRIBUTING.md::Rules for changes`. Tests: `tests/test_pack.py::PackStructureTest.test_every_skill_has_the_required_sections` (every skill has the sections in order), `tests/test_pack.py::PackStructureTest.test_the_lead_role_names_every_skill` (the lead role names every skill). Preferring to extend a skill: test not found (guidance).
- Stay generic: `CONTRIBUTING.md::Rules for changes`. Test: `tests/test_pack.py::LeakScanTest.test_no_secrets_or_private_addresses` covers secret markers and private IP addresses only; company, host and account names: test not found.
- Route by tier; provider and model names in `config.ini`: `CONTRIBUTING.md::Rules for changes`. Test: `tests/test_pack.py::PackStructureTest.test_model_routing_uses_tiers_not_versions` (no pinned model versions in Markdown outside the history records).
- Write plainly: `CONTRIBUTING.md::Rules for changes`. Test not found (style rule).

### Checks

- The test command: `CONTRIBUTING.md::Checks`. The container tests run only where an engine is reachable: `CONTRIBUTING.md::Checks`; skip condition `tests/test_team_cli_export.py::ContainerRunTest`.
- CI runs markdownlint, an offline link check and actionlint: `.github/workflows/docs-ci.yml::jobs.docs`. Test not found (CI configuration).

### Review

- Branch, pull request against `main`, merge after review, describe the change and the test: `CONTRIBUTING.md::Pull requests`, `AGENTS.md::Workflow`. Test not found (procedural rule, not enforced by the pack).
- The project's changes follow the method: approval of the exact commit, green CI and the owner's yes: `.agents/rules.md::8. Quality`, `README.md::Built with WOUF`. Test: `tests/test_pack.py::LeanPackTest.test_the_hard_limits_are_unchanged` pins the rule text only; enforcement test not found (instruction).
- Helping by reviewing pull requests: the guide's invitation, no repository claim; the approach mirrors `.agents/roles/review.md::Role: Review` and `.agents/rules.md::4. Speak up`. Test not found (role and rule text).

### Security issues

- Private vulnerability reporting, the organisation owners until it is enabled, no secrets in reports, revoke a pasted secret first: `SECURITY.md::Reporting a vulnerability`. Test: `tests/test_pack.py::OpenSourceFilesTest.test_project_files_exist` (private vulnerability reporting named); the rest: test not found (policy text).

### License

- Contributions under Apache License 2.0: `CONTRIBUTING.md::License`, `LICENSE`. Test: `tests/test_pack.py::OpenSourceFilesTest.test_license_is_apache_2`.

## Changelog

- Introduction: commands, sandboxing and checks are implemented and tested, with skips by environment (see the entries below and Limits, What the tests cover); the pack's rules, roles and skills are instructions, their text and structure pinned by `tests/test_pack.py::PackStructureTest.test_every_skill_has_the_required_sections`, `tests/test_pack.py::PackStructureTest.test_the_lead_role_names_every_skill`, `tests/test_pack.py::LeanPackTest.test_the_hard_limits_are_unchanged`, with no test that a model follows them: `.agents/rules.md::Rules` (sections 1 to 8). Enforcement test not found (instructions).

### Added

- Pack and setup: see Overview and Getting started.
- `team run` modes and container hardening: see Safety, Sandbox modes.
- `mode = bwrap`, prefix, lifecycle and gate in the sandbox: `.agents/bin/teamlib/bwrap.py::module docstring`, `.agents/bin/teamlib/bwrap.py::gate_run`; limits through a systemd user scope: see Safety, Sandbox modes. Tests: `tests/test_bwrap_run.py::BwrapLifecycleTest.test_create_makes_a_private_box`, `tests/test_bwrap_prefix.py::PrefixRecipeTest.test_a_bot_sandbox_gets_the_prefix_read_only_and_first_on_path`, `tests/test_bwrap_gate.py::GateSetup.test_bridge_is_refused`; the real sandbox runs are skipped without bubblewrap.
- `network = egress`: see Safety, Network and egress.
- `team flow`, classes, tiers, runners, fallback: see Method and Getting started; the gate by mode and the accepted commit prefix: see Method, The brief and Fresh, independent review.
- Parallel steps: `.agents/team/config.ini::[flow]`. Tests: `tests/test_flow_parallel.py::RunFlowListTest.test_two_reviews_run_at_once_and_both_approve`, `tests/test_flow_parallel.py::RunFlowListTest.test_scout_and_research_run_at_once_then_the_worker`.
- `team batch`: the packages of one plan wave at the same time, each on its own worker, when their paths are pairwise disjoint (`.agents/bin/teamlib/batch.py::module docstring`); an overlap starts nothing (`.agents/bin/teamlib/batch.py::run_batch`). Tests: `tests/test_batch.py::OverlapTest.test_disjoint` (disjoint paths), `tests/test_batch.py::BatchCliTest.test_an_overlap_refuses_the_batch_and_names_both_packages` (an overlap refuses the batch, no flow starts), `tests/test_batch.py::BotOverrideTest.test_worker2_and_scout2_run_the_flow` (`worker2` and `scout2` run the second flow).
- `team goal` and the classifier: `.agents/skills/brief.md::Sequence of work`, `.agents/team/config.ini::[classify]`. Tests: `tests/test_goal.py::ClassifierTest.test_twenty_fixture_goals_at_least_eighteen_match`, `tests/test_goal.py::ClassifierTest.test_a_security_path_brings_a_threat_model_skeleton`.
- Secrets and masking: see Safety, Keys.
- Event log and SQLite projection: `.agents/rules.md::7. State lives in the event log; the store is its cache, board and decisions are its readable export`. Tests: `tests/test_events.py::Events.test_origin_creation_validation_and_sequence`, `tests/test_projection.py::Projection.test_rebuild_equals_incremental_500_events`.
- Budget and a bot's end date: `.agents/team/config.ini::[budget]`. Tests: `tests/test_budget.py::BudgetRunTest.test_at_per_day_the_run_is_skipped`, `tests/test_bot_until.py::BotUntilTest.test_past_refused_before_staging`.
- Context graph: `.agents/team/config.ini::[flow]`. Tests: `tests/test_graph.py::GraphTest.test_fixture_and_determinism`, `tests/test_login.py::Login.test_graph_only_with_repo_and_paths`.
- Federation, signed envelopes: `docs/FEDERATION.md::Federation between two WOUF leads`, `.agents/bin/federate::sign`, `.agents/bin/federate::signed_by`, `.agents/bin/federate::cmd_pack`. Tests: `tests/test_federate.py::FederateTest.test_pack_prints_one_signed_envelope_line` (one signed envelope line), `tests/test_federate.py::FederateTest.test_verify_accepts_good_lines_and_names_each_refusal` (verify accepts good lines and names each refusal), `tests/test_federate_transport.py::TransportTest.test_a_forged_signature_is_refused_and_nothing_is_held` (a forged signature is refused and nothing is held).
- Federation, git mailbox: `docs/FEDERATION.md::Federation between two WOUF leads`, `.agents/bin/federate::cmd_send`, `.agents/bin/federate::cmd_pull`. Tests: `tests/test_federate_transport.py::TransportTest.test_send_puts_one_envelope_per_commit_and_only_moves_forward` (send puts one envelope per commit on the mailbox), `tests/test_federate_transport.py::TransportTest.test_round_trip_holds_and_applies_only_by_hand` (a pull carries it back).
- Federation, incoming data kept in quarantine until applied by hand: `docs/FEDERATION.md::Federation between two WOUF leads`, `.agents/bin/federate::hold` (`hold` stores a checked envelope under `import/federation/<peer>/`), `.agents/bin/federate::cmd_receive` (`receive` without `--apply` writes nothing into the team). Tests: `tests/test_federate.py::FederateTest.test_receive_without_apply_only_holds` (receive without `--apply` only holds: the envelope is in quarantine, nothing in the team's knowledge), `tests/test_federate_transport.py::TransportTest.test_round_trip_holds_and_applies_only_by_hand` (a pull holds the envelope, writes nothing into the team, and only `receive --apply` does). Both test files skip without ssh-keygen (`tests/test_federate.py::FederateTest`, `tests/test_federate_transport.py::TransportTest`; the second also needs git).
- `team demo`: `README.md::WOUF`. Test: `tests/test_demo.py::DemoTest.test_cli_json_repeat_cleanup_and_text`.

### Changed

- Dropped SSH connection keeps the result, a host that stays away ends the run naming the lost connection: `.agents/bin/teamlib/engine.py::SSH_ALIVE` (keepalive options), `.agents/bin/teamlib/engine.py::rejoin`, called in `.agents/bin/teamlib/run.py::attach`. Tests: `tests/test_engine_ssh_loss.py::SshLossTest.test_a_stream_that_drops_once_still_brings_the_return_block_and_the_results`, `tests/test_engine_ssh_loss.py::SshLossTest.test_a_host_that_stays_away_ends_the_run_naming_the_lost_connection`.
- At most three round trips: `.agents/bin/teamlib/engine.py::START` (one shell starts the bot, inputs and keys in one call), `.agents/bin/teamlib/engine.py::box_command`, `.agents/bin/teamlib/engine.py::fetch_results` (collect and remove in one call). Test: `tests/test_engine_calls.py::EngineCallsTest.test_a_warm_container_run_takes_at_most_three_round_trips`.
- Loopback around the proxy, fixed in code: `.agents/bin/teamlib/engine.py::EGRESS_ENV`. Tests: `tests/test_egress_run.py::LoopbackTest.test_the_bot_gets_no_proxy_for_its_loopback_only_next_to_the_four_proxy_variables`, `tests/test_egress_run.py::LoopbackTest.test_the_loopback_exception_is_fixed_in_code_and_does_not_widen`.
- Own runner's model hosts only: `.agents/bin/teamlib/flow.py::prepare_egress`. Tests: `tests/test_egress_run.py::RunnerNeedsTest.test_a_claude_run_and_a_codex_run_on_one_config_each_get_their_own_hosts`, `tests/test_egress_run.py::RunnerNeedsTest.test_needs_of_a_runner_replace_its_built_in_list`.
- Locked secret store ends fast with one line: `.agents/bin/secret::PASS_TIMEOUT` (pass gets five seconds), `.agents/bin/teamlib/core.py::read_once` (records the fault), `.agents/bin/teamlib/core.py::register_stored` (says the one line). Tests: `tests/test_secret_locked.py::LockedPassTest.test_a_command_that_prints_team_data_fails_closed`, `tests/test_secret_locked.py::LockedPassTest.test_secret_run_on_a_locked_store_is_one_line`, `tests/test_secret_locked.py::LockedPassTest.test_log_on_a_locked_store_writes_nothing_and_fails_closed`.
- Demo brings its own config: `.agents/bin/teamlib/demo.py::TEAM_CONFIG`, `.agents/bin/teamlib/demo.py::exercise`. Tests: `tests/test_demo.py::DemoTest.test_disposable_team_gets_only_its_own_minimal_config`.

### Not in this version

- Key proxy for egress, so that keys no longer reach the bot: `.agents/team/config.ini::[egress]`, `docs/DESIGN.md::2. Two places, one boundary`. Plan.

## Claims left out

Claims from the owner's status note that this audit could not back with code and tests in this tree were left out: speed figures for queries, comparisons with other products, cost or rate-limit outcomes, and statements about other projects.
