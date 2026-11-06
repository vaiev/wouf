"""Staged runs (T-0028): team run --staged and team flow --staged run the bot as usual but bring nothing back into the
repository; team result shows what would come back, team apply brings it back on purpose."""

from __future__ import annotations

import json
import subprocess
import unittest

from test_team_cli import KEY, Project
from test_team_cli_gate import FLOW_REVIEW, FLOW_WORKER

COMMIT = "echo \"$1\" >> file.txt && git commit -q -am \"$1\"\n"


class StagedRunTest(unittest.TestCase):
    def setUp(self) -> None:
        self.p = Project(mode="host", runner="fake")
        self.addCleanup(self.p.close)
        self.worker = self.p.root / ".agents" / "team" / "env" / "worker"
        self.worker.mkdir(parents=True)
        (self.worker / "fake-cli.sh").write_text(
            "#!/bin/sh\nc() {\n" + COMMIT + "}\necho notes > out/NOTES.md\nmkdir -p out/sub && echo x > out/sub/a.txt\n"
            "cd repo\nc first\nc second\ngit checkout -q -b side\nc aside\ncd ..\n"
            "echo 'WOUF Worker · T-1 · ready-for-review'\n")

    def run_bot(self, *extra: str) -> subprocess.CompletedProcess:
        return self.p.team("run", "worker", "T-1", "--brief", str(self.p.brief), "--repo", *extra)

    def stage(self):
        return self.worker / "tasks" / "T-1"

    def result(self) -> dict:
        return json.loads((self.stage() / "result.json").read_text())

    def refs(self) -> str:
        return self.p.git("for-each-ref")

    def test_a_staged_run_creates_no_refs(self) -> None:
        before = self.refs()
        run = self.run_bot("--staged")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(self.refs(), before)
        self.assertTrue((self.stage() / "out" / "result.bundle").is_file())
        self.assertTrue((self.stage() / "out" / "NOTES.md").is_file())
        self.assertTrue(self.result()["staged"])
        self.assertIsNone(self.result()["branch"])

    def test_the_result_lists_commits_diff_stat_and_output_names(self) -> None:
        self.assertEqual(self.run_bot("--staged").returncode, 0)
        before = self.refs()
        shown = self.p.team("result", "worker", "T-1")
        self.assertEqual(shown.returncode, 0, shown.stdout + shown.stderr)
        lines = shown.stdout.splitlines()
        self.assertIn("staged", lines[0])
        text = shown.stdout
        self.assertLess(text.index("first"), text.index("second"))
        self.assertNotIn("aside", text)
        self.assertIn("file.txt | 2 ++", text)
        self.assertIn("1 file changed", text)
        self.assertLess(text.index("second"), text.index("file.txt |"))
        self.assertLess(text.index("1 file changed"), text.index("NOTES.md"))
        self.assertIn("sub/a.txt", text)
        self.assertNotIn("notes", text.replace("NOTES", ""), "no file contents")
        self.assertNotIn("ready-for-review", text)
        self.assertEqual(self.refs(), before, "the view writes no ref")

    def test_apply_creates_the_branch(self) -> None:
        self.assertEqual(self.run_bot("--staged").returncode, 0)
        applied = self.p.team("apply", "worker", "T-1")
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        self.assertIn("wouf/T-1", applied.stdout)
        self.assertEqual(self.p.git("log", "-2", "--format=%s", "wouf/T-1").split(), ["second", "first"])
        self.assertEqual(self.p.git("log", "-1", "--format=%s", "wouf/T-1--side").strip(), "aside")
        self.assertEqual(self.result()["branch"], "wouf/T-1")
        shown = self.p.team("result", "worker", "T-1")
        self.assertEqual(shown.returncode, 0, shown.stdout + shown.stderr)
        self.assertIn("ready-for-review", shown.stdout)
        self.assertIn("commits are on branch wouf/T-1", shown.stdout)

    def test_apply_refuses_an_existing_branch(self) -> None:
        self.assertEqual(self.run_bot("--staged").returncode, 0)
        self.p.git("branch", "wouf/T-1")
        before = self.refs()
        refused = self.p.team("apply", "worker", "T-1")
        self.assertEqual(refused.returncode, 2)
        self.assertIn("branch wouf/T-1 exists", refused.stderr)
        self.assertEqual(self.refs(), before)
        self.assertTrue(self.result()["staged"])

    def test_apply_refuses_a_run_that_was_not_staged(self) -> None:
        self.assertEqual(self.run_bot().returncode, 0)
        refused = self.p.team("apply", "worker", "T-1")
        self.assertEqual(refused.returncode, 2)
        self.assertIn("was not staged", refused.stderr)

    def test_apply_refuses_a_missing_bundle(self) -> None:
        self.assertEqual(self.run_bot("--staged").returncode, 0)
        (self.stage() / "out" / "result.bundle").unlink()
        before = self.refs()
        refused = self.p.team("apply", "worker", "T-1")
        self.assertEqual(refused.returncode, 2)
        self.assertIn("bundle", refused.stderr)
        self.assertIn("missing", refused.stderr)
        self.assertEqual(self.refs(), before)

    def test_staged_with_dry_run_is_refused(self) -> None:
        for order in (("--staged", "--dry-run"), ("--dry-run", "--staged")):
            with self.subTest(order=order):
                refused = self.run_bot(*order)
                self.assertEqual(refused.returncode, 2)
                self.assertIn("--staged", refused.stderr)
                self.assertIn("dry run", refused.stderr)
                self.assertEqual(refused.stdout, "")
                self.assertFalse(self.stage().exists())


class StagedFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.p = Project(mode="host", runner="fake")
        self.addCleanup(self.p.close)
        env = self.p.root / ".agents" / "team" / "env"
        for member, script in (("worker", FLOW_WORKER), ("review", FLOW_REVIEW)):
            (env / member).mkdir(parents=True)
            (env / member / "fake-cli.sh").write_text(script)
        (env / "review" / "plan").write_text("approve\n")
        self.p.config.write_text(self.p.config.read_text() + "\n[flow]\ngate = grep -q work file.txt\n")
        self.p.store_secret("ALPHA_KEY", KEY)
        self.p.brief.write_text("Class: standard\nPaths: file.txt\nObjective: change file.txt\n")

    def flow(self, *args: str) -> subprocess.CompletedProcess:
        return self.p.team("flow", "T-40", *args)

    def kinds(self) -> list:
        state = json.loads((self.p.root / ".agents" / "team" / "flows" / "T-40" / "state.json").read_text())
        return [step["kind"] for step in state["steps"]]

    def test_the_flow_stops_after_its_work_step(self) -> None:
        before = self.p.git("for-each-ref")
        result = self.flow("--brief", str(self.p.brief), "--repo", str(self.p.root), "--staged")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("staged: apply with team apply worker T-40, then team flow T-40 --retry", result.stdout)
        self.assertEqual(self.kinds(), ["worker"])
        self.assertEqual(self.p.git("for-each-ref"), before)
        self.assertFalse((self.p.root / ".agents" / "team" / "env" / "review" / "tasks").exists())
        # Applied on purpose, the flow goes on from the worker's commits: gate and review, no second worker run.
        self.assertEqual(self.p.team("apply", "worker", "T-40").returncode, 0)
        done = self.flow("--retry")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.kinds(), ["worker", "gate", "review"])
        head = self.p.git("rev-parse", "wouf/T-40").strip()
        self.assertEqual(done.stdout.strip().splitlines()[-1], f"ready wouf/T-40 {head}")


if __name__ == "__main__":
    unittest.main()
