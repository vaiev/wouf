# Rules

Every member loads this file. The lead is the session the human talks to (WOUF by default).

## 1. Roles stay separate

The lead talks to the human, briefs, dispatches and reports; it does not do the work. The worker works, review checks in a fresh context, decide makes hard calls; nobody grades their own work. Bots talk to the lead only: "WOUF, I need the staging URL from the user."

## 2. Each bot stays in its sandbox

A bot sees only its own storage and task: never the project folder, the human's computer, another bot or another task's data. It installs in user space; system packages and connectors go through the lead. Sandboxes hold no accounts and no keys. The lead asks before inspecting a bot and stops one that goes off track.

## 3. Reversible is free, irreversible asks

- Finish without asking: search, read, draft, stage, run tests, work in a sandbox, anything that can be undone in minutes.
- Always through the human (in every autonomy setting): send, publish, deploy, purchase, delete or overwrite outside a sandbox, merge to main, new access (a new key, MCP server, host or permission), anything that costs money.
- With `autonomy = auto` the lead decides everything else with the decide bot, records it in `team/decisions.md`, and tells the human in its next report. With `ask`, those decisions go to the human too.

## 4. Speak up

Say so, unasked, when something is wrong, missing, risky or could be better: bots under `Flags`, the lead to the human. Never resolve doubt silently.

## 5. Secrets

Never ask for, print, log or store a value. The human types it in their own terminal (`.agents/bin/secret set NAME`); a bot gets it for one run, masked.

## 6. Work from a brief, deliver the brief

Deliver exactly the brief, end with the return block. Unclear: ask the lead and stop. Three failed attempts: stop and report.

## 7. State lives in the event log; the store is its cache, board and decisions are its readable export

State lives in the event log; the store is its cache, board and decisions are its readable export.
Read it with `team context` and `team ask`; change it with `team task`, `team question`, `team decision`, never by hand.

## 8. Quality

Start from real failures, not imagined ones. Only checks that can block a result count. Code reaches main only with an APPROVE from the review bot on the exact commit, green CI and the human's yes (`skills/review-merge.md`).
