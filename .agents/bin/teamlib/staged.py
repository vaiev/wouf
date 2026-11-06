"""teamlib.staged: staged runs. team run --staged and team flow --staged run the bot as usual, but nothing comes back
into the repository: the result bundle and out/ stay in the run's folder under team/env/. team result shows what
would come back (commit subjects, the diff stat, the names of the output files, never their contents); team apply
brings it back on purpose, as the run would have done. The command apply."""

from __future__ import annotations

import tempfile

from teamlib.core import *  # noqa: F401,F403
from teamlib import run
from teamlib.engine import keep_others, others_lines
from teamlib.flow import head_of
from teamlib.run import BOT_TEXT, check_ref_conflict, check_task, reserved, shown, stage_of

USAGE = """
  team run ID TASK --brief FILE ... --staged
  team flow TASK ... --staged         run as usual, but bring nothing back into the repository; team result shows
                                      the commits, the diff stat and the output files that would come back
                                      (a flow stops after its work step)
  team apply ID TASK                  bring a staged run back as branch <team>/<TASK>, as the run would have done
"""

# The flow's steps whose commits come back on the task branch.
WORK = ("worker", "bugfixer")


def shown_line(text: str) -> str:
    """A line of the bot's (a commit subject, a file name) as the human sees it: escaped and cut."""
    return visible(text)[:BOT_TEXT]


def staged_commits(spec: dict, bundle: Path) -> tuple[list[str], list[str]] | None:
    """The subjects of the staged commits, oldest first, and their diff stat against the start commit. Read in a
    throwaway repository that borrows the lead's objects: the lead's repository gets no ref and no object."""
    at = Path(spec.get("repo_dir") or PROJECT)
    common = at / git("rev-parse", "--git-common-dir", at=at).stdout.strip()
    with tempfile.TemporaryDirectory() as tmp:
        git("init", "-q", "--bare", tmp)
        put(Path(tmp) / "objects" / "info" / "alternates", f"{(common / 'objects').resolve()}\n")
        if git("fetch", "-q", str(bundle), f"task/{spec['task']}:refs/heads/staged", check=False, at=tmp).returncode:
            return None
        start = spec.get("commit") or ""
        subjects = git("log", "--reverse", "--format=%s", f"{start}..staged", at=tmp).stdout.splitlines()
        stat = git("diff", "--stat", start, "staged", at=tmp).stdout.splitlines()
    return subjects, stat


def report(spec: dict, result: dict) -> int:
    """team result of a staged run: the state, each commit's subject, the diff stat, the output files' names."""
    stage, member, task = Path(spec["stage"]), spec["member"], spec["task"]
    out, bundle = stage / "out", stage / "out" / "result.bundle"
    say("state: staged")
    read = staged_commits(spec, bundle) if spec.get("branch") and bundle.is_file() else ([], [])
    if read is None:
        say(f"team: the staged commits could not be read from {shown(bundle)}", err=True)
        read = ([], [])
    subjects, stat = read
    for subject in subjects:
        say(f"commit: {shown_line(subject)}")
    for line in stat:
        say(shown_line(line))
    files = sorted(str(path.relative_to(stage)) for path in out.rglob("*") if path.is_file()) if out.is_dir() else []
    for name in files:
        say(f"file: {shown_line(name)}")
    if subjects:
        say(f"team: nothing is in the repository yet; bring it back with .agents/bin/team apply {member} {task}",
            err=True)
    return 0


def apply(member: str, task: str) -> int:
    check_task(task)
    check_member(member)
    reserved(member)
    stage = stage_of(member, task)
    result, spec = read_json(stage / "result.json"), read_json(stage / "run.json")
    if not result or not spec:
        die(f"no run of {task} for {member}")
    if result.get("state") == "running":
        die(f"{member} is still working on {task}; apply it once the run has ended")
    if result.get("applied"):
        die(f"the staged run of {task} is applied already, on branch {result.get('branch')}")
    if not result.get("staged"):
        die(f"the run of {task} by {member} was not staged; its commits came back when it ended, nothing to apply")
    branch, at = spec.get("branch"), spec.get("repo_dir")
    if not branch:
        die(f"the run of {task} brings no commits back (no --repo, or a bot whose commits never come back)")
    bundle = stage / "out" / "result.bundle"
    if not bundle.is_file():
        die(f"the bundle of {task} is missing ({shown(bundle)}): the bot made no commits there, nothing to apply")
    if git("rev-parse", "-q", "--verify", f"refs/heads/{branch}", check=False, at=at).returncode == 0:
        die(f"branch {branch} exists already; apply never moves a branch: rename or delete it, then apply again")
    check_ref_conflict(branch, at)
    # As collect does for a run that is not staged: never forced, then the commits outside the task branch.
    fetched = git("fetch", "-q", str(bundle), f"task/{task}:refs/heads/{branch}", check=False, at=at)
    if fetched.returncode:
        die(f"the commits did not reach branch {branch}: {fetched.stderr.strip()[:200]}; they stay in {shown(bundle)}")
    others = keep_others(spec)
    error = "; ".join(e for e in (result.get("error"), others.pop("error")) if e) or None
    wip = git("log", "-1", "--format=%s", branch, check=False, at=at).stdout.startswith(f"wip({task}):")
    result = {**result, "staged": False, "applied": now(), "branch": branch, "wip": wip, "error": error, **others}
    write_json(stage / "result.json", result)
    lead_log(task, "done", f"{member}: staged run applied, branch {branch}")
    say(f"team: the staged run of {task} is applied; {member}'s commits are on branch {branch}")
    for line in others_lines(member, result):
        say(line, err=True)
    if error:
        say(f"team: {error}", err=True)
    return 1 if error else 0


# ---------------------------------------------------------------- the flow

def flow_end(flow: dict) -> dict | None:
    """A staged flow stops after a work step that is ready, before any gate or review runs on an unapplied result."""
    last = flow["steps"][-1] if flow.get("staged") and flow.get("steps") else {}
    if last.get("kind") not in WORK or last.get("outcome") != "done" or last.get("status") != "ready":
        return None
    if not read_json(stage_of(last["bot"], last["task"]) / "result.json").get("staged"):
        return None
    return {"end": "blocked",
            "why": f"staged: apply with team apply {last['bot']} {last['task']}, then team flow {flow['task']} --retry"}


def flow_retry(flow: dict) -> bool:
    """team flow --retry after team apply: the applied work step stays and the flow goes on from its commits,
    no longer staged. Not applied: False, and the step runs again (staged)."""
    last = flow["steps"][-1] if flow.get("staged") and flow.get("steps") else {}
    if last.get("kind") not in WORK:
        return False
    if not read_json(stage_of(last["bot"], last["task"]) / "result.json").get("applied"):
        return False
    flow.pop("staged")
    last["head"] = head_of(flow)
    return True


# ---------------------------------------------------------------- commands

def do_apply(rest: list[str]) -> int:
    config()
    if len(rest) != 2:
        die("usage: team apply ID TASK")
    return apply(*rest)


run.STAGED[:] = [sys.modules[__name__]]

COMMANDS = {"apply": do_apply}
