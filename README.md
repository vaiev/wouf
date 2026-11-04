# WOUF

**An agent team in a folder, set up with the AI tool you already use.**

WOUF shows how to work with AI agents the way a small company works with a team: one lead you talk to, who hands tasks to a worker, has a reviewer check the result, asks a decider for the hard calls, and tells you what happened. The bots run in their own sandboxes on your computer, a VM or a server, so they cannot touch your files or each other.

It is not a product or a platform. It is a folder of plain instructions plus small standard-library Python helpers, which you copy into a project and use with Claude Code, Codex CLI, Gemini CLI, Antigravity, Grok Build or any agent CLI that reads `AGENTS.md`.

## Status

The first public release, **v0.1.0**, is being prepared. The pack, its helpers and their tests land in this repository with that release, under the Apache License 2.0.

## How it works

- **You talk to the lead**, in your project folder, with the CLI you already use.
- **The lead does not do the work.** It writes a short brief; a worker runs as a headless agent CLI in its own sandbox, and its code comes back as a git branch.
- **Every result is reviewed** by a second bot in a fresh context. Nothing reaches your main branch without that review, green CI and your yes.
- **Anything irreversible, outward, paid or needing new access waits for you.**

## It builds itself

WOUF is developed by its own agent team: a lead, workers on Claude and Codex, and reviewers, each change reviewed before it lands.

## About

WOUF is initialized by [VAIEV](https://vaiev.com). Its sibling project is [Vaikor](https://github.com/vaiev/vaikor), an open, security-first Personal Edge Intelligence OS and Runtime.
