"""Tests for the pure parts of a flow in .agents/bin/team: brief header, classes, path scope, review verdicts."""

from __future__ import annotations

import configparser
import importlib
import sys
import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / ".agents"
HEAD = "3f2a9c1e5b7d4f60a8b9c0d1e2f3a4b5c6d7e8f9"


def team_module():
    sys.path.insert(0, str(SOURCE / "bin"))
    engine = importlib.import_module("teamlib.engine")
    egress = engine.EGRESS[:]
    loader = SourceFileLoader("wouf_team_parts", str(SOURCE / "bin" / "teamlib" / "flow.py"))
    module = module_from_spec(spec_from_loader("wouf_team_parts", loader))
    loader.exec_module(module)
    # This copy of flow.py fills engine.EGRESS with its own prepare_egress when it runs; the hook belongs to the
    # teamlib.flow bin/team imports, so put it back (or fill it from that module when nothing held it yet).
    engine.EGRESS[:] = egress or [importlib.import_module("teamlib.flow").prepare_egress]
    # bin/team imports every teamlib module; teamlib.evidence fills gate.EVIDENCE, without which a step stops.
    importlib.import_module("teamlib.evidence")
    return module


class BriefHeaderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.team = team_module()

    def test_class_paths_and_scout(self) -> None:
        header = self.team.brief_header("Task: T-12\nClass: security\nPaths: core/src/**, docs/spec.md\n"
                                        "Scout: no\nObjective: x\n")
        self.assertEqual(header, {"task": "T-12", "class": "security", "paths": ["core/src/**", "docs/spec.md"],
                                  "scout": False})

    def test_defaults(self) -> None:
        header = self.team.brief_header("Objective: x\n")
        self.assertEqual(header["class"], "standard")
        self.assertEqual(header["paths"], [])
        self.assertIsNone(header["scout"])

    def test_an_unknown_class_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            self.team.brief_header("Class: urgent\n")

    def test_only_the_first_line_of_a_key_counts(self) -> None:
        self.assertEqual(self.team.brief_header("Class: mechanical\nClass: security\n")["class"], "mechanical")


class ClassTest(unittest.TestCase):
    def setUp(self) -> None:
        self.team = team_module()
        self.ini = configparser.ConfigParser(interpolation=None)

    def test_generic_defaults(self) -> None:
        mech = self.team.class_options(self.ini, "mechanical")
        self.assertEqual((mech["bot"], mech["tier"], mech["effort"], mech["reviews"], mech["scout"]),
                         ("worker", "light", "low", 1, False))
        sec = self.team.class_options(self.ini, "security")
        self.assertEqual((sec["tier"], sec["reviews"], sec["scout"]), ("strong", 2, True))
        self.assertEqual(self.team.class_options(self.ini, "decision")["bot"], "decide")
        self.assertEqual(self.team.class_options(self.ini, "decision")["reviews"], 0)

    def test_the_team_config_overrides(self) -> None:
        self.ini.read_string("[class.standard]\nmodel = exact-model-1\nreview_runners = codex gemini\n")
        std = self.team.class_options(self.ini, "standard")
        self.assertEqual(std["model"], "exact-model-1")
        self.assertEqual(std["review_runners"], ["codex", "gemini"])
        self.assertEqual(std["tier"], "strong")

    def test_bad_values_are_refused(self) -> None:
        self.ini.read_string("[class.standard]\nreviews = many\n")
        with self.assertRaises(SystemExit):
            self.team.class_options(self.ini, "standard")

    def test_class_defaults_keep_their_tiers(self) -> None:
        tiers = {name: self.team.class_options(self.ini, name)["tier"] for name in self.team.CLASS_DEFAULTS}
        self.assertEqual(tiers, {"mechanical": "light", "standard": "strong", "complex": "strong",
                                 "security": "strong", "decision": "strong", "analysis": "strong", "plan": "strong"})

    def test_a_class_may_run_on_mid(self) -> None:
        self.ini.read_string("[class.standard]\ntier = mid\n[class.analysis]\ntier = mid\n"
                             "[class.mechanical]\ntier = mid\n")
        for name in ("standard", "analysis", "mechanical"):
            self.assertEqual(self.team.class_options(self.ini, name)["tier"], "mid")

    def test_an_unknown_tier_is_refused(self) -> None:
        self.ini.read_string("[class.standard]\ntier = medium\n")
        with self.assertRaises(SystemExit):
            self.team.class_options(self.ini, "standard")

    def test_complex_security_decision_and_plan_never_run_below_strong(self) -> None:
        for name in ("complex", "security", "decision", "plan"):
            for tier in ("light", "mid"):
                with self.subTest(name=name, tier=tier):
                    ini = configparser.ConfigParser(interpolation=None)
                    ini.read_string(f"[class.{name}]\ntier = {tier}\n")
                    with self.assertRaises(SystemExit):
                        self.team.class_options(ini, name)


class ScopeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.team = team_module()

    def test_globs(self) -> None:
        paths = ["core/src/**", "docs/spec.md", "tests/*.py", "site/"]
        inside = ["core/src/a.rs", "core/src/deep/b.rs", "docs/spec.md", "tests/test_a.py", "site/x/y.html"]
        outside = ["core/Cargo.toml", "docs/other.md", "tests/sub/test_b.py", "README.md", "sitemap.xml"]
        self.assertEqual(self.team.outside_paths(inside + outside, paths), outside)

    def test_no_paths_means_no_limit(self) -> None:
        self.assertEqual(self.team.outside_paths(["anything/at/all"], []), [])


class ReviewVerdictTest(unittest.TestCase):
    def setUp(self) -> None:
        self.team = team_module()
        self.name = "WOUF Review"

    def verdict(self, *lines: str) -> dict:
        return self.team.review_verdict(["noise before", *lines], self.name, "T-1.review1", HEAD)

    def test_approve_on_the_head(self) -> None:
        got = self.verdict(f"WOUF Review · T-1.review1 · APPROVE {HEAD[:12]}", "Blocking: none",
                           "Non-blocking: none")
        self.assertEqual(got["verdict"], "approve")
        self.assertEqual(got["bugs"], [])

    def test_approve_on_another_commit_is_no_approve(self) -> None:
        got = self.verdict("WOUF Review · T-1.review1 · APPROVE 0123456789ab", "Blocking: none")
        self.assertEqual(got["verdict"], "stale")

    def test_a_short_or_missing_commit_is_no_approve(self) -> None:
        self.assertEqual(self.verdict(f"WOUF Review · T-1.review1 · APPROVE {HEAD[:5]}")["verdict"], "stale")
        self.assertEqual(self.verdict("WOUF Review · T-1.review1 · APPROVE")["verdict"], "stale")

    def test_changes_with_bugs_and_changes(self) -> None:
        got = self.verdict("WOUF Review · T-1.review1 · CHANGES",
                           "Blocking:",
                           "- [bug] the lock is taken twice → race → take it once",
                           "- [change] rename x → clarity → call it y",
                           "Non-blocking: none",
                           "Checked: all tests")
        self.assertEqual(got["verdict"], "changes")
        self.assertEqual(got["bugs"], ["[bug] the lock is taken twice → race → take it once"])
        self.assertEqual(got["changes"], ["[change] rename x → clarity → call it y"])

    def test_no_block_means_no_verdict(self) -> None:
        self.assertIsNone(self.verdict("I think it is fine.")["verdict"])

    def test_the_last_block_counts(self) -> None:
        got = self.verdict("WOUF Review · T-1.review1 · CHANGES", "Blocking: - [bug] x",
                           f"WOUF Review · T-1.review1 · APPROVE {HEAD}", "Blocking: none")
        self.assertEqual(got["verdict"], "approve")


if __name__ == "__main__":
    unittest.main()


NOW = "2026-10-25T08:00:00+00:00"


def run(kind: str, outcome: str = "done", **extra) -> dict:
    step = {"kind": kind, "outcome": outcome, "runner": "alpha", "tier": "strong"}
    if kind in ("worker", "bugfixer") and outcome == "done":
        step.update(status="ready", head_before="a" * 40, head="b" * 40)
    step.update(extra)
    return step


def gate(outcome: str) -> dict:
    return {"kind": "gate", "outcome": outcome}


def review(verdict, index: int = 1, **extra) -> dict:
    return {"kind": "review", "outcome": "done", "verdict": verdict, "index": index, "bugs": [], "changes": [],
            "runner": "beta", **extra}


class BlockFieldTest(unittest.TestCase):
    def test_a_field_with_a_list_under_it(self) -> None:
        team = team_module()
        block = ["X Worker · T-1 · blocked", "Evidence:", "- a", "Questions:", "- which file?", "- which tool?",
                 "Flags: none"]
        self.assertEqual(team.block_field(block, "Questions"), "which file? / which tool?")
        self.assertEqual(team.block_field(["sign", "Questions: none"], "Questions"), "")
        self.assertEqual(team.block_field(["sign", "Questions: one line"], "Questions"), "one line")


class FlowNextTest(unittest.TestCase):
    """The flow's transition table: from the steps so far to the next step or the end."""

    def setUp(self) -> None:
        self.team = team_module()
        self.opts = {"bot": "worker", "runner": None, "tier": "strong", "model": None, "effort": "medium",
                     "reviews": 1, "review_runners": [], "scout": False}

    def next(self, steps: list, fallbacks: dict | None = None, **opts) -> dict:
        return self.team.flow_next({"steps": steps}, {**self.opts, **opts}, fallbacks or {}, NOW)

    def test_start_with_or_without_scout(self) -> None:
        self.assertEqual(self.next([])["do"], "worker")
        self.assertEqual(self.next([], scout=True)["do"], "scout")
        self.assertEqual(self.next([run("scout")], scout=True)["do"], "worker")

    def test_a_failed_or_limited_scout_does_not_hold_up_the_worker(self) -> None:
        self.assertEqual(self.next([run("scout", "failed")], scout=True), {"do": "worker"})
        limited = run("scout", "limited", until="2026-10-25T09:50:00+00:00")
        self.assertEqual(self.next([limited], scout=True), {"do": "worker"})
        self.assertEqual(self.next([limited], {"alpha": "beta"}, scout=True)["runner"], "beta")

    def test_the_happy_path(self) -> None:
        self.assertEqual(self.next([run("worker")])["do"], "gate")
        self.assertEqual(self.next([run("worker"), gate("green")]), {"do": "review", "index": 1, "runner": None})
        self.assertEqual(self.next([run("worker"), gate("green"), review("approve")])["end"], "ready")

    def test_two_reviews_with_their_own_runners(self) -> None:
        steps = [run("worker"), gate("green")]
        self.assertEqual(self.next(steps, reviews=2, review_runners=["codex", "gemini"])["runner"], "codex")
        steps.append(review("approve"))
        nxt = self.next(steps, reviews=2, review_runners=["codex", "gemini"])
        self.assertEqual((nxt["do"], nxt["index"], nxt["runner"]), ("review", 2, "gemini"))
        steps.append(review("approve", index=2))
        self.assertEqual(self.next(steps, reviews=2)["end"], "ready")

    def test_no_review_classes_end_after_the_work(self) -> None:
        self.assertEqual(self.next([run("worker", head=None, head_before=None)], reviews=0)["end"], "ready")

    def test_a_worker_without_commits_is_blocked(self) -> None:
        nxt = self.next([run("worker", head=None, head_before=None)])
        self.assertEqual(nxt["end"], "blocked")
        self.assertIn("no commits", nxt["why"])

    def test_a_rework_without_new_commits_goes_to_the_gate_again(self) -> None:
        steps = [run("worker"), gate("red"), run("worker", head_before="b" * 40, head="b" * 40)]
        self.assertEqual(self.next(steps)["do"], "gate")

    def test_questions_and_a_blocked_worker_go_to_the_lead(self) -> None:
        self.assertEqual(self.next([run("worker", status="questions", questions="which file?")]),
                         {"end": "question", "why": "which file?"})
        self.assertEqual(self.next([run("worker", status="blocked")])["end"], "blocked")
        self.assertEqual(self.next([run("worker", status="answered")]), {"do": "worker"})

    def test_a_red_gate_goes_back_once_on_strong(self) -> None:
        nxt = self.next([run("worker", tier="light"), gate("red")], tier="light")
        self.assertEqual((nxt["do"], nxt["tier"], nxt["after"]), ("worker", "strong", "gate"))
        nxt = self.next([run("worker"), gate("red"), run("worker"), gate("red")])
        self.assertEqual(nxt["end"], "blocked")
        self.assertIn("gate", nxt["why"])

    def test_changes_go_to_the_worker_and_bugs_to_the_bugfixer(self) -> None:
        base = [run("worker"), gate("green")]
        self.assertEqual(self.next(base + [review("changes", changes=["[change] x"])])["do"], "worker")
        self.assertEqual(self.next(base + [review("changes", bugs=["[bug] y"])])["do"], "bugfixer")
        self.assertEqual(self.next(base + [review("changes", bugs=["[bug] y"]), run("bugfixer")])["do"], "gate")

    def test_mixed_findings_go_to_the_worker_with_everything(self) -> None:
        base = [run("worker"), gate("green")]
        nxt = self.next(base + [review("changes", changes=["[change] x"], bugs=["[bug] y"])])
        self.assertEqual(nxt, {"do": "worker", "after": "review"})

    def test_two_rounds_without_approve_block(self) -> None:
        steps = [run("worker"), gate("green"), review("changes"), run("worker"), gate("green"), review("changes")]
        nxt = self.next(steps)
        self.assertEqual(nxt["end"], "blocked")
        self.assertIn("2 review rounds", nxt["why"])

    def test_a_second_round_needs_all_reviews_again(self) -> None:
        steps = [run("worker"), gate("green"), review("approve"), review("changes", index=2), run("worker"),
                 gate("green")]
        self.assertEqual(self.next(steps, reviews=2)["index"], 1)

    def test_a_review_without_a_verdict_is_asked_once_more(self) -> None:
        base = [run("worker"), gate("green")]
        nxt = self.next(base + [review(None)])
        self.assertEqual((nxt["do"], nxt["index"]), ("review", 1))
        self.assertEqual(self.next(base + [review("stale")])["do"], "review")
        self.assertEqual(self.next(base + [review(None), review("stale")])["end"], "blocked")

    def test_a_limit_switches_to_the_fallback_runner_once(self) -> None:
        limited = run("worker", "limited", runner="alpha", until="2026-10-25T09:50:00+00:00")
        nxt = self.next([limited], fallbacks={"alpha": "beta"})
        self.assertEqual((nxt["do"], nxt["runner"], nxt["fallback"]), ("worker", "beta", True))
        on_fallback = run("worker", "limited", runner="beta", fallback=True, until="2026-10-25T11:00:00+00:00")
        nxt = self.next([limited, on_fallback], fallbacks={"alpha": "beta"})
        self.assertEqual(nxt, {"end": "limited", "until": "2026-10-25T09:50:00+00:00"})

    def test_a_limit_without_fallback_waits_and_then_runs_again(self) -> None:
        limited = run("worker", "limited", until="2026-10-25T09:50:00+00:00")
        self.assertEqual(self.next([limited])["end"], "limited")
        past = run("worker", "limited", until="2026-10-25T07:00:00+00:00")
        self.assertEqual(self.next([past])["do"], "worker")
        unknown = run("worker", "limited", until=None)
        self.assertEqual(self.next([unknown])["end"], "limited")

    def test_a_failed_run_is_tried_once_more(self) -> None:
        self.assertEqual(self.next([run("worker", "failed", tier="light")])["tier"], "strong")
        self.assertEqual(self.next([run("worker", "failed"), run("worker", "timeout")])["end"], "blocked")
        self.assertEqual(self.next([run("worker", "stopped")])["end"], "blocked")

    def test_a_missed_mid_run_goes_again_on_strong(self) -> None:
        for kind in ("worker", "bugfixer"):
            with self.subTest(kind=kind):
                nxt = self.next([run("worker"), gate("green"), review("changes"), run(kind, "failed", tier="mid")],
                                tier="mid")
                self.assertEqual((nxt["do"], nxt["tier"]), (kind, "strong"))
        nxt = self.next([run("worker", tier="mid"), gate("red")], tier="mid")
        self.assertEqual((nxt["do"], nxt["tier"], nxt["after"]), ("worker", "strong", "gate"))
        steps = [run("worker", "failed", tier="mid"), run("worker", "failed", tier="strong")]
        self.assertEqual(self.next(steps, tier="mid")["end"], "blocked")

    def test_the_tier_of_each_step(self) -> None:
        opts = {**self.opts, "tier": "mid"}
        self.assertEqual(self.team.step_tier("worker", {"do": "worker"}, opts), "mid")
        self.assertEqual(self.team.step_tier("worker", {"do": "worker", "tier": "strong"}, opts), "strong")
        self.assertEqual(self.team.step_tier("review", {"do": "review", "index": 1}, opts), "strong")
        self.assertEqual(self.team.step_tier("review", {"do": "review", "tier": "light"}, opts), "strong")
        # Scout and bugfixer take their declaration's tier (mid) unless the flow escalates them.
        self.assertIsNone(self.team.step_tier("scout", {"do": "scout"}, opts))
        self.assertIsNone(self.team.step_tier("bugfixer", {"do": "bugfixer"}, opts))
        self.assertEqual(self.team.step_tier("bugfixer", {"do": "bugfixer", "tier": "strong"}, opts), "strong")

    def test_a_failed_review_run_is_retried_with_its_index(self) -> None:
        steps = [run("worker"), gate("green"), review("approve"), {"kind": "review", "outcome": "failed", "index": 2}]
        nxt = self.next(steps, reviews=2)
        self.assertEqual((nxt["do"], nxt["index"]), ("review", 2))


class FlowFallbackTest(unittest.TestCase):
    """A usage limit and its fallback runner: tried once per limited period, never back and forth (T-0019-05)."""

    def setUp(self) -> None:
        self.team = team_module()
        self.opts = {"bot": "worker", "runner": None, "tier": "strong", "model": None, "effort": "medium",
                     "reviews": 1, "review_runners": [], "scout": False}
        self.fallbacks = {"alpha": "beta"}
        self.limited = run("worker", "limited", runner="alpha", until="2026-10-25T09:50:00+00:00")

    def next(self, steps: list, at: str = NOW) -> dict:
        return self.team.flow_next({"steps": steps}, self.opts, self.fallbacks, at)

    def test_a_failed_fallback_ends_limited_in_two_steps(self) -> None:
        # T-0019-05: claude limited, codex (no login) failed at once; the flow went back and forth for 24 steps.
        steps = [self.limited]
        nxt = self.next(steps)
        self.assertEqual((nxt["do"], nxt["runner"], nxt["fallback"]), ("worker", "beta", True))
        steps.append(run("worker", "failed", runner="beta", fallback=True))
        nxt = self.next(steps)
        self.assertEqual((nxt["end"], nxt["until"]), ("limited", "2026-10-25T09:50:00+00:00"))
        self.assertIn("beta", nxt["why"])
        self.assertIn("failed", nxt["why"])
        self.assertEqual(len(steps), 2)
        # Run again before the reset: still limited, no step and no fallback.
        self.assertEqual(self.next(steps, "2026-10-25T09:00:00+00:00")["end"], "limited")
        # After the reset the primary runner runs again, not the fallback.
        after = self.next(steps, "2026-10-25T10:00:00+00:00")
        self.assertEqual(after["do"], "worker")
        self.assertNotIn("runner", after)

    def test_a_timed_out_fallback_ends_limited_too(self) -> None:
        nxt = self.next([self.limited, run("worker", "timeout", runner="beta", fallback=True)])
        self.assertEqual(nxt["end"], "limited")
        self.assertIn("timeout", nxt["why"])

    def test_the_fallback_is_tried_once_per_limited_period(self) -> None:
        # The fallback did the work; the next step on the primary is limited again before the reset time.
        steps = [self.limited, run("worker", runner="beta", fallback=True), gate("red"),
                 run("worker", "limited", runner="alpha", after="gate", until="2026-10-25T09:50:00+00:00")]
        self.assertEqual(self.next(steps), {"end": "limited", "until": "2026-10-25T09:50:00+00:00"})
        # A limit after the reset is a new period: the fallback is tried again.
        later = "2026-10-25T10:30:00+00:00"
        steps[-1] = run("worker", "limited", runner="alpha", after="gate", until="2026-10-25T15:00:00+00:00")
        self.assertEqual(self.next(steps, later)["runner"], "beta")

    def test_a_limited_fallback_ends_limited_at_the_earlier_reset(self) -> None:
        late = run("worker", "limited", runner="beta", fallback=True, until="2026-10-25T11:00:00+00:00")
        self.assertEqual(self.next([self.limited, late]), {"end": "limited", "until": "2026-10-25T09:50:00+00:00"})
        early = run("worker", "limited", runner="beta", fallback=True, until="2026-10-25T09:10:00+00:00")
        self.assertEqual(self.next([self.limited, early]), {"end": "limited", "until": "2026-10-25T09:10:00+00:00"})

    def fallback_act(self, auth: bool, login) -> tuple:
        self.team.mode = lambda ini: "container"
        self.team.uses_login = lambda ini, runner: auth
        checked = []
        self.team.login_check = lambda ini, member, runner: checked.append((member, runner)) or login
        self.team.record = lambda events: None
        act = {"do": "worker", "runner": "beta", "fallback": True}
        return self.team.no_login(configparser.ConfigParser(), "T-1", act, self.opts), checked

    def test_a_fallback_without_login_is_skipped(self) -> None:
        step, checked = self.fallback_act(True, False)
        self.assertEqual(checked, [("worker", "beta")])
        self.assertEqual((step["kind"], step["outcome"], step["runner"], step["fallback"]),
                         ("worker", "failed", "beta", True))
        self.assertIn("login", step["why"])
        limited = {**self.limited, "bot": "worker"}
        nxt = self.next([limited, step])
        self.assertEqual((nxt["end"], nxt["until"]), ("limited", "2026-10-25T09:50:00+00:00"))
        self.assertIn("login", nxt["why"])

    def test_a_fallback_with_login_or_without_auth_login_runs(self) -> None:
        self.assertIsNone(self.fallback_act(True, True)[0])
        self.assertIsNone(self.fallback_act(True, None)[0], "no login_check: the run shows it")
        step, checked = self.fallback_act(False, False)
        self.assertEqual((step, checked), (None, []))


class FlowLockTest(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self.team = team_module()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)

    def test_the_lock_is_released_after_the_final_state_write(self) -> None:
        team, folder = self.team, self.folder
        (folder / "state.json").write_text('{"task": "T-1", "class": "standard", "branch": "b", "steps": []}')
        writes = []
        write_json = team.write_json

        def spy(path, data):
            writes.append((data.get("status"), (folder / "flow.lock").exists()))
            write_json(path, data)

        team.flow_dir = lambda task: folder
        team.write_json = spy
        team.class_options = lambda ini, name: {"bot": "worker", "scout": False}
        team.flow_next = lambda flow, opts, fallbacks, at: {"end": "blocked", "why": "x"}
        team.head_of = lambda flow: None
        team.record = team.lead_log = team.say = lambda *a, **k: None
        ini = configparser.ConfigParser(interpolation=None)
        self.assertEqual(team.run_flow(ini, "T-1"), 1)
        self.assertEqual(writes[-1], ("blocked", True))
        self.assertFalse((folder / "flow.lock").exists())

    def test_a_zombie_is_no_running_flow(self) -> None:
        import os
        import subprocess
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        self.addCleanup(child.wait)
        for _ in range(200):
            if Path(f"/proc/{child.pid}/stat").is_file() and \
                    Path(f"/proc/{child.pid}/stat").read_text().rsplit(")", 1)[-1].split()[0] == "Z":
                break
            import time
            time.sleep(0.01)
        else:
            self.skipTest("no /proc or the child did not exit")
        self.assertTrue(self.team.alive(child.pid), "kill(pid, 0) still finds the zombie")
        self.assertFalse(self.team.pid_running(child.pid))
        self.assertTrue(self.team.pid_running(os.getpid()))
        (self.folder / "flow.lock").write_text('{"pid": %d}' % child.pid)
        self.assertEqual(self.team.flow_lock(self.folder, "T-1"), self.folder / "flow.lock")


class FlowHeaderTest(unittest.TestCase):
    """The pre-flight a brief asks for: Research: yes, and the context graph instead of a scout (T-0019-21)."""

    def setUp(self) -> None:
        self.team = team_module()

    def test_research_is_read_from_the_brief(self) -> None:
        self.assertTrue(self.team.flow_header("Class: plan\nResearch: yes\n")["research"])
        self.assertFalse(self.team.flow_header("Class: plan\nResearch: no\n")["research"])
        self.assertFalse(self.team.flow_header("Class: plan\n")["research"])

    def test_the_graph_replaces_the_scout_of_complex_and_security_briefs_with_paths(self) -> None:
        scout = lambda text: self.team.preflight_scout(self.team.flow_header(text))  # noqa: E731
        self.assertIs(scout("Class: complex\nPaths: a.py\n"), False)
        self.assertIs(scout("Class: security\nPaths: a.py\n"), False)
        self.assertIs(scout("Class: security\nPaths: a.py\nScout: yes\n"), True)
        self.assertIsNone(scout("Class: complex\n"), "no Paths, no graph: the class's scout")
        self.assertIsNone(scout("Class: plan\nPaths: a.py\n"), "plan keeps its scout")


class FlowParallelNextTest(unittest.TestCase):
    """Steps that run at once: the pre-flight and the two reviews of a security task (T-0019-21)."""

    def setUp(self) -> None:
        self.team = team_module()
        self.opts = {"bot": "worker", "runner": None, "tier": "strong", "model": None, "effort": "high",
                     "reviews": 2, "review_runners": [], "scout": True, "research": False, "review2": "review2"}

    def next(self, steps: list, fallbacks: dict | None = None, at: str = NOW, **opts):
        return self.team.flow_next({"steps": steps}, {**self.opts, **opts}, fallbacks or {}, at)

    def pair(self, first: dict, second: dict) -> list:
        return [{**first, "parallel": True, "group": 9, "head": HEAD},
                {**second, "parallel": True, "group": 9, "bot": "review2", "head": HEAD}]

    def test_scout_and_research_run_at_once(self) -> None:
        self.assertEqual(self.next([], research=True), [{"do": "scout"}, {"do": "research"}])
        self.assertEqual(self.next([], research=True, scout=False), {"do": "research"})
        self.assertEqual(self.next([]), {"do": "scout"}, "a single step stays a dict")

    def test_after_the_preflight_the_worker_runs_whatever_it_gave(self) -> None:
        limited = run("scout", "limited", group=0, until="2026-10-25T09:50:00+00:00")
        for research in (run("research", group=0), run("research", "failed", group=0)):
            self.assertEqual(self.next([limited, research], {"alpha": "beta"}), {"do": "worker"})
        self.assertEqual(self.next([run("research")]), {"do": "worker"})
        self.assertEqual(self.next([run("research", "failed")]), {"do": "worker"})

    def test_two_reviews_at_once_after_a_green_gate(self) -> None:
        nxt = self.next([run("worker"), gate("green")], review_runners=["codex", "gemini"])
        self.assertEqual(nxt, [{"do": "review", "index": 1, "runner": "codex", "parallel": True},
                               {"do": "review", "index": 2, "runner": "gemini", "parallel": True, "bot": "review2"}])

    def test_without_an_active_review2_the_reviews_stay_serial(self) -> None:
        self.assertEqual(self.next([run("worker"), gate("green")], review2=None),
                         {"do": "review", "index": 1, "runner": None})
        self.assertIsInstance(self.next([run("worker"), gate("green")], reviews=3), dict)

    def test_a_red_gate_starts_no_review(self) -> None:
        nxt = self.next([run("worker"), gate("red")])
        self.assertEqual(nxt["do"], "worker")
        self.assertEqual(self.next([run("worker"), gate("red"), run("worker"), gate("red")])["end"], "blocked")

    def test_both_must_approve_the_same_head(self) -> None:
        base = [run("worker"), gate("green")]
        self.assertEqual(self.next(base + self.pair(review("approve"), review("approve", 2))), {"end": "ready"})
        other = self.pair(review("approve"), review("approve", 2))
        other[1]["head"] = "0" * 40
        self.assertEqual(self.next(base + other)["end"], "blocked")

    def test_changes_from_either_review_are_one_round(self) -> None:
        base = [run("worker"), gate("green")]
        both = self.pair(review("changes", bugs=["[bug] a"]), review("changes", 2, changes=["[change] b"]))
        self.assertEqual(self.next(base + both), {"do": "worker", "after": "review"})
        bug = self.pair(review("approve"), review("changes", 2, bugs=["[bug] a"]))
        self.assertEqual(self.next(base + bug), {"do": "bugfixer"})
        second = base + both + [run("worker"), gate("green")] + both
        self.assertIn("2 review rounds", self.next(second)["why"])
        self.assertEqual(self.team.changes_rounds(base + both), 1)

    def test_a_review_without_a_verdict_runs_again_alone_once(self) -> None:
        base = [run("worker"), gate("green")]
        steps = base + self.pair(review("approve"), review(None, 2))
        nxt = self.next(steps)
        self.assertEqual(nxt, {"do": "review", "index": 2, "runner": None, "parallel": True, "bot": "review2"})
        again = {**review("stale", 2), "parallel": True, "bot": "review2"}
        self.assertEqual(self.next(steps + [again])["end"], "blocked")
        self.assertEqual(self.next(steps + [{**review("approve", 2), "parallel": True, "head": HEAD}]),
                         {"end": "ready"})

    def test_a_limited_review_waits_after_the_other_returned_and_keeps_its_result(self) -> None:
        base = [run("worker"), gate("green")]
        limited = {"kind": "review", "outcome": "limited", "index": 2, "runner": "alpha",
                   "until": "2026-10-25T09:50:00+00:00"}
        steps = base + self.pair(review("approve"), limited)
        self.assertEqual(self.next(steps), {"end": "limited", "until": "2026-10-25T09:50:00+00:00"})
        later = self.next(steps, at="2026-10-25T10:00:00+00:00")
        self.assertEqual((later["do"], later["index"], later["bot"]), ("review", 2, "review2"),
                         "only the limited review runs again; the approve is kept")
        on_fallback = self.next(steps, {"alpha": "beta"})
        self.assertEqual((on_fallback["index"], on_fallback["runner"], on_fallback["fallback"]), (2, "beta", True))
        failed = {**limited, "outcome": "failed", "runner": "beta", "fallback": True, "parallel": True}
        self.assertEqual(self.next(steps + [failed], {"alpha": "beta"})["end"], "limited")

    def test_both_missing_run_again_at_once(self) -> None:
        base = [run("worker"), gate("green")]
        nxt = self.next(base + self.pair({"kind": "review", "outcome": "failed", "index": 1},
                                         {"kind": "review", "outcome": "timeout", "index": 2}))
        self.assertEqual([(act["index"], act.get("bot")) for act in nxt], [(1, None), (2, "review2")])
