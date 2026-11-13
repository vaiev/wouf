"""team batch (T-0019-23): the packages of one plan wave run as flows at the same time, each on its own worker and
scout, when their Paths are pairwise disjoint; a repository lock keeps two flows from updating refs at once."""

from __future__ import annotations

import configparser
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
import types
import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

from test_flow_parts import HEAD, SOURCE, team_module
from test_team_cli import KEY, Project
sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import isolate_wouf_env  # noqa: E402


def batch_module():
    sys.path.insert(0, str(SOURCE / "bin"))
    loader = SourceFileLoader("wouf_team_batch_test", str(SOURCE / "bin" / "teamlib" / "batch.py"))
    module = module_from_spec(spec_from_loader("wouf_team_batch_test", loader))
    loader.exec_module(module)
    return module


PLAN = """# PLAN

## 6. Packages, DAG and waves

| ID | Title | Class | Runner | Wave | Size | Depends on |
|---|---|---|---|---|---|---|
| 21 | Parallel flow stages | complex | w | 11 | L | 11 |
| {a} | First | complex | w | 12 | L | 21 |
| {b} | Second | security | w2 | 12 | M | 04 |
| 25 | Goal | complex | w | 13 | L | {a} |
"""


def brief(task: str, paths: str, extra: str = "") -> str:
    return (f"# {task}\n\nTask: {task}\nClass: complex\nPaths: {paths}\nObjective: change {paths}\n"
            "Acceptance criteria: the gate passes\nScope: the listed paths\nOut of scope: other files\n"
            "Inputs: repository\nConstraints: standard library\nExpected output: commits and return block\n" + extra)


def setUpModule() -> None:
    isolate_wouf_env()


class PlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.b = batch_module()

    def test_the_wave_table_gives_ids_and_runners(self) -> None:
        rows = self.b.plan_wave(PLAN.format(a="23", b="24"), 12)
        self.assertEqual([(r["id"], r["runner"]) for r in rows], [("23", "w"), ("24", "w2")])
        self.assertEqual(self.b.plan_wave(PLAN.format(a="23", b="24"), 99), [])

    def test_bots_from_the_runner(self) -> None:
        self.assertEqual(self.b.bots_of("w"), ("worker", "scout"))
        self.assertEqual(self.b.bots_of("w2"), ("worker2", "scout2"))
        self.assertEqual(self.b.bots_of("worker2"), ("worker2", "scout2"))
        self.assertEqual(self.b.runner_of("- Starts after T-1 is merged. Runner: worker2. Wave 12.\n"), "worker2")
        self.assertIsNone(self.b.runner_of("Objective: none\n"))


class OverlapTest(unittest.TestCase):
    FILES = ["a/one.py", "a/two.py", "b/three.py", "tests/test_a.py"]

    def setUp(self) -> None:
        self.b = batch_module()

    def overlap(self, one: str, two: str):
        return self.b.overlap(one.split(), two.split(), self.FILES)

    def test_disjoint(self) -> None:
        self.assertIsNone(self.overlap("a/one.py tests/test_a.py", "a/two.py b/**"))

    def test_a_file_both_globs_match(self) -> None:
        self.assertEqual(self.overlap("a/*.py", "a/two.py"), "a/two.py")
        self.assertEqual(self.overlap("b/", "**/three.py"), "b/three.py")

    def test_a_new_literal_path_the_other_glob_matches(self) -> None:
        self.assertEqual(self.overlap("a/new.py", "a/*.py"), "a/new.py")
        self.assertEqual(self.overlap("a/*.py", "a/new.py"), "a/new.py")

    def test_a_prefix_the_other_glob_matches(self) -> None:
        self.assertEqual(self.overlap("c/**", "c/x/new.py"), "c/x/new.py")
        self.assertEqual(self.overlap("c/x/**", "c/**"), "c/x/")
        self.assertEqual(self.overlap("d/*.md", "d/*.md"), "d/*.md")

    def test_no_paths_overlaps_everything(self) -> None:
        self.assertEqual(self.b.overlap([], ["a/one.py"], self.FILES), "(no Paths)")

    def test_future_glob_intersections_are_refused(self) -> None:
        self.assertIsNotNone(self.b.overlap(["src/a*.py"], ["src/*b.py"], []))
        self.assertIsNotNone(self.b.overlap(["src/"], ["src/new/**"], []))
        self.assertIsNotNone(self.b.overlap(["**/a.py"], ["src/**"], []))

    def test_provably_disjoint_future_scopes(self) -> None:
        for one, two in (("src/a*", "src/b*"), ("src/a.py", "src/*b.py"), ("src/", "tests/**")):
            self.assertIsNone(self.b.overlap([one], [two], []), (one, two))


class LockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.b = batch_module()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.b.env_dir = lambda: Path(tmp.name)
        self.path = Path(tmp.name) / "repo.lock"

    def held(self) -> bool:
        """Whether another holder has the lock: a second open file asks for it without waiting."""
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(fd)
        return False

    def test_update_ref_and_fetch_run_under_the_lock(self) -> None:
        seen = []
        run = types.SimpleNamespace(LAUNCH="x", git=lambda *a, **k: seen.append((a[0], self.held())),
                                    bundle_commit=None)
        run.bundle_commit = lambda *a: (run.git("update-ref", "r", "c"), run.git("bundle", "create"),
                                        seen.append(("bundle_commit", self.held())))
        self.b.lock_refs(run)
        run.bundle_commit("repo", "path", "c", "n")
        run.git("fetch", "-q", "bundle")
        run.git("rev-parse", "HEAD")
        self.assertEqual(seen, [("update-ref", True), ("bundle", True), ("bundle_commit", True), ("fetch", True),
                                ("rev-parse", False)])
        self.assertFalse(self.held(), "the lock is released after each call")
        self.assertIn("'_locked'", run.LAUNCH, "the supervisor of the run is locked too")

    def test_a_second_process_waits_for_the_lock(self) -> None:
        program = ("import fcntl, os, sys, time\nfd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR)\n"
                   "t = time.monotonic(); print(flush=True); fcntl.flock(fd, fcntl.LOCK_EX)\n"
                   "sys.stdout.write(str(time.monotonic() - t))\n")
        with self.b.repo_lock():
            other = subprocess.Popen([sys.executable, "-c", program, str(self.path)], stdout=subprocess.PIPE,
                                     text=True)
            other.stdout.readline()  # the lock is held for 1 s from when the other process is about to take it
            time.sleep(1)
        waited = float(other.communicate(timeout=10)[0])
        self.assertGreater(waited, 0.5)

    def test_only_run_its_supervisor_and_the_gate_go_through_the_lock(self) -> None:
        self.assertEqual(self.b.LOCKED_COMMANDS, ("run", "_supervise", "gate"))
        with self.assertRaises(SystemExit):
            self.b.do_locked(["batch"])

    def test_keep_others_fetch_runs_under_the_lock(self) -> None:
        """collect's second fetch (engine.keep_others, engine's own git) is locked too."""
        seen = []
        engine = types.SimpleNamespace(git=lambda *a, **k: seen.append((a[0], self.held())))
        run = types.SimpleNamespace(LAUNCH="x", git=lambda *a, **k: None, bundle_commit=lambda *a: None)
        self.b.lock_refs(run, engine)
        engine.git("fetch", "-q", "other.bundle")
        engine.git("bundle", "list-heads")
        self.assertEqual(seen, [("fetch", True), ("bundle", False)])

    def test_the_gate_bundles_under_the_lock(self) -> None:
        """team _locked gate: the gate's own bundle_commit (imported from run) runs under the lock."""
        seen = []
        gate = types.SimpleNamespace(bundle_commit=lambda *a: seen.append(self.held()) or "bundled",
                                     COMMANDS={"gate": lambda rest: seen.append(gate.bundle_commit("r", "p", "c", "n"))
                                               or 0})
        self.b.gate_module = lambda: gate
        self.assertEqual(self.b.do_locked(["gate", "T-1"]), 0)
        self.assertEqual(seen, [True, "bundled"])
        self.assertFalse(self.held())

    def test_a_batch_flow_starts_its_gate_locked(self) -> None:
        """flow_step's gate in a batch flow goes through team _locked gate; outside a batch it does not."""
        from unittest import mock
        flow = team_module()
        flow.env_dir = lambda: Path(self.path.parent)
        flow.record = lambda *a, **k: None
        state = {"task": "T-1", "repo": "/r", "branch": "b", "base": "main", "steps": []}
        for batch, persisted, expect in (("1", False, True), (None, False, False), (None, True, True)):
            env = {k: v for k, v in os.environ.items() if k != "WOUF_BATCH"}
            if batch:
                env["WOUF_BATCH"] = batch
            state["batch"] = persisted
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(flow.subprocess, "run") as started:
                flow.flow_step(None, state, {"do": "gate"}, self.path.parent, {})
            argv = started.call_args[0][0]
            self.assertEqual(argv[2:4] == ["_locked", "gate"], expect, argv)


class BotOverrideTest(unittest.TestCase):
    """run_flow with stand-in steps: the flow's --bot and --scout replace the class's worker and the scout."""

    def setUp(self) -> None:
        self.team = module = team_module()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        self.calls: list = []
        module.flow_dir = lambda task: self.folder
        module.head_of = lambda flow: HEAD
        module.record = module.lead_log = module.say = lambda *a, **k: None
        module.active_member = lambda ini, key: None
        self.active = {"worker2", "scout2"}
        module.active_id = self.active_id
        module.flow_step = self.step
        self.ini = configparser.ConfigParser(interpolation=None)

    def active_id(self, member: str) -> str:
        if member not in self.active:
            raise SystemExit(2)
        return member

    def step(self, ini, flow, act, folder, opts) -> dict:
        member = None if act["do"] == "gate" else self.team.act_member(ini, act, opts)
        self.calls.append((act["do"], member))
        if act["do"] == "gate":
            return {"kind": "gate", "outcome": "green", "head": HEAD}
        if act["do"] == "review":
            return {"kind": "review", "bot": member, "outcome": "done", "verdict": "approve", "head": HEAD}
        return {"kind": act["do"], "bot": member, "outcome": "done", "status": "ready", "head": HEAD}

    def start(self, **state) -> int:
        (self.folder / "state.json").write_text(json.dumps(
            {"task": "T-1", "class": "complex", "branch": "b", "scout": True, "steps": [], **state}))
        return self.team.run_flow(self.ini, "T-1")

    def test_worker2_and_scout2_run_the_flow(self) -> None:
        self.assertEqual(self.start(bot="worker2", scout_bot="scout2"), 0)
        self.assertEqual(self.calls, [("scout", "scout2"), ("worker", "worker2"), ("gate", None),
                                      ("review", "review")])

    def test_without_an_override_the_class_bots_run(self) -> None:
        self.assertEqual(self.start(), 0)
        self.assertEqual(self.calls[:2], [("scout", "scout"), ("worker", "worker")])

    def test_an_inactive_override_runs_nothing(self) -> None:
        self.active = {"scout2"}
        with self.assertRaises(SystemExit):
            self.start(bot="worker2", scout_bot="scout2")
        self.assertEqual(self.calls, [])


# A stand-in CLI that sleeps 2 s; a worker commits to the first path of its brief's Paths (so the gate is green).
SLOW_CLI = r"""#!/bin/sh
sign=$(printf '%s\n' "$*" | sed -n 's/.*starting with the line `\([^`]*\) ·`.*/\1/p' | head -1)
path=$(sed -n 's/^Paths: *\([^ ]*\).*/\1/p' in/BRIEF.md | head -1)
sleep 2
case "$sign" in
  *Review*) echo "$sign · APPROVE $(cd repo && git rev-parse HEAD)"; echo "Blocking: none" ;;
  *Worker*) (cd repo && echo work >> "$path" && git add "$path" && git commit -q -m work)
            echo "$sign · ready-for-review"; echo "Questions: none" ;;
  *) echo "$sign · ready-for-review" ;;
esac
"""


class BatchCliTest(unittest.TestCase):
    """team batch end to end in host mode with stand-in CLIs."""

    def setUp(self) -> None:
        self.p = Project(mode="host", runner="fake")
        self.team_dir = self.p.root / ".agents" / "team"
        bots = self.team_dir / "bots"
        for member, source in (("worker2", "worker"), ("scout2", "scout")):
            text = (bots / f"{source}.md").read_text().replace(f"id: {source}\n", f"id: {member}\n")
            (bots / f"{member}.md").write_text(text)
        for member in ("worker", "worker2", "scout", "scout2", "review"):
            (self.team_dir / "env" / member).mkdir(parents=True, exist_ok=True)
            (self.team_dir / "env" / member / "fake-cli.sh").write_text(SLOW_CLI)
        for name in ("a.txt", "b.txt"):
            (self.p.root / name).write_text("start\n")
        self.p.git("add", "a.txt", "b.txt")
        self.p.git("commit", "-q", "-m", "two files")
        self.p.config.write_text(self.p.config.read_text() + "\n[flow]\ngate = true\n")
        self.p.store_secret("ALPHA_KEY", KEY)
        self.plan = self.p.root / "plan"
        (self.plan / "briefs").mkdir(parents=True)

    def tearDown(self) -> None:
        self.p.close()

    def wave(self, a: str, b: str, paths_a: str = "a.txt", paths_b: str = "b.txt", extra_b: str = "") -> None:
        (self.plan / "PLAN.md").write_text("| ID | Runner | Wave | Depends |\n|---|---|---|---|\n"
                                           f"| {a} | w | 12 | none |\n| {b} | w2 | 12 | none |\n")
        (self.plan / "briefs" / f"{a}.md").write_text(brief(f"T-{a}", paths_a))
        (self.plan / "briefs" / f"{b}.md").write_text(brief(f"T-{b}", paths_b, extra_b))

    def batch(self, *args: str) -> subprocess.CompletedProcess:
        return self.p.team("batch", str(self.plan), "--wave", "12", *args)

    def flows(self) -> list:
        folder = self.team_dir / "flows"
        return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []

    def test_dry_run_prints_packages_bots_and_the_verdict(self) -> None:
        self.wave("30", "31")
        result = self.batch("--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("30 T-30 worker scout", result.stdout)
        self.assertIn("31 T-31 worker2 scout2", result.stdout)
        self.assertIn("disjoint", result.stdout)
        self.assertEqual(self.flows(), [], "a dry run starts nothing")

    def test_an_overlap_refuses_the_batch_and_names_both_packages(self) -> None:
        self.wave("30", "31", paths_a="a.txt tests/**", paths_b="*.txt")
        result = self.batch()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("30 and 31", result.stderr)
        self.assertIn("a.txt", result.stderr)
        self.assertEqual(self.flows(), [])
        dry = self.batch("--dry-run")
        self.assertNotEqual(dry.returncode, 0)
        self.assertIn("overlap: 30 and 31 share a.txt", dry.stdout)

    def test_an_inactive_bot_refuses_the_batch_before_any_start(self) -> None:
        path = self.team_dir / "bots" / "worker2.md"
        path.write_text(path.read_text().replace("status: active", "status: proposed"))
        self.wave("30", "31")
        result = self.batch()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("worker2", result.stderr)
        self.assertEqual(self.flows(), [])

    def test_the_brief_runner_line_wins_and_one_bot_twice_is_refused(self) -> None:
        self.wave("30", "31", extra_b="- Runner: worker. Wave 12.\n")
        result = self.batch("--dry-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("31 T-31 worker scout", result.stdout)
        self.assertIn("worker", result.stderr)

    def test_a_repeat_resumes_the_same_wave_without_duplicate_work(self) -> None:
        self.wave("44", "45")
        first = self.batch()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        states = {t: (self.team_dir / "flows" / t / "state.json").read_text() for t in ("T-44", "T-45")}
        repeat = self.batch()
        self.assertEqual(repeat.returncode, 0, repeat.stdout + repeat.stderr)
        self.assertEqual(states, {t: (self.team_dir / "flows" / t / "state.json").read_text() for t in states})

    def test_future_overlap_starts_nothing(self) -> None:
        self.wave("46", "47", "src/a*.py", "src/*b.py")
        result = self.batch("--dry-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("overlap", result.stdout + result.stderr)
        self.assertEqual(self.flows(), [])

    def test_serial_against_batch(self) -> None:
        self.p.brief.write_text(brief("T-40", "a.txt"))
        started = time.monotonic()
        for task, paths in (("T-40", "a.txt"), ("T-41", "b.txt")):
            self.p.brief.write_text(brief(task, paths))
            result = self.p.team("flow", task, "--brief", str(self.p.brief), "--repo", str(self.p.root))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        serial = time.monotonic() - started
        lock = self.team_dir / "env" / "repo.lock"
        self.assertFalse(lock.exists(), "a flow outside a batch takes no repository lock")
        self.wave("42", "43")
        started = time.monotonic()
        result = self.batch()
        parallel = time.monotonic() - started
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(lock.is_file(), "the batch's runs bundled and fetched under the repository lock")
        for task, member in (("T-42", "worker"), ("T-43", "worker2")):
            state = json.loads((self.team_dir / "flows" / task / "state.json").read_text())
            self.assertEqual(state["status"], "ready")
            self.assertEqual([st.get("bot") for st in state["steps"]], [member, None, "review"])
            self.assertEqual(self.p.git("rev-parse", f"wouf/{task}").strip(), state["head"])
        status = self.p.team("batch", "--status", str(self.plan), "--wave", "12")
        self.assertEqual(status.returncode, 0, status.stderr)
        lines = status.stdout.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("42 T-42 ready wouf/T-42 "), lines[0])
        if os.environ.get("BATCH_MEASURE"):
            sys.stderr.write(f"\nserial {serial:.1f} s, batch {parallel:.1f} s ({parallel / serial:.0%})\n")
        self.assertLess(parallel, serial * 0.8, f"serial {serial:.1f} s, batch {parallel:.1f} s")


if __name__ == "__main__":
    unittest.main()
