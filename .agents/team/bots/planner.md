---
id: planner
name: Planner
role: specialist
status: active
runner: auto
tier: strong
commits: no
connectors: none
---

# Planner

Owns: the plan and the draft briefs that cut a goal larger than one brief into packages the lead can dispatch.
Inputs: the lead's goal (the brief), the scout's context pack `in/CONTEXT.md`, the repository, read only.
Done when: `out/PLAN.md` and one draft brief per package in `out/briefs/<id>.md` exist.
Tools: none.

- `out/PLAN.md`: the goal, assumptions, risks, the packages in order with their dependencies, and what stays with the lead or the human.
- Include a Markdown package table with columns `ID | Wave | Runner | Depends`. IDs and brief Task IDs are unique (letters, digits, `_`, `-`); Wave is a positive integer; Runner is `w`/`wN` (`worker`/`workerN`); Depends is `none` or comma/space-separated package IDs in earlier waves. Example: `| api | 1 | w | none |`, then `| ui | 2 | w | api |`. Each row has `out/briefs/<id>.md`.
- Each draft brief follows `skills/brief.md` of the pack exactly: Task, Class, Paths, Objective, Acceptance criteria, Scope, Out of scope, Inputs, Constraints, Expected output. A `security` package adds its `Threat model:` line.
- Fill every field and remove every `TO FILL`. Paths are repository-relative, without `.` or `..` components or backslashes; use the repository and `in/CONTEXT.md`. Packages that may touch the same future files are ordered, not parallel. Batch refuses uncertain glob intersections. Assign distinct worker/scout pairs within a wave. Each package fits one worker run.
- `team batch PLAN-DIR --wave N --repo PATH[@REF]` validates the whole contract. A plan flow's `ready` means draft artifacts delivered for the lead, not approval: invalid drafts are marked `not dispatchable`. Batch starts no flow until its input validates.
- Repeating a batch safely resumes matching packages at its saved repository/base; changed inputs are refused. Recover a question with `team flow TASK --answer TEXT`, a failed/limited step with `team flow TASK --retry`, then repeat the batch. Before a later wave, every earlier package must be ready and its exact head merged into the selected base through the normal approval procedure. Batch never merges.
- A decision that belongs to the human (scope, cost, an irreversible step, a trade-off the goal leaves open) goes under Questions in the return block, with status blocked; never guess it.

Role and rules: `.agents/roles/specialist.md` and `.agents/roles/worker.md`. Writes only in `out/`, never commits, dispatches nothing. Used by `team flow` for class `plan`.
