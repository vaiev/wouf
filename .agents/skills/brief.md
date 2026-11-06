# Skill: Write a brief

## When to use

Before any bot gets work: a brief lets a bot start without asking and the review bot check the result. A goal larger than one brief, or with unclear Paths, goes through the four phases below. Not for questions the store or the knowledge answers.

## Required inputs and access

- The human's request, in their words
- Prior decisions and knowledge (`team ask "words"`), files or a repository the work needs

Approval: none; a brief is a draft.

## Sequence of work

1. Restate the objective in one sentence; if you cannot, ask the human one question.
2. One brief: `.agents/bin/team brief new --class C "objective"` prints the skeleton with the next free task id. Save it as `.agents/team/env/lead/<task>.md` and fill every `TO FILL`: Paths (the pre-gate refuses anything else), criteria, scope, out of scope, inputs, constraints, expected output. Class `security` names a threat model; review blocks only on findings inside it. `bin/project` says what is still open (`check <file>`).
3. Record it: `team task new "title" --class C`; then dispatch (`skills/dispatch.md`).
4. A goal larger than one brief goes through four phases, each recorded in the store (`team goal status T`: phase and state, then each package's task and flow state apart); leaving a phase needs `--yes`, and with `autonomy = ask` the human's yes first. INIT is `active` from `goal new` until CONCEPT starts. CONCEPT and REALIZE are `active` while their command runs, `ready` when it succeeded, `blocked` until resumed, and are left only from `ready`. VERIFY is `active` from `goal verify` (which only inspects) until `goal complete` marks it `done`. A phase that was left after success is `done`:
   - **INIT**: `team goal new "goal" [--repo PATH]` proposes class, Paths, threat model and gate checks (no model) and writes the plan brief. Fill its `TO FILL` lines; CONCEPT refuses them.
   - **CONCEPT**: `team goal concept T --yes` runs `team flow` on the brief as `Class: plan` (scout and planner, nothing dispatched): `PLAN.md` and draft briefs with waves. Take the human's questions to the human; continue with `--answer "…"`, `--retry` or `--status`.
   - **REALIZE**: `team goal realize T --wave N --yes` runs `team batch` on one wave in the goal's repository (`skills/dispatch.md`). A wave `blocked`: bring its flows to `ready` (`team flow`), then `team goal recover T --yes`.
   - **VERIFY**: `team goal verify T --yes` lists per package the review verdict `APPROVE <sha>`, approvals on the flow head, head mismatches and the merge proposal: an inspection, not a pass. Then `skills/review-merge.md`; `team goal complete T --yes` ends the goal.
   - `bin/project phase TASK` is not this: it shows one task's evidence (brief, dependencies, gate, approvals) as pass, open or fail.

## How to validate the result

- Every criterion is observable; out of scope is explicit; nothing was invented.
- A `security` brief names its threat model; a `plan` brief names the goal, not the packages.

## What to return

- The brief file and its task in the store; open questions for the human in one batch (`team question add`)

## What requires approval

None for the draft; leaving a phase under `autonomy = ask`. A brief that needs an irreversible step names it as a gate for the human.
