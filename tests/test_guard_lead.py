"""Tests for the lead mode of .agents/bin/guard: [guard] lead_mode = on refuses the file-tool paths (resolved against the
hook's cwd) outside lead_write and write_roots, and every shell line that does not match the allowlist of brief T-0188
v2, at the tool call (exit 2 = blocked). With lead_mode off the guard gives the result of its base rules, which every
off-mode assertion here compares with (a run with no lead keys in the config). Hook payloads carry the project as cwd,
as Claude Code's do, unless a test says otherwise."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import isolate_wouf_env  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / ".agents" / "bin" / "guard"
LEAD_WRITE = "team/briefs/**, team/board.md, team/knowledge/lead/**, docs/notes/**, docs/friction.md, outbox/**"
CODE = "src/a.py"
NOTE = "docs/notes/x.md"
CANNOT_TELL = "cannot tell where this writes"
NOT_LISTED = "not on the lead's list: brief a bot"
IDENTITY = ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "init.defaultBranch=main"]


def setUpModule() -> None:
    isolate_wouf_env()


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *IDENTITY, *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, "HOME": str(cwd)}).stdout


class LeadGuardCase(unittest.TestCase):
    """A temporary project whose team folder is team/ (WOUF_TEAM_DIR), with code, notes and a memory folder."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name).resolve()
        self.project = root / "project"
        self.team = self.project / "team"
        self.memory = root / "memory"
        self.secrets = root / "secret-store"
        self.home = root / "home"
        for folder in (self.project / ".agents" / "bin", self.team / "briefs", self.project / "docs" / "notes",
                       self.project / "src", self.project / "outbox", self.memory, self.secrets, self.home):
            folder.mkdir(parents=True, exist_ok=True)
        (self.project / CODE).write_text("a = 1\n", encoding="utf-8")
        (self.project / NOTE).write_text("x\n", encoding="utf-8")
        self.lead_write = LEAD_WRITE
        self.lead_run = "ops/*.sh"
        self.lead_env = "GH_TOKEN, WOUF_TEAM_DIR, SYNC_PACK_ON_LAND, PKG_MAIN, PATH"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_config(self, mode: str) -> None:
        lines = ["[guard]", f"secret_dirs = {self.secrets}", f"write_roots = {self.memory}"]
        if mode != "base":
            lines += [f"lead_mode = {mode}", f"lead_write = {self.lead_write}", f"lead_run = {self.lead_run}",
                      f"lead_env = {self.lead_env}"]
        (self.team / "config.ini").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def run_guard(self, mode: str, *args: str, stdin: str = "", guard: Path = GUARD) -> subprocess.CompletedProcess:
        """mode: on, off, or base (a config without the lead keys, as before lead mode existed)."""
        self.write_config(mode)
        dropped = ("AGENTS_PROJECT_DIR", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")
        env = {k: v for k, v in os.environ.items() if not k.startswith("WOUF_") and k not in dropped}
        env.update(AGENTS_PROJECT_DIR=str(self.project), WOUF_TEAM_DIR=str(self.team), HOME=str(self.home))
        return subprocess.run([sys.executable, "-I", str(guard), *args], input=stdin, capture_output=True, text=True,
                              env=env, cwd=self.project)

    def assert_lead_blocked(self, result: subprocess.CompletedProcess, *names: str) -> None:
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("brief a bot", result.stderr)
        for name in names:
            self.assertIn(name, result.stderr)

    def assert_allowed(self, result: subprocess.CompletedProcess) -> None:
        self.assertEqual(result.returncode, 0, result.stderr)

    def assert_off_is_base(self, *args: str, stdin: str = "") -> None:
        base = self.run_guard("base", *args, stdin=stdin)
        off = self.run_guard("off", *args, stdin=stdin)
        self.assertEqual((off.returncode, off.stderr), (base.returncode, base.stderr))

    def path_forms(self, path: str) -> list[tuple[str, tuple[str, ...], str]]:
        """check-path and the Write and Edit hook payloads for one path."""
        write = self.hook("Write", file_path=path, content="x")
        edit = self.hook("Edit", file_path=path, old_string="a", new_string="b")
        return [("check-path", ("check-path", path), ""), ("Write", ("hook",), write), ("Edit", ("hook",), edit)]

    def assert_paths(self, paths: list[str], off: int | None, blocked: bool) -> None:
        """off: the exit code with lead_mode off, None for "as base"; blocked: whether lead mode refuses."""
        for path in paths:
            for kind, args, stdin in self.path_forms(path):
                with self.subTest(path=path, kind=kind):
                    if off is None:
                        self.assert_off_is_base(*args, stdin=stdin)
                    else:
                        self.assertEqual(self.run_guard("off", *args, stdin=stdin).returncode, off)
                    result = self.run_guard("on", *args, stdin=stdin)
                    if blocked:
                        self.assert_lead_blocked(result, path)
                    else:
                        self.assert_allowed(result)

    def hook(self, tool: str, **tool_input) -> str:
        return json.dumps({"tool_name": tool, "tool_input": tool_input, "cwd": str(self.project)})


class PathTableTest(LeadGuardCase):
    """The table of the brief: guard check-path and guard hook, one test per row."""

    def test_row_brief_is_written(self) -> None:
        self.assert_paths(["team/briefs/T-1.md"], off=0, blocked=False)

    def test_row_note_is_written(self) -> None:
        self.assert_paths([NOTE], off=0, blocked=False)

    def test_row_code_and_tests_are_refused(self) -> None:
        self.assert_paths([".agents/bin/team", CODE, "scripts/run.sh", "tests/test_x.md", "docs/notes/tool.py",
                           "team/briefs/run.sh"], off=0, blocked=True)

    def test_row_nested_repository_is_refused(self) -> None:
        nested = self.project / "ref" / "x"
        (nested / "src").mkdir(parents=True)
        git(nested, "init", "-q")
        self.assert_paths(["ref/x/src/a.py"], off=0, blocked=True)

    def test_row_guard_settings_and_pack_are_refused(self) -> None:
        self.assert_paths(["team/config.ini", ".claude/settings.json", ".claude/settings.local.json",
                           ".agents/rules.md", ".agents/team/board.md"], off=None, blocked=True)

    def test_row_dot_dot_outside_lead_write_is_refused(self) -> None:
        self.assert_paths(["docs/notes/../../src/a.py", "docs/notes/../../../outside.md"], off=None, blocked=True)

    def test_row_symlink_to_code_is_resolved_first(self) -> None:
        (self.project / "docs" / "notes" / "link.md").symlink_to(self.project / CODE)
        self.assert_paths(["docs/notes/link.md"], off=None, blocked=True)
        result = self.run_guard("on", "check-path", "docs/notes/link.md")
        self.assertIn("src/a.py", result.stderr)

    def test_row_notebook_and_multi_edit_on_code(self) -> None:
        for stdin in (self.hook("NotebookEdit", notebook_path="src/a.ipynb", new_source="x"),
                      self.hook("MultiEdit", file_path=CODE, edits=[{"old_string": "a", "new_string": "b"}])):
            with self.subTest(stdin=stdin):
                self.assertEqual(self.run_guard("off", "hook", stdin=stdin).returncode, 0)
                self.assert_lead_blocked(self.run_guard("on", "hook", stdin=stdin), "src/a.")

    def test_row_unknown_tool_with_a_path(self) -> None:
        for key in ("file_path", "path"):
            with self.subTest(key=key):
                stdin = self.hook("FancyWrite", **{key: CODE, "content": "x"})
                self.assertEqual(self.run_guard("off", "hook", stdin=stdin).returncode, 0)
                self.assert_lead_blocked(self.run_guard("on", "hook", stdin=stdin), CODE)
                self.assert_allowed(self.run_guard("on", "hook", stdin=self.hook("FancyWrite", **{key: NOTE})))


class HookCwdTest(LeadGuardCase):
    """The table of brief T-0188-fix1, one test per row: a file tool's relative path is resolved against the hook's
    cwd; with no cwd only an absolute path can be told, and lead_write is docs/notes/**. The shell rows run a Bash hook
    for each row that gives a cwd."""

    def setUp(self) -> None:
        super().setUp()
        self.lead_write = "docs/notes/**"
        self.outside = Path(self.tmp.name).resolve() / "outside"
        for folder in (self.project / "ref" / "x", self.outside):
            folder.mkdir(parents=True)

    def payload(self, tool: str, cwd: Path | None, **tool_input) -> str:
        payload = {"tool_name": tool, "tool_input": tool_input}
        if cwd is not None:
            payload["cwd"] = str(cwd)
        return json.dumps(payload)

    def tool_payloads(self, cwd: Path | None, path: str) -> dict:
        """Every file tool that writes path: Write, Edit, MultiEdit, NotebookEdit, unknown tools, a patch."""
        patch = f"*** Begin Patch\n*** Add File: {path}\n+x\n*** End Patch\n"
        return {"Write": self.payload("Write", cwd, file_path=path, content="x"),
                "Edit": self.payload("Edit", cwd, file_path=path, old_string="a", new_string="b"),
                "MultiEdit": self.payload("MultiEdit", cwd, file_path=path, edits=[{"old_string": "a",
                                                                                     "new_string": "b"}]),
                "NotebookEdit": self.payload("NotebookEdit", cwd, notebook_path=path, new_source="x"),
                "unknown_file_path": self.payload("FancyWrite", cwd, file_path=path),
                "unknown_path": self.payload("FancyWrite", cwd, path=path),
                "apply_patch": self.payload("apply_patch", cwd, input=patch)}

    def assert_row(self, cwd: Path | None, path: str, code: int, *names: str) -> None:
        payloads = self.tool_payloads(cwd, path)
        if cwd is not None:
            payloads["Bash"] = self.payload("Bash", cwd, command=f"echo x > {path}")
        for tool, stdin in payloads.items():
            with self.subTest(tool=tool):
                result = self.run_guard("on", "hook", stdin=stdin)
                if code:
                    self.assert_lead_blocked(result, path, *names)
                else:
                    self.assert_allowed(result)
                self.assert_off_is_base("hook", stdin=stdin)

    def test_row_cwd_project_note_is_written(self) -> None:
        self.assert_row(self.project, NOTE, 0)

    def test_row_cwd_nested_folder_is_outside_lead_write(self) -> None:
        self.assert_row(self.project / "ref" / "x", NOTE, 2, "resolves to ref/x/docs/notes/x.md")

    def test_row_cwd_docs_note_is_written(self) -> None:
        self.assert_row(self.project / "docs", "notes/x.md", 0)

    def test_row_cwd_docs_dot_dot_to_code_is_refused(self) -> None:
        self.assert_row(self.project / "docs", "../src/a.py", 2, "resolves to src/a.py")

    def test_row_no_cwd_relative_path_cannot_tell(self) -> None:
        self.assert_row(None, NOTE, 2, CANNOT_TELL)

    def test_row_no_cwd_absolute_note_is_written(self) -> None:
        self.assert_row(None, str(self.project / NOTE), 0)

    def test_row_cwd_outside_the_project_is_refused(self) -> None:
        self.assert_row(self.outside, "x.md", 2, f"resolves to {self.outside / 'x.md'}")

    def test_a_tilde_that_does_not_expand_cannot_tell(self) -> None:
        for tool, stdin in self.tool_payloads(self.project, "~no-such-user-x/docs/notes/x.md").items():
            with self.subTest(tool=tool):
                self.assert_lead_blocked(self.run_guard("on", "hook", stdin=stdin), CANNOT_TELL)


class PathEdgesTest(LeadGuardCase):
    def test_write_roots_stay_writable(self) -> None:
        self.assert_allowed(self.run_guard("on", "check-path", str(self.memory / "note.md")))
        self.assert_allowed(self.run_guard("on", "check-command", f"echo x > {self.memory}/note.md"))

    def test_mv_removes_its_source(self) -> None:
        self.assert_lead_blocked(self.run_guard("on", "check-command", "mv src/a.py docs/notes/y.md"), CODE)
        self.assert_allowed(self.run_guard("on", "check-command", "mv docs/notes/x.md docs/notes/y.md"))

    def test_reading_tools_still_read_code(self) -> None:
        for stdin in (self.hook("Read", file_path=CODE), self.hook("Grep", pattern="a", path="src"),
                      self.hook("Glob", pattern="*.py", path="src")):
            with self.subTest(stdin=stdin):
                self.assert_allowed(self.run_guard("on", "hook", stdin=stdin))
        secret = self.hook("Read", file_path=str(self.secrets / "api.key"))
        self.assert_off_is_base("hook", stdin=secret)
        self.assertEqual(self.run_guard("on", "hook", stdin=secret).returncode, 2)

    def test_patch_tool_paths_are_checked(self) -> None:
        patch = "*** Begin Patch\n*** Update File: docs/notes/x.md\n@@\n-x\n+y\n*** Update File: src/a.py\n@@\n-a\n+b\n"
        self.assert_lead_blocked(self.run_guard("on", "hook", stdin=self.hook("apply_patch", input=patch)), CODE)
        moved = "*** Begin Patch\n*** Update File: docs/notes/x.md\n*** Move to: src/b.py\n@@\n-x\n+y\n*** End Patch\n"
        self.assert_lead_blocked(self.run_guard("on", "hook", stdin=self.hook("apply_patch", input=moved)), "src/b.py")

    def test_bash_hook_follows_its_cwd_and_codex_argv(self) -> None:
        payload = {"tool_name": "Bash", "tool_input": {"command": "echo x > a.py"}, "cwd": str(self.project / "src")}
        self.assert_lead_blocked(self.run_guard("on", "hook", stdin=json.dumps(payload)), "a.py")
        payload = {"tool_name": "Bash", "tool_input": {"command": "echo y > x.md"},
                   "cwd": str(self.project / "docs" / "notes")}
        self.assert_allowed(self.run_guard("on", "hook", stdin=json.dumps(payload)))
        codex = self.hook("shell", command=["bash", "-lc", "echo x > src/a.py"])
        self.assert_lead_blocked(self.run_guard("on", "hook", stdin=codex), CODE)
        git(self.project, "init", "-q")
        # Review T-0188.r5: a shell's -c in an argv is not unwrapped; it cannot tell, like bash -c in a line.
        status = self.hook("shell", command=["bash", "-lc", "git status"])
        self.assert_lead_blocked(self.run_guard("on", "hook", stdin=status), CANNOT_TELL)
        self.assert_allowed(self.run_guard("on", "hook", stdin=self.hook("shell", command=["git", "status"])))

    def test_cd_is_followed(self) -> None:
        self.assert_lead_blocked(self.run_guard("on", "check-command", "cd ./src && echo x > a.py"), "a.py")
        self.assert_allowed(self.run_guard("on", "check-command", "cd ./docs/notes && echo x > x.md"))
        self.assert_lead_blocked(self.run_guard("on", "check-command", "(cd ./docs/notes); echo x > x.md"), "x.md")
        self.assert_allowed(self.run_guard("on", "check-command", "cd ./docs/notes; echo x > x.md"))

    def test_base_rules_still_apply_in_lead_mode(self) -> None:
        for cmd in ("printenv", "rm -rf ~", "git push -f origin main", f"cat {self.secrets}/key"):
            with self.subTest(cmd=cmd):
                result = self.run_guard("on", "check-command", cmd)
                self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(self.run_guard("on", "check-path", "docs/notes/.env").returncode, 2)

    def test_a_missing_lead_module_fails_closed_and_off_never_loads_it(self) -> None:
        bin_dir = Path(self.tmp.name) / "pack" / ".agents" / "bin"
        bin_dir.mkdir(parents=True)
        lone = Path(shutil.copy(GUARD, bin_dir / "guard"))
        on = self.run_guard("on", "check-path", NOTE, guard=lone)
        self.assertEqual(on.returncode, 2, on.stderr)
        self.assertIn(CANNOT_TELL, on.stderr)
        self.assertEqual(self.run_guard("off", "check-path", NOTE, guard=lone).returncode, 0)


class SwitchOffTest(LeadGuardCase):
    """The guard cannot be switched off from inside the session, even with a lead_write that covers its files."""

    def test_guard_files_and_cli_settings_stay_refused(self) -> None:
        self.lead_write = "team/**, .agents/**, .claude/**, ~/.claude/**, ~/.codex/**"
        for path in ("team/config.ini", "team/guard-deny.txt", ".agents/bin/guard", ".agents/bin/guardlib_lead.py",
                     ".claude/settings.json", ".claude/settings.local.json",
                     str(self.home / ".claude" / "settings.json"), str(self.home / ".codex" / "config.toml"),
                     str(GUARD)):
            with self.subTest(path=path):
                self.assert_lead_blocked(self.run_guard("on", "check-path", path))
        for cmd in ("echo x > team/config.ini", "echo x >> team/guard-deny.txt", "sed -i s/on/off/ team/config.ini",
                    "cp x .agents/bin/guard", "echo '{}' > .claude/settings.json"):
            with self.subTest(cmd=cmd):
                self.assert_lead_blocked(self.run_guard("on", "check-command", cmd))
        self.assert_allowed(self.run_guard("on", "check-path", "team/briefs/T-1.md"))


class WrappedFormsTest(LeadGuardCase):
    """A hard link in lead_write to code is refused like the code file."""

    def test_hard_link_in_lead_write_is_refused(self) -> None:
        os.link(self.project / CODE, self.project / "docs" / "notes" / "hard.md")
        self.assert_paths(["docs/notes/hard.md"], off=None, blocked=True)
        self.assert_lead_blocked(self.run_guard("on", "check-command", "echo x > docs/notes/hard.md"), "hard link")


# The table of brief T-0188 v2: per row an allowed example and a refused one (a wrong option or a wrong path), run in
# a project that is its own git repository, with ops/sync.sh as the lead_run script.
ROWS = {
    "readers": ("cat src/a.py | head -3 | tail -1 | wc -l; ls -la src; cut -c1 src/a.py | tr a b; "
                "diff src/a.py docs/notes/x.md; basename src/a.py; dirname src/a.py; realpath src; pwd; date +%F; "
                "echo x; printf '%s\\n' x; true", "date -s 10:00"),
    "grep": ("grep -rn --include='*.py' a src", "grep -n a src/a.py > src/hits.md"),
    "sed": ("sed -n '1,5p;/a/=' src/a.py", "sed -n '1w src/b.md' src/a.py"),
    "sort_uniq": ("sort -u -k1 src/a.py | uniq -c", "sort -o src/a.py src/a.py"),
    "find": ("find src docs -maxdepth 2 -name '*.py' -type f -print", "find src -name '*.py' -delete"),
    "cd": ("cd ./docs/notes && echo x > x.md", "cd / && ls"),
    "mkdir": ("mkdir -p docs/notes/a/b", "mkdir -p src/new"),
    "cp_mv": ("cp src/a.py docs/notes/a.md && mv docs/notes/x.md docs/notes/y.md", "cp docs/notes/x.md src/x.md"),
    "git_read": ("git status && git log --oneline -3 && git diff --stat && git branch --list && git remote -v && "
                 "git -C docs worktree list && git cat-file -p HEAD", "git diff --output=src/a.py"),
    "git_add": ("git add team/briefs/T-1.md", "git add src/a.py"),
    "gh": ("gh pr view 12 && gh pr checks 12 && gh run list", "gh pr merge 12"),
    "pack_tools": (".agents/bin/team context; .agents/bin/log task T-1; .agents/bin/federate status",
                   "cd ./docs/notes && .agents/bin/team context"),
    "lead_run": ("bash ops/sync.sh --dry-run; . ops/sync.sh; source ops/sync.sh", "bash src/run.sh"),
    "lead_env": ("GH_TOKEN=$(pass show gh/token) gh pr list; export PKG_MAIN=main", "FOO=1 gh pr list"),
    "redirect": ("echo x > docs/notes/x.md 2>&1; echo y >> outbox/m.md 2>/dev/null; echo z > /dev/null; "
                 "cat < src/a.py", "echo x > src/a.py"),
}
# The argv a Codex CLI shell hook carries for each row of ROWS: (allowed argvs, a refused argv). An argv has no
# shell: no operator, redirection or expansion, so the redirect rule has no allowed argv; its refused one is Codex's
# own wrapping of the refused line, which cannot tell (review T-0188.r5).
ROW_ARGVS = {
    "readers": ([["cat", CODE], ["head", "-3", CODE], ["tail", "-1", CODE], ["wc", "-l", CODE], ["ls", "-la", "src"],
                 ["cut", "-c1", CODE], ["tr", "a", "b"], ["diff", CODE, NOTE], ["basename", CODE], ["dirname", CODE],
                 ["realpath", "src"], ["pwd"], ["date", "+%F"], ["echo", "x"], ["printf", "%s\\n", "x"], ["true"]],
                ["date", "-s", "10:00"]),
    "grep": ([["grep", "-rn", "--include=*.py", "a", "src"]], ["grep", "-P", "a", "src"]),
    "sed": ([["sed", "-n", "1,5p;/a/=", CODE]], ["sed", "-n", "1w src/b.md", CODE]),
    "sort_uniq": ([["sort", "-u", "-k1", CODE], ["uniq", "-c", CODE]], ["sort", "-o", CODE, CODE]),
    "find": ([["find", "src", "docs", "-maxdepth", "2", "-name", "*.py", "-type", "f", "-print"]],
             ["find", "src", "-name", "*.py", "-delete"]),
    "cd": ([["cd", "./docs/notes"]], ["cd", "/"]),
    "mkdir": ([["mkdir", "-p", "docs/notes/a/b"]], ["mkdir", "-p", "src/new"]),
    "cp_mv": ([["cp", CODE, "docs/notes/a.md"], ["mv", NOTE, "docs/notes/y.md"]], ["cp", NOTE, "src/x.md"]),
    "git_read": ([["git", "status"], ["git", "log", "--oneline", "-3"], ["git", "diff", "--stat"],
                  ["git", "branch", "--list"], ["git", "remote", "-v"], ["git", "-C", "docs", "worktree", "list"],
                  ["git", "cat-file", "-p", "HEAD"]], ["git", "diff", "--output=src/a.py"]),
    "git_add": ([["git", "add", "team/briefs/T-1.md"]], ["git", "add", CODE]),
    "gh": ([["gh", "pr", "view", "12"], ["gh", "pr", "checks", "12"], ["gh", "run", "list"]],
           ["gh", "pr", "merge", "12"]),
    "pack_tools": ([[".agents/bin/team", "context"], [".agents/bin/log", "task", "T-1"],
                    [".agents/bin/federate", "status"]], [".agents/bin/project", "status"]),
    "lead_run": ([["bash", "ops/sync.sh", "--dry-run"], [".", "ops/sync.sh"], ["source", "ops/sync.sh"]],
                 ["bash", "src/run.sh"]),
    "lead_env": ([["export", "PKG_MAIN=main"], ["PKG_MAIN=main", "gh", "pr", "list"]], ["FOO=1", "gh", "pr", "list"]),
    "redirect": ([], ["bash", "-lc", "echo x > src/a.py"]),
}
SHELL_SYNTAX = re.compile(r"[\n;&|()<>$`{}*?\[\]~]")


def argv_form(cmd: str) -> list:
    """cmd as a Codex CLI shell hook carries it: its words when it is one simple command without shell syntax, else
    Codex's own wrapping, ["bash", "-lc", cmd], which cannot tell (review T-0188.r5)."""
    return ["bash", "-lc", cmd] if SHELL_SYNTAX.search(cmd) else shlex.split(cmd)


# Constructs the allowlist does not parse: "cannot tell where this writes".
CANNOT_TELL_FORMS = {
    "eval": 'eval "$CMD"',
    "bash_c": "bash -c 'echo x'",
    "sh_c": "sh -c 'echo x'",
    "substitution": "echo $(date)",
    "backticks": "echo `date`",
    "process_substitution": "diff <(cat src/a.py) docs/notes/x.md",
    "for": "for f in a; do echo $f; done",
    "while": "while true; do echo x; done",
    "if": "if true; then echo x; fi",
    "function": "f() { echo x; }",
    "coproc": "coproc cat",
    "heredoc_to_unlisted": "python3 <<EOF\nprint(1)\nEOF",
    "xargs": "echo x | xargs echo",
    "exec": "exec cat",
    "arithmetic": "echo $((PATH=0))",
    "brace_assignment": "echo ${X:=y}",
}
# Commands and options that are not on the list: "not on the lead's list: brief a bot".
NOT_LISTED_FORMS = {
    "unknown_command": "touch docs/notes/x.md",
    "unknown_option": "cp -p docs/notes/x.md docs/notes/y.md",
    "git_push": "git push origin main",
    "sort_compress_program": "sort --compress-program=sh docs/notes/x.md",
    "printf_v": "printf -v PATH %s docs/notes",
    # Review T-0188-fix3.r2: these passed with exit 0.
    "review_cat_unknown_option": "cat --not-a-real-option docs/notes/x.md",
    "review_git_status_unknown_option": "git status --not-a-real-option",
    "review_gh_pr_view_web": "gh pr view --web",
    # Review of T-0188-fix4: the unlisted word passed as the value of the option before it.
    "review_gh_json_value_unknown_option": "gh pr view --json --not-a-real-option",
    "review_grep_e_value_unknown_option": "grep -e --not-a-real-option src",
    # Review T-0188-fix4.r2: a word that starts with -- passed after the -- separator.
    "review_git_log_after_separator": "git log -- --not-a-real-option",
    "review_git_diff_output_after_separator": "git diff -- --output=src/a.py",
    "sed_after_separator": "sed -n p -- --not-a-real-option",
    "find_test_value": "find . -name --not-a-real-option",
    "git_commit_message": "git commit -q -m --not-a-real-option",
    "git_c_dir": "git -C --not-a-real-option status",
}
# The option table of brief T-0188-fix4, one entry per row: commands with the listed options (each allowed) and
# commands with an unknown or unlisted option (each refused: "not on the lead's list"). The word after an option that
# takes a value is its value: one - in front is allowed (grep -e -needle), -- in front refused (T-0188-fix5).
UNKNOWN = "--not-a-real-option"
NO_OPTION_COMMANDS = ("echo", "printf", "true", "pwd", "date", "basename", "dirname", "realpath")
OPTION_ROWS = {
    "cat": (["cat -n -A -v src/a.py"], [f"cat {UNKNOWN} src/a.py", "cat -e src/a.py"]),
    "head_tail": (["head -n 3 src/a.py", "head -c5 src/a.py", "head -3 src/a.py", "tail -n +2 src/a.py",
                   "tail -c 5 src/a.py", "tail -1 src/a.py", "head -n -1 src/a.py"],
                  [f"head {UNKNOWN} src/a.py", f"tail {UNKNOWN} src/a.py", "tail -f src/a.py", "head -n x src/a.py"]),
    "wc": (["wc -l -w -c -m src/a.py"], [f"wc {UNKNOWN} src/a.py", "wc -L src/a.py"]),
    "ls": (["ls -l -a -A -d -h -t -r -1 -R src", "ls -laR src", "ls -1tr"], [f"ls {UNKNOWN} src", "ls -lS src"]),
    "cut": (["cut -d , -f 1 src/a.py", "cut -d, -f1 src/a.py", "cut -c 1-2 src/a.py", "cut -d - -f 2 src/a.py",
             "cut -f -z src/a.py"],
            [f"cut {UNKNOWN} -f1 src/a.py", "cut --output-delimiter=x -f1 src/a.py", f"cut -d {UNKNOWN} src/a.py",
             f"cut -f {UNKNOWN} src/a.py"]),
    "tr": (["tr -d a", "tr -s ' '"], [f"tr {UNKNOWN} a", "tr -c a b"]),
    "diff": (["diff -u -r -q --brief -N src/a.py docs/notes/x.md", "diff -U 3 src/a.py docs/notes/x.md",
              "diff -U3 src/a.py docs/notes/x.md"],
             [f"diff {UNKNOWN} src/a.py docs/notes/x.md", "diff -y src/a.py docs/notes/x.md"]),
    "grep": (["grep -n -c -l -L -i -v -E -F -r -R -h -H -o -w -x a src", "grep -nclLivEFrRhHowx a src",
              "grep -A 1 -B 1 -C 1 -m 2 -e a src/a.py", "grep -A1 -B1 -C1 -m2 -ea src/a.py",
              "grep -rn --include='*.py' --exclude='*.md' a src", "grep -e -needle src/a.py", "grep -e -Z src"],
             [f"grep {UNKNOWN} a src", "grep -P a src", "grep -f docs/notes/x.md src", f"grep -e {UNKNOWN} src",
              f"grep -e{UNKNOWN} src", f"grep --include={UNKNOWN} a src", f"grep -m {UNKNOWN} a src"]),
    "sort": (["sort -n -r -u -k 1 -t , src/a.py", "sort -k1 -t, src/a.py", "sort -t -o src/a.py"],
             [f"sort {UNKNOWN} src/a.py", "sort -z src/a.py", f"sort -k {UNKNOWN} src/a.py",
              f"sort -t {UNKNOWN} src/a.py"]),
    "uniq": (["uniq -c -d -u src/a.py"], [f"uniq {UNKNOWN} src/a.py", "uniq -i src/a.py"]),
    "no_options": (["echo x", "printf '%s\\n' x", "true", "pwd", "date +%F", "basename src/a.py", "dirname src/a.py",
                    "realpath src"],
                   [f"{name} {UNKNOWN}" for name in NO_OPTION_COMMANDS] + ["echo -n x", "pwd -P", "date -u",
                                                                          "date 0101"]),
    "git_status": (["git status -s --short -b --porcelain"], [f"git status {UNKNOWN}", "git status -uno"]),
    "git_log": (["git log --oneline -3 -n 3 -n3 --format=%h --pretty=%s --stat --name-only --graph --all -p "
                 "HEAD~1..HEAD -- docs", "git log -- docs/notes/x.md"],
                [f"git log {UNKNOWN}", "git log --output=src/a.py", f"git log -- {UNKNOWN}"]),
    "git_show": (["git show --stat --name-only --format=%h -s HEAD", "git show HEAD:docs/notes/x.md"],
                 [f"git show {UNKNOWN} HEAD", "git show --textconv HEAD:src/a.py"]),
    "git_diff": (["git diff --stat --name-only --name-status --cached -U 3 HEAD -- docs", "git diff -U1 HEAD~1 HEAD"],
                 [f"git diff {UNKNOWN}", "git diff --output=src/a.py", "git diff --ext-diff", "git diff --textconv",
                  "git diff -- --output=src/a.py"]),
    "git_revisions": (["git rev-parse --verify -q --short HEAD", "git rev-parse --abbrev-ref HEAD",
                       "git merge-base --is-ancestor HEAD HEAD", "git rev-list --count HEAD"],
                      [f"git rev-parse {UNKNOWN}", f"git merge-base {UNKNOWN} HEAD HEAD",
                       f"git rev-list {UNKNOWN} HEAD", "git rev-parse --git-path hooks"]),
    "git_shown": (["git ls-files", "git for-each-ref", "git cat-file -p HEAD", "git worktree list",
                   "git branch --list -v -a", "git remote -v"],
                  [f"git ls-files {UNKNOWN}", f"git for-each-ref {UNKNOWN}", f"git cat-file -p {UNKNOWN}",
                   f"git worktree list {UNKNOWN}", f"git branch {UNKNOWN}", f"git remote {UNKNOWN}",
                   "git ls-files --others", "git cat-file -t HEAD", "git branch topic"]),
    "gh": (["gh pr view 12 -R o/r --json title --jq .title", "gh pr list --state open -L 5 --limit 5",
            "gh pr checks 12", "gh run list --commit abc123 -L3", "gh run view 99 --log-failed", "gh pr list --jq -w"],
           ["gh pr view --web", f"gh pr view 12 {UNKNOWN}", f"gh pr list {UNKNOWN}", f"gh pr checks 12 {UNKNOWN}",
            f"gh run list {UNKNOWN}", f"gh run view 99 {UNKNOWN}", "gh run view 99 --web",
            f"gh pr view --json {UNKNOWN}", "gh pr view 12 --json --web", "gh pr view 12 --json=--web",
            "gh pr view 12 -R --web", f"gh pr list --jq {UNKNOWN}",
            f"gh pr list --state {UNKNOWN}", f"gh run list --commit {UNKNOWN}", f"gh pr list -L {UNKNOWN}"]),
}
# Every bypass of the earlier reviews (T-0188 r1 to fix2) and of this round: refused in lead mode.
BYPASSES = {
    "rm_r": "rm -r src",
    "cp_r": "cp -r docs/notes src/to",
    "rsync": "rsync -r docs/notes/ src/",
    "tar_x": "tar -xf a.tar -C src",
    "unzip": "unzip a.zip -d src",
    "git_checkout": "git checkout -- src/a.py",
    "git_checkout_revision": "git checkout HEAD~1 -- .",
    "git_restore": "git restore --source HEAD~1 src",
    "git_reset": "git reset --hard",
    "git_stash": "git stash pop",
    "git_clean": "git clean -fd src",
    "git_rm": "git rm -r src",
    "git_apply": "git apply change.patch",
    "git_merge": "git merge topic",
    "git_add_chmod": "git add --chmod=+x src/a.py",
    "git_add_all": "git add -A",
    "git_add_dir": "git add docs/notes",
    "git_add_glob": "git add 'docs/notes/*'",
    "git_c_repo_add_option": "git -C docs/notes add --renormalize x.md",
    "git_commit_pathspec": "git commit -m x -- src/a.py",
    "git_commit_include": "git commit -i -m x src/a.py",
    "git_commit_all": "git commit -am x",
    "git_config": "git -c core.hooksPath=docs/notes status",
    "python_open": "python3 -c \"open('src/a.py','w')\"",
    "dd": "dd if=docs/notes/x.md of=src/a.py",
    "install": "install docs/notes/x.md src/a.py",
    "truncate": "truncate -s0 src/a.py",
    "perl_pi": "perl -pi -e 's/a/b/' src/a.py",
    "sed_i": "sed -i 's/a/b/' src/a.py",
    "sed_i_after_operands": "sed -n 1p -i src/a.py",
    "tee_to_code": "echo x | tee src/a.py",
    "heredoc_to_code": "cat > src/a.py <<EOF\nx\nEOF",
    "group_redirect_to_code": "{ echo x; } > src/a.py",
    "function_body": "function f { cp x src/a.py; }; f",
    "trap": "trap 'cp x src/a.py' EXIT",
    "alias": "alias c='cp x src/a.py'\nc",
    "find_exec": "find . -name '*.py' -exec rm {} +",
    "xargs_rm": "echo src/a.py | xargs rm",
    "substitution_target": "echo x > $(echo src/a.py)",
    "ln": "ln -s docs/notes/x.md src/x.md",
    "chmod_r": "chmod -R 0755 src",
    "mv_into_folder": "mv docs/notes/x.md src",
    "uniq_output": "uniq docs/notes/x.md src/a.py",
    "arithmetic_path": "echo $[PATH=0]",
    "prompt_expansion": "echo ${X@P}",
    "cd_glob": "cd ./docs/notes/* && echo x > a.md",
    "pack_tool_elsewhere": "cd ./docs/notes && .agents/bin/team context",
    "lead_env_path": "export PATH=docs/notes",
    "pip": "pip install x",
    "docker": "docker run -v .:/w alpine true",
    "dup_inherited_fd3": "echo x >&3",
    "dup_inherited_fd3_stderr": "echo x 2>&3",
    "dup_stdout_to_stderr_fd": "echo x 1>&2",
    "dup_to_note": "echo x >& docs/notes/x.md",
    "clobber_note": "echo x >| docs/notes/x.md",
    "read_write_note": "cat <> docs/notes/x.md",
    "ampersand_note": "echo x &> docs/notes/x.md",
    "ampersand_append_note": "echo x &>> docs/notes/x.md",
    "read_dup_inherited_fd3": "cat <&3",
}
# The redirect descriptors of brief T-0188-fix6: > and >> with no descriptor or 1 (to /dev/null, lead_write or a
# write_root, {root} below), 2>&1, 2>/dev/null and < are allowed; every other descriptor form refuses the line.
REDIRECT_ALLOWED = [
    "echo hi >docs/notes/x.md", "echo hi 1>docs/notes/x.md", "echo hi >>docs/notes/x.md",
    "echo hi 1>>docs/notes/x.md", "echo hi >/dev/null", "echo hi 1>/dev/null", "echo hi >>/dev/null",
    "echo hi 1>>/dev/null", "echo hi >{root}/m.md", "echo hi 1>>{root}/m.md", "echo hi >>docs/notes/x.md 2>&1",
    "cat x 2>/dev/null", "cat < src/a.py", "cat <src/a.py 2>&1 1>/dev/null"]
REDIRECT_REFUSED = {
    "fd2_to_note": "echo hi 2>docs/notes/x.md",
    "fd2_to_code": "echo hi 2>src/a.py",
    "fd2_append_null": "echo hi 2>>/dev/null",
    "fd2_append_note": "echo hi 2>>docs/notes/x.md",
    "fd3_to_note": "echo hi 3>docs/notes/x.md",
    "fd3_append_note": "echo hi 3>>docs/notes/x.md",
    "fd9_to_null": "echo hi 9>/dev/null",
    "fd10_to_note": "echo hi 10>docs/notes/x.md",
    "fd01_to_note": "echo hi 01>docs/notes/x.md",
    "named_fd_to_note": "echo hi {fd}>docs/notes/x.md",
    "dup_stdout_to_fd2": "echo hi >&2",
    "dup_fd1_to_fd2": "echo hi 1>&2",
    "dup_fd2_to_fd3": "echo hi 2>&3",
    "dup_fd3_to_fd1": "echo hi 3>&1",
    "close_fd2": "echo hi 2>&-",
    "clobber_note": "echo hi >|docs/notes/x.md",
    "clobber_fd1_note": "echo hi 1>|docs/notes/x.md",
    "read_write_note": "cat <>docs/notes/x.md",
    "read_write_fd3_note": "cat 3<>docs/notes/x.md",
    "ampersand_note": "echo hi &>docs/notes/x.md",
    "ampersand_null": "echo hi &>/dev/null",
    "ampersand_append_note": "echo hi &>>docs/notes/x.md",
    "read_fd3": "cat 3<src/a.py",
    "read_dup_fd0": "cat 0<&3",
    "heredoc_fd3": "cat 3<<EOF\nx\nEOF",
}


class ShapeCase(LeadGuardCase):
    """The project is a git repository; ops/sync.sh is the lead_run script, team/briefs/T-1.md a brief to commit."""

    def setUp(self) -> None:
        super().setUp()
        for name in ("ops/sync.sh", "src/run.sh", "team/briefs/T-1.md"):
            (self.project / name).parent.mkdir(parents=True, exist_ok=True)
            (self.project / name).write_text("true\n", encoding="utf-8")
        git(self.project, "init", "-q")

    def shell_forms(self, cmd: str, argv: list) -> list:
        """The three ways a shell command reaches the guard: guard check-command, Claude Code's Bash hook (a string)
        and Codex CLI's shell hook (an argv)."""
        return [("check-command", ("check-command", cmd), ""),
                ("hook-string", ("hook",), self.hook("Bash", command=cmd)),
                ("hook-argv", ("hook",), self.hook("shell", command=argv))]

    def assert_refused(self, cmd: str, *names: str) -> None:
        """cmd is refused in all three forms (its argv form: argv_form), naming names (CANNOT_TELL for an argv that
        wraps it in bash -lc); with lead_mode off, the base result in each."""
        argv = argv_form(cmd)
        for form, args, stdin in self.shell_forms(cmd, argv):
            expected = (CANNOT_TELL,) if form == "hook-argv" and argv[:2] == ["bash", "-lc"] else names
            with self.subTest(form=form):
                self.assert_lead_blocked(self.run_guard("on", *args, stdin=stdin), *expected)
                self.assert_off_is_base(*args, stdin=stdin)

    def assert_argv_refused(self, argv: list, *names: str) -> None:
        stdin = self.hook("shell", command=argv)
        with self.subTest(argv=argv):
            self.assert_lead_blocked(self.run_guard("on", "hook", stdin=stdin), *names)
            self.assert_off_is_base("hook", stdin=stdin)

    def assert_allowed_forms(self, cmd: str, argvs: list) -> None:
        """cmd passes through check-command and the Bash hook, and each argv through the shell hook; with lead_mode
        off, the base result."""
        forms = self.shell_forms(cmd, [])[:2] + [(f"hook-argv {argv}", ("hook",), self.hook("shell", command=argv))
                                                  for argv in argvs]
        for form, args, stdin in forms:
            with self.subTest(form=form):
                self.assert_allowed(self.run_guard("on", *args, stdin=stdin))
                self.assert_off_is_base(*args, stdin=stdin)


class ShapeRowTest(ShapeCase):
    """One test per row of the table: its allowed example passes, its refused example is blocked, the base result
    when off; git commit is checked against what is staged."""

    def test_row_git_commit_allowed(self) -> None:
        git(self.project, "add", "team/briefs/T-1.md")
        self.assert_allowed_forms("git commit -q -m brief", [["git", "commit", "-q", "-m", "brief"]])

    def test_row_git_commit_refused(self) -> None:
        git(self.project, "add", "team/briefs/T-1.md", CODE)
        self.assert_refused("git commit -q -m brief", CODE)
        self.assert_refused("git commit -q -m brief --amend", NOT_LISTED)

    def test_row_git_commit_refused_when_git_cannot_list(self) -> None:
        shutil.rmtree(self.project / ".git")
        self.assert_refused("git commit -m brief", CANNOT_TELL)


def _row_tests(name: str, allowed: str, refused: str) -> None:
    def ok(self: ShapeCase) -> None:
        if name == "git_read":
            git(self.project, "add", "team/briefs/T-1.md")
            git(self.project, "commit", "-q", "-m", "start")
        self.assert_allowed_forms(allowed, ROW_ARGVS[name][0])

    def no(self: ShapeCase) -> None:
        self.assert_refused(refused)
        self.assert_argv_refused(ROW_ARGVS[name][1])

    setattr(ShapeRowTest, f"test_row_{name}_allowed", ok)
    setattr(ShapeRowTest, f"test_row_{name}_refused", no)


for _name, (_ok, _no) in ROWS.items():
    _row_tests(_name, _ok, _no)


class FormsTest(ShapeCase):
    """The constructs it cannot parse, the commands and options not on the list, and every earlier bypass."""

    def test_git_in_a_repository_whose_git_folder_lead_write_covers(self) -> None:
        git(self.project / "docs" / "notes", "init", "-q")
        self.assert_refused("git -C docs/notes status", "git folder")
        self.assert_refused("git -C docs/notes add x.md", "git folder")
        self.assert_refused("echo x > docs/notes/.git/config", ".git")
        self.assert_allowed(self.run_guard("on", "check-command", "git status"))

    def test_listed_redirect_forms_are_allowed(self) -> None:
        for cmd in ("echo x >/dev/null 2>&1", "echo x 2>/dev/null >> docs/notes/x.md", "cat < src/a.py 2>&1"):
            self.assert_allowed_forms(cmd, [])

    def test_redirect_descriptor_forms_are_allowed(self) -> None:
        cmds = [cmd.format(root=self.memory) for cmd in REDIRECT_ALLOWED]
        for cmd in cmds:
            with self.subTest(cmd=cmd):
                self.assert_allowed(self.run_guard("on", "check-command", cmd))
                self.assert_allowed(self.run_guard("on", "hook", stdin=self.hook("Bash", command=cmd)))
        self.assert_off_is_base("check-command", "; ".join(cmds))

    def test_mv_onto_a_symlink_outside_lead_write_is_refused(self) -> None:
        (self.project / "src" / "link.md").symlink_to(self.project / NOTE)
        self.assert_refused("mv docs/notes/x.md src/link.md", "src/link.md")


def _form_tests(table: dict, kind: str, marker: str) -> None:
    for name, cmd in table.items():
        def test(self: ShapeCase, cmd: str = cmd) -> None:
            self.assert_refused(cmd, marker)

        setattr(FormsTest, f"test_{kind}_{name}", test)


_form_tests(CANNOT_TELL_FORMS, "cannot_tell", CANNOT_TELL)
_form_tests(NOT_LISTED_FORMS, "not_listed", NOT_LISTED)
_form_tests(BYPASSES, "bypass_refused", "brief a bot")
_form_tests(REDIRECT_REFUSED, "redirect_refused", NOT_LISTED)

# Review T-0188.r5: bash -c, sh -c and any shell name with -c (also among other options, or by its path) cannot tell.
SHELL_C_LINES = ["bash -c 'echo x'", "sh -c 'echo x'", "dash -c 'echo x'", "zsh -c 'echo x'", "ksh -c 'echo x'",
                 "mksh -c 'echo x'", "ash -c 'echo x'", "fish -c 'echo x'", "csh -c 'echo x'", "pwsh -c 'echo x'",
                 "bash -lc 'git status'", "bash -l -c 'echo x'", "bash -e -c 'echo x' arg0", "bash --norc -c 'echo x'",
                 "bash -o posix -c 'echo x'", "/bin/bash -c 'echo x'", "/usr/bin/sh -c 'echo x'",
                 "echo y && sh -c 'echo x'", "GH_TOKEN=x bash -c 'echo x'"]
SHELL_C_ARGVS = [["bash", "-c", "echo x"], ["sh", "-c", "echo x"], ["bash", "-lc", "git status"],
                 ["/bin/bash", "-lc", "echo x"], ["/bin/sh", "-c", "echo x"], ["dash", "-c", "echo x"],
                 ["zsh", "-c", "echo x"], ["bash", "-c", "echo x", "arg0"], ["bash", "-e", "-c", "echo x"],
                 ["bash", "--norc", "-c", "echo x"], ["bash", "-c", "echo x > docs/notes/x.md"]]
SHELL_TOOLS = ("Bash", "shell")


class ShellCodeTest(ShapeCase):
    """Review T-0188.r5: a shell given its code with -c refuses the call with "cannot tell where this writes" in
    every form: guard check-command, and a hook payload of Claude Code's Bash tool or Codex CLI's shell tool with the
    command as a string or as an argv; with lead_mode off, the base result."""

    def assert_shell_code_refused(self, args: tuple, stdin: str = "") -> None:
        self.assert_lead_blocked(self.run_guard("on", *args, stdin=stdin), CANNOT_TELL)
        self.assert_off_is_base(*args, stdin=stdin)

    def test_check_command_refuses_shell_c(self) -> None:
        for line in SHELL_C_LINES:
            with self.subTest(line=line):
                self.assert_shell_code_refused(("check-command", line))

    def test_hook_string_refuses_shell_c(self) -> None:
        for tool in SHELL_TOOLS:
            for line in SHELL_C_LINES:
                with self.subTest(tool=tool, line=line):
                    self.assert_shell_code_refused(("hook",), self.hook(tool, command=line))

    def test_hook_argv_refuses_shell_c(self) -> None:
        for tool in SHELL_TOOLS:
            for argv in SHELL_C_ARGVS:
                with self.subTest(tool=tool, argv=argv):
                    self.assert_shell_code_refused(("hook",), self.hook(tool, command=argv))

    def test_a_listed_argv_is_still_allowed(self) -> None:
        for tool in SHELL_TOOLS:
            for argv in (["git", "status"], ["bash", "ops/sync.sh", "--dry-run"], ["cat", "docs/notes/x.md"]):
                with self.subTest(tool=tool, argv=argv):
                    self.assert_allowed(self.run_guard("on", "hook", stdin=self.hook(tool, command=argv)))


# Review T-0188.r6: . PATH and source PATH take exactly one argument; bash PATH keeps its arguments.
SOURCE_ALLOWED = [". ops/sync.sh", "source ops/sync.sh", ". ops/sync.sh > /dev/null", "bash ops/sync.sh --bad extra"]
SOURCE_REFUSED = {
    "dot_option_after": ". ops/sync.sh --bad",
    "dot_extra_word": ". ops/sync.sh extra",
    "dot_option_before": ". -x ops/sync.sh",
    "dot_twice": ". ops/sync.sh ops/sync.sh",
    "dot_alone": ".",
    "source_extra_word": "source ops/sync.sh extra",
    "source_option_after": "source ops/sync.sh --bad",
    "source_short_option_after": "source ops/sync.sh -x",
    "source_alone": "source",
}


class SourceArgumentsTest(ShapeCase):
    """Review T-0188.r6: an extra word after . PATH or source PATH refuses the line; with lead_mode off, the base
    result."""

    def test_dot_and_source_with_one_path_are_allowed(self) -> None:
        for cmd in SOURCE_ALLOWED:
            with self.subTest(cmd=cmd):
                self.assert_allowed(self.run_guard("on", "check-command", cmd))
                self.assert_allowed(self.run_guard("on", "hook", stdin=self.hook("Bash", command=cmd)))
        self.assert_off_is_base("check-command", "; ".join(SOURCE_ALLOWED))


def _source_tests() -> None:
    for name, cmd in SOURCE_REFUSED.items():
        def test(self: ShapeCase, cmd: str = cmd) -> None:
            self.assert_refused(cmd, NOT_LISTED)

        setattr(SourceArgumentsTest, f"test_refused_{name}", test)


_source_tests()

class WriteThenRunTest(ShapeCase):
    """bash PATH, . PATH and source PATH refuse a lead_run script that lead_write or a write_root covers: as written,
    by its own name (a symlink the lead could replace) or where it resolves. trusted/ is a folder outside the
    project that only lead_run names; outbox/tool and outbox/trusted are links into it."""

    def setUp(self) -> None:
        super().setUp()
        self.trusted = Path(self.tmp.name).resolve() / "trusted"
        self.trusted.mkdir()
        self.lead_run = f"ops/*.sh, docs/notes/*.sh, outbox/*, {self.memory}/*, {self.trusted}/*"
        for path in ("docs/notes/run.sh", "outbox/run", str(self.memory / "run.sh"), str(self.trusted / "run.sh"),
                     str(self.trusted / "tool")):
            (self.project / path).write_text("true\n", encoding="utf-8")
        (self.project / "outbox" / "tool").symlink_to(self.trusted / "tool")
        (self.project / "outbox" / "trusted").symlink_to(self.trusted)

    def assert_runs_refused(self, path: str) -> None:
        for form in ("bash", ".", "source"):
            with self.subTest(form=form, path=path):
                self.assert_refused(f"{form} {path}", NOT_LISTED)

    def test_a_lead_write_script_is_refused(self) -> None:
        for path in ("docs/notes/run.sh", "outbox/run"):
            self.assert_runs_refused(path)

    def test_a_write_root_script_is_refused(self) -> None:
        self.assert_runs_refused(str(self.memory / "run.sh"))

    def test_lead_write_over_lead_run_refuses_its_scripts(self) -> None:
        self.lead_write = LEAD_WRITE + ", ops/**"
        self.assert_runs_refused("ops/sync.sh")

    def test_a_link_lead_write_covers_is_refused(self) -> None:
        self.assert_runs_refused("outbox/tool")

    def test_a_path_through_a_linked_folder_lead_write_covers_is_refused(self) -> None:
        self.assert_runs_refused("outbox/trusted/run.sh")

    def test_a_script_the_lead_cannot_write_runs(self) -> None:
        for cmd in ("bash ops/sync.sh", f"bash {self.trusted}/run.sh --dry-run", f". {self.trusted}/tool",
                    f"source {self.trusted}/run.sh"):
            with self.subTest(cmd=cmd):
                self.assert_allowed(self.run_guard("on", "check-command", cmd))
                self.assert_off_is_base("check-command", cmd)

class OptionRowTest(ShapeCase):
    """The option table of brief T-0188-fix4, two tests per row: every listed option passes, an unknown or unlisted
    option refuses the line with "not on the lead's list"; with lead_mode off, the base result."""


def _option_tests(name: str, allowed: list, refused: list) -> None:
    def ok(self: ShapeCase) -> None:
        for cmd in allowed:
            for form, args, stdin in self.shell_forms(cmd, shlex.split(cmd)):
                with self.subTest(cmd=cmd, form=form):
                    self.assert_allowed(self.run_guard("on", *args, stdin=stdin))
        self.assert_off_is_base("check-command", "; ".join(allowed))
        self.assert_off_is_base("hook", stdin=self.hook("Bash", command="; ".join(allowed)))

    def no(self: ShapeCase) -> None:
        for cmd in refused:
            for form, args, stdin in self.shell_forms(cmd, argv_form(cmd)):
                with self.subTest(cmd=cmd, form=form):
                    self.assert_lead_blocked(self.run_guard("on", *args, stdin=stdin), NOT_LISTED)
        self.assert_off_is_base("check-command", "; ".join(refused))
        self.assert_off_is_base("hook", stdin=self.hook("Bash", command="; ".join(refused)))

    setattr(OptionRowTest, f"test_options_{name}_allowed", ok)
    setattr(OptionRowTest, f"test_options_{name}_refused", no)


for _name, (_ok, _no) in OPTION_ROWS.items():
    _option_tests(_name, _ok, _no)

# Reviews T-0188.r7 and r8: sed -n SCRIPT needs a FILE, find a PATH and git branch one of --list, -v and -a; a
# here-string (<<<) is outside the redirect rule, so it refuses the line with every command and descriptor.
OPERAND_ALLOWED = ["sed -n p docs/notes/x.md", "find docs -name x", "git branch --list", "git branch -v",
                   "git branch -a"]
OPERAND_REFUSED = {
    "sed_without_file": "sed -n p",
    "sed_without_file_after_separator": "sed -n p --",
    "sed_e_without_file": "sed -n -E /a/p",
    "find_alone": "find",
    "find_test_without_path": "find -name x",
    "git_branch_alone": "git branch",
    "git_c_branch_alone": "git -C docs branch",
}
HERE_STRINGS = {
    "cat": "cat <<< hello",
    "cat_no_blank": "cat <<<hello",
    "grep_quoted": "grep -c a <<< 'a b'",
    "sed_with_file": "sed -n p docs/notes/x.md <<< x",
    "after_a_listed_command": "echo x; wc -l <<< x",
    "fd0": "cat 0<<< hello",
    "fd3": "cat 3<<< hello",
}


class RequiredOperandTest(ShapeCase):
    """Reviews T-0188.r7 and r8: the operands and options a shape requires, and the here-string, through guard
    check-command and both hook forms; with lead_mode off, the base result."""

    def test_the_required_operands_are_allowed(self) -> None:
        for cmd in OPERAND_ALLOWED:
            self.assert_allowed_forms(cmd, [shlex.split(cmd)])


def _operand_tests() -> None:
    for table, kind in ((OPERAND_REFUSED, "refused"), (HERE_STRINGS, "here_string_refused")):
        for name, cmd in table.items():
            def test(self: ShapeCase, cmd: str = cmd) -> None:
                self.assert_refused(cmd, NOT_LISTED)

            setattr(RequiredOperandTest, f"test_{kind}_{name}", test)


_operand_tests()

# Review T-0188.r10: git commit lists the staged paths past every config that hides or renames an entry; a gitlink
# and a name that is not UTF-8 refuse the commit wherever they are.
GITLINK = "160000"
EMPTY_BLOB = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
COMMIT = "git commit -q -m brief"


def stage(cwd: Path, path: bytes, mode: str = "100644") -> None:
    """An index entry for path (bytes, so any name) with no file in the tree; a gitlink names a commit that need not
    exist."""
    oid = "1" * 40 if mode == GITLINK else EMPTY_BLOB
    subprocess.run([b"git", b"update-index", b"--add", b"--cacheinfo", f"{mode},{oid},".encode() + path], cwd=cwd,
                   check=True, capture_output=True, env={**os.environ, "HOME": str(cwd)})


class StagedListingTest(ShapeCase):
    """Review T-0188.r10: git commit is checked against every path it would record, whatever the repository's config
    hides (diff.ignoreSubmodules, submodule.NAME.ignore, a replace ref) or renames (diff.renames); a staged gitlink
    refuses it inside lead_write too, and so does a name that is not UTF-8; with lead_mode off, the base result."""

    def start(self) -> None:
        git(self.project, "add", "team/briefs/T-1.md")
        git(self.project, "commit", "-q", "-m", "start")

    def test_r10_a_gitlink_diff_ignore_submodules_hides_is_refused(self) -> None:
        self.start()
        git(self.project, "config", "diff.ignoreSubmodules", "all")
        stage(self.project, b"src", GITLINK)
        with self.subTest(staged="the gitlink alone"):
            self.assert_refused(COMMIT, "src", "gitlink")
        git(self.project, "add", NOTE)
        with self.subTest(staged="the gitlink and an allowed note"):
            self.assert_refused(COMMIT, "src", "gitlink")

    def test_a_gitlink_gitmodules_ignore_hides_is_refused(self) -> None:
        self.start()
        (self.project / ".gitmodules").write_text('[submodule "src"]\n\tpath = src\n\turl = ./src\n\tignore = all\n',
                                                  encoding="utf-8")
        stage(self.project, b"src", GITLINK)
        git(self.project, "add", NOTE)
        self.assert_refused(COMMIT, "src", "gitlink")

    def test_a_gitlink_inside_lead_write_is_refused(self) -> None:
        stage(self.project, b"docs/notes/sub", GITLINK)
        with self.subTest(staged="added"):
            self.assert_refused(COMMIT, "docs/notes/sub", "gitlink")
        git(self.project, "commit", "-q", "-m", "start")
        git(self.project, "rm", "-q", "--cached", "docs/notes/sub")
        with self.subTest(staged="removed"):
            self.assert_refused(COMMIT, "docs/notes/sub", "gitlink")

    def test_a_path_a_replace_ref_hides_is_refused(self) -> None:
        self.start()
        git(self.project, "add", NOTE, CODE)
        tree = git(self.project, "write-tree").strip()
        git(self.project, "replace", "HEAD", git(self.project, "commit-tree", tree, "-m", "same tree").strip())
        self.assert_refused(COMMIT, CODE)

    def test_the_old_name_of_a_rename_is_refused(self) -> None:
        (self.project / "src" / "b.md").write_text("b\n" * 20, encoding="utf-8")
        git(self.project, "add", "src/b.md")
        git(self.project, "commit", "-q", "-m", "start")
        git(self.project, "config", "diff.renames", "copies")
        git(self.project, "mv", "src/b.md", "docs/notes/b.md")
        self.assert_refused(COMMIT, "src/b.md")

    def test_a_name_with_a_newline_outside_lead_write_is_refused(self) -> None:
        git(self.project, "add", NOTE)
        stage(self.project, b"src/a\ndocs/notes/x.md")
        self.assert_refused(COMMIT, "src/a\ndocs/notes/x.md", "outside lead_write")

    def test_a_name_that_is_not_utf8_is_refused(self) -> None:
        git(self.project, "add", NOTE)
        stage(self.project, b"src/\xff.md")
        self.assert_refused(COMMIT, CANNOT_TELL, "UTF-8")

    def test_a_name_that_is_not_utf8_inside_lead_write_is_refused(self) -> None:
        stage(self.project, b"docs/notes/\xff.md")
        self.assert_refused(COMMIT, CANNOT_TELL, "UTF-8")

    def test_a_staged_path_behind_a_working_tree_symlink_is_refused(self) -> None:
        """Review T-0188.r3: the staged name src/outside.md resolves through a working-tree symlink src -> docs/notes
        to a lead_write path, but git records src/outside.md, which is outside lead_write."""
        self.start()
        (self.project / "src" / "outside.md").write_text("x\n", encoding="utf-8")
        git(self.project, "add", "src/outside.md")
        shutil.rmtree(self.project / "src")
        (self.project / "src").symlink_to(self.project / "docs" / "notes")
        self.assert_refused(COMMIT, "src/outside.md", "outside lead_write")

    def test_an_allowed_note_alone_is_allowed(self) -> None:
        self.start()
        for key, value in (("diff.ignoreSubmodules", "all"), ("diff.renames", "copies"), ("diff.relative", "true")):
            git(self.project, "config", key, value)
        git(self.project, "add", NOTE)
        self.assert_allowed_forms(COMMIT, [shlex.split(COMMIT)])

    def test_a_first_commit_lists_every_staged_path(self) -> None:
        (self.project / "src" / "b.md").write_text("b\n", encoding="utf-8")
        git(self.project, "add", NOTE)
        with self.subTest(staged="the note alone"):
            self.assert_allowed_forms(COMMIT, [shlex.split(COMMIT)])
        git(self.project, "add", "src/b.md")
        with self.subTest(staged="the note and src/b.md"):
            self.assert_refused(COMMIT, "src/b.md")


# Review T-0188.r12: a word the shell expands can become more operands or another path than the guard checks: uniq
# src/* overwrote src/b.txt. uniq (OUTPUT) and sed (SCRIPT) have an operand that writes, so no word of theirs may
# expand; a path the guard relies on (a write target, cd, cp, mv, git add, bash PATH) takes ~ only as ~ or ~/PATH
# and no $"..." (bash drops its $, so the guard checked outbox/$../README.md while bash wrote README.md).
EXPANDING_REFUSED = {
    "r12_uniq_glob": ("uniq src/*", CANNOT_TELL),
    "r12_uniq_brace": ("uniq src/{a.txt,b.txt}", CANNOT_TELL),
    "r12_uniq_two_files": ("uniq src/a.txt src/b.txt", NOT_LISTED),
    "uniq_question_mark": ("uniq src/?.txt", CANNOT_TELL),
    "uniq_bracket": ("uniq src/[ab].txt", CANNOT_TELL),
    "uniq_sequence": ("uniq src/{a..b}.txt", CANNOT_TELL),
    "uniq_quoted_closing_brace": ('uniq src/{"a}",b.txt}', CANNOT_TELL),
    "uniq_escaped_closing_brace": ("uniq src/{a\\},b.txt}", CANNOT_TELL),
    "uniq_option_and_glob": ("uniq -c src/*", CANNOT_TELL),
    "uniq_tilde_pwd": ("uniq ~+/src/a.txt", CANNOT_TELL),
    "uniq_tilde_user": ("uniq ~root/a.txt", CANNOT_TELL),
    "sed_glob": ("sed -n p src/*", CANNOT_TELL),
    "sed_brace": ("sed -n p src/{a,b}.txt", CANNOT_TELL),
    "redirect_tilde_after_name": ("cd ./docs/notes && echo x > n=~/m.md", CANNOT_TELL),
    "redirect_locale_string": ('echo x > outbox/$"../README.md"', CANNOT_TELL),
    "cp_locale_string": ('cp docs/notes/x.md outbox/$"../x.md"', CANNOT_TELL),
    "cp_tilde_pwd": ("cp docs/notes/x.md ~+/outbox/x.md", CANNOT_TELL),
    "mkdir_tilde_oldpwd": ("mkdir -p ~-/outbox/new", CANNOT_TELL),
    "git_add_tilde_pwd": ("git add ~+/docs/notes/x.md", CANNOT_TELL),
    "cd_tilde_pwd": ("cd ~+/docs/notes", CANNOT_TELL),
}
# A reader has no operand that writes: a word it gets may expand (after a fixed first character), as before.
EXPANDING_ALLOWED = ["cat src/*", "grep -n a docs/notes/*.md", "ls src/{a,b}.txt", "head -1 src/?.txt",
                     "wc -l src/[ab].txt", "sort -u src/*", "find src* -name x", "git log --oneline -- docs/*",
                     "uniq docs/notes/a.md", "uniq -c -u docs/notes/x.md", "uniq -c ~/notes.md"]


class ExpansionTest(ShapeCase):
    """Review T-0188.r12: through guard check-command and both hook forms, a word the shell expands refuses uniq and
    sed and any path the guard relies on; a reader keeps it; with lead_mode off, the base result."""

    def setUp(self) -> None:
        super().setUp()
        for name in ("a.txt", "b.txt"):
            (self.project / "src" / name).write_text("1\n", encoding="utf-8")

    def test_an_expanding_operand_of_a_reader_is_allowed(self) -> None:
        for cmd in EXPANDING_ALLOWED:
            self.assert_allowed_forms(cmd, [shlex.split(cmd)])

    def test_a_quoted_tilde_is_no_home(self) -> None:
        """bash expands ~ only before an unquoted / (or alone): ~"/memory" is the folder ~ in the working folder, so
        a write_root below home does not cover it."""
        self.memory = self.home / "memory"
        self.memory.mkdir()
        self.assert_allowed_forms("echo x > ~/memory/m.md && mkdir -p ~/memory/new",
                                  [["mkdir", "-p", str(self.memory / "new")]])
        for cmd in ('mkdir -p ~"/memory/new"', "echo x > ~\\/memory/m.md", "cp docs/notes/x.md ~''/memory/m.md"):
            with self.subTest(cmd=cmd):
                self.assert_refused(cmd, CANNOT_TELL)

    def test_uniq_takes_its_options_before_input(self) -> None:
        """With POSIXLY_CORRECT in the session's environment, GNU uniq reads uniq x.md -c as INPUT x.md and OUTPUT
        -c, a file in the working folder: an option after INPUT is refused."""
        for cmd in ("uniq docs/notes/x.md -c", "uniq - -u", "uniq docs/notes/x.md -c -d"):
            with self.subTest(cmd=cmd):
                self.assert_refused(cmd, NOT_LISTED, "uniq")
        self.assert_allowed_forms("uniq -c -d docs/notes/x.md", [["uniq", "-c", "-d", "docs/notes/x.md"]])


def _expansion_tests() -> None:
    for name, (cmd, marker) in EXPANDING_REFUSED.items():
        def test(self: ShapeCase, cmd: str = cmd, marker: str = marker) -> None:
            self.assert_refused(cmd, marker)

        setattr(ExpansionTest, f"test_refused_{name}", test)


_expansion_tests()

# Review T-0188.r11: with lead_env = HOME, HOME=docs/notes git status passed, and a .gitconfig the lead wrote there
# ran its core.fsmonitor command. A name of ENV_RUNS is ignored in lead_env, with one stderr line naming it, and
# refuses the line as a prefix or an export.
ENV_IGNORED = ("HOME XDG_CONFIG_HOME XDG_DATA_HOME PATH BASH_ENV ENV SHELLOPTS PS4 IFS LD_PRELOAD LD_LIBRARY_PATH "
               "PYTHONPATH PYTHONSTARTUP PYTHONHOME PYTHONINSPECT GIT_DIR GIT_CONFIG_GLOBAL GIT_EXEC_PATH EDITOR "
               "VISUAL PAGER GIT_PAGER").split()
# Names of the same kind the review did not name: a config folder, a search path, a program run for the user, the
# store and keys pass show reads, option parsing (POSIXLY_CORRECT), where cd goes, where temporary files go, the
# history file (bash truncates HISTFILE when HISTFILESIZE is assigned, also without a terminal) and the programs bash
# skips in PATH (EXECIGNORE).
ENV_ALSO_IGNORED = ("GH_CONFIG_DIR GNUPGHOME ZDOTDIR CDPATH MANPATH PERL5LIB PERL5OPT RUBYOPT NODE_OPTIONS LESSOPEN "
                    "BROWSER SHELL BASHOPTS PS1 PROMPT_COMMAND DYLD_INSERT_LIBRARIES PASSWORD_STORE_DIR SSH_ASKPASS "
                    "JAVA_TOOL_OPTIONS INPUTRC POSIXLY_CORRECT PWD OLDPWD TMPDIR HISTFILE HISTFILESIZE "
                    "EXECIGNORE OPENSSL_CONF").split()
CONFIG_LEAD_ENV = "GH_TOKEN, WOUF_TEAM_DIR, SYNC_PACK_ON_LAND, PKG_MAIN"
IGNORED_LINE = re.compile(r"^guard: lead_env: (\S+) is ignored", re.M)


class LeadEnvTest(ShapeCase):
    """Review T-0188.r11, through guard check-command and both hook forms: the names of ENV_IGNORED are never a
    lead_env name; GH_TOKEN stays one; with lead_mode off, the base result."""

    def test_r11_home_from_lead_env_is_refused(self) -> None:
        self.lead_env = "HOME"
        probe = Path(self.tmp.name) / "probe"
        (self.project / "docs" / "notes" / ".gitconfig").write_text(f"[core]\n\tfsmonitor = touch {probe}\n",
                                                                    encoding="utf-8")
        for cmd in ("HOME=docs/notes git status", f"HOME={self.project}/docs/notes git status",
                    "export HOME=docs/notes", "export HOME=docs/notes; git status"):
            with self.subTest(cmd=cmd):
                self.assert_refused(cmd, NOT_LISTED, "HOME")
        self.assertFalse(probe.exists())
        result = self.run_guard("on", "check-command", "git status")
        self.assert_allowed(result)
        self.assertEqual(IGNORED_LINE.findall(result.stderr), ["HOME"])

    def test_every_name_that_redirects_a_program_is_ignored(self) -> None:
        names = ENV_IGNORED + ENV_ALSO_IGNORED
        self.lead_env = ", ".join(names + ["GH_TOKEN"])
        result = self.run_guard("on", "check-command", "GH_TOKEN=x gh pr list")
        self.assert_allowed(result)
        self.assertEqual(sorted(IGNORED_LINE.findall(result.stderr)), sorted(names))
        for name in names:
            for cmd in (f"{name}=x gh pr list", f"export {name}=x"):
                with self.subTest(cmd=cmd):
                    self.assert_lead_blocked(self.run_guard("on", "check-command", cmd), NOT_LISTED, name)

    def test_r12_openssl_conf_from_lead_env_is_ignored_and_refused(self) -> None:
        """Review T-0188.r12: OPENSSL_CONF selects the OpenSSL configuration file; both forms returned 0."""
        self.lead_env = "OPENSSL_CONF"
        for cmd in ("OPENSSL_CONF=docs/notes/openssl.cnf git status", "export OPENSSL_CONF=docs/notes/openssl.cnf"):
            with self.subTest(cmd=cmd):
                self.assert_refused(cmd, NOT_LISTED, "OPENSSL_CONF")
        result = self.run_guard("on", "check-command", "git status")
        self.assert_allowed(result)
        self.assertEqual(IGNORED_LINE.findall(result.stderr), ["OPENSSL_CONF"])

    def test_gh_token_from_lead_env_is_allowed(self) -> None:
        self.lead_env = "GH_TOKEN"
        self.assert_allowed_forms("GH_TOKEN=x gh pr list", [["GH_TOKEN=x", "gh", "pr", "list"]])
        self.assert_allowed_forms("export GH_TOKEN=x", [["export", "GH_TOKEN=x"]])
        self.assertEqual(IGNORED_LINE.findall(self.run_guard("on", "check-command", "GH_TOKEN=x gh pr list").stderr),
                         [])

    def test_the_names_of_the_config_example_stay_allowed(self) -> None:
        self.lead_env = CONFIG_LEAD_ENV
        for name in CONFIG_LEAD_ENV.split(", "):
            with self.subTest(name=name):
                result = self.run_guard("on", "check-command", f"{name}=x gh pr list; export {name}=y")
                self.assert_allowed(result)
                self.assertEqual(IGNORED_LINE.findall(result.stderr), [])


# Review T-0188-fix10.r3: SSL_CERT_FILE=docs/notes/cert.pem gh pr list (lead_env = SSL_CERT_FILE) and
# TZDIR=docs/notes TZ=Test date (lead_env = TZDIR, TZ) returned 0. A list of names never closes, so the guard bounds
# the value (brief T-0188-fix11): $(pass show ENTRY), or a literal of letters, digits and _ . : @ + , = - that is not
# . or .., starts with neither ~ nor -, and names no file, folder or link in the working folder, after quote
# removal. Any other value is not on the list; a substitution other than pass show still cannot tell.
ENV_VALUES_REFUSED = {
    "slash": ("PKG_MAIN=release/0.2 gh pr list", NOT_LISTED),
    "export_slash": ("export PKG_MAIN=release/0.2", NOT_LISTED),
    "double_quoted_slash": ('PKG_MAIN="release/0.2" gh pr list', NOT_LISTED),
    "single_quoted_slash": ("export PKG_MAIN='release/0.2'", NOT_LISTED),
    "escaped_slash": ("PKG_MAIN=release\\/0.2 gh pr list", NOT_LISTED),
    "absolute": ("PKG_MAIN=/tmp gh pr list", NOT_LISTED),
    "dot": ("PKG_MAIN=. gh pr list", NOT_LISTED),
    "dot_dot": ("export PKG_MAIN=..", NOT_LISTED),
    "tilde": ("PKG_MAIN=~ gh pr list", NOT_LISTED),
    "tilde_user": ("PKG_MAIN=~main gh pr list", NOT_LISTED),
    "dash": ("PKG_MAIN=-main gh pr list", NOT_LISTED),
    "blank": ("PKG_MAIN='a b' gh pr list", NOT_LISTED),
    "glob": ("PKG_MAIN=* gh pr list", NOT_LISTED),
    "existing_folder": ("PKG_MAIN=docs gh pr list", NOT_LISTED),
    "existing_dot_folder": ("export PKG_MAIN=.git", NOT_LISTED),
    "append": ("PKG_MAIN+=x gh pr list", NOT_LISTED),
    "append_pass_show": ("GH_TOKEN+=$(pass show github/pat) gh pr list", NOT_LISTED),
    "variable": ("PKG_MAIN=$PWD gh pr list", NOT_LISTED),
    "substitution": ("PKG_MAIN=$(date) gh pr list", NOT_LISTED),
    "export_substitution": ("export PKG_MAIN=$(date)", NOT_LISTED),
    "backtick_substitution": ("PKG_MAIN=`date` gh pr list", NOT_LISTED),
    "quoted_substitution": ('PKG_MAIN="$(date)" gh pr list', NOT_LISTED),
}
ENV_VALUES_ALLOWED = ["PKG_MAIN=release-0.2 gh pr list", "export PKG_MAIN=main", 'PKG_MAIN="main" gh pr list',
                      "PKG_MAIN= gh pr list", "export PKG_MAIN=v0.2+build.1", "export PKG_MAIN=user@host:a,b=c",
                      "SYNC_PACK_ON_LAND=1 gh pr list", "export SYNC_PACK_ON_LAND=1",
                      "GH_TOKEN=$(pass show github/pat) gh pr list", 'GH_TOKEN="$(pass show github/pat)" gh pr list']


class LeadEnvValueTest(ShapeCase):
    """Brief T-0188-fix11, through guard check-command and both hook forms: a lead_env value is $(pass show ENTRY) or a
    short literal that cannot name a path; with lead_mode off, the base result."""

    def setUp(self) -> None:
        super().setUp()
        self.lead_env = CONFIG_LEAD_ENV

    def assert_value_allowed(self, cmd: str) -> None:
        """cmd passes in every form; one with shell syntax has no argv of its own (Codex wraps it in bash -lc)."""
        self.assert_allowed_forms(cmd, [] if SHELL_SYNTAX.search(cmd) else [shlex.split(cmd)])

    def cwd_hooks(self, cmd: str, cwd: Path) -> list:
        """The Bash hook (a string) and the shell hook (an argv) for cmd, run in cwd."""
        return [json.dumps({"tool_name": tool, "tool_input": {"command": command}, "cwd": str(cwd)})
                for tool, command in (("Bash", cmd), ("shell", shlex.split(cmd)))]

    def test_fix10_r3_ssl_cert_file_naming_a_lead_file_is_refused(self) -> None:
        self.lead_env = "SSL_CERT_FILE"
        (self.project / "docs" / "notes" / "cert.pem").write_text("x\n", encoding="utf-8")
        for cmd in ("SSL_CERT_FILE=docs/notes/cert.pem gh pr list", "export SSL_CERT_FILE=docs/notes/cert.pem",
                    'SSL_CERT_FILE="docs/notes/cert.pem" gh pr list',
                    "cd ./docs/notes && SSL_CERT_FILE=cert.pem gh pr list"):
            with self.subTest(cmd=cmd):
                self.assert_refused(cmd, NOT_LISTED, "SSL_CERT_FILE")
        for stdin in self.cwd_hooks("SSL_CERT_FILE=cert.pem gh pr list", self.project / "docs" / "notes"):
            with self.subTest(stdin=stdin):
                self.assert_lead_blocked(self.run_guard("on", "hook", stdin=stdin), NOT_LISTED, "SSL_CERT_FILE")

    def test_fix10_r3_tzdir_naming_a_lead_folder_is_refused(self) -> None:
        self.lead_env = "TZDIR, TZ"
        for cmd in ("TZDIR=docs/notes TZ=Test date", "export TZDIR=docs/notes TZ=Test", "TZDIR=docs/notes TZ=Test ls"):
            with self.subTest(cmd=cmd):
                self.assert_refused(cmd, NOT_LISTED, "TZDIR")
        self.assert_value_allowed("TZ=UTC date")

    def test_fix11_a_value_naming_an_entry_of_the_working_folder_is_refused(self) -> None:
        """TZDIR=notes where ./notes exists, also as a link that points nowhere (lexists), in the folder that cd or
        the hook's cwd names."""
        self.lead_env = "TZDIR, TZ"
        self.assert_value_allowed("TZDIR=notes TZ=Test date")
        self.assert_refused("cd ./docs && TZDIR=notes TZ=Test date", NOT_LISTED, "TZDIR")
        for stdin in self.cwd_hooks("TZDIR=notes TZ=Test date", self.project / "docs"):
            with self.subTest(stdin=stdin):
                self.assert_lead_blocked(self.run_guard("on", "hook", stdin=stdin), NOT_LISTED, "TZDIR")
        (self.project / "notes").mkdir()
        (self.project / "zone").symlink_to(Path(self.tmp.name) / "missing")
        for cmd in ("TZDIR=notes TZ=Test date", "export TZDIR=notes", "TZDIR=zone date"):
            with self.subTest(cmd=cmd):
                self.assert_refused(cmd, NOT_LISTED, "TZDIR")

    def test_fix11_pass_show_and_the_config_values_stay_allowed(self) -> None:
        for cmd in ("GH_TOKEN=$(pass show github/pat) gh pr list", "SYNC_PACK_ON_LAND=1 gh pr list",
                    "export SYNC_PACK_ON_LAND=1", "PKG_MAIN=main gh pr list"):
            with self.subTest(cmd=cmd):
                self.assert_value_allowed(cmd)
        # Codex's argv for the pass show line is bash -lc LINE, which cannot tell (review T-0188.r5). An argv runs no
        # shell: its word is the literal text, which has a / and blanks.
        self.assert_argv_refused(["bash", "-lc", "GH_TOKEN=$(pass show github/pat) gh pr list"], CANNOT_TELL)
        self.assert_argv_refused(["GH_TOKEN=$(pass show github/pat)", "gh", "pr", "list"], NOT_LISTED, "GH_TOKEN")

    def test_fix11_pkg_main_with_a_slash_is_refused(self) -> None:
        for cmd in ("PKG_MAIN=release/0.2 gh pr list", "export PKG_MAIN=release/0.2"):
            with self.subTest(cmd=cmd):
                self.assert_refused(cmd, NOT_LISTED, "PKG_MAIN")


def _env_value_tests() -> None:
    for name, (cmd, marker) in ENV_VALUES_REFUSED.items():
        def refused(self: LeadEnvValueTest, cmd: str = cmd, marker: str = marker) -> None:
            variable = re.search(r"(\w+)\+?=", cmd).group(1)
            self.assert_refused(cmd, marker, *((variable,) if marker == NOT_LISTED else ()))

        setattr(LeadEnvValueTest, f"test_refused_{name}", refused)
    for number, cmd in enumerate(ENV_VALUES_ALLOWED):
        def allowed(self: LeadEnvValueTest, cmd: str = cmd) -> None:
            self.assert_value_allowed(cmd)

        setattr(LeadEnvValueTest, f"test_allowed_{number}", allowed)


_env_value_tests()

# Review T-0188.r19: bash takes a heredoc delimiter as written (after quote removal; $'...' and $"..." quote too), but
# the lexer dropped its $-forms, so the guard ended the body at another line and never checked the lines after bash's
# end line. A delimiter with a $, a ` or a part the shell computes cannot tell. Each entry: the delimiter as written,
# the end line bash uses, and the line the guard took as the end before the fix; the touch sits between the two.
DYNAMIC_DELIMITERS = {
    "double_quoted_variable": ('"E$X"', "E$X", "E"),
    "variable": ("E$X", "E$X", "E"),
    "ansi_c": ("$'EOF'", "EOF", ""),
    "braced_variable": ("${X}", "${X}", ""),
    "translated": ('$"EOF"', "EOF", "EOF"),
    "strip_tabs_variable": ("-$X", "$X", ""),
    "single_quoted_dollar": ("'E$X'", "E$X", "E$X"),
    "backtick": ("`X`", "`X`", ""),
    "substitution": ("$(X)", "$(X)", ""),
}
# The plain forms, with their end line: as before, cat may read them and python3 may not.
PLAIN_DELIMITERS = {"plain": ("EOF", "EOF"), "single_quoted": ("'EOF'", "EOF"), "double_quoted": ('"EOF"', "EOF"),
                    "strip_tabs": ("-EOF", "\tEOF")}
# bash joins a line of an unquoted body that ends in \ with the next one, the end line too: the guard read them one by
# one, so its end line (and a $(...) split at the \) was not bash's.
BODY_JOINS = {
    "bash_ends_earlier": "cat <<EOF\nEO\\\nF\ntouch {hit}\nEOF\n",
    "bash_ends_later": "cat <<EOF\nx\\\nEOF\ncat <<X\nEOF\ntouch {hit}\nX\n",
    "split_substitution": "cat <<EOF\n$\\\n(touch {hit})\nEOF\n",
}
# A $'...' ends at a ' that no \ escapes: the lexer ended it at the first ', so the rest of the line was quoted for the
# guard and commands for bash; a < target and a pack tool's arguments take a word the shell computes.
ANSI_C_SPLITS = {"read_target": "cat <$'\\'' ; touch {hit} #'",
                 "pack_tool_argument": ".agents/bin/team $'\\'' ; touch {hit} #'"}
# bash takes a word as { or } of a group, or as NAME=VALUE, only as written: a quoted or escaped one is the program
# (here a program of that name on PATH runs the touch). Each with the program bash runs.
PROGRAM_WORDS = {'"GH_TOKEN=x"': "GH_TOKEN=x", "'GH_TOKEN'=x gh pr list": "GH_TOKEN=x",
                 "GH_TOKEN\\=x": "GH_TOKEN=x", "PKG_MAIN=main \"GH_TOKEN=x\"": "GH_TOKEN=x",
                 "\\{ cat docs/notes/x.md": "{", "\\}": "}"}


class HeredocDelimiterTest(ShapeCase):
    """Review T-0188.r19, through guard check-command and both hook forms: a heredoc delimiter with a $, a ` or a
    computed part, an unquoted body line that ends in \\, and a $'...' with a \\ cannot tell, where bash runs the
    touch of a file in the test's folder outside lead_write; a quoted or escaped {, } or NAME=VALUE is the program, not
    on the list; the plain forms stay as before; with lead_mode off, the base result."""

    def setUp(self) -> None:
        super().setUp()
        self.outside = Path(self.tmp.name) / "outside"
        (self.outside / "bin").mkdir(parents=True)
        self.hit = self.outside / "hit"

    def bash_touches(self, cmd: str) -> bool:
        """Whether bash, run on cmd in the project with outside/bin first on PATH, touches outside/hit."""
        path = f"{self.outside / 'bin'}:{os.environ.get('PATH', os.defpath)}"
        subprocess.run(["bash", "-c", cmd], cwd=self.project, capture_output=True, stdin=subprocess.DEVNULL,
                       env={"PATH": path, "HOME": str(self.home)}, timeout=20)
        touched = self.hit.exists()
        if touched:
            self.hit.unlink()
        return touched

    def assert_hidden_touch_refused(self, cmd: str, *names: str) -> None:
        """bash runs the touch of cmd, and the guard refuses cmd."""
        cmd = cmd.replace("{hit}", str(self.hit))
        self.assertTrue(self.bash_touches(cmd), cmd)
        self.assert_refused(cmd, CANNOT_TELL, *names)

    def test_a_plain_delimiter_to_a_listed_command_is_allowed(self) -> None:
        for delimiter, end in PLAIN_DELIMITERS.values():
            with self.subTest(delimiter=delimiter):
                self.assert_allowed_forms(f"cat <<{delimiter}\nx\n{end}\n", [])

    def test_a_plain_delimiter_to_an_unlisted_command_is_refused(self) -> None:
        for delimiter, end in PLAIN_DELIMITERS.values():
            with self.subTest(delimiter=delimiter):
                self.assert_refused(f"python3 <<{delimiter}\nx\n{end}\n", CANNOT_TELL, "a heredoc to")

    def test_a_plain_delimiter_ends_where_bash_ends(self) -> None:
        """The touch after the end line is a command for bash and for the guard, which refuses it."""
        for delimiter, end in PLAIN_DELIMITERS.values():
            cmd = f"cat <<{delimiter}\nx\n{end}\ntouch {self.hit}\n"
            with self.subTest(delimiter=delimiter):
                self.assertTrue(self.bash_touches(cmd))
                self.assert_refused(cmd, NOT_LISTED, "touch")

    def test_a_quoted_body_is_read_as_written(self) -> None:
        """A quoted delimiter: bash joins no line, so a body line that ends in \\ stays allowed."""
        self.assert_allowed_forms("cat <<'EOF'\nEO\\\nF\nEOF\n", [])

    def test_a_dollar_quote_without_a_backslash_is_allowed(self) -> None:
        self.assert_allowed_forms("cat < $'docs/notes/x.md'", [])

    def test_a_program_word_is_not_taken_as_a_group_or_an_assignment(self) -> None:
        """bash runs a quoted or escaped {, } or NAME=VALUE as a program (string forms; an argv has no quotes)."""
        for cmd, program in PROGRAM_WORDS.items():
            (self.outside / "bin" / program).write_text(f"#!/bin/sh\ntouch '{self.hit}'\n", encoding="utf-8")
            (self.outside / "bin" / program).chmod(0o755)
            with self.subTest(cmd=cmd):
                self.assertTrue(self.bash_touches(cmd))
                for form, args, stdin in self.shell_forms(cmd, [])[:2]:
                    self.assert_lead_blocked(self.run_guard("on", *args, stdin=stdin), NOT_LISTED, program)
                    self.assert_off_is_base(*args, stdin=stdin)
        self.assert_allowed_forms("GH_TOKEN=x cat docs/notes/x.md; { cat docs/notes/x.md; }", [])


def _hidden_touch_tests() -> None:
    for name, (delimiter, end, old_end) in DYNAMIC_DELIMITERS.items():
        def dynamic(self: HeredocDelimiterTest, delimiter: str = delimiter, end: str = end, old: str = old_end) -> None:
            self.assert_hidden_touch_refused(f"cat <<{delimiter}\nx\n{end}\ntouch {{hit}}\n{old}\n", "delimiter")

        setattr(HeredocDelimiterTest, f"test_dynamic_delimiter_{name}", dynamic)
    for table, kind, marker in ((BODY_JOINS, "body", "ends in \\"), (ANSI_C_SPLITS, "ansi_c", "$'")):
        for name, cmd in table.items():
            def hidden(self: HeredocDelimiterTest, cmd: str = cmd, marker: str = marker) -> None:
                self.assert_hidden_touch_refused(cmd, marker)

            setattr(HeredocDelimiterTest, f"test_{kind}_{name}", hidden)


_hidden_touch_tests()

# Brief T-0188-fix13 (reviews T-0188.r20 and r21): bash skips a cd after ||, runs one in a pipeline or in ( ) in a
# subshell, and looks a relative folder that starts with neither ./ nor ../ up in CDPATH first, so the commands after
# it run in another folder than the cd names. With lead_write = docs/notes/**, bash writes each line's README.md
# outside lead_write (at the project's root, or below CDPATH): the guard refuses it, naming the target, or cannot
# tell (a folder bash looks up in CDPATH). Each entry: the line and what the refusal names.
CD_REFUSED = {
    "r21_after_or": ("true || cd docs/notes && echo x > README.md", (CANNOT_TELL, "CDPATH")),
    "r21_in_a_pipeline": ("true | cd docs/notes && echo x > README.md", (CANNOT_TELL, "CDPATH")),
    "in_a_subshell": ("(cd docs/notes) && echo x > README.md", (CANNOT_TELL, "CDPATH")),
    "r20_cdpath": ("cd docs/notes && echo x > README.md", (CANNOT_TELL, "CDPATH")),
    "after_or_dot": ("true || cd ./docs/notes && echo x > README.md", "README.md"),
    "in_a_pipeline_dot": ("true | cd ./docs/notes && echo x > README.md", "README.md"),
    "first_in_a_pipeline_dot": ("cd ./docs/notes | true && echo x > README.md", "README.md"),
    "in_a_subshell_dot": ("(cd ./docs/notes) && echo x > README.md", "README.md"),
    "after_or_and_a_newline": ("true ||\ncd ./docs/notes && echo x > README.md", "README.md"),
    "in_a_group_after_or": ("true || { true; cd ./docs/notes; } && echo x > README.md", "README.md"),
    "a_missing_folder_before_semicolon": ("cd ./docs/notes/new; echo x > README.md", "README.md"),
    "after_a_cd_that_failed": ("cd ./docs/notes/new && cd ./sub; echo x > README.md", "README.md"),
    # Review of T-0188-fix13: bash skips a cd after && when the command before it fails, and runs the command after
    # ; or a newline all the same, at the project's root.
    "skipped_after_and_before_semicolon": ("grep missing /dev/null && cd ./docs/notes; echo x > README.md",
                                           "README.md"),
    "skipped_after_and_before_a_newline": ("grep missing /dev/null && cd ./docs/notes\necho x > README.md",
                                           "README.md"),
}
# A cd that bash runs for sure before the commands after it: they run in its folder, and bash writes README.md in
# docs/notes ({project}: the test's absolute project folder).
CD_ALLOWED = ["cd ./docs/notes && echo x > README.md", "cd ./docs/notes; echo x > README.md",
              "cd {project}/docs/notes && echo x > README.md", "cd ./docs/notes\necho x > README.md",
              "cd ./docs && cd ../docs/notes && echo x > README.md"]
# The cd alone as Codex CLI's shell hook carries it, the argv ["cd", FOLDER], for the folders of the lines above
# ({project} as above), and what the refusal names (none: allowed).
CD_ARGVS = {"./docs/notes": (), "{project}/docs/notes": (), "docs/notes": (CANNOT_TELL, "CDPATH")}
# Why the shell hook refuses every line above: Codex CLI carries a line with an operator as its own wrapping, the argv
# ["bash", "-lc", LINE], which the guard never unwraps (review T-0188.r5), so it cannot tell before any cd rule.
WRAPPED = "a shell given its code with -c or on stdin runs code the guard does not see"


class CdTest(ShapeCase):
    """Brief T-0188-fix13, through guard check-command and both hook forms, with lead_write = docs/notes/**: a cd
    narrows the folders of the commands after it only where bash runs it in this shell for sure before them;
    elsewhere each later path must hold in the old folders too. A relative folder without ./ or ../ cannot tell
    (CDPATH). bash, run with a CDPATH, writes README.md outside lead_write for each refused line and in docs/notes
    for each allowed one; with lead_mode off, the base result. The Bash hook carries each line as a string, the shell
    hook as Codex wraps it (WRAPPED) and each cd alone as an argv (CD_ARGVS)."""

    def setUp(self) -> None:
        super().setUp()
        self.lead_write = "docs/notes/**"
        self.elsewhere = Path(self.tmp.name) / "elsewhere"
        (self.elsewhere / "docs" / "notes").mkdir(parents=True)

    def bash_writes(self, cmd: str) -> list:
        """The README.md files bash writes when it runs cmd in the project with CDPATH = the test's elsewhere folder
        (they are removed again)."""
        env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(self.home), "CDPATH": str(self.elsewhere)}
        subprocess.run(["bash", "-c", cmd], cwd=self.project, capture_output=True, stdin=subprocess.DEVNULL, env=env,
                       timeout=20)
        written = sorted(Path(self.tmp.name).rglob("README.md"))
        for path in written:
            path.unlink()
        return written

    def assert_line_forms(self, cmd: str, *names: str) -> None:
        """cmd through guard check-command and the Bash hook: refused naming names, or allowed without names; through
        the shell hook, Codex's argv ["bash", "-lc", cmd], refused because it is wrapped (WRAPPED). With lead_mode
        off, the base result in each form."""
        for form, args, stdin in self.shell_forms(cmd, ["bash", "-lc", cmd]):
            with self.subTest(cmd=cmd, form=form):
                result = self.run_guard("on", *args, stdin=stdin)
                if form == "hook-argv":
                    self.assert_lead_blocked(result, CANNOT_TELL, WRAPPED)
                elif names:
                    self.assert_lead_blocked(result, *names)
                else:
                    self.assert_allowed(result)
                self.assert_off_is_base(*args, stdin=stdin)

    def test_a_cd_bash_runs_for_sure_narrows(self) -> None:
        for cmd in CD_ALLOWED:
            cmd = cmd.format(project=self.project)
            with self.subTest(cmd=cmd):
                self.assert_line_forms(cmd)
                self.assertEqual(self.bash_writes(cmd), [self.project / "docs" / "notes" / "README.md"])

    def test_the_cd_alone_through_the_shell_hook(self) -> None:
        """An argv runs one program, so its cd moves no later command: the shell hook checks only the cd's folder."""
        for folder, names in CD_ARGVS.items():
            stdin = self.hook("shell", command=["cd", folder.format(project=self.project)])
            with self.subTest(folder=folder):
                result = self.run_guard("on", "hook", stdin=stdin)
                if names:
                    self.assert_lead_blocked(result, *names)
                else:
                    self.assert_allowed(result)
                self.assert_off_is_base("hook", stdin=stdin)

    def test_r21_a_skipped_cd_before_mv_is_refused(self) -> None:
        """The same folder list feeds mv: bash skips the cd and moves docs/notes/a.md to the project's root. The copy
        in docs/notes/docs/notes makes the source a regular file in the cd's folder too."""
        for folder in (self.project / "docs" / "notes", self.project / "docs" / "notes" / "docs" / "notes"):
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "a.md").write_text("a\n", encoding="utf-8")
        self.assert_line_forms("true || cd ./docs/notes && mv docs/notes/a.md b.md", "b.md")
        self.assert_line_forms("cd ./docs/notes && mv docs/notes/a.md b.md")

    def test_the_rule_is_finite(self) -> None:
        """bash runs these cds before the write, but only one at the start of the line or after ; or && with no ||
        before it in its and-or list, outside a pipeline, ( ) and { }, narrows: here the root stays, and is refused."""
        for cmd in ("true || true && cd ./docs/notes && echo x > README.md", "(cd ./docs/notes && echo x > README.md)",
                    "{ cd ./docs/notes && echo x > README.md; }", "GH_TOKEN=x cd ./docs/notes && echo x > README.md"):
            with self.subTest(cmd=cmd):
                self.assert_line_forms(cmd, "README.md")


def _cd_tests() -> None:
    for name, (cmd, marker) in CD_REFUSED.items():
        def refused(self: CdTest, cmd: str = cmd, marker: str | tuple = marker) -> None:
            self.assert_line_forms(cmd, *((marker,) if isinstance(marker, str) else marker))
            written = self.bash_writes(cmd)
            self.assertTrue(written, cmd)
            self.assertNotIn(self.project / "docs" / "notes" / "README.md", written)

        setattr(CdTest, f"test_refused_{name}", refused)


_cd_tests()


if __name__ == "__main__":
    unittest.main()
