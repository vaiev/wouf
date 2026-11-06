"""Tests for .agents/bin/guard: the pre-tool tripwire for the lead's session (exit 2 = block)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / ".agents" / "bin" / "guard"


class GuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / "project"
        self.secrets = Path(self.tmp.name) / "secret-store"
        self.team = Path(self.tmp.name) / "team"
        self.memory = Path(self.tmp.name) / "memory"
        (self.project / ".agents").mkdir(parents=True)
        self.team.mkdir()
        self.secrets.mkdir()
        (self.team / "config.ini").write_text(
            f"[guard]\nsecret_dirs = {self.secrets}\nwrite_roots = {self.memory}\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_guard(self, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
        env = {**os.environ, "AGENTS_PROJECT_DIR": str(self.project), "WOUF_TEAM_DIR": str(self.team)}
        return subprocess.run(
            [sys.executable, "-I", str(GUARD), *args],
            input=stdin, capture_output=True, text=True, env=env, cwd=self.project,
        )

    def assert_blocked(self, result: subprocess.CompletedProcess) -> None:
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("blocked", result.stderr)

    def test_harmless_commands_pass(self) -> None:
        for cmd in ("git status", "python3 -m unittest -v", "env | cut -d= -f1", "ls -la docs",
                    "env PYTHONPATH=src make test", "set -euo pipefail", "cat .env.example"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.run_guard("check-command", cmd).returncode, 0)

    def test_environment_dumps_are_blocked(self) -> None:
        for cmd in ("env", "printenv", "printenv TOKEN", "set", "export -p", "cat .env", "cat /proc/self/environ"):
            with self.subTest(cmd=cmd):
                self.assert_blocked(self.run_guard("check-command", cmd))

    def test_dump_words_in_data_pass(self) -> None:
        for cmd in ('grep -n "printenv\\|set)" bin/guard', "git commit -m 'drop env | set'",
                    "python3 - <<'EOF'\nimport os\nenv\nEOF", "cat > notes.md <<EOF\nset\nEOF"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.run_guard("check-command", cmd).returncode, 0)

    def test_dumps_run_by_a_shell_are_blocked(self) -> None:
        for cmd in ("bash -c 'env'", 'sh -c "printenv TOKEN"', "eval 'set'", "x=$(env)",
                    "bash <<'EOF'\nenv\nEOF", "ssh host <<EOF\nprintenv\nEOF", "echo ok; env"):
            with self.subTest(cmd=cmd):
                self.assert_blocked(self.run_guard("check-command", cmd))

    def test_secret_store_is_never_read_by_commands(self) -> None:
        self.assert_blocked(self.run_guard("check-command", f"cat {self.secrets}/api.key"))
        self.assert_blocked(self.run_guard("check-command", "cat ~/.ssh/id_ed25519"))

    def test_destructive_commands_are_blocked(self) -> None:
        for cmd in (
            "rm -rf /",
            "rm -rf ~",
            "tofu destroy",
            "terraform destroy -auto-approve",
            "git push --force origin main",
            "git push -f origin main",
            "psql -c 'DROP DATABASE app'",
        ):
            with self.subTest(cmd=cmd):
                self.assert_blocked(self.run_guard("check-command", cmd))

    def test_project_deny_list_extends_the_rules(self) -> None:
        (self.team / "guard-deny.txt").write_text(
            "# provider APIs only through the wrapper\ncurl\\s+https://api\\.example\\.test\n", encoding="utf-8"
        )
        self.assert_blocked(self.run_guard("check-command", "curl https://api.example.test/v1/zones"))
        self.assertEqual(self.run_guard("check-command", "curl https://docs.example.test/").returncode, 0)

    def test_paths(self) -> None:
        self.assertEqual(self.run_guard("check-path", str(self.project / "docs" / "STAND.md")).returncode, 0)
        self.assertEqual(self.run_guard("check-path", "docs/STAND.md").returncode, 0)
        self.assert_blocked(self.run_guard("check-path", "/etc/passwd"))
        self.assert_blocked(self.run_guard("check-path", "../outside.txt"))
        self.assert_blocked(self.run_guard("check-path", str(self.secrets / "api.key")))
        self.assert_blocked(self.run_guard("check-path", str(GUARD)))
        self.assert_blocked(self.run_guard("check-path", str(self.team / "guard-deny.txt")))
        self.assert_blocked(self.run_guard("check-path", ".env"))
        self.assertEqual(self.run_guard("check-path", str(self.memory / "note.md")).returncode, 0)
        self.assert_blocked(self.run_guard("check-path", str(self.memory) + "-other/note.md"))

    def test_hook_payload_claude_bash(self) -> None:
        payload = {"tool_name": "Bash", "tool_input": {"command": "printenv"}}
        self.assert_blocked(self.run_guard("hook", stdin=json.dumps(payload)))
        payload["tool_input"]["command"] = "git status"
        self.assertEqual(self.run_guard("hook", stdin=json.dumps(payload)).returncode, 0)

    def test_hook_payload_file_write(self) -> None:
        payload = {"tool_name": "Write", "tool_input": {"file_path": "/etc/hosts", "content": "x"}}
        self.assert_blocked(self.run_guard("hook", stdin=json.dumps(payload)))
        payload["tool_input"]["file_path"] = str(self.project / "README.md")
        self.assertEqual(self.run_guard("hook", stdin=json.dumps(payload)).returncode, 0)

    def test_hook_payload_patch_paths(self) -> None:
        patch = "*** Begin Patch\n*** Update File: src/app.py\n@@\n-a\n+b\n*** Add File: ../../escape.txt\n+x\n*** End Patch\n"
        payload = {"tool_name": "apply_patch", "tool_input": {"input": patch}}
        self.assert_blocked(self.run_guard("hook", stdin=json.dumps(payload)))

    def test_unreadable_hook_payload_fails_closed(self) -> None:
        self.assert_blocked(self.run_guard("hook", stdin="not json"))

    def test_unknown_tools_pass(self) -> None:
        payload = {"tool_name": "WebSearch", "tool_input": {"query": "release notes"}}
        self.assertEqual(self.run_guard("hook", stdin=json.dumps(payload)).returncode, 0)


if __name__ == "__main__":
    unittest.main()
