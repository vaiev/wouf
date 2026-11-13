"""Token caps per run and per day (T-0027): [budget] in config.ini. Over the day's cap a run is not started (skipped,
the flow waits like a limit until the next UTC midnight); a run past per_run is marked over-budget and its flow
waits for the lead. Without [budget] nothing changes. Runs go through a fake engine."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import isolate_wouf_env, wouf_env  # noqa: E402
from test_engine_calls import FAKE_ENGINE, MARKER  # noqa: E402
from test_flow_parts import team_module as flow_module  # noqa: E402
from test_team_cli import KEY, Project  # noqa: E402

BIN = Path(__file__).resolve().parents[1] / ".agents" / "bin"
sys.path.insert(0, str(BIN))
from teamlib import engine  # noqa: E402
from teamlib import stats  # noqa: E402

SIGN = '    print("WOUF Worker · T-1 · ready-for-review")\n'
# The fake engine's container prints the run's numbers as the usage step would, when the test gives them.
USAGE_ENGINE = FAKE_ENGINE.replace(SIGN, SIGN + '    if (state / "usage").exists():\n'
                                   '        print((state / "usage").read_text().strip())\n')
NOW = "2026-10-25T08:00:00+00:00"


def receipt(when: datetime, signal: str, cost: str, task: str = "T-9") -> str:
    return json.dumps({"t": when.isoformat(timespec="milliseconds"), "who": "lead", "task": task, "outcome": "done",
                       "signal": signal, "ref": "", "cost": cost}) + "\n"


def today(**delta) -> datetime:
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return start + timedelta(minutes=1, **delta)


def setUpModule() -> None:
    # No test of this module sees a WOUF_* of the caller; a test's own wouf_env(...) adds what it sets.
    isolate_wouf_env()


class TokensTodayTest(unittest.TestCase):
    """The day's sum from the lead's receipts: today's (UTC) only, per bot by the signal's first word."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.team = Path(tmp.name)
        (self.team / "logs").mkdir()
        env = wouf_env(WOUF_TEAM_DIR=str(self.team))
        env.start()
        self.addCleanup(env.stop)

    def test_today_per_bot_and_all(self) -> None:
        (self.team / "logs" / "lead.jsonl").write_text(
            receipt(today(), "worker returned, branch wouf/T-9", "tokens=10/20/30/40")
            + receipt(today(), "review returned", "tokens=1/1/1/1")
            + receipt(today(), "worker via claude exit 1, see output.log", "tokens=unknown")
            + receipt(today(), "worker via claude strong default (container)", "")
            + receipt(today(days=-1), "worker returned", "tokens=1000/0/0/0")
            + "not json\n" + json.dumps(["a list"]) + "\n"
            + receipt(today(), "worker returned", "tokens=1/2/3/99999999999999"))
        self.assertEqual(engine.tokens_today(), 104)
        self.assertEqual(engine.tokens_today("worker"), 100)
        self.assertEqual(engine.tokens_today("review"), 4)
        self.assertEqual(engine.tokens_today("decide"), 0)

    def test_no_receipts_is_zero(self) -> None:
        self.assertEqual(engine.tokens_today(), 0)

    def test_the_per_run_mark(self) -> None:
        self.assertEqual(engine.over_run({"per_run": 100}, "50/50/0/1"), "101/100")
        self.assertIsNone(engine.over_run({"per_run": 100}, "50/50/0/0"), "at the cap is not past it")
        self.assertIsNone(engine.over_run({"per_run": 100}, "unknown"))
        self.assertIsNone(engine.over_run({"per_run": None}, "500/0/0/0"))
        self.assertEqual(engine.budget_mark("worker returned, branch b", "101/100"),
                         "worker over-budget 101/100 tokens, returned, branch b")
        self.assertEqual(engine.budget_mark("worker returned", None), "worker returned")

    def test_a_cap_that_is_not_a_number_is_refused(self) -> None:
        ini = engine.configparser.ConfigParser(interpolation=None)
        ini.read_string("[budget]\nper_day = 1000\nper_run =\nper_day.worker = 500\n")
        self.assertEqual(engine.budget_caps(ini), {"per_day": 1000, "per_day.worker": 500}, "empty is no cap")
        self.assertEqual(engine.budget_caps(engine.configparser.ConfigParser(interpolation=None)), {})
        for bad in ("per_day = 5k", "per_run = -1", "per_day.review = 1e6"):
            ini = engine.configparser.ConfigParser(interpolation=None)
            ini.read_string(f"[budget]\n{bad}\n")
            err = io.StringIO()
            with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
                engine.budget_caps(ini)
            self.assertIn("a number of tokens", err.getvalue())


class BudgetRunTest(unittest.TestCase):
    """team run through a fake engine, with today's receipts written first."""

    def setUp(self) -> None:
        self.p = Project(mode="container", engine_name="fakeengine", runner="alpha")
        self.addCleanup(self.p.close)
        self.p.store_secret("ALPHA_KEY", MARKER)
        # The runner's CLI is claude: the run's numbers are read from its output.
        self.p.set("host_command = alpha-cli --safe", "host_command = claude --safe")
        self.state = Path(self.p.tmp.name) / "engine"
        self.state.mkdir()
        fake = self.p.bin / "fakeengine"
        fake.write_text(USAGE_ENGINE.format(python=sys.executable, state=str(self.state)))
        fake.chmod(0o755)
        self.team = self.p.root / ".agents" / "team"
        self.stage = self.team / "env" / "worker" / "tasks" / "T-1"
        (self.team / "logs").mkdir(exist_ok=True)

    def budget(self, text: str) -> None:
        self.p.config.write_text(self.p.config.read_text() + f"\n[budget]\n{text}\n")

    def spent(self, *rows: tuple) -> None:
        with (self.team / "logs" / "lead.jsonl").open("a") as log:
            for signal, cost in rows:
                log.write(receipt(today(), signal, cost))

    def run_worker(self):
        return self.p.team("run", "worker", "T-1", "--brief", str(self.p.brief), "--repo")

    def result(self) -> dict:
        return json.loads((self.stage / "result.json").read_text())

    def receipts(self) -> list:
        return [json.loads(line) for line in (self.team / "logs" / "lead.jsonl").read_text().splitlines()]

    def wait_receipt(self, **expected) -> dict:
        """Wait for this run's final receipt, including an in-progress JSONL write."""
        expected = {"task": "T-1", **expected}
        deadline = time.monotonic() + 10
        last = None
        while True:
            try:
                last = self.receipts()[-1]
            except (FileNotFoundError, IndexError, json.JSONDecodeError):
                pass  # The receipt may not exist yet or may still be getting written.
            if last is not None and all(last.get(key) == value for key, value in expected.items()):
                return last
            if time.monotonic() >= deadline:
                self.fail(f"Timed out waiting for receipt {expected!r}; last receipt: {last!r}")
            time.sleep(.05)

    def started(self) -> bool:
        calls = self.state / "calls.jsonl"
        return calls.exists() and any(json.loads(c)["argv"][0] == "start" for c in calls.read_text().splitlines())

    def assert_skipped(self, done, why: str) -> None:
        self.assertEqual(done.returncode, 4, done.stdout + done.stderr)
        self.assertFalse(self.started(), "the bot was not started")
        result = self.result()
        self.assertEqual((result["state"], result["error"]), ("skipped", why))
        midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        self.assertEqual(result["until"], midnight.isoformat(timespec="seconds"))
        self.assertIn(why, done.stderr)
        shown = self.p.team("result", "worker", "T-1")
        self.assertEqual(shown.returncode, 4, shown.stdout + shown.stderr)
        self.assertIn(f"skipped T-1: {why}", shown.stderr)
        last = self.wait_receipt(outcome="limited")
        self.assertEqual((last["outcome"], last["cost"]), ("limited", ""), "no numbers: it is no run")
        self.assertIn(why, last["signal"])
        self.assertFalse((self.stage / "run.lock").exists())

    def test_no_budget_section_changes_nothing(self) -> None:
        (self.state / "usage").write_text("wouf-usage tokens=900000/0/0/1 skipped=0")
        self.spent(("worker returned", "tokens=9000000/0/0/0"))
        done = self.run_worker()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.result()["state"], "done")
        self.assertIsNone(self.result()["over_budget"])
        self.assertEqual(self.wait_receipt(signal="worker returned")["signal"], "worker returned")
        self.assertNotIn("budget", done.stdout + done.stderr)

    def test_under_per_day_the_run_starts(self) -> None:
        self.budget("per_day = 1000")
        self.spent(("review returned", "tokens=100/200/300/399"))
        done = self.run_worker()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(self.started())
        self.assertEqual(self.result()["state"], "done")

    def test_at_per_day_the_run_is_skipped(self) -> None:
        self.budget("per_day = 1000")
        self.spent(("review returned", "tokens=100/200/300/400"))
        self.assert_skipped(self.run_worker(), "over budget: 1000/1000 tokens today")

    def test_over_per_day_the_run_is_skipped(self) -> None:
        self.budget("per_day = 1000")
        self.spent(("review returned", "tokens=100/200/300/400"), ("worker returned", "tokens=1/0/0/0"))
        self.assert_skipped(self.run_worker(), "over budget: 1001/1000 tokens today")

    def test_yesterday_does_not_count(self) -> None:
        self.budget("per_day = 1000")
        with (self.team / "logs" / "lead.jsonl").open("a") as log:
            log.write(receipt(today(days=-1), "worker returned", "tokens=5000/0/0/0"))
        self.assertEqual(self.run_worker().returncode, 0)
        self.assertEqual(self.result()["state"], "done")

    def test_a_bots_own_cap(self) -> None:
        self.budget("per_day = 100000\nper_day.worker = 500")
        self.spent(("review returned", "tokens=9000/0/0/0"))
        done = self.run_worker()
        self.assertEqual(done.returncode, 0, "another bot's tokens do not count against the worker's own cap")
        self.spent(("worker returned", "tokens=500/0/0/0"))
        (self.state / "calls.jsonl").unlink()
        self.assert_skipped(self.run_worker(), "over budget: 500/500 tokens today")

    def test_a_cap_that_is_not_a_number_stops_before_the_lock(self) -> None:
        self.budget("per_day = 1000\nper_day.review = lots")
        done = self.run_worker()
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("[budget] per_day.review = lots: a number of tokens", done.stderr)
        self.assertFalse(self.started(), "the bot was not started")
        self.assertFalse((self.stage / "run.lock").exists(), "no stale lock")
        self.assertFalse((self.stage / "result.json").exists(), "no run was recorded")
        self.p.set("per_day.review = lots", "per_day.review = 5")
        self.assertEqual(self.run_worker().returncode, 0, "fixed, the next run starts at once")

    def test_per_run_marks_the_receipt_and_the_result(self) -> None:
        self.budget("per_run = 1000")
        (self.state / "usage").write_text("wouf-usage tokens=400/500/50/60 skipped=0")
        done = self.run_worker()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        result = self.result()
        self.assertEqual((result["state"], result["over_budget"]), ("done", "1010/1000"), "marked, not cut off")
        last = self.wait_receipt(signal="worker over-budget 1010/1000 tokens, returned")
        self.assertEqual(last["signal"], "worker over-budget 1010/1000 tokens, returned")
        self.assertEqual(last["cost"], "tokens=400/500/50/60")
        self.assertIn("over-budget: 1010/1000 tokens in this run", done.stderr)
        self.assertIn("over-budget", self.p.team("result", "worker", "T-1").stderr)
        # team stats still reads the bot from the marked receipt.
        self.assertEqual(stats_bots(self.team), [("worker", "T-1", 1010)])

    def test_at_per_run_no_mark(self) -> None:
        self.budget("per_run = 1010")
        (self.state / "usage").write_text("wouf-usage tokens=400/500/50/60 skipped=0")
        self.assertEqual(self.run_worker().returncode, 0)
        self.assertIsNone(self.result()["over_budget"])
        last = self.wait_receipt(signal="worker returned")
        self.assertEqual(last["signal"], "worker returned")


def stats_bots(team: Path) -> list:
    with wouf_env(WOUF_TEAM_DIR=str(team)):
        return stats.bot_runs(None)


class BudgetFlowTest(unittest.TestCase):
    """The flow: a skipped step waits like a limit until its reset, without a fallback; an over-budget step blocks."""

    def setUp(self) -> None:
        self.team = flow_module()
        self.opts = {"bot": "worker", "runner": None, "tier": "strong", "model": None, "effort": "medium",
                     "reviews": 1, "review_runners": [], "scout": False}

    def next(self, steps: list, at: str = NOW) -> dict:
        return self.team.flow_next({"steps": steps}, self.opts, {"claude": "codex"}, at)

    def test_a_skipped_step_waits_until_midnight_without_a_fallback(self) -> None:
        skipped = {"kind": "worker", "bot": "worker", "outcome": "skipped", "runner": "claude",
                   "until": "2026-10-26T00:00:00+00:00", "why": "over budget: 10/10 tokens today"}
        self.assertEqual(self.next([skipped]), {"end": "limited", "until": "2026-10-26T00:00:00+00:00",
                                                "why": "over budget: 10/10 tokens today"})
        # After the reset it runs again, on its own runner; once.
        self.assertEqual(self.next([skipped], "2026-10-26T00:00:01+00:00"), {"do": "worker"})

    def test_a_skipped_review_waits_too(self) -> None:
        steps = [{"kind": "worker", "outcome": "done", "status": "ready", "head": "h"},
                 {"kind": "gate", "outcome": "green", "head": "h"},
                 {"kind": "review", "index": 1, "outcome": "skipped", "until": "2026-10-26T00:00:00+00:00"}]
        self.assertEqual(self.next(steps)["end"], "limited")
        self.assertEqual(self.next(steps, "2026-10-27T00:00:00+00:00"), {"do": "review", "index": 1})

    def test_a_skipped_scout_does_not_hold_the_worker(self) -> None:
        self.assertEqual(self.next([{"kind": "scout", "outcome": "skipped", "until": "2026-10-26T00:00:00+00:00"}]),
                         {"do": "worker"})

    def test_per_run_blocks_the_flow(self) -> None:
        steps = [{"kind": "worker", "outcome": "done", "status": "ready", "head": "h", "over_budget": "1010/1000"}]
        self.assertEqual(self.next(steps), {"end": "blocked", "why": "over the per-run budget"})
        steps = [{**steps[0], "over_budget": None}, {"kind": "gate", "outcome": "green", "head": "h"}]
        self.assertEqual(self.next(steps)["do"], "review")
        steps.append({"kind": "review", "index": 1, "outcome": "done", "verdict": "approve", "head": "h",
                      "over_budget": "5/1"})
        self.assertEqual(self.next(steps), {"end": "blocked", "why": "over the per-run budget"})

    def test_the_step_takes_the_mark_and_the_reason_from_the_result(self) -> None:
        with tempfile.TemporaryDirectory() as folder, wouf_env(WOUF_TEAM_DIR=folder):
            module = self.team
            module.record = lambda *a, **k: None
            module.subprocess = type("NoRun", (), {"run": staticmethod(lambda *a, **k: None), "STDOUT": None})
            module.flow_brief = lambda *a: Path(folder) / "brief.md"
            module.head_of = lambda flow: "h"
            module.bot = lambda member: {"id": member, "name": "Worker"}
            stage = module.stage_of("worker", "T-1")
            stage.mkdir(parents=True)
            (stage / "run.json").write_text(json.dumps({"runner": "claude", "tier": "strong"}))
            ini = module.configparser.ConfigParser(interpolation=None)
            flow = {"task": "T-1", "steps": [], "repo": folder, "base": "b", "branch": "wouf/T-1", "class": "standard"}
            for result, expected in (
                    ({"state": "done", "over_budget": "1010/1000"}, {"over_budget": "1010/1000"}),
                    ({"state": "skipped", "until": "2026-10-26T00:00:00+00:00", "error": "over budget: 1/1 tokens today"},
                     {"why": "over budget: 1/1 tokens today", "until": "2026-10-26T00:00:00+00:00"})):
                (stage / "result.json").write_text(json.dumps(result))
                step = module.flow_step(ini, flow, {"do": "worker"}, Path(folder), {**self.opts, "bot": "worker"})
                for key, value in expected.items():
                    self.assertEqual(step[key], value)
                self.assertEqual(step["outcome"], result["state"])


class BudgetFlowEndToEndTest(unittest.TestCase):
    """team flow in host mode: over the day's budget the worker is not started and the flow ends limited."""

    def test_the_flow_ends_limited_until_midnight(self) -> None:
        p = Project(mode="host", runner="fake")
        self.addCleanup(p.close)
        team = p.root / ".agents" / "team"
        (team / "env" / "worker").mkdir(parents=True)
        (team / "env" / "worker" / "fake-cli.sh").write_text("echo ran > out/ran.txt\n")
        p.store_secret("ALPHA_KEY", KEY)
        p.config.write_text(p.config.read_text() + "\n[budget]\nper_day = 10\n")
        (team / "logs").mkdir(exist_ok=True)
        (team / "logs" / "lead.jsonl").write_text(receipt(today(), "review returned", "tokens=5/5/0/0"))
        p.brief.write_text("Task: T-3\nClass: analysis\nObjective: x\n")
        done = p.team("flow", "T-3", "--brief", str(p.brief), "--repo", str(p.root))
        self.assertEqual(done.returncode, 4, done.stdout + done.stderr)
        state = json.loads((team / "flows" / "T-3" / "state.json").read_text())
        midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        self.assertEqual((state["status"], state["until"]), ("limited", midnight.isoformat(timespec="seconds")))
        self.assertEqual(state["why"], "over budget: 10/10 tokens today")
        self.assertEqual([st["outcome"] for st in state["steps"]], ["skipped"], "one step, no fallback, no loop")
        self.assertFalse((team / "env" / "worker" / "tasks" / "T-3" / "out" / "ran.txt").exists())


class BudgetStatsTest(unittest.TestCase):
    """team stats: today's tokens against the caps, only when a cap is set."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.team, self.transcripts = root / "team", root / "transcripts"
        for folder in (self.team / "logs", self.transcripts, root / "events"):
            folder.mkdir(parents=True)
        env = wouf_env(WOUF_TEAM_DIR=str(self.team), WOUF_EVENTS_DIR=str(root / "events"))
        env.start()
        self.addCleanup(env.stop)
        (self.team / "logs" / "lead.jsonl").write_text(
            receipt(today(), "worker returned", "tokens=100/0/0/0")
            + receipt(today(), "review returned", "tokens=20/0/0/0")
            + receipt(today(days=-1), "worker returned", "tokens=5000/0/0/0"))

    def stats(self, *args: str) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(stats.cmd_stats(["--transcripts", str(self.transcripts), *args]), 0)
        return out.getvalue()

    def test_today_against_the_caps(self) -> None:
        (self.team / "config.ini").write_text("[budget]\nper_run = 50\nper_day = 1000\nper_day.worker = 300\n")
        budget = json.loads(self.stats("--json"))["budget"]
        day = datetime.now(timezone.utc).date().isoformat()
        self.assertEqual(budget, {"day": day, "per_day": {"used": 120, "cap": 1000},
                                  "per_day.worker": {"used": 100, "cap": 300}, "per_run": 50})
        text = self.stats()
        self.assertIn(f"Budget today ({day} UTC), per_day: 120 of 1000 tokens", text)
        self.assertIn(f"Budget today ({day} UTC), per_day.worker: 100 of 300 tokens", text)
        self.assertIn("Budget per run: 50 tokens", text)

    def test_no_caps_no_lines(self) -> None:
        self.assertNotIn("budget", json.loads(self.stats("--json")))
        (self.team / "config.ini").write_text("[budget]\nper_run =\nper_day =\n")
        self.assertNotIn("budget", json.loads(self.stats("--json")))
        self.assertNotIn("Budget", self.stats())


if __name__ == "__main__":
    unittest.main()
