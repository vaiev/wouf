"""teamlib.batch: the packages of one plan wave as flows at the same time, each on its own worker and scout, when
their Paths are pairwise disjoint; the repository lock around ref updates of a batch's runs; the command batch."""

from __future__ import annotations

import fcntl
import threading
from contextlib import contextmanager

from teamlib.core import *  # noqa: F401,F403
from teamlib import engine as engine_module, run as run_module
from teamlib.flow import (PLAN_SCHEMA, active_id, flow_dir, head_of, load_plan,
                          plan_bots as bots_of, plan_cells as cells, plan_runner as runner_of)
from teamlib.run import TEAM, split_repo


USAGE = """
  team batch PLAN-DIR --wave N [--repo PATH[@REF]] [--resume] [--dry-run]
                                      run the packages of wave N of PLAN-DIR/PLAN.md (briefs in PLAN-DIR/briefs/<id>.md)
                                      as flows at once, each on its own worker and scout (Runner: w, w2), when their
                                      Paths are pairwise disjoint; --dry-run shows packages, bots and the verdict
                                      Matching waves resume automatically at their saved repository/base.
                                      For a question or failed step: team flow TASK --answer TEXT or --retry,
                                      then repeat the batch. Earlier waves must be ready and merged into the base.
  team batch --status PLAN-DIR --wave N
                                      one line per package: state, branch, head
"""

# Set for the flows a batch starts: their runs update refs only under the repository lock.
BATCH_ENV = "WOUF_BATCH"


# ---------------------------------------------------------------- the plan

def plan_wave(text: str, wave: int) -> list[dict]:
    """The rows of wave N in the plan's package table (the first table with the columns ID and Wave): id and runner."""
    lines = text.splitlines()
    for at, line in enumerate(lines):
        if not line.lstrip().startswith("|"):
            continue
        head = [cell.lower() for cell in cells(line)]
        if "id" not in head or "wave" not in head:
            continue
        rows = []
        for row in lines[at + 2:]:
            if not row.lstrip().startswith("|"):
                break
            values = dict(zip(head, cells(row)))
            if values.get("wave") == str(wave) and values.get("id"):
                rows.append({"id": values["id"], "runner": values.get("runner") or None})
        return rows
    return []


# ---------------------------------------------------------------- the overlap check

def literal_prefix(glob: str) -> str:
    """The part of a glob before its first wildcard: the folder (or file) everything it matches starts with."""
    found = re.search(r"[*?]", glob)
    return glob[:found.start()] if found else glob


def overlap(one: list[str], two: list[str], files: list[str]) -> str | None:
    """One path both packages may change, or None: (a) a file of the repository that a glob of each matches, (b) a
    literal path or prefix matched by the other, (c) an unresolved intersection with compatible literal prefixes.
    A package without Paths may change anything. None means the future scopes are provably disjoint."""
    if not one or not two:
        return "(no Paths)"
    first, second = [glob_regex(g) for g in one], [glob_regex(g) for g in two]
    for path in files:
        if any(r.match(path) for r in first) and any(r.match(path) for r in second):
            return path
    for globs, others in ((one, second), (two, first)):
        for glob in globs:
            for probe in dict.fromkeys((glob if literal_prefix(glob) == glob else "", literal_prefix(glob))):
                if probe and any(r.match(probe) for r in others):
                    return probe
    same = sorted(set(one) & set(two))
    if same:
        return same[0]
    # Globs with compatible literal prefixes may intersect on files that do not exist yet. We deliberately
    # refuse uncertainty; incompatible prefixes prove disjointness. A trailing / means an implicit **.
    for a in one:
        for b in two:
            a = a + "**" if a.endswith("/") else a
            b = b + "**" if b.endswith("/") else b
            if literal_prefix(a) == a or literal_prefix(b) == b:
                continue  # Exact literal matches have already been checked above.
            ap, bp = literal_prefix(a), literal_prefix(b)
            if ap.startswith(bp) or bp.startswith(ap):
                return f"(unresolved glob intersection: {a}, {b})"
    return None


def first_overlap(packages: list[dict], files: list[str]) -> tuple | None:
    for at, one in enumerate(packages):
        for two in packages[at + 1:]:
            path = overlap(one["paths"], two["paths"], files)
            if path:
                return one["id"], two["id"], path
    return None


# ---------------------------------------------------------------- the repository lock

LOCKS = threading.local()


@contextmanager
def repo_lock():
    """One ref update at a time across the flows of a batch: flock on team/env/repo.lock (re-entrant per thread)."""
    depth = getattr(LOCKS, "depth", 0)
    if depth:
        LOCKS.depth = depth + 1
        try:
            yield
        finally:
            LOCKS.depth -= 1
        return
    path = env_dir() / "repo.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        LOCKS.depth = 1
        try:
            yield
        finally:
            LOCKS.depth = 0
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# What a locked run starts for its supervisor (which brings the commits back): `bin/team _locked _supervise …`, in
# its own session, as run.LAUNCH starts `bin/team _supervise …`.
LOCKED_LAUNCH = ("import subprocess, sys; subprocess.Popen([*sys.argv[1:3], '_locked', *sys.argv[3:]], "
                 "start_new_session=True)")
LOCKED_COMMANDS = ("run", "_supervise", "gate")


def locking_git(git):
    def locked_git(*args, **kwargs):
        if args[:1] in (("update-ref",), ("fetch",)):
            with repo_lock():
                return git(*args, **kwargs)
        return git(*args, **kwargs)
    return locked_git


def locking_bundle(bundle):
    def locked_bundle(*args, **kwargs):
        with repo_lock():
            return bundle(*args, **kwargs)
    return locked_bundle


def lock_refs(run, engine=None) -> None:
    """The run module of this process with the lock around bundle_commit (its git update-ref) and around run.py's
    git update-ref and fetch (the fetch of the results in collect); the engine module's git update-ref and fetch
    (keep_others, the second fetch of collect) as well; its supervisor starts locked too."""
    run.git, run.bundle_commit, run.LAUNCH = locking_git(run.git), locking_bundle(run.bundle_commit), LOCKED_LAUNCH
    if engine is not None:
        engine.git = locking_git(engine.git)


def gate_module():
    from teamlib import gate
    return gate


# ---------------------------------------------------------------- the command

def packages(plan_dir: Path, wave: int) -> list[dict]:
    return [p for p in checked_plan(plan_dir) if p["wave"] == wave] or no_wave(plan_dir, wave)


def checked_plan(plan_dir: Path) -> list[dict]:
    try:
        return load_plan(plan_dir)
    except (ValueError, OSError, UnicodeError) as error:
        die(f"invalid plan {plan_dir}: {error}")


def no_wave(plan_dir: Path, wave: int):
    die(f"no package in wave {wave} of {plan_dir / 'PLAN.md'}")


def check_start(ini, found: list[dict], repo: Path, base: str, resume: bool) -> None:
    """Everything that refuses the batch before any flow starts: bots that are not active, one bot in two packages,
    a foreign flow/branch, or a resumed flow whose fixed inputs disagree with the saved wave."""
    bots = [b for p in found for b in (p["bot"], p["scout"])]
    for member in dict.fromkeys(bots):
        active_id(member)
    twice = sorted({member for member in bots if bots.count(member) > 1})
    if twice:
        die(f"one bot in two packages of the batch: {' '.join(twice)}")
    for p in found:
        branch = f"{slug(team_name(ini))}/{p['task']}"
        folder = flow_dir(p["task"])
        if (folder / "state.json").is_file():
            flow = read_json(folder / "state.json")
            expected = {"task": p["task"], "repo": str(repo), "base": base, "branch": branch,
                        "bot": p["bot"], "scout_bot": p["scout"], "class": p["class"]}
            source = p["brief"].read_text(encoding="utf-8")
            saved = (folder / "brief.md").read_text(encoding="utf-8") if (folder / "brief.md").is_file() else ""
            if (not resume or any(flow.get(k) != v for k, v in expected.items()) or
                    not (saved == source or saved.startswith(source + "\n## Answer from the lead"))):
                die(f"the flow of {p['task']} exists with different fixed inputs; the batch starts nothing")
            if flow.get("status") == "ready" and (not flow.get("head") or head_of(flow) != flow["head"]):
                die(f"ready flow {p['task']} has a changed or missing branch head; inspect it before recovery")
            p["resume"] = True
            continue
        if git("rev-parse", "-q", "--verify", f"refs/heads/{branch}", check=False, at=repo).returncode == 0:
            die(f"branch {branch} exists already; use a new task id for package {p['id']}")


def start_flow(p: dict, repo: Path, base: str, results: dict) -> None:
    argv = [sys.executable, str(TEAM), "flow", p["task"]]
    if not p.get("resume"):
        argv += ["--brief", str(p["brief"]), "--repo", f"{repo}@{base}", "--bot", p["bot"], "--scout", p["scout"]]
    results[p["id"]] = subprocess.run(argv, env={**os.environ, BATCH_ENV: "1"}, capture_output=True, text=True,
                                      stdin=subprocess.DEVNULL)


def status(found: list[dict]) -> int:
    for p in found:
        flow = read_json(flow_dir(p["task"]) / "state.json")
        head = (flow.get("head") or head_of(flow)) if flow.get("branch") and flow.get("repo") else None
        say(f"{p['id']} {p['task']} {flow.get('status') or 'not-started'} {flow.get('branch') or '-'} {head or '-'}")
    return 0


def cmd_batch(ini, args: list[str]) -> int:
    flags, positional, wave, repo_arg = set(), [], None, None
    it = iter(args)
    for arg in it:
        if arg == "--wave":
            wave = next(it, None)
        elif arg == "--repo":
            repo_arg = next(it, None)
            if not repo_arg:
                die("--repo needs PATH[@REF]")
        elif arg in ("--dry-run", "--status", "--resume"):
            flags.add(arg)
        else:
            positional.append(arg)
    if len(positional) != 1 or not wave or not wave.isdigit() or int(wave) < 1:
        die(USAGE.strip())
    plan_dir = Path(positional[0]).resolve()
    all_packages = checked_plan(plan_dir)
    found = [p for p in all_packages if p["wave"] == int(wave)]
    if not found:
        no_wave(plan_dir, wave)
    if "--status" in flags:
        return status(found)
    key = hashlib.sha256(str(plan_dir).encode()).hexdigest()[:24] + f"-wave-{int(wave)}"
    folder = env_dir() / "batches"
    folder.mkdir(parents=True, exist_ok=True)
    # This lock is distinct from repo.lock: children must be able to acquire the ref lock while we wait for them.
    with (folder / (key + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return run_batch(ini, plan_dir, int(wave), all_packages, found, flags, repo_arg, folder / (key + ".json"))


def prior_waves(all_packages: list[dict], wave: int, repo: Path, base: str) -> None:
    """Readiness alone is insufficient: every prior exact head must already be in the selected base."""
    for p in all_packages:
        if p["wave"] >= wave:
            continue
        flow = read_json(flow_dir(p["task"]) / "state.json")
        if flow.get("status") != "ready" or flow.get("repo") != str(repo) or not flow.get("head"):
            die(f"prior wave {p['wave']} package {p['id']} ({p['task']}) is not ready in this repository; "
                f"recover it with team flow {p['task']} --answer TEXT or --retry, then repeat its batch")
        if head_of(flow) != flow["head"]:
            die(f"prior wave {p['wave']} package {p['id']} has a changed branch head; inspect it before continuing")
        if git("merge-base", "--is-ancestor", flow["head"], base, check=False, at=repo).returncode:
            die(f"prior wave {p['wave']} package {p['id']} head {flow['head'][:12]} must be merged into the "
                "selected base before starting this wave; follow the review/merge approval procedure")


def run_batch(ini, plan_dir: Path, wave: int, all_packages: list[dict], found: list[dict], flags: set,
              repo_arg: str | None, manifest: Path) -> int:
    saved = read_json(manifest)
    repo, ref = split_repo(repo_arg or str(PROJECT))
    repo = repo or PROJECT
    if saved:
        if repo_arg and str(repo) != saved.get("repo"):
            die("the wave's repository is fixed; use its saved repository")
        repo = Path(saved["repo"])
    requested = git("rev-parse", "-q", "--verify", f"{ref or 'HEAD'}^{{commit}}", check=False, at=repo).stdout.strip()
    base = saved.get("base") if saved else requested
    if saved and repo_arg and ("@" in repo_arg or split_repo(repo_arg)[0] is None) and requested != base:
        die("the wave's base is fixed; resume at the saved base, or use a new plan/task for a new base")
    if not base:
        die(f"{repo}: no commit to start the batch from")
    signature = [{k: v for k, v in p.items() if k != "brief"} for p in all_packages]
    if saved and saved.get("packages") != signature:
        die("the saved wave's plan or briefs changed; restore them before resuming")
    prior_waves(all_packages, wave, repo, base)
    if "--dry-run" in flags:
        say(f"batch: wave {wave} of {shown_path(plan_dir)}, {len(found)} packages from {base[:12]}")
        for p in found:
            say(f"{p['id']} {p['task']} {p['bot']} {p['scout']} {' '.join(p['paths']) or '(no Paths)'}")
    check_start(ini, found, repo, base, bool(saved))
    files = git("ls-tree", "-r", "-z", "--name-only", base, at=repo).stdout.split("\0")
    shared = first_overlap(found, [f for f in files if f])
    if "--dry-run" in flags:
        say(f"overlap: {shared[0]} and {shared[1]} share {shared[2]}; nothing would start" if shared else
            "verdict: disjoint; the packages would start at once")
        return 1 if shared else 0
    if shared:
        die(f"overlap: {shared[0]} and {shared[1]} share {shared[2]}; the batch starts nothing")
    if not saved:
        write_json(manifest, {"repo": str(repo), "base": base, "packages": signature})
    results: dict = {}
    threads = [threading.Thread(target=start_flow, args=(p, repo, base, results), daemon=True) for p in found]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    code = 0
    for p in found:
        result = results.get(p["id"])
        if result is None:
            say(f"{p['id']} {p['task']} the flow did not start", err=True)
            code = code or 1
            continue
        for line in result.stdout.splitlines():
            say(f"{p['id']} {p['task']} {line}")
        for line in result.stderr.splitlines():
            say(f"{p['id']} {p['task']} {line}", err=True)
        if result.returncode:
            say(f"{p['id']} {p['task']} recovery: team flow {p['task']} --answer TEXT (question) or --retry "
                "(after fixing a failure/limit), then repeat this batch; repository/base remain fixed")
        code = code or result.returncode
    return code


def shown_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT))
    except ValueError:
        return str(path)


def do_batch(rest: list[str]) -> int:
    return cmd_batch(config(), rest)


def do_locked(rest: list[str]) -> int:
    """`team _locked run …` and `team _locked gate …` (what a batch's flow starts) and `team _locked _supervise …`:
    the command with its ref updates under the repository lock."""
    if not rest or rest[0] not in LOCKED_COMMANDS:
        die("usage: team _locked run|gate ARGS (started by the flows of team batch)")
    if rest[0] == "gate":
        # The gate imported bundle_commit by name: its own binding is the one to lock.
        gate = gate_module()
        gate.bundle_commit = locking_bundle(gate.bundle_commit)
        return gate.COMMANDS["gate"](rest[1:])
    lock_refs(run_module, engine_module)
    return run_module.COMMANDS[rest[0]](rest[1:])


COMMANDS = {"batch": do_batch, "_locked": do_locked}
