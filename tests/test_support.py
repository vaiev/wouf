"""tests/support.py: a writable copy of a read-only tree that leaves the source as it was, and the time factor."""

from __future__ import annotations

import ast
import concurrent.futures
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402
from support import writable_copy, wouf_env  # noqa: E402

HERE = Path(__file__).resolve().parent


def modes(root: Path) -> dict:
    return {str(p.relative_to(root)): p.lstat().st_mode for p in (root, *root.rglob("*"))}


class WritableCopyTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.src = self.base / "src"
        (self.src / "team").mkdir(parents=True)
        (self.src / "team" / "config.ini").write_text("[team]\n")
        (self.src / "team" / "run.sh").write_text("#!/bin/sh\n")
        (self.src / "team" / "run.sh").chmod(0o555)
        (self.src / "team" / "config.ini").chmod(0o444)
        (self.src / "back").symlink_to(self.src / "team")
        (self.src / "team").chmod(0o555)
        self.addCleanup((self.src / "team").chmod, 0o755)

    def test_a_read_only_tree_is_copied_writable_and_the_source_stays_read_only(self) -> None:
        before = modes(self.src)
        copy = writable_copy(self.src, self.base / "copy", symlinks=True)
        self.assertEqual(copy, self.base / "copy")
        for path in (copy, copy / "team", copy / "team" / "config.ini", copy / "team" / "run.sh"):
            self.assertTrue(path.lstat().st_mode & stat.S_IWUSR, path)
        self.assertTrue((copy / "team" / "run.sh").stat().st_mode & stat.S_IXUSR, "other bits are kept")
        self.assertTrue((copy / "back").is_symlink())
        self.assertEqual(modes(self.src), before, "the source, also through the link back into it, is unchanged")
        (copy / "team" / "config.ini").write_text("[team]\nname = x\n")
        (copy / "team" / "new.txt").write_text("x\n")

    def test_a_read_only_file_is_copied_writable(self) -> None:
        (self.base / "out").mkdir()
        copy = writable_copy(self.src / "team" / "config.ini", self.base / "out" / "config.ini")
        self.assertTrue(copy.stat().st_mode & stat.S_IWUSR)
        self.assertEqual((self.src / "team" / "config.ini").stat().st_mode & 0o777, 0o444)


class TimeFactorTest(unittest.TestCase):
    def factor(self, value: str | None) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if k != "WOUF_TEST_TIME_FACTOR"}
        if value is not None:
            env["WOUF_TEST_TIME_FACTOR"] = value
        return subprocess.run([sys.executable, "-c", "import support; print(support.TIME_FACTOR)"], cwd=HERE,
                              env=env, capture_output=True, text=True)

    def test_default_set_and_refused(self) -> None:
        self.assertEqual(self.factor(None).stdout, "1.0\n")
        self.assertEqual(self.factor("").stdout, "1.0\n")
        self.assertEqual(self.factor("3").stdout, "3.0\n")
        for bad in ("0.5", "-1", "nan", "inf"):
            done = self.factor(bad)
            self.assertNotEqual(done.returncode, 0, bad)
            self.assertIn("WOUF_TEST_TIME_FACTOR must be a number from 1 to 5", done.stderr)

    def test_only_a_finite_number_from_1_to_the_cap(self) -> None:
        self.assertEqual(support.TIME_FACTOR_CAP, 5)
        for text, factor in ((None, 1), ("", 1), ("1", 1), ("2.5", 2.5), ("5", 5), ("5.0", 5)):
            with self.subTest(text=text):
                self.assertEqual(support.time_factor(text), factor)
        for bad in ("inf", "Infinity", "-inf", "nan", "0.5", "0", "-1", "5.01", "6", "1e9", "three", "3 s"):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "from 1 to 5"):
                support.time_factor(bad)


def isolating_modules() -> list[str]:
    """Every test module that clears the caller's WOUF_* variables (isolate_wouf_env or wouf_env), found by reading
    tests/: a module that starts to use the helper is guarded without anyone listing it here."""
    return sorted(p.stem for p in HERE.glob("test_*.py")
                  if p.name != "test_support.py"
                  and re.search(r"\b(isolate_wouf_env|wouf_env)\(", p.read_text("utf-8")))


def in_process_modules() -> list[str]:
    """The modules that run teamlib in this process under wouf_env (test_budget, test_run_usage, test_usage wrote fake
    receipts to a sandbox's /bot/logs this way): these few run again in a hostile environment. Every module
    clears the caller's WOUF_* in setUpModule (checked below) and get none from scripts/test-parallel.py either, so
    running all of them a second time inside the suite only doubled its load (the gate's container ran out of room)."""
    return sorted(p.stem for p in HERE.glob("test_*.py")
                  if p.name != "test_support.py" and re.search(r"(?<!isolate_)\bwouf_env\(", p.read_text("utf-8")))


def isolates_in_setup(path: Path) -> bool:
    """Whether the module's setUpModule calls isolate_wouf_env (support.isolate_wouf_env or the bare name)."""
    for node in ast.parse(path.read_text("utf-8")).body:
        if isinstance(node, ast.FunctionDef) and node.name == "setUpModule":
            for call in ast.walk(node):
                if isinstance(call, ast.Call):
                    name = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
                    if name == "isolate_wouf_env":
                        return True
    return False


def setUpModule() -> None:
    support.isolate_wouf_env()


class WoufEnvTest(unittest.TestCase):
    def test_no_wouf_variable_the_test_does_not_set_survives_and_all_return_after(self) -> None:
        caller = {"WOUF_LOG_DIR": "/caller/logs", "WOUF_STATE_DB": "/caller/db", "WOUF_PASS_PREFIX": "p", "KEEP": "1"}
        with unittest.mock.patch.dict(os.environ, caller):
            before = dict(os.environ)
            with wouf_env(WOUF_TEAM_DIR="/own/team", WOUF_LOG_DIR="/own/logs"):
                self.assertEqual({k: v for k, v in os.environ.items() if k.startswith("WOUF_")},
                                 {"WOUF_TEAM_DIR": "/own/team", "WOUF_LOG_DIR": "/own/logs"})
                self.assertEqual(os.environ["KEEP"], "1")
                self.assertEqual(os.environ["PATH"], before["PATH"])
            self.assertEqual(dict(os.environ), before)

    def test_start_and_stop_work_as_for_patch_dict(self) -> None:
        with unittest.mock.patch.dict(os.environ, {"WOUF_LOG_DIR": "/caller/logs"}):
            env = wouf_env(WOUF_TEAM_DIR="/own/team")
            env.start()
            self.assertNotIn("WOUF_LOG_DIR", os.environ)
            env.stop()
            self.assertEqual(os.environ["WOUF_LOG_DIR"], "/caller/logs")
            self.assertNotIn("WOUF_TEAM_DIR", os.environ)

    def test_secret_store_points_the_file_backend_at_a_temp_folder_until_the_module_is_done(self) -> None:
        code = ("import os, sys, unittest\nsys.path.insert(0, sys.argv[1])\nimport support\n"
                "support.isolate_wouf_env(secret_store=True)\n"
                "folder = os.environ['WOUF_SECRET_DIR']\n"
                "print(os.environ['WOUF_SECRET_BACKEND'], os.path.isdir(folder), sorted(k for k in os.environ "
                "if k.startswith('WOUF_')))\n"
                "support.do_module_cleanups()\n"
                "print(os.path.exists(folder), os.environ.get('WOUF_SECRET_DIR'), os.environ.get('WOUF_LOG_DIR'))\n")
        env = {**os.environ, "WOUF_SECRET_DIR": "/caller/secrets", "WOUF_LOG_DIR": "/caller/logs"}
        done = subprocess.run([sys.executable, "-c", code, str(HERE)], env=env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.splitlines(), ["file True ['WOUF_SECRET_BACKEND', 'WOUF_SECRET_DIR']",
                                                    "False /caller/secrets /caller/logs"])


class SuiteStaysInItsFoldersTest(unittest.TestCase):
    """Whatever WOUF_* a caller has (a bot sandbox sets WOUF_LOG_DIR=/bot/logs), the in-process modules neither fail
    nor write there: run with the log, events and team folders all one empty folder, it stays empty. Every module
    that clears the caller's WOUF_* does so in its setUpModule, for all its tests (a test's own wouf_env adds to it)."""

    def test_every_isolating_module_clears_the_callers_variables_in_set_up_module(self) -> None:
        modules = isolating_modules()
        self.assertGreaterEqual(len(modules), 25)
        for module in modules:
            with self.subTest(module=module):
                self.assertTrue(isolates_in_setup(HERE / f"{module}.py"),
                                f"{module}: no isolate_wouf_env() in setUpModule")

    def run_module(self, module: str, guard: str, scratch: str) -> tuple[str, list]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("WOUF_")}
        env.update(WOUF_LOG_DIR=guard, WOUF_EVENTS_DIR=guard, WOUF_TEAM_DIR=guard, TMPDIR=scratch,
                   WOUF_STATE_DB=f"{guard}/state.sqlite", WOUF_SECRET_BACKEND="pass", WOUF_PASS_PREFIX="caller",
                   WOUF_MASK='["lead"]')
        done = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(HERE), "-p", f"{module}.py"],
                              env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace")
        # the verdict line of unittest (a module's own output may follow it)
        last = next((line for line in reversed(done.stderr.splitlines()) if re.match(r"(OK|FAILED)\b", line)), "")
        left = sorted(str(p.relative_to(guard)) for p in Path(guard).rglob("*"))
        return last if re.fullmatch(r"OK( \(skipped=\d+\))?", last) else f"{last}\n{done.stderr[-2000:]}", left

    def test_the_in_process_modules_pass_and_leave_the_callers_folders_empty(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            modules = in_process_modules()
            self.assertGreaterEqual(len(modules), 3)

            def one(module: str) -> tuple[str, list]:
                guard, scratch = (Path(root) / module / name for name in ("guard", "scratch"))
                guard.mkdir(parents=True)
                scratch.mkdir()
                return self.run_module(module, str(guard), str(scratch))

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results = dict(zip(modules, pool.map(one, modules)))
        for module, (last, left) in results.items():
            with self.subTest(module=module):
                self.assertTrue(last.startswith("OK"), f"{module}: {last}")
                self.assertEqual(left, [], f"{module} wrote to the caller's folder")


if __name__ == "__main__":
    unittest.main()
