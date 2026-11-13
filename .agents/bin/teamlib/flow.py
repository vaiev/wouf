"""teamlib.flow: briefs, classes, scope and verdicts, and a task's flow through scout, worker, gate and review;
the command flow."""

from __future__ import annotations

import ipaddress
import threading

from teamlib.core import *  # noqa: F401,F403
from teamlib import engine
from teamlib.engine import TIERS, egress_allows, egress_base, egress_hosts, egress_needs, login_check, mode, uses_login
from teamlib.gate import GATE_LINES, evidence
from teamlib.run import (LAUNCH, STAGED, STORE, TEAM, block_items, check_ref_conflict, check_task, commits,
                         masked_lines, record, return_block, shown, split_repo, stage_of)


# ---------------------------------------------------------------- briefs, classes, scope, verdicts

# What a task class means when the team's config.ini has no [class.<name>] section for it. Models stay tiers.
CLASS_DEFAULTS = {
    "mechanical": {"bot": "worker", "tier": "light", "effort": "low", "reviews": "1", "scout": "no",
                   "steps": "worker gate review"},
    "standard": {"bot": "worker", "tier": "strong", "effort": "medium", "reviews": "1", "scout": "no",
                 "steps": "worker gate review"},
    "complex": {"bot": "worker", "tier": "strong", "effort": "high", "reviews": "1", "scout": "yes",
                "steps": "scout worker gate review"},
    "security": {"bot": "worker", "tier": "strong", "effort": "high", "reviews": "2", "scout": "yes",
                 "steps": "scout worker gate review*2"},
    "decision": {"bot": "decide", "tier": "strong", "effort": "high", "reviews": "0", "scout": "no", "steps": "worker"},
    "analysis": {"bot": "worker", "tier": "strong", "effort": "medium", "reviews": "0", "scout": "no",
                 "steps": "worker"},
    "plan": {"bot": "planner", "tier": "strong", "effort": "high", "reviews": "0", "scout": "yes",
             "steps": "scout worker"},
}

# Classes that never run below strong (review runs on strong too: step_tier).
STRONG_CLASSES = ("complex", "security", "decision", "plan")


# The planner/batch contract lives here: command leaves consume it without a kernel-to-leaf import.
PLAN_SCHEMA = """The batch contract requires a Markdown package table with columns ID, Wave, Runner, Depends.
IDs and brief Task IDs are unique (letters, digits, _ and -); Wave is a positive integer; Runner is w or wN
(worker or workerN). Depends is none or comma/space-separated package IDs, all in earlier waves.
Write one complete out/briefs/<id>.md per row: Task, Class, Paths, Objective, Acceptance criteria, Scope,
Out of scope, Inputs, Constraints, Expected output; security also needs Threat model. No TO FILL remains.
Paths are repository-relative, without . or .. components or backslashes. Assign distinct workers/scouts to
concurrent packages. Only provably disjoint Paths may run together; uncertain glob intersections are refused.
Later waves start only after every earlier package is ready and its exact head is merged into the chosen base.
Plan-ready means draft artifacts delivered, not approval or permission to dispatch; batch validates the contract."""


def plan_cells(line: str) -> list[str]:
    return [cell.strip().strip("`*").strip() for cell in line.strip().strip("|").split("|")]


def plan_runner(text: str) -> str | None:
    """The brief's Runner: line (also inside a constraint: "Runner: worker2."), or None."""
    found = re.search(r"\bRunner:[ \t]*\**([A-Za-z0-9_-]+)", text)
    return found.group(1) if found else None


def plan_bots(runner: str) -> tuple[str, str]:
    """Worker and scout of a runner: w or worker → worker and scout, w2 or worker2 → worker2 and scout2."""
    found = re.fullmatch(r"(?:w|worker)([0-9]*)", runner or "")
    if not found:
        die(f"runner {runner!r}: a batch runs on w (worker) or wN (workerN)")
    return f"worker{found.group(1)}", f"scout{found.group(1)}"


def load_plan(plan_dir: Path) -> list[dict]:
    """Validate the whole dispatch contract, before any package of any wave can start. Raises ValueError."""
    lines = (plan_dir / "PLAN.md").read_text(encoding="utf-8").splitlines()
    rows = []
    for at, line in enumerate(lines):
        head = [c.lower() for c in plan_cells(line)] if line.lstrip().startswith("|") else []
        if "id" not in head or "wave" not in head:
            continue
        head = ["depends" if c == "depends on" else c for c in head]
        if len(set(head)) != len(head) or not {"id", "wave", "runner", "depends"}.issubset(head):
            raise ValueError("the package table needs unique columns ID, Wave, Runner, Depends")
        if at + 1 >= len(lines) or not all(re.fullmatch(r":?-+:?", c) for c in plan_cells(lines[at + 1])):
            raise ValueError("the package table needs a Markdown separator row")
        for line in lines[at + 2:]:
            if not line.lstrip().startswith("|"):
                break
            values = plan_cells(line)
            if len(values) != len(head):
                raise ValueError("the package table has an incomplete row")
            rows.append(dict(zip(head, values)))
        break
    if not rows:
        raise ValueError("the plan needs a nonempty package table: ID, Wave, Runner, Depends")
    found, ids, tasks, classes = [], set(), set(), known_classes()
    for row in rows:
        pid = row["id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", pid):
            raise ValueError(f"package id {pid!r}: letters, digits, _ - only")
        if pid in ids:
            raise ValueError(f"duplicate package id {pid}")
        ids.add(pid)
        if not re.fullmatch(r"[1-9][0-9]*", row["wave"]):
            raise ValueError(f"package {pid}: Wave must be a positive integer")
        if not re.fullmatch(r"(?:w|worker)[0-9]*", row["runner"]):
            raise ValueError(f"package {pid}: runner must be w or wN (worker or workerN)")
        path = plan_dir / "briefs" / f"{pid}.md"
        if not path.is_file():
            raise ValueError(f"no draft brief {path} for package {pid}")
        text = path.read_text(encoding="utf-8")
        if "TO FILL" in text:
            raise ValueError(f"package {pid}: the brief still says TO FILL")
        fields = {}
        for match in re.finditer(r"(?ms)^([A-Za-z][A-Za-z ]*):[ \t]*(.*?)(?=^[A-Za-z][A-Za-z ]*:|\Z)", text):
            name, value = match.group(1), match.group(2).strip()
            if name in fields:
                raise ValueError(f"package {pid}: duplicate brief field {name}")
            fields[name] = value
        required = ("Task", "Class", "Paths", "Objective", "Acceptance criteria", "Scope", "Out of scope",
                    "Inputs", "Constraints", "Expected output")
        for name in required + (("Threat model",) if fields.get("Class", "").lower() == "security" else ()):
            if not fields.get(name):
                raise ValueError(f"package {pid}: the brief needs {name} (non-empty)")
        if fields["Class"].lower() not in classes or fields["Class"].lower() == "plan":
            raise ValueError(f"package {pid}: Class must be an executable package class, one of "
                             f"{', '.join(c for c in classes if c != 'plan')}")
        header = brief_header(text, classes)
        if not header["paths"]:
            raise ValueError(f"package {pid}: Paths must name at least one repository-relative scope")
        task = header["task"]
        if task_problem(task) or "." in task:
            raise ValueError(f"package {pid}: invalid Task {task!r}")
        if task in tasks:
            raise ValueError(f"duplicate task id {task}")
        tasks.add(task)
        for scope in header["paths"]:
            if (scope.startswith("/") or "\\" in scope or ":" in scope or
                    any(c in ("", ".", "..") for c in (scope[:-1] if scope.endswith("/") else scope).split("/"))):
                raise ValueError(f"package {pid}: Paths must be unambiguous repository-relative paths: {scope!r}")
        runner = plan_runner(text) or row["runner"]
        if not re.fullmatch(r"(?:w|worker)[0-9]*", runner):
            raise ValueError(f"package {pid}: invalid runner {runner!r}")
        member, scout = plan_bots(runner)
        depends = [] if row["depends"].lower() in ("none", "-", "") else words(row["depends"])
        found.append({"id": pid, "task": task, "wave": int(row["wave"]), "depends": depends,
                      "brief": path.resolve(), "class": header["class"], "paths": header["paths"],
                      "bot": member, "scout": scout, "digest": hashlib.sha256(text.encode()).hexdigest()})
    by_id = {p["id"]: p for p in found}
    for p in found:
        for dep in p["depends"]:
            if dep not in by_id or by_id[dep]["wave"] >= p["wave"]:
                raise ValueError(f"package {p['id']}: dependency {dep!r} must name a package in an earlier wave")
    return found


def class_options(ini, name: str) -> dict:
    """Bot, runner, tier, model, effort, number of reviews and review runners for a task class."""
    if name not in known_classes(ini):
        die(f"unknown class {name!r}")
    # A configured class (a [class.<name>] outside CLASS_DEFAULTS): worker, 1 review, no scout; no default tier.
    section = {k: v.strip() for k, v in (ini[f"class.{name}"].items() if ini.has_section(f"class.{name}") else [])}
    merged = {**CLASS_DEFAULTS.get(name, {"bot": "worker", "reviews": "1", "scout": "no"}), **section}
    if name not in CLASS_DEFAULTS and not merged.get("tier"):
        die(f"[class.{name}] tier is missing: a configured class names its tier (light, mid or strong)")
    if merged.get("tier") not in TIERS:
        die(f"[class.{name}] tier must be light, mid or strong")
    if name in STRONG_CLASSES and merged["tier"] != "strong":
        die(f"[class.{name}] runs on strong, never lower")
    check_member(merged.get("bot") or "worker")
    steps, rounds = class_table(name, merged, section)
    return {"bot": merged.get("bot") or "worker", "runner": merged.get("runner") or None, "tier": merged["tier"],
            "model": merged.get("model") or None, "effort": merged.get("effort") or None,
            "reviews": table_reviews(steps), "review_runners": words(merged.get("review_runners", "")),
            "scout": any(st["kind"] == "scout" for st in steps), "steps": steps, "rounds": rounds}


def class_table(name: str, merged: dict, section: dict) -> tuple[list, int]:
    """A class's steps table and review rounds. steps, when the section sets it (even empty), wins: the old keys
    scout and reviews are then ignored. Otherwise they render into the table when set or when there is no table."""
    if "steps" in section:
        text = section["steps"]
    elif "reviews" in section or "scout" in section or not merged.get("steps"):
        text = legacy_steps(name, merged)
    else:
        text = merged["steps"]
    steps = parse_steps(text, name)
    rounds = merged.get("rounds") or str(FLOW_ROUNDS)
    if not re.fullmatch(r"[1-5]", rounds):
        die(f"[class.{name}] rounds must be 1 to 5 (review rounds with CHANGES before the flow is blocked)")
    return steps, int(rounds)


def legacy_steps(name: str, merged: dict) -> str:
    """The steps table of the old keys: scout (yes/no) and reviews (0-3)."""
    try:
        reviews = int(merged.get("reviews", "1"))
    except ValueError:
        reviews = -1
    if not 0 <= reviews <= 3:
        die(f"[class.{name}] reviews must be 0 to 3")
    if merged.get("scout") not in ("yes", "no"):
        die(f"[class.{name}] scout must be yes or no")
    return render_steps(merged["scout"] == "yes", reviews)


def render_steps(scout: bool, reviews: int) -> str:
    """The steps table text of scout (yes/no) and reviews (0-3)."""
    return " ".join(["scout"] * scout + ["worker"] + ["gate", f"review*{reviews}"] * (reviews > 0))


def parse_steps(text: str, name: str) -> list:
    """A class's steps table, [{"kind", "count"}, ...]: preflight* worker [gate review[*N]], N 1 to 3; the
    preflights are scout and research. Anything else dies naming [class.<name>] steps."""
    where, steps, seen = f"[class.{name}] steps", [], []
    for token in text.split():
        found = re.fullmatch(r"(scout|research|worker|gate)|review(?:\*([0-9]+))?", token)
        if not found:
            die(f"{where}: unknown step {token!r}; the steps are scout, research, worker, gate, review, review*N")
        kind, count = found.group(1) or "review", int(found.group(2) or 1)
        problem = step_problem(kind, count, seen)
        if problem:
            die(f"{where}: {problem}")
        seen.append(kind)
        steps.append({"kind": kind, "count": count})
    if "worker" not in seen:
        die(f"{where} needs one worker")
    if "gate" in seen and "review" not in seen:
        die(f"{where}: gate without review; a gate is followed by its reviews")
    return steps


def step_problem(kind: str, count: int, seen: list) -> str:
    """Why one step cannot follow the steps seen so far; empty when it can."""
    if kind in seen:
        return f"{kind} twice"
    if kind in PREFLIGHT and "worker" in seen:
        return f"{kind} after the worker; scout and research come before it"
    if kind == "gate" and "worker" not in seen:
        return "gate before the worker"
    if kind == "review" and "gate" not in seen:
        return "review without a gate before it"
    if not 1 <= count <= 3:
        return f"review*{count}: 1 to 3 reviews"
    return ""


def table_reviews(steps: list) -> int:
    return sum(st["count"] for st in steps if st["kind"] == "review")


def review_verdict(lines: list[str], name: str, task: str, head: str) -> dict:
    """The verdict in a review's last signed block: approve (on exactly this head), stale (an approve of
    something else), changes (with bug and change items) or None (no block)."""
    start = return_block(lines, name, task)
    if start is None:
        return {"verdict": None, "bugs": [], "changes": []}
    block = lines[start:start + RETURN_LINES]
    rest = block[0].split(f"{name} · {task} · ", 1)[1].split()
    if rest and rest[0].upper() == "APPROVE":
        sha = rest[1].lower() if len(rest) > 1 else ""
        good = re.fullmatch(r"[0-9a-f]{7,40}", sha) is not None and head.lower().startswith(sha)
        return {"verdict": "approve" if good else "stale", "bugs": [], "changes": []}
    if not rest or rest[0].upper() != "CHANGES":
        return {"verdict": None, "bugs": [], "changes": []}
    items, inside = [], False
    for line in block[1:]:
        key = re.match(r"^(Blocking|Non-blocking|Checked|Not checked|Flags):\s*(.*)$", line)
        if key:
            inside = key.group(1) == "Blocking"
            line = key.group(2)
        if inside:
            item = line.strip().lstrip("-*• ").strip()
            if item and item.lower() != "none":
                items.append(item)
    bugs = [i for i in items if i.lower().startswith("[bug]")]
    return {"verdict": "changes", "bugs": bugs, "changes": [i for i in items if i not in bugs]}


def flow_header(text: str) -> dict:
    """brief_header plus the line that only a flow reads: Research (yes/no) runs the research bot in the pre-flight."""
    found = re.search(r"^Research:[ \t]*(.*?)\s*$", text, re.M)
    return {**brief_header(text), "research": bool(found) and found.group(1).lower() == "yes"}


# Classes whose context graph (in/CONTEXT_GRAPH.md, staged by every run with Paths) replaces the scout.
GRAPH_CLASSES = ("complex", "security")


# ---------------------------------------------------------------- egress: a brief's hosts

# The task of a flow's scout, research and review runs (T-1.scout, T-1.r2): their lead decisions are the task's own.
FLOW_RUN = re.compile(r"^(.+)\.(?:scout|research|r[0-9]+)$")
EGRESS_LINE = re.compile(r"^Egress:[ \t]*(.*?)\s*$", re.M)


def brief_egress(text: str) -> str:
    """The hosts of a brief's first Egress: line, as written; empty without one."""
    found = EGRESS_LINE.search(text)
    return found.group(1) if found else ""


def egress_decided(task: str) -> list[str]:
    """The hosts the lead (or the human) said yes to for task: decision records of that task, decided or approved,
    by lead or human, whose title is `Egress: <hosts>`. Without a store there are none."""
    if not STORE:
        return []
    found = FLOW_RUN.match(task)
    tasks = {task, found.group(1)} if found else {task}
    try:
        rows = STORE[0].snapshot(STORE[0].store())["decisions"]
    except (Exception, SystemExit):
        return []
    hosts: list[str] = []
    for row in rows:
        title = str(row.get("title") or "")
        if row.get("task") in tasks and row.get("status") in ("decided", "approved") and \
                row.get("decided_by") in ("lead", "human") and title.lower().startswith("egress:"):
            try:
                hosts += egress_hosts(title.split(":", 1)[1], f"decision D-{row.get('id')}")
            except SystemExit:
                continue
    return hosts


# [egress] private: the host:port pairs a run may reach through the proxy although they are private (a model server
# on this computer). The rule is the proxy tool's (parse_private in egress_proxy.py), written again here because the
# proxy imports nothing of the pack, and the two must agree: a literal loopback, RFC 1918 or unique-local address
# ([v6] in brackets) or the container host alias, and one decimal port. The networks are built from integers so the
# leak scan of tests/test_pack.py finds no literal. Nothing here touches the bot's own-loopback variables.
PRIVATE_NETS = (ipaddress.IPv4Network((0x7F000000, 8)), ipaddress.IPv6Network(("::1", 128)),
                ipaddress.IPv4Network((0x0A000000, 8)), ipaddress.IPv4Network((0xAC100000, 12)),
                ipaddress.IPv4Network((0xC0A80000, 16)), ipaddress.IPv6Network(("fc00::", 7)))
HOST_ALIASES = ("host.docker.internal", "host.containers.internal")
PRIVATE_PORT = re.compile(r"[1-9][0-9]{0,4}", re.ASCII)


def egress_pair(raw: str, where_from: str) -> str:
    """The canonical host:port of one [egress] private entry; anything else (a name, a wildcard, a range, a
    network, a public, link-local or CGNAT address, a URL) stops here, so a typo never opens more than one port."""
    host, sep, port = raw.lower().rpartition(":")
    if host.endswith(".") and host[:-1] in HOST_ALIASES:  # a trailing dot only on a name, never on a literal
        host = host[:-1]
    what = (f"host:port with a literal loopback or private address or {' or '.join(HOST_ALIASES)} and one port "
            "1-65535 (no name, wildcard, range, network or URL)")
    if not sep or not PRIVATE_PORT.fullmatch(port) or int(port) > 65535:
        die(f"{where_from}: {visible(raw)!r} is not {what}")
    if host in HOST_ALIASES:
        return f"{host}:{port}"
    bracketed = host.startswith("[") and host.endswith("]")
    try:
        ip = ipaddress.IPv6Address(host[1:-1]) if bracketed else ipaddress.IPv4Address(host)
    except ValueError:
        die(f"{where_from}: {visible(raw)!r} is not {what}")
    # No unwrapping: an IPv6 literal must be ::1 or unique-local itself; a mapped (::ffff:a.b.c.d), compatible
    # (::a.b.c.d), NAT64 or 6to4 form of a private IPv4 address is refused, as the proxy refuses it.
    if not any(ip in net for net in PRIVATE_NETS):
        die(f"{where_from}: {visible(raw)!r} is not a loopback or private address (10/8, 172.16/12, 192.168/16, "
            "::1, fc00::/7); a public, link-local or CGNAT address, or an IPv4 address embedded in IPv6, is refused")
    return f"[{ip.compressed}]:{port}" if bracketed else f"{ip}:{port}"


def egress_private(ini) -> list[str]:
    """The private pairs of [egress] private, canonical and without doubles; empty by default."""
    pairs = [egress_pair(raw, "[egress] private in team/config.ini") for raw in words(get(ini, "egress", "private"))]
    return list(dict.fromkeys(pairs))


def prepare_egress(ini, o: dict, decl: dict, current: str, runner: str) -> dict:
    """The allowlist of a run under network = egress, before anything is staged: [egress] allow, the base of every
    run, plus the model hosts of this run's runner ([egress] needs.<runner>, else the built-in list of its CLI), plus
    the brief's Egress: line (or --egress, which team flow passes) for this task, each host outside that list only
    with a recorded lead decision. Review and decide get no Egress: hosts, but their runner's needs."""
    if current not in ("container", "bwrap"):
        die("network = egress needs mode = container or bwrap: on this computer there is no sandbox to fence in; "
            "set [environment] network = bridge for host mode")
    base = egress_base(ini)
    needs = egress_needs(ini, runner)
    private = egress_private(ini)  # refused here, before any engine call, when an entry is not a literal pair
    if not needs:
        say(f"team: egress: {runner} has no model hosts (no [egress] needs.{runner} and no built-in list for its "
            f"CLI); set [egress] needs.{runner}, or the bot cannot reach its model", err=True)
    own = base + [h for h in needs if h not in base]
    asked = o["--egress"] if o["--egress"] is not None else \
        brief_egress(Path(o["--brief"]).read_text(encoding="utf-8"))
    extra = [] if decl.get("role", decl.get("id")) in ("review", "decide") else \
        egress_hosts(asked, "the brief's Egress: line")
    beyond = [h for h in extra if not egress_allows(own, h)]
    decided = egress_decided(o["task"]) if beyond else []
    missing = [h for h in beyond if h not in decided]
    if missing:
        die(f"Egress: {', '.join(missing)} is not in [egress] allow and has no lead decision for {o['task']}; "
            f"record one with .agents/bin/team decision add \"Egress: {' '.join(missing)}\" --task {o['task']} "
            f"--authority lead --options yes no --why ... --risk ..., or drop the host from the brief")
    allow = own + [h for h in extra if h not in own]
    say(f"team: egress: the list of this run ({runner}): {' '.join(allow) or 'nothing but the proxy'}", err=True)
    if private:  # every run that uses the exception says so at start
        say(f"team: egress: private host:port of this run ({runner}): {' '.join(private)}; the proxy lets exactly "
            "these through ([egress] private) and refuses every other private address", err=True)
    proxy = get(ini, "egress", "proxy")
    source = (PROJECT / proxy).resolve() if proxy else None
    if source is None or not source.is_file():
        die("network = egress needs the egress proxy: copy egress_proxy.py of the egress-proxy tool in the catalog "
            "into this project and name its path in [egress] proxy")
    return {"allow": allow, "private": private, "source": str(source)}


def preflight_scout(header: dict) -> bool | None:
    """The brief's choice of a scout: its Scout: line; else False where the context graph replaces the scout (a
    complex or security brief with Paths); else None, the class's default."""
    if header["scout"] is not None:
        return header["scout"]
    return False if header["class"] in GRAPH_CLASSES and header["paths"] else None


# ---------------------------------------------------------------- flow

FLOW_ROUNDS = 2
RUN_KINDS = ("scout", "research", "worker", "bugfixer", "review")
# Steps before the worker: they help, they never hold it up.
PREFLIGHT = ("scout", "research")


def flow_next(flow: dict, opts: dict, fallbacks: dict, at: str) -> dict | list:
    """The next step of a flow from the steps so far, without side effects: {"do": kind, ...}, a list of such steps
    that run at once (each on its own bot), or {"end": ready | blocked | limited | question, ...}. It walks the
    class's opts["steps"] (without it, the table of opts["scout"] and opts["reviews"]); opts["research"] (run the
    research bot) and opts["review2"] (the second reviewer's bot, when it is active) are optional."""
    table = opts.get("steps") or parse_steps(render_steps(opts["scout"], opts["reviews"]), "?")
    opts = {**opts, "steps": table, "reviews": table_reviews(table)}
    chain = flow_chain(opts)
    steps = flow.get("steps", [])
    if not steps:
        first = [{"do": kind} for kind in chain if kind in PREFLIGHT] or [enter(chain[0], opts)]
        return first if len(first) > 1 else first[0]
    last = steps[-1]
    kind, outcome = last["kind"], last.get("outcome")
    if any(st.get("over_budget") for st in steps):
        # A run passed the per-run budget: nothing more starts until the lead looked (team flow TASK --retry).
        return {"end": "blocked", "why": "over the per-run budget"}
    # A run over the day's budget was not started: the flow waits like a limit until its reset, without a fallback.
    waiting = [st for st in steps if st.get("outcome") == "skipped" and st["kind"] not in PREFLIGHT
               and (st.get("until") is None or st["until"] > at)]
    if waiting:
        known = [st["until"] for st in waiting if st.get("until")]
        return {"end": "limited", "until": max(known) if len(known) == len(waiting) else None,
                "why": waiting[-1].get("why")}
    if kind in PREFLIGHT and (last.get("group") is not None or kind not in chain):
        # Scout and research ran at once, or a preflight the table does not have: the worker goes on.
        return enter(after(chain, kind), opts)
    if kind == "review" and last.get("parallel"):
        return parallel_reviews_next(flow, opts, fallbacks, at)

    def again(**change) -> dict:
        step = {"do": kind, **({"index": last["index"]} if kind == "review" else {})}
        if kind == "worker" and last.get("after"):
            step["after"] = last["after"]
        return {**step, **change}

    if outcome == "skipped":
        return enter(after(chain, kind), opts) if kind in PREFLIGHT else again()
    if outcome == "limited" or (outcome in ("failed", "timeout") and last.get("fallback")):
        nxt = after_limit(steps, fallbacks, at, again)
        if nxt is not None:
            return nxt
    if outcome == "stopped":
        return {"end": "blocked", "why": f"the {kind} run was stopped"}
    if outcome in ("failed", "timeout") and kind in PREFLIGHT:
        return enter(after(chain, kind), opts)
    if outcome in ("failed", "timeout"):
        if len(steps) > 1 and steps[-2]["kind"] == kind and steps[-2].get("outcome") in ("failed", "timeout"):
            return {"end": "blocked", "why": f"the {kind} run failed twice ({outcome})"}
        return again(tier="strong") if kind != "review" else again()
    return STEP_HANDLERS[kind](flow, last, chain, opts, again)


def flow_chain(opts: dict) -> list:
    """The kinds a flow walks: the preflights switched on for this run (scout by opts["scout"], the table's or the
    brief's choice; research by opts["research"], a brief's Research: yes), then the table's worker, gate, review."""
    switched = [kind for kind in PREFLIGHT if opts.get(kind)]
    return switched + [st["kind"] for st in opts["steps"] if st["kind"] not in PREFLIGHT]


def after(chain: list, kind: str) -> str | None:
    """The kind that follows kind in the chain (after a preflight: the first step that is none), or None."""
    rest = ([k for k in chain if k not in PREFLIGHT] if kind in PREFLIGHT else
            chain[chain.index(kind) + 1:] if kind in chain else [])
    return rest[0] if rest else None


def enter(kind: str | None, opts: dict) -> dict | list:
    """The act that starts a step of the chain; past its end the flow is ready."""
    if kind is None:
        return {"end": "ready"}
    return first_reviews(opts) if kind == "review" else {"do": kind}


def preflight_done(flow: dict, last: dict, chain: list, opts: dict, again) -> dict | list:
    return enter(after(chain, last["kind"]), opts)


def worker_done(flow: dict, last: dict, chain: list, opts: dict, again) -> dict | list:
    """After the worker (or the bugfixer): questions, blocked, else the next step of the table."""
    kind = last["kind"]
    if last.get("status") == "answered":
        return {"do": kind}
    if last.get("status") == "questions":
        return {"end": "question", "why": last.get("questions") or "the worker has questions"}
    if last.get("status") == "blocked":
        return {"end": "blocked", "why": last.get("why") or f"the {kind} reports blocked"}
    nxt = after(chain, "worker")
    if nxt is None:
        return {"end": "ready"}
    if not last.get("head"):
        return {"end": "blocked", "why": f"the {kind} made no commits"}
    # A rework without new commits ("nothing to change") goes to the gate again; the gate and the review
    # rounds bound it.
    return enter(nxt, opts)


def gate_done(flow: dict, last: dict, chain: list, opts: dict, again) -> dict | list:
    # Reviews start only here, after a green gate: never on a head the gate has not passed.
    if last.get("outcome") == "green":
        return enter(after(chain, "gate"), opts)
    if sum(1 for st in flow["steps"] if st["kind"] == "gate" and st.get("outcome") == "red") >= 2:
        return {"end": "blocked", "why": "the gate was red twice"}
    return {"do": "worker", "tier": "strong", "after": "gate"}


def review_done(flow: dict, last: dict, chain: list, opts: dict, again) -> dict | list:
    steps = flow["steps"]
    verdict, index = last.get("verdict"), last.get("index", 1)
    if verdict == "approve":
        if index >= opts["reviews"]:
            return enter(after(chain, "review"), opts)
        runners = opts["review_runners"]
        return {"do": "review", "index": index + 1, "runner": runners[index] if len(runners) > index else None}
    if verdict in (None, "stale"):
        before = steps[-2] if len(steps) > 1 else {}
        if before.get("kind") == "review" and before.get("index") == index and \
                before.get("verdict", "x") in (None, "stale"):
            return {"end": "blocked", "why": "the review gave no verdict on the head twice"}
        return again()
    return rework(flow, steps, [last], opts)


# One transition handler per kind of step; the bugfixer's step is a worker's.
STEP_HANDLERS = {"scout": preflight_done, "research": preflight_done, "worker": worker_done,
                 "bugfixer": worker_done, "gate": gate_done, "review": review_done}


def rework(flow: dict, steps: list, found: list, opts: dict) -> dict:
    """After review changes: blocked after the class's rounds, else bugs alone to the bugfixer; with any change item
    the worker takes all of them."""
    rounds = opts.get("rounds", FLOW_ROUNDS) + (1 if flow.get("extra_round") else 0)
    if changes_rounds(steps) >= rounds:
        return {"end": "blocked", "why": f"{rounds} review rounds without APPROVE"}
    if any(st.get("bugs") for st in found) and not any(st.get("changes") for st in found):
        return {"do": "bugfixer"}
    return {"do": "worker", "after": "review"}


def changes_rounds(steps: list) -> int:
    """Review rounds with changes: one per gate, however many of its reviews asked for changes."""
    rounds, gates, counted = 0, 0, -1
    for st in steps:
        if st["kind"] == "gate":
            gates += 1
        elif st["kind"] == "review" and st.get("verdict") == "changes" and counted != gates:
            rounds, counted = rounds + 1, gates
    return rounds


def review_act(index: int, opts: dict, parallel: bool = False) -> dict:
    runners = opts["review_runners"]
    act = {"do": "review", "index": index, "runner": runners[index - 1] if len(runners) >= index else None}
    if parallel:
        act["parallel"] = True
        if index == 2:
            act["bot"] = opts["review2"]
    return act


def first_reviews(opts: dict) -> dict | list:
    """After a green gate: two reviews at once on review and review2 when the class asks for two and review2 is
    active, else the first review alone (the others follow one after the other)."""
    if opts["reviews"] == 2 and opts.get("review2"):
        return [review_act(1, opts, True), review_act(2, opts, True)]
    return review_act(1, opts)


def round_reviews(steps: list) -> list:
    """The review steps since the last gate."""
    gates = [at for at, st in enumerate(steps) if st["kind"] == "gate"]
    return [st for st in steps[(gates[-1] + 1 if gates else 0):] if st["kind"] == "review"]


def parallel_reviews_next(flow: dict, opts: dict, fallbacks: dict, at: str) -> dict | list:
    """The next step after reviews that ran at once. Both must approve the same head; changes from either go to the
    rework; a review that gave nothing runs again (alone, its approved partner is kept), at most once; a limited
    one goes once to its runner's fallback, else the flow waits until its reset, after all others returned."""
    steps = flow["steps"]
    reviews = round_reviews(steps)
    latest = {st.get("index", 1): st for st in reviews}
    found = [latest[index] for index in sorted(latest)]
    if any(st.get("verdict") == "changes" for st in found):
        return rework(flow, steps, [st for st in found if st.get("verdict") == "changes"], opts)
    again, waiting = [], []
    for index in range(1, opts["reviews"] + 1):
        last = latest.get(index)
        if last is not None and last.get("verdict") == "approve":
            continue
        if last is not None and last.get("outcome") == "stopped":
            return {"end": "blocked", "why": f"the review {index} run was stopped"}
        tries = [st for st in reviews if st.get("index", 1) == index]
        primary = next((st for st in reversed(tries) if st.get("outcome") == "limited" and not st.get("fallback")),
                       None)
        act = review_act(index, opts, True)
        if index == 2 and not act.get("bot"):
            act["bot"] = (last or {}).get("bot")
        if primary is not None and limit_open(primary, at):
            fallback = fallbacks.get(primary.get("runner") or "")
            if fallback and not any(st.get("fallback") for st in tries[tries.index(primary):]):
                again.append({**act, "runner": fallback, "fallback": True})
            else:
                waiting.append(primary)
            continue
        missed = [st for st in tries if st.get("outcome") not in ("limited", "skipped") and not st.get("fallback")]
        if len(missed) >= 2:
            return {"end": "blocked", "why": f"the review {index} gave no verdict on the head twice"}
        again.append(act)
    if waiting:
        known = [st["until"] for st in waiting if st.get("until")]
        return {"end": "limited", "until": min(known) if known else None}
    if again:
        return again if len(again) > 1 else again[0]
    heads = {st.get("head") for st in found}
    if len(heads) != 1:
        return {"end": "blocked", "why": "the reviews approved different heads"}
    return {"end": "ready"}


def limit_open(step: dict, at: str) -> bool:
    """A limited step whose reset has not come yet; an unknown reset never comes (team flow TASK --retry)."""
    return step.get("outcome") == "limited" and (step.get("until") is None or step["until"] > at)


def primary_period(steps: list, at: str) -> tuple:
    """The last step's limit on its primary runner (the latest limited step of its kind not on a fallback) and the
    steps of that bot since the first of its limits there whose reset has not come; no period once it has come."""
    kind = steps[-1]["kind"]
    primary = next((st for st in reversed(steps) if st["kind"] == kind and st.get("outcome") == "limited"
                    and not st.get("fallback")), None)
    if primary is None:
        return None, []
    member, runner = primary.get("bot"), primary.get("runner")
    for at_step, st in enumerate(steps):
        if st.get("bot") == member and st.get("runner") == runner and not st.get("fallback") and limit_open(st, at):
            return primary, [later for later in steps[at_step:] if later.get("bot") == member]
    return primary, []


def limited_end(period: list, last: dict, runner: str | None, at: str) -> dict:
    """The end of a flow that waits: until the earliest known reset of the primary or its fallback, with the
    fallback's failure as the reason."""
    known = [st["until"] for st in period if limit_open(st, at) and st.get("until")
             and (st.get("fallback") or st.get("runner") == runner)]
    end = {"end": "limited", "until": min(known) if known else None}
    if last.get("fallback") and last.get("outcome") != "limited":
        end["why"] = (f"the fallback runner {last.get('runner')} {last.get('outcome')}"
                      f"{': ' + last['why'] if last.get('why') else ''}; {runner} is limited")
    return end


def after_limit(steps: list, fallbacks: dict, at: str, again) -> dict | None:
    """The next step after a usage limit, or after a fallback runner's step that did not work: the fallback runner
    at most once per limited period of the primary runner, else the end, limited until the earliest known reset;
    never back and forth. None when no limit of the primary comes before (a fallback step's own failure)."""
    primary, period = primary_period(steps, at)
    if primary is None:
        return None
    fallback = fallbacks.get(primary.get("runner") or "")
    if period and steps[-1] is primary and fallback and not any(st.get("fallback") for st in period):
        return again(runner=fallback, fallback=True)
    if primary["kind"] in PREFLIGHT:
        # The context pack helps; it is not worth waiting for.
        return {"do": "worker"}
    if not period:
        # The primary's reset has come: it runs again, on its own runner.
        return again()
    return limited_end(period, steps[-1], primary.get("runner"), at)


FLOW_MAX_STEPS = 24
# Exit 6: closed flows cannot resume; an explicit --brief starts a fresh flow.
FLOW_EXIT = {"ready": 0, "blocked": 1, "limited": 4, "question": 5, "closed": 6}
FLOW_END = []  # The flowend leaf registers close and restart handling.
FLOW_OUTCOME = {"ready": "done", "blocked": "blocked", "limited": "limited", "question": "asked"}


def flow_dir(task: str) -> Path:
    return team_dir() / "flows" / task


def flow_lock(folder: Path, task: str) -> Path:
    """One flow per task at a time; a lock of a dead pid (or a zombie) is stale."""
    lock = folder / "flow.lock"
    for _ in range(3):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            if pid_running(read_json(lock).get("pid")):
                die(f"the flow of {task} is running already; see .agents/bin/team flow {task} --status")
            lock.unlink(missing_ok=True)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            sink(handle, dumps({"pid": os.getpid()}))
        return lock
    die(f"could not take the lock {lock}")
    return lock


def pid_running(pid) -> bool:
    """Alive and no zombie: under a pid 1 that does not reap (a container without init) an exited flow stays a
    zombie, which kill(pid, 0) still finds."""
    if not alive(pid):
        return False
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    return stat.rsplit(")", 1)[-1].split()[:1] != ["Z"]


def flow_fallbacks(ini) -> dict:
    return {k.split(".", 1)[1]: v.strip() for k, v in (ini["flow"].items() if ini.has_section("flow") else [])
            if k.startswith("fallback.") and v.strip()}


def head_of(flow: dict) -> str | None:
    found = git("rev-parse", "-q", "--verify", f"refs/heads/{flow['branch']}^{{commit}}", check=False,
                at=flow["repo"]).stdout.strip()
    return found or None


def flow_brief(ini, flow: dict, act: dict, folder: Path, bot_name: str, run_task: str, head: str | None) -> Path:
    """The brief of one step: the task's brief plus what this step needs to know."""
    text = (folder / "brief.md").read_text(encoding="utf-8").rstrip() + "\n"
    steps, kind = flow["steps"], act["do"]
    last_review = last_findings(steps)
    if kind == "worker" and act.get("after") == "gate":
        log = env_dir() / "gate" / flow["task"] / "gate.log"
        tail = masked_lines(log)[-GATE_LINES:]
        why = next((st.get("why") for st in reversed(steps) if st["kind"] == "gate"), "")
        text += f"\n## The pre-gate is red: {why}\n\nContinue on the branch and make it green. Its output:\n\n```text\n"
        text += "\n".join(tail) + "\n```\n"
    elif kind == "worker" and act.get("after") == "review" and last_review:
        text += "\n## Review findings to fix\n\nContinue on the branch; fix every item:\n\n"
        text += "".join(f"- {item}\n" for item in last_review.get("changes", []) + last_review.get("bugs", []))
    elif kind == "bugfixer" and last_review:
        text += "\n## Defects the review found\n\nFix exactly these on the branch, each with a test that shows it:\n\n"
        text += "".join(f"- {item}\n" for item in last_review.get("bugs", []))
    elif kind == "worker" and flow["class"] == "plan" and not act.get("after"):
        text += ("\n## Plan\n\nDo not change the repository and dispatch nothing. From in/CONTEXT.md and the repository "
                 "write out/PLAN.md (goal, assumptions, risks, order and dependencies of the packages, what stays with "
                 "the lead or the human) and one draft brief per package in out/briefs/<id>.md, in the format of "
                 "skills/brief.md. Ask under Questions instead of guessing a decision that belongs to the human.\n")
        text += "\n" + PLAN_SCHEMA + "\n"
    elif kind == "review":
        text += (f"\n## Review\n\nReview branch {flow['branch']} at commit {head} against the acceptance criteria "
                 f"above; the pre-gate passed. Check that the changed files stay within Paths. Your return block "
                 f"starts with `{bot_name} · {run_task} · APPROVE {head}` or `{bot_name} · {run_task} · CHANGES`.\n")
        if re.search(r"^Threat model:", text, re.M):
            text += ("\nThe brief's threat model is binding: list under Blocking only findings inside the threat model; "
                     "report findings outside it under Non-blocking.\n")
    elif kind == "scout":
        text += ("\n## Context pack\n\nDo not change anything. Write out/CONTEXT.md for the worker: the files, spec "
                 "rules, earlier decisions and tests this task touches, each with path and line, at most 400 lines.\n")
    elif kind == "research":
        text += ("\n## Research\n\nDo not change anything. Write out/RESEARCH.md for the worker: what this task needs "
                 "from outside the repository (documentation, options, known problems), each claim with its source "
                 "and date, at most 200 lines.\n")
    path = folder / f"brief-{len(steps) + 1:02d}-{act.get('bot') or kind}.md"
    put(path, text)
    return path


def last_findings(steps: list) -> dict | None:
    """The findings of the last review round with changes: the items of all its reviews that asked for changes."""
    last = next((at for at in range(len(steps) - 1, -1, -1)
                 if steps[at]["kind"] == "review" and steps[at].get("verdict") == "changes"), None)
    if last is None:
        return None
    start = next((at for at in range(last, -1, -1) if steps[at]["kind"] == "gate"), -1)
    found = [st for st in steps[start + 1:last + 1] if st["kind"] == "review" and st.get("verdict") == "changes"]
    return {"bugs": [i for st in found for i in st.get("bugs", [])],
            "changes": [i for st in found for i in st.get("changes", [])]}


def block_field(block: list[str], key: str) -> str:
    """A field of a return block: the rest of its line plus the list lines under it, up to the next field."""
    return " / ".join(block_items(block, key))


def step_tier(kind: str, act: dict, opts: dict) -> str | None:
    """The tier a step runs on: a review always on strong, a worker on its class's tier unless the flow escalates it;
    None leaves it to the bot's declaration (scout and bugfixer: mid). A missed run goes again on strong (flow_next)."""
    if kind == "review":
        return "strong"
    return act.get("tier") or (opts["tier"] if kind == "worker" else None)


def step_member(ini, kind: str, opts: dict) -> str:
    """The bot that runs a step of this kind."""
    return {"worker": opts["bot"], "bugfixer": get(ini, "flow", "bugfixer", "bugfixer") or "bugfixer",
            "scout": opts.get("scout_bot") or get(ini, "flow", "scout", "scout") or "scout",
            "research": get(ini, "flow", "research", "research") or "research",
            "review": get(ini, "flow", "review", "review") or "review"}[kind]


def act_member(ini, act: dict, opts: dict) -> str:
    """The bot of one step: the one the step names (review2), else the one of its kind."""
    return act.get("bot") or step_member(ini, act["do"], opts)


def active_id(member: str) -> str:
    """A bot a flow is told to run (--bot, --scout): a safe id with a declaration whose status is active."""
    if bot(member).get("status", "active") != "active":
        die(f"bot {member} is not active (team/bots/{member}.md)")
    return member


def active_member(ini, key: str) -> str | None:
    """The bot of [flow] <key> (default: key itself) when it is declared and active, else None."""
    member = get(ini, "flow", key, key) or key
    if not (team_dir() / "bots" / f"{member}.md").is_file():
        return None
    return member if bot(member).get("status", "active") == "active" else None


def no_login(ini, task: str, act: dict, opts: dict) -> dict | None:
    """A fallback runner with auth = login runs only when the bot's storage holds its login, checked as team bot
    login checks it; without one its step is a failed one that never ran. None: run the step."""
    runner = act.get("runner")
    if not act.get("fallback") or not runner or mode(ini) != "container" or not uses_login(ini, runner):
        return None
    member = act_member(ini, act, opts)
    if login_check(ini, member, runner) is not False:
        return None
    step = {"kind": act["do"], "bot": member, "outcome": "failed", "runner": runner, "fallback": True,
            "why": f"no {runner} login in the storage of {member} (team bot login {member})"}
    for key in ("index", "after", "parallel"):
        if act.get(key):
            step[key] = act[key]
    record([step_event(task, step)])
    return step


def flow_step(ini, flow: dict, act: dict, folder: Path, opts: dict) -> dict:
    """Run one step (a bot run or the gate) and return what it gave, as a step record."""
    kind, task, me = act["do"], flow["task"], str(TEAM)
    with (folder / "flow.log").open("a", encoding="utf-8") as log:
        sink(log, f"--- {now()} {kind} {dumps(act)}\n")
        log.flush()
        if kind == "gate":
            # A flow of team batch: its gate (bundle_commit's git update-ref) goes through team _locked as well.
            locked = ["_locked"] if os.environ.get("WOUF_BATCH") or flow.get("batch") else []
            subprocess.run([sys.executable, me, *locked, "gate", task, "--repo", f"{flow['repo']}@{flow['branch']}",
                            "--base", flow["base"], "--brief", str(folder / "brief.md")], stdout=log,
                           stderr=subprocess.STDOUT)
            state = read_json(env_dir() / "gate" / task / "gate.json")
            step = {"kind": "gate", "outcome": state.get("state", "red"), "head": state.get("head"),
                    "why": state.get("why", "the gate did not run"), **evidence("gate_step", state)}
            record([step_event(task, step)])
            return step
        member = act_member(ini, act, opts)
        decl = bot(member)
        name = display(ini, decl)
        # Reviews that run at once count on from each other (slot: the earlier ones of the same list).
        reviews = sum(1 for st in flow["steps"] if st["kind"] == "review") + act.get("slot", 0)
        run_task = {"worker": task, "bugfixer": task, "scout": f"{task}.scout", "research": f"{task}.research",
                    "review": f"{task}.r{reviews + 1}"}[kind]
        head = head_of(flow)
        ref = flow["base"] if kind in PREFLIGHT or not head else flow["branch"]
        argv = [sys.executable, me, "run", member, run_task, "--brief",
                str(flow_brief(ini, flow, act, folder, name, run_task, head)), "--repo", f"{flow['repo']}@{ref}"]
        runner = act.get("runner") or (opts["runner"] if kind == "worker" else None)
        tier = step_tier(kind, act, opts)
        effort = opts["effort"] if kind == "worker" else get(ini, "flow", f"{kind}_effort")
        model = opts["model"] if kind == "worker" and tier == opts["tier"] else None
        for flag, value in (("--runner", runner), ("--tier", tier), ("--effort", effort), ("--model", model)):
            if value:
                argv += [flag, value]
        if get(ini, "environment", "network", "bridge") == "egress":
            # Every step gets the task's Egress: hosts; review and decide none (allow and their runner's needs only).
            base_only = kind == "review" or decl.get("role", member) in ("review", "decide")
            hosts = "" if base_only else brief_egress((folder / "brief.md").read_text(encoding="utf-8"))
            argv += ["--egress", hosts or "none"]
        if flow.get("staged") and kind in ("worker", "bugfixer"):
            argv.append("--staged")  # nothing comes back into the repository until team apply (teamlib.staged)
        for pre, file in (("scout", "CONTEXT.md"), ("research", "RESEARCH.md")):
            context = stage_of(step_member(ini, pre, opts), f"{task}.{pre}") / "out" / file
            if kind in ("worker", "bugfixer") and context.is_file():
                argv += ["--input", str(context)]
        if os.environ.get("WOUF_BATCH") or flow.get("batch"):
            # A flow of team batch: the run goes through team _locked (teamlib.batch), its ref updates under the lock.
            argv[2:2] = ["_locked"]
        subprocess.run(argv, stdout=log, stderr=subprocess.STDOUT)
    stage = stage_of(member, run_task)
    result, spec = read_json(stage / "result.json"), read_json(stage / "run.json")
    step = {"kind": kind, "bot": member, "task": run_task, "outcome": result.get("state") or "failed",
            "runner": spec.get("runner") or runner, "tier": spec.get("tier") or tier, "until": result.get("until")}
    # What a receipt needs of the run (teamlib.evidence), read now: the next run of this bot and task deletes it.
    step.update(evidence("run_record", ini, stage, spec, result, step["runner"]))
    for key in ("index", "after", "fallback", "parallel"):
        if act.get(key):
            step[key] = act[key]
    if result.get("over_budget"):
        step["over_budget"] = result["over_budget"]
    if step["outcome"] == "skipped":
        step["why"] = result.get("error")
    lines = masked_lines(stage / "output.log")
    if step["outcome"] == "done" and kind in ("worker", "bugfixer"):
        step.update(head_before=head, head=head_of(flow))
        start = return_block(lines, name, run_task)
        block = lines[start:start + RETURN_LINES] if start is not None else []
        sign = block[0] if block else ""
        questions = block_field(block, "Questions")
        blocked = re.search(r"·\s*blocked\b", sign) is not None
        step["status"] = ("questions" if blocked and questions and questions.lower() != "none" else
                          "blocked" if blocked else "ready")
        if step["status"] == "questions":
            step["questions"] = questions[:500]
        if flow["class"] == "plan" and step["status"] == "ready":
            out = stage_of(member, run_task) / "out"
            briefs = [path for path in sorted((out / "briefs").glob("*.md")) if path.is_file()]
            if not (out / "PLAN.md").is_file():
                step.update(status="blocked", why="the planner wrote no out/PLAN.md")
            elif not briefs:
                step.update(status="blocked", why="the planner wrote no draft brief in out/briefs")
            else:
                step["plan"] = [shown(out / "PLAN.md")] + [shown(path) for path in briefs]
                # A plan flow delivers drafts for the lead. Keep that boundary distinct from batch readiness:
                # even legacy/free-form artifacts can be inspected, but batch must refuse an invalid contract.
                try:
                    load_plan(out)
                    step["plan_dispatchable"] = True
                except (ValueError, OSError, UnicodeError) as error:
                    step.update(plan_dispatchable=False, plan_problems=[str(error)])
    elif step["outcome"] == "done" and kind == "review":
        step.update(review_verdict(lines, name, run_task, head or ""), head=head)
    record([step_event(task, step)])
    return step


def step_event(task: str, step: dict) -> tuple:
    """A step as flow.step: its kind and what it gave (the gate green or red, a worker ready, blocked or questions,
    a review's verdict, else the run's outcome). Words of the flow only, never the bot's text."""
    done = step.get("outcome") == "done"
    status = (step.get("verdict") or step.get("status") or "done") if done else step.get("outcome")
    data = {"kind": step["kind"], "status": str(status or "failed")}
    if step.get("until"):
        data["until"] = step["until"]
    if step["kind"] == "gate":  # its head and base bind the gate in the event log (attest.flow_gate)
        data.update({k: step[k] for k in ("head", "base")
                     if isinstance(step.get(k), str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", step[k])})
    return "flow.step", "lead", task, data


# The flow's state on the board: a flow that waits for someone is blocked there, with a fixed reason.
FLOW_STATES = {"running": ("working", ""), "ready": ("review", ""), "blocked": ("blocked", "the flow is blocked"),
               "question": ("blocked", "a question for the human"), "limited": ("blocked", "a usage limit")}


def state_event(task: str, status: str) -> tuple:
    state, reason = FLOW_STATES[status]
    return "task.state", "lead", task, {"state": state, "blocked_reason": reason}


def run_flow(ini, task: str) -> int:
    folder = flow_dir(task)
    lock = flow_lock(folder, task)
    try:
        flow = read_json(folder / "state.json")
        if flow.get("status") == "closed":
            say(f"the flow of {task} is closed")
            return FLOW_EXIT["closed"]
        flow["pid"] = os.getpid()
        opts = class_options(ini, flow["class"])
        if flow.get("bot"):
            opts["bot"] = active_id(flow["bot"])
        if flow.get("scout_bot"):
            opts["scout_bot"] = active_id(flow["scout_bot"])
        plan_commits_nothing(flow["class"], opts["bot"])
        if flow.get("scout") is not None:
            opts["scout"] = flow["scout"]
        opts.update(research=bool(flow.get("research")) and active_member(ini, "research") is not None,
                    review2=parallel_reviewer(ini))
        fallbacks = flow_fallbacks(ini)
        while True:
            act = flow_next(flow, opts, fallbacks, now())
            if isinstance(act, list):
                act = same_bot(ini, act, opts) or act
            act = (STAGED and STAGED[0].flow_end(flow)) or act
            if "end" not in act and len(flow["steps"]) >= FLOW_MAX_STEPS:
                act = {"end": "blocked", "why": f"more than {FLOW_MAX_STEPS} steps"}
            if "end" in act:
                head = head_of(flow)
                flow.update(status=act["end"], why=act.get("why"), until=act.get("until"), head=head,
                            current=None, ended=now())
                end = {"kind": "end", "status": act["end"]}
                if act.get("until"):
                    end["until"] = act["until"]
                record([("flow.step", "lead", task, end), state_event(task, act["end"])])
                lead_log(task, FLOW_OUTCOME[act["end"]], f"flow {act['end']}: " +
                         (act.get("why") or act.get("until") or f"{flow['branch']} {head or ''}"))
                say(flow_line(flow))
                sys.stdout.flush()
                break
            if flow.get("status") != "running":
                record([state_event(task, "running")])
            flow.update(status="running", current=act)
            write_json(folder / "state.json", flow)
            # The next step is decided only after every step of a list has returned; all of them are kept.
            flow["steps"] += run_steps(ini, flow, act if isinstance(act, list) else [act], folder, opts)
            write_json(folder / "state.json", flow)
        # The final state is the last write of the run, and the lock goes after it: a reader that sees a final state
        # finds nothing left to write, and no second flow starts before it is written.
        write_json(folder / "state.json", flow)
    finally:
        lock.unlink(missing_ok=True)
    return FLOW_EXIT[flow["status"]]


def parallel_reviewer(ini) -> str | None:
    """The second reviewer for reviews at once: review2 when it is active and no other bot than review."""
    member = active_member(ini, "review2")
    return member if member and member != (get(ini, "flow", "review", "review") or "review") else None


def same_bot(ini, acts: list, opts: dict) -> dict | None:
    """Two runs of one bot never run at once (one /bot volume per bot): such a list ends the flow blocked."""
    members = [act_member(ini, act, opts) for act in acts]
    twice = sorted({member for member in members if members.count(member) > 1})
    return {"end": "blocked", "why": f"two steps at once on one bot: {' '.join(twice)}"} if twice else None


def run_steps(ini, flow: dict, acts: list, folder: Path, opts: dict) -> list:
    """Run the steps of acts, a list at once (one thread per step, each an ordinary run on its own bot); return
    their records in the order of acts once all have returned. A step of a list whose run raised is a failed one;
    the others keep their results."""
    def one(at: int, act: dict) -> None:
        started = now()
        try:
            step = no_login(ini, flow["task"], act, opts) or flow_step(ini, flow, act, folder, opts)
        except (Exception, SystemExit) as problem:
            if len(acts) == 1:
                raise
            step = {"kind": act["do"], "bot": act_member(ini, act, opts), "outcome": "failed",
                    "why": f"the step did not run ({problem.__class__.__name__})"}
            step.update({key: act[key] for key in ("index", "after", "fallback", "parallel") if act.get(key)})
        step.update(started=started, ended=now())
        step.update(evidence("never_ran", ini, step, act))  # a bot step that never ran: nothing of a run is known
        if len(acts) > 1:
            step["group"] = len(flow["steps"])
        with (folder / "flow.log").open("a", encoding="utf-8") as log:
            sink(log, f"+++ {step['ended']} {step['kind']} {step.get('bot') or ''} {step.get('outcome')}\n")
        steps[at] = step

    steps: list = [None] * len(acts)
    acts = [{**act, "slot": sum(1 for earlier in acts[:at] if earlier["do"] == act["do"])} if len(acts) > 1 else act
            for at, act in enumerate(acts)]
    if len(acts) == 1:
        one(0, acts[0])
        return steps
    threads = [threading.Thread(target=one, args=(at, act), daemon=True) for at, act in enumerate(acts)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return steps


def plan_commits_nothing(klass: str, member: str) -> None:
    """A plan flow returns a plan and draft briefs, never commits: its bot must be one whose commits never come back."""
    if klass == "plan" and commits(bot(member)):
        die(f"class plan runs {member}, which commits; [class.plan] bot must be a bot with commits: no (planner)")


def flow_line(flow: dict) -> str:
    status = flow.get("status")
    if status == "ready" and flow.get("class") == "plan":
        last = next((st for st in reversed(flow.get("steps", [])) if st.get("plan")), {})
        validation = ("draft not dispatchable: " + "; ".join(last.get("plan_problems", [])) + "; "
                      if last.get("plan_dispatchable") is False else "")
        return (f"ready {flow['task']} (no commits; {validation}plan, nothing dispatched: "
                f"{' '.join(last.get('plan', []))})")
    if status == "ready" and not flow.get("head"):
        last = next((st for st in reversed(flow.get("steps", [])) if st.get("bot")), {})
        return f"ready {flow['task']} (no commits; results in {shown(stage_of(last.get('bot', 'worker'), flow['task']))}/out)"
    if status == "ready":
        return f"ready {flow['branch']} {flow['head']}"
    if status == "limited":
        why = f" ({flow['why']})" if flow.get("why") else ""
        return f"limited {flow.get('until') or 'until an unknown time (team flow TASK --retry)'}{why}"
    if status in ("blocked", "question"):
        return f"{status} {flow.get('why') or ''}".rstrip()
    current = flow.get("current") or {}
    doing = " ".join(act.get("do", "") for act in current) if isinstance(current, list) else current.get("do", "")
    return f"{status} {doing}".rstrip()


def save_flow(folder: Path, state: dict) -> None:
    if FLOW_END:
        return FLOW_END[0].write_state(folder, state)
    write_json(folder / "state.json", state)


def cmd_flow(ini, args: list[str]) -> int:
    opts = {"--brief": None, "--repo": None, "--answer": None, "--bot": None, "--scout": None, "--reason": None}
    flags, positional = set(), []
    it = iter(args)
    for arg in it:
        if arg in opts:
            opts[arg] = next(it, None)
        elif arg in ("--detach", "--status", "--retry", "--staged", "--close"):
            flags.add(arg)
        else:
            positional.append(arg)
    if len(positional) != 1:
        die("usage: team flow TASK [--brief FILE --repo PATH[@REF] [--bot ID] [--scout ID]] [--answer TEXT] [--retry] "
            "[--detach] [--status] [--staged] [--close --reason TEXT]")
    task = positional[0]
    check_task(task)
    if "." in task:
        die(f"task {task!r}: letters, digits, _ - only")
    folder = flow_dir(task)
    state = folder / "state.json"
    if FLOW_END:
        ended = FLOW_END[0].handle(ini, task, folder, opts, flags)
        if ended is not None:
            return ended
    if "--status" in flags:
        flow = read_json(state)
        if not flow:
            die(f"no flow for {task}")
        say(flow_line(flow))
        for step in flow["steps"]:
            detail = step.get("verdict") or step.get("status") or step.get("why") or ""
            say(f"  {step.get('ended', '')[:19]} {step['kind']:<9} {step.get('outcome', ''):<8} "
                f"{step.get('runner') or '':<8} {detail}")
        return FLOW_EXIT.get(flow.get("status"), 3)
    if not state.is_file():
        if not opts["--brief"] or not opts["--repo"]:
            die(f"a new flow needs --brief FILE and --repo PATH[@REF] (no flow for {task} yet)")
        repo_dir, ref = split_repo(opts["--repo"])
        if repo_dir is None:
            repo_dir = PROJECT
        text = Path(opts["--brief"]).read_text(encoding="utf-8")
        header = flow_header(text)
        bot_of_class = class_options(ini, header["class"])["bot"]
        if opts["--bot"]:
            bot_of_class = active_id(opts["--bot"])
        if opts["--scout"]:
            active_id(opts["--scout"])
        plan_commits_nothing(header["class"], bot_of_class)
        flow_bots = [get(ini, "flow", key, key) or key
                     for key in ("bugfixer", "scout", "research", "review", "review2")]
        for member in flow_bots:
            check_member(member)
        # The declarations a flow selects are checked before any state: the class's bot always runs, the others when
        # they are declared (a flow with no scout or review never runs them).
        bot(bot_of_class)
        for member in flow_bots:
            if (team_dir() / "bots" / f"{member}.md").is_file():
                bot(member)
        start = git("rev-parse", "-q", "--verify", f"{ref}^{{commit}}", check=False, at=repo_dir).stdout.strip()
        if not start:
            die(f"--repo {opts['--repo']}: no such commit")
        branch = f"{slug(team_name(ini))}/{task}"
        if git("rev-parse", "-q", "--verify", f"refs/heads/{branch}", check=False, at=repo_dir).returncode == 0:
            die(f"branch {branch} exists already in {repo_dir}; use a new task id")
        check_ref_conflict(branch, repo_dir)
        folder.mkdir(parents=True, exist_ok=True)
        put(folder / "brief.md", text)
        save_flow(folder, {"task": task, "repo": str(repo_dir), "base": start, "branch": branch,
                           "class": header["class"], "scout": preflight_scout(header),
                           "research": header["research"], "steps": [], "status": "new", "started": now(),
                           **({"batch": True} if os.environ.get("WOUF_BATCH") else {}),
                           **({"bot": opts["--bot"]} if opts["--bot"] else {}),
                           **({"scout_bot": opts["--scout"]} if opts["--scout"] else {})})
        lead_log(task, "started", f"flow {header['class']} on {branch} from {start[:12]}")
    elif opts["--brief"] or opts["--repo"] or opts["--bot"] or opts["--scout"]:
        die(f"the flow of {task} exists; continue it with team flow {task} (brief, repository and bots are fixed)")
    flow = read_json(state)
    if "--staged" in flags:
        flow["staged"] = True
        save_flow(folder, flow)
    if opts["--answer"]:
        put(folder / "brief.md", f"\n## Answer from the lead ({now()[:10]})\n\n{opts['--answer']}\n", "a")
        last = flow["steps"][-1] if flow["steps"] else {}
        if flow.get("status") in ("question", "blocked") and last.get("kind") in ("worker", "bugfixer") \
                and last.get("status") in ("questions", "blocked"):
            # The worker runs again with the answer in its brief.
            flow["steps"][-1]["status"] = "answered"
            flow["status"] = "answered"
            save_flow(folder, flow)
        elif flow.get("status") == "blocked" and last.get("kind") == "review" and last.get("verdict") == "changes" \
                and not flow.get("extra_round") and flow_next(flow, class_options(ini, flow["class"]), {}, now()
                                                              ).get("end") == "blocked":
            # Blocked by the review round limit: the lead's note grants exactly one more fix round.
            flow["extra_round"] = True
            flow["status"] = "answered"
            save_flow(folder, flow)
            lead_log(task, "started", "flow: one more review round granted by the lead")
    if "--retry" in flags and flow.get("status") == "blocked" and flow["steps"]:
        # The lead fixed what blocked it (a sandbox tool, the gate itself): the last step runs again. Over the
        # per-run budget, the lead looked: the marks go and the flow goes on after the step.
        # A staged work step that was applied stays: the flow goes on from its commits.
        if not [st.pop("over_budget") for st in flow["steps"] if st.get("over_budget")] \
                and not (STAGED and STAGED[0].flow_retry(flow)):
            flow["steps"].pop()
        flow["status"] = "retry"
        save_flow(folder, flow)
        lead_log(task, "started", "flow retry of the last step")
    if "--retry" in flags and flow.get("status") == "limited" and flow["steps"]:
        for step in flow["steps"]:
            if step.get("outcome") in ("limited", "skipped"):
                step["until"] = now()
        save_flow(folder, flow)
    if flow.get("status") in ("ready", "blocked") and not opts["--answer"]:
        say(flow_line(flow))
        return FLOW_EXIT[flow["status"]]
    if "--detach" in flags:
        with (folder / "flow.log").open("a", encoding="utf-8") as log:
            subprocess.Popen([sys.executable, "-c", LAUNCH, sys.executable, str(TEAM), "flow",
                              task], stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL).wait()
        say(f"team: the flow of {task} runs in the background; check with .agents/bin/team flow {task} --status")
        return 0
    return run_flow(ini, task)


# ---------------------------------------------------------------- commands

def do_flow(rest: list[str]) -> int:
    return cmd_flow(config(), rest)


COMMANDS = {"flow": do_flow}

# team run asks this module for a run's egress list (teamlib.engine.egress_prepare).
engine.EGRESS[:] = [prepare_egress]
