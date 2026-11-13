"""teamlib.snapshot: the read-only interface a cockpit binds to. `team snapshot --json` prints the team's state as one
JSON object of schema wouf.cockpit.v1 (SCHEMA below); `team events --since SEQ --json` prints the events after SEQ,
one JSON object (the bin/log envelope: v, id, seq, t, origin, actor, kind, task, scope, data) per line.

Both read the store only: the state cache through bin/log's projection (refreshed or rebuilt when stale), the event
log through bin/log's validating reader, and the bots' declarations; never board.md, never a sandbox, and they write
no event. Every string leaves masked, without control characters, capped (text 2000, titles and the rest 200), with
the project as <repo>, the team folder as <team>, every other absolute path as <path> (a home path to its line end),
and the user and host names as <user> and <host>. A change of the meaning of a field is a new schema id; v1 only gains fields,
so a client ignores the fields it does not know. The task status enum also accepts closed: an explicitly
ended flow, preserved in the store with step_status closed, even if its board task remains blocked.
cli_command is the exact `team` command an owner action runs; a quoted "…" is the text the owner types.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import socket
from datetime import datetime, timezone

from teamlib.core import (KNOWN, PROJECT, config, die, dumps, frontmatter, get, mask_secrets, member_problem, say,
                          slug, task_problem, team_dir, team_name)
from teamlib.run import RESERVED, STORE

USAGE = """
  team snapshot [--task T] --json     the team's state as one JSON object (schema wouf.cockpit.v1): projects,
                                      phases, tasks, edges, bots, questions, decisions, merge proposals (read only)
  team events --since SEQ [--limit N] --json
                                      the events after SEQ, oldest first, one JSON object per line; at most N
                                      (default 200, at most 1000) (read only)
"""

SCHEMA_ID = "wouf.cockpit.v1"
CAP = {"text": 2000, "title": 200, "short": 200}
LIMIT, MAX_LIMIT = 200, 1000
STR, NSTR, INT = {"type": "string"}, {"type": ["string", "null"]}, {"type": "integer"}


def obj(**fields) -> dict:
    return {"type": "object", "required": list(fields), "properties": fields, "additionalProperties": True}


def ref(name: str) -> dict:
    return {"type": "array", "items": {"$ref": f"#/$defs/{name}"}}


SECTIONS = {"projects": "project", "phases": "phase", "tasks": "task", "edges": "edge", "bots": "bot",
            "questions": "question", "decisions": "decision", "merges": "merge"}
# The JSON Schema of wouf.cockpit.v1, as text for the interface document. Every object may gain fields (additive).
SCHEMA = json.dumps({
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": SCHEMA_ID,
    "type": "object",
    "required": ["schema", "generated", "team", *SECTIONS],
    "additionalProperties": True,
    "properties": {"schema": {"const": SCHEMA_ID}, "generated": {"type": "string", "format": "date-time"},
                   "team": {"$ref": "#/$defs/team"}, **{key: ref(name) for key, name in SECTIONS.items()}},
    "$defs": {
        "team": obj(name=STR, autonomy=STR, seq=INT),
        "project": obj(id=STR, name=STR),
        "phase": obj(id=STR, project=STR, task=STR, kind=STR, status=NSTR, reason=NSTR, updated=NSTR),
        "task": obj(id=STR, title=STR, **{"class": NSTR}, status={"enum": ["todo", "working", "blocked", "review",
                                                                              "done", "closed"]},
                    owner_bot=NSTR, paths={"type": "array", "items": STR}, branch=NSTR, head_sha=NSTR,
                    current_step=NSTR, step_status=NSTR, round=INT, cost_total=NSTR, updated=NSTR),
        "edge": obj(**{"from": STR}, to=STR, kind={"enum": ["depends_on", "path_conflict"]}),
        "bot": obj(id=STR, name=STR, role=STR, lifecycle_status=STR, runner=STR, tier=NSTR,
                   run_state={"enum": ["idle", "working", "limited", "offline"]}, limited_until=NSTR,
                   shared_quota_pool=NSTR, task=NSTR),
        "question": obj(id=STR, task=STR, **{"from": STR}, text=STR, status={"enum": ["open", "answered"]},
                        cli_command=NSTR),
        "decision": obj(id=STR, task=STR, title=STR, status=STR, lead_proposal=NSTR,
                        options={"type": "array", "items": STR}, cli_command=NSTR, cli_reject=NSTR),
        "merge": obj(task=STR, branch=NSTR, head_sha=STR, review_verdict=NSTR, pre_gate_status=NSTR,
                     status={"enum": ["proposed", "marked"]}, cli_command=NSTR),
    },
}, indent=2)

# ---------------------------------------------------------------- what leaves

# C0, C1, line and paragraph separators, bidi controls and zero-width characters.
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏ -‮⁠-⁩﻿]+")
# Line ends: a home path takes the rest of its line, so lines are split before the control characters go.
LINES = re.compile(r"\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]")
# Every absolute path, by one rule: a root (/ at a word start or after : or =, ~, X: before / or \, \\ or // at a
# word start) up to the next whitespace, quote, |, <, >, ), ] or line end. The project and the team folder are
# <repo> and <team> first, and what follows them stays.
ABSOLUTE = re.compile(r"(?:(?<!\w)[A-Za-z]:[/\\]|(?<!\w)\\\\|(?<![\w/])(?<!<repo>)(?<!<team>)/|(?<!\w)~)"
                      r"[^\s\"'`|<>)\]]*")
# A home or users segment, any case, anywhere in a path: the path is masked to the line end.
HOME_SEGMENT = re.compile(r"(?:^|[/\\:])(?:home|users)(?:[/\\]|$)", re.IGNORECASE)
# Account and host names that are no one's: never replaced as words.
GENERIC = {"root", "node", "user", "admin", "ubuntu", "bot", "runner", "localhost", "lead", "human", "team"}
PRIVATE: list = []


def names() -> tuple[set, set]:
    """This computer's user and host names and the docker_host alias (and its user), as words to hide."""
    users = {os.environ.get(k, "") for k in ("USER", "LOGNAME", "USERNAME")}
    try:
        users.add(getpass.getuser())
    except (Exception, SystemExit):
        pass
    hosts = set()
    try:
        full = socket.gethostname()
        hosts |= {full, full.split(".")[0]}
    except OSError:
        pass
    try:
        target = get(config(), "environment", "docker_host")
    except (Exception, SystemExit):
        target = ""
    where = target.split("://", 1)[-1].split("/", 1)[0] if target else ""
    if "@" in where:
        user, where = where.rsplit("@", 1)
        users.add(user)
    hosts.add(where.split(":", 1)[0])
    return users, hosts


def private() -> list:
    """The replacements of roots and names, longest root first, built once per process; $HOME is a home path."""
    if not PRIVATE:
        roots = {str(PROJECT): "<repo>", str(team_dir()): "<team>"}
        found = [(re.compile(re.escape(root) + r"(?![\w.-])"), mark)
                 for root, mark in sorted(roots.items(), key=lambda item: -len(item[0])) if len(root) > 1]
        home = (os.environ.get("HOME") or os.path.expanduser("~")).rstrip("/")
        if len(home) > 1 and home != "~":
            found.append((re.compile(re.escape(home) + r"(?![\w.-]).*"), "<path>"))
        found.append((None, None))
        users, hosts = names()
        for words, mark in ((users, "<user>"), (hosts, "<host>")):
            for word in sorted(w for w in words if len(w) >= 3 and w.lower() not in GENERIC):
                found.append((re.compile(r"(?<![\w-])" + re.escape(word) + r"(?![\w-])", re.IGNORECASE), mark))
        PRIVATE[:] = found
    return PRIVATE


def paths(line: str) -> str:
    """One line with every absolute path as <path>; a path with a home or users segment takes the rest of the line."""
    out, pos = [], 0
    for match in ABSOLUTE.finditer(line):
        out += [line[pos:match.start()], "<path>"]
        pos = match.end()
        if HOME_SEGMENT.search(match.group()):
            return "".join(out)
    return "".join(out) + line[pos:]


def clean(value, kind: str = "short"):
    """A string as it may leave: masked (before and after the control characters go, so no split key survives),
    one line, no private path or name, capped; the cut comes last, so a key across it is hidden whole."""
    if value is None:
        return None
    lines = []
    for line in LINES.split(mask_secrets(str(value), KNOWN)):
        line = mask_secrets(CONTROL.sub(" ", line), KNOWN)
        for pattern, mark in private():
            line = paths(line) if pattern is None else pattern.sub(mark, line)
        lines.append(line)
    text = " ".join(" ".join(lines).split())
    cap = CAP[kind]
    return text if len(text) <= cap else text[:cap - 1] + "…"


def cleaned(value):
    """Every string of a JSON value cleaned, keys too."""
    if isinstance(value, str):
        return clean(value)
    if isinstance(value, dict):
        return {clean(k): cleaned(v) for k, v in value.items()}
    if isinstance(value, list):
        return [cleaned(v) for v in value]
    return value


def emit(data) -> None:
    say(dumps(data, ensure_ascii=True, separators=(",", ":")))

# ---------------------------------------------------------------- the snapshot

def parsed(value) -> list:
    """A list the cache keeps as JSON text."""
    if isinstance(value, list):
        return value
    try:
        found = json.loads(value) if isinstance(value, str) and value.startswith("[") else []
    except ValueError:
        return []
    return found if isinstance(found, list) else []


def newest(rows: list, state) -> dict | None:
    return max(rows, key=lambda r: (r.get("updated") or "", state.seq_of(r["id"])), default=None)


def cost_of(rows: list) -> str | None:
    """The sum of the first number of each receipt's cost; None without one."""
    numbers = [re.search(r"[0-9]+(?:\.[0-9]+)?", str(r.get("cost") or "")) for r in rows]
    numbers = [float(m.group(0)) for m in numbers if m]
    return f"{sum(numbers):.2f}" if numbers else None


def read_cache() -> tuple[dict, int]:
    state = STORE[0]
    log = state.store()
    db = log.state_db()
    try:
        tables = {name: state.table(db, name) for name in ("tasks", "decisions", "runs", "flows", "claims", "phases",
                                                            "dag_edges", "questions")}
        seq = dict(db.execute("SELECT k,v FROM meta")).get("max_seq") or "0"
    finally:
        db.close()
    return tables, int(seq) if seq.isdigit() else 0


def declarations() -> list[dict]:
    found = []
    for path in sorted((team_dir() / "bots").glob("*.md")):
        try:
            decl = frontmatter(path)
        except (OSError, UnicodeDecodeError):
            continue
        member = decl.get("id", path.stem)
        if member == path.stem and not member_problem(member) and member not in RESERVED:
            found.append(decl)
    return found


def bot_rows(decls: list, runs: list, flows: dict, at: str) -> list[dict]:
    state, rows = STORE[0], []
    last = state.last_runs(runs)
    for decl in decls:
        member, run = decl["id"], last.get(decl["id"]) or {}
        runner = decl.get("runner") or "auto"
        if runner == "auto" and run.get("runner"):
            runner = run["runner"]
        lifecycle = decl.get("status") or "active"
        until, run_state = None, "idle"
        if lifecycle != "active":
            run_state = "offline"
        elif run.get("outcome") == "started":
            run_state = "working"
        elif run.get("outcome") == "limited":
            flow = flows.get(run.get("task") or "") or {}
            until = flow.get("until") if flow.get("status") == "limited" else None
            run_state = "limited" if until is None or until > at else "idle"
            until = until if run_state == "limited" else None
        rows.append({"id": clean(member), "name": clean(decl.get("name") or member), "role": clean(decl.get("role")
                     or member), "lifecycle_status": clean(lifecycle), "runner": clean(runner),
                     "tier": clean(decl.get("tier") or None), "run_state": run_state, "limited_until": clean(until),
                     "shared_quota_pool": clean(runner) if runner != "auto" else None,
                     "task": clean(run.get("task") or None)})
    return rows


def build(only: str | None) -> dict:
    state = STORE[0]
    ini = config()
    tables, seq = read_cache()
    at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    name = team_name(ini)
    project = slug(name)
    decls = declarations()
    reviewers = {d["id"] for d in decls if d.get("role") == "review"} | {"review"}
    flows = {f["id"]: f for f in tables["flows"]}
    merges = [r for r in tables["runs"] if r.get("outcome") in ("proposed", "marked") and r.get("head")]
    runs = [r for r in tables["runs"] if r.get("outcome") not in ("proposed", "marked")]
    mine = (lambda task: task == only) if only else (lambda task: True)
    natural = state.natural

    tasks = []
    task_rows = list(tables["tasks"])
    known_tasks = {t["id"] for t in task_rows}
    task_rows += [{"id": task} for task, f in flows.items()
                  if task not in known_tasks and f.get("status") == "closed"]
    for t in sorted(task_rows, key=lambda t: natural(t["id"])):
        task = t["id"]
        if not mine(task):
            continue
        flow = flows.get(task) or {}
        own = [r for r in runs if r.get("task") == task]
        merge = newest([m for m in merges if m.get("task") == task], state)
        branch, head = (merge.get("branch"), merge.get("head")) if merge else (None, None)
        if not merge:
            for r in sorted((r for r in own if r.get("runner") and r.get("ref")),
                            key=lambda r: (r.get("updated") or "", state.seq_of(r["id"]))):
                if state.HEX.fullmatch(r["ref"]):
                    head = r["ref"]
                else:
                    branch = r["ref"]
        paths = []
        for c in tables["claims"]:
            if c.get("task") == task and c.get("status") != "released":
                paths += [p for p in parsed(c.get("paths")) if isinstance(p, str) and p not in paths]
        tasks.append({"id": clean(task), "title": clean(t.get("title") or "", "title"),
                      "class": clean(t.get("class") or None),
                      "status": "closed" if flow.get("status") == "closed" else
                      t.get("state") if t.get("state") in state.STATUSES else "todo",
                      "owner_bot": clean(t.get("owner") or None),
                      "paths": [clean(p) for p in paths], "branch": clean(branch or None),
                      "head_sha": clean(head or None), "current_step": clean(flow.get("kind")),
                      "step_status": clean(flow.get("status")),
                      "round": sum(1 for r in own if r.get("runner") and r.get("bot") in reviewers
                                   and r.get("outcome") != "started"),
                      "cost_total": cost_of([r for r in own if r.get("cost")]), "updated": clean(t.get("updated"))})

    phases = [{"id": clean(p["id"]), "project": clean(project), "task": clean(p.get("task") or ""),
               "kind": clean(p.get("phase") or ""), "status": clean(p.get("state")),
               "reason": clean(p.get("reason"), "text"), "updated": clean(p.get("updated"))}
              for p in sorted(tables["phases"], key=lambda p: natural(p["id"])) if mine(p.get("task"))]
    edges = [{"from": clean(e.get("from")), "to": clean(e.get("to")), "kind": clean(e.get("kind"))}
             for e in sorted(tables["dag_edges"], key=lambda e: (natural(e.get("from")), natural(e.get("to")),
                                                                  e.get("kind") or ""))
             if not only or only in (e.get("from"), e.get("to"))]

    questions = []
    for q in sorted(state.questions_of(tables["questions"]).values(), key=lambda q: natural(q["id"])):
        if mine(q["task"]):
            questions.append({"id": clean(q["id"]), "task": clean(q["task"]), "from": clean(q["from"]),
                              "text": clean(q["text"], "text"), "status": q["status"],
                              "cli_command": f'team question answer {q["id"]} "…"' if q["status"] == "open"
                              else None})

    decisions = []
    for d in sorted(tables["decisions"], key=lambda d: natural(d["id"])):
        if not d["id"].isdigit() or not mine(d.get("task") or ""):
            continue
        number, pending = f"D-{d['id']}", d.get("status") == "pending"
        decisions.append({"id": number, "task": clean(d.get("task") or ""), "title": clean(d.get("title") or "",
                          "title"), "status": clean(d.get("status") or "decided"),
                          "lead_proposal": clean(d.get("why"), "text"),
                          "options": [clean(o) for o in parsed(d.get("options")) if isinstance(o, str)],
                          "cli_command": f"team decision approve {number}" if pending else None,
                          "cli_reject": f"team decision reject {number}" if pending else None})

    proposals = []
    for task in sorted({m["task"] for m in merges if m.get("task")}, key=natural):
        m = newest([m for m in merges if m.get("task") == task], state)
        if not mine(task) or not state.HEX.fullmatch(m["head"]) or task_problem(task):
            continue
        flow = flows.get(task) or {}
        # A flow reaches review only after a green gate: an approved head passed it.
        gate = "green" if m.get("verdict") == "approve" else flow.get("status") if flow.get("kind") == "gate" else None
        proposals.append({"task": clean(task), "branch": clean(m.get("branch") or None), "head_sha": m["head"],
                          "review_verdict": clean(m.get("verdict") or None), "pre_gate_status": clean(gate),
                          "status": m["outcome"], "cli_command": f"team merge mark {task} --head {m['head']}"
                          if m["outcome"] == "proposed" else None})

    return {"schema": SCHEMA_ID, "generated": at,
            "team": {"name": clean(name), "autonomy": clean(get(ini, "team", "autonomy", "ask")), "seq": seq},
            "projects": [{"id": clean(project), "name": clean(name)}], "phases": phases, "tasks": tasks,
            "edges": edges, "bots": bot_rows(decls, runs, flows, at), "questions": questions,
            "decisions": decisions, "merges": proposals}

# ---------------------------------------------------------------- the event feed

def feed(since: int, limit: int) -> list[dict]:
    """The events after since, oldest first, as bin/log reads them (validated and masked); malformed lines are
    left out, as the projection leaves them out."""
    log = STORE[0].store()
    found = []
    for path in sorted(log.events_dir().glob("*.jsonl")):
        events, _ = log.event_read(path)
        found += [e for e in events if e["seq"] > since]
    found.sort(key=lambda e: (e["seq"], e["t"], e["origin"]))
    return [cleaned(e) for e in found[:limit]]

# ---------------------------------------------------------------- commands

def options(rest: list[str], usage: str, takes: tuple) -> dict:
    found, i = {}, 0
    while i < len(rest):
        arg = rest[i]
        if arg == "--json" and arg not in found:
            found[arg] = True
            i += 1
        elif arg in takes and arg not in found and i + 1 < len(rest):
            found[arg] = rest[i + 1]
            i += 2
        else:
            die(f"usage:{usage}")
    if "--json" not in found:
        die(f"usage:{usage}")
    return found


def number(value: str, label: str, low: int, high: int) -> int:
    if not value.isdigit() or not low <= int(value) <= high:
        die(f"{label} takes a number from {low} to {high}")
    return int(value)


def do_snapshot(rest: list[str]) -> int:
    opts = options(rest, " team snapshot [--task T] --json", ("--task",))
    task = opts.get("--task")
    if task is not None and task_problem(task):
        die(f"--task: {task_problem(task)}")
    emit(build(task))
    return 0


def do_events(rest: list[str]) -> int:
    usage = " team events --since SEQ [--limit N] --json"
    opts = options(rest, usage, ("--since", "--limit"))
    if "--since" not in opts:
        die(f"usage:{usage}")
    since = number(opts["--since"], "--since", 0, 10 ** 12)
    for event in feed(since, number(opts.get("--limit", str(LIMIT)), "--limit", 1, MAX_LIMIT)):
        emit(event)
    return 0


COMMANDS = {"snapshot": do_snapshot, "events": do_events}
