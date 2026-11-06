"""Commits a bot made outside its task branch are kept (T-0041): other branches and a detached HEAD come back as
<result branch>--<name>, and a run whose task branch has no new commits but other branches do needs attention."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest

from test_team_cli import Project

COMMIT = "echo \"$1\" >> file.txt && git commit -q -am \"$1\"\n"


class RunBranchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.p = Project(mode="host", runner="fake")
        self.addCleanup(self.p.close)
        self.worker = self.p.root / ".agents" / "team" / "env" / "worker"
        self.worker.mkdir(parents=True)

    def run_bot(self, script: str) -> subprocess.CompletedProcess:
        (self.worker / "fake-cli.sh").write_text(
            "#!/bin/sh\nc() {\n" + COMMIT + "}\ncd repo\n" + script + "cd ..\necho 'WOUF Worker · T-1 · ready-for-review'\n")
        return self.p.team("run", "worker", "T-1", "--brief", str(self.p.brief), "--repo")

    def result(self) -> dict:
        return json.loads((self.worker / "tasks" / "T-1" / "result.json").read_text())

    def branches(self) -> list[str]:
        return self.p.git("branch", "--list", "wouf/*", "--format=%(refname:short)").split()

    def receipt(self) -> str:
        return subprocess.run([sys.executable, str(self.p.root / ".agents" / "bin" / "log"), "task", "T-1"],
                              capture_output=True, text=True, env=self.p.env).stdout

    def test_commits_only_on_another_branch_are_kept_and_need_attention(self) -> None:
        run = self.run_bot("git checkout -q -b atlas/T-0019-15r\nc other\n")
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        result = self.result()
        self.assertEqual(result["state"], "needs-attention")
        self.assertIsNone(result["branch"])
        self.assertEqual(result["kept"], ["wouf/T-1--atlas-T-0019-15r"])
        self.assertIn("atlas/T-0019-15r", result["reason"])
        self.assertEqual(self.branches(), ["wouf/T-1--atlas-T-0019-15r"])
        self.assertEqual(self.p.git("log", "-1", "--format=%s", "wouf/T-1--atlas-T-0019-15r").strip(), "other")
        self.assertIn("wouf/T-1--atlas-T-0019-15r", run.stdout + run.stderr)
        self.assertIn("atlas/T-0019-15r", self.receipt())
        again = self.p.team("result", "worker", "T-1")
        self.assertIn("wouf/T-1--atlas-T-0019-15r", again.stdout + again.stderr)
        self.assertIn("needs attention", again.stdout + again.stderr)

    def test_commits_on_both_keep_both_and_stay_done(self) -> None:
        run = self.run_bot("c mine\ngit checkout -q -b side\nc aside\n")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        result = self.result()
        self.assertEqual((result["state"], result["branch"]), ("done", "wouf/T-1"))
        self.assertEqual(result["kept"], ["wouf/T-1--side"])
        self.assertEqual(self.branches(), ["wouf/T-1", "wouf/T-1--side"])
        self.assertEqual(self.p.git("log", "-1", "--format=%s", "wouf/T-1").strip(), "mine")
        self.assertEqual(self.p.git("log", "-1", "--format=%s", "wouf/T-1--side").strip(), "aside")
        self.assertIn("wouf/T-1--side", run.stdout + run.stderr)

    def test_commits_on_a_detached_head_are_kept(self) -> None:
        run = self.run_bot("git checkout -q --detach\nc loose\n")
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        result = self.result()
        self.assertEqual(result["state"], "needs-attention")
        self.assertEqual(result["kept"], ["wouf/T-1--detached"])
        self.assertIn("detached", result["reason"])
        self.assertEqual(self.p.git("log", "-1", "--format=%s", "wouf/T-1--detached").strip(), "loose")

    def test_commits_on_the_local_base_branch_are_kept(self) -> None:
        run = self.run_bot("git checkout -q base\nc moved\n")
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        result = self.result()
        self.assertEqual((result["state"], result["branch"]), ("needs-attention", None))
        self.assertEqual(result["kept"], ["wouf/T-1--base"])
        self.assertIn("base", result["reason"])
        self.assertEqual(self.p.git("log", "-1", "--format=%s", "wouf/T-1--base").strip(), "moved")

    def test_a_task_branch_on_a_moved_base_comes_back_whole(self) -> None:
        run = self.run_bot("git checkout -q base\nc first\ngit checkout -q -B task/T-1 base\nc second\n")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        result = self.result()
        self.assertEqual((result["state"], result["branch"]), ("done", "wouf/T-1"))
        self.assertEqual(self.p.git("log", "-2", "--format=%s", "wouf/T-1").split(), ["second", "first"])

    def test_commits_on_a_renamed_task_branch_are_kept(self) -> None:
        run = self.run_bot("git branch -m task/T-1 atlas/T-0019-15r\nc other\n")
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        result = self.result()
        self.assertEqual((result["state"], result["branch"]), ("needs-attention", None))
        self.assertEqual(result["kept"], ["wouf/T-1--atlas-T-0019-15r"])
        self.assertIn("atlas/T-0019-15r", result["reason"])
        self.assertEqual(self.p.git("log", "-1", "--format=%s", "wouf/T-1--atlas-T-0019-15r").strip(), "other")

    def test_commits_after_deleting_the_task_branch_are_kept(self) -> None:
        run = self.run_bot("git checkout -q -b side\ngit branch -D task/T-1\nc other\n")
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        result = self.result()
        self.assertEqual((result["state"], result["branch"]), ("needs-attention", None))
        self.assertEqual(result["kept"], ["wouf/T-1--side"])
        self.assertEqual(self.p.git("log", "-1", "--format=%s", "wouf/T-1--side").strip(), "other")

    def test_no_commits_stays_done(self) -> None:
        run = self.run_bot("git checkout -q -b empty\n")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        result = self.result()
        self.assertEqual((result["state"], result["branch"], result["kept"]), ("done", None, []))
        self.assertEqual(self.branches(), [])


if __name__ == "__main__":
    unittest.main()
