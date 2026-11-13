"""Tests for .agents/bin/teamlib/attest.py (receipts R3a): `team receipt make ... --dry-run` builds the unsigned
change receipt of one landed commit from the team's own records, and `team receipt need`. A real git repository
(squash, fast-forward, a two-parent merge, a main that moved, a submodule bump, an empty change); flow steps whose
evidence comes from teamlib.evidence's real functions; run.finished events written through bin/log. Every body built
here passes teamlib.receipt.check_receipt (the R1 contract)."""

from __future__ import annotations

import configparser
import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_flow_evidence import CONTAINER, evidence_module, proxy

ev = evidence_module()
from teamlib import attest, core, flow, receipt, state  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import isolate_wouf_env  # noqa: E402

TEAM = Path(__file__).resolve().parents[1] / ".agents" / "bin" / "team"
TASK = "T-9"
NAME = "Werkstatt Review"
CONFIG = ("[team]\nname = Werkstatt\n[federation]\nname = werkstatt\n[environment]\nmode = container\n"
          "network = egress\n[runner.alpha]\ncommand = env A=1 /opt/tools/claude -p\n"
          "[runner.beta]\ncommand = codex exec\n[flow]\ngate = scripts/pre-gate.sh\n")
PRIVATE = [proxy("127.0.0.1", "11434", "allowed", "private"), "egress-proxy: listening on port 3128"]
PUBLIC = [proxy("api.example.com", "443", "allowed", "ok"), *PRIVATE]
BRIDGE = {"mode": "container", "network": "bridge"}
# Built from parts so this file holds no contiguous key.
AWS = "AKIA" + "IOSFODNN7EXAMPL0"
GIT_ENV = {**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")}, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_AUTHOR_NAME": "Test Human", "GIT_AUTHOR_EMAIL": "h@example.invalid",
           "GIT_COMMITTER_NAME": "Test Human", "GIT_COMMITTER_EMAIL": "h@example.invalid"}


def ini_of(text: str = CONFIG) -> configparser.ConfigParser:
    ini = configparser.ConfigParser(interpolation=None)
    ini.read_string(text)
    return ini


def setUpModule() -> None:
    isolate_wouf_env()


class Records(unittest.TestCase):
    """A team folder (config, flow, stages, events) and a git repository with base and reviewed commits."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.team = self.root / "team"
        self.team.mkdir()
        (self.team / "config.ini").write_text(CONFIG)
        self.ini = ini_of()
        env = {"WOUF_TEAM_DIR": str(self.team), "WOUF_EVENTS_DIR": str(self.team / "events"),
               "WOUF_LOG_DIR": str(self.team / "logs"), "WOUF_STATE_DB": str(self.team / "env" / "state.sqlite")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.base = self.commit({"a.txt": "one\n"}, "base")
        self.git("checkout", "-q", "-b", "work")
        self.reviewed = self.commit({"a.txt": "two\n"}, "the change")
        self.git("checkout", "-q", "main")
        self.steps: list = []
        self.folder = self.team / "flows" / TASK
        self.folder.mkdir(parents=True)
        (self.folder / "brief.md").write_text("Task: T-9\nClass: security\nObjective: x\n")

    # ------------------------------------------------------------ git

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True, text=True,
                              env=GIT_ENV).stdout.strip()

    def commit(self, files: dict, message: str) -> str:
        for name, text in files.items():
            (self.repo / name).write_text(text)
            self.git("add", name)
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def squash(self, parent: str | None = None, of: str | None = None) -> str:
        """A squash merge onto main: the reviewed tree on parent (default base); main moves to it."""
        landed = self.git("commit-tree", f"{of or self.reviewed}^{{tree}}", "-p", parent or self.base, "-m", "squash")
        self.git("update-ref", "refs/heads/main", landed)
        return landed

    # ------------------------------------------------------------ records

    def stage(self, bot: str, run_id: str, commit: str, lines: list | None = PRIVATE, spec: dict = CONTAINER,
              block: str | None = None, runner: str = "alpha") -> dict:
        """A run folder as the lead collects it; returns run_record of it, as flow_step keeps it."""
        stage = self.team / "env" / bot / "tasks" / run_id
        stage.mkdir(parents=True, exist_ok=True)
        run = {**spec, "task": run_id, "member": bot, "name": f"Werkstatt {bot.title()}", "runner": runner,
               "model": "default", "tier": "strong", "commit": commit,
               "branch": None if run_id != TASK else f"werkstatt/{TASK}"}
        (stage / "run.json").write_text(json.dumps(run))
        result = {"state": "done", "tokens": "1/2/3/4"}
        (stage / "result.json").write_text(json.dumps(result))
        (stage / "output.log").write_text(f"work\n{block}\nEvidence: x\n" if block else "work\n")
        if lines is not None:
            (stage / "egress.log").write_text("".join(f"{line}\n" for line in lines))
        return ev.run_record(self.ini, stage, run, result, runner)

    def event(self, run_id: str, bot: str, ref: str | None, outcome: str = "done") -> None:
        data = {"bot": bot, "runner": "alpha", "tier": "strong", "model": "default", "effort": "high",
                "outcome": outcome, **({"ref": ref} if ref else {})}
        state.store().event_append("run.finished", "lead", run_id, data)

    def worker(self, lines: list | None = PRIVATE, spec: dict = CONTAINER) -> None:
        kept = self.stage("worker", TASK, self.base, lines, spec)
        self.event(TASK, "worker", f"werkstatt/{TASK}")
        self.steps.append({"kind": "worker", "bot": "worker", "task": TASK, "outcome": "done", "runner": "alpha",
                           "tier": "strong", **kept, "status": "ready", "head": self.reviewed})

    def gate(self, head: str | None = None, base: str | None = None, network: str = "none",
             event: bool = True, bound: bool = True) -> dict:
        """A gate step as flow_step keeps it: gate.json as team gate writes it, read back by gate_step, and its
        flow.step event (step_event; not bound: a gate event of before it carried head and base)."""
        log = self.root / "gate.log"
        log.write_text("2 files, 30 functions, 0 failures\n")
        ini = ini_of(CONFIG + f"gate_network = {network}\n")
        result = {"task": TASK, "head": head or self.reviewed, "base": base or self.base, "outside": [], "code": 0,
                  "state": "green", "why": "gate command passed",
                  **ev.gate_record(ini, "scripts/pre-gate.sh", log, "a.txt\0")}
        step = {"kind": "gate", "outcome": "green", "head": result["head"], "why": result["why"],
                **ev.gate_step(result)}
        self.steps.append(step)
        self.gate_json(result)
        if event:
            kind, actor, task, data = flow.step_event(TASK, step)
            if not bound:
                data = {k: v for k, v in data.items() if k not in ("head", "base")}
            state.store().event_append(kind, actor, task, data)
        return result

    def gate_json(self, result: dict | None) -> None:
        """gate.json as team gate leaves it (None: no gate ran)."""
        folder = self.team / "env" / "gate" / TASK
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "gate.json").unlink(missing_ok=True)
        if result is not None:
            (folder / "gate.json").write_text(json.dumps(result))

    def review(self, number: int, commit: str | None = None, typed: str | None = None, word: str = "APPROVE",
               lines: list | None = PRIVATE, spec: dict = CONTAINER, flow: bool = True, event: bool = True) -> str:
        """A review run T-9.r<n> on commit (default reviewed) whose block says word typed (default the commit)."""
        run_id, commit = f"{TASK}.r{number}", commit or self.reviewed
        block = f"{NAME} · {run_id} · {word} {typed or commit}".rstrip()
        kept = self.stage("review", run_id, commit, lines, spec, block, runner="beta")
        if event:
            self.event(run_id, "review", commit)
        if flow:
            verdict = {"APPROVE": "approve", "CHANGES": "changes"}[word]
            self.steps.append({"kind": "review", "bot": "review", "task": run_id, "outcome": "done", "runner": "beta",
                               "tier": "strong", "index": number, **kept, "verdict": verdict, "bugs": [],
                               "changes": [], "head": commit})
        return run_id

    def flow(self, **extra) -> None:
        state_json = {"task": TASK, "repo": str(self.repo), "base": self.base, "branch": f"werkstatt/{TASK}",
                      "class": "security", "steps": self.steps, "status": "ready", "head": self.reviewed, **extra}
        (self.folder / "state.json").write_text(json.dumps(state_json))

    def happy(self) -> None:
        """worker, green gate, two approving reviews in the flow."""
        self.worker()
        self.gate()
        self.review(1)
        self.review(2)
        self.flow()

    def build(self, landed: str, reviewed: str | None = None, **opts) -> dict:
        o = {"--reviewed": reviewed or self.reviewed, "--landed": landed, "--repo": None, "--repo-name": "wouf",
             "--pr": 7, "--brief": None, **opts}
        body = attest.build(self.ini, TASK, o)
        receipt.check_receipt(body)  # the R1 contract, again
        receipt.check_receipt(json.loads(receipt.canonical(body)))
        return body

    def refused(self, landed: str, text: str, **opts) -> str:
        with self.assertRaises(receipt.Refused) as caught:
            self.build(landed, **opts)
        message = str(caught.exception)
        self.assertIn(text, message)
        self.assertEqual(len(message.splitlines()), 1)
        return message


class ChangeTest(Records):
    def test_a_squash_merge_builds_a_checked_sovereign_receipt(self) -> None:
        self.happy()
        landed = self.squash()
        body = self.build(landed)
        self.assertEqual(body["change"], {"base": self.base, "reviewed": self.reviewed, "landed": landed,
                                          "landed_parent": self.base, "tree": self.git("rev-parse", f"{landed}^{{tree}}"),
                                          "files": 1, "merge": "squash", "pr": 7})
        self.assertEqual((body["team"], body["class"], body["repo"], body["brief"]["source"]),
                         ("werkstatt", "security", "wouf", "flow"))
        self.assertEqual((body["reviews"]["required"], body["reviews"]["approved"]), (2, 2))
        self.assertEqual([r["source"] for r in body["reviews"]["rounds"]], ["flow", "flow"])
        self.assertEqual([r["cli"] for r in body["runs"]], ["claude", "codex", "codex"])
        self.assertEqual([r["vendor"] for r in body["runs"]], ["anthropic", "openai", "openai"])
        self.assertEqual(body["gate"]["network"], "none")
        self.assertEqual(body["flags"], {"sovereign": True})
        self.assertEqual(body["usage"], {"tokens": 30, "known_runs": 3, "unknown_runs": 0, "cost": "unknown"})

    def test_a_fast_forward_lands_the_reviewed_commit_itself(self) -> None:
        self.happy()
        self.git("update-ref", "refs/heads/main", self.reviewed)
        body = self.build(self.reviewed)
        self.assertEqual((body["change"]["merge"], body["change"]["landed"], body["change"]["landed_parent"]),
                         ("fast-forward", self.reviewed, self.base))

    def test_a_fast_forward_past_the_reviewed_commit_is_refused(self) -> None:
        self.happy()
        self.git("checkout", "-q", "work")
        later = self.commit({"b.txt": "more\n"}, "unreviewed")
        self.git("update-ref", "refs/heads/main", later)
        self.refused(later, "fast-forward: the landed commit is not the reviewed commit")

    def test_a_two_parent_merge_is_refused(self) -> None:
        self.happy()
        merged = self.git("commit-tree", f"{self.reviewed}^{{tree}}", "-p", self.base, "-p", self.reviewed,
                          "-m", "merge")
        self.git("update-ref", "refs/heads/main", merged)
        self.refused(merged, "has 2 parents")

    def test_a_main_that_moved_before_the_merge_is_refused(self) -> None:
        self.happy()
        moved = self.commit({"c.txt": "other\n"}, "main moved")
        self.refused(self.squash(parent=moved), "squash: landed^1 is not change.base")

    def test_a_landed_tree_that_is_not_the_reviewed_tree_is_refused(self) -> None:
        self.happy()
        self.git("checkout", "-q", "work")
        other = self.commit({"a.txt": "three\n"}, "other")
        self.git("checkout", "-q", "main")
        self.refused(self.squash(of=other), "squash: the landed tree is not the reviewed tree")

    def test_a_submodule_bump_counts_as_a_file(self) -> None:
        self.git("update-index", "--add", "--cacheinfo", f"160000,{'1' * 40},sub")
        self.base = self.commit({}, "a submodule")
        self.git("checkout", "-q", "-B", "work")
        self.git("update-index", "--cacheinfo", f"160000,{'2' * 40},sub")
        self.reviewed = self.commit({}, "bump")
        self.git("checkout", "-q", "main")
        self.happy()
        self.assertEqual(self.build(self.squash())["change"]["files"], 1)

    def test_an_empty_change_is_refused(self) -> None:
        self.git("checkout", "-q", "-B", "work", self.base)
        self.reviewed = self.commit({}, "nothing")
        self.git("checkout", "-q", "main")
        self.happy()
        self.refused(self.squash(), "files = 0")

    def test_a_landed_commit_outside_main_is_refused(self) -> None:
        self.happy()
        landed = self.git("commit-tree", f"{self.reviewed}^{{tree}}", "-p", self.base, "-m", "squash")
        self.refused(landed, "not reachable from refs/heads/main")

    def test_a_sha256_commit_or_base_is_refused(self) -> None:
        self.happy()
        landed = self.squash()
        self.refused(landed, "v1 is SHA-1 only (SPEC 2.1)", **{"--reviewed": "a" * 64})
        self.flow(base="b" * 64)
        self.refused(landed, "change.base is a SHA-256 commit id: v1 is SHA-1 only (SPEC 2.1)")

    def test_a_secret_shaped_string_is_refused_without_its_value(self) -> None:
        self.happy()
        message = self.refused(self.squash(), "looks like it contains a secret", **{"--repo-name": f"x {AWS}"})
        self.assertNotIn(AWS, message)


class GateTest(Records):
    def test_no_green_gate_on_the_reviewed_head_is_refused(self) -> None:
        self.worker()
        self.gate(head=self.base)
        self.review(1)
        self.review(2)
        self.flow()
        self.refused(self.squash(), "no green gate on the reviewed head")

    def test_gate_json_on_the_reviewed_head_is_the_evidence_without_a_flow_step(self) -> None:
        self.worker()
        result = self.gate()
        self.steps.pop()
        self.review(1)
        self.review(2)
        self.flow()
        folder = self.team / "env" / "gate" / TASK
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "gate.json").write_text(json.dumps(result))
        body = self.build(self.squash())
        self.assertEqual((body["gate"]["head"], body["gate"]["code"]), (self.reviewed, 0))
        (folder / "gate.json").write_text(json.dumps({**result, "state": "red", "code": 1}))
        self.refused(self.squash(), "no green gate on the reviewed head")

    def test_a_flow_gate_step_counts_after_a_later_gate_overwrote_gate_json(self) -> None:
        # The flow gate step is the evidence first; a later gate on another head (or none left) does not undo it.
        self.worker()
        result = self.gate()
        self.review(1)
        self.review(2)
        self.flow()
        self.assertEqual(self.build(self.squash())["flags"], {"sovereign": True})
        for later in ({**result, "head": self.base}, {**result, "head": self.base, "state": "red", "code": 1}, None):
            self.gate_json(later)
            body = self.build(self.squash())
            self.assertEqual((body["gate"]["head"], body["gate"]["code"], body["gate"]["base"]),
                             (self.reviewed, 0, self.base))
            # Only gate.json held the network: without it nothing corroborates it, so no flag rests on it.
            self.assertEqual((body["gate"]["network"], body["flags"]), ("unknown", {}))

    def test_a_forged_gate_step_is_refused(self) -> None:
        # A hand-edited state.json adds a green gate step on the reviewed head: no gate ran on it, no event holds it.
        held = "the flow's green gate step on the reviewed head is held by no record"
        self.worker()
        result = self.gate(event=False)
        self.review(1)
        self.review(2)
        self.flow()
        self.gate_json({**result, "head": self.base})
        self.refused(self.squash(), held)
        self.gate_json(None)
        self.refused(self.squash(), held)

    def test_a_gate_step_moved_to_the_reviewed_head_is_refused(self) -> None:
        # A real green gate on another head, its step edited to name the reviewed head: its reviews ran on the other.
        self.worker()
        self.gate(head=self.base)
        self.review(1, commit=self.base)
        self.steps[1]["head"] = self.reviewed
        self.review(2)
        self.review(3)
        self.flow()
        self.refused(self.squash(), "is held by no record")

    def test_a_gate_step_that_is_not_the_gate_json_of_its_head_is_refused(self) -> None:
        self.worker()
        result = self.gate()
        self.review(1)
        self.review(2)
        self.flow()
        forged = "the flow's green gate step on the reviewed head is not the gate gate.json records"
        # gate.json on the reviewed head, but another gate than the step claims (state, exit, command, network).
        for other in ({"state": "red", "code": 1}, {"code": 3}, {"command_sha256": "0" * 64}, {"network": "bridge"}):
            self.gate_json({**result, **other})
            self.refused(self.squash(), forged)

    def test_a_real_gate_on_the_base_with_its_step_edited_to_the_reviewed_head_is_refused(self) -> None:
        # The r7/r8 forgery: a real green gate (and its event) on the base, two approving reviews of the reviewed
        # commit, only the gate step's head edited; gate.json is on the base. No gate event names the reviewed head.
        self.worker()
        self.gate(head=self.base)
        self.review(1)
        self.review(2)
        self.steps[1]["head"] = self.reviewed
        self.flow()
        self.refused(self.squash(), "no gate event on the reviewed head")
        # The same with the reviews placed after the gate step out of the flow, as r7 reordered them.
        self.review(3, flow=False)
        self.flow()
        self.refused(self.squash(), "no gate event on the reviewed head")

    def test_a_genuine_gate_on_the_reviewed_head_builds_from_its_bound_event(self) -> None:
        self.worker()
        self.gate()
        self.review(1)
        self.review(2)
        self.flow()
        self.gate_json(None)
        body = self.build(self.squash())
        self.assertEqual((body["gate"]["head"], body["gate"]["base"], body["gate"]["network"]),
                         (self.reviewed, self.base, "unknown"))

    def test_a_gate_event_on_the_reviewed_head_of_another_base_binds_nothing(self) -> None:
        self.worker()
        self.gate(base=self.reviewed)
        self.steps[1]["base"] = self.base
        self.review(1)
        self.review(2)
        self.flow()
        self.gate_json(None)
        self.refused(self.squash(), "no gate event on the reviewed head")

    def test_an_old_gate_event_without_a_head_builds_only_from_gate_json(self) -> None:
        self.worker()
        result = self.gate(bound=False)
        self.review(1)
        self.review(2)
        self.flow()
        body = self.build(self.squash())
        self.assertEqual((body["gate"]["head"], body["gate"]["network"]), (self.reviewed, "none"))
        for later in ({**result, "head": self.base}, None):
            self.gate_json(later)
            self.refused(self.squash(), "no gate event on the reviewed head")

    def test_a_gate_on_another_base_is_refused(self) -> None:
        self.worker()
        self.gate(base=self.reviewed)
        self.review(1)
        self.review(2)
        self.flow()
        self.refused(self.squash(), "the gate's base is not change.base")


class ClosedFlowTest(Records):
    """A closed flow (state.json has `closed`) is history, not evidence: build reads base and gate from gate.json and
    the runs and rounds from the run events, as without a flow (#158: the lead drove the task after its flow closed)."""

    CLOSED = {"at": "2026-11-07T12:46:57+00:00", "reason": "ship and lead steps drive this task; flow bugfixer stopped",
              "from": "blocked"}

    def old_commit(self) -> str:
        """A commit of the repository that is neither landed^1 nor an ancestor of the reviewed commit."""
        return self.git("commit-tree", f"{self.base}^{{tree}}", "-m", "old base")

    def test_a_closed_flow_with_an_old_base_builds_from_gate_json_on_the_reviewed_head(self) -> None:
        old = self.old_commit()
        self.worker()
        self.review(1, commit=self.base)  # the closed flow's review, of its own old head
        self.gate(head=self.base, base=old)  # the closed flow's gate step, on its old head and base
        self.flow(base=old, head=self.base)
        landed = self.squash()
        self.refused(landed, "squash: landed^1 is not change.base")  # the open flow is the evidence, as today
        self.gate(event=False)  # the lead's gate on the reviewed head, base = landed^1: gate.json only
        self.steps.pop()
        self.review(2, flow=False)
        self.review(3, flow=False)
        self.flow(base=old, head=self.base, status="closed", closed=self.CLOSED)
        body = self.build(landed)
        self.assertEqual((body["change"]["base"], body["change"]["landed_parent"]), (self.base, self.base))
        self.assertEqual((body["gate"]["head"], body["gate"]["base"], body["gate"]["network"]),
                         (self.reviewed, body["change"]["base"], "none"))
        self.assertEqual([(r["run"], r["source"], r["counts"]) for r in body["reviews"]["rounds"]],
                         [(f"{TASK}.r1", "run", False), (f"{TASK}.r2", "run", True), (f"{TASK}.r3", "run", True)])
        self.assertEqual(body["reviews"]["approved"], 2)
        self.assertEqual([r["run"] for r in body["runs"]], [TASK, f"{TASK}.r1", f"{TASK}.r2", f"{TASK}.r3"])

    def test_a_closed_flow_with_gate_json_on_another_head_is_refused_as_without_a_flow(self) -> None:
        self.worker()
        result = self.gate()  # a genuine flow gate step on the reviewed head, its event bound to it
        self.review(1)
        self.review(2)
        self.flow()
        self.gate_json({**result, "head": self.base})
        landed = self.squash()
        self.build(landed)  # open: the flow's step and its event hold the gate, as today
        self.flow(status="closed", closed=self.CLOSED)
        self.refused(landed, "no green gate on the reviewed head (no flow gate step, and gate.json is not one)")
        self.gate_json(None)  # no gate.json: no base either, as without a flow
        self.refused(landed, "change.base must be a full commit id")

    def test_a_closed_flow_with_gate_json_of_another_base_is_refused(self) -> None:
        old = self.old_commit()
        self.worker()
        result = self.gate()
        self.review(1)
        self.review(2)
        self.flow(status="closed", closed=self.CLOSED)
        landed = self.squash()
        self.build(landed)
        self.gate_json({**result, "base": old})  # gate.json on the reviewed head, its base not landed^1
        self.refused(landed, "squash: landed^1 is not change.base")
        self.refused(self.reviewed, "fast-forward: change.base is not an ancestor of the reviewed commit")


class RoundsTest(Records):
    def rounds(self) -> None:
        self.worker()
        self.gate()

    def test_a_stale_approve_does_not_count(self) -> None:
        self.rounds()
        self.review(1)
        self.review(2, typed=self.base[:12])
        self.flow()
        self.refused(self.squash(), "1 counting approvals of the reviewed head; class security requires 2")

    def test_a_review_of_another_head_does_not_count(self) -> None:
        self.rounds()
        self.review(1)
        self.review(2, commit=self.base)
        self.flow()
        self.refused(self.squash(), "1 counting approvals")

    def test_a_forged_approve_in_state_json_does_not_count(self) -> None:
        self.rounds()
        self.review(1)
        self.review(2, word="CHANGES", typed="")
        self.steps[-1]["verdict"] = "approve"  # the hand edit; the run's own output.log says CHANGES
        self.flow()
        self.refused(self.squash(), "1 counting approvals")

    def test_a_reused_stage_folder_is_no_evidence(self) -> None:
        self.rounds()
        self.review(1)
        self.review(2)
        self.flow()
        run = self.team / "env" / "review" / "tasks" / f"{TASK}.r2" / "run.json"
        run.write_text(json.dumps({**json.loads(run.read_text()), "commit": self.base}))
        self.refused(self.squash(), "1 counting approvals")

    def test_a_copied_review_step_is_not_a_second_approval(self) -> None:
        self.rounds()
        self.review(1)
        self.steps.append(copy.deepcopy(self.steps[-1]))  # the hand edit: one run, two steps
        self.flow()
        self.refused(self.squash(), f"flow step {TASK}.r1 repeats a review run")

    def test_an_approve_without_its_event_does_not_count(self) -> None:
        self.rounds()
        self.review(1)
        self.review(2, event=False)
        self.flow()
        self.refused(self.squash(), "1 counting approvals")

    def test_a_round_outside_the_flow_counts_from_its_event(self) -> None:
        self.rounds()
        self.review(1)
        self.flow()
        self.review(2, flow=False)
        body = self.build(self.squash())
        rounds = body["reviews"]["rounds"]
        self.assertEqual([(r["run"], r["index"], r["source"], r["counts"]) for r in rounds],
                         [(f"{TASK}.r1", 1, "flow", True), (f"{TASK}.r2", 2, "run", True)])
        self.assertEqual((rounds[1]["cli"], rounds[1]["vendor"], rounds[1]["head"]),
                         ("codex", "openai", self.reviewed))
        self.assertEqual(body["runs"][-1]["run"], f"{TASK}.r2")

    def test_a_stage_outside_the_flow_with_another_commit_is_refused_as_evidence(self) -> None:
        self.rounds()
        self.review(1)
        self.flow()
        self.review(2, flow=False)
        self.event(f"{TASK}.r2", "review", self.base)  # a later event whose ref is not the stage's commit
        self.refused(self.squash(), "refused as evidence")

    def test_need_prints_the_reviews_of_the_class(self) -> None:
        self.flow()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(attest.cmd_receipt(self.ini, ["need", TASK]), 0)
        self.assertEqual(out.getvalue(), "reviews 2\n")


class SovereignTest(Records):
    def flag(self, worker: dict = None, review: dict = None, network: str = "none") -> dict:
        self.worker(**(worker or {}))
        self.gate(network=network)
        self.review(1, **(review or {}))
        self.review(2)
        self.flow()
        return self.build(self.squash())["flags"]

    def test_only_private_pairs_with_gate_network_none_give_the_flag(self) -> None:
        self.assertEqual(self.flag(), {"sovereign": True})

    def test_a_bridge_run_gives_no_flag(self) -> None:
        self.assertEqual(self.flag(worker={"lines": None, "spec": BRIDGE}), {})

    def test_a_bridge_run_edited_to_claim_a_proxy_log_gives_no_flag(self) -> None:
        # The hand edit: the bridge worker's step claims the egress of a private proxy run (a sha256, no host).
        self.worker(lines=None, spec=BRIDGE)
        self.gate()
        self.review(1)
        self.review(2)
        self.steps[0]["egress"] = copy.deepcopy(self.steps[2]["egress"])
        self.flow()
        body = self.build(self.squash())
        self.assertEqual(body["runs"][0]["egress"], {"mode": "unknown", "log": "unknown", "hosts": [], "private": 0,
                                                     "refused": 0})
        self.assertEqual(body["flags"], {})

    def test_a_proxy_log_claim_without_its_run_gives_no_flag(self) -> None:
        self.worker()
        self.gate()
        self.review(1)
        self.review(2)
        self.flow()
        log = self.team / "env" / "review" / "tasks" / f"{TASK}.r1" / "egress.log"
        log.write_text(log.read_text() + PUBLIC[0] + "\n")  # the stage no longer holds what the step claims
        self.assertEqual(self.build(self.squash())["flags"], {})
        log.unlink()
        self.assertEqual(self.build(self.squash())["flags"], {})

    def test_a_run_without_a_log_gives_no_flag(self) -> None:
        self.assertEqual(self.flag(review={"lines": None}), {})

    def test_an_incomplete_log_gives_no_flag(self) -> None:
        masked = [proxy("***.example.com", "443", "allowed", "ok")]
        self.assertEqual(self.flag(worker={"lines": masked}), {})

    def test_an_allowed_public_host_gives_no_flag(self) -> None:
        self.assertEqual(self.flag(review={"lines": PUBLIC}), {})

    def test_a_gate_on_network_bridge_gives_no_flag(self) -> None:
        self.assertEqual(self.flag(network="bridge"), {})


class OldStateTest(Records):
    def test_a_flow_recorded_before_r21_builds_a_valid_receipt_without_the_flag(self) -> None:
        self.happy()
        old = copy.deepcopy(self.steps)
        old[0]["egress"]["log"] = True
        old[0]["tier"] = "mid→strong"
        old[2]["egress"]["log"] = False
        del old[3]["egress"]["log"]
        for key in ("code", "command_sha256"):
            del old[1][key]
        self.steps[:] = old
        self.flow()
        body = self.build(self.squash())
        self.assertEqual([r["egress"]["log"] for r in body["runs"]], ["unknown"] * 3)
        self.assertEqual(body["runs"][0]["tier"], "mid->strong")
        self.assertEqual((body["gate"]["code"], body["gate"]["command_sha256"]), (0, "unknown"))
        self.assertEqual(body["flags"], {})


class CommandTest(Records):
    def team_cli(self, *args: str) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if not k.startswith("WOUF_")}
        return subprocess.run([sys.executable, str(TEAM), *args], capture_output=True, text=True, timeout=120,
                              env={**env, "WOUF_TEAM_DIR": str(self.team)}, cwd=self.root)

    def test_receipt_is_a_registered_command_and_team_help_lists_it(self) -> None:
        self.assertIs(attest.COMMANDS["receipt"], attest.do_receipt)
        done = self.team_cli("help")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("team receipt make TASK --reviewed SHA --landed SHA", done.stdout)
        self.assertIn("team receipt need TASK", done.stdout)

    def test_make_dry_run_prints_the_canonical_body_and_need_the_reviews(self) -> None:
        self.happy()
        landed = self.squash()
        args = ("receipt", "make", TASK, "--reviewed", self.reviewed, "--landed", landed, "--repo-name", "wouf")
        done = self.team_cli(*args)
        self.assertEqual((done.returncode, done.stdout), (1, ""))  # R3b signs: without a key it stops
        self.assertIn("set [federation] signing_key", done.stderr)
        self.assertFalse((self.team / "receipts" / f"{landed}.json").exists())
        done = self.team_cli(*args, "--dry-run")
        self.assertEqual(done.returncode, 0, done.stderr)
        body = json.loads(done.stdout)
        receipt.check_receipt(body)
        self.assertEqual(done.stdout, receipt.canonical(body).decode() + "\n")
        done = self.team_cli("receipt", "make", TASK, "--reviewed", self.base, "--landed", landed, "--dry-run")
        self.assertEqual((done.returncode, done.stdout), (1, ""))
        self.assertEqual(len(done.stderr.splitlines()), 1)
        self.assertIn(f"receipt {TASK}: refused:", done.stderr)
        done = self.team_cli("receipt", "need", TASK)
        self.assertEqual((done.returncode, done.stdout), (0, "reviews 2\n"))


if __name__ == "__main__":
    unittest.main()
