"""prepare() in teamlib/run.py, split into smaller functions (T-0037c), gives main's spec and main's refusals.

The golden side is main's run.py, taken with `git show` in this run (PREPARE_GOLDEN_REF, main or origin/main); only
that one file is swapped into the same throwaway project, so the two sides differ in prepare() alone. Both sides start
from the same copy of the project. Normalised: `nonce` (os.urandom per run). The spec holds no time; the commit, the
paths and the secrets' names are the same on both sides and compared as they are."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_team_cli import ROOT, Project  # noqa: E402

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
        shutil.copytree(self.p.root, snap, symlinks=True)
        main = None
        if MAIN is not None:
            (self.p.root / RUN_PY).write_bytes(MAIN)
            main = call(*args)
            shutil.rmtree(self.p.root)
            shutil.copytree(snap, self.p.root, symlinks=True)
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
        self.refuse(s, "--repo nope: no such commit in this repository", "worker", "T-1", "--brief",
                    str(s.p.brief), "--repo", "nope")
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


if __name__ == "__main__":
    unittest.main()
