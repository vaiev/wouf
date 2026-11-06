# Companion owner actions: the interface

Status: v1 (2026-10-29). Code: `companion.py` (`commands_for`, `do_POST`). Tests: `test_actions.py`.

With `--actions`, the local cockpit lets the owner answer questions, approve or reject decisions and mark merge
proposals. Each action is one or more `team` commands from a fixed list, run with an argument list (no shell), and
the page shows the exact commands before it runs them. Without `--actions` the cockpit stays read-only: a POST
gets 501.

```sh
python3 companion.py --team path/to/team --actions
# companion with owner actions on http://127.0.0.1:8765/#token=… (open this link; Ctrl-C stops)
```

## Actions

| Path | Body (JSON) | Commands, in order |
|---|---|---|
| `/action/answer` | `{"answers": [{"id": "Q-n", "text": "…"}, …], "confirm": bool}` (1 to 20 answers) | `team question answer Q-n "text"`, one per answer |
| `/action/decision` | `{"id": "D-n", "verdict": "approve"\|"reject", "note": "…"?, "confirm": bool}` | `team decision approve\|reject D-n [--note "…"]` |
| `/action/merge-proposal` | `{"task": "T-n", "head": "<7–40 hex>", "confirm": bool}` | `team merge mark T-n --head <hex>` |

- **`confirm: false`:** nothing runs. The answer is `{"commands": [{"command": "team …"}]}`, which the page shows
  in its confirmation sheet.
- **`confirm: true`:** the commands run one after another, one action at a time. The answer is
  `{"results": [{"command": "team …", "code": 0, "output": "…"}]}`. The output is the last 2,000 characters of
  stdout and stderr, masked like the rest of the page.
- **Arguments:**
  - **Ids:** `Q-<n>`, `D-<n>`, `T-<n>`; a head is 7 to 40 lowercase hex characters.
  - **Free text** (an answer: up to 500 characters, team's own cap; a note: up to 300) must be printable apart from
    line breaks, must not start with `-`, and must hold no key shape (the pack's `secret_in` and token rule).
  - **Fields:** no other field is accepted, and a key may not appear twice.
  - A bad body gets 400, and nothing runs.
- **Not possible:** there is no action that merges, pushes, deletes or runs anything but these three `team`
  subcommands. Merging stays the owner's step, outside the app.

## Every POST must carry

| Check | Refused with 403 when |
|---|---|
| `Host` | not exactly `127.0.0.1:<port>` or `localhost:<port>` of this server (DNS rebinding) |
| `X-WOUF-Token` | not the launch token (compared in constant time) |
| `Content-Type` | not `application/json` (a form or text post is what another site can send without asking) |
| `Sec-Fetch-Site` | present and not `same-origin` |
| `Origin` | present and not this server's origin; absent when `Sec-Fetch-Site` is absent too |

A body over 16 KiB gets 413. `GET` never changes anything, and `/action/*` exists only as POST. The server binds
127.0.0.1 only.

## The launch token

`companion --actions` makes a new token (`secrets.token_urlsafe(32)`) at each start and prints the link with the
token in the fragment (`#token=…`). A browser sends no fragment in a request line, a `Referer` or a server log.
The page reads it once, keeps it in memory and removes it from the address bar (`history.replaceState`). Without
the token, the page still shows what waits for the owner, but no action passes.

## What the page shows

Under "For the owner":

- **Questions:** open questions from `team/events`. A `question.asked` counts until its `question.answered`.
  Each has a text field, and one button answers all the filled ones.
- **Decisions:** pending decisions (`decision.recorded` with status `pending`), each with an optional note and
  Approve or Reject.
- **Merge proposals:** the latest `merge.proposed` per task, and whether a `merge.marked` for the same head
  followed.

Every button first asks the server for its commands (`confirm: false`) and shows them in a sheet. Only "Run"
sends `confirm: true`. The sheet keeps the owner panel from redrawing while it is open, and drafts survive the
live updates.

## Threat model

- **In scope:**
  - a web page in the owner's browser that sends requests to the port (CSRF, DNS rebinding), stopped by the
    token, the origin checks and the content type;
  - another local user reaching the port without the token;
  - argument injection, stopped by fixed argv shapes, checked ids and no leading `-` in text;
  - script injection through question or decision text, which the page renders through `textContent` only, under
    a hash-only CSP.
- **Out of scope:**
  - malware running as the owner, which can run `team` itself;
  - the owner's own deliberate actions. The app adds no power beyond the CLI.
