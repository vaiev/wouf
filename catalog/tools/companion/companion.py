#!/usr/bin/env python3
"""Companion: a read-only cockpit for a WOUF team folder, and a single-file showcase export. Stdlib only."""
import argparse, base64, hashlib, hmac, html, importlib, importlib.util, json, os, re, secrets, shlex, shutil, sqlite3, stat, subprocess
import sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent

PHASES = ["INIT", "CONCEPT", "REALIZE", "VERIFY", "done"]
LOG_LIMIT = 200
SSE_CLIENTS = 8   # open /events streams at once; each one reads the team folder once a second
KEEPALIVE = 15    # seconds without a change before a comment line keeps the stream open
MAX_READ = 4 * 1024 * 1024  # bytes read from one file of the team folder; a log keeps its newest part
BOARD_COLUMNS = ("ID", "Task", "Owner", "Status", "Next gate", "Artifacts", "Blocked reason", "Updated")
# Key shapes come from the pack (bin/log: SECRET_PATTERNS, the same block as bin/team). The rules here are the
# ones that are no key shape: IPv4, key=value secrets, URLs with credentials, e-mail addresses, home or system paths.
CONTEXT_RE = re.compile(
    r"\b(?:\d{1,3}\.){3}\d{1,3}\b"
    r'|[\"\']?(?:api[_-]?key|password|secret|token)[\"\']?\s*[:=]\s*(?:"[^"\n]*"|\'[^\'\n]*\'|[^\s&#/]+)'
    r"|https?://[^/\s@]+@[^/\s]+", re.I)
# An e-mail address: a plain local part, or a quoted one as RFC 5321 allows ("jane.doe"@x, "a b"@x, "a\"b"@x), at a
# domain name or any address literal in brackets (user@[192.0.2.1], user@[IPv6:2001:db8::1], a tagged one).
EMAIL = re.compile(r'(?:"(?:[^"\\\n]|\\.)*"|(?<![\w.+-])[\w.+-]+)'
                   r"@(?:[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}|\[[^\[\]\s]+\])")
# The lead's path rule (decision 2026-10-28), the same as team snapshot's (teamlib/snapshot.py ABSOLUTE and
# HOME_SEGMENT), copied because a catalog tool does not import the pack. Every absolute path is redacted: a root is
# "/" at a word start or after ":" or "=", "~", a drive "X:" with "/" or "\", or "\\" at a word start, and the path
# runs to the next whitespace, quote, |, <, >, ) or ] or the line end. A home or users segment anywhere in it, in any
# case (/mnt/c/Users/, /var/home/, c:\users\), runs it to the line end, since a home folder's name may hold spaces;
# so do ~/ and ~user, a drive-relative home (D:Users\), D:\Documents and Settings\ and a home segment glued to a
# word (build/Users/bob). Over-redaction in prose is accepted.
ABSOLUTE = re.compile(r"(?:(?<!\w)[A-Za-z]:[/\\]|(?<!\w)\\\\|(?<![\w/])/|(?<!\w)~"
                      r"|(?<!\w)[A-Za-z]:(?=(?:home|users)[/\\])|(?<=\w)/(?=(?:home|users)/))[^\s\"'`|<>)\]]*", re.I)
HOME_SEGMENT = re.compile(r"(?:^|[/\\:])(?:home|users)(?:[/\\]|$)", re.I)
OLD_HOME = re.compile(r"[A-Za-z]:[/\\]documents and settings(?:[/\\]|$)|~[\w./-]", re.I)


def redact_paths(text):
    """text with every absolute path replaced by [path], by the rule above."""
    out, pos = [], 0
    while True:
        found = ABSOLUTE.search(text, pos)
        if not found: return "".join(out + [text[pos:]])
        end = found.end()
        if HOME_SEGMENT.search(found.group(0)) or OLD_HOME.match(text, found.start()):
            line_end = text.find("\n", end)
            end = len(text) if line_end < 0 else line_end
        out += [text[pos:found.start()], "[path]"]
        pos = end


_LOG, _BIN = [], []


def pack_log(team=None, pack=None):
    """The pack's bin/log as a module, for its SECRET_PATTERNS and token rule: next to the team folder (.agents/team
    or a team folder beside .agents), at --pack or WOUF_PACK, or in the repository this tool sits in."""
    if _LOG: return _LOG[0]
    places = [Path(pack)] if pack else []
    places += [Path(os.environ["WOUF_PACK"])] if os.environ.get("WOUF_PACK") else []
    if team: places += [Path(team).resolve().parent, Path(team).resolve().parent / ".agents"]
    places.append(HERE.parents[2] / ".agents")
    found = next((p / "bin" / "log" for p in places if (p / "bin" / "log").is_file()), None)
    if found is None:
        raise SystemExit("companion: no WOUF pack found (bin/log); pass --pack DIR or set WOUF_PACK")
    loader = SourceFileLoader("wouf_log", str(found))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("wouf_log", loader))
    loader.exec_module(module)
    _LOG.append(module)
    _BIN.append(found.parent)
    return module


def redact(value):
    if isinstance(value, dict): return {redact(k): redact(v) for k, v in value.items()}
    if isinstance(value, list): return [redact(v) for v in value]
    if not isinstance(value, str): return value
    log = pack_log()
    text = value.encode("utf-8", "replace").decode("utf-8")  # a lone surrogate from a JSON file becomes "?"
    for _, pattern in log.SECRET_PATTERNS: text = pattern.sub("[redacted]", text)
    text = log.TOKEN_LIKE.sub(lambda m: "[redacted]" if log.token_like(m.group(0)) else m.group(0), text)
    text = EMAIL.sub("[email]", text)  # before the IPv4 rule, which would break up an address literal (a@[192.0.2.1])
    text = CONTEXT_RE.sub("[redacted]", text)
    return redact_paths(text)


def strings(value):
    """Every text in a value, keys included."""
    if isinstance(value, str): return [value]
    if isinstance(value, dict): return [s for k, v in value.items() for s in strings(k) + strings(v)]
    if isinstance(value, list): return [s for v in value for s in strings(v)]
    return []


def residual(text):
    """What a last check still finds in a page about to be written: every kind, never a value."""
    log = pack_log()
    kind = log.secret_in(text)
    kinds = [f"a secret shape ({kind})" if kind else "", "an e-mail address" if EMAIL.search(text) else "",
             "a token-like word" if log.token_like(text) else "", "a private path" if ABSOLUTE.search(text) else ""]
    return ", ".join(k for k in kinds if k) or None


# ---------- reading the team folder: never through a link, regular files only, at most MAX_READ bytes ----------
NOFOLLOW, DIRECTORY = getattr(os, "O_NOFOLLOW", 0), getattr(os, "O_DIRECTORY", 0)
CLOEXEC, NONBLOCK = getattr(os, "O_CLOEXEC", 0), getattr(os, "O_NONBLOCK", 0)
FD_WALK = bool(NOFOLLOW and DIRECTORY and NONBLOCK and os.open in os.supports_dir_fd and os.listdir in os.supports_fd)


def unsafe_platform():
    """Why this system cannot read the team folder safely ("" when it can). There is no fallback by path: a check
    and then an open would race with a link swapped in between, and a FIFO would block the open."""
    return "" if FD_WALK else ("this system cannot open a file relative to its folder without following a link "
                               "(O_NOFOLLOW, O_DIRECTORY, dir_fd); companion reads nothing from the team folder here")


def open_inside(team, rel, folder=False):
    """A descriptor for team/rel, or None when it is missing, is or goes through a link, or is not a regular file
    (a folder with folder=True). Where the OS allows, each part is opened relative to the one before with
    O_NOFOLLOW, so no link can be swapped in between a check and the open; a FIFO opens without blocking."""
    if not FD_WALK: return None
    try: fd = os.open(team, os.O_RDONLY | DIRECTORY | CLOEXEC)
    except OSError: return None
    try:
        for n, part in enumerate(Path(rel).parts):
            leaf = n == len(Path(rel).parts) - 1 and not folder
            flags = os.O_RDONLY | NOFOLLOW | CLOEXEC | (NONBLOCK if leaf else DIRECTORY)
            fd, parent = os.open(part, flags, dir_fd=fd), fd
            os.close(parent)
        mode = os.fstat(fd).st_mode
        if not (stat.S_ISDIR(mode) if folder else stat.S_ISREG(mode)):
            os.close(fd)
            return None
        if not folder: os.set_blocking(fd, True)
    except OSError:
        os.close(fd)
        return None
    return fd


def names(team, rel):
    """The entries of a folder in the team folder, sorted; [] when it is missing or a link."""
    fd = open_inside(team, rel, folder=True)
    if fd is None: return []
    try: return sorted(os.listdir(fd))
    finally: os.close(fd)


def read_text(team, rel, tail=False):
    """A file's text; a larger file than MAX_READ gives "" (or its newest whole lines with tail=True)."""
    fd = open_inside(team, rel)
    if fd is None: return ""
    with os.fdopen(fd, "rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        if size > MAX_READ and not tail: return ""
        if size > MAX_READ: handle.seek(size - MAX_READ)
        data = handle.read(MAX_READ)
    if size > MAX_READ: data = data[data.find(b"\n") + 1:]
    try: return data.decode("utf-8")
    except UnicodeDecodeError: return ""


def read_json(team, rel):
    try: data = json.loads(read_text(team, rel))
    except ValueError: return {}
    return data if isinstance(data, dict) else {}


# ---------- readers ----------
def board(team):
    rows, header, questions, asking = [], None, [], False
    for line in read_text(team, "board.md").splitlines():
        if line.startswith("## "):
            asking = line.strip() == "## Questions for the human"
        elif asking and line.startswith("- ") and line.strip() != "-":
            questions.append(line[2:].strip())
        elif line.startswith("|") and not asking:
            cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip())[1:-1]]
            # The pack's columns by position: a header cell is the file's text, never a key of the state.
            if header is None: header = cells
            elif not all(set(c) <= set("-: ") for c in cells) and any(cells):
                rows.append(dict(zip(BOARD_COLUMNS, cells)))
    return rows, questions


def frontmatter(text):
    fields = {}
    if text.startswith("---\n"):
        for line in text[4:text.find("\n---", 4)].splitlines():
            key, sep, value = line.partition(":")
            if sep: fields[key.strip()] = value.split(" #", 1)[0].strip()
    return fields


def log_entries(team):
    out = []
    for name in names(team, "logs"):
        if not name.endswith(".jsonl"): continue
        for line in read_text(team, f"logs/{name}", tail=True).splitlines():
            try: e = json.loads(line)
            except ValueError: continue
            if isinstance(e, dict) and e.get("t"):
                out.append({k: str(e.get(k) or "") for k in ("t", "who", "task", "outcome", "signal")})
    return sorted(out, key=lambda e: e["t"])[-LOG_LIMIT:]


def brief_line(text, key):
    m = re.search(rf"^{key}:[ \t]*(.*?)\s*$", text, re.M)
    return m.group(1) if m else ""


def phase_of(flow, deps_open):
    kinds = {s.get("kind") for s in flow.get("steps") or []}
    if flow.get("status") == "ready": return "done"
    if "review" in kinds: return "VERIFY"
    if kinds & {"worker", "bugfixer", "gate"}: return "REALIZE"
    return "CONCEPT" if deps_open else "INIT"


def flows(team):
    found = {}
    for folder in names(team, "flows"):
        flow = read_json(team, f"flows/{folder}/state.json")
        if not flow: continue
        brief = read_text(team, f"flows/{folder}/brief.md")
        task = str(flow.get("task") or folder)
        deps = [d for d in re.split(r"[,\s]+", brief_line(brief, "Depends")) if d and d != "none"]
        raw = flow.get("steps") if isinstance(flow.get("steps"), list) else []
        # The folder's JSON may hold any type: every step value becomes text (or None), so no reader trips on it.
        steps = [{k: None if s.get(k) is None else str(s.get(k)) for k in
                  ("kind", "bot", "outcome", "verdict", "head", "until", "ended", "why")} for s in raw if isinstance(s, dict)]
        for step in steps: step["head"] = (step["head"] or "")[:12]
        flow["steps"] = steps
        found[task] = {"task": task, "class": flow.get("class"), "status": flow.get("status"),
                       "branch": flow.get("branch"), "objective": brief_line(brief, "Objective"), "depends": deps,
                       "started": flow.get("started"), "steps": steps, "_raw": flow}
    for f in found.values():
        open_deps = [d for d in f["depends"] if d in found and found[d]["status"] != "ready"]
        f["phase"] = phase_of(f.pop("_raw"), open_deps)
    return list(found.values())


def waves(items):
    known = {f["task"]: [d for d in f["depends"] if d != f["task"]] for f in items}
    done, out, left = set(), [], dict(known)
    while left:
        wave = sorted(t for t, deps in left.items() if all(d in done or d not in known for d in deps))
        if not wave:
            out.append(sorted(left)); break  # a cycle: shown as one last wave
        out.append(wave); done.update(wave)
        for t in wave: del left[t]
    return out


def bots(team, log, flow_list):
    last = {}
    for e in log: last[e["who"]] = e
    out = []
    for name in names(team, "bots"):
        if not name.endswith(".md"): continue
        decl = frontmatter(read_text(team, f"bots/{name}"))
        if not decl: continue
        member = decl.get("id") or name[:-3]
        entry = last.get(member, {})
        until = None
        for f in flow_list:
            for s in f["steps"]:
                if s.get("bot") == member and s.get("outcome") == "limited" and s.get("until"): until = s["until"]
        out.append({"id": member, "name": decl.get("name") or member, "role": decl.get("role") or "",
                    "status": decl.get("status") or "active", "state": entry.get("outcome") or "idle",
                    "task": entry.get("task") or "", "since": entry.get("t") or "",
                    "until": until if entry.get("outcome") == "limited" else None})
    return out


def federation(team):
    outbox, held = "env/federation/outbox", "import/federation"
    peers = sorted({n[:-6] for n in names(team, outbox) if n.endswith(".jsonl")} |
                   {n for n in names(team, held) if open_folder(team, f"{held}/{n}")})
    out = []
    for peer in peers:
        envs = [e for n in names(team, f"{held}/{peer}") if n.endswith(".json")
                for e in [read_json(team, f"{held}/{peer}/{n}")] if e]
        latest = max(envs, key=lambda e: str(e.get("at")), default=None)
        out.append({"peer": peer, "sent": len(read_text(team, f"{outbox}/{peer}.jsonl").splitlines()), "held": len(envs),
                    "last": {"type": latest.get("type"), "at": latest.get("at")} if latest else None})
    return out


def open_folder(team, rel):
    fd = open_inside(team, rel, folder=True)
    if fd is not None: os.close(fd)
    return fd is not None


_HUB = {}


def hub_counts(path):
    """Counts from a copy of hub.db (and its -wal) in a temporary folder: SQLite may create or change -shm and -wal
    files even read-only, and the copy keeps the committed rows of the WAL. Copied again only when either changes."""
    if not path: return {}
    files = [Path(path), Path(f"{path}-wal")]
    try: key = tuple((f.stat().st_mtime_ns, f.stat().st_size) if f.is_file() else None for f in files)
    except OSError: return {}
    if key[0] is None: return {}
    if _HUB.get(str(path), (None,))[0] == key: return _HUB[str(path)][1]
    with tempfile.TemporaryDirectory() as folder:
        try:
            for f, name in zip(files, ("hub.db", "hub.db-wal")):
                if f.is_file(): shutil.copyfile(f, Path(folder, name))
        except OSError:
            return {}
        out = read_hub(Path(folder, "hub.db"))
    _HUB[str(path)] = (key, out)
    return out


def as_text(value):
    """A hub value as text: bytes decoded with replacement, anything else through str()."""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def read_hub(copy):
    out = {}
    try:
        con = sqlite3.connect(copy)
        try:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ("work", "handoffs", "decisions"):
                if table in tables:
                    rows = con.execute(f"SELECT status, count(*) FROM {table} GROUP BY status")
                    out[table] = {as_text(status): n for status, n in rows}  # a BLOB or NULL status becomes text
            if "notes" in tables:
                out["notes"] = {"total": con.execute("SELECT count(*) FROM notes").fetchone()[0]}
        finally:
            con.close()
    except sqlite3.Error:
        return {}
    return out


QID, DID = re.compile(r"Q-[0-9]{1,9}"), re.compile(r"D-[0-9]{1,9}")
TASK_ID, HEAD = re.compile(r"T-[0-9]{1,9}"), re.compile(r"[0-9a-f]{7,40}")


def natural(text):
    return [int(part) if i % 2 else part for i, part in enumerate(re.split(r"([0-9]+)", str(text)))]


def team_events(team):
    """The team's events (team/events/<origin>.jsonl), each line checked by bin/log's own parser; a bad line is
    skipped. Read through the same no-link walk as every other file."""
    log, found = pack_log(), []
    for name in names(team, "events"):
        origin = name[:-6] if name.endswith(".jsonl") else ""
        if not re.fullmatch(r"[0-9a-f]{12}", origin): continue
        for line in read_text(team, f"events/{name}", tail=True).splitlines():
            try: found.append(log.event_parse((line + "\n").encode("utf-8"), origin))
            except (ValueError, TypeError, RecursionError, UnicodeError): continue
    return sorted(found, key=lambda e: (e["t"], e["origin"], e["seq"]))


def owner_items(team):
    """What waits for the owner: open questions, pending decisions, merge proposals (and whether one is marked)."""
    questions, decisions, merges = {}, {}, {}
    for e in team_events(team):
        kind, d, task = e["kind"], e["data"], e["task"]
        if kind == "question.asked" and QID.fullmatch(str(d.get("id"))):
            questions[d["id"]] = {"id": d["id"], "task": task, "from": str(d.get("from") or ""),
                                  "text": str(d.get("text") or "")}
        elif kind == "question.answered":
            questions.pop(d.get("id"), None)
        elif kind == "decision.recorded" and type(d.get("number")) is int:
            key = f"D-{d['number']}"
            if d.get("status") == "pending":
                options = d.get("options") if isinstance(d.get("options"), list) else []
                decisions[key] = {"id": key, "task": task, "title": str(d.get("title") or ""),
                                  "options": [str(o) for o in options]}
            else:
                decisions.pop(key, None)
        elif kind in ("merge.proposed", "merge.marked") and HEAD.fullmatch(str(d.get("head"))) \
                and TASK_ID.fullmatch(str(d.get("task"))):
            head = d["head"][:12]
            if kind == "merge.proposed":
                merges[d["task"]] = {"task": d["task"], "head": head, "branch": str(d.get("branch") or ""),
                                     "verdict": str(d.get("verdict") or ""), "marked": False}
            elif d["task"] in merges and merges[d["task"]]["head"] == head:
                merges[d["task"]]["marked"] = True
    by_id = lambda item: natural(item.get("id") or item.get("task"))
    return {"questions": sorted(questions.values(), key=by_id), "decisions": sorted(decisions.values(), key=by_id),
            "merges": sorted(merges.values(), key=by_id)}


def collect(team, hub=None, mode="live"):
    team = Path(team)
    pack_log(team)
    rows, questions = board(team)
    log = log_entries(team)
    flow_list = flows(team)
    name = re.search(r"^\[team\][^\[]*?^name\s*=\s*(\S+)", read_text(team, "config.ini"), re.M | re.S)
    state = {"mode": mode, "team": name.group(1) if name else "WOUF", "board": rows, "questions": questions,
             "bots": bots(team, log, flow_list), "flows": flow_list, "waves": waves(flow_list), "log": log,
             "federation": federation(team), "hub": hub_counts(hub), "phases": PHASES}
    if mode == "live":
        state["owner"] = owner_items(team)
    return redact(state)


# ---------- page ----------
# The page wears VAIEV UI v8, brand wouf: the files in static/ (v8/ is vendored, wouf only; cockpit.css is the
# companion's own), served by this server alone. The live page links them; an exported snapshot inlines them.
STATIC = HERE / "static"
SHEETS = ("v8/css/tokens.css", "v8/css/fonts.css", "v8/css/base.css", "v8/css/materials.css",
          "v8/css/components.css", "v8/css/patterns.css", "cockpit.css")
STATIC_TYPES = {".css": "text/css; charset=utf-8", ".woff2": "font/woff2", ".svg": "image/svg+xml",
                ".txt": "text/plain; charset=utf-8"}
# The views of the mock (v8/sites/wouf-app) that have data here; chat, map and goal and plan have none and are left out.
VIEWS = (("today", "Today", "home"), ("approvals", "Approvals", "check"), ("board", "Board", "poll"),
         ("team", "Team", "bot"))


def static_files():
    """/static/<path> for each file under static/ with a known type, found without following a link."""
    found = {}
    for folder, dirs, files in os.walk(STATIC):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(folder, d))]
        for name in files:
            path = Path(folder, name)
            if Path(name).suffix in STATIC_TYPES and not path.is_symlink():
                found["/static/" + path.relative_to(STATIC).as_posix()] = STATIC_TYPES[path.suffix]
    return found


def static_body(url):
    """The bytes of a vendored file, read without following a link (None when that fails)."""
    fd = open_inside(STATIC, url[len("/static/"):])
    if fd is None: return None
    with os.fdopen(fd, "rb") as handle:
        return handle.read(MAX_READ)


def inline_css():
    """The sheets as one text for a page that may load nothing: no fonts, no url(), no comments."""
    text = "\n".join((STATIC / s).read_text(encoding="utf-8") for s in SHEETS if not s.endswith("fonts.css"))
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"[^;{}]*url\([^)]*\)[^;{}]*;?", "", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


SCRIPT = r"""
const data0 = JSON.parse(document.getElementById('data').textContent);
const LIVE_PAGE = data0.mode === 'live', SVG = 'http://www.w3.org/2000/svg';
const panels = {}, shown = {};
// what the owner actions (ACTIONS, with --actions only) plug into the Approvals view
const ext = {approvals: null, key: () => '', hold: () => false};
let state = data0;
function h(tag, cls, text, kids) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text !== undefined && text !== null) el.textContent = String(text);
  for (const k of kids || []) if (k) el.appendChild(k);
  return el;
}
function icon(name, cls) {
  if (!LIVE_PAGE) return null;  // a snapshot loads nothing, not even the icons
  const svg = document.createElementNS(SVG, 'svg'), use = document.createElementNS(SVG, 'use');
  svg.setAttribute('class', 'icon' + (cls ? ' ' + cls : ''));
  svg.setAttribute('aria-hidden', 'true');
  use.setAttribute('href', '/static/v8/icons.svg#' + name);
  svg.appendChild(use);
  return svg;
}
function tone(word) {
  const w = String(word || '').toLowerCase();
  if (['done', 'ready', 'approve', 'approved', 'green', 'active', 'answered', 'marked', 'merged'].includes(w)) return 'ok';
  if (['blocked', 'failed', 'red', 'changes', 'rejected'].includes(w)) return 'bad';
  if (['limited', 'question', 'asked', 'proposed', 'pending'].includes(w)) return 'warn';
  if (['running', 'working', 'started', 'review'].includes(w)) return 'run';
  return '';
}
function chip(text, word) {
  const el = h('span', 'state', text), t = tone(word === undefined ? text : word);
  if (t) el.dataset.tone = t;
  return el;
}
function classChip(name) { const el = h('span', 'class-chip', name); el.dataset.class = name; return el; }
function stamp(t) { return String(t || '').slice(0, 16).replace('T', ' '); }
function section(title, count, kids) {
  return h('section', 'app-section', null, [h('div', 'app-section-head', null, [h('h2', 'title-l', title),
    count === null ? null : h('span', 'count', count)]), ...kids]);
}
function empty(text, detail) {
  let mark = null;
  if (LIVE_PAGE) { mark = h('img', ''); mark.src = '/static/v8/brand/wouf-icon.svg'; mark.alt = ''; mark.width = mark.height = 56; }
  return h('div', 'empty-state plane', null, [mark, h('p', 'headline-s', text), detail ? h('p', 'muted', detail) : null]);
}
function evidence(label, items) {
  return h('details', 'evidence', null, [h('summary', '', null, [h('span', '', label), icon('chevron-down', 'icon-s')]),
    h('ul', '', null, items.map(i => h('li', '', i)))]);
}
function countdown(until) { const el = h('p', 'countdown', left(until)); el.dataset.until = until; return el; }
function left(until) {
  const ms = Date.parse(until) - Date.now();
  if (!(ms > 0)) return 'free again';
  const m = Math.floor(ms / 60000);
  return 'limited ' + Math.floor(m / 60) + 'h ' + (m % 60) + 'm';
}
function waiting(d) {
  if (!d.owner) return (d.questions || []).length;
  const o = d.owner;
  return o.questions.length + o.decisions.length + o.merges.filter(m => !m.marked).length;
}
function run(f) {
  const steps = f.steps.map((s, i) => {
    const li = h('li', '', (s.kind || 'step') + (s.outcome ? ' · ' + s.outcome : ''));
    if (i < f.steps.length - 1) li.dataset.done = ''; else li.setAttribute('aria-current', 'step');
    if (tone(s.outcome) === 'bad' || tone(s.verdict) === 'bad') li.dataset.state = 'blocked';
    return li;
  });
  const list = steps.length ? h('ol', 'steps-live', null, steps) : null;
  if (list) list.setAttribute('aria-label', 'Steps');
  return h('article', 'run plane', null, [
    h('span', 'run-top', null, [h('span', 'task-id', f.task), f.class ? classChip(f.class) : null, chip(f.status || 'open')]),
    h('span', 'title-m', f.objective || f.task), list,
    h('span', 'meta-line', null, [h('span', '', f.phase), f.branch ? h('span', '', f.branch) : null,
      f.depends.length ? h('span', '', 'after ' + f.depends.join(', ')) : null])]);
}
function phases(d) {
  const by = Object.fromEntries(d.phases.map(p => [p, []]));
  for (const f of d.flows) (by[f.phase] || by.INIT).push(f);
  return h('div', 'pipe', null, d.phases.map(p => h('div', 'stage plane-low', null, [
    h('div', 'stage-head', null, [h('h3', 'title-s', p), h('span', 'count', by[p].length)]),
    h('div', 'cluster', null, by[p].map(f => chip(f.task + (f.class ? ' ' + f.class : ''), f.status)))])));
}
function viewToday(d) {
  const running = d.flows.filter(f => f.status !== 'ready'), n = waiting(d), out = [];
  if (d.mode === 'live') {
    const open = h('button', 'btn btn-tonal press', 'Open approvals');
    open.type = 'button';
    open.addEventListener('click', () => show('approvals'));
    out.push(section('Needs you', n, [n ? h('div', 'cluster', null, [h('p', 'body-l',
      n + (n === 1 ? ' item waits' : ' items wait') + ' for you.'), open]) : h('p', 'muted', 'Nothing waits for you.')]));
  }
  out.push(section('Running', running.length, [running.length ? h('div', 'runs', null, running.map(run)) :
    h('p', 'muted', 'No flow is running.')]));
  out.push(section('Phase gates', null, [phases(d)]));
  const log = d.mode === 'showcase' ? d.log : d.log.slice().reverse();
  out.push(section(d.mode === 'showcase' ? 'How it went' : 'Timeline', d.log.length, [log.length ?
    h('ol', 'log plane', null, log.map(e => h('li', '', null, [h('time', '', stamp(e.t)), h('span', 'what', null, [
      chip(e.who, e.outcome), h('span', '', (e.task && e.task !== '-' ? e.task + ' · ' : '') + e.outcome + ' — ' + e.signal)])]))) :
    h('p', 'muted', 'The log is empty.')]));
  return out;
}
function card(top, text, more) { return h('article', 'decision plane', null, [h('p', 'run-top', null, top), h('p', 'decision-text', text), ...more]); }
function ownerItems(o, act) {
  const out = [act ? act.note() : null, act ? act.results() : null];
  if (o.questions.length) out.push(section('Questions', o.questions.length, [h('div', 'inbox', null, o.questions.map(q =>
    card([h('span', 'task-id', q.id), q.task ? h('span', 'task-id', q.task) : null], q.text,
      [h('p', 'muted', 'from ' + q.from), act ? act.answer(q) : null]))), act ? act.answerAll(o.questions) : null]));
  if (o.decisions.length) out.push(section('Decisions', o.decisions.length, [h('div', 'inbox', null, o.decisions.map(d =>
    card([h('span', 'task-id', d.id), d.task ? h('span', 'task-id', d.task) : null], d.title,
      [d.options.length ? evidence('Options', d.options) : null, act ? act.decide(d) : null])))]));
  if (o.merges.length) out.push(section('Merge proposals', o.merges.length, [h('div', 'inbox', null, o.merges.map(m =>
    card([h('span', 'task-id', m.task), chip('review ' + m.verdict, m.verdict), m.marked ? chip('marked') : null],
      m.branch + ' at ' + m.head, [act && !m.marked ? act.mark(m) : null])))]));
  return out;
}
function viewApprovals(d) {
  const out = d.owner ? ownerItems(d.owner, ext.approvals) : [];
  if (d.owner && !ext.approvals && waiting(d)) out.unshift(h('p', 'muted',
    'Read-only. To act here, start companion with --actions and open the link it prints.'));
  if (d.questions.length) out.push(section('Questions on the board', d.questions.length, [h('div', 'inbox', null,
    d.questions.map(q => h('article', 'decision plane', null, [h('p', 'decision-text', q)])))]));
  if (!waiting(d) && !d.questions.length) out.push(empty('Nothing waits for you.',
    'When something needs your decision, it shows here.'));
  return out;
}
const ORDER = ['idea', 'todo', 'open', 'planned', 'ready', 'working', 'running', 'review', 'blocked', 'done'];
function rank(name) { const i = ORDER.indexOf(String(name).toLowerCase()); return i < 0 ? ORDER.length - 1.5 : i; }
function viewBoard(d) {
  const cols = [];
  for (const r of d.board) {
    const name = r.Status || '—';
    let col = cols.find(c => c.name === name);
    if (!col) cols.push(col = {name: name, rows: []});
    col.rows.push(r);
  }
  cols.sort((a, b) => rank(a.name) - rank(b.name));
  const kanban = h('div', 'kanban', null, cols.map(c => {
    const col = h('section', 'kcol plane-low', null, [h('div', 'kcol-head', null, [h('h3', 'title-m', c.name),
      h('span', 'count', c.rows.length)]), h('div', 'kcol-cards', null, c.rows.map(r => h('article', 'kcard plane', null, [
        h('span', 'run-top', null, [h('span', 'task-id', r.ID), chip(r.Status)]), h('span', 'kcard-title', r.Task),
        h('span', 'meta-line', null, [r.Owner ? h('span', '', r.Owner) : null, r['Next gate'] ? h('span', '', 'next ' + r['Next gate']) : null]),
        r['Blocked reason'] ? h('p', 'needs', r['Blocked reason']) : null])))]);
    col.setAttribute('aria-label', c.name);
    return col;
  }));
  return [section('Board', d.board.length, [d.board.length ? kanban : h('p', 'muted', 'The board is empty.')]),
    section('Task DAG', d.waves.length, [d.waves.length ? h('div', 'waves', null, d.waves.map((w, i) =>
      h('div', 'stage plane-low', null, [h('div', 'stage-head', null, [h('h3', 'title-s', 'wave ' + (i + 1)),
        h('span', 'count', w.length)]), ...w.map(t => {
          const f = d.flows.find(x => x.task === t) || {};
          return h('div', 'cluster', null, [chip(t, f.status), f.depends && f.depends.length ?
            h('span', 'needs', 'after ' + f.depends.join(', ')) : null]);
        })]))) : h('p', 'muted', 'No flows yet.')])];
}
function viewTeam(d) {
  const out = [section('Bots', d.bots.length, [d.bots.length ? h('div', 'inbox', null, d.bots.map(b =>
    h('article', 'decision plane', null, [h('p', 'run-top', null, [h('span', 'title-m', b.name), h('span', 'task-id', b.role)]),
      h('div', 'cluster', null, [chip(b.status), chip(b.state + (b.task ? ' ' + b.task : ''), b.state)]),
      b.since ? h('p', 'muted', 'since ' + stamp(b.since)) : null, b.until ? countdown(b.until) : null]))) :
    h('p', 'muted', 'No bots declared.')])];
  out.push(section('Federation', d.federation.length, [d.federation.length ? h('ul', 'rows plane', null,
    d.federation.map(p => h('li', '', null, [h('p', 'row', null, [h('span', 'row-label', p.peer),
      h('span', 'row-value', 'sent ' + p.sent + ' · held ' + p.held),
      h('span', 'muted', p.last ? 'last ' + p.last.type + ' · ' + p.last.at : 'nothing received')])]))) :
    h('p', 'muted', 'No peers yet.')]));
  const hub = Object.entries(d.hub || {});
  if (hub.length) out.push(section('Hub', null, [h('ul', 'rows plane', null, hub.map(([t, c]) => h('li', '', null, [
    h('p', 'row', null, [h('span', 'row-label', t), h('span', 'row-value cluster', null,
      Object.entries(c).map(([k, v]) => chip(k + ' ' + v, k)))])])))]));
  return out;
}
const VIEW = {today: viewToday, approvals: viewApprovals, board: viewBoard, team: viewTeam};
const SLICE = {today: d => [d.flows, d.log, d.owner, d.questions], approvals: d => [d.owner, d.questions],
  board: d => [d.board, d.waves, d.flows], team: d => [d.bots, d.federation, d.hub]};
const tabs = Array.from(document.querySelectorAll('[data-view]'));
function show(name) {
  for (const b of tabs) if (b.dataset.view === name) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
  for (const [k, p] of Object.entries(panels)) p.hidden = k !== name;
  document.getElementById('view-title').textContent = tabs.find(b => b.dataset.view === name).dataset.label;
  window.scrollTo(0, 0);
}
function render(d) {
  state = d;
  for (const k of Object.keys(VIEW)) {
    if (k === 'approvals' && ext.hold()) continue;
    const key = JSON.stringify(SLICE[k](d)) + (k === 'approvals' ? ext.key() : '');
    if (shown[k] === key) continue;
    shown[k] = key;
    panels[k].replaceChildren(...VIEW[k](d).filter(Boolean));
  }
  const n = waiting(d), tab = tabs.find(b => b.dataset.view === 'approvals'), dot = tab.querySelector('.dot');
  tab.setAttribute('aria-label', n ? 'Approvals, ' + n + ' open' : 'Approvals');
  if (n && !dot) { const el = h('span', 'dot'); el.setAttribute('aria-hidden', 'true'); tab.appendChild(el); }
  if (!n && dot) dot.remove();
  const live = document.getElementById('stamp');
  live.textContent = d.mode === 'showcase' ? 'snapshot' : 'live · ' + new Date().toLocaleTimeString();
  if (d.mode === 'showcase') live.dataset.state = 'snapshot';
}
for (const b of tabs) {
  const p = h('section', 'view');
  p.id = 'view-' + b.dataset.view;
  panels[b.dataset.view] = p;
  document.getElementById('app').appendChild(p);
  b.addEventListener('click', () => show(b.dataset.view));
}
setInterval(() => document.querySelectorAll('.countdown').forEach(c => c.textContent = left(c.dataset.until)), 1000);
render(data0);
show('today');
"""

# Live: the server pushes each new state on /events; while the stream is down, poll /api/state every 3 seconds.
LIVE = """
let poll = null;
const events = new EventSource('/events');
events.addEventListener('state', e => { if (poll) { clearInterval(poll); poll = null; } render(JSON.parse(e.data)); });
events.onerror = () => { if (!poll) poll = setInterval(() =>
  fetch('/api/state').then(r => r.json()).then(render).catch(() => {}), 3000); };
"""


# Owner actions (only with --actions): each button first asks the server which `team` commands it would run
# (confirm: false), shows them in the Trusted Bar, and runs them only on "Run" (confirm: true). The launch token comes
# in the URL fragment, which no request carries; the page keeps it in memory and removes it from the address bar.
ACTIONS = r"""
const token = ((location.hash.match(/^#token=([A-Za-z0-9_-]{20,100})$/) || [])[1]) || '';
if (location.hash) history.replaceState(null, '', location.pathname);
const drafts = {}, notes = {};
let sheetOpen = false, lastResults = null, cancelSheet = null;
function button(label, cls, fn) {
  const b = h('button', ('btn press ' + cls).trim(), label);
  b.type = 'button';
  b.addEventListener('click', fn);
  return b;
}
function post(path, body) {
  return fetch(path, {method: 'POST', credentials: 'omit', cache: 'no-store',
    headers: {'Content-Type': 'application/json', 'X-WOUF-Token': token}, body: JSON.stringify(body)})
    .then(r => r.json().catch(() => ({})).then(j => ({status: r.status, body: j})));
}
function refused(r) { return [{command: 'refused (' + r.status + ')', code: r.status, output: r.body.error || ''}]; }
function done(results) { lastResults = results; shown.approvals = null; render(state); }
function closeSheet() { const s = document.getElementById('sheet'); if (s) s.remove(); sheetOpen = false; cancelSheet = null; }
function ask(path, body) {
  post(path, Object.assign({}, body, {confirm: false})).then(p => {
    if (p.status !== 200) return done(refused(p));
    closeSheet();
    sheetOpen = true;
    const run = () => { closeSheet(); post(path, Object.assign({}, body, {confirm: true}))
      .then(r => done(r.status === 200 ? r.body.results : refused(r))); };
    cancelSheet = () => { closeSheet(); shown.approvals = null; render(state); };
    const go = button('Run', 'btn-allow', run);
    const sheet = h('div', 'trusted', null, [
      h('p', 'trusted-who label-m', null, [icon('lock', 'icon-s'), h('span', '', 'companion · this team folder')]),
      h('p', 'trusted-text', 'Run these commands?'),
      h('div', 'trusted-commands', null, p.body.commands.map(c => h('pre', 'out', c.command))),
      h('div', 'trusted-actions', null, [button('Cancel', '', cancelSheet), go])]);
    sheet.id = 'sheet';
    sheet.setAttribute('role', 'alertdialog');
    sheet.setAttribute('aria-label', 'Run these commands?');
    document.body.appendChild(sheet);
    go.focus();
  });
}
document.addEventListener('keydown', e => { if (e.key === 'Escape' && cancelSheet) cancelSheet(); });
ext.approvals = {
  note: () => token ? null : h('p', 'muted', 'Open the link with #token=… that companion printed to act here.'),
  results: () => lastResults ? h('div', 'results', null, lastResults.map(r =>
    h('pre', 'out', r.command + '  → exit ' + r.code + (r.output ? '\n' + r.output : '')))) : null,
  answer(q) {
    const area = h('textarea', 'input');
    area.rows = 2; area.maxLength = 500; area.placeholder = 'Your answer'; area.value = drafts[q.id] || '';
    area.setAttribute('aria-label', 'Answer to ' + q.id);
    area.addEventListener('input', () => { drafts[q.id] = area.value; });
    return area;
  },
  answerAll(qs) {
    return h('div', 'decision-actions', null, [button('Answer the filled questions', 'btn-filled', () => {
      const answers = qs.filter(q => (drafts[q.id] || '').trim()).map(q => ({id: q.id, text: drafts[q.id].trim()}));
      if (answers.length) ask('/action/answer', {answers});
    })]);
  },
  decide(d) {
    const note = h('input', 'input');
    note.placeholder = 'note (optional)'; note.maxLength = 300; note.value = notes[d.id] || '';
    note.setAttribute('aria-label', 'Note for ' + d.id);
    note.addEventListener('input', () => { notes[d.id] = note.value; });
    const body = v => Object.assign({id: d.id, verdict: v}, (notes[d.id] || '').trim() ? {note: notes[d.id].trim()} : {});
    return h('div', 'stack stack-3', null, [note, h('div', 'decision-actions', null, [
      button('Approve', 'btn-filled', () => ask('/action/decision', body('approve'))),
      button('Reject', 'btn-tonal', () => ask('/action/decision', body('reject')))])]);
  },
  mark(m) {
    return h('div', 'decision-actions', null, [button('Mark for merge', 'btn-filled', () =>
      ask('/action/merge-proposal', {task: m.task, head: m.head}))]);
  }
};
ext.key = () => JSON.stringify(lastResults) + (token ? '' : '-');
ext.hold = () => sheetOpen;
shown.approvals = null;
render(state);
"""


def digest(text):
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode("ascii") + "'"


def csp(script, connect, style=None):
    """Only this page's own script runs (by hash). The live page's styles, fonts and icons are the vendored files
    from this server ('self'); a snapshot inlines its style (by hash) and loads nothing."""
    if style is None:
        return (f"default-src 'none'; style-src 'self'; font-src 'self'; img-src 'self'; script-src {digest(script)}; "
                f"connect-src {connect}; base-uri 'none'; form-action 'none'")
    return (f"default-src 'none'; style-src {digest(style)}; script-src {digest(script)}; connect-src {connect}; "
            "base-uri 'none'; form-action 'none'")


# Sent on every response of the server, error pages included; a page may not be framed by another site.
CSP_LIVE = csp(SCRIPT + LIVE, "'self'") + "; frame-ancestors 'none'"


def csp_for(actions):
    return csp(SCRIPT + LIVE + ACTIONS, "'self'") + "; frame-ancestors 'none'" if actions else CSP_LIVE


# The showcase is a file someone opens anywhere: it may not connect to anything.
def showcase_meta():
    return f'<meta http-equiv="Content-Security-Policy" content="{csp(SCRIPT, chr(39) + "none" + chr(39), inline_css())}">'


def nav(live):
    """The Morph Bar of the mock with one item per view; a snapshot has words instead of icons (it loads nothing)."""
    items = []
    for n, (key, label, glyph) in enumerate(VIEWS):
        current = ' aria-current="page"' if n == 0 else ""
        if live:
            items.append(f'<button class="bar-item" type="button" data-view="{key}" data-label="{label}" '
                         f'aria-label="{label}"{current}><svg class="icon" aria-hidden="true"><use '
                         f'href="/static/v8/icons.svg#{glyph}"/></svg><span class="bar-label" aria-hidden="true">'
                         f'{label}</span></button>')
        else:
            items.append(f'<button class="bar-item bar-word" type="button" data-view="{key}" data-label="{label}"'
                         f'{current}>{label}</button>')
    kind = ("dock", "bar bar-labelled lens") if live else ("dock dock-wide", "bar lens")
    return f'<nav class="{kind[0]}" aria-label="Views"><div class="{kind[1]}">{"".join(items)}</div></nav>'


def page(data, title=None, actions=False):
    title = title or f"{data.get('team') or 'WOUF'} cockpit"
    blob = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    live = data.get("mode") == "live"
    script = SCRIPT + (LIVE + (ACTIONS if actions else "") if live else "")
    if live:
        head = "".join(f'<link rel="stylesheet" href="/static/{s}">' for s in SHEETS)
        head += '<link rel="icon" href="/static/v8/brand/wouf-icon.svg" type="image/svg+xml">'
    else:
        head = f"{showcase_meta()}<style>{inline_css()}</style>"
    lead = ("A snapshot of real runs: tasks, gates, reviews and the log, with secrets and private paths removed."
            if data.get("mode") == "showcase" else "Read-only view of the team folder.")
    if live and actions: lead = "The team folder, with owner actions: each one shows its commands before it runs."
    team = html.escape(str(data.get("team") or "WOUF"))
    folder = '<svg class="icon icon-s" aria-hidden="true"><use href="/static/v8/icons.svg#folder"/></svg>' if live else ""
    return (f"<!doctype html><html lang=\"en\" data-brand=\"wouf\"><head><meta charset=\"utf-8\">"
            f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1,viewport-fit=cover\">"
            f"<meta name=\"referrer\" content=\"no-referrer\">{head}<title>{html.escape(title)}</title></head><body>"
            f"<div class=\"field\" aria-hidden=\"true\"></div><main class=\"page app\" id=\"top\">"
            f"<header class=\"app-head\"><h1 class=\"display-s\" id=\"view-title\">{VIEWS[0][1]}</h1>"
            f"<span class=\"chip\">{folder}{team}</span><p class=\"live push\" id=\"stamp\"></p></header>"
            f"<p class=\"muted lead-line\"><b>{html.escape(title)}</b> · {html.escape(lead)}</p>"
            f"<div id=\"app\"></div></main>{nav(live)}<script type=\"application/json\" id=\"data\">{blob}</script>"
            f"<script>{script}</script></body></html>")


def export_showcase(team, out, title="WOUF at work"):
    data = collect(team, mode="showcase")
    title = redact(title)
    text = page(data, title=title)
    # Checked on what came from the team folder, every key and value: the page's own markup and script hold "/" by
    # design (</script>), and the hashes in its policy look random.
    found = residual("\n".join(strings(data) + [title]))
    if found:
        raise SystemExit(f"companion: not written, the page still holds {found}; check the team folder's text")
    Path(out).write_text(text, encoding="utf-8")
    return out


# ---------- server ----------
class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args):
        self.stopping, self.streams = threading.Event(), threading.BoundedSemaphore(SSE_CLIENTS)
        super().__init__(*args)

    def server_close(self):
        self.stopping.set()
        super().server_close()


ACTION_PATHS = ("/action/answer", "/action/decision", "/action/merge-proposal")
MAX_ANSWERS, MAX_TEXT, MAX_NOTE, MAX_BODY, MAX_OUTPUT = 20, 500, 300, 16 * 1024, 2000


class Bad(Exception):
    """A request the actions refuse before anything runs; the message is safe to show."""


def new_token():
    return secrets.token_urlsafe(32)


def launch_url(port, token):
    """The token travels in the fragment: the browser never sends it in a request line, a Referer or a log."""
    return f"http://127.0.0.1:{port}/#token={token}"


def shown(argv):
    return shlex.join(argv)


def text_arg(value, cap, what):
    """Free text that becomes one argv element: never an option, printable, no key shape."""
    if not isinstance(value, str) or not value.strip() or len(value) > cap:
        raise Bad(f"{what}: 1 to {cap} characters")
    if value.lstrip().startswith("-"):
        raise Bad(f"{what} may not start with -")
    if any(not c.isprintable() and c != "\n" for c in value):
        raise Bad(f"{what}: printable text and line breaks only")
    log = pack_log()
    if log.secret_in(value) or log.token_like(value):
        raise Bad(f"{what} holds a key shape; nothing was run")
    return value


def fields(obj, allowed, required):
    if not isinstance(obj, dict) or set(obj) - allowed or required - set(obj):
        raise Bad(f"fields: {', '.join(sorted(required))} (and only {', '.join(sorted(allowed))})")


def commands_for(path, body):
    """The `team` argv lists one action asks for: only these three shapes, built here from checked values."""
    if path == "/action/answer":
        fields(body, {"answers", "confirm"}, {"answers", "confirm"})
        answers = body["answers"]
        if not isinstance(answers, list) or not 1 <= len(answers) <= MAX_ANSWERS:
            raise Bad(f"answers: 1 to {MAX_ANSWERS}")
        out = []
        for a in answers:
            fields(a, {"id", "text"}, {"id", "text"})
            if not isinstance(a["id"], str) or not QID.fullmatch(a["id"]):
                raise Bad("a question id is Q-<n>")
            out.append(["question", "answer", a["id"], text_arg(a["text"], MAX_TEXT, "an answer")])
        return out
    if path == "/action/decision":
        fields(body, {"id", "verdict", "note", "confirm"}, {"id", "verdict", "confirm"})
        if not isinstance(body["id"], str) or not DID.fullmatch(body["id"]):
            raise Bad("a decision id is D-<n>")
        if body["verdict"] not in ("approve", "reject"):
            raise Bad("verdict: approve or reject")
        note = ["--note", text_arg(body["note"], MAX_NOTE, "a note")] if "note" in body else []
        return [["decision", body["verdict"], body["id"], *note]]
    fields(body, {"task", "head", "confirm"}, {"task", "head", "confirm"})
    if not isinstance(body["task"], str) or not TASK_ID.fullmatch(body["task"]):
        raise Bad("a task id is T-<n>")
    if not isinstance(body["head"], str) or not HEAD.fullmatch(body["head"]):
        raise Bad("head: 7 to 40 lowercase hex characters")
    return [["merge", "mark", body["task"], "--head", body["head"]]]


def strict_json(raw):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out: raise Bad("a key appears twice")
            out[key] = value
        return out
    try: return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    except (ValueError, UnicodeDecodeError, RecursionError): raise Bad("not one JSON object") from None


def run_commands(team, team_bin, commands):
    """Each command with an argv list (no shell) through the pack's own team; its output is masked before it is shown."""
    env = {**os.environ, "WOUF_TEAM_DIR": str(Path(team).resolve())}
    results = []
    for argv in commands:
        try:
            done = subprocess.run([sys.executable, str(team_bin), *argv], capture_output=True, text=True, env=env,
                                  stdin=subprocess.DEVNULL, timeout=120)
            code, output = done.returncode, (done.stdout + done.stderr)[-MAX_OUTPUT:]
        except subprocess.TimeoutExpired:
            code, output = 124, "timed out after 120 seconds"
        results.append({"command": shown(["team", *argv]), "code": code, "output": redact(output)})
    return results


def make_server(team, port=8765, hub=None, actions=False, token=None, team_bin=None):
    team = Path(team)
    pack_log(team)
    if actions and not token:
        raise ValueError("actions need a launch token")
    team_bin = Path(team_bin) if team_bin else _BIN[0] / "team"
    policy, running, files = csp_for(actions), threading.Lock(), static_files()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            port = self.server.server_address[1]
            # a page on another site cannot read this one (DNS rebinding): the Host names this server, port included
            if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
                return self.send(403, "text/plain", b"forbidden")
            if self.path == "/events":
                return self.events()
            if self.path == "/api/state":
                return self.send(200, "application/json", json.dumps(collect(team, hub)).encode())
            if self.path == "/":
                return self.send(200, "text/html; charset=utf-8", page(collect(team, hub), actions=actions).encode())
            # a vendored file: only a path from the list made at the start, read without following a link
            body = static_body(self.path) if self.path in files else None
            if body is not None:
                return self.send(200, files[self.path], body)
            self.send(404, "text/plain", b"not found")

        def do_POST(self):
            if not actions:
                return self.send_error(501)
            if self.path not in ACTION_PATHS:
                return self.answer(404, {"error": "no such action"})
            port = self.server.server_address[1]
            own = (f"127.0.0.1:{port}", f"localhost:{port}")
            origin, site = self.headers.get("Origin"), self.headers.get("Sec-Fetch-Site")
            kind = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            sent = (self.headers.get("X-WOUF-Token") or "").encode("utf-8", "replace")
            checks = (self.headers.get("Host") in own,                   # DNS rebinding: another name for this port
                      hmac.compare_digest(sent, token.encode()),         # the launch token, in constant time
                      kind == "application/json",                        # a form or text post is a cross-site shape
                      site in (None, "same-origin"),                     # what the browser says about the caller
                      origin in [f"http://{h}" for h in own] if origin is not None else site == "same-origin")
            if not all(checks):
                return self.answer(403, {"error": "forbidden"})
            try:
                length = int(self.headers.get("Content-Length") or "-1")
            except ValueError:
                length = -1
            if not 0 <= length <= MAX_BODY:
                return self.answer(413, {"error": f"a body of at most {MAX_BODY} bytes"})
            try:
                body = strict_json(self.rfile.read(length))
                commands = commands_for(self.path, body)
                if type(body.get("confirm")) is not bool:
                    raise Bad("confirm: true or false")
            except Bad as why:
                return self.answer(400, {"error": str(why)})
            if not body["confirm"]:
                return self.answer(200, {"commands": [{"command": shown(["team", *c])} for c in commands]})
            with running:  # one action at a time
                return self.answer(200, {"results": run_commands(team, team_bin, commands)})

        def answer(self, code, data):
            self.send(code, "application/json", json.dumps(data).encode())

        def events(self):
            """Server-sent events: the state once, then each time it changes; a comment line while nothing does."""
            if not self.server.streams.acquire(blocking=False):
                return self.send(503, "text/plain", b"too many open streams")
            try:
                self.head(200, "text/event-stream")
                last, quiet = None, 0
                while not self.server.stopping.is_set():
                    state = json.dumps(collect(team, hub))
                    if state != last:
                        self.wfile.write(f"event: state\ndata: {state}\n\n".encode())
                        last, quiet = state, 0
                    elif quiet >= KEEPALIVE:
                        self.wfile.write(b": keepalive\n\n")
                        quiet = 0
                    self.wfile.flush()
                    self.server.stopping.wait(1)
                    quiet += 1
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                self.server.streams.release()

        def head(self, code, kind):
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        def end_headers(self):
            # Here, not in head(): http.server's own error pages (501 for POST, 400 for a bad request) pass too.
            self.send_header("Content-Security-Policy", policy)
            self.send_header("X-Content-Type-Options", "nosniff")
            super().end_headers()

        def send(self, code, kind, body):
            self.head(code, kind)
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return Server(("127.0.0.1", port), Handler)


# Sibling modules that add a subcommand: each one's COMMAND = (name, main) registers it (showcase.py: showcase).
SUBCOMMANDS = ("showcase",)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] and argv[0] in SUBCOMMANDS:
        if str(HERE) not in sys.path: sys.path.insert(0, str(HERE))
        name, run = importlib.import_module(argv[0]).COMMAND
        return run(argv[1:])
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--team", default=os.environ.get("WOUF_TEAM_DIR") or ".agents/team", help="team folder")
    p.add_argument("--hub", default=os.environ.get("HUB_DB"), help="hub.db to count (read-only)")
    p.add_argument("--pack", help="the WOUF pack folder (.agents) whose bin/log has the secret patterns")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--json", action="store_true", help="print the state once and exit")
    p.add_argument("--export-showcase", metavar="OUT.html", help="write a self-contained snapshot page and exit")
    p.add_argument("--title", default="WOUF at work", help="title of the showcase page")
    p.add_argument("--actions", action="store_true",
                   help="let the page answer questions, approve or reject decisions and mark merges (team commands)")
    a = p.parse_args(argv)
    if not Path(a.team, "board.md").is_file():
        p.error(f"{a.team} is no team folder (no board.md); pass --team or set WOUF_TEAM_DIR")
    pack_log(a.team, a.pack)
    if unsafe_platform():
        print(f"companion: {unsafe_platform()}", file=sys.stderr)
    if a.json:
        print(json.dumps(collect(a.team, a.hub), indent=1)); return 0
    if a.export_showcase:
        export_showcase(a.team, a.export_showcase, a.title)
        print(f"showcase written to {a.export_showcase}; read it before you share it"); return 0
    token = new_token() if a.actions else None
    server = make_server(a.team, a.port, a.hub, actions=a.actions, token=token)
    port = server.server_address[1]
    if a.actions:
        print(f"companion with owner actions on {launch_url(port, token)} (open this link; Ctrl-C stops)")
    else:
        print(f"companion on http://127.0.0.1:{port}/ (Ctrl-C stops)")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
