"""Bot end dates use local calendar days and are checked before staging."""
import subprocess
import sys
import unittest

from test_team_cli import Project

DRIVER = '''
import sys
from datetime import date
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
loader = SourceFileLoader("team_cli", sys.argv[1])
team = module_from_spec(spec_from_loader("team_cli", loader))
loader.exec_module(team)
core = sys.modules["teamlib.core"]
core.local_today = lambda: date(2026, 11, 4)
if sys.argv[2] == "active":
    print(",".join(b["id"] for b in core.active_bots()))
else:
    sys.exit(team.guarded(sys.argv[2:]))
'''


class BotUntilTest(unittest.TestCase):
    def setUp(self):
        self.p = Project(runner="alpha")
        self.addCleanup(self.p.close)

    def until(self, value):
        path = self.p.bot_file("worker")
        path.write_text(path.read_text().replace("---\n", f"---\nuntil: {value}\n", 1))

    def call(self, *args):
        return subprocess.run([sys.executable, "-c", DRIVER, str(self.p.root / ".agents/bin/team"), *args],
                              cwd=self.p.root, env=self.p.env, text=True, capture_output=True)

    def run_bot(self):
        return self.call("run", "worker", "T-1", "--brief", str(self.p.brief), "--dry-run")

    def test_past_refused_before_staging(self):
        self.until("2026-11-03")
        result = self.run_bot()
        self.assertEqual(result.returncode, 2)
        self.assertIn("worker", result.stderr)
        self.assertIn("its login or plan ended on 2026-11-03", result.stderr)
        self.assertIn("extend it and change or remove `until`, or set status: paused", result.stderr)
        self.assertFalse((self.p.root / ".agents/team/env/worker/tasks/T-1").exists())

    def test_today_allowed(self):
        self.until("2026-11-04")
        self.assertEqual(self.run_bot().returncode, 0)

    def test_three_day_notice(self):
        self.until("2026-11-07")
        for result in (self.run_bot(), self.call("status")):
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("worker ends on 2026-11-07 (in 3 days)", result.stdout + result.stderr)

    def test_active_bots_excludes_ended_and_status_lists_once(self):
        self.until("2026-11-03")
        self.assertNotIn("worker", self.call("active").stdout.strip().split(","))
        result = self.call("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("ended 2026-11-03"), 1)

    def test_malformed_refused_by_run_and_status(self):
        for value in ("tomorrow", "2026-02-30", "2026-1-01", ""):
            with self.subTest(value=value):
                path = self.p.bot_file("worker")
                original = path.read_text()
                self.until(value)
                for result in (self.run_bot(), self.call("status")):
                    self.assertEqual(result.returncode, 2)
                    self.assertIn("worker.md", result.stderr)
                path.write_text(original)
