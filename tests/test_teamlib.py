"""Tests for .agents/bin/teamlib: the kernel bin/team shares with its command modules, and the command registry."""

from __future__ import annotations

import ast
import contextlib
import importlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import writable_copy  # noqa: E402
from test_team_cli import Project  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / ".agents"
TEAM = SOURCE / "bin" / "team"
# What .agents/bin/federate (and later bin/project) take from bin/team when they load it as a module.
FEDERATE_NAMES = ("say", "put", "put_bytes", "secret_in", "config", "get", "team_dir", "env_dir", "CLASSES", "TASK",
                  "visible", "words", "secrets_module", "class_options", "read_json", "known_classes")
HELLO = '''"""A command module for the tests."""

from __future__ import annotations

from teamlib.core import say


def hello(args: list[str]) -> int:
    say("hello " + " ".join(args))
    return 3


COMMANDS = {"hello": hello}
USAGE = "  team hello [WORD]...                 say hello"
'''

HELLO_USAGE = "  team hello [WORD]...                 say hello"
# What T-0019-05b and T-0019-07 moved out of bin/team, by the module that holds it now: commands, and functions and
# constants.
MOVED_COMMANDS = {"engine": ("bot", "install", "image"), "status": ("doctor", "status", "runners", "context"),
                  "porting": ("export", "import"), "run": ("run", "result", "_supervise"), "gate": ("gate",),
                  "flow": ("flow",)}
MOVED_NAMES = {
    "engine": ("mode", "engine", "where", "engine_env", "sh", "plain", "image", "user", "image_tag", "volume", "labels",
               "runner_names", "cli_of", "image_tools", "pick_runner", "connector", "mcp_entry", "NEEDS_RUNNER",
               "image_runners", "recipe", "ensure_image", "build_image", "ensure_volume", "running", "uses_login",
               "login_check", "throwaway", "archive", "bot_command", "cmd_install"),
    "status": ("cmd_doctor", "cmd_status", "cmd_runners", "CONTEXT_LIMIT", "DUE_WINDOW", "RECEIPT_WIDTH", "ROW_WIDTH",
               "NAME_WIDTH", "WORD_WIDTH", "one_line", "fit_lines", "more", "latest_receipts", "schedule_rows",
               "cmd_context"),
    "porting": ("has_secret", "cmd_export", "DATA_FILES", "DATA_FOLDERS", "BOT_KEYS", "is_bot", "is_data", "proposed",
                "unsafe_member", "archive_folder", "cmd_import"),
    "core": ("copy_regular", "mask_receipt", "CLASSES", "brief_header", "glob_regex", "outside_paths"),
    "run": ("pack_stage", "commits", "stage_of", "logged_tasks", "check_task", "limit_seconds",
            "merge_log", "mask_log", "KEY_OPEN", "KEY_CLOSE", "HOLD_LINES", "open_value", "mask_stream", "masked_lines",
            "check_ref_conflict", "check_branch", "bundle_commit", "PATH_START", "split_repo", "parse_run", "take_lock",
            "release", "prepare", "check_inputs", "stage_run", "run", "LAUNCH", "supervise", "execute", "collect",
            "span", "shown", "report", "cmd_result", "LIMIT_PATTERNS", "LIMIT_TAIL", "CLOCK", "RELATIVE", "ISO",
            "EPOCH", "reset_time", "limit_hit", "return_block", "thaw", "remove_tree"),
    "usage": ("USAGE_MARK", "TOKENS", "USAGE_LINE", "USAGE_SINCE", "USAGE_ADAPTERS", "usage_adapter", "usage_step",
              "run_steps_usage", "usage_of", "receipt", "claude_project", "claude_usage"),
    "gate": ("GATE_LINES", "GATE_USAGE", "fresh_cache", "cmd_gate", "gate_execute", "run_masked"),
    "flow": ("CLASS_DEFAULTS", "class_options", "review_verdict", "FLOW_ROUNDS", "RUN_KINDS", "flow_next",
             "FLOW_MAX_STEPS", "FLOW_EXIT", "FLOW_OUTCOME", "flow_dir", "flow_lock", "flow_fallbacks", "head_of",
             "flow_brief", "block_field", "flow_step", "run_flow", "plan_commits_nothing", "flow_line", "cmd_flow"),
}
# The commands of team help at 9fc6165, when bin/team held them all; each is now registered by exactly one module.
BASE_COMMANDS = ("bot", "context", "doctor", "export", "flow", "gate", "image", "import", "install", "result", "run",
                 "runners", "status")
# The modules in import order: each may import only the ones before it (core <- engine <- usage <- run <- gate <-
# flow <- status); porting and stats sit beside that chain.
ORDER = ("core", "engine", "usage", "run", "gate", "flow", "status")
# team help before the command modules left bin/team: they may change the order of its lines, never a line.
HELP_BEFORE = '''team: run this folder's agent team. Every bot works in its own sandbox; this folder stays untouched.

  team doctor                         check what is missing, with the fix for each item
  team status                         team name, environment, bots and where each one stands
  team runners                        agent CLIs in config.ini, whether found, models per tier
  team context [--claude]             open board rows, questions and unfinished flows, for a new session
                                      (--claude: as a Claude Code SessionStart hook)

  team bot add ID                     copy an example bot from .agents/bots/ into the team (status: proposed)
  team bot create ID                  give an active bot its own storage (and build the team image if needed)
  team bot stop ID                    stop the bot's running task at once
  team bot reset ID                   archive the bot's files, then give it clean storage
  team bot remove ID                  archive its files, delete its storage, mark it retired
  team bot inspect ID [PATH]          list files in the bot's storage (read only; ask the bot first)
  team bot login ID                   sign the bot's agent CLI in to a subscription, once, in its own storage
                                      (runners with auth = login; only in your own terminal)

  team install -- COMMAND             add a system package or tool to the team image (all bots get it)
  team image                          build the team image now (otherwise it is built when needed)

  team run ID TASK --brief FILE [--tier light|mid|strong] [--runner NAME] [--model NAME] [--effort LEVEL]
           [--input PATH]... [--repo [REF]] [--detach] [--dry-run]
                                      hand a brief to a bot; with --repo the bot gets this repository at REF
                                      (default HEAD), or another one with --repo PATH[@REF], and a worker's
                                      commits come back there as branch <team>/<TASK>;
                                      --detach returns at once (a stopped caller never stops the run)
  team result ID TASK [--wait SECONDS]
                                      the run's state, or its return block and branch once done
                                      (exit 3: still working, 4: a usage limit, until a reset time)

  team gate TASK --repo REF|PATH@REF --base REF [--brief FILE] [--fresh-cache]
                                      the pre-gate of a branch, no model, no keys: files against the brief's
                                      Paths, then [flow] gate on the head in a sandbox (exit 0 green, 1 red)
  team gate --fresh-cache             empty the gate cache (WOUF_CACHE), which every task's gate shares

  team flow TASK --brief FILE --repo PATH[@REF] [--detach]
                                      take a task through its class's steps: scout where the class or brief
                                      asks, worker, gate and review, blocked after two review rounds with
                                      changes (--answer grants one more), to ready, blocked, limited or a
                                      question; again later to continue it. decision, analysis and plan have no
                                      review: they end ready after their work step, unreviewed
                                      (Class: plan runs scout and planner and ends ready with the plan's paths)
  team flow TASK [--answer TEXT] [--retry] [--status]
                                      continue with an answer, after a limit, or show where it stands

  team export [ID...] [--with-data] [-o FILE]   bot declarations and settings as .tar.gz; never secrets
  team import FILE [--apply [--force]]          list what an exported team adds; --apply writes only data (bots
                                                as proposed, skills, knowledge, board, decisions, schedule,
                                                tools.md) and puts every other file in team/import/<archive>/
                                                as it was, secrets masked, for the human to read; never applied;
                                                --force replaces existing files

How bots run (mode = container): one image for the whole team holds the agent CLIs and tools; each bot has
its own storage volume; a container exists only while a bot works on a task. Nothing of this folder or this
computer is mounted: briefs, inputs and a git bundle are copied in, results, logs and commits copied out.
Keys reach the bot through the container's standard input for one run and are masked in the output.
'''


def team_module():
    """bin/team loaded the way federate loads it."""
    loader = SourceFileLoader("wouf_team_lib", str(TEAM))
    module = module_from_spec(spec_from_loader("wouf_team_lib", loader))
    loader.exec_module(module)
    return module


def docstring() -> str:
    return ast.get_docstring(ast.parse(TEAM.read_text(encoding="utf-8")), clean=False).strip()


def assert_help(test: unittest.TestCase, text: str, hello: bool = False) -> None:
    """Core help stays present; every registered module contributes each usage line once."""
    lines = text.splitlines()
    for line in docstring().splitlines():
        if line.strip():
            test.assertIn(line, lines)
    _, usages = team_module().registry()
    if hello:
        usages.append(HELLO_USAGE)
    for block in usages:
        for line in block.splitlines():
            if line.strip():
                test.assertEqual(lines.count(line), 1, line)


def defined(path: Path) -> set:
    """The names a file defines at module level: functions, classes and assigned names."""
    names = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(n.id for t in node.targets for n in ast.walk(t) if isinstance(n, ast.Name))
    return names


class Pack:
    """A copy of the pack in a temp folder, to add command modules to; no team, no config."""

    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "proj"
        writable_copy(SOURCE, self.root / ".agents", ignore=shutil.ignore_patterns("__pycache__"))
        self.lib = self.root / ".agents" / "bin" / "teamlib"
        caller = {k: v for k, v in os.environ.items() if not k.startswith("WOUF_")}
        self.env = {**caller, "WOUF_SECRET_BACKEND": "file", "WOUF_SECRET_DIR": str(Path(self.tmp.name) / "store")}

    def module(self, name: str, text: str) -> None:
        (self.lib / name).write_text(text)

    def team(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(self.root / ".agents" / "bin" / "team"), *args], env=self.env,
                              capture_output=True, text=True, timeout=120, cwd=cwd or self.tmp.name)


class RegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.pack = Pack()
        self.addCleanup(self.pack.tmp.cleanup)

    def test_a_module_command_runs_and_its_usage_is_in_help(self) -> None:
        self.pack.module("hello.py", HELLO)
        done = self.pack.team("hello", "big", "world")
        self.assertEqual((done.returncode, done.stdout, done.stderr), (3, "hello big world\n", ""))
        done = self.pack.team("help")
        self.assertEqual(done.returncode, 0, done.stderr)
        assert_help(self, done.stdout, hello=True)

    def test_without_added_modules_help_is_the_built_in_help(self) -> None:
        for args in (("help",), ("--help",), ("-h",), ()):
            done = self.pack.team(*args)
            self.assertEqual(done.returncode, 0, args)
            assert_help(self, done.stdout)

    def test_a_clash_with_a_built_in_command_dies_with_both_names(self) -> None:
        self.pack.module("clash.py", HELLO.replace('{"hello": hello}', '{"help": hello}'))
        done = self.pack.team("help")
        self.assertEqual(done.returncode, 2)
        self.assertIn("'help'", done.stderr)
        self.assertIn("built-in", done.stderr)
        self.assertIn("clash", done.stderr)
        self.assertEqual(done.stdout, "")

    def test_a_clash_between_two_modules_dies_with_both_names(self) -> None:
        self.pack.module("first.py", HELLO)
        self.pack.module("second.py", HELLO)
        done = self.pack.team("hello")
        self.assertEqual(done.returncode, 2)
        self.assertIn("hello", done.stderr)
        self.assertIn("first", done.stderr)
        self.assertIn("second", done.stderr)

    def test_a_module_with_another_name_shape_is_never_imported(self) -> None:
        bad = 'raise SystemExit("imported")\n'
        for name in ("Bad-Name.py", "_hidden.py", "9lives.py"):
            self.pack.module(name, bad)
        self.pack.module("Upper.py", HELLO.replace('{"hello": hello}', '{"upper": hello}'))
        done = self.pack.team("help")
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        assert_help(self, done.stdout)
        done = self.pack.team("upper")
        self.assertEqual(done.returncode, 2)
        self.assertIn("unknown command", done.stderr)

    def test_help_from_another_folder(self) -> None:
        elsewhere = Path(self.pack.tmp.name) / "elsewhere"
        elsewhere.mkdir()
        done = subprocess.run([sys.executable, str(TEAM), "help"], capture_output=True, text=True, cwd=elsewhere,
                              env=self.pack.env, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        assert_help(self, done.stdout)

    def test_teamlib_comes_from_the_pack_never_from_the_cwd_or_pythonpath(self) -> None:
        # A teamlib in the caller's folder and on PYTHONPATH: loaded as a module (federate) or run, bin/team uses
        # its own.
        evil = Path(self.pack.tmp.name) / "evil"
        (evil / "teamlib").mkdir(parents=True)
        (evil / "teamlib" / "__init__.py").write_text('raise SystemExit("hijacked")\n')
        (evil / "teamlib" / "core.py").write_text('raise SystemExit("hijacked")\n')
        env = {**self.pack.env, "PYTHONPATH": str(evil)}
        team = self.pack.root / ".agents" / "bin" / "team"
        load = ("import sys\nfrom importlib.machinery import SourceFileLoader\n"
                "from importlib.util import module_from_spec, spec_from_loader\n"
                f"loader = SourceFileLoader('wouf_team', {str(team)!r})\n"
                "module = module_from_spec(spec_from_loader('wouf_team', loader))\nloader.exec_module(module)\n"
                "print(sys.modules['teamlib.core'].__file__)\n")
        done = subprocess.run([sys.executable, "-c", load], capture_output=True, text=True, cwd=evil, env=env,
                              timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(Path(done.stdout.strip()).resolve(), (self.pack.lib / "core.py").resolve())
        done = subprocess.run([sys.executable, str(team), "help"], capture_output=True, text=True, cwd=evil, env=env,
                              timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        assert_help(self, done.stdout)


class PreloadedTeamlibTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.marker = Path(self.tmp.name) / "executed"
        self.evil = Path(self.tmp.name) / "evil"
        (self.evil / "teamlib").mkdir(parents=True)
        # No __init__.py: teamlib is a namespace package, so importing it runs nothing; only core.py would.
        (self.evil / "teamlib" / "core.py").write_text(f"open({str(self.marker)!r}, 'w').write('ran')\n")

    def load_after(self, preload: str) -> subprocess.CompletedProcess:
        # A foreign teamlib (from the cwd) already imported by the caller, then bin/team loaded the way federate does.
        load = ("import sys\nfrom importlib.machinery import SourceFileLoader\n"
                "from importlib.util import module_from_spec, spec_from_loader\n"
                f"sys.path.insert(0, '.')\n{preload}\n"
                f"loader = SourceFileLoader('wouf_team', {str(TEAM)!r})\n"
                "module = module_from_spec(spec_from_loader('wouf_team', loader))\nloader.exec_module(module)\n")
        env = {k: v for k, v in os.environ.items() if not k.startswith("WOUF_")}
        return subprocess.run([sys.executable, "-c", load], capture_output=True, text=True, cwd=self.evil, env=env,
                              timeout=120)

    def test_a_foreign_teamlib_preloaded_by_the_caller_is_refused_before_it_runs(self) -> None:
        done = self.load_after("import teamlib")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("team: refused: teamlib is not the copy next to this team", done.stderr)
        self.assertFalse(self.marker.exists(), "the foreign teamlib/core.py ran")


class ImportGuardRedactionTest(unittest.TestCase):
    # A foreign teamlib is refused with a fixed message: nothing of its origin (a folder name shaped like a key) reaches
    # stdout or stderr, and no traceback is printed.
    # Split so that no key marker appears literally in the source (tests/test_pack.py scans for it).
    KEY_DIR = "sk-" + "ant-api03-SyntheticGuardKeyShapedName0123456789"
    MESSAGE = "team: refused: teamlib is not the copy next to this team"

    def test_a_key_shaped_foreign_origin_never_reaches_stdout_or_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            evil = Path(tmp) / self.KEY_DIR
            (evil / "teamlib").mkdir(parents=True)
            (evil / "teamlib" / "core.py").write_text("x = 1\n")
            load = ("import sys\nfrom importlib.machinery import SourceFileLoader\n"
                    "from importlib.util import module_from_spec, spec_from_loader\n"
                    f"sys.path.insert(0, '.')\nimport teamlib\n"
                    f"loader = SourceFileLoader('wouf_team', {str(TEAM)!r})\n"
                    "module = module_from_spec(spec_from_loader('wouf_team', loader))\nloader.exec_module(module)\n")
            env = {k: v for k, v in os.environ.items() if not k.startswith("WOUF_")}
            done = subprocess.run([sys.executable, "-c", load], capture_output=True, text=True, cwd=evil, env=env,
                                  timeout=120)
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(done.stdout, "")
        self.assertEqual(done.stderr, self.MESSAGE + "\n")
        self.assertNotIn(self.KEY_DIR, done.stdout + done.stderr)
        self.assertNotIn("Traceback", done.stderr)


class StoredValueTest(unittest.TestCase):
    def test_a_stored_value_is_masked_in_team_output(self) -> None:
        # A plain value, no key shape: only its registration from the store can mask it. Its name is no runner's
        # auth, so the config does not register it either.
        plain = "plain-stored-value-4711"
        p = Project()
        self.addCleanup(p.close)
        p.store_secret("OTHER_TOKEN", plain)
        done = p.team(plain, "x")
        self.assertEqual(done.returncode, 2)
        self.assertIn("unknown command: *** x", done.stderr)
        self.assertNotIn(plain, done.stdout + done.stderr)


class SharedStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.team = team_module()
        self.core = sys.modules["teamlib.core"]
        known, error = list(self.core.KNOWN), list(self.core.LAST_ERROR)
        self.addCleanup(self.core.KNOWN.__setitem__, slice(None), known)
        self.addCleanup(self.core.LAST_ERROR.__setitem__, slice(None), error)

    def test_a_value_registered_through_team_is_masked_by_the_sinks_in_core(self) -> None:
        plain = "plain-shared-value-5150"
        self.team.register([plain])
        self.assertIs(self.team.KNOWN, self.core.KNOWN)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.core.say(f"said {plain}")
        self.assertEqual(out.getvalue(), "said ***\n")
        self.assertEqual(json.loads(self.core.dumps({"k": plain})), {"k": "***"})
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            self.team.die(f"no {plain}")
        self.assertIs(self.team.LAST_ERROR, self.core.LAST_ERROR)
        self.assertEqual(self.core.LAST_ERROR[0], "no ***")
        self.assertNotIn(plain, err.getvalue())

    def test_the_kernel_is_loaded_once(self) -> None:
        self.assertIs(team_module().say, self.team.say)
        self.assertEqual(Path(self.core.__file__).resolve(), (SOURCE / "bin" / "teamlib" / "core.py").resolve())

    def test_the_kernel_reaches_the_engine_helpers_of_team(self) -> None:
        # fetch_masked runs in core and calls where and engine_env of the engine, which bin/team re-exports.
        self.assertIs(self.core.where, self.team.where)
        self.assertIs(self.core.engine_env, self.team.engine_env)
        with tempfile.TemporaryDirectory() as tmp:
            dst = Path(tmp) / "out"
            at = {"engine": shutil.which("false") or "false", "docker_host": ""}
            self.assertFalse(self.core.fetch_masked(at, "box", "/bot/out", dst))
            self.assertEqual(list(dst.iterdir()), [], "a failing engine leaves an empty folder")


class SplitTest(unittest.TestCase):
    """engine, status and porting (T-0019-05b), then run, gate, flow and context (T-0019-07) left bin/team for
    teamlib/: moved, not changed."""

    def test_each_moved_command_is_registered_once_by_its_module(self) -> None:
        team = team_module()
        commands, _ = team.registry()
        owners = []
        for path in sorted((SOURCE / "bin" / "teamlib").glob("*.py")):
            if path.stem != "__init__":
                module = importlib.import_module(f"teamlib.{path.stem}")
                owners += [(name, path.stem) for name in getattr(module, "COMMANDS", {})]
        for stem, names in MOVED_COMMANDS.items():
            for name in names:
                with self.subTest(command=name):
                    self.assertEqual([owner for command, owner in owners if command == name], [stem])
                    self.assertIs(commands[name], sys.modules[f"teamlib.{stem}"].COMMANDS[name])
                    self.assertNotIn(name, team.BUILT_IN)

    def test_team_defines_none_of_the_moved_names(self) -> None:
        left = defined(TEAM)
        for stem, names in MOVED_NAMES.items():
            held = defined(SOURCE / "bin" / "teamlib" / f"{stem}.py")
            for name in names:
                with self.subTest(name=name):
                    self.assertNotIn(name, left)
                    self.assertIn(name, held)

    def test_the_usage_names_left_run_and_engine(self) -> None:
        for stem in ("run", "engine"):
            with self.subTest(module=stem):
                self.assertFalse(set(MOVED_NAMES["usage"]) & defined(SOURCE / "bin" / "teamlib" / f"{stem}.py"))

    def test_help_prints_the_same_lines_as_before(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("WOUF_")}
        with tempfile.TemporaryDirectory() as tmp:
            done = subprocess.run([sys.executable, str(TEAM), "help"], capture_output=True, text=True, cwd=tmp,
                                  env=env, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        lines = sorted(line for line in done.stdout.splitlines() if line.strip())
        self.assertTrue(set(line for line in HELP_BEFORE.splitlines() if line.strip()).issubset(lines))
        assert_help(self, done.stdout)

    def test_a_clash_with_a_moved_command_names_both_modules(self) -> None:
        pack = Pack()
        self.addCleanup(pack.tmp.cleanup)
        pack.module("clash.py", HELLO.replace('{"hello": hello}', '{"status": hello}'))
        done = pack.team("help")
        self.assertEqual((done.returncode, done.stdout), (2, ""))
        self.assertIn("'status' of teamlib/status.py clashes with teamlib/clash.py", done.stderr)

    def test_moved_commands_keep_their_order_and_argument_checks(self) -> None:
        p = Project()
        self.addCleanup(p.close)
        for args in (("doctor", "x"), ("status", "x"), ("runners", "x"), ("image", "x"), ("bot",), ("bot", "create")):
            with self.subTest(args=args):
                done = p.team(*args)
                self.assertEqual((done.returncode, done.stdout, done.stderr),
                                 (2, "", f"team: unknown command: {' '.join(args)} (see: team help)\n"))
        done = p.team("install")
        self.assertEqual((done.returncode, done.stderr), (2, "team: usage: team install -- COMMAND\n"))
        # Without a config (each run reports it once, while it registers the stored values): import still runs, as
        # it reads none; every other moved command stops at the config before it looks at its arguments.
        p.config.unlink()
        done = p.team("import", "missing.tar.gz")
        self.assertEqual((done.returncode, done.stderr.splitlines()[-1]),
                         (2, "team: usage: team import FILE [--apply [--force]]"))
        for args in (("doctor", "x"), ("status",), ("runners",), ("image", "x"), ("bot",), ("install",), ("export",)):
            with self.subTest(args=args):
                done = p.team(*args)
                self.assertEqual((done.returncode, done.stdout), (2, ""))
                self.assertIn("config.ini is missing", done.stderr.splitlines()[-1])

    def test_modules_import_in_order_and_without_side_effects(self) -> None:
        # A rule, not a list: each kernel module (ORDER) imports only the ones before it; every other module of teamlib/
        # is a leaf that imports kernel modules only, never another leaf; so no kernel module imports a leaf. porting
        # and stats keep their tighter imports (core and engine, core). No module loads bin/team.
        kernel = set(ORDER)
        tighter = {"porting": {"core", "engine"}, "stats": {"core"}}
        allowed = {stem: set(ORDER[:at]) for at, stem in enumerate(ORDER)}
        for path in (SOURCE / "bin" / "teamlib").glob("*.py"):
            if path.stem != "__init__" and path.stem not in allowed:
                allowed[path.stem] = tighter.get(path.stem, kernel)
        for stem, ok in allowed.items():
            tree = ast.parse((SOURCE / "bin" / "teamlib" / f"{stem}.py").read_text(encoding="utf-8"))
            used = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "teamlib":
                    used.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("teamlib."):
                    used.add(node.module.split(".", 1)[1])
                elif isinstance(node, ast.Import):
                    used.update(a.name.split(".", 1)[1] for a in node.names if a.name.startswith("teamlib."))
            self.assertLessEqual(used, ok, stem)
        # Imported alone, in a fresh interpreter: no process started, no file written, bin/team never loaded.
        pack = Pack()
        self.addCleanup(pack.tmp.cleanup)
        home = Path(pack.tmp.name) / "home"
        home.mkdir()
        probe = ("import json, subprocess, sys\nfrom pathlib import Path\ncalls = []\n"
                 "class Refused:\n    def __init__(self, *args, **kwargs):\n        calls.append(args)\n"
                 "        raise OSError('no process at import')\n"
                 "subprocess.Popen = Refused\nsys.path.insert(0, sys.argv[1])\n"
                 "__import__('teamlib.' + sys.argv[2])\n"
                 "files = [Path(getattr(m, '__file__', '') or '.').name for m in list(sys.modules.values())]\n"
                 "print(json.dumps({'calls': len(calls), 'team': 'team' in files}))\n")

        def tree() -> list:
            return sorted(str(path) for path in Path(pack.tmp.name).rglob("*"))

        before = tree()
        for stem in allowed:
            with self.subTest(module=stem):
                done = subprocess.run([sys.executable, "-I", "-B", "-c", probe, str(pack.lib.parent), stem],
                                      capture_output=True, text=True, cwd=home, timeout=120,
                                      env={**pack.env, "HOME": str(home)})
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(json.loads(done.stdout), {"calls": 0, "team": False})
                self.assertEqual(done.stderr, "")
        self.assertEqual(tree(), before)

    def test_command_modules_come_from_the_pack_never_from_the_cwd_or_pythonpath(self) -> None:
        pack = Pack()
        self.addCleanup(pack.tmp.cleanup)
        evil = Path(pack.tmp.name) / "evil"
        (evil / "teamlib").mkdir(parents=True)
        for name in ("__init__", "core", "engine", "status", "porting", "run", "gate", "flow"):
            (evil / "teamlib" / f"{name}.py").write_text('raise SystemExit("hijacked")\n')
        env = {**pack.env, "PYTHONPATH": str(evil)}
        team = pack.root / ".agents" / "bin" / "team"
        load = ("import sys\nfrom importlib.machinery import SourceFileLoader\n"
                "from importlib.util import module_from_spec, spec_from_loader\n"
                f"loader = SourceFileLoader('wouf_team', {str(team)!r})\n"
                "module = module_from_spec(spec_from_loader('wouf_team', loader))\nloader.exec_module(module)\n"
                "module.registry()\n"
                "print(*(sys.modules['teamlib.' + n].__file__ for n in ('engine', 'status', 'porting', 'run', 'gate',"
                " 'flow')), sep='\\n')\n")
        done = subprocess.run([sys.executable, "-c", load], capture_output=True, text=True, cwd=evil, env=env,
                              timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual([Path(line).resolve() for line in done.stdout.splitlines()],
                         [(pack.lib / f"{n}.py").resolve() for n in ("engine", "status", "porting", "run", "gate",
                                                                    "flow")])
        done = subprocess.run([sys.executable, str(team), "help"], capture_output=True, text=True, cwd=evil, env=env,
                              timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        assert_help(self, done.stdout)


class LauncherTest(unittest.TestCase):
    """bin/team is a launcher (T-0019-07): the docstring, the imports, the registry and main; the commands live in
    teamlib/."""

    def test_every_command_of_the_base_help_is_registered_exactly_once(self) -> None:
        team = team_module()
        commands, _ = team.registry()
        owners = []
        for path in sorted((SOURCE / "bin" / "teamlib").glob("*.py")):
            if path.stem != "__init__":
                owners += list(getattr(importlib.import_module(f"teamlib.{path.stem}"), "COMMANDS", {}))
        for name in BASE_COMMANDS:
            with self.subTest(command=name):
                self.assertEqual(owners.count(name), 1)
                self.assertIn(name, commands)
                self.assertNotIn(name, team.BUILT_IN)

    def test_team_has_fewer_than_150_lines(self) -> None:
        self.assertLess(len(TEAM.read_text(encoding="utf-8").splitlines()), 150)

    def test_lesson_collection_helpers_live_in_engine(self) -> None:
        names = {"lesson_outputs", "collect_lessons", "event_problem"}
        self.assertLessEqual(names, defined(SOURCE / "bin/teamlib/engine.py"))
        self.assertFalse({"lesson_outputs", "event_problem"} & defined(SOURCE / "bin/teamlib/run.py"))

    def test_each_module_has_fewer_than_1300_lines(self) -> None:
        for path in sorted((SOURCE / "bin" / "teamlib").glob("*.py")):
            with self.subTest(module=path.name):
                self.assertLess(len(path.read_text(encoding="utf-8").splitlines()), 1300)

    def test_run_and_engine_left_room(self) -> None:
        # P01 made room for the packages after it: neither may grow back over 1200 lines.
        for stem in ("run", "engine"):
            with self.subTest(module=stem):
                path = SOURCE / "bin" / "teamlib" / f"{stem}.py"
                self.assertLessEqual(len(path.read_text(encoding="utf-8").splitlines()), 1200)

    def test_the_supervisor_and_the_flow_steps_start_bin_team_by_its_absolute_path(self) -> None:
        # run, flow_step and flow --detach start bin/team itself, never a module a bot could have put a file next to.
        team_module()
        run = sys.modules["teamlib.run"]
        self.assertTrue(run.TEAM.is_absolute())
        self.assertEqual(run.TEAM, TEAM.resolve())
        self.assertIs(sys.modules["teamlib.flow"].TEAM, run.TEAM)
        for stem in ("run", "gate", "flow", "status"):
            text = (SOURCE / "bin" / "teamlib" / f"{stem}.py").read_text(encoding="utf-8")
            with self.subTest(module=stem):
                self.assertEqual(text.count("__file__"), 1 if stem == "run" else 0)


class ReExportTest(unittest.TestCase):
    def test_names_federate_uses_stay_importable_from_team(self) -> None:
        team = team_module()
        for name in FEDERATE_NAMES:
            with self.subTest(name=name):
                self.assertTrue(hasattr(team, name), name)

    def test_modules_are_not_executable(self) -> None:
        for path in (SOURCE / "bin" / "teamlib").glob("*.py"):
            self.assertFalse(os.access(path, os.X_OK), path.name)
            self.assertIn("\nfrom __future__ import annotations\n", path.read_text(), path.name)


if __name__ == "__main__":
    unittest.main()
