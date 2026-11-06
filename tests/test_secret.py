"""Tests for .agents/bin/secret with the file backend in a temporary home."""

from __future__ import annotations

import os
import pty
import stat
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRET = ROOT / ".agents" / "bin" / "secret"
VALUE = "s3cr3t-value-for-tests"


class SecretTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / "store"
        self.env = {**{k: v for k, v in os.environ.items() if not k.startswith("WOUF_")}, "WOUF_SECRET_BACKEND": "file", "WOUF_SECRET_DIR": str(self.store)}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_secret(self, *args: str, cwd: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(SECRET), *args], env=self.env, cwd=cwd or self.tmp.name,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)

    def set_with_tty(self, name: str, value: str) -> int:
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(self.tmp.name)
            os.execve(sys.executable, [sys.executable, str(SECRET), "set", name], self.env)
        # getpass flushes pending input when it turns echo off, so answer only after the prompt.
        output = b""
        while b"(hidden)" not in output:
            output += os.read(fd, 1024)
        os.write(fd, (value + "\n").encode())
        while True:
            try:
                chunk = os.read(fd, 1024)
            except OSError:
                break
            if not chunk:
                break
            output += chunk
        _, status = os.waitpid(pid, 0)
        self.assertNotIn(value.encode(), output, "the value must not be echoed")
        return os.waitstatus_to_exitcode(status)

    def test_set_refuses_without_a_terminal(self) -> None:
        result = self.run_secret("set", "API_TOKEN")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("own terminal", result.stderr)

    def test_set_then_run_injects_and_masks(self) -> None:
        self.assertEqual(self.set_with_tty("API_TOKEN", VALUE), 0)
        mode = stat.S_IMODE((self.store / "API_TOKEN").stat().st_mode)
        self.assertEqual(mode, 0o600)
        self.assertEqual(stat.S_IMODE(self.store.stat().st_mode), 0o700)
        script = "import os; print('len', len(os.environ['API_TOKEN'])); print(os.environ['API_TOKEN'])"
        result = self.run_secret("run", "API_TOKEN", "--", sys.executable, "-c", script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"len {len(VALUE)}", result.stdout)
        self.assertNotIn(VALUE, result.stdout)
        self.assertIn("***", result.stdout)

    def test_run_keeps_the_exit_code(self) -> None:
        self.assertEqual(self.set_with_tty("API_TOKEN", VALUE), 0)
        result = self.run_secret("run", "API_TOKEN", "--", sys.executable, "-c", "raise SystemExit(3)")
        self.assertEqual(result.returncode, 3)

    def test_run_fails_closed_on_a_missing_secret(self) -> None:
        result = self.run_secret("run", "MISSING_TOKEN", "--", "true")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("secret set MISSING_TOKEN", result.stderr)

    def test_an_empty_stored_value_counts_as_missing(self) -> None:
        self.store.mkdir(mode=0o700, parents=True)
        (self.store / "EMPTY_TOKEN").write_text("\n")
        result = self.run_secret("run", "EMPTY_TOKEN", "--", sys.executable, "-c", "print('hello')")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("***", result.stdout)

    def test_has_and_list_show_names_only(self) -> None:
        self.assertEqual(self.set_with_tty("API_TOKEN", VALUE), 0)
        self.assertEqual(self.run_secret("has", "API_TOKEN").returncode, 0)
        self.assertNotEqual(self.run_secret("has", "OTHER_TOKEN").returncode, 0)
        listing = self.run_secret("list")
        self.assertIn("API_TOKEN", listing.stdout)
        self.assertNotIn(VALUE, listing.stdout)

    def test_there_is_no_get(self) -> None:
        self.assertNotEqual(self.run_secret("get", "API_TOKEN").returncode, 0)

    def test_rejects_bad_names(self) -> None:
        for name in ("lower", "../x", "A B", "1ABC"):
            self.assertNotEqual(self.run_secret("has", name).returncode, 0, name)

    @unittest.skipUnless(shutil.which("git"), "needs git, which is not installed")
    def test_refuses_a_store_inside_a_git_repository(self) -> None:
        repo = Path(self.tmp.name) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        self.env["WOUF_SECRET_DIR"] = str(repo / "secrets")
        result = self.run_secret("list", cwd=str(repo))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("inside a git repository", result.stderr)


# Stands in for pass(1): entries are files under $PASS_STUB_DIR, every argv is appended to $PASS_STUB_DIR/argv.
PASS_STUB = """#!/bin/sh
echo "$*" >> "$PASS_STUB_DIR/argv"
case "$1" in
  show) cat "$PASS_STUB_DIR/$2" 2>/dev/null || exit 1 ;;
  insert) shift; while [ "${1#-}" != "$1" ]; do shift; done
          mkdir -p "$(dirname "$PASS_STUB_DIR/$1")"; cat > "$PASS_STUB_DIR/$1" ;;
  rm) shift; while [ "${1#-}" != "$1" ]; do shift; done; rm -f "$PASS_STUB_DIR/$1" ;;
  *) exit 2 ;;
esac
"""


class PassBackendTest(SecretTest):
    def setUp(self) -> None:
        super().setUp()
        self.bin = Path(self.tmp.name) / "bin"
        self.entries = Path(self.tmp.name) / "pass"
        self.bin.mkdir()
        self.entries.mkdir()
        stub = self.bin / "pass"
        stub.write_text(PASS_STUB)
        stub.chmod(0o755)
        self.env.update({"WOUF_SECRET_BACKEND": "pass", "PASS_STUB_DIR": str(self.entries),
                         "PATH": f"{self.bin}:{os.environ['PATH']}"})

    def test_set_then_run_injects_and_masks(self) -> None:
        self.assertEqual(self.set_with_tty("API_TOKEN", VALUE), 0)
        self.assertEqual((self.entries / "wouf" / "API_TOKEN").read_text().splitlines()[0], VALUE)
        script = "import os; print(os.environ['API_TOKEN'])"
        result = self.run_secret("run", "API_TOKEN", "--", sys.executable, "-c", script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("***", result.stdout)
        self.assertNotIn(VALUE, (self.entries / "argv").read_text(), "the value must never be an argument")

    def test_only_the_first_line_is_the_value(self) -> None:
        (self.entries / "wouf").mkdir()
        (self.entries / "wouf" / "API_TOKEN").write_text(f"{VALUE}\nlogin: someone\n")
        script = "import os; print(len(os.environ['API_TOKEN']))"
        result = self.run_secret("run", "API_TOKEN", "--", sys.executable, "-c", script)
        self.assertIn(str(len(VALUE)), result.stdout)

    def test_the_prefix_can_be_chosen(self) -> None:
        self.env["WOUF_PASS_PREFIX"] = "team/bots"
        self.assertEqual(self.set_with_tty("API_TOKEN", VALUE), 0)
        self.assertTrue((self.entries / "team" / "bots" / "API_TOKEN").is_file())

    def test_rm_removes_the_entry(self) -> None:
        self.assertEqual(self.set_with_tty("API_TOKEN", VALUE), 0)
        pid, fd = pty.fork()
        if pid == 0:
            os.execve(sys.executable, [sys.executable, str(SECRET), "rm", "API_TOKEN"], self.env)
        while True:
            try:
                if not os.read(fd, 1024):
                    break
            except OSError:
                break
        os.waitpid(pid, 0)
        self.assertFalse((self.entries / "wouf" / "API_TOKEN").exists())
        self.assertNotEqual(self.run_secret("has", "API_TOKEN").returncode, 0)

    def test_set_refuses_without_a_terminal(self) -> None:
        super().test_set_refuses_without_a_terminal()


if __name__ == "__main__":
    unittest.main()
