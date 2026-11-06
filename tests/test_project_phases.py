"""Tests for .agents/bin/project: a brief from a plain goal, the dependency waves of a plan, phase evidence."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / ".agents" / "bin" / "project"
# The sections .agents/skills/brief.md asks of every brief, filled.
SECTIONS = ("Acceptance criteria: the endpoint answers 200\nScope: src/api\nOut of scope: auth\nInputs: the repository\n"
            "Constraints: stdlib only\nExpected output: commits and the return block\n")


class ProjectTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.team = self.tmp / "team"
        shutil.copytree(ROOT / ".agents" / "team", self.team)
        self.repo = self.tmp / "repo"
        for rel in ("src/api/status.py", "src/api/routes.py", "src/auth/login.py", "docs/readme.md",
                    "web/billing/invoice.ts", "node_modules/auth/x.js"):
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text("x\n")
        self.env = {**os.environ, "WOUF_TEAM_DIR": str(self.team)}

    def run_project(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(PROJECT), *args], env=self.env, capture_output=True,
                              text=True, timeout=60)

    def fields(self, text: str) -> dict:
        out = {}
        for line in text.splitlines():
            key, sep, value = line.partition(":")
            if sep and key in ("Task", "Class", "Paths", "Objective", "Threat model", "Depends") and key not in out:
                out[key] = value.strip()
        return out

    # ------------------------------------------------------------ INIT

    def test_brief_from_a_plain_goal(self) -> None:
        done = self.run_project("brief", "Add a status endpoint to the api", "--repo", str(self.repo))
        self.assertEqual(done.returncode, 0, done.stderr)
        f = self.fields(done.stdout)
        self.assertEqual(f["Task"], "T-1")
        self.assertEqual(f["Class"], "standard")
        self.assertEqual(f["Paths"], "src/api/**")
        self.assertEqual(f["Objective"], "Add a status endpoint to the api")
        self.assertNotIn("Threat model", f)

    def test_the_next_free_task_id_comes_from_the_board(self) -> None:
        board = self.team / "board.md"
        board.write_text(board.read_text().replace("| | | | | | | | |", "| T-7 | x | worker | done | | | | |"))
        done = self.run_project("brief", "fix a typo in the docs readme", "--repo", str(self.repo))
        f = self.fields(done.stdout)
        self.assertEqual(f["Task"], "T-8")
        self.assertEqual(f["Class"], "mechanical")
        self.assertEqual(f["Paths"], "docs/**")

    def test_security_class_and_threat_model_when_a_sensitive_path_is_touched(self) -> None:
        for goal in ("change the login flow", "rotate the api token", "Passwort-Regeln verschärfen"):
            with self.subTest(goal=goal):
                done = self.run_project("brief", goal, "--repo", str(self.repo), "--task", "T-3")
                f = self.fields(done.stdout)
                self.assertEqual(f["Class"], "security")
                self.assertIn("TO FILL", f["Threat model"])

    def test_other_classes_from_the_goal(self) -> None:
        for goal, klass in (("decide which billing provider we use", "decision"),
                            ("investigate why invoices are slow", "analysis"),
                            ("migrate the billing module to the new architecture", "complex")):
            with self.subTest(goal=goal):
                self.assertEqual(self.fields(self.run_project("brief", goal, "--repo", str(self.repo)).stdout)
                                 ["Class"], klass)

    def test_no_matching_path_is_said_not_guessed(self) -> None:
        done = self.run_project("brief", "write a poem", "--repo", str(self.repo))
        self.assertEqual(done.returncode, 0)
        self.assertEqual(self.fields(done.stdout)["Paths"], "TO FILL")
        self.assertIn("no path", done.stderr)

    def test_vendored_folders_are_not_paths(self) -> None:
        done = self.run_project("brief", "change the auth login", "--repo", str(self.repo))
        self.assertNotIn("node_modules", done.stdout)

    def test_check_refuses_a_brief_that_is_not_filled(self) -> None:
        brief = self.tmp / "b.md"
        brief.write_text(self.run_project("brief", "change the login flow", "--repo", str(self.repo)).stdout)
        done = self.run_project("check", str(brief))
        self.assertEqual(done.returncode, 1)
        self.assertIn("TO FILL", done.stdout)
        brief.write_text(brief.read_text().replace("TO FILL", "attacker reaches the session store via the form"))
        self.assertEqual(self.run_project("check", str(brief)).returncode, 0)

    # ------------------------------------------------------------ CONCEPT

    def write_plan(self, briefs: dict) -> Path:
        folder = self.tmp / "briefs"
        folder.mkdir(exist_ok=True)
        for task, depends in briefs.items():
            (folder / f"{task}.md").write_text(
                f"Task: {task}\nClass: standard\nPaths: src/**\nObjective: do {task}\nDepends: {depends}\n")
        return folder

    def test_deps_groups_tasks_into_parallel_waves(self) -> None:
        folder = self.write_plan({"T-1": "none", "T-2": "none", "T-3": "T-1, T-2", "T-4": "T-3"})
        done = self.run_project("deps", str(folder), "--json")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["waves"], [["T-1", "T-2"], ["T-3"], ["T-4"]])
        text = self.run_project("deps", str(folder)).stdout
        self.assertIn("wave 1: T-1, T-2", text)
        self.assertIn("T-3 waits for T-1, T-2", text)

    def test_deps_refuses_cycles_and_unknown_tasks(self) -> None:
        cyc = self.run_project("deps", str(self.write_plan({"T-1": "T-2", "T-2": "T-1"})))
        self.assertEqual(cyc.returncode, 1)
        self.assertIn("cycle", cyc.stdout)
        shutil.rmtree(self.tmp / "briefs")
        unknown = self.run_project("deps", str(self.write_plan({"T-1": "T-9"})))
        self.assertEqual(unknown.returncode, 1)
        self.assertIn("T-9", unknown.stdout)

    # ------------------------------------------------------------ REALIZE, VERIFY

    def flow(self, task: str, status: str, steps: list, klass: str = "standard", depends: str = "none") -> None:
        folder = self.team / "flows" / task
        folder.mkdir(parents=True)
        (folder / "brief.md").write_text(
            f"Task: {task}\nClass: {klass}\n" + ("" if klass == "plan" else "Paths: src/**\n")
            + f"Objective: do it\nDepends: {depends}\n" + SECTIONS
            + ("Threat model: the form reaches the session store\n" if klass == "security" else ""))
        (folder / "state.json").write_text(json.dumps({"task": task, "class": klass, "status": status,
                                                       "steps": steps}))

    def phases(self, task: str) -> dict:
        done = self.run_project("phase", task, "--json")
        return {"code": done.returncode, **json.loads(done.stdout)}

    def test_phases_of_a_finished_flow(self) -> None:
        self.flow("T-5", "ready", [{"kind": "worker", "outcome": "done", "head": "a" * 40},
                                   {"kind": "gate", "outcome": "green", "head": "a" * 40},
                                   {"kind": "review", "outcome": "done", "verdict": "approve"}])
        p = self.phases("T-5")
        self.assertEqual(p["code"], 0)
        self.assertEqual([x["phase"] for x in p["phases"]], ["INIT", "CONCEPT", "REALIZE", "VERIFY"])
        self.assertTrue(all(x["state"] == "pass" for x in p["phases"]), p)
        self.assertEqual(p["current"], "done")
        self.assertIn("a" * 12, json.dumps(p["phases"][2]))

    def test_a_red_gate_holds_realize_and_names_why(self) -> None:
        self.flow("T-6", "blocked", [{"kind": "worker", "outcome": "done", "head": "b" * 40},
                                     {"kind": "gate", "outcome": "red", "head": "b" * 40, "why": "gate exit 1"}])
        p = self.phases("T-6")
        self.assertEqual(p["code"], 1)
        self.assertEqual(p["current"], "REALIZE")
        self.assertIn("gate exit 1", p["phases"][2]["evidence"])
        self.assertEqual(p["phases"][3]["state"], "open")

    def test_security_needs_two_approvals(self) -> None:
        steps = [{"kind": "worker", "outcome": "done", "head": "c" * 40},
                 {"kind": "gate", "outcome": "green", "head": "c" * 40},
                 {"kind": "review", "outcome": "done", "verdict": "approve"}]
        self.flow("T-7", "running", steps, klass="security")
        p = self.phases("T-7")
        self.assertEqual(p["current"], "VERIFY")
        self.assertIn("1 of 2", p["phases"][3]["evidence"])

    def test_concept_waits_for_an_unfinished_dependency(self) -> None:
        self.flow("T-1", "running", [])
        self.flow("T-2", "new", [], depends="T-1")
        p = self.phases("T-2")
        self.assertEqual(p["current"], "CONCEPT")
        self.assertIn("T-1", p["phases"][1]["evidence"])

    def test_a_plan_passes_with_its_plan_and_no_review(self) -> None:
        self.flow("T-8", "ready", [{"kind": "worker", "outcome": "done", "status": "ready",
                                    "plan": ["env/planner/T-8/out/PLAN.md", "env/planner/T-8/out/briefs/T-9.md"]}],
                  klass="plan")
        p = self.phases("T-8")
        self.assertEqual(p["current"], "done", p)
        self.assertIn("PLAN.md", p["phases"][2]["evidence"])
        self.assertIn("0 of 0", p["phases"][3]["evidence"])

    def test_a_plan_without_its_plan_holds_realize(self) -> None:
        self.flow("T-8", "blocked", [{"kind": "worker", "outcome": "done", "status": "blocked",
                                      "why": "the planner wrote no out/PLAN.md"}], klass="plan")
        p = self.phases("T-8")
        self.assertEqual(p["current"], "REALIZE")
        self.assertIn("no out/PLAN.md", p["phases"][2]["evidence"])

    def test_an_analysis_passes_without_commits(self) -> None:
        self.flow("T-9", "ready", [{"kind": "worker", "outcome": "done", "status": "ready"}], klass="analysis")
        p = self.phases("T-9")
        self.assertEqual(p["current"], "done", p)
        self.assertIn("no commits", p["phases"][2]["evidence"])

    def test_realize_follows_the_flow_rule_of_the_class(self) -> None:
        # reviews > 0 (security): a finished worker run without a head is what team flow blocks
        self.flow("T-1", "blocked", [{"kind": "worker", "outcome": "done", "status": "ready"}], klass="security")
        p = self.phases("T-1")
        self.assertEqual((p["current"], p["phases"][2]["state"]), ("REALIZE", "fail"), p)
        self.assertIn("made no commits", p["phases"][2]["evidence"])
        # reviews = 0 (analysis, decision): a finished run passes, with or without a head, and no gate is waited for
        self.flow("T-2", "ready", [{"kind": "worker", "outcome": "done", "status": "ready", "head": "d" * 40}],
                  klass="analysis")
        p = self.phases("T-2")
        self.assertEqual(p["current"], "done", p)
        self.assertIn("d" * 12, p["phases"][2]["evidence"])
        self.flow("T-3", "ready", [{"kind": "worker", "bot": "decide", "outcome": "done", "status": "ready"}],
                  klass="decision")
        p = self.phases("T-3")
        self.assertEqual(p["current"], "done", p)
        self.assertIn("no commits", p["phases"][2]["evidence"])
        self.assertIn("0 of 0", p["phases"][3]["evidence"])

    def test_check_follows_the_brief_skill(self) -> None:
        brief = self.tmp / "b.md"
        brief.write_text("Task: T-1\nClass: standard\nPaths: src/**\nObjective: do it\n")
        done = self.run_project("check", str(brief))
        self.assertEqual(done.returncode, 1)
        for section in ("Acceptance criteria", "Scope", "Out of scope", "Inputs", "Constraints", "Expected output"):
            self.assertIn(f"needs {section}", done.stdout)
        # a section may be a heading or a field with its lines below
        brief.write_text("Task: T-1\nClass: standard\nPaths: src/**\nObjective: do it\n\n## Acceptance criteria\n\n"
                         "- the endpoint answers 200\n\nScope:\n  src/api\n" + SECTIONS.split("\n", 2)[2])
        self.assertEqual(self.run_project("check", str(brief)).returncode, 0, self.run_project("check", str(brief)).stdout)
        brief.write_text("Task: T-1\nClass: standard\nPaths: src/**\nObjective: do it\nAcceptance criteria:\nScope: x\n"
                         + SECTIONS.split("\n", 2)[2])
        self.assertIn("needs Acceptance criteria", self.run_project("check", str(brief)).stdout)
        # a plan names the goal and no Paths (skills/brief.md step 5); every other class needs them
        brief.write_text("Task: T-1\nClass: plan\nObjective: move billing to the new API\n" + SECTIONS)
        done = self.run_project("check", str(brief))
        self.assertEqual(done.returncode, 0, done.stdout)
        brief.write_text("Task: T-1\nClass: standard\nObjective: do it\n" + SECTIONS)
        self.assertIn("Paths", self.run_project("check", str(brief)).stdout)
        self.flow("T-8", "new", [], klass="plan")
        self.assertEqual(self.phases("T-8")["phases"][0]["state"], "pass")

    def test_a_generated_brief_names_every_section_to_fill(self) -> None:
        done = self.run_project("brief", "Add a status endpoint to the api", "--repo", str(self.repo))
        for section in ("Acceptance criteria", "Scope", "Out of scope", "Inputs", "Constraints", "Expected output"):
            self.assertIn(f"{section}: TO FILL", done.stdout)
        plan = self.run_project("brief", "plan the billing migration in packages", "--repo", str(self.repo)).stdout
        self.assertNotIn("Paths:", plan)

    def test_peer_and_flow_text_is_shown_visibly(self) -> None:
        self.flow("T-4", "blocked", [{"kind": "worker", "outcome": "done", "status": "blocked",
                                      "why": "stop\x1b[2Jhidden"}])
        done = self.run_project("phase", "T-4")
        self.assertNotIn("\x1b", done.stdout)
        self.assertIn("\\x1b", done.stdout)

    def test_check_shows_problems_visibly(self) -> None:
        brief = self.tmp / "b.md"
        brief.write_text("Task: T-1\x1b[2J\nClass: standard\nPaths: src/**\nObjective: do it\n" + SECTIONS)
        done = self.run_project("check", str(brief))
        self.assertEqual(done.returncode, 1)
        self.assertNotIn("\x1b", done.stdout)

    def test_deps_reports_unreadable_and_repeated_briefs(self) -> None:
        folder = self.write_plan({"T-1": "none", "T-2": "T-1"})
        (folder / "bad.md").write_bytes(b"Task: T-3\n\xff\xfe\n")
        (folder / "copy.md").write_text("Task: T-1\nDepends: none\n")
        done = self.run_project("deps", str(folder))
        self.assertEqual(done.returncode, 1)
        self.assertNotIn("Traceback", done.stderr)
        self.assertIn("bad.md", done.stdout)
        self.assertIn("T-1 is in two briefs", done.stdout)

    def test_a_brief_that_is_not_utf8_is_an_error_not_a_traceback(self) -> None:
        self.flow("T-6", "ready", [])
        (self.team / "flows/T-6/brief.md").write_bytes(b"Task: T-6\nClass: standard\n\xff\xfe\n")
        done = self.run_project("phase", "T-6")
        self.assertEqual(done.returncode, 2)
        self.assertNotIn("Traceback", done.stderr)
        self.assertIn("UTF-8", done.stderr)

    def test_brief_runs_no_fsmonitor_of_the_repository(self) -> None:
        if not shutil.which("git"):
            self.skipTest("no git here")
        marker = self.tmp / "fsmonitor-ran"
        hook = self.tmp / "hook.sh"
        hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
        hook.chmod(0o755)
        for args in (["init", "-q"], ["config", "core.fsmonitor", str(hook)], ["add", "."]):
            subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)
        marker.unlink(missing_ok=True)
        done = self.run_project("brief", "Add a status endpoint to the api", "--repo", str(self.repo))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.fields(done.stdout)["Paths"], "src/api/**")
        self.assertFalse(marker.exists())

    def test_brief_knows_main_classes(self) -> None:
        done = self.run_project("brief", "plan the billing migration in packages", "--repo", str(self.repo))
        self.assertEqual(self.fields(done.stdout)["Class"], "plan")

    def test_an_unknown_task_is_an_error(self) -> None:
        done = self.run_project("phase", "T-404")
        self.assertEqual(done.returncode, 2)
        self.assertIn("T-404", done.stderr)



class FoundTest(unittest.TestCase):
    def test_the_brief_skill_names_project_and_the_boot_does_not(self) -> None:
        # Every session reads START.md at boot (BootBudgetTest); a lead finds bin/project when it writes a brief.
        self.assertNotIn("bin/project", (ROOT / ".agents" / "START.md").read_text(encoding="utf-8"))
        self.assertIn("`bin/project`", (ROOT / ".agents" / "skills" / "brief.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
