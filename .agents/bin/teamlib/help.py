"""teamlib.help: what the lead reads instead of a whole file. One skill's "Sequence of work" (team skill), and a brief
skeleton with the next free task id from the state cache (team brief new) (T-0019-29)."""

from __future__ import annotations

import importlib
import re

from teamlib.core import CLASSES, KNOWN, PACK, die, mask_secrets, say, visible

USAGE = """
  team skill NAME                     only the "Sequence of work" of skills/NAME.md, instead of the whole file
  team brief new --class C "objective"
                                      a brief skeleton (skills/brief.md) with the next free task id from the state
                                      cache and the lead's drafts; printed only, every open line says TO FILL
"""

STEPS = "## Sequence of work"
SKILL_NAME = re.compile(r"[a-z][a-z0-9-]{0,40}")
# bin/project check refuses a brief while a line says this.
TO_FILL = "TO FILL"
OBJECTIVE_CAP = 500
SECTIONS = (
    ("Acceptance criteria", "checkable, one per line"),
    ("Scope", "what to change or produce"),
    ("Out of scope", "what not to touch"),
    ("Inputs", "files (--input), the repository (--repo), knowledge topics, decisions"),
    ("Constraints", "rules, style, limits, deadlines"),
    ("Expected output", "files in out/, commits, the return block"),
)


def skills() -> list[str]:
    return sorted(p.stem for p in (PACK / "skills").glob("*.md") if not p.name.startswith("_"))


def skill_steps(name: str) -> str:
    """The "Sequence of work" section of skills/<name>.md, its heading included, up to the next section."""
    known = skills()
    if not SKILL_NAME.fullmatch(name) or name not in known:
        die(f"no skill {visible(name)!r}; one of: {', '.join(known)}")
    text = (PACK / "skills" / f"{name}.md").read_text(encoding="utf-8")
    if STEPS not in text:
        die(f"skills/{name}.md has no {STEPS!r} section")
    start = text.index(STEPS)
    end = text.find("\n## ", start + 1)
    return (text[start:] if end < 0 else text[start:end]).rstrip("\n")


def do_skill(args: list[str]) -> int:
    """team skill NAME. Not `team help NAME`: `help` stays the built-in of bin/team."""
    if len(args) != 1:
        die(f"usage: team skill NAME (one of: {', '.join(skills())})")
    say(skill_steps(args[0]))
    return 0


def next_task() -> str:
    # goal and state are command leaves: looked up when used (state plugs itself into run.STORE).
    importlib.import_module("teamlib.state")
    return importlib.import_module("teamlib.goal").next_task()


def skeleton(task: str, klass: str, objective: str) -> str:
    lines = [f"Task: {task}", f"Class: {klass}"]
    if klass != "plan":
        lines.append(f"Paths: {TO_FILL}: files and globs the work may change; the pre-gate refuses anything else")
    lines.append(f"Objective: {objective}")
    lines += [f"{name}: {TO_FILL}: {hint}" for name, hint in SECTIONS]
    if klass == "security":
        lines.append(f"Threat model: {TO_FILL}: what attacker or accident is in scope, what is out of scope and why")
    return "\n".join(lines)


def do_brief(args: list[str]) -> int:
    usage = 'usage: team brief new --class C "objective"'
    if not args or args[0] != "new":
        die(usage)
    state = importlib.import_module("teamlib.state")
    positional, opts = state.parse(args[1:], usage, single=("--class",))
    if len(positional) != 1 or "--class" not in opts:
        die(usage)
    klass = opts["--class"]
    if klass not in CLASSES:
        die(f"class {visible(klass)!r} is not one of {', '.join(CLASSES)}")
    objective = positional[0].strip()
    if not objective or "\n" in objective or "\r" in objective:
        die("the objective is one sentence on one line")
    if len(objective) > OBJECTIVE_CAP:
        die(f"the objective is longer than {OBJECTIVE_CAP} characters")
    say(skeleton(next_task(), klass, visible(mask_secrets(objective, KNOWN))))
    return 0


COMMANDS = {"brief": do_brief, "skill": do_skill}
