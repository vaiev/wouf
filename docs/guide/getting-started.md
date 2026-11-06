# Getting started

## What you need

- An AI assistant for the terminal, an agent CLI, signed in once.
- Python 3.9 or newer and git.
- For the sandboxes: Docker or Podman, on your computer or on a VM or server. On Linux without either, bubblewrap can stand in, with the trade-offs explained in the safety part.

If something is missing, the setup tells you what and why, and installs it with you after you say yes.

## Set it up

Make a folder for your work, open it in the terminal, start your agent CLI there and paste this sentence:

> Set up the agent team from <https://github.com/vaiev/wouf/blob/main/INSTALL.md> in this folder. I am not technical: explain in plain words and do the steps for me.

The agent follows `INSTALL.md`. It copies `.agents/` into the folder without overwriting anything, adds the adapter lines your CLI needs, asks you one round of questions with a recommendation each, creates the bots and runs `.agents/bin/team doctor` and a smoke test. One step is yours: typing your key into your own terminal, so that no key ends up in a chat.

## `AGENTS.md` and adapters

Most agent CLIs read a file called `AGENTS.md` in the folder they start in. Setup adds one line at its end, or creates the file with it:

```text
This folder runs an agent team. Every agent session reads .agents/START.md first.
```

Some CLIs read their own file instead, so setup adds a small adapter:

- **Claude Code** reads `CLAUDE.md`: setup creates it with the single line `@AGENTS.md`, or adds that line, and lets the helpers run without prompts in `.claude/settings.json`. With your yes it also adds session hooks.
- **Gemini CLI** reads `GEMINI.md`: the same, with the line `@AGENTS.md`.
- **Codex CLI, Antigravity, Grok Build, Cursor and GitHub Copilot** read `AGENTS.md` directly; nothing more to do.

So setup is a few added lines, not a single line for every CLI.

## Runners: which CLI runs the bots

The lead is your own session. The bots run a CLI too, headless, and each CLI is described once as a runner: a `[runner.<name>]` section in `.agents/team/config.ini`. The shipped runners are `claude`, `codex` and `agy`, plus an empty `local` section to fill in for a CLI that talks to a local model server.

A runner says how to install the CLI in the team image, how to start it in a container and on your computer, how to pass the model and the reasoning effort, and which environment variables carry its key or that it signs in with a subscription instead.

You can choose the CLI for the whole team, for one bot or for one run: "WOUF, use Codex for the worker." The commands in the shipped runners follow each CLI's documentation; check them against your versions.

## Tiers: which model

Bots ask for a tier, not a model name:

- `strong`: the provider's top model, the default for real work, review and decisions.
- `mid`: the provider's middle model, for bounded reading work such as the scout and the bugfixer. A runner without a mid model runs it on strong.
- `light`: the provider's small model, for mechanical, frequent work.

Each runner maps the tiers to its own models. An empty entry means the CLI's own default model. A brief can ask for another tier, and `team run --model NAME` picks an exact model for one run. Review, decisions, plans and `complex` or `security` work always stay on `strong`.

## First steps

- Try `.agents/bin/team demo` for an offline demonstration with a fake agent.
- Talk to the lead by name: "WOUF, what can you do?"
- Start a session with "without WOUF" when you want your CLI without the team.
- Settings live in `.agents/team/config.ini`, bots in `.agents/team/bots/`. You can change both by asking the lead.
