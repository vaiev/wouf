"""team flow runs independent steps at once (T-0019-21): the pre-flight (scout and research) and the two reviews of a
security task, each an ordinary run on its own bot; no review before a green gate."""

from __future__ import annotations

import configparser
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path

from test_flow_parts import HEAD, team_module
from test_team_cli import KEY, Project
sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import isolate_wouf_env  # noqa: E402

FUTURE = "2999-01-01T00:00:00+00:00"


def setUpModule() -> None:
    isolate_wouf_env()


class RunFlowListTest(unittest.TestCase):
    """run_flow with stand-in steps: what runs at once, what waits, what is kept."""

    def setUp(self) -> None:
        self.team = module = team_module()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        self.calls: list = []
        self.lock = threading.Lock()
        self.active = {"review2"}
        module.flow_dir = lambda task: self.folder
        module.head_of = lambda flow: HEAD
        module.record = module.lead_log = module.say = lambda *a, **k: None
        module.active_member = lambda ini, key: key if key in self.active else None
        module.flow_step = self.step
        self.ini = configparser.ConfigParser(interpolation=None)
        self.gate = "green"
        self.reviews: dict = {}
        self.barrier = threading.Barrier(2, timeout=5)

    def start(self, klass: str = "security", **state) -> int:
        (self.folder / "state.json").write_text(json.dumps(
            {"task": "T-1", "class": klass, "branch": "b", "scout": False, "steps": [], **state}))
        return self.team.run_flow(self.ini, "T-1")

    def state(self) -> dict:
        return json.loads((self.folder / "state.json").read_text())

    def step(self, ini, flow, act, folder, opts) -> dict:
        kind = act["do"]
        member = None if kind == "gate" else self.team.act_member(ini, act, opts)
        with self.lock:
            self.calls.append((kind, member))
        keep = {key: act[key] for key in ("index", "parallel") if act.get(key)}
        if kind == "gate":
            return {"kind": "gate", "outcome": self.gate, "head": HEAD}
        if kind in ("worker", "bugfixer"):
            return {"kind": kind, "bot": member, "outcome": "done", "status": "ready", "head": HEAD}
        if kind == "review":
            if act.get("parallel"):
                self.barrier.wait()  # breaks (and the step fails) unless both reviews run at once
            outcome, verdict, pause = self.reviews.get(act.get("index"), ("done", "approve", 0))
            time.sleep(pause)
            step = {"kind": kind, "bot": member, "outcome": outcome, "head": HEAD, **keep}
            if outcome == "done":
                step["verdict"] = verdict
            else:
                step["until"] = FUTURE
            return step
        return {"kind": kind, "bot": member, "outcome": "done"}

    def test_two_reviews_run_at_once_and_both_approve(self) -> None:
        self.assertEqual(self.start(), 0)
        steps = self.state()["steps"]
        self.assertEqual([(st["kind"], st.get("bot")) for st in steps],
                         [("worker", "worker"), ("gate", None), ("review", "review"), ("review", "review2")])
        self.assertEqual([st.get("verdict") for st in steps[2:]], ["approve", "approve"])
        self.assertEqual({st["group"] for st in steps[2:]}, {2})
        log = (self.folder / "flow.log").read_text()
        self.assertEqual(log.count("+++ "), 4, "each step's return is in flow.log")

    def test_a_red_gate_starts_no_reviewer(self) -> None:
        self.gate = "red"
        self.assertEqual(self.start(), 1)
        self.assertEqual([kind for kind, _ in self.calls], ["worker", "gate", "worker", "gate"])
        self.assertNotIn("review", [st["kind"] for st in self.state()["steps"]])

    def test_without_an_active_review2_the_reviews_run_one_after_the_other(self) -> None:
        self.active = set()
        self.assertEqual(self.start(), 0)
        self.assertEqual(self.calls[2:], [("review", "review"), ("review", "review")])
        self.assertNotIn("group", self.state()["steps"][2])

    def test_a_list_that_names_one_bot_twice_is_refused(self) -> None:
        acts = [{"do": "review", "index": 1}, {"do": "review", "index": 2}]
        self.team.flow_next = lambda flow, opts, fallbacks, at: acts
        self.assertEqual(self.start(), 1)
        self.assertEqual(self.calls, [])
        self.assertIn("one bot: review", self.state()["why"])
        self.ini.read_string("[flow]\nreview2 = review\n")
        self.active = {"review"}
        self.assertIsNone(self.team.parallel_reviewer(self.ini), "review2 = review never runs at once")

    def test_a_limited_review_makes_the_flow_limited_after_the_other_returned(self) -> None:
        self.reviews = {1: ("done", "approve", 0.5), 2: ("limited", None, 0)}
        self.assertEqual(self.start(), 4)
        state = self.state()
        self.assertEqual((state["status"], state["until"]), ("limited", FUTURE))
        first, second = state["steps"][2:]
        self.assertEqual((first["verdict"], second["outcome"]), ("approve", "limited"))
        self.assertLessEqual(first["ended"], state["ended"])

    def test_scout_and_research_run_at_once_then_the_worker(self) -> None:
        self.active = {"research"}
        self.team.flow_step = self.preflight
        self.assertEqual(self.start("plan", scout=None, research=True), 0)
        kinds = [(st["kind"], st.get("group")) for st in self.state()["steps"]]
        self.assertEqual(kinds, [("scout", 0), ("research", 0), ("worker", None)])

    def test_research_without_an_active_bot_is_skipped(self) -> None:
        self.active = set()
        self.team.flow_step = self.preflight
        self.assertEqual(self.start("plan", scout=None, research=True), 0)
        self.assertEqual([st["kind"] for st in self.state()["steps"]], ["scout", "worker"])

    def preflight(self, ini, flow, act, folder, opts) -> dict:
        if len(flow["steps"]) == 0 and opts.get("research"):
            self.barrier.wait()
        return {"kind": act["do"], "bot": self.team.act_member(ini, act, opts), "outcome": "done",
                "status": "ready", "plan": ["PLAN.md"]}


SLOW_CLI = r"""#!/bin/sh
sign=$(printf '%s\n' "$*" | sed -n 's/.*starting with the line `\([^`]*\) ·`.*/\1/p' | head -1)
sleep 2
ls in > out/inputs.txt
case "$sign" in
  *Review*) echo "$sign · APPROVE $(cd repo && git rev-parse HEAD)"; echo "Blocking: none" ;;
  *Worker*) (cd repo && echo work >> file.txt && git add file.txt && git commit -q -m work)
            echo "$sign · ready-for-review"; echo "Questions: none" ;;
  *Planner*) echo "# Plan" > out/PLAN.md; mkdir -p out/briefs; printf 'Task: T-41\n' > out/briefs/T-41.md
             echo "$sign · ready-for-review"; echo "Questions: none" ;;
  *) echo "file.txt:1" > out/CONTEXT.md; echo "a source" > out/RESEARCH.md; echo "$sign · ready-for-review" ;;
esac
"""


def seconds(stamp: str) -> float:
    return datetime.fromisoformat(stamp).timestamp()


class ParallelMeasureTest(unittest.TestCase):
    """End to end in host mode with stand-in CLIs that sleep 2 s: the parallel stage takes about its longest step,
    not the sum. FLOW_MEASURE=1 writes the seconds to stderr."""

    def setUp(self) -> None:
        self.p = Project(mode="host", runner="fake")
        team = self.p.root / ".agents" / "team"
        for member in ("worker", "scout", "research", "planner", "review", "review2"):
            (team / "env" / member).mkdir(parents=True)
            (team / "env" / member / "fake-cli.sh").write_text(SLOW_CLI)
        for member in ("research", "review2"):
            path = team / "bots" / f"{member}.md"
            path.write_text(path.read_text().replace("status: proposed", "status: active")
                            .replace("connectors: context7", "connectors: none"))
        self.p.config.write_text(self.p.config.read_text() + "\n[flow]\ngate = grep -q work file.txt\n")
        self.p.store_secret("ALPHA_KEY", KEY)

    def tearDown(self) -> None:
        self.p.close()

    def flow(self, task: str, brief: str) -> dict:
        self.p.brief.write_text(brief)
        result = self.p.team("flow", task, "--brief", str(self.p.brief), "--repo", str(self.p.root))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads((self.p.root / ".agents" / "team" / "flows" / task / "state.json").read_text())

    def stage(self, task: str, kinds: tuple) -> tuple:
        """The stage's steps from flow.log: (wall seconds from the first start to the last return, sum of steps)."""
        starts, ends = {}, {}
        for line in (self.p.root / ".agents" / "team" / "flows" / task / "flow.log").read_text().splitlines():
            parts = line.split(" ")
            if line.startswith("--- ") and parts[2] in kinds:
                act = json.loads(line.split(" ", 3)[3])
                starts[(parts[2], act.get("index"))] = seconds(parts[1])
            elif line.startswith("+++ ") and parts[2] in kinds:
                ends[(parts[2], len([k for k in ends if k[0] == parts[2]]) + 1)] = seconds(parts[1])
        spans = sorted(starts.values()), sorted(ends.values())
        self.assertEqual(len(spans[0]), len(kinds))
        self.assertEqual(len(spans[1]), len(kinds))
        return spans[1][-1] - spans[0][0], sum(e - s for s, e in zip(*spans))

    def report(self, name: str, wall: float, serial: float) -> None:
        if os.environ.get("FLOW_MEASURE"):
            sys.stderr.write(f"\n{name}: parallel {wall:.0f} s, serial (sum of steps) {serial:.0f} s\n")

    def test_plan_with_scout_and_research(self) -> None:
        state = self.flow("T-50", "Class: plan\nResearch: yes\nObjective: cut the goal into packages\n")
        self.assertEqual([(st["kind"], st.get("group")) for st in state["steps"]],
                         [("scout", 0), ("research", 0), ("worker", None)])
        inputs = (self.p.root / ".agents" / "team" / "env" / "planner" / "tasks" / "T-50" / "out" / "inputs.txt")
        self.assertIn("RESEARCH.md", inputs.read_text())
        self.assertIn("CONTEXT.md", inputs.read_text())
        wall, serial = self.stage("T-50", ("scout", "research"))
        self.report("plan pre-flight (scout + research)", wall, serial)
        self.assertGreaterEqual(serial, 4)
        self.assertLess(wall, serial - 1, "the pre-flight takes about its longest step, not the sum")

    def test_security_with_two_reviews_and_the_graph_instead_of_a_scout(self) -> None:
        state = self.flow("T-51", "Class: security\nPaths: file.txt\nObjective: change file.txt\n")
        self.assertEqual([(st["kind"], st.get("bot")) for st in state["steps"]],
                         [("worker", "worker"), ("gate", None), ("review", "review"), ("review", "review2")])
        self.assertEqual({st["head"] for st in state["steps"][2:]}, {state["head"]})
        inputs = self.p.root / ".agents" / "team" / "env" / "worker" / "tasks" / "T-51" / "out" / "inputs.txt"
        self.assertIn("CONTEXT_GRAPH.md", inputs.read_text())
        wall, serial = self.stage("T-51", ("review", "review"))
        self.report("security reviews (review + review2)", wall, serial)
        self.assertGreaterEqual(serial, 4)
        self.assertLess(wall, serial - 1, "the reviews take about the longest review, not the sum")
        # The same task with review2 proposed: the reviews run one after the other.
        path = self.p.root / ".agents" / "team" / "bots" / "review2.md"
        path.write_text(path.read_text().replace("status: active", "status: proposed"))
        state = self.flow("T-52", "Class: security\nPaths: file.txt\nObjective: change file.txt\n")
        self.assertEqual([(st["kind"], st.get("bot")) for st in state["steps"]][2:],
                         [("review", "review"), ("review", "review")])
        wall, serial = self.stage("T-52", ("review", "review"))
        self.report("security reviews one after the other", wall, serial)
        self.assertGreaterEqual(wall, serial - 1)


if __name__ == "__main__":
    unittest.main()
