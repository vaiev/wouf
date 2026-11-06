"""Offline goal CLI journey with real planner, batch, flow and review subprocesses."""
from __future__ import annotations

import json
import subprocess
import unittest

from test_batch import SLOW_CLI, brief
TABLE = "| ID | Wave | Runner | Depends |\n|---|---|---|---|\n"
from test_team_cli import KEY, Project


class GoalJourneyTest(unittest.TestCase):
    def setUp(self):
        self.p = Project(mode="host", runner="fake")
        self.addCleanup(self.p.close)
        self.team = self.p.root / ".agents" / "team"
        self.p.set("autonomy = ask", "autonomy = auto")
        self.p.config.write_text(self.p.config.read_text() + "\n[flow]\ngate = true\n")
        self.p.store_secret("ALPHA_KEY", KEY)
        self.repo = self.p.root / "external"
        self.repo.mkdir()
        for args in (("init", "-q"), ("config", "user.name", "Fixture"),
                     ("config", "user.email", "fixture@example.invalid")):
            subprocess.run(["git", "-C", str(self.repo), *args], check=True)
        (self.repo / "file.txt").write_text("external\n")
        self.git("add", "file.txt")
        self.git("commit", "-qm", "initial")
        self.script = script = SLOW_CLI.replace("sleep 2", "")
        planner = self.planner("| a | 1 | w | none |\n| b | 2 | w | a |\n", {"a": "file.txt", "b": "file.txt"})
        worker = script.replace('*Worker*) (cd repo',
            '*Worker*) if ! grep -q "Answer from the lead" in/BRIEF.md && [ "$path" = file.txt ] '
            '&& [ ! -e ../../answered ]; then echo "$sign · blocked"; '
            'echo "Questions: which behaviour?"; exit 0; fi\n'
            'touch ../../answered\n(cd repo')
        for member in ("planner", "worker", "scout", "review"):
            folder = self.team / "env" / member
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "fake-cli.sh").write_text(planner if member == "planner" else worker if member == "worker" else script)

    def planner(self, rows, paths):
        """A stand-in planner: blocked until answered, then writes PLAN.md and one brief per package."""
        artifacts = "mkdir -p out/briefs\ncat > out/PLAN.md <<'PLAN'\n" + TABLE + rows + "PLAN\n"
        for pid, path in paths.items():
            artifacts += "cat > out/briefs/" + pid + ".md <<'BRIEF'\n"
            artifacts += brief("P-" + pid, path) + "BRIEF\n"
        return self.script.replace('  *) echo "$sign · ready-for-review" ;;',
                                   '  *Planner*)\nif ! grep -q "Answer from the lead" in/BRIEF.md; then '
                                   'echo "$sign · blocked"; echo "Questions: which plan?"; exit 0; fi\n' + artifacts + 'echo "$sign · ready-for-review" ;;\n'
                                   '  *) echo "$sign · ready-for-review" ;;')

    def concept(self):
        self.cli("goal", "new", "Change file.txt", "--repo", str(self.repo))
        draft = self.team / "env" / "lead" / "T-1.md"
        draft.write_text(draft.read_text().replace("TO FILL", "Complete as specified"))
        self.cli("goal", "concept", "T-1", "--yes", code=5)
        self.cli("goal", "concept", "T-1", "--answer", "Use one wave.")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True).strip()

    def cli(self, *args, code=0):
        result = self.p.team(*args)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result.stdout + result.stderr

    def flow(self, task):
        return json.loads((self.team / "flows" / task / "state.json").read_text())

    def test_goal_two_waves_recovery_and_verify(self):
        self.cli("goal", "new", "Change file.txt", "--repo", str(self.repo))
        self.assertIn("T-2", self.cli("task", "new", "Separate task", "--class", "standard"))
        self.assertIn("not ready", self.cli("goal", "concept", "T-1", "--yes", code=2))
        draft = self.team / "env" / "lead" / "T-1.md"
        draft.write_text(draft.read_text().replace("TO FILL", "Complete as specified"))
        self.cli("goal", "concept", "T-1", "--yes", code=5)
        self.assertIn("blocked", self.cli("goal", "status", "T-1"))
        self.cli("goal", "concept", "T-1", "--answer", "Use two waves.")
        self.assertEqual(self.flow("T-1")["repo"], str(self.repo))
        self.assertTrue(self.flow("T-1")["steps"][-1]["plan_dispatchable"])
        self.cli("goal", "realize", "T-1", "--wave", "1", "--yes", code=5)
        self.assertEqual(self.flow("P-a")["status"], "question")
        self.assertIn("blocked", self.cli("goal", "status", "T-1"))
        self.cli("flow", "P-a", "--answer", "Use the specified behaviour.")
        self.cli("goal", "recover", "T-1", "--yes")
        self.cli("goal", "realize", "T-1", "--wave", "1")
        self.assertEqual(self.flow("P-a")["repo"], str(self.repo))
        (self.team / "env" / "goals" / "T-1.json").unlink()
        self.assertIn("flow ready", self.cli("goal", "status", "T-1"))
        self.assertIn("merged", self.cli("goal", "realize", "T-1", "--wave", "2", code=2))
        self.git("merge", "--ff-only", "wouf/P-a")
        self.cli("goal", "realize", "T-1", "--wave", "2")
        self.assertEqual(self.flow("P-b")["base"], self.flow("P-a")["head"])
        state_path = self.team / "flows" / "P-b" / "state.json"
        state = self.flow("P-b")
        review = next(step for step in state["steps"] if step["kind"] == "review")
        review["head"] = "a" * 40
        state["class"] = "security"
        state_path.write_text(json.dumps(state))
        output = self.cli("goal", "verify", "T-1", "--yes")
        self.assertIn("inspection only", output)
        self.assertIn("head mismatch", output)
        self.assertIn("approvals 0/2", output)
        self.cli("goal", "complete", "T-1", "--yes")
        self.assertIn("VERIFY (done)", self.cli("goal", "status", "T-1"))

    def test_goal_realize_refuses_overlapping_globs_in_one_wave(self):
        bots = self.team / "bots"
        for member, source in (("worker2", "worker"), ("scout2", "scout")):
            text = (bots / f"{source}.md").read_text().replace(f"id: {source}\n", f"id: {member}\n")
            (bots / f"{member}.md").write_text(text)
            (self.team / "env" / member).mkdir(parents=True, exist_ok=True)
            (self.team / "env" / member / "fake-cli.sh").write_text(self.script)
        (self.team / "env" / "planner" / "fake-cli.sh").write_text(self.planner(
            "| c | 1 | w | none |\n| d | 1 | w2 | none |\n", {"c": "src/a*.py", "d": "src/*b.py"}))
        self.concept()
        head = self.git("rev-parse", "HEAD")
        output = self.cli("goal", "realize", "T-1", "--wave", "1", "--yes", code=2)
        self.assertIn("overlap", output)
        self.assertFalse((self.team / "flows" / "P-c").exists())
        self.assertFalse((self.team / "flows" / "P-d").exists())
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
