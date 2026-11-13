"""Tests for teamlib.gate_series: the completeness gate of a change that comes in packages (T-0189). Each test builds
its own repository (main, a feature branch, a work branch), writes a brief with a Series line and runs team gate on
it; the verdict per case of the brief's table, the malformed lines, the upgrade test the gate runs itself, the
fields of gate.json, the help, the plug-in into teamlib.gate, and that a receipt ignores those fields."""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_receipt_build  # noqa: E402
from support import isolate_wouf_env  # noqa: E402
from test_team_cli import Project, team_module  # noqa: E402

SUITE = "echo suite-ran"
# Upgrade tests the gate runs on the head: they print where they ran, so the log shows it was the gate's checkout.
PASSING = ("import os\nimport unittest\n\n\nclass Upgrade(unittest.TestCase):\n    def test_upgrade(self):\n"
           "        print('upgrade-ran', os.environ['WOUF_HEAD'], os.path.isfile('.agents/VERSION'))\n")
FAILING = ("import unittest\n\n\nclass Upgrade(unittest.TestCase):\n    def test_upgrade(self):\n"
           "        self.fail('the upgrade broke')\n")
LATER = "package {k} of {n} cannot land on main alone; land the series on a feature branch, then one complete change"
NO_TEST = "series complete but no green upgrade test"
COMPLETE = {".agents/VERSION": "0.3.0\n", ".agents/upgrade/0.3.0.md": "# 0.3.0\n\nFrom: 0.2.0\n",
            "tests/test_upgrade_0_3_0.py": PASSING}
# gate.json of a gate with a command, as the base writes it (no complexity line in this suite's output).
BASE_KEYS = {"task", "head", "base", "outside", "code", "started", "state", "why", "network", "command_sha256",
             "ended"}


def setUpModule() -> None:
    isolate_wouf_env()


class SeriesGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.p = Project(mode="host")
        self.addCleanup(self.p.close)
        self.p.config.write_text(self.p.config.read_text() + f"\n[flow]\ngate = {SUITE}\n")
        self.repo = Path(self.p.tmp.name) / "target"
        self.repo.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.git("init", "-q")
        self.git("symbolic-ref", "HEAD", "refs/heads/main")
        for key, value in (("user.name", "Test"), ("user.email", "test@example.invalid"), ("commit.gpgsign", "false")):
            self.git("config", key, value)
        self.main = self.commit({".agents/VERSION": "0.2.0\n", ".agents/upgrade/0.2.0.md": "# 0.2.0\n",
                                 "README.md": "start\n"})
        self.git("checkout", "-q", "-b", "series")
        self.feature = self.commit({"docs/series.md": "the series' own first commit\n"})
        self.brief = Path(self.p.tmp.name) / "brief.md"

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True, text=True,
                              env=self.env).stdout.strip()

    def commit(self, files: dict) -> str:
        for name, text in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            self.git("add", name)
        self.git("commit", "-q", "-m", "change")
        return self.git("rev-parse", "HEAD")

    def work(self, files: dict, base: str | None = None) -> str:
        """The work branch, off base (default main), with files: the package the gate checks."""
        self.git("checkout", "-q", "-B", "work", base or self.main)
        return self.commit(files)

    def gate(self, lines: str = "", base: str | None = None) -> tuple[subprocess.CompletedProcess, dict]:
        self.brief.write_text(f"Task: T-1\nObjective: one package of a change\n{lines}", encoding="utf-8")
        done = self.p.team("gate", "T-1", "--repo", f"{self.repo}@work", "--base", base or self.main,
                           "--brief", str(self.brief))
        path = self.folder() / "gate.json"
        self.assertTrue(path.is_file(), f"team gate wrote no gate.json:\n{done.stdout[-2000:]}{done.stderr[-2000:]}")
        return done, json.loads(path.read_text())

    def folder(self) -> Path:
        return self.p.root / ".agents" / "team" / "env" / "gate" / "T-1"

    def log(self) -> str:
        path = self.folder() / "gate.log"
        return path.read_text() if path.exists() else ""

    def assert_red(self, done: subprocess.CompletedProcess, state: dict, why: str, suite: bool = False) -> None:
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertEqual((state["state"], state["why"]), ("red", why))
        self.assertIn(f"gate T-1 red: {why}", done.stdout)
        self.assertEqual("suite-ran" in self.log(), suite, "the suite runs only when the series check passed")

    def assert_green(self, done: subprocess.CompletedProcess, state: dict) -> None:
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual((state["state"], state["why"]), ("green", "gate command passed"))
        self.assertIn("suite-ran", self.log())

    # ---- the table: brief, base, migration, upgrade test -> verdict

    def test_a_brief_without_series_gates_as_at_the_base(self) -> None:
        for name, base in (("main", self.main), ("branch", self.feature)):
            with self.subTest(base=name):
                self.work({"README.md": "a change without migration or upgrade test\n"}, base)
                done, state = self.gate("", base)
                self.assert_green(done, state)
                self.assertEqual(set(state), BASE_KEYS, "no series field without a Series line")
                self.assertNotIn("upgrade test", self.log())
        self.p.set(f"gate = {SUITE}", "gate = echo suite-ran; exit 3")
        done, state = self.gate("", self.main)
        self.assert_red(done, state, "gate command exit 3", suite=True)
        self.assertEqual(set(state), BASE_KEYS)

    def test_package_k_of_n_on_main_is_red_whatever_its_diff(self) -> None:
        for files in ({"README.md": "package 3\n"}, COMPLETE):
            with self.subTest(files=sorted(files)):
                self.work(files)
                done, state = self.gate("Series: T-0167 3/5\n")
                self.assert_red(done, state, LATER.format(k=3, n=5))
                self.assertEqual(state["series"], {"name": "T-0167", "package": 3, "of": 5, "main": True})
                self.assertEqual((state["migration"], state["upgrade_test"]), ("not checked", "not checked"))
        self.work({"README.md": "package 1\n"})
        self.assert_red(*self.gate("Series: T-0167 1/2\n"), LATER.format(k=1, n=2))

    def test_package_k_of_n_on_a_feature_branch_gates_as_at_the_base(self) -> None:
        self.work({"README.md": "package 3\n"}, self.feature)
        done, state = self.gate("Series: T-0167 3/5\n", self.feature)
        self.assert_green(done, state)
        self.assertEqual(state["series"], {"name": "T-0167", "package": 3, "of": 5, "main": False})
        self.assertEqual((state["migration"], state["upgrade_test"]), ("not checked", "not checked"))
        self.p.set(f"gate = {SUITE}", "gate = echo suite-ran; exit 3")
        self.assert_red(*self.gate("Series: T-0167 3/5\n", self.feature), "gate command exit 3", suite=True)

    def test_the_complete_package_on_main_with_migration_and_a_green_upgrade_test_is_green(self) -> None:
        head = self.work(COMPLETE)
        done, state = self.gate("Series: T-0167 5/5\n")
        self.assert_green(done, state)
        self.assertEqual(state["series"], {"name": "T-0167", "package": 5, "of": 5, "main": True})
        self.assertEqual(state["migration"], ".agents/upgrade/0.3.0.md")
        self.assertEqual(state["upgrade_test"], {"files": ["tests/test_upgrade_0_3_0.py"], "code": 0})
        self.assertEqual(set(state), BASE_KEYS | {"series", "migration", "upgrade_test"})
        self.assertEqual(state["code"], 0)
        # the gate ran it itself, after the suite, on the head's own checkout
        log = self.log()
        self.assertLess(log.index("suite-ran"), log.index("--- upgrade test: python3 -m unittest "
                                                          "tests.test_upgrade_0_3_0: exit 0 ---"))
        self.assertIn(f"upgrade-ran {head} True", log)
        self.assertTrue((self.folder() / "upgrade" / "T-1" / "gate.log").is_file())
        # green only if the rest is green: a red suite is red, and the upgrade test does not run
        self.p.set(f"gate = {SUITE}", "gate = echo suite-ran; exit 3")
        done, state = self.gate("Series: T-0167 5/5\n")
        self.assert_red(done, state, "gate command exit 3", suite=True)
        self.assertEqual(state["upgrade_test"], {"files": ["tests/test_upgrade_0_3_0.py"], "code": None})
        self.assertNotIn("upgrade-ran", self.log())

    def test_the_complete_package_on_main_without_its_migration_is_red(self) -> None:
        self.work({k: v for k, v in COMPLETE.items() if k != ".agents/upgrade/0.3.0.md"})
        for lines in ("", "Migration: none, a reason does not replace the migration of a new VERSION\n"):
            with self.subTest(lines=lines):
                done, state = self.gate(f"Series: T-0167 5/5\n{lines}")
                self.assert_red(done, state, "series complete but no migration .agents/upgrade/0.3.0.md")
                self.assertEqual((state["migration"], state["upgrade_test"]), ("missing", "not checked"))
        # the migration of another version is not this one
        self.work({**COMPLETE, ".agents/upgrade/0.3.0.md": "", ".agents/VERSION": "0.4.0\n"})
        self.assert_red(*self.gate("Series: T-0167 5/5\n"), "series complete but no migration .agents/upgrade/0.4.0.md")

    def test_the_complete_package_on_main_without_an_upgrade_test_is_red(self) -> None:
        self.work({k: v for k, v in COMPLETE.items() if k != "tests/test_upgrade_0_3_0.py"})
        done, state = self.gate("Series: T-0167 5/5\n")
        self.assert_red(done, state, NO_TEST)
        self.assertEqual((state["migration"], state["upgrade_test"]), (".agents/upgrade/0.3.0.md", "missing"))
        # a file of another name is no upgrade test
        self.work({**{k: v for k, v in COMPLETE.items() if k != "tests/test_upgrade_0_3_0.py"},
                   "tests/test_migration_0_3_0.py": PASSING})
        self.assert_red(*self.gate("Series: T-0167 5/5\n"), NO_TEST)

    def test_the_complete_package_on_main_with_a_failing_upgrade_test_is_red(self) -> None:
        self.work({**COMPLETE, "tests/test_upgrade_0_3_0.py": FAILING})
        done, state = self.gate("Series: T-0167 5/5\n")
        self.assert_red(done, state, NO_TEST, suite=True)
        self.assertEqual(state["code"], 0, "the suite passed; the upgrade test did not")
        self.assertEqual(state["upgrade_test"]["files"], ["tests/test_upgrade_0_3_0.py"])
        self.assertNotIn(state["upgrade_test"]["code"], (0, None))
        self.assertIn("the upgrade broke", done.stdout, "the red output shows the upgrade test's failure")

    def test_an_upgrade_test_that_runs_no_test_is_red(self) -> None:
        # unittest exits 0 for a module without tests, and for one whose tests are all skipped: neither is a green
        # upgrade test (the review's reproduction: an empty tests/test_upgrade_0_3_0.py came out green, code 0)
        empty = "import unittest\n"
        skipped = ("import unittest\n\n\nclass Upgrade(unittest.TestCase):\n    @unittest.skip('later')\n"
                   "    def test_upgrade(self):\n        pass\n")
        for body in (empty, skipped):
            with self.subTest(body=body):
                self.work({**COMPLETE, "tests/test_upgrade_0_3_0.py": body})
                done, state = self.gate("Series: T-0167 5/5\n")
                self.assert_red(done, state, NO_TEST, suite=True)
                self.assertNotEqual(state["upgrade_test"]["code"], 0, "no upgrade test ran and passed")
                self.assertNotIn("upgrade-ran", self.log())

    def test_the_complete_package_with_version_unchanged_needs_migration_none_with_a_reason(self) -> None:
        self.work({"tests/test_upgrade_0_2_0_series.py": PASSING, "README.md": "the series, complete\n"})
        why = ("series complete but no migration .agents/upgrade/0.2.0.md (VERSION is unchanged: or the brief's line "
               "Migration: none REASON)")
        for lines in ("", "Migration: none\n", "Migration: none -\n", "Migration: nonesuch reason\n",
                      "Migration: none, one reason\nMigration: none, another\n"):
            with self.subTest(lines=lines):
                done, state = self.gate(f"Series: T-0167 5/5\n{lines}")
                self.assert_red(done, state, why)
                self.assertEqual(state["migration"], "missing")
        done, state = self.gate("Series: T-0167 5/5\nMigration: none, the series changes no installed file\n")
        self.assert_green(done, state)
        self.assertEqual(state["migration"], "none")
        self.assertEqual(state["upgrade_test"], {"files": ["tests/test_upgrade_0_2_0_series.py"], "code": 0})

    def test_a_base_commit_on_main_counts_as_main_and_gate_main_names_the_branch(self) -> None:
        self.work({"README.md": "package 2\n"})
        self.git("checkout", "-q", "main")
        self.commit({"later.md": "main moved on\n"})
        self.assert_red(*self.gate("Series: T-0167 2/3\n", self.main), LATER.format(k=2, n=3))
        # [gate] main names the default branch: the feature branch's own commit is on trunk, not on main
        self.git("branch", "trunk", "series")
        self.work({"README.md": "package 2\n"}, self.feature)
        self.assert_green(*self.gate("Series: T-0167 2/3\n", self.feature))
        self.p.config.write_text(self.p.config.read_text() + "\n[gate]\nmain = trunk\n")
        self.assert_red(*self.gate("Series: T-0167 2/3\n", self.feature), LATER.format(k=2, n=3))
        self.p.set("main = trunk", "main = gone")
        self.assert_red(*self.gate("Series: T-0167 2/3\n", self.feature),
                        "series: no branch gone ([gate] main) in the repository, so the gate cannot tell whether the "
                        "base is on main")

    # ---- malformed Series lines: red on a feature branch, where a good line would be green

    def assert_malformed(self, line: str, problem: str) -> None:
        self.work({"README.md": "a package\n"}, self.feature)
        done, state = self.gate(line, self.feature)
        self.assert_red(done, state, f"malformed Series line: {problem} (Series: NAME K/N, NAME a task id, "
                                     "1 <= K <= N <= 20)")
        self.assertEqual(state["series"], "invalid")

    def test_a_series_line_with_k_greater_than_n_is_red(self) -> None:
        self.assert_malformed("Series: T-0167 4/3\n", "K 4 is greater than N 3")

    def test_a_series_line_with_a_zero_is_red(self) -> None:
        for line in ("Series: T-0167 0/3\n", "Series: T-0167 1/0\n", "Series: T-0167 0/0\n"):
            with self.subTest(line=line):
                self.assert_malformed(line, "K and N count from 1")

    def test_a_series_line_without_its_slash_is_red(self) -> None:
        for line in ("Series: T-0167 3 5\n", "Series: T-0167 35\n", "Series: T-0167\n", "Series:\n",
                     "Series: T-0167 3/5 more\n"):
            with self.subTest(line=line):
                self.assert_malformed(line, "not NAME K/N")
        self.assert_malformed("Series: ../T-0167 3/5\n", "NAME is no task id")

    def test_a_series_line_with_more_than_twenty_packages_is_red(self) -> None:
        self.assert_malformed("Series: T-0167 3/21\n", "N 21 is more than 20")
        self.work({"README.md": "a package\n"}, self.feature)
        self.assert_green(*self.gate("Series: T-0167 20/20\n", self.feature))

    def test_two_series_lines_are_red(self) -> None:
        self.assert_malformed("Series: T-0167 1/2\nSeries: T-0167 2/2\n", "2 Series lines, a brief has one")

    def test_k_and_n_are_one_or_two_ascii_digits_else_the_line_is_malformed_and_gate_json_is_written(self) -> None:
        # review T-0189.r2: int() of 5000 nines raised ValueError, so team gate wrote no gate.json; a sign, a non-ASCII
        # digit or whitespace inside the numbers is no K/N either (the gate() helper asserts gate.json exists)
        for label, line in (("K of 5000 nines", "Series: T-0167 " + "9" * 5000 + "/3\n"),
                            ("N of 5000 nines", "Series: T-1 1/" + "9" * 5000 + "\n"),
                            ("Arabic-Indic digits", "Series: T-1 \u0661/\u0663\n"), ("minus", "Series: T-1 -1/3\n"),
                            ("plus", "Series: T-1 +1/3\n"), ("space", "Series: T-1 1 /3\n"),
                            ("tab", "Series: T-1 1/\t3\n")):
            with self.subTest(line=label):
                self.assert_malformed(line, "not NAME K/N")
        # a leading zero is still two ASCII digits: 01/03 is 1/3
        self.work({"README.md": "a package\n"}, self.feature)
        done, state = self.gate("Series: T-1 01/03\n", self.feature)
        self.assert_green(done, state)
        self.assertEqual(state["series"], {"name": "T-1", "package": 1, "of": 3, "main": False})
        self.work({"README.md": "a package\n"})
        self.assert_red(*self.gate("Series: T-1 01/03\n"), LATER.format(k=1, n=3))

    # ---- help and plug-in

    def test_gate_help_names_the_series_and_migration_lines(self) -> None:
        for flag in ("--help", "-h"):
            done = self.p.team("gate", flag)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("usage: team gate TASK", done.stdout)
            self.assertIn("Series: NAME K/N", done.stdout)
            self.assertIn("Migration: none REASON", done.stdout)


class PlugInTest(unittest.TestCase):
    def test_bin_team_fills_the_series_hook_and_the_gate_stops_without_it(self) -> None:
        team_module().registry()  # what bin/team does before a command: every teamlib module imported
        gate = importlib.import_module("teamlib.gate")
        self.assertEqual(gate.SERIES, [importlib.import_module("teamlib.gate_series")])
        kept = gate.SERIES[:]
        self.addCleanup(lambda: gate.SERIES.__setitem__(slice(None), kept))
        gate.SERIES[:] = []
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as stopped:
            gate.series("plan", None, None, None, "a" * 40, "b" * 40)
        self.assertNotEqual(stopped.exception.code, 0)
        self.assertIn("teamlib.gate_series", err.getvalue())


class ReceiptTest(test_receipt_build.Records):
    """The signed receipt format stays as it is (carrying the series fields is T-0192): a receipt reads its gate
    through evidence.gate_step, which keeps none of them."""

    def test_a_receipt_from_a_gate_json_with_the_series_fields_is_byte_for_byte_the_one_without(self) -> None:
        attest, receipt = test_receipt_build.attest, test_receipt_build.receipt
        self.happy()
        landed = self.squash()
        gate_json = self.team / "env" / "gate" / test_receipt_build.TASK / "gate.json"
        plain = json.loads(gate_json.read_text())
        # as team gate writes them: package N/N on main, green; package K<N on a feature branch, green
        added = ({"series": {"name": "T-0167", "package": 5, "of": 5, "main": True},
                  "migration": ".agents/upgrade/0.3.0.md",
                  "upgrade_test": {"files": ["tests/test_upgrade_0_3_0.py"], "code": 0}},
                 {"series": {"name": "T-0167", "package": 3, "of": 5, "main": False}, "migration": "not checked",
                  "upgrade_test": "not checked"})
        gate_step = [step for step in self.steps if step["kind"] == "gate"]
        with mock.patch.object(attest, "stamp", return_value=attest.stamp()):
            for evidence in ("the flow's gate step", "gate.json alone"):
                if evidence == "gate.json alone":
                    self.steps[:] = [step for step in self.steps if step not in gate_step]
                    self.flow()
                self.gate_json(plain)
                without = receipt.canonical(self.build(landed))
                for fields in added:
                    with self.subTest(evidence=evidence, fields=fields):
                        self.gate_json({**plain, **fields})
                        self.assertEqual(receipt.canonical(self.build(landed)), without)


if __name__ == "__main__":
    unittest.main()
