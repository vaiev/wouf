"""A locked pass store must end quickly with one line, never hang a team call and never print unmasked output."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRET = ROOT / ".agents" / "bin" / "secret"
TEAM = ROOT / ".agents" / "bin" / "team"
LOG = ROOT / ".agents" / "bin" / "log"
VALUE = "s3cr3t-value-for-locked-tests"
# Stands in for pass(1). Every call is logged; it sleeps (a locked store waiting for a passphrase) while
# $PASS_STUB_DIR/sleep exists. Entries are files under $PASS_STUB_DIR.
PASS_STUB = """#!/bin/sh
echo "$*" >> "$PASS_STUB_DIR/argv"
echo "$PPID $*" >> "$PASS_STUB_DIR/calls"
echo "$PASSWORD_STORE_GPG_OPTS" >> "$PASS_STUB_DIR/gpg_opts"
if [ -e "$PASS_STUB_DIR/sleep" ]; then sleep 60; fi
case "$1" in
  show) cat "$PASS_STUB_DIR/$2" 2>/dev/null || exit 1 ;;
  *) exit 2 ;;
esac
"""
# Loads bin/team as a module (every teamlib module plugs in), then reads the stored values the way a command does.
DRIVER = """
import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
loader = SourceFileLoader("team_cli", sys.argv[1])
team = module_from_spec(spec_from_loader("team_cli", loader))
loader.exec_module(team)
core = sys.modules["teamlib.core"]
core.register_stored()
core.register_stored()
print(sorted(core.stored(["API_TOKEN", "DEPLOY_KEY"])))
"""
# Loads bin/log as a module, the way companion and federate do, and masks a line with a stored value in it.
IMPORTER = """
import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
loader = SourceFileLoader("wouf_imported_log", sys.argv[1])
log = module_from_spec(spec_from_loader("wouf_imported_log", loader))
loader.exec_module(log)
print(log.mask("note s3cr3t-value-for-locked-tests end"))
"""


class LockedPassTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = root / "store"
        self.store.mkdir()
        (self.store / ".names").write_text("API_TOKEN\nDEPLOY_KEY\n")
        self.bin = root / "bin"
        self.bin.mkdir()
        self.entries = root / "pass"
        (self.entries / "wouf").mkdir(parents=True)
        (self.entries / "wouf" / "API_TOKEN").write_text(f"{VALUE}\n")
        (self.entries / "wouf" / "DEPLOY_KEY").write_text("another-value-for-locked-tests\n")
        stub = self.bin / "pass"
        stub.write_text(PASS_STUB)
        stub.chmod(0o755)
        team = root / "team"
        team.mkdir()
        (team / "config.ini").write_text((ROOT / ".agents" / "team" / "config.ini").read_text())
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("WOUF_", "PASSWORD_STORE"))}
        self.logs = root / "logs"
        home = root / "home"
        home.mkdir()
        # A home of its own: the caller's ~/.password-store and ~/.config must not decide what the stub returns.
        self.env.update({"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config")})
        self.env.update({"WOUF_SECRET_BACKEND": "pass", "WOUF_SECRET_DIR": str(self.store),
                         "WOUF_TEAM_DIR": str(team), "PASS_STUB_DIR": str(self.entries), "WOUF_LOG_DIR": str(self.logs),
                         "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}"})

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def lock(self) -> None:
        (self.entries / "sleep").touch()

    def run_cmd(self, *args: str, timeout: float = 10) -> tuple[subprocess.CompletedProcess, float]:
        started = time.monotonic()
        result = subprocess.run(*args, env=self.env, cwd=self.tmp.name, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=timeout)
        return result, time.monotonic() - started

    def test_help_ends_within_ten_seconds_with_one_line(self) -> None:
        self.lock()
        result, elapsed = self.run_cmd([sys.executable, str(TEAM), "--help"])
        self.assertLess(elapsed, 10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("team context", result.stdout, "the usage text still goes on")
        lines = result.stderr.splitlines()
        self.assertEqual(len(lines), 1, result.stderr)
        self.assertIn("pass store", lines[0])
        self.assertIn("unlock", lines[0])
        self.assertNotIn(VALUE, result.stdout + result.stderr)
        self.assertNotIn("API_TOKEN", result.stderr)

    def test_a_command_that_prints_team_data_fails_closed(self) -> None:
        self.lock()
        result, elapsed = self.run_cmd([sys.executable, str(TEAM), "context"])
        self.assertLess(elapsed, 10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "", "nothing may reach the output unmasked")
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertIn("pass store", result.stderr)

    def test_secret_run_on_a_locked_store_is_one_line(self) -> None:
        self.lock()
        result, elapsed = self.run_cmd([sys.executable, str(SECRET), "run", "API_TOKEN", "--", "echo", "hi"])
        self.assertLess(elapsed, 10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertNotIn(VALUE, result.stderr)

    def test_log_on_a_locked_store_writes_nothing_and_fails_closed(self) -> None:
        self.lock()
        result, elapsed = self.run_cmd([sys.executable, str(LOG), "add", "review", "T-9", "done",
                                        f"leak {VALUE} here"])
        self.assertLess(elapsed, 10)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertIn("store", result.stderr)
        self.assertIn("unlock", result.stderr)
        self.assertNotIn(VALUE, result.stderr)
        self.assertFalse((self.logs / "review.jsonl").exists(), "no receipt may be written unmasked")

    def test_log_usage_still_goes_on_with_a_locked_store(self) -> None:
        self.lock()
        result, elapsed = self.run_cmd([sys.executable, str(LOG), "--help"])
        self.assertLess(elapsed, 10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("log add ID", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_pass_is_asked_not_to_start_pinentry(self) -> None:
        result, _ = self.run_cmd([sys.executable, str(SECRET), "has", "API_TOKEN"])
        self.assertEqual(result.returncode, 0, result.stderr)
        options = (self.entries / "gpg_opts").read_text().splitlines()
        self.assertTrue(options and all("--pinentry-mode error" in line for line in options), options)

    def test_each_stored_value_is_read_once_per_process(self) -> None:
        result, _ = self.run_cmd([sys.executable, "-c", DRIVER, str(TEAM)])
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = (self.entries / "argv").read_text().splitlines()
        self.assertEqual(calls.count("show wouf/API_TOKEN"), 1, calls)
        self.assertEqual(calls.count("show wouf/DEPLOY_KEY"), 1, calls)
        self.assertNotIn(VALUE, result.stdout, "the driver prints names only")

    def test_a_real_command_reads_each_stored_value_once(self) -> None:
        result, _ = self.run_cmd([sys.executable, str(TEAM), "context"])
        self.assertEqual(result.returncode, 0, result.stderr)
        # Counted per process: a log subprocess of team is its own process and reads for itself.
        calls = Counter((line.split()[0], line.split(None, 1)[1]) for line in
                        (self.entries / "calls").read_text().splitlines())
        per_process = {(pid, name): n for (pid, name), n in calls.items() if name.startswith("show ")}
        self.assertTrue(per_process, "the command read nothing from the store")
        self.assertEqual(max(per_process.values()), 1, per_process)
        self.assertNotIn(VALUE, result.stdout + result.stderr)

    def test_a_module_that_loads_bin_log_fails_closed_on_a_locked_store(self) -> None:
        # companion and federate load bin/log as a module and use its mask: a locked store must stop them, not pass
        # text through unmasked.
        self.lock()
        result, elapsed = self.run_cmd([sys.executable, "-c", IMPORTER, str(LOG)])
        self.assertLess(elapsed, 10)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "", "no text may leave the importer unmasked")
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertIn("unlock", result.stderr)
        self.assertNotIn(VALUE, result.stderr)


if __name__ == "__main__":
    unittest.main()
