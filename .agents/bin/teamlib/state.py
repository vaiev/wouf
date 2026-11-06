"""State commands: tasks, questions, decisions, merge proposals and the clock written as events into the store as they
happen, and the three Markdown exports (board.md, decisions.md, schedule.md) projected from it after every write.

The append-only event log is the truth, the SQLite cache serves reads, the Markdown is only projected (owner Q1). The
hash of each export written is kept in team/env/state-exports.json; a file that is neither that nor the projection
before the write (before the first export: nor the empty template) was edited by hand: it is never overwritten.
`team state import FILE` offers board.md's edits as events; a hand edit to generated text (the Updated cell, the
template's prose) is listed with old and new text and refuses the import until reverted or `--force`, which says
what it discards. decisions.md and schedule.md are not imported field by field; `team state diff FILE` shows their
difference. Reads go through bin/log's projection only. A proposal records a ready flow's head; nothing
here merges.
"""
from __future__ import annotations

import contextlib
import copy
import difflib
import hashlib
import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timezone
from importlib.machinery import SourceFileLoader
from pathlib import Path

from teamlib.core import (KNOWN, PACK, case_twin, die, env_dir, mask_secrets, member_problem, put, read_json, say,
                          secret_in, task_problem, team_dir, visible, write_json)
from teamlib import run

USAGE = """
  team task new TITLE --class C [--owner ID] [--id T]    a task in the store with the next free id (T-<n>), status todo
  team task start|block|review|done T [--reason TEXT] [--gate TEXT] [--artifacts A...]
                                                its status (working, blocked, review, done); block needs --reason
  team question add T "text" [--from ID]        an open question for the human, kept with its sender (Q-<n>)
  team question answer Q-n "text"               the answer; the question leaves the board
  team decision add "title" --options A... --why TEXT --risk TEXT --authority lead|owner|decide [--task T]
                    [--next TEXT]               a decision record (D-<n>); authority owner waits for the human
  team decision approve|reject D-n [--note TEXT]   the human's call on a pending decision
  team merge propose T --head SHA               record a ready flow's head, branch and review verdict; never merges
  team merge mark T --head SHA                  flag the proposed head for the owner; refused for any other head
  team schedule add WHEN "what" --owner ID --kind reminder|deadline|routine [--ending standing-yes|draft]
                                                a clock entry (S-<n>): ISO time with zone or a cron line
  team schedule done S-n                        the entry is done; it leaves schedule.md, the store keeps it
  team state import FILE [--apply [--force]]    rows of a hand-edited board.md that differ from the store; --apply
                                                writes them as events, all or nothing; --force discards the hand
                                                edits to generated text (Updated, the template's prose) and says so
  team state diff FILE                          how a hand-edited board.md, decisions.md or schedule.md differs
                                                from the store (read only)
"""

# bin/team's CLASSES; a module cannot import bin/team (tests/test_state_cmd.py keeps the two equal).
CLASSES = ("mechanical", "standard", "complex", "security", "decision", "analysis", "plan")
STATES = {"start": "working", "block": "blocked", "review": "review", "done": "done"}
STATUSES = ("todo", "working", "blocked", "review", "done")
AUTHORITY = {"owner": "human", "lead": "standing yes lead", "decide": "decide bot within the rules (autonomy = auto)"}
DECIDER = {"owner": "human", "lead": "lead", "decide": "decide"}
KINDS = ("reminder", "deadline", "routine")
ENDINGS = ("standing-yes", "draft")
# Length caps of one field; an event line stays far below bin/log's 4096 bytes.
CAP = {"title": 200, "text": 500, "reason": 200, "gate": 100, "artifacts": 300, "option": 200, "why": 500,
       "risk": 500, "next": 200, "note": 300, "when": 60, "branch": 100}
MAX_OPTIONS = 5
EXPORTS = ("board.md", "decisions.md", "schedule.md")
HASHES = "state-exports.json"
QID = re.compile(r"Q-([0-9]{1,9})")
DID = re.compile(r"D-([0-9]{1,9})")
SID = re.compile(r"S-([0-9]{1,9})")
HEX = re.compile(r"[0-9a-f]{7,40}")
DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
CRON_WORD = re.compile(r"[0-9A-Za-z*/,?#LW-]{1,20}")
MARKDOWN = re.compile(r"([\\<>|\[\]`])")  # federate's set: no cell can start markup or split the row

# ---------------------------------------------------------------- the templates' prose, byte for byte

BOARD_HEAD = ("# Board\n\nThe team's state. Nothing is done until this file says so. Keep only open and recently done "
              "work here; the log keeps the history. Owners are bot ids (`worker`, `review`, `decide`, …) or "
              "`human`.\n\n| ID | Task | Owner | Status | Next gate | Artifacts | Blocked reason | Updated |\n"
              "|---|---|---|---|---|---|---|---|\n")
BOARD_EMPTY = "| | | | | | | | |\n"
BOARD_MID = ("\nStatus: `todo`, `working`, `blocked`, `review`, `done`.\n\n## Questions for the human\n\n"
             "The lead collects the bots' open questions here and asks them in one batch.\n\n")
QUESTIONS = "## Questions for the human"
BOARD_HEADER = "| ID | Task | Owner | Status | Next gate | Artifacts | Blocked reason | Updated |"
DECISIONS_HEAD = ("# Decisions\n\nOne entry per decision, newest first. Written by the lead from the decide bot's return "
                  "block (or for small calls of its own). Short: the record must survive handoffs, not retell them.\n\n"
                  "```text\nD-<n> <YYYY-MM-DD> <task id> — <decision in one line>\n  Options: <a> / <b>\n"
                  "  Why: <one or two reasons>\n  Risk: <residual risk>\n"
                  "  Authority: human | standing yes <name> | decide bot within the rules (autonomy = auto)\n"
                  "  Next: <owner>, <gate>\n```\n")
SCHEDULE_HEAD = ("# Schedule\n\nThe lead's clock: reminders, deadlines and routines. The lead reads it at every session "
                 "start (`.agents/skills/clock.md`) and reports what is due before anything else. Times in ISO format "
                 "with time zone or as a cron line.\n\n| When | What | Owner | Kind | Ending | Last run |\n"
                 "|---|---|---|---|---|---|\n")
SCHEDULE_EMPTY = "| | | | | | |\n"
SCHEDULE_TAIL = ("\n- **Kind**: `reminder` (tell the human), `deadline` (a task must be done by then), `routine` (work "
                 "that runs by itself).\n- **Ending**: for routines only, decided before the first run: `standing-yes` "
                 "(safe and reversible, the human agreed once) or `draft` (stage and stop). There is no option that "
                 "waits.\n- **Last run**: date and outcome from the owner's log line.\n\nDone reminders and deadlines "
                 "move to the board or are deleted; the log keeps the record.\n")
RECORD = re.compile(r"D-([0-9]{1,9}) ([0-9]{4}-[0-9]{2}-[0-9]{2}) (\S+) — (.+)")
STATUS_LINE = re.compile(r"(pending|approved|rejected)(?: by (\S+))?(?:; note: (.*))?")
QUESTION_LINE = re.compile(r"- (Q-[0-9]{1,9})(?: (\S+))?, from (\S+): (.*)")

# ---------------------------------------------------------------- the store (bin/log, loaded once)

LOADED: list = []


def store():
    """bin/log as a module, loaded once and only when a command runs: its event writer and its projection. Values
    team registered are masked by log too. With WOUF_TEAM_DIR set, events, receipts and the cache follow that team
    unless their own variables name them, so events and exports never land in two different teams."""
    if not LOADED:
        team = os.environ.get("WOUF_TEAM_DIR")
        if team:
            root = team_dir()
            os.environ.setdefault("WOUF_EVENTS_DIR", str(root / "events"))
            os.environ.setdefault("WOUF_LOG_DIR", str(root / "logs"))
            os.environ.setdefault("WOUF_STATE_DB", str(root / "env" / "state.sqlite"))
        loader = SourceFileLoader("wouf_state_events", str(PACK / "bin" / "log"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        log = importlib.util.module_from_spec(spec)
        loader.exec_module(log)
        log.REGISTERED[:] = sorted(set(log.REGISTERED + KNOWN), key=len, reverse=True)
        LOADED.append(log)
    return LOADED[0]


@contextlib.contextmanager
def locked():
    """One state command at a time: the next free id, the append, the exports and their hashes. Its own lock file,
    never bin/log's events/.lock, which event_append takes itself."""
    import fcntl
    folder = env_dir()
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / ".state.lock").open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


def table(db, name: str) -> list[dict]:
    cursor = db.execute(f"SELECT * FROM {name}")
    return [dict(zip([c[0] for c in cursor.description], row)) for row in cursor]


def seq_of(event_id) -> int:
    tail = str(event_id).rpartition("-")[2]
    return int(tail) if tail.isdigit() else 0


def questions_of(rows: list[dict]) -> dict:
    """The projection's question rows by Q-<n>. It keeps one row per task and sender (its key), so a sender has at
    most one open question per task; rows without a Q-<n> were not written here and are left out."""
    found = {}
    for r in rows:
        if QID.fullmatch(str(r.get("qid") or "")):
            found[r["qid"]] = {"id": r["qid"], "task": r.get("task") or "", "from": r.get("from_bot") or "",
                               "text": r.get("text") or "", "status": r.get("status") or "open"}
    return found


def snapshot(log, more: tuple = ()) -> dict:
    """Everything the commands and the exports need, read from the projection (bin/log's state_db); more names
    further tables read as they are (context and status: runs, flows)."""
    db = log.state_db()
    try:
        tasks, decisions, schedule = table(db, "tasks"), table(db, "decisions"), table(db, "schedule")
        merges = [r for r in table(db, "runs") if r.get("outcome") in ("proposed", "marked") and r.get("head")]
        questions = questions_of(table(db, "questions"))
        extra = {name: table(db, name) for name in more}
    finally:
        db.close()
    return {"tasks": tasks, "decisions": decisions, "schedule": schedule, "merges": merges, "questions": questions,
            **extra}


def last_runs(runs: list[dict]) -> dict:
    """Each bot's newest run row: from the run events (they name a runner) when it has any, else its newest receipt."""
    found = {}
    for r in sorted(runs, key=lambda r: (bool(r.get("runner")), r.get("updated") or "", seq_of(r["id"]))):
        if r.get("bot") and r.get("outcome") not in ("proposed", "marked"):
            found[r["bot"]] = r
    return found


EMPTY = {"tasks": [], "decisions": [], "schedule": [], "merges": [], "questions": {}}

# ---------------------------------------------------------------- rendering

def natural(text: str) -> list:
    return [int(part) if i % 2 else part for i, part in enumerate(re.split(r"([0-9]+)", str(text)))]


def plain(value) -> str:
    """Stored text for one line of Markdown: one line, control characters shown escaped."""
    return visible(" ".join(str(value if value is not None else "").split()))


def cell(value, empty: str = "-") -> str:
    text = MARKDOWN.sub(r"\\\1", plain(value))
    return text or empty


def row(cells: list[str]) -> str:
    return "|" + "|".join(f" {c} " if c else " " for c in cells) + "|\n"


def board_cells(task: dict) -> list[str]:
    return [cell(task["id"]), cell(task.get("title")), cell(task.get("owner")), cell(task.get("state") or "todo"),
            cell(task.get("next_gate")), cell(task.get("artifacts")), cell(task.get("blocked_reason")),
            cell((task.get("updated") or "")[:10])]


def question_line(q: dict) -> str:
    where = f" {q['task']}" if q.get("task") else ""
    return f"- {q['id']}{where}, from {plain(q.get('from'))}: {cell(q.get('text'), '')}"


def render_board(snap: dict) -> str:
    tasks = sorted(snap["tasks"], key=lambda t: natural(t["id"]))
    rows = "".join(row(board_cells(t)) for t in tasks) or BOARD_EMPTY
    open_ = sorted((q for q in snap["questions"].values() if q["status"] == "open"), key=lambda q: natural(q["id"]))
    items = "".join(question_line(q) + "\n" for q in open_) or "-\n"
    return BOARD_HEAD + rows + BOARD_MID + items


def options_of(value) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    try:
        parsed = json.loads(value or "[]")
    except ValueError:
        return [str(value)]
    return [str(v) for v in parsed] if isinstance(parsed, list) else [str(parsed)]


def status_text(d: dict) -> str | None:
    status = d.get("status")
    if status not in ("pending", "approved", "rejected"):
        return None
    text = status + (f" by {plain(d['decided_by'])}" if d.get("decided_by") and status != "pending" else "")
    return text + (f"; note: {plain(d['note'])}" if d.get("note") else "")


def record(d: dict, date: str) -> str:
    lines = [f"D-{d['id']} {date} {plain(d.get('task')) or '-'} — {plain(d.get('title'))}",
             f"  Options: {' / '.join(plain(o) for o in options_of(d.get('options')))}",
             f"  Why: {plain(d.get('why'))}", f"  Risk: {plain(d.get('risk'))}",
             f"  Authority: {AUTHORITY.get(d.get('authority') or '', plain(d.get('authority')))}",
             f"  Next: {plain(d.get('next'))}"]
    status = status_text(d)
    if status:
        lines.append(f"  Status: {status}")
    return "\n".join(lines) + "\n"


def render_decisions(snap: dict) -> str:
    """Newest first; a record's date is the day of its latest event (an approval or rejection is the day the call
    was made)."""
    found = sorted((d for d in snap["decisions"] if str(d["id"]).isdigit()), key=lambda d: -int(d["id"]))
    return DECISIONS_HEAD + "".join("\n" + record(d, (d.get("updated") or "")[:10]) for d in found)


def schedule_cells(item: dict) -> list[str]:
    return [cell(item.get(k), "") for k in ("when", "title", "owner", "kind", "ending", "last_run")]


def open_items(snap: dict) -> list[dict]:
    return sorted((s for s in snap["schedule"] if s.get("status") != "done" and SID.fullmatch(str(s["id"]))),
                  key=lambda s: natural(s["id"]))


def render_schedule(snap: dict) -> str:
    rows = "".join(row(schedule_cells(s)) for s in open_items(snap)) or SCHEDULE_EMPTY
    return SCHEDULE_HEAD + rows + SCHEDULE_TAIL


RENDER = {"board.md": render_board, "decisions.md": render_decisions, "schedule.md": render_schedule}

# ---------------------------------------------------------------- writing

def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def describe(line: bytes | None) -> str:
    if line is None:
        return "(absent)"
    return '"' + visible(line.rstrip(b"\r\n").decode("utf-8", errors="replace")) + '"'


def line_ending(line: bytes | None) -> str:
    if line is None:
        return "(absent)"
    text = line.rstrip(b"\r\n")
    return {b"\n": "LF", b"\r\n": "CRLF", b"\r": "CR", b"": "no final newline"}[line[len(text):]]


def line_pairs(expected_raw: bytes, raw: bytes) -> list[tuple[int, bytes | None, bytes | None]]:
    """Each differing line: its number in raw, the expected line and the found one (None where absent)."""
    if expected_raw == raw:
        return []
    expected = expected_raw.splitlines(keepends=True)
    found = raw.splitlines(keepends=True)
    pairs = []
    matcher = difflib.SequenceMatcher(None, expected, found, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        for offset in range(max(i2 - i1, j2 - j1)):
            old = expected[i1 + offset] if i1 + offset < i2 else None
            new = found[j1 + offset] if j1 + offset < j2 else None
            pairs.append((j1 + offset + 1, old, new))
    return pairs


def line_change(old: bytes | None, new: bytes | None) -> str:
    return f"text {describe(old)} -> {describe(new)}; line ending {line_ending(old)} -> {line_ending(new)}"


def export_differences(expected_raw: bytes, raw: bytes) -> list[tuple[int, str]]:
    """Compare exact export bytes, accounting for text, whitespace, terminators and EOF."""
    return [(number, line_change(old, new)) for number, old, new in line_pairs(expected_raw, raw)]


def put_atomic(path: Path, text: str) -> str:
    """Write text through put, atomically (temp file and rename, a link at either name replaced, never followed);
    return the hash of the bytes now in the file."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    put(tmp, text, "x")
    written = digest(tmp.read_bytes())
    os.replace(tmp, path)
    return written


def export(before: dict, after: dict, imported: tuple[str, str] | None = None) -> list[Path]:
    """Regenerate the three exports from the store. A file that is neither the export last written (its hash) nor
    the projection before this write was edited by hand: it stays as it is. Only before the first export (no hash
    recorded) is the empty template taken as untouched; afterwards a file reset to it is a hand edit too. imported
    is the export name and the hash of the exact content just imported: only that file, with that content, is
    regenerated after its rows were written as events."""
    hashes = read_json(env_dir() / HASHES)
    edited = []
    for name, render in RENDER.items():
        path = team_dir() / name
        text = render(after)
        if path.is_symlink():
            edited.append(path)
            continue
        if path.exists():
            raw = path.read_bytes()
            current = digest(raw)
            matches_projection = render(before).encode("utf-8") == raw
            known = {hashes[name] if name in hashes else digest(render(EMPTY).encode())}
            exempt = imported is not None and imported[0] == name and imported[1] == current
            if not matches_projection and current not in known and not exempt:
                edited.append(path)
                continue
            if current == digest(text.encode()) and mask_secrets(text, KNOWN) == text:
                hashes[name] = current
                continue
        hashes[name] = put_atomic(path, text)
    write_json(env_dir() / HASHES, hashes)
    return edited


def strings(value, label: str):
    if isinstance(value, str):
        yield label, value
    elif isinstance(value, list):
        for v in value:
            yield from strings(v, label)
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from strings(v, k)


def secret_kind(value: str) -> str | None:
    """Why value cannot be stored: a key shape, a token-like run (bin/log refuses both) or a registered value."""
    return secret_in(value) or ("a token" if store().token_like(value) else None) or \
        ("a stored value" if mask_secrets(value, KNOWN) != value else None)


def check(log, events: list) -> None:
    """Every event validated before the first is written: a key or a registered value in any field refuses the whole
    command without repeating it; nothing reaches the store, the exports or the hashes."""
    for kind, actor, task, data in events:
        for label, value in strings(data, "field"):
            found = secret_kind(value)
            if found:
                die(f"the {label} looks like it contains a secret ({found}); nothing was written")
        try:
            log.event_validate(kind, actor, task, data, "local")
            log.event_value(data)
        except (ValueError, RecursionError):
            die(f"{kind}: a field is invalid or too long; nothing was written")
        if len(json.dumps(data, ensure_ascii=True)) > 3000:
            die(f"{kind}: the fields are too long together; nothing was written")


def transact(prepare, imported: list | None = None) -> int:
    """Under the state lock: read the store, let prepare validate and name the events and the line to print, write
    them, regenerate the exports. prepare dies on any refusal, before anything is written. imported, filled by
    prepare, names an export and the hash of the content imported: if the file still holds that content, every line
    of it is in the store now and it is regenerated even though it was edited."""
    log = store()
    imported = [] if imported is None else imported
    with locked():
        before = snapshot(log, ("phases",))
        events, line = prepare(before)
        check(log, events)
        for kind, actor, task, data in events:
            log.event_append(kind, actor, task, data)
        edited = export(before, snapshot(log), imported[0] if imported else None)
    for path in edited:
        if path.name == "board.md":
            line += (f"; {path.name} was edited by hand and not overwritten: team state import {path} "
                     "offers the edits as events")
        else:
            line += (f"; {path.name} was edited by hand and not overwritten: team state diff {path} "
                     "shows the difference")
    if imported and imported[0][0] not in {p.name for p in edited}:
        line += f"; {imported[0][0]} regenerated from the store"
    say(line)
    return 0

# ---------------------------------------------------------------- arguments and fields

def parse(args: list[str], usage: str, single=(), many=()) -> tuple[list[str], dict]:
    flags = (*single, *many)
    positional, found = [], {}
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in flags:
            if arg in found:
                die(usage)
            if arg in many:
                values = []
                while i + 1 < len(args) and args[i + 1] not in flags:
                    i += 1
                    values.append(args[i])
                if not values:
                    die(usage)
                found[arg] = values
            else:
                if i + 1 >= len(args):
                    die(usage)
                i += 1
                found[arg] = args[i]
        elif arg.startswith("--"):
            die(usage)
        else:
            positional.append(arg)
        i += 1
    return positional, found


def need(fn, *args):
    """A validator's value, or its refusal as the command's error."""
    try:
        return fn(*args)
    except ValueError as error:
        die(str(error))


def field(label: str, value: str, cap: str | None = None, empty: bool = False) -> str:
    """One line of text from the command line or a hand-edited file: whitespace collapsed, capped, no key (refused
    without repeating it)."""
    found = secret_kind(value)
    if found:
        raise ValueError(f"the {label} looks like it contains a secret ({found}); nothing was written")
    value = " ".join(value.split())
    if not value and not empty:
        raise ValueError(f"the {label} is empty")
    if len(value) > CAP[cap or label]:
        raise ValueError(f"the {label} is longer than {CAP[cap or label]} characters")
    return value


def person(value: str) -> str:
    """An owner or a sender: lead, human or a valid member id."""
    if value in ("lead", "human"):
        return value
    problem = member_problem(value)
    if problem:
        raise ValueError(problem)
    return value


def task_id(value: str) -> str:
    problem = task_problem(value)
    if problem:
        raise ValueError(problem)
    return value


def next_number(values, pattern: re.Pattern) -> int:
    return 1 + max((int(m.group(1)) for v in values if (m := pattern.fullmatch(str(v)))), default=0)


def usage_of(command: str) -> str:
    lines = [ln.strip() for ln in USAGE.strip("\n").splitlines() if ln.strip().startswith(f"team {command} ")]
    return "usage: " + " | ".join(lines)


def known_task(snap: dict, task: str) -> dict:
    found = next((t for t in snap["tasks"] if t["id"] == task), None)
    if found is None:
        die(f"{task} is not in the store; team task new, or team state import a board that has it")
    return found

# ---------------------------------------------------------------- task

def do_task(rest: list[str]) -> int:
    usage = usage_of("task")
    action, args = (rest[0], rest[1:]) if rest else ("", [])
    if action == "new":
        positional, opts = parse(args, usage, single=("--class", "--owner", "--id"))
        if len(positional) != 1 or "--class" not in opts:
            die(usage)
        title = need(field, "title", positional[0])
        klass = opts["--class"]
        if klass not in CLASSES:
            die(f"class {klass!r} is not one of {', '.join(CLASSES)}")
        owner = need(person, opts.get("--owner", "lead"))

        def prepare(snap):
            known = [t["id"] for t in snap["tasks"]]
            reserved = known + [r.get("task") or "" for r in snap["phases"]]
            reserved += [p.stem for p in (env_dir() / "lead").glob("*.md")]
            task = opts.get("--id") or f"T-{next_number([k.upper() for k in reserved], re.compile(r'T-([0-9]+)'))}"
            if "." in task:
                die("task id cannot contain a dot (flow reserves dotted ids)")
            need(task_id, task)
            if task.lower() in {k.lower() for k in known}:
                die(f"task {task} already exists")
            if task.lower() in {(r.get("task") or "").lower() for r in snap["phases"]}:
                die(f"task {task} is reserved by a goal")
            twin = case_twin(task, known)
            if task_problem(task) or twin:
                die(f"no free task id: {task} clashes with {twin}")
            return [("task.created", "lead", task, {"title": title, "class": klass, "owner": owner})], \
                f"{task} todo ({klass}, owner {owner})"
        return transact(prepare)
    if action not in STATES:
        die(usage)
    positional, opts = parse(args, usage, single=("--reason", "--gate"), many=("--artifacts",))
    if len(positional) != 1:
        die(usage)
    task, state = need(task_id, positional[0]), STATES[action]
    if (state == "blocked") != ("--reason" in opts):
        die("block needs --reason TEXT; only block takes it")
    data = {"state": state, "blocked_reason": need(field, "reason", opts["--reason"]) if state == "blocked" else ""}
    if "--gate" in opts:
        data["next_gate"] = need(field, "gate", opts["--gate"])
    if "--artifacts" in opts:
        data["artifacts"] = need(field, "artifacts", ", ".join(opts["--artifacts"]))

    def prepare(snap):
        known_task(snap, task)
        return [("task.state", "lead", task, data)], f"{task} {state}"
    return transact(prepare)

# ---------------------------------------------------------------- question

def do_question(rest: list[str]) -> int:
    usage = usage_of("question")
    action = rest[0] if rest else ""
    if action == "add":
        positional, opts = parse(rest[1:], usage, single=("--from",))
        if len(positional) != 2:
            die(usage)
        task, said = need(task_id, positional[0]), need(field, "text", positional[1])
        sender = need(person, opts.get("--from", "lead"))

        def prepare(snap):
            open_ = next((q for q in snap["questions"].values()
                          if q["status"] == "open" and (q["task"], q["from"]) == (task, sender)), None)
            if open_:
                die(f"{sender} has an open question on {task} ({open_['id']}); answer it first, the store keeps one "
                    "open question per task and sender")
            qid = f"Q-{next_number(snap['questions'], QID)}"
            # The sender is stored with the text; the board shows the text as data, after its source.
            return [("question.asked", "lead", task, {"id": qid, "text": said, "from": sender})], \
                f"{qid} open on {task}, from {sender}"
        return transact(prepare)
    if action == "answer":
        positional, _ = parse(rest[1:], usage)
        if len(positional) != 2 or not QID.fullmatch(positional[0]):
            die(usage)
        qid, said = positional[0], need(field, "text", positional[1])

        def prepare(snap):
            q = snap["questions"].get(qid)
            if q is None or q["status"] != "open":
                die(f"{qid} is not an open question")
            # The asked row's task and sender, so the projection's question row is the one answered.
            return [("question.answered", "lead", q["task"], {"id": qid, "text": said, "from": q["from"]})], \
                f"{qid} answered"
        return transact(prepare)
    die(usage)
    return 2

# ---------------------------------------------------------------- decision

def do_decision(rest: list[str]) -> int:
    usage = usage_of("decision")
    action = rest[0] if rest else ""
    if action == "add":
        positional, opts = parse(rest[1:], usage, single=("--why", "--risk", "--authority", "--task", "--next"),
                                 many=("--options",))
        if len(positional) != 1 or not {"--options", "--why", "--risk", "--authority"} <= set(opts):
            die(usage)
        authority = opts["--authority"]
        if authority not in AUTHORITY:
            die(f"--authority {authority!r} is not one of lead, owner, decide")
        if len(opts["--options"]) > MAX_OPTIONS:
            die(f"at most {MAX_OPTIONS} options")
        pending = authority == "owner"
        nxt = opts.get("--next") or ("human, approve or reject" if pending else "lead, -")
        data = {"title": need(field, "title", positional[0]),
                "options": [need(field, "option", o) for o in opts["--options"]],
                "why": need(field, "why", opts["--why"]), "risk": need(field, "risk", opts["--risk"]),
                "authority": authority,
                "next": need(field, "next", nxt),
                "status": "pending" if pending else "decided"}
        if not pending:
            data["decided_by"] = DECIDER[authority]
        task = need(task_id, opts["--task"]) if "--task" in opts else ""

        def prepare(snap):
            number = next_number([d["id"] for d in snap["decisions"]], re.compile(r"([0-9]{1,9})"))
            return [("decision.recorded", "lead", task, {"number": number, **data})], \
                f"D-{number} {data['status']} ({AUTHORITY[authority]})"
        return transact(prepare)
    if action in ("approve", "reject"):
        positional, opts = parse(rest[1:], usage, single=("--note",))
        if len(positional) != 1 or not DID.fullmatch(positional[0]):
            die(usage)
        number = int(DID.fullmatch(positional[0]).group(1))
        note = need(field, "note", opts["--note"]) if "--note" in opts else None

        def prepare(snap):
            found = next((d for d in snap["decisions"] if d["id"] == str(number)), None)
            if found is None or found.get("status") != "pending":
                die(f"D-{number} is not a pending decision ({found.get('status') if found else 'not recorded'})")
            # Title, why and next again: the projection's search text of the record is built from them.
            data = {"number": number, "title": found.get("title") or "", "why": found.get("why") or "",
                    "next": found.get("next") or "",
                    "status": {"approve": "approved", "reject": "rejected"}[action],
                    "decided_by": DECIDER.get(found.get("authority") or "", "human")}
            if note:
                data["note"] = note
            return [("decision.recorded", "lead", found.get("task") or "", data)], \
                f"D-{number} {data['status']} by {data['decided_by']}"
        return transact(prepare)
    die(usage)
    return 2

# ---------------------------------------------------------------- merge

def latest(merges: list, task: str, outcome: str) -> dict | None:
    rows = [m for m in merges if m.get("task") == task and m.get("outcome") == outcome]
    return max(rows, key=lambda m: (m.get("updated") or "", seq_of(m["id"])), default=None)


def do_merge(rest: list[str]) -> int:
    usage = usage_of("merge")
    action = rest[0] if rest else ""
    if action not in ("propose", "mark"):
        die(usage)
    positional, opts = parse(rest[1:], usage, single=("--head",))
    if len(positional) != 1 or "--head" not in opts:
        die(usage)
    task, head = need(task_id, positional[0]), opts["--head"]
    if not HEX.fullmatch(head):
        die("--head takes a commit id: 7 to 40 lowercase hex characters")
    if action == "propose":
        flow = read_json(team_dir() / "flows" / task / "state.json")
        status, flow_head = flow.get("status"), flow.get("head")
        manual = False
        if not flow:
            from teamlib import flow as flowlib
            from teamlib.core import PROJECT, config, git
            stage = run.stage_of("review", task)
            spec, result = read_json(stage / "run.json"), read_json(stage / "result.json")
            snap = snapshot(store())
            item = next((t for t in snap["tasks"] if t["id"] == task), None)
            if item and flowlib.class_options(config(), item.get("class") or "standard")["reviews"] > 1:
                die("manual delivery does not support multiple reviews; use a flow")
            branch, commit = spec.get("repo"), spec.get("commit")
            repo = spec.get("repo_dir") or str(PROJECT)
            if (item and spec.get("role") == "review" and result.get("state") == "done" and
                    isinstance(branch, str) and isinstance(commit, str) and HEX.fullmatch(commit) and
                    flowlib.class_options(config(), item.get("class") or "standard")["reviews"] == 1):
                current = git("rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}", check=False, at=repo)
                verdict = flowlib.review_verdict(run.masked_lines(stage / "output.log"), spec.get("name") or "", task, commit)
                if current.returncode == 0 and current.stdout.strip() == commit and verdict["verdict"] == "approve":
                    flow = {"status": "ready", "head": commit, "repo": repo, "branch": branch,
                            "class": item.get("class") or "standard",
                            "steps": [{"kind": "review", "index": 1, "head": commit, "verdict": "approve"}]}
                    status, flow_head, manual = "ready", commit, True
        if status != "ready":
            die(f"the flow of {task} has not ended ready ({status or 'no flow'}); nothing to propose")
        if not isinstance(flow_head, str) or not HEX.fullmatch(flow_head):
            die(f"the flow of {task} ended ready without commits; nothing to merge")
        if not flow_head.startswith(head):
            die(f"--head {head} is not the head of the flow of {task} ({flow_head[:12]})")
        from teamlib import flow as flowlib
        from teamlib.core import config, git
        branch = need(field, "branch", str(flow.get("branch") or ""))
        repo = flow.get("repo")
        if repo:
            current = git("rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}", check=False, at=repo)
            if current.returncode or current.stdout.strip() != flow_head:
                die(f"branch {branch} no longer points to the approved head")
        required = flowlib.class_options(config(), flow.get("class") or "standard")["reviews"]
        steps = flow.get("steps") or []
        if required:
            gates = [i for i, st in enumerate(steps) if st.get("kind") == "gate"]
            if not manual and (not gates or steps[gates[-1]].get("outcome") != "green" or
                               steps[gates[-1]].get("head") != flow_head):
                die("no green gate on the proposed head")
            reviews = {st.get("index", 1): st for st in steps[gates[-1] + 1 if gates else 0:]
                       if st.get("kind") == "review"}
            if any(i not in reviews or reviews[i].get("verdict") != "approve" or
                   reviews[i].get("head") != flow_head for i in range(1, required + 1)):
                die("required reviews have not approved the proposed head")
        review = {"verdict": "approve" if required else "none"}
        data = {"task": task, "head": flow_head, "branch": need(field, "branch", str(flow.get("branch") or "")),
                "verdict": str(review.get("verdict") or "none")}

        def prepare(snap):
            return [("merge.proposed", "lead", task, data)], \
                f"{task} proposed: {data['branch']} {flow_head[:12]}, review {data['verdict']}; nothing merged"
        return transact(prepare)

    def prepare(snap):
        proposal = latest(snap["merges"], task, "proposed")
        if proposal is None:
            die(f"no merge proposal for {task}; team merge propose {task} --head SHA first")
        if not proposal["head"].startswith(head):
            die(f"--head {head} is not the proposed head of {task} ({proposal['head'][:12]}); propose it again")
        data = {"task": task, "head": proposal["head"], "branch": proposal.get("branch") or "",
                "verdict": proposal.get("verdict") or "none"}
        return [("merge.marked", "lead", task, data)], \
            f"{task} {proposal['head'][:12]} marked for the owner; the human merges"
    return transact(prepare)

# ---------------------------------------------------------------- schedule

def schedule_data(when: str, what: str, owner: str, kind: str, ending: str) -> dict:
    when = field("when", when)
    try:
        moment = datetime.fromisoformat(when.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            raise ValueError(f"when {when!r} has no time zone")
    except ValueError as error:
        words = when.split()
        if "time zone" in str(error) or not (5 <= len(words) <= 6 and all(CRON_WORD.fullmatch(w) for w in words)):
            raise ValueError(f"when {when!r}: an ISO time with time zone or a cron line") from None
    if kind not in KINDS:
        raise ValueError(f"kind {kind!r} is not one of {', '.join(KINDS)}")
    if (kind == "routine") != bool(ending):
        raise ValueError("a routine needs an ending (standing-yes or draft) decided before its first run; "
                         "only routines take one")
    if ending and ending not in ENDINGS:
        raise ValueError(f"ending {ending!r} is not one of {', '.join(ENDINGS)}")
    data = {"title": field("what", what, "title"), "when": when, "owner": person(owner), "kind": kind}
    if ending:
        data["ending"] = ending
    return data


def do_schedule(rest: list[str]) -> int:
    usage = usage_of("schedule")
    action = rest[0] if rest else ""
    if action == "add":
        positional, opts = parse(rest[1:], usage, single=("--owner", "--kind", "--ending"))
        if len(positional) != 2 or not {"--owner", "--kind"} <= set(opts):
            die(usage)
        data = need(schedule_data, positional[0], positional[1], opts["--owner"], opts["--kind"],
                    opts.get("--ending", ""))

        def prepare(snap):
            sid = f"S-{next_number([s['id'] for s in snap['schedule']], SID)}"
            return [("schedule.item", "lead", "", {"id": sid, **data, "status": "open"})], \
                f"{sid} {data['kind']} {data['when']}, owner {data['owner']}"
        return transact(prepare)
    if action == "done":
        positional, _ = parse(rest[1:], usage)
        if len(positional) != 1 or not SID.fullmatch(positional[0]):
            die(usage)
        sid = positional[0]

        def prepare(snap):
            if not any(s["id"] == sid for s in open_items(snap)):
                die(f"{sid} is not an open schedule entry")
            return [("schedule.item", "lead", "", {"id": sid, "status": "done"})], f"{sid} done"
        return transact(prepare)
    die(usage)
    return 2

# ---------------------------------------------------------------- state import

def unescape(text: str) -> str:
    return re.sub(r"\\(.)", r"\1", text)


def cells_of(line: str) -> list[str]:
    """The cells of one table row, escapes undone; an escaped | stays inside its cell."""
    cells, current, i, body = [], [], 0, line.strip()
    body = body[1:] if body.startswith("|") else body
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            current.append(body[i + 1])
            i += 2
            continue
        if c == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(c)
        i += 1
    if "".join(current).strip():
        cells.append("".join(current).strip())
    return cells


def separator(line: str, cells: list[str]) -> bool:
    return set(line.strip()) <= set("|-: ") or not any(cells)


class Rows:
    """What one export holds that differs from the store: events to write, lines to show, refused lines, and other
    lines: generated text (the Updated cell, the template's prose) edited by hand, never imported, each with why."""

    def __init__(self) -> None:
        self.events, self.shown, self.refused, self.others = [], [], 0, []

    def add(self, mark: str, label: str, events: list) -> None:
        self.events += events
        self.shown.append(f"{mark} {label}")

    def refuse(self, number: int, problem: str) -> None:
        self.refused += 1
        self.shown.append(f"! line {number} refused: {problem}")

    def other(self, number: int, what: str, why: str = "") -> None:
        if why:
            self.others.append(f"line {number} ({why}) {what}")
            self.shown.append(f"! line {number} not imported ({why}): {what}")
        else:
            self.others.append(f"line {number} {what}")
            self.shown.append(f"! line {number} edited by hand, not imported: {what}")


def scan_board(lines: list[str], snap: dict) -> Rows:
    """Validate editable rows and collect the proposed events, without writing."""
    out = Rows()
    tasks = {t["id"]: t for t in snap["tasks"]}
    seen, asked, new = set(), set(), []
    in_table = in_questions = False
    next_q = next_number(snap["questions"], QID)
    blank = lambda c: "" if c == "-" else c
    for number, line in enumerate(lines, 1):
        if line == BOARD_HEADER or line == QUESTIONS:
            in_table, in_questions = line == BOARD_HEADER, line == QUESTIONS
            continue
        if in_table and line.startswith("|"):
            cells = cells_of(line)
            if separator(line, cells):
                continue
            try:
                if len(cells) != 8:
                    raise ValueError(f"{len(cells)} cells, the board has 8")
                task = task_id(cells[0])
                if task in seen:
                    raise ValueError(f"{task} is on the board twice")
                seen.add(task)
                title = field("title", cells[1])
                status = blank(cells[3]) or "todo"
                if status not in STATUSES:
                    raise ValueError(f"status {status!r} is not one of {', '.join(STATUSES)}")
                data = {"state": status, "owner": person(blank(cells[2])) if blank(cells[2]) else "",
                        "next_gate": field("gate", blank(cells[4]), empty=True),
                        "artifacts": field("artifacts", blank(cells[5]), empty=True),
                        "blocked_reason": field("reason", blank(cells[6]), empty=True)}
            except ValueError as error:
                out.refuse(number, str(error))
                continue
            old = tasks.get(task)
            if old is None:
                out.add("+", f"{task} | {title} | {status}",
                        [("task.created", "lead", task, {"title": title, "owner": data["owner"]}),
                         ("task.state", "lead", task, data)])
                continue
            stored = {"state": old.get("state") or "todo", **{k: plain(old.get(k)) for k in data if k != "state"}}
            changed = [k for k in data if data[k] != stored[k]]
            renamed = title != plain(old.get("title"))
            events = [("task.created", "lead", task, {"title": title})] if renamed else []
            changes = [f'title "{plain(old.get("title"))}" -> "{title}"'] if renamed else []
            changes += [f'{k} "{stored[k]}" -> "{data[k]}"' for k in changed]
            if changed:
                events.append(("task.state", "lead", task, data))
            if events:
                out.add("~", f"{task} {', '.join(changes)}", events)
            continue
        in_table = False
        if in_questions and line.startswith("- "):
            match = QUESTION_LINE.fullmatch(line)
            known = snap["questions"].get(match.group(1)) if match else None
            known = known if known and known["status"] == "open" else None
            try:
                task = task_id(match.group(2)) if match and match.group(2) else ""
                sender = person(match.group(3)) if match else "lead"
                said = field("text", unescape(match.group(4) if match else line[2:]))
                if known and known["id"] in asked:
                    raise ValueError(f"{known['id']} is on the board twice")
                if known and (task, sender) != (known["task"], known["from"]):
                    raise ValueError(f"{known['id']} is on {known['task'] or '-'} from {known['from']}; a new "
                                     "question goes on its own line without an id")
            except ValueError as error:
                out.refuse(number, str(error))
                continue
            if not known:
                new.append((number, task, sender, said))
                continue
            asked.add(known["id"])
            if said != plain(known["text"]):
                # Asked again under its own id: the projection's row (task and sender) takes the edited text.
                out.add("~", f'{known["id"]} text "{plain(known["text"])}" -> "{said}"',
                        [("question.asked", "lead", task, {"id": known["id"], "text": said, "from": sender})])
            continue
        if in_questions and line == "-":
            continue
    for task in sorted(set(tasks) - seen, key=natural):
        out.shown.append(f"? {task} only in the store; it stays")
    for qid, q in sorted(snap["questions"].items(), key=lambda item: natural(item[0])):
        if q["status"] == "open" and qid not in asked:
            out.add("-", f'{qid} "{plain(q["text"])}" removed from the board: answered',
                    [("question.answered", "lead", q["task"],
                      {"id": qid, "text": "removed from board.md by hand", "from": q["from"]})])
    # New questions after the removals: the store keeps one open question per task and sender.
    taken = {(q["task"], q["from"]) for q in snap["questions"].values() if q["id"] in asked}
    for number, task, sender, said in new:
        if (task, sender) in taken:
            out.refuse(number, f"{sender} has an open question on {task or '-'} already; the store keeps one per "
                               "task and sender: put both in one line")
            continue
        taken.add((task, sender))
        qid = f"Q-{next_q}"
        next_q += 1
        out.add("+", f"{qid} {task or '-'}, from {sender}: {said}",
                [("question.asked", "lead", task, {"id": qid, "text": said, "from": sender})])
    return out


def import_board(raw: bytes, snap: dict) -> Rows:
    """Render the proposed events, then account for every remaining line difference.

    Only event-derived representations are normalized (new question ids and generated
    dates). Structure and whitespace remain data to report, never silently discard.
    """
    lines = raw.decode("utf-8").splitlines(keepends=True)
    out = scan_board(raw.decode("utf-8").splitlines(), snap)
    projected = copy.deepcopy(snap)
    tasks = {t["id"]: t for t in projected["tasks"]}
    today = datetime.now(timezone.utc).isoformat()[:10]
    changed_tasks, new_questions = set(), []
    for kind, actor, task, data in out.events:
        if kind.startswith("task."):
            target = tasks.setdefault(task, {"id": task})
            target.update(data)
            target["updated"] = today
            changed_tasks.add(task)
        elif kind == "question.asked":
            qid = data["id"]
            if qid not in snap["questions"]:
                new_questions.append(qid)
            projected["questions"][qid] = {"id": qid, "task": task, "from": data["from"],
                                            "text": data["text"], "status": "open"}
        elif kind == "question.answered":
            projected["questions"][data["id"]]["status"] = "answered"
    projected["tasks"] = list(tasks.values())
    expected_raw = render_board(projected).encode("utf-8")
    if raw == expected_raw:
        return out
    found = list(lines)
    originals = {t["id"]: t for t in snap["tasks"]}
    for i, raw_line in enumerate(found):
        line = raw_line.rstrip("\r\n")
        ending = raw_line[len(line):]
        cells = cells_of(line) if line.startswith("|") else []
        if len(cells) == 8 and cells[0] in changed_tasks:
            old = originals.get(cells[0], {})
            old_date = (old.get("updated") or "")[:10] or "-"
            # A row the store does not know (an older team moving in) keeps the date it was written with.
            if cells[0] not in originals and DATE.fullmatch(cells[7]):
                old_date = cells[7]
            # Accept the pre-event date, but preserve all surrounding whitespace.
            if cells[7] == old_date:
                found[i] = re.sub(r"(?<= )" + re.escape(old_date) + r"(?= \|$)", today, line) + ending
        elif line.startswith("- ") and not QUESTION_LINE.fullmatch(line) and new_questions:
            qid = new_questions.pop(0)
            question = projected["questions"][qid]
            if line == "- " + cell(question["text"], ""):
                found[i] = question_line(question) + ending
    # Compare bytes before diffing: line terminators and EOF are part of the export.
    if "".join(found).encode("utf-8") == expected_raw:
        return out

    for number, old, new in line_pairs(expected_raw, "".join(found).encode("utf-8")):
        out.other(number, *not_imported(old, new))
    return out


TEMPLATE_LINES = set((BOARD_HEAD + BOARD_EMPTY + BOARD_MID + "-\n").encode("utf-8").splitlines())
LAST_CELL = re.compile(rb"(\| T-[0-9]+ \|.*\| )([^|]*?)( \|)")


def not_imported(old: bytes | None, new: bytes | None) -> tuple[str, str]:
    """What a line no event accounts for shows, and why it is not imported: a task row that differs only in its
    Updated cell (the store derives it), the template's prose (rewritten on every export), else a plain hand edit."""
    old_text = old.rstrip(b"\r\n") if old is not None else None
    new_text = new.rstrip(b"\r\n") if new is not None else None
    if old is not None and new is not None and line_ending(old) == line_ending(new):
        before, after = LAST_CELL.fullmatch(old_text), LAST_CELL.fullmatch(new_text)
        if before and after and (before[1], before[3]) == (after[1], after[3]):
            task = cells_of(old_text.decode("utf-8"))[0]
            return (f"Updated of {task} {describe(before[2])} -> {describe(after[2])}",
                    f"derived: the store sets it to the day of {task}'s latest event")
    if old_text in TEMPLATE_LINES or (old is None and not new_text.startswith((b"|", b"- "))):
        return line_change(old, new), "template prose: board.md's prose is the template's, rewritten on every export"
    return line_change(old, new), ""


def do_state(rest: list[str]) -> int:
    usage = usage_of("state")
    if not rest or rest[0] not in ("import", "diff"):
        die(usage)
    action, args = rest[0], rest[1:]
    apply = action == "import" and "--apply" in args
    force = "--force" in args
    if force and not apply:
        die(usage)
    positional, _ = parse([a for a in args if a not in ("--apply", "--force")], usage)
    if len(positional) != 1:
        die(usage)
    path = Path(positional[0])
    name = path.name
    if name not in RENDER:
        die(f"{name}: team state import reads board.md; team state diff reads board.md, decisions.md or schedule.md")
    try:
        raw = path.read_bytes()
    except OSError:
        die(f"cannot read {path}")
    if action == "diff":
        expected = RENDER[name](snapshot(store())).encode("utf-8")
        differences = export_differences(expected, raw)
        if not differences:
            say(f"{name}: the same as the store")
        else:
            shown = difflib.unified_diff(raw.decode("utf-8", errors="replace").splitlines(),
                                         expected.decode("utf-8").splitlines(),
                                         f"{name} (file)", f"{name} (store)", lineterm="")
            for line in shown:
                say(visible(line))
            for number, detail in differences:
                say(visible(f"! line {number} edited by hand: {detail}"))
        return 0
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        die(f"{path}: invalid UTF-8; cannot import; nothing written")
    if name != "board.md":
        die(f"{name} is not imported: a hand edit is never overwritten and its rows are not read back; "
            f"team state diff {path} shows the difference, and the state commands change the record")

    def summary(rows: Rows, verb: str) -> str:
        return f"{name}: {len(rows.events)} events {verb}, {rows.refused} lines refused, {len(rows.others)} other lines"

    if not apply:
        rows = import_board(raw, snapshot(store()))
        for line in rows.shown:
            say(visible(line))
        hint = (f"; team state import {path} --apply refuses until they are reverted, or --force discards them"
                if rows.others else f"; team state import {path} --apply writes them" if rows.events else "")
        say(summary(rows, "to write") + hint)
        return 0
    imported: list = []

    def prepare(snap):
        rows = import_board(raw, snap)
        for line in rows.shown:
            say(visible(line))
        # All or nothing: with a refused line nothing is written and the file stays. Other lines (generated text edited
        # by hand) refuse too, unless --force discards them, and then each one is named. Otherwise every line of the
        # file is in the store afterwards, so the file (if still exactly this content) is regenerated.
        if rows.refused:
            die(summary(rows, "to write") + "; nothing written: fix or remove those lines, then import again")
        if rows.others and not force:
            die(summary(rows, "to write") + "; nothing written: revert those lines, or import with --force, which "
                "discards them")
        for other in rows.others:
            say(visible(f"--force discards, not written: {other}"))
        imported.append((name, digest(raw)))
        kept = "; --force discarded the other lines" if rows.others else ""
        return rows.events, summary(rows, "written") + kept
    return transact(prepare, imported)


COMMANDS = {"task": do_task, "question": do_question, "decision": do_decision, "merge": do_merge,
            "schedule": do_schedule, "state": do_state}

# run, flow and status write and read the store through this module; they are kernel modules and never import it.
run.STORE[:] = [sys.modules[__name__]]
