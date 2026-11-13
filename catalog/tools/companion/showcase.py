"""Showcase: one finished task or epic as a secret-free offline replay (showcase.json, schema wouf.showcase.v1, and
showcase.html) the owner may publish. Stdlib only.

    python3 companion.py showcase T-0019 -o DIR [--brief FILE] [--title TEXT] [--team DIR] [--pack DIR]

The data comes only from `team snapshot --json` and `team events --since SEQ --json`, run as subprocesses: no file of
the team folder is read here. An epic is a task id whose tasks are named <epic>-<n> (T-0019 holds T-0019-22). Every
string is cleaned again (the snapshot is masked already), and a residual check on the final bytes refuses to write
anything when a pattern still matches, naming the field, never the value."""
import argparse, html, json, os, re, socket, subprocess, sys
from datetime import datetime
from pathlib import Path

import companion

SCHEMA = "wouf.showcase.v1"
TEMPLATE = Path(__file__).resolve().parent / "static" / "showcase.html"
PAGE_SIZE = 1000  # team events --limit takes at most 1000
STEP_KINDS = ("task.created", "task.state", "run.started", "run.finished", "flow.step", "phase.state",
              "question.asked", "question.answered", "decision.recorded", "merge.proposed", "merge.marked")
DONE = "done"
LAST_REFUSAL = []  # the last refusal's message, for the tests

# What the companion's redact() does not drop, by the brief's list. A workspace root keeps the path inside it.
WORKSPACE = re.compile(r"(?<![\w/>])/(?:bot/tasks|tmp)(?:/[^\s\"'`|<>)\]/]+)?")
IDS = re.compile(r"\b[ue]?[ug]id(?:[=:]|\s+)\d+(?:\([^)\s]*\))?|--user[= ]\d+(?::\d+)?", re.I)
SIGNED = re.compile(r"(?:Signed-off|Co-authored)-by:[^\n]*", re.I)
# An issue ref: any token that ends in #<digits> (#123, widget#45, github.com/acme/widget#12,
# gitlab.example.com/grp/sub/proj#7, a mask inside: github.com/<host>/widget#12); a token starts after a space, a
# quote or an opening bracket. Never an HTML entity (&#38;): a # right after & does not end a ref.
ISSUE = re.compile(r"(?<![^\s\"'`(<\[{])[^\s\"'`(\[{]*?(?<!&)#\d+\b")
SSH = re.compile(r"\bssh://\S+", re.I)
# An scp-style target (git@alias:infra/repo): an account at a host, then a colon.
SCP = re.compile(r"\b[\w.-]+@[A-Za-z0-9][\w-]*(?:\.[\w-]+)*:[^\s\"'`|<>)\]]*")
IPV6 = re.compile(r"(?<![\w:])(?:f[cd][0-9a-f]{2}|fe[89ab][0-9a-f]):[0-9a-f:]*(?:%[\w.-]+)?", re.I)
# An account at a host without a domain (jane@localhost, admin@db), after the e-mail rule took every address.
LOCAL_ADDRESS = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9][\w-]*(?![\w.@-])")
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# The masks the snapshot (and this file) put in: the path after one is relative and stays. The sentinel is a
# private-use character no input keeps (it is dropped first), so the path rule does not see a root behind a mask.
MARKS = re.compile(r"<(repo|team|workspace)>/")
MARKED = re.compile(r"<(?:repo|team|workspace|path)>/[^\s\"'`|<>)\]]*")
# Every path, absolute or behind a mask: one with a home or users segment runs to its line end (team snapshot's rule),
# found before the workspace roots and the masks, which would otherwise keep the segment as a relative path.
PATHS = re.compile(f"(?:{companion.ABSOLUTE.pattern})|{MARKED.pattern}", re.I)
SENTINEL = "\ue000"
CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff]")
GENERIC = {"root", "node", "user", "admin", "ubuntu", "bot", "runner", "localhost", "lead", "human", "team"}
# Asks the pack's own teamlib.snapshot.names() (this computer's user and host names, getpass's user, the docker_host
# alias and its user) in a subprocess, and which of them are configured (docker_host's alias and user): the pack reads
# its config there, this exporter reads no file of the team. -I keeps the current folder off the subprocess's path.
NAMES = """
import json, sys
sys.path.insert(0, sys.argv[1])
from teamlib import snapshot
users, hosts = snapshot.names()
try:
    target = snapshot.get(snapshot.config(), "environment", "docker_host") or ""
except (Exception, SystemExit):
    target = ""
where = target.split("://", 1)[-1].split("/", 1)[0]
user, where = where.rsplit("@", 1) if "@" in where else ("", where)
configured = [w for w in (user, where.split(":", 1)[0]) if w]
print(json.dumps({"users": sorted(users), "hosts": sorted(hosts), "configured": configured}))
"""


def pack_names(team_bin, env):
    done = subprocess.run([sys.executable, "-I", "-c", NAMES, str(Path(team_bin).parent)], env=env,
                          capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
    try:
        found = json.loads(done.stdout) if done.returncode == 0 else None
        return {key: [str(w) for w in found[key]] for key in ("users", "hosts", "configured")}
    except (ValueError, TypeError, KeyError):
        refuse(f"the pack did not name this computer's users and hosts (exit {done.returncode}); nothing written")


def names(env, known=None):
    """The words to hide, as (word, mark): this process's user and host names and what the pack names (known).
    A configured name (docker_host's alias and user) is hidden whatever its length; a guessed one from 3 characters
    on and when it is no generic word."""
    known = known or {}
    users = {env.get(k, "") for k in ("USER", "LOGNAME", "USERNAME")} | set(known.get("users") or [])
    hosts = set(known.get("hosts") or [])
    try:
        full = socket.gethostname()
        hosts |= {full, full.split(".")[0]}
    except OSError:
        pass
    configured = [w for w in known.get("configured") or [] if w]
    guessed = lambda words: sorted(w for w in words if len(w) >= 3 and w.lower() not in GENERIC and w not in configured)
    return [(w, "<host>" if w in hosts else "<user>") for w in configured] + \
           [(w, "<user>") for w in guessed(users)] + [(w, "<host>") for w in guessed(hosts)]


def registered(env):
    """The values bin/log masks (REGISTERED: its secret store and this process's WOUF_MASK), and env's WOUF_MASK as
    bin/log's registered() reads it."""
    try:
        extra = json.loads(env.get("WOUF_MASK") or "[]")
    except ValueError:
        extra = []
    extra = [v for v in extra if isinstance(v, str) and v] if isinstance(extra, list) else []
    return sorted({*companion.pack_log().REGISTERED, *extra}, key=len, reverse=True)


def word(text):
    """A name as a word: no letter or digit right before or after it (zorbatest_build and db-1 hold one)."""
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(text) + r"(?![A-Za-z0-9])", re.I)


def homes(text):
    """Each line with its first path that holds a home or users segment masked to the line end."""
    lines = []
    for line in text.split("\n"):
        for found in PATHS.finditer(line):
            if companion.HOME_SEGMENT.search(found.group(0)) or companion.OLD_HOME.match(line, found.start()):
                line = line[:found.start()] + "[path]"
                break
        lines.append(line)
    return "\n".join(lines)


def scrub(text, env, known=None):
    """One string as it may leave: registered values, sign-offs, home paths (to the line end), workspace roots, ssh
    targets, then the companion's redact() (keys, e-mail, IPv4, credentials, every absolute path), then ids, issue
    refs, private IPv6 and the local and configured names."""
    text = CONTROL.sub(" ", str(text).replace(SENTINEL, ""))
    text = companion.pack_log().mask_secrets(text, registered(env))
    text = SIGNED.sub("[signed-off]", text)
    text = ISSUE.sub("[ref]", text)  # before the path rule, which takes /widget#12 behind a mask for a path
    text = homes(text)
    text = WORKSPACE.sub("<workspace>", text)
    text = MARKS.sub(lambda m: f"<{m.group(1)}>{SENTINEL}", text)
    text = SCP.sub("[host]", SSH.sub("[host]", text))
    text = companion.redact(text).replace(SENTINEL, "/")
    text = companion.EMAIL.sub("[email]", text)  # user@192.0.2.1 is user@[redacted] after the IPv4 rule: one address
    text = LOCAL_ADDRESS.sub("[host]", text)
    text = IDS.sub("[id]", text)
    text = ISSUE.sub("[ref]", text)
    text = IPV6.sub("[ip]", text)
    for name, mark in names(env, known):
        text = word(name).sub(mark, text)
    return text


def scrubbed(value, env, known=None):
    """Every string value cleaned; keys are the schema's own words (KEYS), never data, and stay."""
    if isinstance(value, str): return scrub(value, env, known)
    if isinstance(value, dict): return {k: scrubbed(v, env, known) for k, v in value.items()}
    if isinstance(value, list): return [scrubbed(v, env, known) for v in value]
    return value


def residual(text, env, known=None):
    """The kinds a cleaned string still holds, never its value."""
    plain = MARKS.sub(lambda m: f"<{m.group(1)}>{SENTINEL}", text)
    home = any(companion.HOME_SEGMENT.search(m.group(0)) for m in MARKED.finditer(text))
    kinds = [companion.residual(plain) or "", "a private path" if home else "",
             "a registered value" if any(v in text for v in registered(env)) else "",
             "a signed-off line" if SIGNED.search(text) else "",
             "an IP address" if IPV4.search(text) or IPV6.search(text) else "",
             "a user or group id" if IDS.search(text) else "", "an issue ref" if ISSUE.search(text) else "",
             "an ssh target" if SSH.search(text) or SCP.search(text) or LOCAL_ADDRESS.search(text) else "",
             "a local name" if any(word(n).search(text) for n, _ in names(env, known)) else ""]
    return ", ".join(dict.fromkeys(k for k in ", ".join(kinds).split(", ") if k))


# Every key of wouf.showcase.v1: a key is the schema's word, never data, so it is checked against this list.
KEYS = frozenset({"manifest", "tasks", "steps", "gates", "reviews", "artifacts", "time_saved_s", "v", "task_id",
                  "title", "class", "objective", "acceptance_criteria", "started_at", "completed_at",
                  "wall_duration_s", "total_cost", "final_verdict", "id", "status", "seq", "at", "task", "kind", "bot",
                  "outcome", "what", "duration_s", "runner", "verdict", "round", "paths", "branches", "tokens"})


def residual_fields(blob, env, known=None):
    """Every field of JSON text that a pattern still matches, as path (kinds); a key outside the schema is named by
    where it sits, never by itself."""
    found = []

    def walk(value, where):
        if isinstance(value, dict):
            for k, v in value.items():
                if k not in KEYS:
                    found.append(f"{where or 'top level'}: a key outside the schema")
                    continue
                walk(v, f"{where}.{k}" if where else k)
        elif isinstance(value, list):
            for i, v in enumerate(value): walk(v, f"{where}[{i}]")
        elif isinstance(value, str) and not (where == "manifest.v" and value == SCHEMA):  # the constant id
            kinds = residual(value, env, known)
            if kinds: found.append(f"{where} ({kinds})")
    walk(json.loads(blob), "")
    return found


DATA_RE = re.compile(r'(<script type="application/json" id="showcase-data">)(.*?)(</script>)', re.S)
TITLE_RE = re.compile(r"(<title>)(.*?)(</title>)", re.S)


def page_residual(text, env, known=None):
    """What the final page bytes still hold: its embedded data and title by field, and anything beside the
    template's own markup (the page with its data and title cut out must be the template)."""
    data, title = DATA_RE.search(text), TITLE_RE.search(text)
    if not data or not title:
        return ["showcase.html (no data or no title)"]
    found = [f"showcase.html data {f}" for f in residual_fields(data.group(2), env, known)]
    kinds = residual(html.unescape(title.group(2)), env, known)
    found += [f"showcase.html <title> ({kinds})"] if kinds else []
    cut = lambda page_text: TITLE_RE.sub(r"\1\3", DATA_RE.sub(r"\1\3", page_text))
    if cut(text) != cut(page({}, "")):
        found.append("showcase.html markup (differs from static/showcase.html)")
    return found


def refuse(message):
    LAST_REFUSAL[:] = [message]
    print(f"companion showcase: {message}", file=sys.stderr)
    raise SystemExit(1)


# ---------- reading: team snapshot and team events, nothing else ----------
def team(team_bin, env, *args):
    done = subprocess.run([sys.executable, str(team_bin), *args], env=env, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=120)
    if done.returncode != 0:
        refuse(f"team {args[0]} failed (exit {done.returncode}): {scrub(done.stderr.strip()[-300:], env)}")
    return done.stdout


def events(team_bin, env):
    """Every event, oldest first, a page of PAGE_SIZE at a time."""
    found, since = [], 0
    while True:
        page = [json.loads(line) for line in team(team_bin, env, "events", "--since", str(since), "--limit",
                                                  str(PAGE_SIZE), "--json").splitlines() if line.strip()]
        found += page
        if len(page) < PAGE_SIZE: return found
        since = page[-1]["seq"]


# A run's own id: its package, or a run of it, as a store records them: a review round T.r<n>, a scout or research run
# T.scout, a fix round T-fix<n>, and these combined (T.r2-fix1). Each belongs to its package T.
RUN_SUFFIX = re.compile(r"(?:\.[A-Za-z][\w-]*|-fix\d+)+$")
REVIEW_ROUND = re.compile(r"\.r(\d+)(?![\w])")


def package_of(task):
    return RUN_SUFFIX.sub("", task)


def selected(target, tasks):
    """The packages of a task or epic: the task itself and every T-<n> under it, never a run's own id."""
    return [t for t in tasks if package_of(t["id"]) == t["id"]
            and (t["id"] == target or t["id"].startswith(target + "-"))]


# ---------- the model ----------
def when(t):
    return datetime.fromisoformat(str(t).replace("Z", "+00:00"))


def seconds(start, end):
    return int((when(end) - when(start)).total_seconds())


def what(e):
    """A step's line: fixed words and short fields only, never a bot's free text."""
    kind, d = e["kind"], e.get("data") or {}
    if kind == "task.created": return f"created: {d.get('title') or ''}"
    if kind == "task.state": return f"state {d.get('state') or ''}"
    if kind in ("run.started", "run.finished"):
        return " ".join(x for x in (d.get("bot"), "on", d.get("runner"), d.get("tier")) if x)
    if kind == "flow.step": return f"{d.get('kind') or ''} step"
    if kind == "phase.state": return f"phase {d.get('phase') or ''}"
    if kind.startswith("question."): return f"{d.get('id') or ''} {kind.split('.')[1]} by {d.get('from') or ''}"
    if kind == "decision.recorded": return f"D-{d.get('number')}: {d.get('title') or ''} ({d.get('why') or ''})"
    return f"{kind.split('.')[1]} {d.get('branch') or ''}"


def outcome(e):
    d = e.get("data") or {}
    return d.get("verdict") or d.get("outcome") or d.get("status") or d.get("state") or None


def steps_of(evs):
    """The replay: one step per event of the selection, a finished run with its time since the bot's start."""
    started, out = {}, []
    for e in evs:
        if e["kind"] not in STEP_KINDS: continue
        d, duration = e.get("data") or {}, None
        key = (e["task"], d.get("bot"))
        if e["kind"] == "run.started": started[key] = e["t"]
        elif e["kind"] == "run.finished" and key in started:
            duration = max(0, seconds(started.pop(key), e["t"]))
        out.append({"seq": e["seq"], "at": e["t"], "task": e["task"], "kind": e["kind"], "bot": d.get("bot"),
                    "outcome": outcome(e), "what": what(e), "duration_s": duration})
    return out


def time_saved(steps, wall):
    """Serial step time minus wall time: what running steps side by side saved; never below 0."""
    return max(0, sum(s["duration_s"] or 0 for s in steps) - wall)


def tokens(evs):
    """The sum of the runs' token counts (input/cache read/cache write/output), or None without one."""
    counts = [[int(n) for n in e["data"]["tokens"].split("/")] for e in evs
              if e["kind"] == "run.finished" and re.fullmatch(r"\d+/\d+/\d+/\d+", str(e["data"].get("tokens")))]
    return "/".join(str(sum(c[i] for c in counts)) for i in range(4)) if counts else None


def brief_fields(path):
    """Objective, acceptance criteria and class from a brief the owner names (not a file of the team folder)."""
    text = Path(path).read_text(encoding="utf-8")
    line = lambda key: (re.search(rf"^{key}:[ \t]*(.*?)\s*$", text, re.M) or [None, None])[1] or None
    block = re.search(r"^Acceptance criteria:[ \t]*\n((?:[ \t]*- .*\n?)+)", text, re.M)
    criteria = [item.strip()[2:].strip() for item in block.group(1).splitlines()] if block else []
    return line("Objective"), criteria, line("Class")


def build(target, snap, evs, brief=None, title=None, notes=None):
    """The showcase of target; what the store does not hold (a package without a review) goes to notes."""
    notes = [] if notes is None else notes
    tasks = selected(target, snap["tasks"])
    if not tasks: refuse(f"no task {target} (or {target}-<n>) in the snapshot")
    open_ = [t["id"] for t in tasks if t["status"] != DONE]
    if open_: refuse(f"not finished yet: {', '.join(open_)}")
    ids = {t["id"] for t in tasks}
    mine = [e for e in evs if package_of(e["task"]) in ids]
    if not mine: refuse(f"no events for {target}")
    reviewers = {b["id"] for b in snap.get("bots") or [] if b.get("role") == "review"} | {"review"}
    steps = steps_of(mine)
    done_at = max(e["t"] for e in mine if e["kind"] == "task.state" and (e.get("data") or {}).get("state") == DONE) \
        if any(e["kind"] == "task.state" and (e.get("data") or {}).get("state") == DONE for e in mine) else mine[-1]["t"]
    start = min(e["t"] for e in mine)
    wall = seconds(start, done_at)
    gates = [{"seq": e["seq"], "at": e["t"], "task": package_of(e["task"]), "status": e["data"].get("status")}
             for e in mine if e["kind"] == "flow.step" and e["data"].get("kind") == "gate"]
    # A review: a finished run with a verdict, by a reviewer or in a review round (T.r<n>, whose n is its round).
    reviews, rounds = [], {}
    for e in mine:
        d, package, numbered = e.get("data") or {}, package_of(e["task"]), REVIEW_ROUND.search(e["task"])
        if e["kind"] == "run.finished" and d.get("verdict") and (d.get("bot") in reviewers or numbered):
            rounds[package] = int(numbered.group(1)) if numbered else rounds.get(package, 0) + 1
            reviews.append({"seq": e["seq"], "at": e["t"], "task": package, "bot": d["bot"],
                            "runner": d.get("runner"), "verdict": d["verdict"], "round": rounds[package]})
    last = {r["task"]: r["verdict"] for r in reviews}
    unreviewed = sorted(ids - set(last), key=companion.natural)
    if unreviewed:
        notes.append(f"no review verdict in the store for {', '.join(unreviewed)}: final_verdict is null")
    verdict = None if unreviewed else "approve" if all(v == "approve" for v in last.values()) \
        else next(v for v in last.values() if v != "approve")
    costs = [float(t["cost_total"]) for t in tasks if t.get("cost_total")]
    exact = next((t for t in tasks if t["id"] == target), None)
    objective, criteria, klass = brief_fields(brief) if brief else (None, [], None)
    paths = [p for t in tasks for p in t.get("paths") or []]
    branches = sorted({e["data"]["ref"] for e in mine if e["kind"] == "run.finished" and e["data"].get("ref")
                       and not re.fullmatch(r"[0-9a-f]{7,40}", e["data"]["ref"])})
    manifest = {"v": SCHEMA, "task_id": target, "title": title or (exact["title"] if exact else target),
                "class": exact.get("class") if exact else klass, "objective": objective,
                "acceptance_criteria": criteria, "started_at": start, "completed_at": done_at,
                "wall_duration_s": wall, "total_cost": f"{sum(costs):.2f}" if costs else None,
                "final_verdict": verdict}
    return {"manifest": manifest,
            "tasks": [{"id": t["id"], "title": t["title"], "class": t.get("class"), "status": t["status"]}
                      for t in tasks],
            "steps": steps, "gates": gates, "reviews": reviews,
            "artifacts": {"tasks": len(tasks), "paths": sorted(dict.fromkeys(paths)), "branches": branches,
                          "tokens": tokens(mine)},
            "time_saved_s": time_saved(steps, wall)}


# ---------- writing ----------
def page(data, title):
    template = TEMPLATE.read_text(encoding="utf-8")
    style = re.search(r"<style>(.*?)</style>", template, re.S).group(1)
    script = re.search(r"<script>(.*?)</script>", template, re.S).group(1)
    policy = (f"default-src 'none'; style-src {companion.digest(style)}; script-src "
              f"{companion.digest(script)}; connect-src 'none'; img-src 'none'; base-uri 'none'; form-action 'none'")
    blob = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    fill = {"POLICY": policy, "TITLE": html.escape(title), "DATA": blob}
    return re.sub(r"__(POLICY|TITLE|DATA)__", lambda m: fill[m.group(1)], template)


def write(folder, name, text):
    """A new file in folder, never through a link: O_NOFOLLOW refuses a link at the name."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try: fd = os.open(Path(folder, name), flags, 0o644)
    except OSError as why: refuse(f"cannot write {name} ({why.strerror}); is it a link?")
    with os.fdopen(fd, "w", encoding="utf-8") as handle: handle.write(text)


def export(target, team_dir, out, env=None, pack=None, brief=None, title=None):
    env = dict(os.environ if env is None else env)
    if not Path(team_dir).is_dir(): refuse(f"{team_dir} is no team folder; pass --team or set WOUF_TEAM_DIR")
    log = companion.pack_log(team_dir, pack)
    problem = log.task_problem(target)
    if problem: refuse(f"task id: {problem}")
    env["WOUF_TEAM_DIR"] = str(Path(team_dir).resolve())
    team_bin = companion._BIN[0] / "team"
    snap = json.loads(team(team_bin, env, "snapshot", "--json"))
    known, notes = pack_names(team_bin, env), []
    raw = build(target, snap, events(team_bin, env), brief, title, notes)
    data = scrubbed(raw, env, known)
    data["manifest"]["v"] = SCHEMA  # the schema id is a constant, never data: it does not pass the masking
    if data["manifest"]["title"] != raw["manifest"]["title"]:
        notes.append("the title held a secret, a private path or a name of this computer or its config and is "
                     "masked; pass --title to choose another")
    blob = json.dumps(data, ensure_ascii=False, indent=1) + "\n"
    text = page(json.loads(blob), data["manifest"]["title"])
    # The last check runs on the final bytes of both files, before either is written.
    found = [f"showcase.json {f}" for f in residual_fields(blob, env, known)] + page_residual(text, env, known)
    if found: refuse(f"not written; still found in {'; '.join(found[:10])}")
    out = Path(out)
    for name in ("showcase.json", "showcase.html"):
        if out.is_symlink() or Path(out, name).is_symlink(): refuse(f"{out / name} is a link; not written")
    out.mkdir(parents=True, exist_ok=True)
    write(out, "showcase.json", blob)
    try:
        write(out, "showcase.html", text)
    except BaseException:  # one file alone is no export: the first goes again
        Path(out, "showcase.json").unlink()
        raise
    return out, notes


def main(argv):
    p = argparse.ArgumentParser(prog="companion.py showcase", description="export one finished task or epic "
                                "as showcase.json and showcase.html (offline, secret-free)")
    p.add_argument("target", help="a task id, or an epic: the tasks named <id>-<n>")
    p.add_argument("-o", "--out", required=True, metavar="DIR", help="folder for showcase.json and showcase.html")
    p.add_argument("--brief", help="the task's brief, for its objective and acceptance criteria")
    p.add_argument("--title", help="the page's title (default: the task's title, or the epic's id)")
    p.add_argument("--team", default=os.environ.get("WOUF_TEAM_DIR") or ".agents/team", help="team folder")
    p.add_argument("--pack", help="the WOUF pack folder (.agents) with bin/team and bin/log")
    a = p.parse_args(argv)
    out, notes = export(a.target, a.team, a.out, pack=a.pack, brief=a.brief, title=a.title)
    print(f"showcase written to {out}/showcase.json and showcase.html; read it before you share it")
    for note in notes:
        print(f"note: {note}")
    return 0


COMMAND = ("showcase", main)
