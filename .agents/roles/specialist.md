# Role: Specialist (template for new bots)

Copy this when the team needs a bot for one domain that comes up again and again: research, writing, data, design, operations, a language, a customer. WOUF proposes it; the human decides (`skills/team.md`).

To add one, write `team/bots/<id>.md`:

```markdown
---
id: research
name: Research
role: specialist
status: proposed        # proposed → active (after the human's yes) → retired
runner: auto            # or claude, codex, agy, local
tier: strong            # or light for mechanical, frequent work
commits: yes            # no: reads a repository but its commits never come back (like review, scout, planner)
---

# Research

Owns: <one sentence that will still be true next month>
Inputs: <what it gets>
Done when: <checkable>
Tools: <skills or MCP servers from team/tools.md it may use>
```

Then `.agents/bin/team bot create <id>`.

## What every specialist follows

- Its domain only; its notes stay in its own declaration and sandbox.
- The worker's rules for scope, questions and secrets (`roles/worker.md`).
- Return block: `WOUF <Name> · <task> · ready-for-review | blocked`, with Evidence, Artifacts, Questions and Flags.
