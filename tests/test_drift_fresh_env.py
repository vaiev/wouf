"""Regression tests for the fresh-start boundary: a session that starts with no inherited WOUF_* variables.

Drift causes pinned (werkstatt docs/notes/2026-11-10-drift-forensics.md numbering): 3 stale autonomy memories read as
live, 4 nested developer rules (a session started in a folder under another one). A new session must find its own
project from where it stands, never through a path a former session left in WOUF_TEAM_DIR or WOUF_LOG_DIR. Two parts:
the boot's entry points (`team context`, `team doctor`, `log status`, `federate status`) run from the project folder
with an env of PATH, HOME and LANG only; and the suites a contributor runs directly with `python -m unittest` (not
through scripts/test-parallel.py, which strips WOUF_* itself) run under a hostile WOUF_* env.

What this proves: no child of a direct run gets a caller WOUF_* variable (the recorded environment of Python children and of bwrap or the fake bwrap); no file is left in the decoy folders after the run; and no Python-level open for writing into a decoy happens during the run. What it does not prove: a write by a non-Python child that is removed before the run ends, or a write anywhere else. Both need an OS sandbox (drift cause 4/5 class, out of scope here).

How: a recorder in every Python the run starts notes each child and each test handed a caller WOUF_* and each
Python-level open for writing into a decoy; a fake bwrap notes the WOUF_* it is started with; a snapshot of the run's
temp tree before and after shows what is left in the decoys. For the boot's entry points, whose env is clean and names
no decoy, the decoy is compared before and after only. Also not proved: any model behaviour, a host's shell profile or
a hook's own environment. `team doctor` is report-only here: its paths are asserted, not its verdict. No model is
called; every test runs offline in a temp project."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import isolate_wouf_env, wouf_env, writable_copy  # noqa: E402
from test_team_cli import Project  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
# Every variable a session may have inherited from a former one; each hostile value holds DECOY.
HOSTILE = ("WOUF_TEAM_DIR", "WOUF_LOG_DIR", "WOUF_EVENTS_DIR", "WOUF_STATE_DB", "WOUF_SECRET_BACKEND",
           "WOUF_PASS_PREFIX", "WOUF_SECRET_DIR", "WOUF_KNOWLEDGE_DB", "WOUF_CACHE")
# Run directly, as the PR template tells contributors: python -m unittest tests.<module>.
DIRECT = ("test_verify", "test_receipt_export", "test_guard", "test_project_phases", "test_bwrap_gate")
# Written as sitecustomize into the run's tools folder, once per run, with @RECORD@ and @DECOY@ filled in; it is loaded
# into the run and every Python it starts through PYTHONPATH. It notes each child process handed a WOUF_* variable
# with DECOY in it, each test that sees one in its own process, and each Python-level open for writing into the decoy
# folder (DECOY, the run's own). It watches nothing else: not a write by a child that is not Python, and not a write
# outside the decoy folder.
RECORDER = '''\
import os
import subprocess
import sys
import unittest.case

RECORD = @RECORD@
DECOY = @DECOY@
WRITING = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
PATHS = {"os.mkdir": (0,), "os.remove": (0,), "os.rmdir": (0,), "os.rename": (0, 1), "os.link": (0, 1),
         "os.symlink": (1,), "os.truncate": (0,), "os.chmod": (0,), "os.utime": (0,)}
busy = False


def line(text):
    with open(RECORD, "a") as handle:
        handle.write(text + "\\n")


def note(where, env):
    bad = sorted(k for k, v in env.items() if k.startswith("WOUF_") and "DECOY" in v)
    if bad:
        line(f"{where}: {' '.join(bad)}")


def watched(path):
    real = os.path.realpath(os.fsdecode(path))
    return real == DECOY or real.startswith(DECOY + os.sep)


def audit(event, args):
    global busy
    if busy:
        return
    busy = True
    try:
        if event == "open":
            mode, flags = args[1], args[2]
            writes = (isinstance(mode, str) and set(mode) & set("wax+")) or (isinstance(flags, int) and flags & WRITING)
            paths = (args[0],) if writes else ()
        else:
            paths = tuple(args[i] for i in PATHS.get(event, ()) if i < len(args))
        for path in paths:
            if isinstance(path, (str, bytes, os.PathLike)) and watched(path):
                line(f"write {event} {os.path.realpath(os.fsdecode(path))}")
    finally:
        busy = False


popen_init, test_run = subprocess.Popen.__init__, unittest.case.TestCase.run


def init(self, *args, **kwargs):
    note(f"child {args[0] if args else kwargs.get('args')}", os.environ if kwargs.get("env") is None else kwargs["env"])
    popen_init(self, *args, **kwargs)


def run(self, *args, **kwargs):
    note(f"test {self.id()}", os.environ)
    return test_run(self, *args, **kwargs)


subprocess.Popen.__init__, unittest.case.TestCase.run = init, run
sys.addaudithook(audit)
'''
# A bwrap that cannot start a sandbox (so bwrap_usable() is false, as on a host without user namespaces) and says its
# version; it notes every call, and the WOUF_* variables with DECOY in them that it was started with.
FAKE_BWRAP = '''#!/bin/sh
echo "$*" >> @CALLS@
bad=$(env | sed -n 's/^\\(WOUF_[A-Z_]*\\)=.*DECOY.*/\\1/p' | sort | tr '\\n' ' ')
[ -z "$bad" ] || echo "bwrap $1: $bad" >> @RECORD@
[ "$1" = "--version" ] && { echo "bubblewrap 0.8.0"; exit 0; }
exit 1
'''


def setUpModule() -> None:
    isolate_wouf_env()


def fingerprint(folder: Path) -> dict[str, str]:
    """Every path under folder with the hash of its bytes (folders: empty): equal before and after means untouched."""
    return {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else ""
            for p in sorted(folder.rglob("*"))}


class FreshBootTest(unittest.TestCase):
    """The entry points of the boot, from the project folder, with a clean env, beside a decoy of a former session."""

    def setUp(self) -> None:
        self.p = Project(mode="host", runner="fake")
        self.addCleanup(self.p.close)
        self.root = self.p.root.resolve()
        self.team_dir = self.root / ".agents" / "team"
        self.p.config.write_text(self.p.config.read_text() + "\n[federation]\nname = own-federation\npeers = peer\n")
        self.p.team("task", "new", "own-row", "--class", "standard")
        # the decoy: a whole team dir and a log dir of a former session, beside the project, with rows of its own
        parent = self.root.parent
        self.decoy = parent / "decoy"
        self.decoy_team = writable_copy(self.team_dir, self.decoy / "team")
        for name in ("events", "env"):  # a former session's own store would show its own-row; the decoy has a board
            shutil.rmtree(self.decoy_team / name, ignore_errors=True)
        self.decoy_logs = self.decoy / "logs"
        self.decoy_logs.mkdir()
        (self.decoy_logs / "worker.jsonl").write_text(
            '{"t": "2026-10-20T09:00:00.000+00:00", "who": "worker", "task": "T-9", "outcome": "done", '
            '"signal": "decoy-receipt", "ref": "", "cost": ""}\n')
        config = self.decoy_team / "config.ini"
        config.write_text(config.read_text().replace("own-federation", "decoy-federation"))
        (self.decoy_team / "board.md").write_text(
            (self.team_dir / "board.md").read_text().replace("own-row", "decoy-row"))
        self.home = parent / "home"
        self.home.mkdir()
        self.clean = {"PATH": os.environ["PATH"], "HOME": str(self.home), "LANG": "C.UTF-8"}

    def enter(self, command: str, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(self.root / ".agents" / "bin" / command), *args],
                              env=self.clean if env is None else env, cwd=self.root, capture_output=True, text=True,
                              timeout=120)

    def former_session(self):
        """The caller's env as a former session left it: WOUF_TEAM_DIR and WOUF_LOG_DIR name the decoy."""
        return wouf_env(WOUF_TEAM_DIR=str(self.decoy_team), WOUF_LOG_DIR=str(self.decoy_logs))

    def assert_decoy_untouched(self, before: dict, *outputs: subprocess.CompletedProcess) -> None:
        self.assertEqual(fingerprint(self.decoy), before)
        for done in outputs:
            self.assertNotIn(str(self.decoy), done.stdout + done.stderr)
            self.assertNotIn("decoy", done.stdout + done.stderr)

    def test_the_control_inherited_variables_do_lead_to_the_decoy(self) -> None:
        """Without this the tests below could pass on a decoy nothing would ever read."""
        with self.former_session():
            inherited = {**os.environ, "PATH": self.clean["PATH"]}
        context = self.enter("team", "context", env=inherited)
        self.assertIn("decoy-row", context.stdout)
        self.assertNotIn("own-row", context.stdout)
        self.assertIn("decoy-receipt", self.enter("log", "status", env=inherited).stdout)
        self.assertIn("decoy-federation", self.enter("federate", "status", env=inherited).stdout)

    def test_team_context_finds_the_projects_own_team_dir(self) -> None:
        before = fingerprint(self.decoy)
        with self.former_session():
            done = self.enter("team", "context")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertRegex(done.stdout, r"\| T-1 \| own-row \|")
        self.assert_decoy_untouched(before, done)

    def test_team_doctor_names_the_projects_own_folder(self) -> None:
        """Report-only: the verdict (exit code, items to fix) depends on the host; the paths it names do not."""
        before = fingerprint(self.decoy)
        with self.former_session():
            done = self.enter("team", "doctor")
        self.assertIn(f"cd {self.root} && .agents/bin/secret set ALPHA_KEY", done.stdout, done.stdout + done.stderr)
        self.assert_decoy_untouched(before, done)

    def test_log_finds_the_projects_own_log_dir(self) -> None:
        before = fingerprint(self.decoy)
        with self.former_session():
            added = self.enter("log", "add", "worker", "T-1", "done", "own-receipt")
            status = self.enter("log", "status")
        self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
        self.assertIn("own-receipt", (self.team_dir / "logs" / "worker.jsonl").read_text())
        self.assertIn("own-receipt", status.stdout)
        self.assertNotIn("decoy-receipt", status.stdout)
        self.assert_decoy_untouched(before, added, status)

    def test_federate_finds_the_projects_own_team_dir(self) -> None:
        before = fingerprint(self.decoy)
        with self.former_session():
            done = self.enter("federate", "status")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("federation own-federation", done.stdout)
        self.assert_decoy_untouched(before, done)

    def test_the_boot_leaves_the_projects_own_state_alone(self) -> None:
        """Reading is not writing: the boot's three reads change nothing in the team dir but its caches."""
        def state() -> dict:
            return {k: v for k, v in fingerprint(self.team_dir).items() if not k.startswith("env")}
        before = state()
        for command in (("team", "context"), ("team", "doctor"), ("log", "status")):
            self.enter(*command)
        self.assertEqual(state(), before)


class DirectRunTest(unittest.TestCase):
    """`python -m unittest tests.<module>` with the caller's WOUF_* naming a decoy: no child gets one, no file is left in
    the decoys, and no Python-level open for writing into a decoy happens.

    Each run has a folder of its own, BASE/runs/<module>/, with a fresh empty cwd, HOME, TMPDIR and decoy in it. Two
    things see these: the recorder in every Python the run starts notes each Python-level open for writing into the
    decoy, and a snapshot of BASE before and after shows a file left in it (it also shows a file left beside the run
    folders; that is a side check, not part of what is proved). A write by a non-Python child that is removed before
    the run ends is seen by neither, and nor is a write anywhere else: both need an OS sandbox and are not claimed.
    A fake bwrap, first on PATH (which makes the run the same whether this host has bubblewrap or not), notes each call
    and any WOUF_* variable with DECOY in it it is started with."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name) / "base"
        self.folder = self.base / "runs"
        self.folder.mkdir(parents=True)
        tools = Path(tmp.name) / "tools"  # beside BASE, so the snapshot of BASE does not hold what the recorder notes
        tools.mkdir()
        self.record, self.calls = tools / "record", tools / "bwrap-calls"
        fake = tools / "bin" / "bwrap"
        fake.parent.mkdir()
        fake.write_text(FAKE_BWRAP.replace("@RECORD@", str(self.record)).replace("@CALLS@", str(self.calls)))
        fake.chmod(0o755)
        self.tools = tools
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("WOUF_")}
        self.env.update(PATH=str(fake.parent) + os.pathsep + os.environ["PATH"], PYTHONDONTWRITEBYTECODE="1")

    def unittest(self, *names: str, path: Path = ROOT) -> subprocess.CompletedProcess:
        """The run of the modules `names` (found through path) in its own folder, with a hostile WOUF_* env."""
        run = self.folder / names[0]
        for name in ("cwd", "home", "tmp", "DECOY"):
            (run / name).mkdir(parents=True)
        # the recorder of this run, with the decoy it watches baked in: a child that drops the env still has it
        (self.tools / "sitecustomize.py").write_text(
            RECORDER.replace("@RECORD@", repr(str(self.record))).replace("@DECOY@", repr(os.path.realpath(run / "DECOY"))))
        env = {**self.env, **{name: str(run / "DECOY" / name.lower()) for name in HOSTILE},
               "HOME": str(run / "home"), "TMPDIR": str(run / "tmp"),
               "PYTHONPATH": os.pathsep.join([str(self.tools), str(path), os.environ.get("PYTHONPATH", "")])}
        return subprocess.run([sys.executable, "-m", "unittest", *names], env=env, cwd=run / "cwd",
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=600)

    def snapshot(self) -> dict[str, str]:
        """BASE as it is now (the tools folder, with the recorder and the fake bwrap, is not in it)."""
        return fingerprint(self.base)

    def changed(self, run: str, before: dict) -> list[str]:
        """The paths of BASE that differ from `before` other than in the run's own four empty folders, and those
        four if anything was left in them (the decoys included). Only BASE is looked at."""
        run = f"runs/{run}"
        empty = {run: "", **{f"{run}/{name}": "" for name in ("cwd", "home", "tmp", "DECOY")}}
        after = self.snapshot()
        return sorted(k for k in set(after) | set(before)
                      if after.get(k) != (empty.get(k) if k == run or k.startswith(run + "/") else before.get(k)))

    def seen(self) -> str:
        """What the recorder and the fake bwrap noted since the last call (a child or test handed a caller WOUF_*, a
        write into a decoy), as at most five lines."""
        found = self.record.read_text().splitlines() if self.record.exists() else []
        self.record.unlink(missing_ok=True)
        return "\n".join(found[:5] + ([f"... {len(found) - 5} more"] if len(found) > 5 else []))

    def test_the_recorder_sees_a_module_that_passes_the_callers_variables_on(self) -> None:
        """The control: a module written the way the five were is caught, so a silent record means something."""
        leaky = self.base / "leaky"
        leaky.mkdir()
        (leaky / "test_leaky.py").write_text(textwrap.dedent("""\
            import os, subprocess, sys, unittest

            class Leaky(unittest.TestCase):
                def test_child(self):
                    subprocess.run([sys.executable, "-c", "pass"], env={**os.environ, "WOUF_TEAM_DIR": "x"})

                def test_bwrap(self):
                    subprocess.run(["bwrap", "--version"], capture_output=True)
            """))
        done = self.unittest("test_leaky", path=leaky)
        self.assertEqual(done.returncode, 0, done.stderr)
        seen = self.seen()
        self.assertIn("test test_leaky.Leaky.test_child: ", seen)
        self.assertIn("child ", seen)
        self.assertIn("WOUF_LOG_DIR", seen)
        self.assertIn("bwrap --version: ", seen, "the fake bwrap notes the env it is started with")

    def test_a_write_into_the_run_folder_or_beside_it_in_base_is_seen(self) -> None:
        """The control for the snapshot: a module that leaves a file in its decoy, beside its temp dirs, above them and
        beside the runs, written by the test or by a child, changes the snapshot of BASE, so an unchanged snapshot means
        something. Only BASE is looked at; this proves nothing about a write outside it."""
        stray = self.base / "stray"
        stray.mkdir()
        (stray / "test_stray.py").write_text(textwrap.dedent("""\
            import os, subprocess, sys, unittest
            from pathlib import Path

            class Stray(unittest.TestCase):
                def test_write(self):
                    Path(os.environ["STRAY"]).write_text("x")

                def test_child_write(self):
                    code = "import os; open(os.environ['STRAY2'], 'w').write('x')"
                    subprocess.run([sys.executable, "-c", code], check=True)
            """))
        run = self.folder / "test_stray"  # the folder the run below gets; its own four are empty ones inside it
        cases = (("into the decoy", run / "DECOY" / "stray.txt"), ("beside the temp dirs", run / "stray.txt"),
                 ("above them", self.folder / "stray.txt"), ("beside the runs", self.base / "outside-snapshot"))
        for where, path in cases:
            with self.subTest(where=where):
                shutil.rmtree(run, ignore_errors=True)
                before = self.snapshot()
                self.env["STRAY"] = self.env["STRAY2"] = str(path)
                done = self.unittest("test_stray", path=stray)
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertNotEqual(self.changed("test_stray", before), [], "the snapshot of BASE")

    def test_a_write_into_a_decoy_is_seen_even_when_removed_again(self) -> None:
        """The control for a Python-level write into a decoy that is removed again before the run ends: the snapshot
        cannot see it (it is gone when the snapshot is taken), the recorder does, in the test's process and in a Python
        child's. A write that stays is seen by both. A write by a non-Python child that is removed again is seen by
        neither, and is not claimed."""
        probe = self.base / "probe"
        probe.mkdir()
        (probe / "test_probe.py").write_text(textwrap.dedent("""\
            import os, subprocess, sys, unittest
            from pathlib import Path

            class Probe(unittest.TestCase):
                def test_write_then_remove(self):
                    path = Path(os.environ["WOUF_TEAM_DIR"])
                    path.write_text("x")
                    path.unlink()

                def test_child_write_then_remove(self):
                    code = ("import os; p = os.environ['WOUF_LOG_DIR']; open(p, 'w').write('x'); os.remove(p)")
                    subprocess.run([sys.executable, "-c", code], check=True)

                def test_write_and_keep(self):
                    Path(os.environ["WOUF_EVENTS_DIR"]).write_text("x")
            """))
        before = self.snapshot()
        done = self.unittest("test_probe", path=probe)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        decoy = (self.folder / "test_probe" / "DECOY").resolve()
        self.assertEqual(self.changed("test_probe", before), ["runs/test_probe/DECOY/wouf_events_dir"],
                         "the snapshot sees the write that stays and nothing else")
        noted = self.record.read_text()
        for target in ("wouf_team_dir", "wouf_log_dir", "wouf_events_dir"):
            self.assertIn(f"write open {decoy / target}", noted, "the recorder")
        self.assertIn(f"write os.remove {decoy / 'wouf_team_dir'}", noted)
        self.assertIn(f"write os.remove {decoy / 'wouf_log_dir'}", noted)
        self.assertNotIn(f"write os.remove {decoy / 'wouf_events_dir'}", noted)

    def test_direct_runs_ignore_the_callers_wouf_variables_and_leave_the_decoys_empty(self) -> None:
        for name in DIRECT:
            with self.subTest(module=name):
                before = self.snapshot()
                done = self.unittest(f"tests.{name}")
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertFalse(noted := self.seen(),
                                 f"{name} hands a caller WOUF_* on or opens a decoy to write:\n{noted}")
                self.assertEqual(self.changed(f"tests.{name}", before), [],
                                 f"{name} left a file in a decoy, beside the run folders in BASE, or in its temp dirs")

    def test_the_fake_bwrap_stands_in_for_the_host_one(self) -> None:
        """bwrap_usable() is asked, with the clean env, whether the host has bubblewrap or not: the gate suite asks it
        and, the fake being unable to start a sandbox, skips its real-bwrap tests."""
        done = self.unittest("tests.test_bwrap_gate")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertFalse(handed := self.seen(), f"the gate suite hands WOUF_* on:\n{handed}")
        self.assertIn("--version", self.calls.read_text(), "the gate suite asked a bwrap, and it was the fake")


if __name__ == "__main__":
    unittest.main()
