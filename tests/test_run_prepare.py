"""prepare() in teamlib/run.py, split into smaller functions (T-0037c), gives main's spec and main's refusals.

The golden side is main's run.py, taken with `git show` in this run (PREPARE_GOLDEN_REF, main or origin/main); only
that one file is swapped into the same throwaway project, so the two sides differ in prepare() alone. Both sides start
from the same copy of the project. Normalised: `nonce` (os.urandom per run). The spec holds no time; the commit, the
paths and the secrets' names are the same on both sides and compared as they are."""

from __future__ import annotations

import configparser
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import writable_copy  # noqa: E402
from test_team_cli import ROOT, Project, team_module  # noqa: E402

RUN_PY = ".agents/bin/teamlib/run.py"
NONCE = "<nonce>"
# Loads bin/team as a module (every teamlib module plugs in), then calls prepare() as `team run` does.
DRIVER = """
import json, sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
loader = SourceFileLoader("team_cli", sys.argv[1])
team = module_from_spec(spec_from_loader("team_cli", loader))
loader.exec_module(team)
core, run = sys.modules["teamlib.core"], sys.modules["teamlib.run"]
core.register_stored()
spec, secrets = run.prepare(core.config(), run.parse_run(sys.argv[2:]))
spec["nonce"] = %r
sys.stdout.write(json.dumps(spec) + "\\n" + json.dumps(sorted(secrets)) + "\\n")
""" % NONCE


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True)


def main_run_py() -> bytes | None:
    refs = (os.environ.get("PREPARE_GOLDEN_REF"), "main", "origin/main")
    for ref in refs:
        if ref and not git("rev-parse", "--verify", "-q", ref + "^{commit}").returncode:
            shown = git("show", f"{ref}:{RUN_PY}")
            if not shown.returncode:
                return shown.stdout
    return None


MAIN = main_run_py()


class Sides:
    """One project, run twice from the same copy: once with main's run.py, once with this tree's."""

    def __init__(self, test: unittest.TestCase, **project) -> None:
        self.p = Project(**project)
        test.addCleanup(self.p.close)
        self.p.store_secret("ALPHA_KEY", "alpha-value-1234")

    def team(self, *args: str) -> tuple:
        done = self.p.team(*args)
        return done.returncode, done.stdout, done.stderr

    def spec(self, *args: str) -> tuple:
        done = subprocess.run([sys.executable, "-c", DRIVER, str(self.p.root / ".agents" / "bin" / "team"), *args],
                              env=self.p.env, capture_output=True, text=True, timeout=300, cwd=self.p.root)
        return done.returncode, done.stdout, done.stderr

    def both(self, call, *args: str) -> tuple:
        """(main, head), each from the same state of the project; main is None without a main ref."""
        snap = Path(self.p.tmp.name) / "snap"
        writable_copy(self.p.root, snap, symlinks=True)
        main = None
        if MAIN is not None:
            (self.p.root / RUN_PY).write_bytes(MAIN)
            main = call(*args)
            shutil.rmtree(self.p.root)
            writable_copy(snap, self.p.root, symlinks=True)
        head = call(*args)
        shutil.rmtree(snap)
        return main, head


class GoldenSpecTest(unittest.TestCase):
    """The JSON spec from prepare() at main and at this tree is byte for byte the same, nonce normalised."""

    def check(self, sides: Sides, *args: str) -> dict:
        main, head = sides.both(sides.spec, *args)
        self.assertEqual(head[0], 0, head[2])
        spec = json.loads(head[1].splitlines()[0])
        self.assertEqual(spec["nonce"], NONCE)
        if main is None:
            self.skipTest("no main ref in this checkout (set PREPARE_GOLDEN_REF)")
        self.assertEqual(main, head)
        return spec

    def test_plain_task(self) -> None:
        sides = Sides(self, runner="alpha")
        spec = self.check(sides, "worker", "T-1", "--brief", str(sides.p.brief))
        self.assertEqual((spec["repo"], spec["branch"]), (None, None))

    def test_repo_with_a_branch(self) -> None:
        sides = Sides(self, runner="alpha")
        sides.p.git("branch", "feature")
        spec = self.check(sides, "worker", "T-2", "--brief", str(sides.p.brief), "--repo", "feature")
        self.assertEqual((spec["repo"], spec["branch"]), ("feature", "wouf/T-2"))

    def test_staged(self) -> None:
        sides = Sides(self, runner="alpha")
        spec = self.check(sides, "worker", "T-3", "--brief", str(sides.p.brief), "--staged")
        self.assertTrue(spec["staged"])

    def test_brief_with_inputs(self) -> None:
        sides = Sides(self, runner="alpha")
        folder = sides.p.root / "notes"
        folder.mkdir()
        (folder / "a.md").write_text("a\n")
        (folder / "b.bin").write_bytes(bytes(range(256)))
        spec = self.check(sides, "worker", "T-4", "--brief", str(sides.p.brief), "--input", str(sides.p.root / "file.txt"),
                          "--input", str(folder))
        self.assertEqual(spec["binary_inputs"], ["in/notes/b.bin"])

    def test_tier_model_and_effort(self) -> None:
        sides = Sides(self, runner="alpha")
        spec = self.check(sides, "review", "T-5", "--brief", str(sides.p.brief), "--tier", "light", "--model", "m-1",
                          "--effort", "high", "--repo")
        self.assertEqual((spec["model"], spec["effort"]), ("m-1", "default"))


class RefusalTest(unittest.TestCase):
    """Every refusal of prepare() (its own and those of the checks it calls) with its message, and main's output."""

    def refuse(self, sides: Sides, message: str, *args: str, code: int = 2) -> None:
        main, head = sides.both(sides.team, "run", *args)
        self.assertEqual(head[0], code, head[2])
        self.assertIn(message, head[1] + head[2])
        if main is not None:
            self.assertEqual(main, head)

    def sides(self, **project) -> Sides:
        return Sides(self, **{"runner": "alpha", **project})

    def test_reserved_member(self) -> None:
        s = self.sides()
        self.refuse(s, "bot id 'lead' is reserved", "lead", "T-1", "--brief", str(s.p.brief))

    def test_inactive_bot(self) -> None:
        s = self.sides()
        s.p.bot_file("worker").write_text(s.p.bot_file("worker").read_text().replace("status: active", "status: paused"))
        self.refuse(s, "worker is paused; activate it before handing it work", "worker", "T-1", "--brief", str(s.p.brief))

    def test_bad_task_id(self) -> None:
        s = self.sides()
        self.refuse(s, "team:", "worker", "T 1", "--brief", str(s.p.brief))

    def test_budget_cap_not_a_number(self) -> None:
        s = self.sides()
        s.p.config.write_text(s.p.config.read_text() + "\n[budget]\nper_run = lots\n")
        self.refuse(s, "[budget] per_run = lots: a number of tokens", "worker", "T-1", "--brief", str(s.p.brief))

    def test_brief_missing(self) -> None:
        s = self.sides()
        self.refuse(s, "a brief file is required (--brief FILE)", "worker", "T-1")
        self.refuse(s, "a brief file is required (--brief FILE)", "worker", "T-1", "--brief", "nope.md")

    def test_unknown_runner(self) -> None:
        s = self.sides()
        self.refuse(s, "unknown runner 'nope'", "worker", "T-1", "--brief", str(s.p.brief), "--runner", "nope")

    def test_runner_without_command(self) -> None:
        s = self.sides()
        s.p.set("command = beta-cli exec {prompt}", "command =")
        self.refuse(s, "[runner.beta] has no command; fill it in in team/config.ini", "worker", "T-1", "--brief",
                    str(s.p.brief), "--runner", "beta")

    def test_bad_tier(self) -> None:
        s = self.sides()
        self.refuse(s, "tier must be light, mid or strong, not 'huge'", "worker", "T-1", "--brief", str(s.p.brief),
                    "--tier", "huge")

    def test_bad_model(self) -> None:
        s = self.sides()
        self.refuse(s, "model a b: letters, digits and . _ : / @ - only", "worker", "T-1", "--brief", str(s.p.brief),
                    "--model", "a b")

    def test_bad_effort(self) -> None:
        s = self.sides()
        self.refuse(s, "effort x y: a level such as low, medium, high", "worker", "T-1", "--brief", str(s.p.brief),
                    "--effort", "x y")

    def test_unknown_connector(self) -> None:
        s = self.sides()
        s.p.bot_file("worker").write_text(s.p.bot_file("worker").read_text().replace("connectors: none",
                                                                                     "connectors: nope"))
        self.refuse(s, "unknown connector 'nope'", "worker", "T-1", "--brief", str(s.p.brief))

    def container(self, runner: str, script: str = "exit 0") -> Sides:
        s = self.sides(mode="container", engine_name="fakeengine", runner=runner)
        fake = s.p.bin / "fakeengine"
        fake.write_text(f"#!/bin/sh\n{script}\n")
        fake.chmod(0o755)
        return s

    def test_image_without_a_runner(self) -> None:
        s = self.container("auto")
        self.refuse(s, "the team image needs to know the agent CLI", "worker", "T-1", "--brief", str(s.p.brief),
                    "--runner", "alpha")

    def test_runner_not_in_the_image(self) -> None:
        s = self.container("alpha")
        self.refuse(s, "beta is not in the team image; set runner: beta in .agents/team/bots/worker.md", "worker",
                    "T-1", "--brief", str(s.p.brief), "--runner", "beta")

    def test_unreachable_engine(self) -> None:
        s = self.container("alpha", "echo 'Cannot connect' >&2\nexit 1")
        self.refuse(s, "the container engine is not reachable: Cannot connect (start Docker or Podman",
                    "worker", "T-1", "--brief", str(s.p.brief))

    def test_input_not_found(self) -> None:
        s = self.sides()
        self.refuse(s, "input nope.txt not found", "worker", "T-1", "--brief", str(s.p.brief), "--input", "nope.txt")

    def test_repo_folders(self) -> None:
        s = self.sides()
        missing = Path(s.p.tmp.name) / "missing"
        self.refuse(s, f"--repo {missing}: no such folder", "worker", "T-1", "--brief", str(s.p.brief),
                    "--repo", str(missing))
        plain = Path(s.p.tmp.name) / "plain"
        plain.mkdir()
        self.refuse(s, f"--repo {plain}: not a git repository", "worker", "T-1", "--brief", str(s.p.brief),
                    "--repo", str(plain))

    def test_no_such_commit(self) -> None:
        s = self.sides()
        # A bare name that is no commit names the folder looked for too, unlike main: CallerPathTest.
        self.refuse(s, f"--repo nope: no such commit in {s.p.root.resolve()}", "worker", "T-1", "--brief",
                    str(s.p.brief), "--repo", f"{s.p.root}@nope")

    def test_inputs_that_collide(self) -> None:
        s = self.sides()
        for name in ("one", "two"):
            (s.p.root / name).mkdir()
            (s.p.root / name / "same.txt").write_text(name)
        self.refuse(s, "refusing the inputs:", "worker", "T-1", "--brief", str(s.p.brief),
                    "--input", str(s.p.root / "one" / "same.txt"), "--input", str(s.p.root / "two" / "same.txt"))

    def test_branch_checked_out(self) -> None:
        s = self.sides()
        s.p.git("checkout", "-q", "-b", "wouf/T-1")
        self.refuse(s, "branch wouf/T-1 is checked out", "worker", "T-1", "--brief", str(s.p.brief), "--repo")

    def test_lock_held(self) -> None:
        s = self.sides()
        stage = s.p.root / ".agents" / "team" / "env" / "worker" / "tasks" / "T-1"
        stage.mkdir(parents=True)
        (stage / "run.lock").write_text(json.dumps({"pid": os.getpid()}))
        self.refuse(s, "Worker is already working on T-1", "worker", "T-1", "--brief", str(s.p.brief))

    def test_over_the_days_budget(self) -> None:
        s = self.sides()
        s.p.config.write_text(s.p.config.read_text() + "\n[budget]\nper_day = 0\n")
        self.refuse(s, "over budget: 0/0 tokens today", "worker", "T-1", "--brief", str(s.p.brief), code=4)


class CallerPathTest(unittest.TestCase):
    """Paths as the caller gives them, read from the caller's folder; main's run.py differs here, so only this tree's
    is run."""

    def setUp(self) -> None:
        self.s = Sides(self, runner="alpha")
        self.tmp = Path(self.s.p.tmp.name)

    def run_from(self, cwd: Path, *args: str, spec: bool = False) -> subprocess.CompletedProcess:
        team = str(self.s.p.root / ".agents" / "bin" / "team")
        command = [sys.executable, "-c", DRIVER, team, *args] if spec else [sys.executable, team, "run", *args]
        return subprocess.run(command, env=self.s.p.env, capture_output=True, text=True, timeout=300, cwd=cwd)

    def test_a_relative_repo_path_is_read_from_the_callers_folder(self) -> None:
        other = self.tmp / "ref" / "wouf"
        other.mkdir(parents=True)
        for args in (("init", "-q", "-b", "main"), ("-c", "user.name=T", "-c", "user.email=t@example.invalid",
                                                    "commit", "-q", "--allow-empty", "-m", "start")):
            subprocess.run(["git", "-C", str(other), *args], check=True, capture_output=True)
        head = subprocess.run(["git", "-C", str(other), "rev-parse", "main"], check=True, capture_output=True,
                              text=True).stdout.strip()
        done = self.run_from(self.tmp, "worker", "T-1", "--brief", str(self.s.p.brief), "--repo", "ref/wouf@main",
                             spec=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        spec = json.loads(done.stdout.splitlines()[0])
        self.assertEqual((spec["repo_dir"], spec["commit"]), (str(other.resolve()), head))

    def test_a_missing_relative_repo_path_names_the_resolved_path(self) -> None:
        done = self.run_from(self.tmp, "worker", "T-1", "--brief", str(self.s.p.brief), "--repo", "nope/wouf@main")
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn(f"--repo {(self.tmp / 'nope' / 'wouf').resolve()}: no such folder", done.stderr)
        # Without an @REF too; a name with a slash that is a commit of this folder stays a ref.
        done = self.run_from(self.tmp, "worker", "T-1", "--brief", str(self.s.p.brief), "--repo", "missing/repo")
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertEqual(done.stderr, f"team: --repo {(self.tmp / 'missing' / 'repo').resolve()}: no such folder\n")
        # A PATH@REF without a slash is a path too (review r2 of T-0153).
        done = self.run_from(self.tmp, "worker", "T-1", "--brief", str(self.s.p.brief), "--repo", "missing@main")
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertEqual(done.stderr, f"team: --repo {(self.tmp / 'missing').resolve()}: no such folder\n")
        self.s.p.git("branch", "feature/x")
        done = self.run_from(self.tmp, "worker", "T-1", "--brief", str(self.s.p.brief), "--repo", "feature/x",
                             spec=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        spec = json.loads(done.stdout.splitlines()[0])
        self.assertEqual(spec["commit"], self.s.p.git("rev-parse", "feature/x").strip())

    def test_a_single_name_is_a_ref_or_a_folder_of_the_caller(self) -> None:
        # Neither a commit nor a folder: the error names the folder looked for (review r4 of T-0153).
        done = self.run_from(self.tmp, "worker", "T-1", "--brief", str(self.s.p.brief), "--repo", "missing")
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertEqual(done.stderr, "team: --repo missing: no such commit in this repository (is this folder a git "
                                      f"repository?); no folder {(self.tmp / 'missing').resolve()} either\n")
        # A bare repository of the caller's folder is a path.
        subprocess.run(["git", "clone", "-q", "--bare", str(self.s.p.root), str(self.tmp / "bare")], check=True,
                       capture_output=True)
        head = subprocess.run(["git", "-C", str(self.tmp / "bare"), "rev-parse", "HEAD"], check=True,
                              capture_output=True, text=True).stdout.strip()
        done = self.run_from(self.tmp, "worker", "T-1", "--brief", str(self.s.p.brief), "--repo", "bare", spec=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        spec = json.loads(done.stdout.splitlines()[0])
        self.assertEqual((spec["repo_dir"], spec["commit"]), (str((self.tmp / "bare").resolve()), head))
        # A commit of this folder stays a ref, even with a folder of that name beside the caller.
        self.s.p.git("branch", "keep")
        (self.tmp / "keep").mkdir()
        done = self.run_from(self.tmp, "worker", "T-1", "--brief", str(self.s.p.brief), "--repo", "keep", spec=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIsNone(json.loads(done.stdout.splitlines()[0])["repo_dir"])

    def test_an_input_in_the_tasks_own_out_is_refused_and_out_kept(self) -> None:
        out = self.s.p.root / ".agents" / "team" / "env" / "worker" / "tasks" / "T-1" / "out"
        out.mkdir(parents=True)
        (out / "result.md").write_text("the last run's result\n")
        done = self.run_from(out, "worker", "T-1", "--brief", str(self.s.p.brief), "--input", "result.md")
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertEqual(done.stderr, "team: input result.md lies in T-1's out/, which this run empties: copy it "
                                      "elsewhere first\n")
        self.assertEqual((out / "result.md").read_text(), "the last run's result\n")
        self.assertFalse((out.parent / "run.lock").exists())


SHIPPED = configparser.ConfigParser(interpolation=None)
SHIPPED.read(ROOT / ".agents" / "team" / "config.ini")
KEYRINGS = ".local/share/keyrings"


class LoginPathsTest(unittest.TestCase):
    """[runner.X] login_paths (plan T-0167 §3, board T-0148): a boxed run copies each login path from the member's
    shared /bot/login into its own HOME before the CLI (a regular file, or a folder's regular files), and nothing in
    the sandbox writes /bot/login after it: the host keeps what the CLI changed (taskstore.keep_login,
    tests/test_taskstore.py). A host run's script is unchanged; a path that could leave HOME or the login storage
    stops the run before its lock."""

    def setUp(self) -> None:
        team_module()
        self.engine = sys.modules["teamlib.engine"]

    def sides(self, mode: str, auth: str, paths: str | None) -> Sides:
        """A project whose bots run on agy as shipped (auth, login_paths), with a fake CLI and a fake engine."""
        s = Sides(self, mode=mode, engine_name="fakeengine", runner="agy")
        (s.p.bin / "fakeengine").write_text("#!/bin/sh\nexit 0\n")
        (s.p.bin / "fakeengine").chmod(0o755)
        s.agy = (f"\n[runner.agy]\ninstall =\ncommand = alpha-cli -p {{prompt}}\nhost_command = alpha-cli --safe -p "
                 f"{{prompt}}\nauth = {auth}\n" + ("" if paths is None else f"login_paths = {paths}\n"))
        s.p.config.write_text(s.p.config.read_text() + s.agy)
        return s

    def script(self, s: Sides) -> list[str]:
        done = s.spec("worker", "T-1", "--brief", str(s.p.brief))
        self.assertEqual(done[0], 0, done[2])
        return json.loads(done[1].splitlines()[0])["script"].split("\n")

    def test_the_shipped_config_ships_login_paths_off_for_every_runner(self) -> None:
        # At this version a run keeps no sign-in between runs, so agy's keyring path ships as a comment only.
        self.assertEqual(SHIPPED.get("runner.agy", "auth"), "login")
        for runner in ("claude", "codex", "agy", "local"):
            self.assertFalse(SHIPPED.has_option(f"runner.{runner}", "login_paths"), runner)
        text = (ROOT / ".agents" / "team" / "config.ini").read_text()
        self.assertIn(f"\n# login_paths = {KEYRINGS}\n", text)
        self.assertNotIn(f"\nlogin_paths = {KEYRINGS}", text)

    def test_login_paths_only_with_auth_login(self) -> None:
        ini = configparser.ConfigParser(interpolation=None)
        ini.read_string("[runner.a]\nauth = login\nlogin_paths = .codex/auth.json .local/share/keyrings\n"
                        "[runner.b]\nauth = KEY\nlogin_paths = .codex/auth.json\n[runner.c]\nauth = login\n")
        self.assertEqual(self.engine.login_paths(ini, "a"), [".codex/auth.json", KEYRINGS])
        self.assertEqual((self.engine.login_paths(ini, "b"), self.engine.login_paths(ini, "c")), ([], []))
        self.assertEqual(self.engine.login_link_steps([]), ([], []))
        with self.assertRaises(ValueError):
            self.engine.login_link_steps(["../outside"])

    def test_an_agy_run_copies_its_login_paths_before_the_cli_and_writes_nothing_back_after_it(self) -> None:
        s = self.sides("container", SHIPPED.get("runner.agy", "auth"), KEYRINGS)
        script = self.script(s)
        before, after = self.engine.login_link_steps([KEYRINGS])
        self.assertEqual((len(before), after), (1, []))
        self.assertIn("/bot/login/.local/share/keyrings", before[0])
        self.assertNotIn("ln ", before[0])
        # Copied once HOME exists and before the CLI; nothing after the CLI names the login storage.
        start, cli = script.index(before[0]), script.index('"$@" </dev/null &')
        self.assertLess(script.index('mkdir -p "$HOME" /bot/logs'), start)
        self.assertLess(start, cli)
        self.assertEqual([line for line in script[cli:] if "/bot/login" in line], [])
        self.assertIn('cp -R "$T/out" /outbox/out', script[cli:])

    def test_a_run_without_login_paths_links_nothing(self) -> None:
        for auth, paths in (("login", None), ("login", ""), ("ALPHA_KEY", KEYRINGS)):
            with self.subTest(auth=auth, paths=paths):
                script = "\n".join(self.script(self.sides("container", auth, paths)))
                self.assertNotIn("/bot/login", script)
                self.assertIn('cp -R "$T/out" /outbox/out', script)

    def test_a_login_path_that_could_leave_home_or_the_login_storage_stops_the_run_before_its_lock(self) -> None:
        s = self.sides("container", "login", KEYRINGS)
        stage = s.p.root / ".agents" / "team" / "env" / "worker" / "tasks" / "T-1"
        bad = ("../outside", ".local/../../..", "..", "a..b", "/bot/tasks", ".codex\\auth.json", ".codex/$(id)",
               "a'b", "a\"b", ".", "./x", "a/./b", "a//b", "x/", "~/x", "a*b")
        for path in bad:
            with self.subTest(path=path):
                s.p.config.write_text(s.p.config.read_text().replace(s.agy, s.agy.replace(KEYRINGS, path)))
                done = s.spec("worker", "T-1", "--brief", str(s.p.brief))
                self.assertEqual(done[0], 2, done[2])
                self.assertIn("[runner.agy] login_paths: ", done[2])
                self.assertIn("relative to HOME", done[2])
                self.assertFalse((stage / "run.lock").exists())
                self.assertFalse((stage / "in").exists())
                with self.assertRaises(ValueError):
                    self.engine.login_link_steps([path])
                s.p.config.write_text(s.p.config.read_text().replace(s.agy.replace(KEYRINGS, path), s.agy))

    def test_a_host_runs_script_is_unchanged(self) -> None:
        s = self.sides("host", "login", f"{KEYRINGS} .codex/auth.json")
        main, head = s.both(s.spec, "worker", "T-1", "--brief", str(s.p.brief))
        self.assertEqual(head[0], 0, head[2])
        if main is not None:  # this tree's run.py and main's, byte for byte (nonce normalised)
            self.assertEqual(main, head)
        s.p.config.write_text(s.p.config.read_text().replace(f"login_paths = {KEYRINGS} .codex/auth.json\n", ""))
        self.assertNotIn("login_paths", s.p.config.read_text())
        self.assertEqual(json.loads(head[1].splitlines()[0])["script"].split("\n"), self.script(s))

    def test_the_steps_copy_regular_files_and_never_link_or_follow(self) -> None:
        """The steps themselves, in a shell, with the login storage in a temporary folder: HOME gets regular files
        (a folder's regular files only), no link; the login storage is only read."""
        home, login, task = self.roots()
        paths = [".codex/auth.json", KEYRINGS, ".missing/file", "token.json"]
        before, after = self.engine.login_link_steps(paths)
        self.assertEqual(after, [])
        (login / ".codex").mkdir()
        (login / ".codex" / "auth.json").write_text("v1")
        (login / "token.json").write_text("t1")
        (login / KEYRINGS).mkdir(parents=True)
        (login / KEYRINGS / "login.keyring").write_text("k1")
        (login / KEYRINGS / ".hidden.keyring").write_text("h1")
        (login / KEYRINGS / "sub").mkdir()  # a folder in it, a link and a special file: left out
        (login / KEYRINGS / "sub" / "deep.keyring").write_text("deep")
        (login / KEYRINGS / "link.keyring").symlink_to(task / "task.keyring")
        os.mkfifo(login / KEYRINGS / "pipe")
        stored = self.tree(login)
        self.assertEqual(self.shell(before, home, login), [])
        self.assertEqual(self.tree(home), {".codex": "dir", ".codex/auth.json": "v1", ".local": "dir",
                                           ".local/share": "dir", KEYRINGS: "dir", f"{KEYRINGS}/.hidden.keyring": "h1",
                                           f"{KEYRINGS}/login.keyring": "k1", "token.json": "t1"})
        for path in (".codex/auth.json", "token.json", f"{KEYRINGS}/login.keyring"):
            self.assertTrue((home / path).is_file() and not (home / path).is_symlink(), path)
        self.assertTrue((home / KEYRINGS).is_dir() and not (home / KEYRINGS).is_symlink())
        # The CLI changes its copies; the login storage keeps what it held, and the task folder stays empty.
        self.sign_in(home, ".codex/auth.json", "v2")
        (home / KEYRINGS / "login.keyring").write_text("k2")
        self.assertEqual((self.tree(login), self.tree(task)), (stored, {}))
        # A rerun of the task copies the stored files again over what HOME holds (an earlier copy, or a link that
        # an earlier run left there, never followed).
        (home / "token.json").unlink()
        (task / "task-file").write_text("task file")
        (home / "token.json").symlink_to(task / "task-file")
        (home / KEYRINGS / "extra").write_text("x")
        self.assertEqual(self.shell(before, home, login), [])
        self.assertEqual({k: v for k, v in self.tree(home).items() if v != "dir"},
                         {".codex/auth.json": "v1", f"{KEYRINGS}/.hidden.keyring": "h1",
                          f"{KEYRINGS}/login.keyring": "k1", "token.json": "t1"})
        self.assertEqual((self.tree(login), self.tree(task)), (stored, {"task-file": "task file"}))
        # Nothing stored at a path: HOME keeps what it holds there.
        shutil.rmtree(login / KEYRINGS)
        (home / KEYRINGS / "login.keyring").write_text("k3")
        self.assertEqual(self.shell(before, home, login), [])
        self.assertEqual((home / KEYRINGS / "login.keyring").read_text(), "k3")

    def roots(self) -> tuple[Path, Path, Path]:
        """An empty HOME, login storage and task folder, side by side in a temporary folder."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        for sub in ("home", "login", "task"):
            (tmp / sub).mkdir()
        return tmp / "home", tmp / "login", tmp / "task"

    def shell(self, steps: list[str], home: Path, login: Path) -> list[str]:
        """The steps in sh as the run script runs them (set -e), /bot/login in login and HOME in home: they end
        with 0, and their stderr lines are returned."""
        text = "\n".join(["set -e", *(step.replace("/bot/login", str(login)) for step in steps)])
        done = subprocess.run(["sh", "-c", text], env={**os.environ, "HOME": str(home)}, capture_output=True,
                              text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stderr.splitlines()

    @staticmethod
    def sign_in(home: Path, path: str, text: str) -> None:
        """The CLI writes its sign-in file by rename, through whatever HOME holds on the way."""
        (home / path).parent.mkdir(parents=True, exist_ok=True)
        (home / (path + ".tmp")).write_text(text)
        os.replace(home / (path + ".tmp"), home / path)

    @staticmethod
    def tree(base: Path) -> dict:
        """Every entry under base, a link not followed: its target, a file's text, or 'dir'."""
        return {str(p.relative_to(base)): ("-> " + os.readlink(p) if p.is_symlink() else p.read_text() if p.is_file()
                                           else "dir") for p in sorted(base.rglob("*"))}

    def assert_skipped(self, lines: list[str], *paths: str) -> None:
        self.assertEqual(len(lines), len(paths), lines)
        for line, path in zip(lines, paths):
            self.assertIn(f"login path {path}: ", line)
            self.assertIn("skipped", line)

    def test_a_login_path_whose_leaf_in_the_login_storage_is_a_link_is_skipped(self) -> None:
        """Review r1, finding 1: /bot/login/<path> itself a link (here into a task folder) is never read through
        into HOME; one line on stderr says so."""
        home, login, task = self.roots()
        (task / "keyrings").mkdir()
        (task / "auth.json").write_text("task file")
        (login / ".codex").mkdir()
        (login / ".codex" / "auth.json").symlink_to(task / "auth.json")
        (login / ".local" / "share").mkdir(parents=True)
        (login / KEYRINGS).symlink_to(task / "keyrings")
        planted = self.tree(task), self.tree(login)
        before, after = self.engine.login_link_steps([".codex/auth.json", KEYRINGS])
        lines = self.shell(before, home, login)
        self.assertEqual(self.tree(home), {})  # no copy, and no folder for one
        self.assertEqual((self.tree(task), self.tree(login)), planted)
        self.assert_skipped(lines, ".codex/auth.json", KEYRINGS)
        self.assertEqual(after, [])

    def test_a_parent_link_in_home_or_the_login_storage_is_skipped(self) -> None:
        """Review r1, finding 2: a folder above the path that is a link, in /bot/login or in HOME, at any depth:
        nothing is copied through it before the CLI, and one line on stderr says so. The task folder keeps exactly
        what it held, the login storage too."""
        for side in ("login", "home"):
            for path, linked in ((".codex/auth.json", ".codex"), (".config/agy/auth.json", ".config/agy")):
                with self.subTest(side=side, path=path):
                    home, login, task = self.roots()
                    if side == "login":  # the counterpart, through the link, is a file of the task folder
                        (task / "auth.json").write_text("task file")
                        (login / linked).parent.mkdir(parents=True, exist_ok=True)
                        (login / linked).symlink_to(task)
                    else:  # the counterpart is real; HOME leads into the task folder
                        (login / path).parent.mkdir(parents=True)
                        (login / path).write_text("v1")
                        (home / linked).parent.mkdir(parents=True, exist_ok=True)
                        (home / linked).symlink_to(task)
                    planted = self.tree(task), self.tree(login)
                    before, after = self.engine.login_link_steps([path])
                    lines = self.shell(before, home, login)
                    self.assertFalse(os.path.lexists(home / path))  # no copy, in HOME or in the task folder
                    self.assertEqual((self.tree(task), self.tree(login)), planted)
                    self.assert_skipped(lines, path)
                    self.assertEqual(after, [])

    def test_nothing_in_the_sandbox_writes_the_login_storage(self) -> None:
        """Review r3 of T-0167-2: a check in sh cannot hold while another run of the member swaps a folder of
        /bot/login for a link, so no step writes there. A run's steps around a CLI that changes one sign-in file and
        makes new ones (beside a link planted above one of them) leave the login storage as it was; the CLI's files
        stay in HOME, for the host to keep after the run."""
        home, login, task = self.roots()
        paths = ["token.json", ".codex/auth.json", ".config/agy/auth.json", ".gemini/sub/oauth.json"]
        (login / ".codex").mkdir()
        (login / ".codex" / "auth.json").write_text("v1")
        (login / ".gemini").symlink_to(task)
        planted = self.tree(login), self.tree(task)
        before, after = self.engine.login_link_steps(paths)
        self.assertEqual(after, [])
        self.assert_skipped(self.shell(before, home, login), ".gemini/sub/oauth.json")
        for path in paths:
            self.sign_in(home, path, f"new {path}")
        self.assertEqual(self.shell(after, home, login), [])
        self.assertEqual((self.tree(login), self.tree(task)), planted)
        for path in paths:
            self.assertEqual((home / path).read_text(), f"new {path}")
            self.assertFalse((home / path).is_symlink())


if __name__ == "__main__":
    unittest.main()
