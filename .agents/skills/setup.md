# Skill: Set up the bot environment

## When to use

On the first session in a folder (`mode = undeclared` in `team/config.ini`), when the human wants to change where bots run or which agent CLI they use, and when `.agents/bin/team doctor` reports a problem. Run it before any other work.

## Required inputs and access

- `team/config.ini` and `team/bots/*.md`
- What is installed: `command -v docker podman colima orbstack multipass limactl claude codex agy`
- The human, for one round of questions

Approval: the human confirms the plan. Installing software on the human's computer (a container engine) needs their yes; you then do it for them, or walk them through it step by step if it needs a password or an installer window. Anything paid or remote (a VPS) is prepared for the human, never created by you.

## Sequence of work

1. Detect what you can. On Windows, run the lead in a WSL 2 terminal first (detached runs, locks and stopping a host run need a POSIX system). Do not read SSH keys or configs; an SSH alias name the human gives you is enough.
2. Notice who you talk to. Someone who is new to this gets plain words, your recommendation as one yes/no question, and you doing the steps. Someone technical gets the options.
3. Ask once, recommendation first:
   - **Where bots run.** Recommended: `container`. The team shares one image with the agent CLIs; each bot has its own storage; a container runs only while a bot works. It needs a container engine:
     - already installed (Docker, Podman, OrbStack, Colima): use it;
     - none on macOS: OrbStack or Colima (light) or Docker Desktop;
     - none on Linux: Docker Engine or Podman from the distribution;
     - none on Linux, and the human cannot or will not install one: `bwrap` (bubblewrap, a small package of the distribution; it needs unprivileged user namespaces). Each bot still runs in its own sandbox with its own storage and sees nothing of the human's home, but say plainly what it does not do: no syscall filter; CPU, memory and process limits only when a systemd user scope works; nothing installed as root (an install line that needs root is skipped and named, and the human installs that tool themselves). It refuses `network = bridge`: a cloud CLI needs `network = egress` with `[egress] proxy` (`egress_proxy.py` of the catalog's egress-proxy tool, copied into this folder) and the runner's hosts (`needs.<runner>`; Claude Code and Codex have built-in ones); the team prefix gets the CLI through that proxy too. `none` only for a local model.
     - none on Windows: run in WSL 2 and use Docker Desktop or Podman Desktop (with WSL 2 integration) or Docker/Podman inside WSL 2;
     - a separate machine: Docker on a small VM or a VPS, reached as `docker_host = ssh://<alias>`.
     Only to try things out, without any install: `host` (no sandbox; say so plainly).
   - **Which agent CLI and models.** Default: the CLI this session runs in (`strong` = its top model, `light` = its small model; `.agents/bin/team runners` shows what is configured). `auto` detects Claude Code from the session and otherwise picks the first installed runner, so choose the explicit runner name; fill in `strong`, `mid` and `light` when the runner's model fields are blank (or keep empty for the CLI's default), and note when a runner's MCP fields (`mcp_arg` / `mcp_path`) are empty (connectors are not wired until configured). Another CLI, or a local model, if the human prefers.
   - **Keys.** Each bot needs a key for its CLI (Claude Code: `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`, or `ANTHROPIC_API_KEY`; Codex: `OPENAI_API_KEY`, or a ChatGPT subscription with `auth = login`, `login = codex login --device-auth` and `login_check = test -s "$HOME/.codex/auth.json"` in `[runner.codex]`; Antigravity CLI: no key, `auth = login`). For `auth = login` (in `container` or `bwrap` mode), the human signs each active bot in once in their own terminal with `.agents/bin/team bot login <id>` (in `host` mode, sign in with the CLI itself). For API keys, the human stores the value in their own terminal, never in chat (`skills/secrets.md`), into their per-user secret store (`~/.config/wouf/secrets` or the system keyring, outside this folder): give them one line to paste into a second terminal window, with this folder's full path, `cd '<folder>' && .agents/bin/secret set NAME`; the value is typed hidden.
   - **Name, autonomy and test gate.** Keep the name WOUF or choose another; `ask` (default) or `auto`; and ask for the project's test command for `[flow] gate` (for example `pytest` or `npm test`; if left empty, say plainly: `Paths only; no tests configured`).
4. Write the answers into `team/config.ini` (`mode`, `engine`, `docker_host`, `network` and `[egress]` for `bwrap`, `name`, `autonomy`, `[flow] gate`, `[runner.<cli>]` models/login settings, and `[team] runner` with the CLI's explicit name: the team image installs that CLI, so containers and `bwrap` need a name, not `auto`).
   Signing key, with the human's yes (a new key): receipts (`skills/review-merge.md`) and federation use one Ed25519 key outside this folder. The human makes it, `ssh-keygen -t ed25519 -f ~/.ssh/wouf-a2a`, and you set `[federation] name` (the team's lowercase name) and `signing_key = ~/.ssh/wouf-a2a`; `docs/FEDERATION.md` (Setup) and `docs/RECEIPTS.md` (Signature and trust) in the WOUF repository. Without it `team receipt make` refuses.
5. If this folder is not a git repository or has no commit yet, offer `git init` and an initial commit (local; required before `team flow`, `team goal` or any run with `--repo`). Then `.agents/bin/team bot create <id>` for each active bot (`worker`, `review`, `decide`, `bugfixer`, `scout`, `planner`). The first one builds the team image (with `bwrap`: the team prefix), which takes a few minutes once: run it in the background or with the longest time limit your CLI allows.
6. `.agents/bin/team doctor` until it says "all good" (doctor checks local prerequisites, sandbox and stored key names or login files; only the smoke run below verifies a live model reply). Then run the smoke test: write `.agents/team/env/lead/T-smoke.md` with `Objective: Reply with the word ready and your return block.`, run `.agents/bin/team run worker T-smoke --brief .agents/team/env/lead/T-smoke.md --tier light --detach`, then poll `.agents/bin/team result worker T-smoke --wait 100` (exit `0`: check the signed return block `WOUF Worker · T-smoke ·`; exit `3`: still working, run `team result` again to keep waiting rather than treating 100 seconds as a failure; exit `4`: usage limit until the reset time shown, wait or switch runner). Three failed attempts: stop and show the human the error in plain words.
7. For Claude Code, with the human's yes, merge the `SessionStart` and `SessionEnd` hooks from `INSTALL.md` into this folder's `.claude/settings.json`, once each; keep existing hooks and the guard's `PreToolUse` entry in place. Other CLIs follow `START.md` without hooks.
8. When the human switches to another CLI later, add its adapter in this folder only: `CLAUDE.md` or `GEMINI.md` with the line `@AGENTS.md`.

## How to validate the result

- `team doctor` says "all good"; the smoke test returned a signed return block.
- No addresses, keys or passwords in `team/config.ini`.

## What to return

- Five plain lines to the human: where bots run, which CLI and models (and whether connectors are wired), which keys or logins are stored, name, autonomy and test gate (`[flow] gate`, or `Paths only; no tests configured`), and what is still missing
- One log line: `bin/log add lead setup done "<mode>, <runner>"`

## What requires approval

The plan itself, installing software on the human's computer, changing the agent CLI or models the human chose, and anything that costs money.
