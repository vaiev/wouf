"""teamlib.goal: a goal in plain words through four phases, each a phase.state event in the store. INIT proposes the
brief with the deterministic classifier (teamlib.classify, no model); CONCEPT runs team flow with class plan; REALIZE
runs team batch per wave of the plan; VERIFY lists each package's review verdict and merge proposal. Leaving a phase
needs the lead's --yes; under autonomy = ask the human says yes first. The goal text is data: masked, capped and never
passed to a shell or on a command line (team flow gets the brief file)."""

from __future__ import annotations

import importlib

from teamlib.core import *  # noqa: F401,F403
from teamlib.flow import class_options, flow_dir
from teamlib.run import STORE, TEAM, check_task, shown

USAGE = """
  team goal new "TEXT" [--repo PATH] [--task T]
                                      INIT: the classifier (no model) proposes Class, Paths, a threat model and the
                                      gate checks; writes the plan brief team/env/lead/<T>.md and prints it
  team goal concept T [--answer TEXT] [--retry] [--detach] [--status] [--yes]
                                      CONCEPT: team flow with class plan on the goal's brief (or continue its flow)
  team goal realize T --wave N [--plan DIR] [--yes]
                                      REALIZE: team batch on wave N of the plan (default: the concept flow's PLAN.md)
  team goal verify T [--yes]          VERIFY: per package the review verdict (APPROVE <sha>) and the merge proposal
  team goal recover T --yes           after recovering every flow in a blocked wave, resume the goal
  team goal complete T --yes          complete VERIFY after inspection (lead approval, not a pass/fail verdict)
  team goal status T                  phase and separate task/flow states
                                      active: executing/inspection; ready: command succeeded; blocked: resume;
                                      done: succeeded and left, or VERIFY explicitly completed.
                                      Leaving or completing needs --yes (human yes first under ask).
"""

PHASES = ("INIT", "CONCEPT", "REALIZE", "VERIFY")
TO_FILL = "TO FILL"
HEADER = re.compile(r"^(Task|Class|Paths|Scout):[ \t]*(.*?)\s*$")
SECTION_STOP = re.compile(r"^(?:#|Gate checks:$|Phase: |(?:Task|Class|Paths|Scout|Research|Egress):)", re.I)
SECTIONS = ("Acceptance criteria", "Scope", "Out of scope", "Inputs", "Constraints", "Expected output")
NAMES = ("Objective", "Depends", "Threat model") + SECTIONS
SECTION = re.compile(r"^(?:#{1,6}[ \t]+(%s)[ \t]*:?[ \t]*$|(%s):[ \t]*(.*?))\s*$" % (("|".join(NAMES),) * 2), re.I)
# Source files the context graph reads for the identifiers a goal names.
SYMBOL_FILES = 5000
SYMBOL_BYTES = 1024 * 1024


# ---------------------------------------------------------------- files and the store
# classify, graph, batch and state are command leaves: looked up when used, as leaves do not import each other
# (state plugs itself into run.STORE).

def leaf(name: str):
    return importlib.import_module(f"teamlib.{name}")


def state():
    return STORE[0]


def goal_file(task: str) -> Path:
    return env_dir() / "goals" / f"{task}.json"


def brief_file(task: str, suffix: str = "") -> Path:
    return env_dir() / "lead" / f"{task}{suffix}.md"


def team_call(args: list[str]) -> int:
    """bin/team with these arguments (never a shell); its output relayed through the sinks."""
    done = subprocess.run([sys.executable, str(TEAM), *args], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL)
    for line in done.stdout.splitlines():
        say(line)
    for line in done.stderr.splitlines():
        say(line, err=True)
    return done.returncode


def phase_rows(task: str) -> list[dict]:
    log = state().store()
    rows = state().snapshot(log, ("phases",))["phases"]
    return [r for r in rows if r.get("task") == task and r.get("phase") in PHASES]


def current_phase(task: str) -> str | None:
    """The furthest active phase of the goal (two events of one second keep their order this way)."""
    rows = phase_rows(task)
    active = [r["phase"] for r in rows if r.get("state") == "active"] or [r["phase"] for r in rows]
    return max(active, key=PHASES.index, default=None)


def write_phase(task: str, events: list[tuple[str, str, str]]) -> None:
    log = state().store()
    with state().locked():
        for phase, status, reason in events:
            log.event_append("phase.state", "lead", task, {"phase": phase, "state": status, "reason": reason})


def files_of(repo: Path) -> list[str]:
    done = git("ls-files", "-z", at=repo, check=False)
    if done.returncode:
        die(f"--repo {repo}: not a git repository")
    return sorted(f for f in done.stdout.split("\0") if f)


def symbols_of(repo: Path, files: list[str], names: list[str]) -> dict:
    """Identifier -> files defining it, for the names the goal mentions: the context graph's definitions."""
    if not names:
        return {}
    texts = {}
    for name in files[:SYMBOL_FILES]:
        path = repo / name
        if leaf("graph").LANGUAGES.get(path.suffix) is None or path.is_symlink() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8") if path.stat().st_size < SYMBOL_BYTES else ""
        except (OSError, UnicodeDecodeError):
            continue
        if any(word in text for word in names):
            texts[name] = text
    definitions, _, _ = leaf("graph")._definitions(texts, sorted(texts), None)
    wanted, found = set(names), {}
    for path, items in definitions.items():
        for name, _, _ in items:
            if name in wanted:
                found.setdefault(name, set()).add(path)
    return {name: sorted(paths) for name, paths in sorted(found.items())}


def rules(ini) -> dict:
    found = leaf("classify").rules_of(ini)
    found["reviews"] = {k: class_options(ini, k)["reviews"] for k in known_classes(ini)}
    found["gate"] = get(ini, "flow", "gate")
    return found


# ---------------------------------------------------------------- INIT

def draft_paths(result: dict) -> str:
    if result["paths"]:
        return ", ".join(result["paths"])
    return "" if result["class"] == "plan" else TO_FILL


def render(task: str, result: dict, repo: Path) -> str:
    objective = " ".join(result["text"].split())
    lines = [f"# {task} goal", "", f"Task: {task}", f"Class: {result['class']}",
             f"Paths: {draft_paths(result)}".rstrip(), f"Objective: {objective}",
             f"Acceptance criteria: {TO_FILL} (<checkable, one per line>)",
             f"Scope: {TO_FILL} (<what to change or produce>)",
             f"Out of scope: {TO_FILL} (<what not to touch>)", f"Inputs: the repository {shown(repo)}",
             f"Constraints: {TO_FILL} (<rules, style, limits>)",
             f"Expected output: {TO_FILL} (<commits, files in out/, the return block>)"]
    if result["threat_model"]:
        threat = re.sub(r"<[^>]+>", f"{TO_FILL} (\\g<0>)", result["threat_model"])
        lines.append(f"Threat model: {threat}")
    lines += ["", "Gate checks:", *(f"- {check}" for check in result["gates"]), "",
              f"Phase: INIT (deterministic classifier, no model: {'; '.join(result['reasons'])})"]
    return "\n".join(lines) + "\n"


def header_fields(text: str) -> dict:
    out: dict = {}
    for line in text.splitlines():
        found = HEADER.match(line)
        if found and found.group(1) not in out:
            name, value = found.group(1), found.group(2)
            out[name] = " ".join(words(value)) if name == "Paths" else value
    return out


def sections(text: str) -> dict:
    """The flow header lines (Task, Class, Paths, Scout: exact key, single line, first match) and each named body
    section's text (the rest of its line and the lines up to the next section or heading)."""
    out: dict = {}
    current = None
    for line in text.splitlines():
        found = SECTION.match(line)
        if found:
            name = next(n for n in NAMES if n.lower() == (found.group(1) or found.group(2)).lower())
            current = name if name not in out else None
            if current:
                out[current] = (found.group(3) or "").strip()
        elif SECTION_STOP.match(line):
            current = None
        elif current and line.strip():
            out[current] = (out[current] + "\n" + line.strip()).strip()
    return {**out, **header_fields(text)}


def brief_problems(text: str) -> list[str]:
    """The rules of .agents/skills/brief.md: Task, Class, Objective and every section; Paths except for a plan;
    a Threat model for security."""
    found = sections(text)
    klass = found.get("Class", "").lower()
    problems = []
    why = task_problem(found.get("Task", ""))
    if why:
        problems.append(f"Task: {why}")
    elif "." in found.get("Task", ""):
        problems.append(f"Task: task {found.get('Task', '')!r}: letters, digits, _ - only")
    classes = known_classes()
    if klass not in classes:
        problems.append(f"the brief needs a Class: line, one of {', '.join(classes)}")
    need = ["Objective", *([] if klass == "plan" else ["Paths"]), *SECTIONS,
            *(["Threat model"] if klass == "security" else [])]
    problems += [f"the brief needs {name} (non-empty)" for name in need if not found.get(name)]
    problems += [f"line {n} still says {TO_FILL}" for n, line in enumerate(text.splitlines(), 1) if TO_FILL in line]
    return problems


def brief_field(text: str, name: str) -> str:
    """The first single-line header "Name: value" of a brief (Task, Class, Paths, Depends) as written."""
    found = re.search(rf"^{re.escape(name)}:[ \t]*(.*?)[ \t\r]*$", text, re.M)
    return found.group(1) if found else ""


# ---------------------------------------------------------------- bin/project: waves and phase evidence

def waves(plan: dict) -> tuple[list[list[str]], list[str]]:
    """Kahn's order in levels; problems name unknown dependencies and cycles."""
    problems = [f"{task} depends on {dep}, which is not in the plan"
                for task, deps in sorted(plan.items()) for dep in deps if dep not in plan]
    if problems:
        return [], problems
    left, done, out = dict(plan), set(), []
    while left:
        wave = sorted(t for t, deps in left.items() if set(deps) <= done)
        if not wave:
            return out, [f"cycle among: {', '.join(sorted(left))}"]
        out.append(wave)
        done.update(wave)
        for task in wave:
            del left[task]
    return out, []


def task_done(task: str) -> tuple[bool, str]:
    flow = read_json(team_dir() / "flows" / task / "state.json")
    if flow:
        return flow.get("status") == "ready", flow.get("status") or "new"
    board = team_dir() / "board.md"
    for line in board.read_text(encoding="utf-8").splitlines() if board.is_file() else []:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells and cells[0] == task and len(cells) > 3:
            return cells[3] == "done", cells[3] or "?"
    return False, "unknown"


def plan_of_briefs(folder: Path) -> dict:
    """The Depends: lines of the briefs in folder as waves, with what could not be read or is twice."""
    plan, source, unread = {}, {}, []
    for path in sorted(folder.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            unread.append(f"cannot read {path.name} (not UTF-8 text)")
            continue
        task = brief_field(text, "Task") or path.stem
        if task in plan:
            unread.append(f"{task} is in two briefs: {source[task]}, {path.name}")
            continue
        plan[task], source[task] = depends_of(text), path.name
    order, problems = waves(plan)
    return {"waves": order, "depends": plan, "problems": unread + problems}


def draft_brief(task: str, klass: str, paths: list[str], objective: str) -> list[str]:
    """bin/project's brief: a plan names the goal and no Paths (the planner returns the packages and their paths);
    a security class gets a Threat model: line to fill."""
    lines = [f"Task: {task}", f"Class: {klass}", *([] if klass == "plan" else [f"Paths: {' '.join(paths) or TO_FILL}"]),
             f"Objective: {objective}", "Depends: none", *(f"{name}: {TO_FILL}" for name in SECTIONS)]
    if klass == "security":
        lines.append(f"Threat model: {TO_FILL}: what an attacker can reach through "
                     f"{', '.join(paths) or 'these paths'}, and what must stay true")
    return lines


def class_for(text: str, paths: list[str], files: list[str]) -> str:
    """teamlib's rules ([classify] of config.ini and its classes' reviews, as team goal new reads them, when it reads)
    applied to a goal and paths found elsewhere."""
    try:
        ini = config() if (team_dir() / "config.ini").is_file() else None
    except configparser.Error:
        ini = None
    classify = leaf("classify")
    return classify.class_of(text, paths, files, rules(ini) if ini is not None else classify.rules_of(None))["class"]


def board_next_task() -> str:
    """The next task id after the highest T-<n> row of board.md (bin/project brief)."""
    board = team_dir() / "board.md"
    taken = ["T-" + "".join(str(int(c)) for c in n)
             for n in re.findall(r"^\| T-(\d+) \|", board.read_text(encoding="utf-8"), re.M)] \
        if board.is_file() else []
    return next_task_id(taken)


def phase_brief(task: str) -> Path | None:
    """The brief bin/project phase reads: the flow's brief.md, else the lead's env/lead/<task>.md."""
    folder = team_dir() / "flows" / task
    return next((p for p in (folder / "brief.md", env_dir() / "lead" / f"{task}.md") if p.is_file()), None)


def depends_of(text: str) -> list[str]:
    return words(brief_field(text, "Depends"))


def concept_evidence(text: str) -> tuple[str, str]:
    waiting = [f"{dep} ({status})" for dep in depends_of(text) for ok, status in [task_done(dep)] if not ok]
    if waiting:
        return "open", f"waits for {', '.join(waiting)}"
    deps = depends_of(text)
    return "pass", f"dependencies done: {', '.join(deps)}" if deps else "no dependencies"


def realize_evidence(klass: str, steps: list[dict], need: int) -> tuple[str, str]:
    runs = [s for s in steps if s.get("kind") in ("worker", "bugfixer")]
    work = [s for s in runs if s.get("outcome") == "done"]
    plans = [s["plan"] for s in work if s.get("plan")]
    stuck = runs[-1] if runs and runs[-1].get("status") in ("blocked", "questions") else None
    if stuck:
        return "fail", f"last run {stuck['status']}: {stuck.get('why') or stuck.get('questions') or 'no reason'}"
    if klass == "plan":
        # A plan commits nothing: its evidence is the PLAN.md and draft briefs the flow recorded.
        return ("pass", f"plan written: {', '.join(plans[-1][:3])}") if plans else ("open", "no plan written yet")
    if not work:
        return "open", "no finished worker run yet"
    return gate_evidence(steps, work, need)


def gate_evidence(steps: list[dict], work: list[dict], need: int) -> tuple[str, str]:
    commit = work[-1].get("head")
    gates = [s for s in steps if s.get("kind") == "gate"]
    gate = gates[-1] if gates else None
    # team flow's rule: with reviews = 0 a finished run ends the flow ready, with or without a head and without a
    # gate; with reviews > 0 a finished run without a head blocks it ("made no commits").
    if need == 0:
        return "pass", (f"finished on {commit[:12]} (no gate for this class)" if commit else
                        "no commits: results in the run's out/ (no gate for this class)")
    if not commit:
        return "fail", f"the {work[-1].get('kind') or 'worker'} made no commits"
    if not gate or gate.get("head") != commit:
        return "open", f"no gate on the head {commit[:12]} yet"
    if gate.get("outcome") != "green":
        return "fail", f"gate red on {commit[:12]}: {gate.get('why') or 'no reason given'}"
    return "pass", f"gate green on {commit[:12]}"


def phase_evidence(text: str, flow: dict) -> list[dict]:
    """INIT, CONCEPT, REALIZE, VERIFY of a task from its brief and flow state, each with its evidence; a phase only
    counts once every phase before it passed."""
    steps = flow.get("steps") or []
    klass = (brief_field(text, "Class") or "standard").lower()
    need = class_options(config(), klass)["reviews"] if klass in known_classes() else 1
    problems = brief_problems(text)
    out = [("fail", "; ".join(problems)) if problems else
           ("pass", f"brief complete: Class {klass}, Paths {brief_field(text, 'Paths') or 'none (a plan)'}"),
           concept_evidence(text), realize_evidence(klass, steps, need)]
    out.append(verify_evidence(steps, need, flow.get("status") or "new"))
    result, blocked = [], False
    for phase, (mark, why) in zip(PHASES, out):
        mark = "open" if blocked else mark
        result.append({"phase": phase, "state": mark, "evidence": why})
        blocked = blocked or mark != "pass"
    return result


def verify_evidence(steps: list[dict], need: int, status: str) -> tuple[str, str]:
    """The approvals after the last finished run against the reviews the class needs."""
    work = [s for s in steps if s.get("kind") in ("worker", "bugfixer") and s.get("outcome") == "done"]
    after = max((i for i, s in enumerate(steps) if s in work), default=-1)
    approvals = sum(1 for s in steps[after + 1:] if s.get("kind") == "review" and s.get("verdict") == "approve")
    return ("pass" if approvals >= need and status == "ready" else "open",
            f"{approvals} of {need} approvals on the head; flow {status}")


def next_task() -> str:
    log = state().store()
    snap = state().snapshot(log, ("phases",))
    known = [t["id"] for t in snap["tasks"]] + [r.get("task") or "" for r in snap["phases"]]
    known += [p.stem for p in (env_dir() / "lead").glob("*.md")]
    return next_task_id(known)


def goal_new(ini, args: list[str]) -> int:
    with state().locked():
        return goal_new_locked(ini, args)


def goal_new_locked(ini, args: list[str]) -> int:
    positional, opts = state().parse(args, usage_of(), single=("--repo", "--task"))
    if len(positional) != 1:
        die(usage_of())
    repo = Path(opts.get("--repo") or PROJECT).resolve()
    if not repo.is_dir():
        die(f"--repo {repo}: no such folder")
    task = opts.get("--task") or next_task()
    check_task(task)
    snap = state().snapshot(state().store(), ("phases",))
    reserved = [t["id"] for t in snap["tasks"]] + [r.get("task") or "" for r in snap["phases"]]
    reserved += [p.stem for p in (env_dir() / "lead").glob("*.md")]
    if "." in task or task.lower() in {x.lower() for x in reserved}:
        die(f"task {task} is invalid or already reserved")
    if brief_file(task).exists() or phase_rows(task):
        die(f"the goal {task} exists already; team goal status {task}")
    classify = leaf("classify")
    # The symbols read the masked goal's first CAP characters; classify gets it whole and unmasked, masks it itself and
    # keeps the unmasked text for the review floor only (bin/project's former stems), which shows no goal text.
    text = mask_secrets(positional[0], KNOWN)
    files = files_of(repo)
    names = classify.symbol_names(text[:classify.CAP])
    result = classify.classify(positional[0], files, symbols_of(repo, files, names), rules(ini))
    brief = render(task, result, repo)
    brief_file(task).parent.mkdir(parents=True, exist_ok=True)
    put(brief_file(task), brief)
    goal_file(task).parent.mkdir(parents=True, exist_ok=True)
    write_json(goal_file(task), {"task": task, "repo": str(repo), "class": result["class"], "plan": None})
    state().store().event_append("phase.state", "lead", task, {"phase": "INIT", "state": "active",
                               "reason": json.dumps({"goal_repo": str(repo), "class": result["class"]})})
    say(brief.rstrip("\n"))
    return 0


# ---------------------------------------------------------------- leaving a phase

def usage_of() -> str:
    return "usage: " + " | ".join(ln.strip() for ln in USAGE.strip("\n").splitlines()
                                  if ln.strip().startswith("team goal "))


def load_goal(task: str) -> dict:
    check_task(task)
    goal = read_json(goal_file(task)) or {"task": task}
    if current_phase(task) is None:
        die(f"no goal {task}; team goal new \"TEXT\" first")
    log = state().store()
    for path in sorted(log.events_dir().glob("*.jsonl")):
        for event in log.event_read(path)[0]:
            if event.get("task") != task or event.get("kind") != "phase.state":
                continue
            try:
                metadata = json.loads(event.get("data", {}).get("reason") or "")
            except (ValueError, TypeError):
                continue
            if isinstance(metadata, dict):
                if "goal_repo" in metadata:
                    goal.update(repo=metadata["goal_repo"], **{"class": metadata.get("class")})
                if "goal_plan" in metadata:
                    goal["plan"] = metadata["goal_plan"]
                    goal["wave"] = metadata.get("wave")
    if not goal.get("repo"):
        die(f"goal {task} metadata is unavailable; restore its goal cache")
    return goal


def reachable(task: str, phase: str) -> str:
    """The goal's current phase, when phase is that one or the next; no phase is skipped or entered again."""
    now_in = current_phase(task)
    before = PHASES[PHASES.index(phase) - 1]
    rows = phase_rows(task)
    current = next((r for r in rows if r.get("phase") == now_in), {})
    if current.get("state") == "done" and now_in == "VERIFY":
        die(f"{task} is complete")
    if now_in != phase and now_in in ("CONCEPT", "REALIZE") and current.get("state") != "ready":
        die(f"{task} phase {now_in} is not ready; resume or inspect it first")
    if now_in not in (phase, before):
        die(f"{task} is in {now_in}; phase {phase} comes after {before}, no phase is skipped")
    return now_in


def enter_check(ini, task: str, phase: str, yes: bool) -> str:
    """Check whether phase can be entered; require --yes when leaving the previous phase."""
    now_in = reachable(task, phase)
    if now_in == "CONCEPT" and phase == "REALIZE":
        flow_path = flow_dir(task) / "state.json"
        if flow_path.is_file() and read_json(flow_path).get("status") != "ready":
            die(f"{task} concept flow is not ready; resume or inspect it first")
    if now_in != phase and not yes:
        if get(ini, "team", "autonomy", "ask") == "auto":
            die(f"leaving {now_in} for {phase} needs the lead: run it again with --yes")
        die(f"autonomy = ask: leaving {now_in} for {phase} needs the human's yes first; once they said yes, "
            "the lead runs it again with --yes")
    return now_in


def enter(ini, task: str, phase: str, yes: bool, reason: str) -> None:
    """Move the goal into phase: from the phase before it (leaving that needs --yes), or stay in it (no --yes)."""
    now_in = enter_check(ini, task, phase, yes)
    if now_in == phase:
        write_phase(task, [(phase, "active", reason)])
        return
    write_phase(task, [(now_in, "done", f"left for {phase}"), (phase, "active", reason)])


def flag_args(args: list[str], single=()) -> tuple[list[str], dict, bool]:
    yes = "--yes" in args
    positional, opts = state().parse([a for a in args if a != "--yes"], usage_of(), single=single)
    if len(positional) != 1:
        die(usage_of())
    return positional, opts, yes


def concept_flags(args: list[str]) -> tuple[str, list[str], bool]:
    flags = ("--retry", "--detach", "--status")
    chosen = [a for a in flags if a in args]
    rest = [a for a in args if a not in flags]
    positional, opts, yes = flag_args(rest, single=("--answer",))
    forward = [*([f"--answer", opts["--answer"]] if "--answer" in opts else []),
               *(f for f in chosen if f != "--status")]
    return positional[0], (["--status"] if "--status" in chosen else forward), yes


def prepare_concept_brief(task: str) -> Path:
    source = brief_file(task)
    if not source.is_file():
        die(f"no brief {source}")
    try:
        text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        die(f"cannot read {source}: {error}")
    problems = brief_problems(text)
    if problems:
        die(f"the brief {shown(source)} is not ready ({'; '.join(problems)})")
    proposed = brief_header(text)["class"]
    text = re.sub(r"(?m)^Class:.*$", f"Class: plan\nProposed class: {proposed}", text, count=1)
    target = brief_file(task, ".concept")
    put(target, text)
    return target


def concept_call(task: str, args: list[str]) -> int:
    try:
        return team_call(args)
    except BaseException:
        write_phase(task, [("CONCEPT", "blocked", "flow interrupted; resume concept")])
        raise


def start_or_resume_concept(ini, task: str, goal: dict, forward: list[str], yes: bool) -> int:
    enter_check(ini, task, "CONCEPT", yes)
    if (flow_dir(task) / "state.json").is_file():
        enter(ini, task, "CONCEPT", yes, "team flow with class plan")
        code = concept_call(task, ["flow", task, *forward])
        if code or "--detach" not in forward:
            write_phase(task, [("CONCEPT", "blocked" if code else "ready", "flow finished; resume if blocked")])
        return code
    target = prepare_concept_brief(task)
    enter(ini, task, "CONCEPT", yes, "team flow with class plan")
    code = concept_call(task, ["flow", task, "--brief", str(target), "--repo", goal["repo"], *forward])
    if code or "--detach" not in forward:
        write_phase(task, [("CONCEPT", "blocked" if code else "ready", "flow finished; resume if blocked")])
    return code


def goal_concept(ini, args: list[str]) -> int:
    task, forward, yes = concept_flags(args)
    goal = load_goal(task)
    if forward == ["--status"]:
        if current_phase(task) == "INIT":
            die(f"{task} is in INIT; phase CONCEPT has not started")
        return team_call(["flow", task, "--status"])
    return start_or_resume_concept(ini, task, goal, forward, yes)


def plan_of(task: str, goal: dict) -> Path | None:
    """The plan folder: the one the goal remembers, else the folder of the concept flow's PLAN.md."""
    if goal.get("plan"):
        return Path(goal["plan"])
    flow = read_json(flow_dir(task) / "state.json")
    for step in reversed(flow.get("steps") or []):
        found = [p for p in (step.get("plan") or []) if isinstance(p, str) and p.endswith("PLAN.md")]
        if found:
            path = Path(found[0])
            return (path if path.is_absolute() else PROJECT / path).parent
    return None


_TEAM_CALL = team_call


def batch_call(plan: Path, wave: str, repo: str | Path | None) -> int:
    if not repo or team_call is not _TEAM_CALL:
        return team_call(["batch", str(plan), "--wave", wave])
    repo_path = Path(repo).resolve()
    if repo_path == PROJECT.resolve():
        return team_call(["batch", str(plan), "--wave", wave])
    batch = leaf("batch")
    before = batch.PROJECT
    batch.PROJECT = repo_path
    try:
        return batch.do_batch([str(plan), "--wave", wave])
    finally:
        batch.PROJECT = before


def goal_realize(ini, args: list[str]) -> int:
    positional, opts, yes = flag_args(args, single=("--wave", "--plan"))
    task, wave = positional[0], opts.get("--wave") or ""
    if not wave.isdigit():
        die(usage_of())
    goal = load_goal(task)
    enter_check(ini, task, "REALIZE", yes)
    plan = Path(opts["--plan"]).resolve() if opts.get("--plan") else plan_of(task, goal)
    if plan is None or not (plan / "PLAN.md").is_file():
        die(f"no plan for {task}: the concept flow has no PLAN.md yet; or give --plan DIR")
    row = next((r for r in phase_rows(task) if r.get("phase") == "REALIZE"), {})
    if current_phase(task) == "REALIZE" and row.get("state") == "blocked":
        blocked_wave = str(goal.get("wave") or "")
        blocked_plan = goal.get("plan")
        if (not blocked_wave.isdigit() or int(wave) != int(blocked_wave)
                or not blocked_plan or plan.resolve() != Path(blocked_plan).resolve()):
            die(f"{task} REALIZE wave {blocked_wave or 'unknown'} is blocked; resume or recover it first")
    enter(ini, task, "REALIZE", yes, json.dumps({"goal_plan": str(plan), "wave": wave}))
    write_json(goal_file(task), {**goal, "plan": str(plan)})
    try:
        code = batch_call(plan, wave, goal.get("repo") or PROJECT)
    except BaseException:
        write_phase(task, [("REALIZE", "blocked", "batch interrupted; resume realize")])
        raise
    write_phase(task, [("REALIZE", "blocked" if code else "ready", "batch finished; resume if blocked")])
    return code


# ---------------------------------------------------------------- packages

def packages(plan: Path) -> list[dict]:
    """Every package of the plan's table (the first with the columns ID and Wave): id and its brief's task."""
    lines = (plan / "PLAN.md").read_text(encoding="utf-8").splitlines()
    for at, line in enumerate(lines):
        head = [c.lower() for c in leaf("batch").cells(line)] if line.lstrip().startswith("|") else []
        if "id" not in head or "wave" not in head:
            continue
        found = []
        for row in lines[at + 2:]:
            if not row.lstrip().startswith("|"):
                break
            values = dict(zip(head, leaf("batch").cells(row)))
            pid = values.get("id") or ""
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", pid):
                continue
            path = plan / "briefs" / f"{pid}.md"
            task = brief_header(path.read_text(encoding="utf-8"))["task"] if path.is_file() else ""
            found.append({"id": pid, "task": task if task and not task_problem(task) else pid})
        return found
    return []


def goal_verify(ini, args: list[str]) -> int:
    positional, _, yes = flag_args(args)
    task = positional[0]
    goal = load_goal(task)
    reachable(task, "VERIFY")
    plan = plan_of(task, goal)
    if plan is None or not (plan / "PLAN.md").is_file():
        die(f"no plan for {task}; team goal realize {task} --wave N --plan DIR first")
    enter(ini, task, "VERIFY", yes, "review verdicts and merge proposals")
    say("VERIFY inspection only; completion requires team goal complete --yes")
    merges = state().snapshot(state().store())["merges"]
    for p in packages(plan):
        flow = read_json(flow_dir(p["task"]) / "state.json")
        review = next((st for st in reversed(flow.get("steps") or []) if isinstance(st, dict)
                       and st.get("kind") == "review"), {})
        head = str(review.get("head") or "")
        verdict = f"review {review.get('verdict') or 'none'} {head[:12]}".rstrip()
        proposal = state().latest(merges, p["task"], "proposed")
        merge = f"merge proposal {proposal['head'][:12]}" if proposal else "no merge proposal"
        say(f"{p['id']} {p['task']} {verdict} · {merge}")
        flow_head = str(flow.get("head") or "")
        branch_head = ""
        if flow.get("branch") and flow.get("repo") and Path(flow["repo"]).is_dir():
            branch_head = leaf("flow").head_of(flow) or ""
        steps = flow.get("steps") or []
        reviews = leaf("flow").round_reviews(steps)
        latest = {r.get("index", 1): r for r in reviews}
        klass = flow.get("class") or "standard"
        required = class_options(ini, klass)["reviews"] if ini is not None else (2 if klass == "security" else 1)
        approved = sum(1 for i in range(1, required + 1) if i in latest
                       and latest[i].get("verdict") == "approve" and flow_head
                       and latest[i].get("head") == flow_head)
        say(f"  flow {flow.get('status') or 'unknown'} head {flow_head or 'missing'}; "
            f"branch head {branch_head or 'unavailable'}; approvals {approved}/{required} on flow head")
        for item in (st for st in steps if isinstance(st, dict) and st.get("kind") == "review"):
            review_head = str(item.get("head") or "")
            say(f"  review {item.get('index', 1)}: {item.get('verdict') or 'none'} {review_head or 'missing'}" +
                (" (head mismatch)" if review_head != flow_head else ""))
        if branch_head and branch_head != flow_head:
            say("  branch/flow head mismatch")
        if proposal:
            say(f"  proposal head {proposal['head']}" +
                (" (proposal/flow head mismatch)" if proposal.get("head") != flow_head else ""))
    return 0


def goal_status(ini, args: list[str]) -> int:
    positional, _, _ = flag_args(args)
    task = positional[0]
    goal = load_goal(task)
    phase = current_phase(task)
    phase_state = next((r.get("state") for r in phase_rows(task) if r.get("phase") == phase), "unknown")
    say(f"{task} phase {phase} ({phase_state})")
    plan = plan_of(task, goal)
    if plan is None or not (plan / "PLAN.md").is_file():
        say("packages: none yet (no plan)")
        return 0
    snap = state().snapshot(state().store(), ("flows",))
    tasks = {t["id"]: t.get("state") for t in snap["tasks"]}
    flows = {f["id"]: f.get("status") for f in snap["flows"]}
    for p in packages(plan):
        say(f"{p['id']} ({p['task']}): task {tasks.get(p['task']) or 'not-started'}; "
            f"flow {flows.get(p['task']) or 'not-started'}")
    return 0


def goal_recover(ini, args: list[str]) -> int:
    positional, _, yes = flag_args(args)
    task = positional[0]
    goal = load_goal(task)
    phase = current_phase(task)
    row = next((r for r in phase_rows(task) if r.get("phase") == phase), {})
    if phase != "REALIZE" or row.get("state") != "blocked":
        die(f"{task} has no blocked REALIZE wave to recover")
    if not yes:
        die("recovering REALIZE needs the lead's --yes (human approval first under autonomy = ask)")
    plan = plan_of(task, goal)
    wave = str(goal.get("wave") or "")
    if not plan or not (plan / "PLAN.md").is_file() or not wave.isdigit():
        die(f"{task} has no recorded plan and wave; restore the goal metadata")
    rows = leaf("batch").plan_wave((plan / "PLAN.md").read_text(encoding="utf-8"), int(wave))
    by_id = {p["id"]: p for p in packages(plan)}
    if not rows or any(r["id"] not in by_id for r in rows):
        die(f"{task} wave {wave} has no complete package list")
    pending = [r["id"] for r in rows
               if read_json(flow_dir(by_id[r["id"]]["task"]) / "state.json").get("status") != "ready"]
    if pending:
        die(f"{task} wave {wave} flows are not ready: {', '.join(pending)}; recover them with team flow")
    write_phase(task, [("REALIZE", "ready", f"wave {wave} flows recovered")])
    say(f"{task} wave {wave} recovered; REALIZE ready")
    return 0


def goal_complete(ini, args: list[str]) -> int:
    positional, _, yes = flag_args(args)
    task = positional[0]
    load_goal(task)
    if current_phase(task) != "VERIFY":
        die(f"{task} must be in VERIFY before completion")
    reachable(task, "VERIFY")
    if not yes:
        die("completing VERIFY needs the lead's --yes (human approval first under autonomy = ask)")
    write_phase(task, [("VERIFY", "done", "inspection completed with lead approval")])
    say(f"{task} complete; VERIFY was inspection, not a pass/fail verdict")
    return 0


ACTIONS = {"recover": goal_recover, "complete": goal_complete, "new": goal_new, "concept": goal_concept, "realize": goal_realize, "verify": goal_verify,
           "status": goal_status}


def do_goal(rest: list[str]) -> int:
    if not rest or rest[0] not in ACTIONS:
        die(usage_of())
    return ACTIONS[rest[0]](config(), rest[1:])


COMMANDS = {"goal": do_goal}
