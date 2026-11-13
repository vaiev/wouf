"""teamlib.attest: the unsigned change receipt of one landed commit (receipts v1, R3a; docs/RECEIPTS.md). Built from
the team's own records only: the flow's state.json and its steps (with the evidence teamlib.evidence kept), gate.json,
the run.finished events and each review run's run.json and output.log, and git. A step of a hand-edited state.json
counts only where the events and run.json agree with it. Nothing is signed or stored here (R3b): `team receipt make
... --dry-run` prints the canonical body, after teamlib.receipt.check_receipt passed it. A leaf: receipt, evidence and
state are leaves too, reached at call time.

Without --dry-run, make signs the body with the federation key and stores it as SPEC 2.6 says (R3b): the file
team/receipts/<landed>.json, the receipt.signed event and the note refs/notes/wouf-receipts on the landed commit, one
set of bytes. resign is the one replacement of a stored receipt: same body, signed_at kept, resigned_at set, the
current key. Nothing is pushed or fetched here (R5)."""

from __future__ import annotations

import copy
import fcntl
import importlib
import tempfile

from teamlib.core import *  # noqa: F401,F403
from teamlib.flow import FLOW_RUN, RUN_KINDS, class_options, flow_dir, review_verdict
from teamlib.gate import evidence
from teamlib.run import masked_lines, return_block, stage_of

USAGE = """
  team receipt make TASK --reviewed SHA --landed SHA [--repo PATH] [--repo-name LABEL] [--pr N] [--brief FILE]
                    --dry-run                   the unsigned change receipt of one landed commit, checked; prints
                                                its canonical body and signs nothing (refused, in one line, without
                                                a green gate on the reviewed head, enough approvals or the reviewed tree)
                    without --dry-run           signs it with [federation] signing_key and stores it: the file
                                                team/receipts/<landed>.json, the receipt.signed event and the note
                                                refs/notes/wouf-receipts on the landed commit (the same bytes); the
                                                same receipt again changes nothing, another one is refused
  team receipt resign SHA [--repo PATH]         signs the stored receipt of landed commit SHA again with the current
                                                key: signed_at kept, resigned_at set, nothing else changed; replaces
                                                the file and the note, writes receipt.resigned
  team receipt export SHA --in-toto [--repo PATH]  export a stored receipt as an in-toto Statement
  team receipt need TASK                        the reviews the task's class requires: reviews <n>"""

UNKNOWN = "unknown"
HEX40 = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")
SHA_PREFIX = re.compile(r"[0-9a-f]{7,40}")
SHA1_ONLY = "v1 is SHA-1 only (SPEC 2.1)"
# git reads no system or global config (a hook, a replace ref or an alias of this computer's user changes nothing),
# and no GIT_* variable of the caller points it elsewhere.
GIT_ENV = {**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")}, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_NO_REPLACE_OBJECTS": "1", "GIT_TERMINAL_PROMPT": "0"}
# A gate step recorded before R2.1 has no code: its why, the gate's own fixed words (teamlib.gate), tells it.
OLD_GATE_CODE = {"gate command passed": 0, "paths checked; no gate command in [flow] gate": None}
NOTES_REF = "refs/notes/wouf-receipts"
# The times a signature sets; every other byte of a stored receipt is fixed once it is stored (SPEC 2.6).
TIMES = ("signed_at", "resigned_at")
RUN_SUFFIX = {"worker": "", "bugfixer": "", "scout": ".scout", "research": ".research"}


def lib(name: str):
    """A leaf of teamlib (receipt, evidence, state), loaded when a command runs."""
    return importlib.import_module(f"teamlib.{name}")


def refuse(reason: str) -> None:
    raise lib("receipt").Refused(reason)


# ---------------------------------------------------------------- git

def git_out(repo: str, *args: str) -> str | None:
    done = subprocess.run(["git", "--no-replace-objects", "-C", repo, *args], env=GIT_ENV, capture_output=True,
                          text=True)
    return done.stdout.strip() if done.returncode == 0 else None


def sha1(value, what: str) -> str:
    """value when it is a full SHA-1 commit id; a SHA-256 id (64 hex, which R2 records) is refused: v1 holds SHA-1."""
    if isinstance(value, str) and HEX64.fullmatch(value):
        refuse(f"{what} is a SHA-256 commit id: {SHA1_ONLY}")
    if not isinstance(value, str) or not HEX40.fullmatch(value):
        refuse(f"{what} must be a full commit id (40 lower-case hex)")
    return value


def recorded_sha(value, what: str) -> str:
    """A commit a record holds: 40 hex as it is, else unknown; 64 hex is refused (sha1)."""
    if isinstance(value, str) and HEX64.fullmatch(value):
        sha1(value, what)
    return value if isinstance(value, str) and HEX40.fullmatch(value) else UNKNOWN


def commit_in(repo: str, value: str, what: str) -> str:
    sha1(value, what)
    if git_out(repo, "rev-parse", "-q", "--verify", f"{value}^{{commit}}") != value:
        refuse(f"{what} is no commit in the repository")
    return value


def change_of(repo: str, base: str, reviewed: str, landed: str, pr) -> dict:
    """receipt.change: squash (landed^1 is base, tree(landed) is tree(reviewed)) or fast-forward (landed is reviewed);
    one parent, at least one file (submodules counted), reachable from refs/heads/main."""
    if git_out(repo, "rev-parse", "--show-object-format") not in (None, "sha1"):
        refuse(f"the repository is no SHA-1 repository: {SHA1_ONLY}")
    for value, what in ((base, "change.base"), (reviewed, "--reviewed"), (landed, "--landed")):
        commit_in(repo, value, what)
    parents = (git_out(repo, "rev-list", "--parents", "-n", "1", landed) or "").split()[1:]
    if len(parents) != 1:
        refuse(f"the landed commit has {len(parents)} parents: a receipt vouches for a squash or a fast-forward")
    if landed == reviewed:
        merge = "fast-forward"
        if git_out(repo, "merge-base", "--is-ancestor", base, reviewed) is None:
            refuse("fast-forward: change.base is not an ancestor of the reviewed commit")
    elif git_out(repo, "merge-base", "--is-ancestor", reviewed, landed) is not None:
        refuse("fast-forward: the landed commit is not the reviewed commit")
    else:
        merge = "squash"
        if parents[0] != base:
            refuse("squash: landed^1 is not change.base (main moved before the merge, or another base)")
        if git_out(repo, "rev-parse", f"{landed}^{{tree}}") != git_out(repo, "rev-parse", f"{reviewed}^{{tree}}"):
            refuse("squash: the landed tree is not the reviewed tree")
    names = git_out(repo, "diff", "--name-only", "-z", "--no-renames", "--ignore-submodules=none", base, landed)
    files = len([n for n in (names or "").split("\0") if n])
    if not files:
        refuse("files = 0: the change holds no file, nothing to vouch for")
    if git_out(repo, "merge-base", "--is-ancestor", landed, "refs/heads/main") is None:
        refuse("the landed commit is not reachable from refs/heads/main")
    return {"base": base, "reviewed": reviewed, "landed": landed, "landed_parent": parents[0],
            "tree": git_out(repo, "rev-parse", f"{landed}^{{tree}}"), "files": files, "merge": merge, "pr": pr}


# ---------------------------------------------------------------- the gate

def gate_of(flow: dict, task: str, reviewed: str, base: str, events: dict) -> dict:
    """receipt.gate: the flow's last green gate step on the reviewed head, else gate.json on it, green; its base must
    be change.base. Values of R2.1 as they are; a step of before R2.1 gets its code from its fixed why. A step counts
    only where an independent record holds it: gate.json while it is on the reviewed head, else a green gate event
    bound to that head (flow_gate); a step only the event holds keeps network unknown (no flag rests on a value
    nothing corroborates). A gate event without a head (an older flow) binds nothing: such a flow builds from
    gate.json, or is refused."""
    steps = flow.get("steps") or []
    found = [at for at, st in enumerate(steps) if isinstance(st, dict) and st.get("kind") == "gate"
             and st.get("head") == reviewed and st.get("outcome") == "green"]
    state = read_json(env_dir() / "gate" / task / "gate.json")
    on_head = state.get("head") == reviewed and state.get("state") == "green"
    if found:
        step = steps[found[-1]]
        kept = evidence("gate_step", step)
        if "code" not in step:
            kept["code"] = OLD_GATE_CODE.get(step.get("why"), UNKNOWN)
        recorded = evidence("gate_step", state)
        if state.get("head") == reviewed:
            # gate.json, which team gate itself writes, holds the gate of this head: the step must be that gate. A key
            # a step of before R2.1 lacks is not compared.
            if not on_head or any(kept[k] != recorded[k] for k in kept if k in step or k == "code"):
                refuse("the flow's green gate step on the reviewed head is not the gate gate.json records (another "
                       "gate on it): a hand-edited state.json is refused as evidence")
        elif flow_gate(reviewed, base, events):
            # A later gate on another head overwrote gate.json: the gate's own event holds this one.
            kept["network"] = UNKNOWN
        else:
            refuse("the flow's green gate step on the reviewed head is held by no record (gate.json is on another "
                   "head, no gate event on the reviewed head): a hand-edited state.json is refused as evidence")
    else:
        if not on_head:
            refuse("no green gate on the reviewed head (no flow gate step, and gate.json is not one)")
        kept = evidence("gate_step", state)
    if kept["code"] == UNKNOWN:
        refuse("the green gate's exit code is not recorded")
    if kept["base"] != base:
        refuse("the gate's base is not change.base")
    return {"state": "green", "head": reviewed, **kept}


def flow_gate(head: str, base: str, events: dict) -> bool:
    """Whether the event log holds a green gate flow.step event of the task on head (flow.step_event binds a gate's
    head and base): its head is head, and its base, when it has one, change.base. State.json is not read: its steps,
    their heads and their order are the editor's."""
    return any(data.get("head") == head and data.get("base", base) == base for data in events["gates"])


# ---------------------------------------------------------------- the records

def lead_events() -> list:
    """The lead's local events in this team's own event file (a copied or federated file is not read), with a data
    object."""
    log = lib("state").store()
    folder = Path(log.events_dir())
    try:
        origin = (folder / "ORIGIN").read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return []
    path = folder / f"{origin}.jsonl"
    if path.is_symlink() or not path.is_file():
        return []
    return [e for e in log.event_read(path)[0] if e.get("actor") == "lead" and e.get("scope") == "local"
            and isinstance(e.get("data"), dict)]


def run_events(task: str) -> dict:
    """The lead's records of the task in this team's own event file (the lead wrote it, locally; a copied or federated
    file is not read): "runs", the newest run.finished event of each run (TASK, TASK.scout, TASK.r<n>, ...); "bots",
    the newest of each run and bot ((run, bot): a worker and a bugfixer share a run id); "gates", the data of each
    green gate flow.step event of the task."""
    found = {"runs": {}, "bots": {}, "gates": []}
    for event in lead_events():
        name, data = event.get("task"), event.get("data")
        mine = name == task or (isinstance(name, str) and (FLOW_RUN.fullmatch(name) or [None, None])[1] == task)
        if event.get("kind") == "run.finished" and mine:
            found["runs"][name] = found["bots"][(name, data.get("bot"))] = data
        elif event.get("kind") == "flow.step" and name == task and data.get("kind") == "gate" \
                and data.get("status") == "green":
            found["gates"].append(data)
    return found


def stage_agrees(run_id: str, data, review: bool):
    """(stage, run.json) of a run when they are the run its event names, else None: another bot or task, or another
    commit than the event's ref (a review's ref is its commit; a worker's may be its branch)."""
    data = data if isinstance(data, dict) else {}
    bot_id = str(data.get("bot") or "")
    stage = stage_of(bot_id, run_id) if member_problem(bot_id) is None else None
    spec = read_json(stage / "run.json") if stage else {}
    ref = data.get("ref")
    same = spec.get("task") == run_id and spec.get("member") == data.get("bot") and isinstance(ref, str) and \
        (ref == spec.get("commit") or (not review and spec.get("branch") and ref == spec.get("branch")))
    return (stage, spec) if same else None


def stage_record(run_id: str, data: dict, review: bool) -> tuple[Path, dict]:
    """stage_agrees, refused as evidence when they are not the run of its event (a reused stage folder)."""
    agrees = stage_agrees(run_id, data, review)
    if agrees is None:
        refuse(f"run {run_id}: its run.json is not the run of its run.finished event (another commit, bot or task: "
               "a reused stage folder); refused as evidence")
    return agrees


def egress_kept(run_id: str, kind: str, step: dict, events: dict) -> dict:
    """runs[].egress of a flow step. A step that claims a complete proxy log (a sha256) holds it only where its run
    corroborates it: its run.finished event (this run and bot), run.json (stage_agrees, the step's commit) and the
    summary of the stage's egress.log as teamlib.evidence reads it are the step's. Else the egress is unknown (fail
    closed: a hand-edited step gives no flag). Any other log (unknown, null) is taken as it is."""
    kept = egress_of(step.get("egress"))
    if not (isinstance(kept["log"], str) and HEX64.fullmatch(kept["log"])):
        return kept
    agrees = stage_agrees(run_id, events["bots"].get((run_id, step.get("bot"))), kind == "review")
    if agrees and agrees[1].get("commit") == step.get("commit") and \
            egress_of(lib("evidence").egress_summary(*agrees)) == kept:
        return kept
    return egress_of({})


def typed_sha(lines: list, name: str, run_id: str):
    """The commit a review typed after APPROVE in its last signed block, when it is 7 to 40 hex; else None."""
    start = return_block(lines, name, run_id)
    rest = lines[start].split(f"{name} · {run_id} · ", 1)[-1].split() if start is not None else []
    typed = rest[1].lower() if len(rest) > 1 and rest[0].upper() == "APPROVE" else ""
    return typed if SHA_PREFIX.fullmatch(typed) else None


def confirmed(run_id: str, bot_id, commit: str, events: dict):
    """(verdict, approve_sha) as review_verdict reads the run's own output.log for its commit, when its run.finished
    event (done, ref = commit) and its run.json (this task, bot and commit) agree; None when they do not."""
    data = events["runs"].get(run_id)
    if not data or data.get("outcome") != "done" or data.get("ref") != commit or data.get("bot") != bot_id \
            or commit == UNKNOWN:
        return None
    stage = stage_of(str(bot_id), run_id)
    spec = read_json(stage / "run.json")
    if (spec.get("task"), spec.get("member"), spec.get("commit")) != (run_id, bot_id, commit):
        return None
    lines, name = masked_lines(stage / "output.log"), str(spec.get("name") or "")
    verdict = review_verdict(lines, name, run_id, commit)["verdict"] or UNKNOWN
    return verdict, typed_sha(lines, name, run_id) if verdict in ("approve", "stale") else None


def tool_of(ini, runner, cli, model) -> dict:
    """runner, cli, vendor and model: names from the config, else unknown; the vendor is the CLI's fixed map."""
    receipt = lib("receipt")
    runner = runner if isinstance(runner, str) and receipt.NAME.fullmatch(runner) else UNKNOWN
    if not (isinstance(cli, str) and receipt.NAME.fullmatch(cli)):
        cli = lib("evidence").cli_name(ini, None if runner == UNKNOWN else runner, None)
    model = model if isinstance(model, str) and receipt.MODEL.fullmatch(model) else UNKNOWN
    return {"runner": runner, "cli": cli, "vendor": receipt.VENDORS.get(cli, UNKNOWN), "model": model}


def egress_of(kept) -> dict:
    """runs[].egress as a step keeps it. R2.1's log (64 hex, unknown, null) as it is; before R2.1 (true, false) or
    without the key it is unknown: no sha was kept."""
    kept = kept if isinstance(kept, dict) else {}
    log = kept.get("log", UNKNOWN)
    if not (log is None or (isinstance(log, str) and (log == UNKNOWN or HEX64.fullmatch(log)))):
        log = UNKNOWN
    hosts = kept.get("hosts") if isinstance(kept.get("hosts"), list) else []
    mode = kept.get("mode")
    return {"mode": mode if mode in lib("receipt").EGRESS_MODES else UNKNOWN, "log": log,
            "hosts": sorted({str(h) for h in hosts}),
            **{k: kept[k] if type(kept.get(k)) is int else 0 for k in ("private", "refused")}}


def tier_of(value) -> str:
    """A tier as the receipt names it: the fallback arrow of before R2.1 (mid→strong) in ASCII (mid->strong)."""
    tier = str(value or "").replace("→", "->")
    return tier if lib("receipt").TIER.fullmatch(tier) else UNKNOWN


def run_entry(ini, run_id: str, role: str, bot_id, kept: dict, outcome) -> dict:
    """One runs[] entry from what a step or run_record kept."""
    tokens = kept.get("tokens")
    return {"run": run_id, "role": role, "bot": str(bot_id or UNKNOWN),
            **tool_of(ini, kept.get("runner"), kept.get("cli"), kept.get("model")), "tier": tier_of(kept.get("tier")),
            "commit": recorded_sha(kept.get("commit"), f"run {run_id} commit"),
            "outcome": outcome if isinstance(outcome, str) and outcome else UNKNOWN,
            "tokens": tokens if isinstance(tokens, str) and lib("receipt").TOKENS.fullmatch(tokens) else UNKNOWN,
            "egress": egress_of(kept.get("egress"))}


def round_entry(run: dict, reviewed: str, verdict, typed, source: str, proof) -> dict:
    """One reviews.rounds[] entry: its head is the run's input commit; it counts only when its own output.log
    confirmed an approve of exactly that commit (proof) and the commit is the reviewed one."""
    verdict = verdict if verdict in lib("receipt").VERDICTS else UNKNOWN
    counts = bool(proof) and proof[0] == "approve" and run["commit"] == reviewed and isinstance(proof[1], str) \
        and reviewed.startswith(proof[1])
    return {"run": run["run"], "index": 0, "bot": run["bot"], "runner": run["runner"], "cli": run["cli"],
            "vendor": run["vendor"], "model": run["model"], "verdict": verdict, "head": run["commit"],
            "approve_sha": typed, "counts": counts, "source": source}


def step_runs(task: str, steps: list) -> list:
    """(step, kind, run id) of each step of the flow; a run id the step lacks (it never ran) is the flow's own."""
    found, reviews = [], 0
    for step in steps:
        kind = step.get("kind") if isinstance(step, dict) else None
        reviews += kind == "review"
        run_id = None
        if kind in RUN_KINDS:
            run_id = str(step.get("task") or (f"{task}.r{reviews}" if kind == "review" else task + RUN_SUFFIX[kind]))
        found.append((step, kind, run_id))
    return found


def flow_records(ini, task: str, flow: dict, reviewed: str, events: dict) -> tuple[list, list]:
    """runs[] and rounds of the flow's bot steps. A worker run id repeats (worker, then bugfixer); a review run id
    never does, so a repeated one is refused. A step's egress counts as its run corroborates it (egress_kept)."""
    runs, rounds, seen = [], [], set()
    for step, kind, run_id in step_runs(task, flow.get("steps") or []):
        if kind not in RUN_KINDS:
            continue
        run = run_entry(ini, run_id, kind, step.get("bot"), step, step.get("outcome"))
        run["egress"] = egress_kept(run_id, kind, step, events)
        runs.append(run)
        if kind == "review":
            # A review run id is new for every round (flow_step: r<n+1>); a repeat is a copied step, not a round.
            if run_id in seen:
                refuse(f"flow step {run_id} repeats a review run: a hand-edited state.json is refused as evidence")
            seen.add(run_id)
            proof = confirmed(run_id, step.get("bot"), run["commit"], events)
            verdict = proof[0] if proof else step.get("verdict")
            rounds.append(round_entry(run, reviewed, verdict, proof[1] if proof else None, "flow", proof))
    return runs, rounds


def outside_records(ini, task: str, known: set, reviewed: str, events: dict) -> tuple[list, list]:
    """runs[] and rounds of the task's runs outside the flow, from their run.finished events: their stage folder
    (checked against the event), run_record for commit, model, tokens and egress, cli from the config."""
    runs, rounds = [], []
    for run_id in sorted(set(events["runs"]) - known):
        data = events["runs"][run_id]
        found = FLOW_RUN.fullmatch(run_id)
        kind = run_id.rsplit(".", 1)[1] if found else "worker"
        review = kind.startswith("r") and kind[1:].isdigit()
        stage, spec = stage_record(run_id, data, review)
        kept = {**evidence("run_record", ini, stage, spec, read_json(stage / "result.json"), spec.get("runner")),
                "runner": spec.get("runner"), "tier": spec.get("tier")}
        if HEX64.fullmatch(str(spec.get("commit"))):
            sha1(spec["commit"], f"run {run_id} commit")
        run = run_entry(ini, run_id, "review" if review else kind, data.get("bot"), kept, data.get("outcome"))
        runs.append(run)
        if review:
            proof = confirmed(run_id, data.get("bot"), run["commit"], events)
            rounds.append(round_entry(run, reviewed, proof[0] if proof else UNKNOWN, proof[1] if proof else None,
                                      "run", proof))
    return runs, rounds


def sovereign(runs: list, gate: dict) -> dict:
    """The flag of SPEC 2.5 as R2.1 sets it: every run's egress.log is a sha256 (a complete proxy log) and names no
    public host (private pairs only), and the gate ran on network none. Anything else gives no flag."""
    whole = bool(runs) and all(isinstance(r["egress"]["log"], str) and HEX64.fullmatch(r["egress"]["log"])
                               and not r["egress"]["hosts"] for r in runs)
    return {"sovereign": True} if whole and gate["network"] == "none" else {}


def usage_of(runs: list) -> dict:
    known = [r["tokens"] for r in runs if r["tokens"] != UNKNOWN]
    return {"tokens": sum(int(n) for t in known for n in t.split("/")), "known_runs": len(known),
            "unknown_runs": len(runs) - len(known), "cost": UNKNOWN}


def class_of(task: str, flow: dict) -> str:
    """The task's class: its flow's, else the store's."""
    if flow.get("class"):
        return str(flow["class"])
    state = lib("state")
    item = next((t for t in state.snapshot(state.store())["tasks"] if t.get("id") == task), None)
    if not item or not item.get("class"):
        refuse(f"the class of {task} is not known (no flow, not in the store)")
    return str(item["class"])


def brief_of(flow_folder: Path, file: str | None) -> dict:
    path, source = (Path(file), "file") if file else (flow_folder / "brief.md", "flow")
    if path.is_symlink() or not path.is_file():
        refuse("no brief: the flow has no brief.md and no --brief FILE was given")
    return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "source": source}


def stamp() -> str:
    """Now, UTC, as a receipt writes a time."""
    return datetime.now(timezone.utc).strftime(lib("receipt").TIME)


def repo_of(task: str, given) -> str:
    """The repository of the task: --repo, else the flow's, else this project."""
    return str(given or read_json(flow_dir(task) / "state.json").get("repo") or PROJECT)


def open_flow(flow: dict) -> dict:
    """The flow as evidence of a landing: a closed flow (state.json holds a `closed` object) is history, not evidence,
    so it is none, and build reads as without a flow: base and gate from gate.json, runs and rounds from the run
    events. The lead may drive a task by hand after its flow closed; the closed flow's base and steps are not that
    landing's."""
    return {} if isinstance(flow.get("closed"), dict) else flow


def build(ini, task: str, o: dict) -> dict:
    """The unsigned receipt of one landed commit, checked by receipt.check_receipt. Raises receipt.Refused."""
    flow = read_json(flow_dir(task) / "state.json")
    evident = open_flow(flow)
    reviewed, landed = sha1(o["--reviewed"], "--reviewed"), sha1(o["--landed"], "--landed")
    base = evident.get("base") if evident else read_json(env_dir() / "gate" / task / "gate.json").get("base")
    base = sha1(base, "change.base")
    repo = repo_of(task, o["--repo"])
    change = change_of(str(repo), base, reviewed, landed, o["--pr"])
    events = run_events(task)
    gate = gate_of(evident, task, reviewed, base, events)
    runs, rounds = flow_records(ini, task, evident, reviewed, events)
    more_runs, more_rounds = outside_records(ini, task, {r["run"] for r in runs}, reviewed, events)
    runs, rounds = runs + more_runs, rounds + more_rounds
    for index, item in enumerate(rounds, 1):
        item["index"] = index
    klass = class_of(task, flow)
    required, approved = class_options(ini, klass)["reviews"], sum(1 for r in rounds if r["counts"])
    if approved < required:
        refuse(f"{approved} counting approvals of the reviewed head; class {klass} requires {required}")
    team = get(ini, "federation", "name")
    if not team:
        refuse("[federation] name is not set: the receipt names its team")
    body = {"schema": lib("receipt").SCHEMA, "team": team, "signed_at": stamp(), "resigned_at": None,
            "repo": o["--repo-name"] or Path(str(repo)).resolve().name, "task": task, "class": klass,
            "brief": brief_of(flow_dir(task), o["--brief"]), "change": change, "gate": gate,
            "reviews": {"required": required, "approved": approved, "rounds": rounds}, "runs": runs,
            "usage": usage_of(runs), "flags": sovereign(runs, gate)}
    lib("receipt").check_receipt(body)
    return body


# ---------------------------------------------------------------- the store (SPEC 2.6, R3b)

def key_of(ini) -> str:
    """[federation] signing_key, checked by receipt.signing_key (outside the team folder, .pub next to it, Ed25519);
    only its .pub is read, the private key is opened by ssh-keygen alone. Refused before anything is written."""
    return lib("receipt").signing_key(get(ini, "federation", "signing_key") or "")


def signed_by(body: dict, sig: str, key: str) -> bool:
    """Whether sig is a good receipt signature over body by the key whose .pub lies next to key."""
    words = Path(f"{key}.pub").read_text(encoding="utf-8", errors="replace").split()[:2]
    with tempfile.TemporaryDirectory() as folder:
        signers = Path(folder) / "allowed_signers"
        put(signers, f'{body["team"]} namespaces="{lib("receipt").NAMESPACE}" {" ".join(words)}\n')
        return lib("receipt").signature_ok(body, sig, signers)


def without_times(body: dict) -> bytes:
    return lib("receipt").canonical({k: v for k, v in body.items() if k not in TIMES})


def receipt_path(landed: str) -> Path:
    path = team_dir() / "receipts" / f"{landed}.json"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        refuse(f"team/receipts/{landed}.json is no plain file")
    return path


def read_stored(path: Path) -> bytes | None:
    return path.read_bytes() if path.is_file() else None


def notes_env(repo: str) -> dict:
    """GIT_ENV and the caller's own git identity (its user.name and user.email, as git var tells it), which the
    notes commit needs; refused before anything is written when git has none."""
    caller = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") or
              k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")}
    done = subprocess.run(["git", "-C", repo, "var", "GIT_COMMITTER_IDENT"], env=caller, capture_output=True,
                          text=True)
    found = re.fullmatch(r"(.+) <([^<>]+)> [0-9]+ [+-][0-9]{4}", done.stdout.strip()) if not done.returncode else None
    if not found:
        refuse("git has no user.name and user.email for the commit of the receipt note")
    name, email = found.groups()
    return {**GIT_ENV, "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": name,
            "GIT_COMMITTER_EMAIL": email}


def note_of(repo: str, landed: str) -> bytes | None:
    """The bytes of the note on landed in refs/notes/wouf-receipts, else None."""
    base = ["git", "--no-replace-objects", "-C", repo, "notes", f"--ref={NOTES_REF}"]
    listed = subprocess.run([*base, "list", landed], env=GIT_ENV, capture_output=True)
    if listed.returncode:
        return None
    shown = subprocess.run([*base, "show", landed], env=GIT_ENV, capture_output=True)
    if shown.returncode:
        refuse("the receipt note on the landed commit cannot be read")
    return shown.stdout


def write_receipt(path: Path, data: bytes, replace: bool) -> None:
    """The file, whole or not at all: written next to it, read back (a mask on the way would change the signed
    bytes), then linked in (never over an existing file) or, for resign, moved over the old one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.unlink(missing_ok=True)
    try:
        put_bytes(temp, data)
        if temp.read_bytes() != data:
            refuse("the receipt file would not hold the signed bytes")
        if replace:
            os.replace(temp, path)
        else:
            try:
                os.link(temp, path)
            except FileExistsError:
                refuse("a receipt for the landed commit was stored meanwhile")
    finally:
        temp.unlink(missing_ok=True)


def add_note(repo: str, landed: str, path: Path, data: bytes, env: dict, force: bool) -> None:
    done = subprocess.run(["git", "--no-replace-objects", "-C", repo, "notes", f"--ref={NOTES_REF}", "add",
                           *(["-f"] if force else []), "-F", str(path), landed], env=env, capture_output=True)
    if done.returncode:
        refuse("git notes add failed for the receipt note")
    if note_of(repo, landed) != data:
        refuse("the receipt note does not hold the bytes of the stored file")


def recorded(landed: str, digest: str) -> bool:
    """Whether a receipt event of the lead already holds this stored file."""
    return any(e.get("kind") in ("receipt.signed", "receipt.resigned") and e["data"].get("landed") == landed
               and e["data"].get("sha256") == digest for e in lead_events())


def last_recorded(landed: str) -> str | None:
    """The sha256 of the newest receipt event of the lead for landed (receipt.signed or receipt.resigned), else None."""
    found = [e["data"].get("sha256") for e in lead_events()
             if e.get("kind") in ("receipt.signed", "receipt.resigned") and e["data"].get("landed") == landed]
    return found[-1] if found else None


def receipt_event(kind: str, body: dict, data: bytes) -> None:
    change = body["change"]
    lib("state").store().event_append(kind, "lead", body["task"], {
        "task": body["task"], "landed": change["landed"], "reviewed": change["reviewed"],
        "sha256": hashlib.sha256(data).hexdigest()})


def receipts_lock():
    """One store command at a time (team/receipts/.lock); event_append takes its own lock."""
    folder = team_dir() / "receipts"
    folder.mkdir(parents=True, exist_ok=True)
    handle = (folder / ".lock").open("a+b")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def store(ini, task: str, o: dict) -> str:
    """make without --dry-run: the key is checked, the body built, signed and its signature checked before anything
    is written. A stored file or note for the landed commit that holds the same receipt (all but its times) and a
    good signature by the current key is kept as it is, and only a missing part is added; any other is refused."""
    receipt = lib("receipt")
    key = key_of(ini)
    o = {**o, "--repo": repo_of(task, o["--repo"])}
    landed = commit_in(o["--repo"], sha1(o["--landed"], "--landed"), "--landed")
    env = notes_env(o["--repo"])
    with receipts_lock():
        body = build(ini, task, o)
        path = receipt_path(landed)
        on_file, note = read_stored(path), note_of(o["--repo"], landed)
        if on_file is not None and note is not None and on_file != note:
            refuse("the receipt note on the landed commit is not the stored file")
        kept = on_file if on_file is not None else note
        if kept is None:
            sig = receipt.sign(body, key)
            data = receipt.stored(body, sig)
        else:
            if note is not None and on_file is None:
                try:
                    receipt.parse(note)
                except receipt.Refused:
                    refuse("the receipt note on the landed commit holds no receipt (another note)")
            old = receipt.parse(kept)
            if without_times(old["receipt"]) != without_times(body):
                refuse("a different receipt for the landed commit is stored; only team receipt resign replaces it")
            body, sig, data = old["receipt"], old["sig"], kept
        if not signed_by(body, sig, key):
            refuse("the receipt is not signed by the current key (team receipt resign signs a stored one again)")
        done = []
        if on_file is None:
            write_receipt(path, data, replace=False)
            done.append(f"stored team/receipts/{landed}.json")
        if note is None:
            add_note(o["--repo"], landed, path, data, env, force=False)
            done.append(f"note {NOTES_REF}")
        kind = "receipt.resigned" if body["resigned_at"] is not None else "receipt.signed"
        if not recorded(landed, hashlib.sha256(data).hexdigest()):
            receipt_event(kind, body, data)
            done.append(f"event {kind}")
    return f"receipt {landed}: " + (", ".join(done) if done else f"unchanged (team/receipts/{landed}.json)")


def resigned_of(body: dict, at: str) -> dict:
    """The stored body signed again: signed_at kept, resigned_at now."""
    return {**copy.deepcopy(body), "resigned_at": at}


def resign(ini, landed: str, o: dict) -> str:
    """team receipt resign: the stored receipt of landed signed with the current key. Refused unless the new body
    without signed_at and resigned_at is byte-equal to the stored one and signed_at is kept; then the file and the
    note (git notes add -f) are replaced and receipt.resigned is written."""
    receipt = lib("receipt")
    key = key_of(ini)
    landed = sha1(landed, "the landed commit")
    with receipts_lock():
        path = receipt_path(landed)
        kept = read_stored(path)
        if kept is None:
            refuse(f"no stored receipt for {landed} (team/receipts/{landed}.json): team receipt make first")
        # Only the bytes the lead recorded last are signed again: a hand-edited file (its old signature kept or not)
        # would otherwise come out of resign as a good signature by the current key.
        if last_recorded(landed) != hashlib.sha256(kept).hexdigest():
            refuse("the stored receipt is not the one the lead's newest receipt event records (an edited file); "
                   "resign signs only a recorded receipt")
        old = receipt.parse(kept)["receipt"]
        if old["change"]["landed"] != landed:
            refuse("the stored receipt is not the receipt of its file name's commit")
        repo = repo_of(old["task"], o["--repo"])
        commit_in(repo, landed, "the landed commit")
        env = notes_env(repo)
        note = note_of(repo, landed)
        if note is not None and note != kept:
            try:
                same = without_times(receipt.parse(note)["receipt"]) == without_times(old)
            except receipt.Refused:
                same = False
            if not same:
                refuse("the receipt note on the landed commit holds another receipt than the stored file")
        body = resigned_of(old, stamp())
        if without_times(body) != without_times(old) or body.get("signed_at") != old["signed_at"] \
                or set(body) != set(old):
            refuse("a resign changes signed_at and resigned_at only: the new body differs in another field")
        sig = receipt.sign(body, key)
        data = receipt.stored(body, sig)
        if not signed_by(body, sig, key):
            refuse("the receipt is not signed by the current key")
        write_receipt(path, data, replace=True)
        add_note(repo, landed, path, data, env, force=True)
        receipt_event("receipt.resigned", body, data)
    return f"receipt {landed}: re-signed at {body['resigned_at']}, replaced team/receipts/{landed}.json and note " \
           f"{NOTES_REF}, event receipt.resigned"


# ---------------------------------------------------------------- commands

def make_args(args: list[str]) -> tuple[str, dict, bool]:
    o = {"--reviewed": None, "--landed": None, "--repo": None, "--repo-name": None, "--pr": None, "--brief": None}
    positional, dry = [], False
    it = iter(args)
    for arg in it:
        if arg in o:
            o[arg] = next(it, None)
        elif arg == "--dry-run":
            dry = True
        else:
            positional.append(arg)
    if len(positional) != 1 or not o["--reviewed"] or not o["--landed"] or None in [o[k] for k in o if k in args]:
        die("usage: team receipt make TASK --reviewed SHA --landed SHA [--repo PATH] [--repo-name LABEL] [--pr N] "
            "[--brief FILE] [--dry-run]")
    if o["--pr"] is not None and not re.fullmatch(r"[1-9][0-9]{0,8}", o["--pr"]):
        die("--pr takes a pull request number from 1")
    o["--pr"] = int(o["--pr"]) if o["--pr"] else None
    task_arg(positional[0])
    return positional[0], o, dry


def cmd_receipt(ini, args: list[str]) -> int:
    action, rest = (args[0] if args else ""), args[1:]
    if action == "export":
        return lib("intoto").cmd_export(rest)
    if action == "need" and len(rest) == 1:
        task_arg(rest[0])
        try:
            klass = class_of(rest[0], read_json(flow_dir(rest[0]) / "state.json"))
        except lib("receipt").Refused as problem:
            die(f"receipt {rest[0]}: {problem}", 1)
        say(f"reviews {class_options(ini, klass)['reviews']}")
        return 0
    if action == "resign":
        o = {"--repo": None}
        if len(rest) == 3 and rest[1] == "--repo":
            o["--repo"] = rest[2]
        elif len(rest) != 1:
            die("usage: team receipt resign SHA [--repo PATH]")
        try:
            say(resign(ini, rest[0], o))
        except lib("receipt").Refused as problem:
            die(f"receipt {rest[0][:40]}: refused: {problem}", 1)
        return 0
    if action != "make":
        die("usage: team receipt make TASK --reviewed SHA --landed SHA ... [--dry-run] | team receipt resign SHA "
            "| team receipt export SHA --in-toto [--repo PATH] | team receipt need TASK")
    task, o, dry = make_args(rest)
    try:
        if not dry:
            say(store(ini, task, o))
            return 0
        body = build(ini, task, o)
    except lib("receipt").Refused as problem:
        die(f"receipt {task}: refused: {problem}", 1)
    say(lib("receipt").canonical(body).decode("ascii"))
    return 0


def do_receipt(rest: list[str]) -> int:
    return cmd_receipt(config(), rest)


def task_arg(task: str) -> None:
    """A task id, never a run id (T-1.r1)."""
    problem = task_problem(task)
    if problem or "." in task:
        die(f"task {task!r}: {problem or 'a task id, not a run id'}")


COMMANDS = {"receipt": do_receipt}
