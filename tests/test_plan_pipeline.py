"""P03: planner/batch contract, repository selection, and safe wave recovery."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_batch import SLOW_CLI, batch_module, brief
from test_team_cli import KEY, Project


TABLE = "| ID | Wave | Runner | Depends |\n|---|---|---|---|\n"


class SchemaTest(unittest.TestCase):
    def setUp(self):
        self.b = batch_module()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.plan = Path(tmp.name)
        (self.plan / "briefs").mkdir()
        (self.plan / "PLAN.md").write_text(TABLE + "| a | 1 | w | none |\n| b | 2 | w | a |\n")
        for pid in ("a", "b"):
            (self.plan / "briefs" / (pid + ".md")).write_text(brief("P-" + pid, pid + ".txt"))

    def refused(self, fragment):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            self.b.packages(self.plan, 1)
        self.assertIn(fragment, err.getvalue())

    def test_all_waves_have_complete_briefs_before_dispatch(self):
        (self.plan / "briefs" / "b.md").write_text("Task: P-b\nClass: standard\nPaths: b.txt\nObjective: change b\n")
        self.refused("Acceptance criteria")

    def test_duplicate_package_and_task_ids_are_refused(self):
        with (self.plan / "PLAN.md").open("a") as out:
            out.write("| a | 3 | w | b |\n")
        self.refused("duplicate package")
        (self.plan / "PLAN.md").write_text(TABLE + "| a | 1 | w | none |\n| b | 2 | w | a |\n")
        (self.plan / "briefs" / "b.md").write_text(brief("P-a", "b.txt"))
        self.refused("duplicate task")

    def test_dependencies_must_name_an_earlier_wave(self):
        for dep in ("missing", "a", "b"):
            (self.plan / "PLAN.md").write_text(TABLE + f"| a | 1 | w | {dep} |\n| b | 2 | w | a |\n")
            self.refused("depend")

    def test_runner_and_table_are_required(self):
        (self.plan / "PLAN.md").write_text("# Free form plan\n")
        self.refused("ID")
        (self.plan / "PLAN.md").write_text(TABLE + "| a | 1 | | none |\n")
        self.refused("runner")

    def test_ambiguous_paths_and_unfilled_briefs_are_refused(self):
        for paths in ("../a", "/a", "./a", "a//b", "a/../b", "a\\b"):
            (self.plan / "briefs" / "a.md").write_text(brief("P-a", paths))
            self.refused("Paths")
        (self.plan / "briefs" / "a.md").write_text(brief("P-a", "a.txt", "Constraints: TO FILL\n"))
        self.refused("TO FILL")


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.p = Project(mode="host", runner="fake")
        self.addCleanup(self.p.close)
        self.team = self.p.root / ".agents" / "team"
        for member in ("planner", "worker", "scout", "review"):
            folder = self.team / "env" / member
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "fake-cli.sh").write_text(SLOW_CLI.replace("sleep 2", ""))
        self.p.config.write_text(self.p.config.read_text() + "\n[flow]\ngate = true\n")
        self.p.store_secret("ALPHA_KEY", KEY)
        self.plan = self.p.root / "plan"
        (self.plan / "briefs").mkdir(parents=True)
        self.write_plan()

    def write_plan(self):
        (self.plan / "PLAN.md").write_text(TABLE + "| a | 1 | w | none |\n| b | 2 | w | a |\n")
        for pid in ("a", "b"):
            (self.plan / "briefs" / (pid + ".md")).write_text(brief("P-" + pid, "file.txt"))

    def batch(self, wave="1", *args):
        return self.p.team("batch", str(self.plan), "--wave", wave, *args)

    def state(self, task):
        return json.loads((self.team / "flows" / task / "state.json").read_text())

    def ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def external(self):
        path = self.p.root / "external"
        path.mkdir()
        for args in (("init", "-q"), ("config", "user.name", "Fixture"),
                     ("config", "user.email", "fixture@example.invalid")):
            subprocess.run(["git", "-C", str(path), *args], check=True)
        (path / "file.txt").write_text("external\n")
        subprocess.run(["git", "-C", str(path), "add", "file.txt"], check=True)
        subprocess.run(["git", "-C", str(path), "commit", "-qm", "external"], check=True)
        return path

    def test_external_repository_and_explicit_base_reach_every_flow(self):
        repo = self.external()
        base = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        (repo / "future.txt").write_text("after the selected base\n")
        subprocess.run(["git", "-C", str(repo), "add", "future.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "later head"], check=True)
        self.ok(self.batch("1", "--repo", str(repo) + "@" + base))
        state = self.state("P-a")
        self.assertEqual((state["repo"], state["base"]), (str(repo), base))
        self.assertEqual(self.p.git("branch", "--list", "wouf/P-a"), "")
        self.assertNotEqual(state["head"], base)
        files = subprocess.check_output(["git", "-C", str(repo), "ls-tree", "--name-only", state["head"]], text=True)
        self.assertNotIn("future.txt", files)

    def test_later_wave_requires_ready_heads_merged_in_its_base(self):
        premature = self.batch("2")
        self.assertNotEqual(premature.returncode, 0)
        self.assertIn("prior wave", premature.stderr)
        self.assertFalse((self.team / "flows" / "P-b").exists())
        self.ok(self.batch())
        not_merged = self.batch("2")
        self.assertNotEqual(not_merged.returncode, 0)
        self.assertIn("merged", not_merged.stderr)
        self.p.git("merge", "--ff-only", "wouf/P-a")
        self.ok(self.batch("2"))
        self.assertEqual(self.state("P-b")["base"], self.state("P-a")["head"])

    def test_ready_wave_is_idempotent_and_keeps_its_base_after_head_moves(self):
        self.ok(self.batch())
        before = self.state("P-a")
        self.p.git("merge", "--ff-only", "wouf/P-a")
        self.ok(self.batch())
        self.assertEqual(self.state("P-a"), before)
        changed = self.batch("1", "--repo", str(self.p.root) + "@HEAD")
        self.assertNotEqual(changed.returncode, 0)
        self.assertIn("base", changed.stderr)

    def test_changed_plan_or_repository_cannot_hijack_a_wave(self):
        self.ok(self.batch())
        other = self.batch("1", "--repo", str(self.external()))
        self.assertNotEqual(other.returncode, 0)
        self.assertIn("repository", other.stderr)
        with (self.plan / "briefs" / "a.md").open("a") as out:
            out.write("\nA changed requirement.\n")
        changed = self.batch()
        self.assertNotEqual(changed.returncode, 0)
        self.assertIn("changed", changed.stderr)

    def test_interrupted_and_limited_flows_resume_without_new_creation_arguments(self):
        self.ok(self.batch())
        original = self.state("P-a")
        path = self.team / "flows" / "P-a" / "state.json"
        for status in ("running", "limited"):
            # The worker's commit returned, but the supervising batch was interrupted before gate/review.
            state = {**original, "steps": original["steps"][:1], "status": status, "pid": 99999999}
            path.write_text(json.dumps(state))
            self.ok(self.batch())
            self.assertEqual(self.state("P-a")["status"], "ready")
            self.assertEqual(self.state("P-a")["head"], original["head"])
            self.assertEqual([st["kind"] for st in self.state("P-a")["steps"]],
                             [st["kind"] for st in original["steps"]])

    def test_a_real_usage_limit_recovers_through_the_package_then_the_wave(self):
        worker = self.team / "env" / "worker" / "fake-cli.sh"
        worker.write_text(SLOW_CLI.replace("sleep 2", "").replace(
            '*Worker*) (cd repo',
            '*Worker*) if [ ! -e ../../once ]; then touch ../../once; '
            'echo "You\'ve hit your usage limit. Try again in 2 hours."; exit 1; fi\n(cd repo'))
        limited = self.batch()
        self.assertEqual(limited.returncode, 4, limited.stdout + limited.stderr)
        base = self.state("P-a")["base"]
        self.assertTrue(self.state("P-a").get("batch"))
        self.ok(self.p.team("flow", "P-a", "--retry"))
        self.ok(self.batch())
        self.assertEqual(self.state("P-a")["base"], base)
        self.assertEqual(self.state("P-a")["status"], "ready")

    def test_a_real_question_keeps_the_answer_when_the_wave_resumes(self):
        worker = self.team / "env" / "worker" / "fake-cli.sh"
        worker.write_text(SLOW_CLI.replace("sleep 2", "").replace(
            '*Worker*) (cd repo',
            '*Worker*) if ! grep -q "Answer from the lead" in/BRIEF.md; then '
            'echo "$sign · blocked"; echo "Questions: which behaviour?"; exit 0; fi\n(cd repo'))
        question = self.batch()
        self.assertNotEqual(question.returncode, 0)
        self.assertEqual(self.state("P-a")["status"], "question")
        self.ok(self.p.team("flow", "P-a", "--answer", "Use the briefed behaviour."))
        self.ok(self.batch())
        self.assertEqual(self.state("P-a")["status"], "ready")

    def test_blocked_package_names_the_per_package_recovery_command(self):
        self.ok(self.batch())
        state = self.state("P-a")
        state.update(status="blocked", why="fixture question")
        (self.team / "flows" / "P-a" / "state.json").write_text(json.dumps(state))
        result = self.batch()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("team flow P-a --answer", result.stdout)
        self.assertIn("--retry", result.stdout)

    def test_planner_output_is_validated_by_the_same_batch_contract(self):
        planner = self.team / "env" / "planner" / "fake-cli.sh"
        script = SLOW_CLI.replace("sleep 2", "")
        artifact_script = "mkdir -p out/briefs\ncat > out/PLAN.md <<'PLAN'\n" + (self.plan / "PLAN.md").read_text() + "PLAN\n"
        for pid in ("a", "b"):
            artifact_script += "cat > out/briefs/" + pid + ".md <<'BRIEF'\n" + (self.plan / "briefs" / (pid + ".md")).read_text() + "BRIEF\n"
        script = script.replace('  *) echo "$sign · ready-for-review" ;;',
                                '  *Planner*)\n' + artifact_script + 'echo "$sign · ready-for-review" ;;\n'
                                '  *) echo "$sign · ready-for-review" ;;')
        planner.write_text(script)
        self.p.brief.write_text("Class: plan\nObjective: create packages\n")
        self.ok(self.p.team("flow", "T-plan", "--brief", str(self.p.brief), "--repo", str(self.p.root)))
        step = self.state("T-plan")["steps"][-1]
        self.assertTrue(step.get("plan_dispatchable"), step)
        out = self.team / "env" / "planner" / "tasks" / "T-plan" / "out"
        self.ok(self.p.team("batch", str(out), "--wave", "1", "--dry-run"))

    def test_incomplete_planner_artifacts_are_explicitly_non_dispatchable_drafts(self):
        planner = self.team / "env" / "planner" / "fake-cli.sh"
        from test_team_cli_gate import FLOW_PLANNER
        planner.write_text(FLOW_PLANNER)
        self.p.brief.write_text("Class: plan\nObjective: draft packages\n")
        result = self.p.team("flow", "T-draft", "--brief", str(self.p.brief), "--repo", str(self.p.root))
        self.ok(result)
        self.assertIn("not dispatchable", result.stdout)
        self.assertFalse(self.state("T-draft")["steps"][-1].get("plan_dispatchable", True))


if __name__ == "__main__":
    unittest.main()
