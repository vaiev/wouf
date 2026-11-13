# Overview

## What WOUF is

WOUF is an agent team in a folder, set up with the AI tool you already use. You talk to one lead. The lead hands tasks to a worker, has a reviewer check the result, asks a decider for the hard calls and tells you what happened.

WOUF does not replace your agent CLI. It is a folder of plain instructions plus small helper scripts written in standard-library Python. You copy that folder into a project and keep using your own agent CLI, such as Claude Code, Codex CLI or another CLI that reads `AGENTS.md`.

WOUF is only the example name. You can call your team whatever you like.

## A plain folder

Everything lives in one folder, `.agents/`, next to your work:

- the rules every member follows, in `.agents/rules.md`
- the roles: lead, worker, review, decide and specialist, in `.agents/roles/`
- the skills the lead loads for a step, such as writing a brief or reviewing and merging, in `.agents/skills/`
- your own team: settings, bots, board, decisions, schedule, knowledge and logs, in `.agents/team/`
- small helper scripts in `.agents/bin/`, mainly `team` for bots, runs and checks, `log` for run records and search, and `secret` for keys

The board, the decisions, the schedule and the knowledge are files you can open and read. An update replaces the shared pack and leaves `.agents/team/` as it is.

## A team that works by a method

What makes the folder a team is the method, not a new program:

- you talk to one lead, and the lead does not do the work itself
- every piece of work starts from a written brief
- a second bot reviews the result in a fresh context
- hard calls go to a decide bot or to you
- anything irreversible, outward, paid or needing new access waits for your yes

The method part of this guide explains each step.

## Where the safety comes from

The rules are instructions to the models. A model can still get them wrong. Docker, Podman and bubblewrap runs stage inputs and a repository copy into a separate workspace; host mode runs without a sandbox and can access the host under the user's permissions. Results come back only as plain files.

The safety part of this guide explains the sandbox modes and their trade-offs, and the limits part says plainly what WOUF does not do.

## Try it without a model

`.agents/bin/team demo` runs an offline demonstration with a fake agent. It shows the gate that checks a change without any model, and an approval that only counts for the exact commit it names. It needs Python 3.9 or newer and git.

## Status

This guide describes the repository as it is on `main`. Version 0.2.0, dated 2026-11-12, is the latest release. See the changelog for what it contains.
