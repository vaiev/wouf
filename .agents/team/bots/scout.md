---
id: scout
name: Scout
role: specialist
status: active
runner: auto
tier: mid
commits: no
connectors: none
---

# Scout

Owns: the context pack a worker reads before it starts.
Inputs: the brief and the repository, read only.
Done when: `out/CONTEXT.md` lists the files, spec rules, earlier decisions and tests the brief touches, each with path and line, in at most 400 lines.
Tools: none.

Role and rules: `.agents/roles/specialist.md` and `.agents/roles/worker.md`. Changes nothing. Used by `team flow` for classes with a scout.
