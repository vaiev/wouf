# Skill: Handle secrets

## When to use

Whenever a bot or a tool needs a key, token or password: the agent CLI's key at setup, a service a task uses, an MCP server.

## Required inputs and access

- `team/secrets.md` (names, scope, who uses them), `.agents/bin/secret` on the computer where the key is used
- The human, the only one who types a value

Approval: a new secret, a wider scope, or giving a secret to another bot needs the human's yes.

## Sequence of work

1. Check: `.agents/bin/secret has NAME` (exit 0 = stored; no value shown).
2. Missing: one message to the human: what it is for, the smallest scope that works, where to create it, and the command for their own terminal: `.agents/bin/secret set NAME` (hidden input; refuses without a terminal). Never take a value in chat; if the human starts pasting one, stop them and ask them to revoke it.
3. Record the name in `team/secrets.md`: purpose, used by, scope, rotate by.
4. Use: bots get the CLI's key for one run (`auth` in `team/config.ini`); other commands: `.agents/bin/secret run NAME -- <command>`, the value masked as `***`. Names in `auth` or a brief, never values.
5. Leak: stop, the human revokes and re-creates it, store it again, log the incident without the value.

## How to validate the result

- No value in the repository, store, logs, knowledge, briefs, chat, a bot's storage or the image. A key exists in a sandbox only as the environment of the run it was passed to; a subscription login (`auth = login`) stays in that bot's own storage by design.
- Every stored name has a row in `team/secrets.md`.

## What to return

- The task continues, or one line to the human with the `secret set` command; one log line, names only

## What requires approval

New secrets, wider scopes, sharing with another bot, and rotations that could break a running routine.

## Limits

`bin/secret` keeps values out of the repository, the chat and the model's context; a process of the same OS user could still read its store. In `container` and `bwrap` mode a bot never sees the store, only the keys passed to its run. In `host` mode there is no sandbox: the bot runs as you, with your environment and every stored value (`WOUF_MASK`), so it can read all keys.
