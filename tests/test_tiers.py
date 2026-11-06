"""Tests for the three tiers light, mid and strong: the model a tier names on a runner, mid's fall back to strong
when a runner leaves it empty, the tier a run's receipt names, and the refusal of an unknown tier (T-0019-17)."""

from __future__ import annotations

import configparser
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_team_cli import FAKE_CLI, KEY, SOURCE, Project  # noqa: E402

sys.path.insert(0, str(SOURCE / "bin"))
from teamlib import engine  # noqa: E402


class ResolveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ini = configparser.ConfigParser(interpolation=None)
        self.ini.read_string("[runner.alpha]\nstrong = big\nmid = medium\nlight = small\n"
                             "[runner.beta]\nstrong = big\nmid =\nlight = small\n"
                             "[runner.gamma]\nstrong =\nlight =\n")

    def test_each_tier_names_its_own_model(self) -> None:
        for tier, model in (("light", "small"), ("mid", "medium"), ("strong", "big")):
            with self.subTest(tier=tier):
                self.assertEqual(engine.tier_model(self.ini, "alpha", tier), (model, tier))

    def test_an_empty_or_missing_mid_falls_back_to_strong(self) -> None:
        self.assertEqual(engine.tier_model(self.ini, "beta", "mid"), ("big", "mid→strong"))
        self.assertEqual(engine.tier_model(self.ini, "gamma", "mid"), ("", "mid→strong"))

    def test_the_label_names_the_tier_actually_used(self) -> None:
        self.assertEqual(engine.tier_label("mid", "strong"), "mid→strong")
        self.assertEqual(engine.tier_label("mid", "mid"), "mid")
        self.assertEqual(engine.tier_label("light", "light"), "light")

    def test_a_model_given_for_the_run_wins(self) -> None:
        self.assertEqual(engine.tier_model(self.ini, "beta", "mid", "exact-model-1"), ("exact-model-1", "mid"))

    def test_an_unknown_tier_is_refused(self) -> None:
        for tier in ("medium", "Strong", "light "):
            with self.subTest(tier=tier), self.assertRaises(SystemExit):
                engine.tier_model(self.ini, "alpha", tier)

    def test_the_three_tiers(self) -> None:
        self.assertEqual(engine.TIERS, ("light", "mid", "strong"))


class RunTierTest(unittest.TestCase):
    def setUp(self) -> None:
        self.p = Project(mode="container")

    def tearDown(self) -> None:
        self.p.close()

    def plan(self, *args: str, member: str = "worker") -> dict:
        return self.p.plan(member, "T-1", "--brief", str(self.p.brief), *args)

    def test_mid_uses_the_runners_mid_model(self) -> None:
        self.p.set("light = small\nauth = ALPHA_KEY", "light = small\nmid = medium\nauth = ALPHA_KEY")
        plan = self.plan("--tier", "mid")
        self.assertEqual(plan["argv"][-2:], ["--model", "medium"])
        self.assertEqual(plan["tier"], "mid")

    def test_mid_without_a_model_runs_on_strong(self) -> None:
        plan = self.plan("--tier", "mid")
        self.assertEqual(plan["argv"][-2:], ["--model", "big"])
        self.assertEqual(plan["tier"], "mid→strong")
        beta = self.plan("--tier", "mid", "--runner", "beta")
        self.assertNotIn("-m", beta["argv"])
        self.assertEqual(beta["tier"], "mid→strong")

    def test_an_explicit_model_wins_over_the_tier(self) -> None:
        plan = self.plan("--tier", "mid", "--model", "exact-model-1")
        self.assertEqual(plan["argv"][-2:], ["--model", "exact-model-1"])
        self.assertEqual(plan["tier"], "mid")

    def test_scout_and_bugfixer_default_to_mid(self) -> None:
        for member in ("scout", "bugfixer"):
            with self.subTest(member=member):
                self.assertIn("\ntier: mid\n", self.p.bot_file(member).read_text())
                self.assertEqual(self.plan(member=member)["tier"], "mid→strong")

    def test_an_unknown_tier_is_refused(self) -> None:
        for tier in ("medium", "MID", "strong "):
            with self.subTest(tier=tier):
                result = self.p.team("run", "worker", "T-1", "--brief", str(self.p.brief), "--tier", tier,
                                     "--dry-run")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("light, mid or strong", result.stderr)

    def test_runners_shows_the_mid_model(self) -> None:
        self.p.set("light = small\nauth = ALPHA_KEY", "light = small\nmid = medium\nauth = ALPHA_KEY")
        result = self.p.team("runners")
        self.assertEqual(result.returncode, 0, result.stderr)
        alpha = next(line for line in result.stdout.splitlines() if " alpha " in line)
        beta = next(line for line in result.stdout.splitlines() if " beta " in line)
        self.assertIn("mid=medium", alpha)
        self.assertIn("mid=strong", beta)


class ReceiptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.p = Project(mode="host", runner="fake")
        fake = self.p.root / ".agents" / "team" / "env" / "worker"
        fake.mkdir(parents=True)
        (fake / "fake-cli.sh").write_text(FAKE_CLI)
        self.p.store_secret("ALPHA_KEY", KEY)

    def tearDown(self) -> None:
        self.p.close()

    def test_the_receipt_names_the_tier_actually_used(self) -> None:
        result = self.p.team("run", "worker", "T-1", "--brief", str(self.p.brief), "--tier", "mid")
        self.assertEqual(result.returncode, 0, result.stderr)
        log = subprocess.run([sys.executable, str(self.p.root / ".agents" / "bin" / "log"), "task", "T-1"],
                             capture_output=True, text=True, env=self.p.env).stdout
        self.assertIn("worker via fake mid→strong default", log)


if __name__ == "__main__":
    unittest.main()
