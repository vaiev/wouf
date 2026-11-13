"""Explicit flow endings: preserve all evidence, record closure and require a new brief to restart."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from teamlib import flow
from teamlib.core import KNOWN, PROJECT, die, git, lead_log, mask_secrets, now, put, read_json, say, write_json
from teamlib.run import record, split_repo

USAGE = """
  team flow TASK --close --reason TEXT    close a blocked, limited, question or retry flow, keeping every step
                                          (reason required; running and ready flows cannot close)
  Closed flows cannot resume (exit 6); --brief FILE starts a fresh flow, preserving the previous flow's evidence.
"""

CLOSABLE = ("blocked", "limited", "question", "retry")


def reason_text(reason: str | None) -> str:
    """Mask before cutting, and again after joining controls: a split secret cannot survive."""
    text = mask_secrets(reason or "", KNOWN)
    text = mask_secrets(re.sub(r"[\x00-\x1f\x7f\s]+", " ", text), KNOWN).strip()
    if not text:
        die("--close requires --reason TEXT")
    return text[:200]


def running(folder: Path, state: dict) -> bool:
    return (state.get("status") == "running" or flow.pid_running(state.get("pid")) or
            flow.pid_running(read_json(folder / "flow.lock").get("pid")))


def check_close(task: str, folder: Path, state: dict) -> None:
    if not state:
        die(f"no flow for {task}")
    if running(folder, state):
        die(f"the flow of {task} is running; cannot close a live flow")
    if state.get("status") not in (*CLOSABLE, "closed"):
        die(f"the flow of {task} is {state.get('status')}; cannot close it")


def close(task: str, folder: Path, reason: str | None) -> int:
    reason = reason_text(reason)
    state = read_json(folder / "state.json")
    check_close(task, folder, state)
    lock = flow.flow_lock(folder, task)
    try:
        # Re-read under the same lock as run_flow: a simultaneous resume must finish before a close.
        state = read_json(folder / "state.json")
        # Our own lock is live now; only the state's worker pid and status matter under it.
        if state.get("status") == "running" or flow.pid_running(state.get("pid")):
            die(f"the flow of {task} is running; cannot close a live flow")
        if state.get("status") == "closed":
            say(f"the flow of {task} is already closed")
            return 0
        if state.get("status") not in CLOSABLE:
            die(f"the flow of {task} is {state.get('status')}; cannot close it")
        state["closed"] = {"at": now(), "reason": reason, "from": state["status"]}
        state["status"] = "closed"
        write_json(folder / "state.json", state)
        # flow.step already projects end/status into the flows cache, without changing the board.
        record([("flow.step", "lead", task, {"kind": "end", "status": "closed"})])
        lead_log(task, "done", f"flow closed: {reason}")
        say(f"the flow of {task} is closed: {reason}")
        return 0
    finally:
        lock.unlink(missing_ok=True)


def write_state(folder: Path, state: dict) -> None:
    """A command edit cannot overwrite a close after reading an older state outside the lock."""
    lock = flow.flow_lock(folder, state["task"])
    try:
        if read_json(folder / "state.json").get("status") == "closed":
            say(f"the flow of {state['task']} is closed")
            raise SystemExit(flow.FLOW_EXIT["closed"])
        write_json(folder / "state.json", state)
    finally:
        lock.unlink(missing_ok=True)


def restart(ini, task: str, folder: Path, opts: dict, flags: set) -> int:
    """An explicit new brief starts with no steps; previous runs remain embedded as history."""
    text = Path(opts["--brief"]).read_text(encoding="utf-8")
    header = flow.flow_header(text)
    member = flow.active_id(opts["--bot"]) if opts["--bot"] else flow.class_options(ini, header["class"])["bot"]
    flow.plan_commits_nothing(header["class"], member)
    flow.bot(member)
    if opts["--scout"]:
        flow.bot(flow.active_id(opts["--scout"]))
    lock = flow.flow_lock(folder, task)
    try:
        old = read_json(folder / "state.json")
        if old.get("status") != "closed":
            die(f"the flow of {task} is no longer closed; cannot restart it")
        repo, ref = split_repo(opts["--repo"]) if opts["--repo"] else (Path(old["repo"]), old["branch"])
        repo = repo or PROJECT
        start = git("rev-parse", "-q", "--verify", f"{ref}^{{commit}}", check=False, at=repo).stdout.strip()
        if not start and not opts["--repo"]:
            start = old["base"]
        if not start:
            die(f"--repo {opts['--repo']}: no such commit")
        # Keep the old brief and state, including every step, without moving or deleting flow folders.
        history = old.pop("history", [])
        history.append({**old, "brief": (folder / "brief.md").read_text(encoding="utf-8")})
        fresh = {"task": task, "repo": str(repo), "base": start, "branch": old["branch"],
                 "class": header["class"], "scout": flow.preflight_scout(header), "research": header["research"],
                 "steps": [], "status": "new", "started": now(), "history": history,
                 **({"bot": opts["--bot"]} if opts["--bot"] else {}),
                 **({"scout_bot": opts["--scout"]} if opts["--scout"] else {})}
        put(folder / "brief.md", text)
        write_json(folder / "state.json", fresh)
        record([("flow.step", "lead", task, {"kind": "start", "status": "running"})])
        lead_log(task, "started", "fresh flow from a new brief after closure")
    finally:
        lock.unlink(missing_ok=True)
    return flow.cmd_flow(ini, [task, *sorted(flags)])


def handle(ini, task: str, folder: Path, opts: dict, flags: set) -> int | None:
    if "--close" in flags:
        if flags != {"--close"} or any(value for key, value in opts.items() if key != "--reason"):
            die("--close takes only --reason TEXT")
        return close(task, folder, opts["--reason"])
    if opts["--reason"] is not None:
        die("--reason requires --close")
    if read_json(folder / "state.json").get("status") == "closed":
        if opts["--brief"]:
            return restart(ini, task, folder, opts, flags)
        say(f"the flow of {task} is closed; use --brief FILE to start a fresh flow")
        return flow.FLOW_EXIT["closed"]
    return None


flow.FLOW_END[:] = [sys.modules[__name__]]
