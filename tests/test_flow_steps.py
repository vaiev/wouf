"""A class's steps table (P03): `steps` and `rounds` in [class.<name>], and flow_next walking the table."""

from __future__ import annotations

import configparser
import io
import unittest
from contextlib import redirect_stderr

from test_flow_parts import NOW, SOURCE, gate, review, run, team_module

TABLES = {"mechanical": "worker gate review", "standard": "worker gate review",
          "complex": "scout worker gate review", "security": "scout worker gate review*2", "decision": "worker",
          "analysis": "worker", "plan": "scout worker"}
OLD = {"mechanical": (1, False), "standard": (1, False), "complex": (1, True), "security": (2, True),
       "decision": (0, False), "analysis": (0, False), "plan": (0, True)}


def table(text: str) -> list:
    steps = []
    for token in text.split():
        kind, _, count = token.partition("*")
        steps.append({"kind": kind, "count": int(count or 1)})
    return steps


def ini_of(text: str = "") -> configparser.ConfigParser:
    ini = configparser.ConfigParser(interpolation=None)
    ini.read_string(text)
    return ini


class ClassStepsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.team = team_module()

    def options(self, text: str, name: str = "standard") -> dict:
        return self.team.class_options(ini_of(text), name)

    def refused(self, steps: str) -> str:
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit):
            self.options(f"[class.docs]\ntier = strong\nsteps = {steps}\n", "docs")
        return err.getvalue()

    def test_every_built_in_class_has_its_table_and_keeps_reviews_and_scout(self) -> None:
        self.assertEqual({name: d["steps"] for name, d in self.team.CLASS_DEFAULTS.items()}, TABLES)
        for name in TABLES:
            with self.subTest(name=name):
                opts = self.team.class_options(ini_of(), name)
                self.assertEqual(opts["steps"], table(TABLES[name]))
                self.assertEqual((opts["reviews"], opts["scout"]), OLD[name])
                self.assertEqual(opts["rounds"], self.team.FLOW_ROUNDS)
        self.assertEqual(self.team.FLOW_ROUNDS, 2)

    def test_reviews_2_and_steps_review_2_are_the_same(self) -> None:
        by_reviews = self.options("[class.standard]\nreviews = 2\n")
        self.assertEqual(by_reviews, self.options("[class.standard]\nsteps = worker gate review*2\n"))
        self.assertEqual((by_reviews["reviews"], by_reviews["steps"]), (2, table("worker gate review*2")))

    def test_the_old_keys_render_into_the_table(self) -> None:
        self.assertEqual(self.options("[class.standard]\nscout = yes\n")["steps"], table("scout worker gate review"))
        self.assertEqual(self.options("[class.complex]\nreviews = 0\n", "complex")["steps"], table("scout worker"))
        self.assertEqual(self.options("[class.security]\nscout = no\n", "security")["steps"],
                         table("worker gate review*2"))
        docs = self.options("[class.docs]\ntier = strong\n", "docs")
        self.assertEqual((docs["steps"], docs["reviews"], docs["scout"]), (table("worker gate review"), 1, False))

    def test_steps_win_over_reviews_and_scout(self) -> None:
        opts = self.options("[class.standard]\nreviews = 1\nscout = yes\nsteps = worker gate review*3\n")
        self.assertEqual((opts["steps"], opts["reviews"], opts["scout"]), (table("worker gate review*3"), 3, False))
        opts = self.options("[class.complex]\nreviews = 2\nsteps = worker\n", "complex")
        self.assertEqual((opts["steps"], opts["reviews"], opts["scout"]), (table("worker"), 0, False))

    def test_steps_ignore_invalid_reviews_and_scout(self) -> None:
        opts = self.options("[class.standard]\nreviews = 4\nscout = maybe\nsteps = worker\n")
        self.assertEqual((opts["steps"], opts["reviews"], opts["scout"]), (table("worker"), 0, False))
        opts = self.options("[class.docs]\ntier = strong\nreviews = many\nsteps = scout worker gate review\n", "docs")
        self.assertEqual((opts["steps"], opts["reviews"], opts["scout"]), (table("scout worker gate review"), 1, True))

    def test_without_steps_invalid_reviews_and_scout_are_refused(self) -> None:
        for text, message in (("reviews = 4", "[class.standard] reviews"), ("reviews = many", "[class.standard] reviews"),
                              ("scout = maybe", "[class.standard] scout")):
            err = io.StringIO()
            with self.subTest(key=text), redirect_stderr(err), self.assertRaises(SystemExit):
                self.options(f"[class.standard]\n{text}\n")
            self.assertIn(message, err.getvalue())

    def test_the_grammar_accepts_preflights_a_worker_and_a_gate_with_reviews(self) -> None:
        for text in ("worker", "research worker", "scout research worker gate review*3", "worker gate review*1"):
            with self.subTest(steps=text):
                opts = self.options(f"[class.docs]\ntier = strong\nsteps = {text}\n", "docs")
                self.assertEqual([st["kind"] for st in opts["steps"]], [t.split("*")[0] for t in text.split()])

    def test_rounds(self) -> None:
        self.assertEqual(self.options("[class.standard]\nrounds = 3\n")["rounds"], 3)
        for bad in ("0", "many", "6"):
            with self.subTest(rounds=bad), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.options(f"[class.standard]\nrounds = {bad}\n")

    def test_an_unknown_token_is_refused(self) -> None:
        self.assertIn("[class.docs] steps", self.refused("worker lint"))

    def test_two_workers_are_refused(self) -> None:
        self.assertIn("[class.docs] steps", self.refused("worker worker gate review"))

    def test_review_without_gate_is_refused(self) -> None:
        self.assertIn("[class.docs] steps", self.refused("worker review"))

    def test_gate_before_worker_is_refused(self) -> None:
        self.assertIn("[class.docs] steps", self.refused("gate worker review"))

    def test_review_4_is_refused(self) -> None:
        self.assertIn("[class.docs] steps", self.refused("worker gate review*4"))

    def test_gate_without_review_is_refused(self) -> None:
        self.assertIn("[class.docs] steps", self.refused("worker gate"))

    def test_an_empty_steps_is_refused(self) -> None:
        self.assertIn("[class.docs] steps", self.refused(""))
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit):
            self.options("[class.standard]\nreviews = 1\nsteps =\n")
        self.assertIn("[class.standard] steps", err.getvalue())


class FlowStepsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.team = team_module()

    def opts(self, text: str) -> dict:
        return self.team.class_options(ini_of("[class.docs]\ntier = strong\n" + text), "docs")

    def next(self, steps: list, opts: dict, fallbacks: dict | None = None) -> dict | list:
        return self.team.flow_next({"steps": steps}, opts, fallbacks or {}, NOW)

    def test_three_rounds_of_changes_block_with_rounds_3(self) -> None:
        opts = self.opts("steps = worker gate review\nrounds = 3\n")
        steps = []
        for round_ in range(3):
            steps += [run("worker"), gate("green")]
            self.assertEqual(self.next(steps, opts), {"do": "review", "index": 1, "runner": None})
            steps.append(review("changes", changes=["[change] x"]))
            if round_ < 2:
                self.assertEqual(self.next(steps, opts), {"do": "worker", "after": "review"})
        self.assertEqual(self.next(steps, opts), {"end": "blocked", "why": "3 review rounds without APPROVE"})

    def test_research_then_the_worker_then_ready(self) -> None:
        opts = {**self.opts("steps = research worker\n"), "research": True}
        self.assertEqual(self.next([], opts), {"do": "research"})
        research = run("research")
        self.assertEqual(self.next([research], opts), {"do": "worker"})
        worker = run("worker", status="ready", head=None, head_before=None)
        self.assertEqual(self.next([research, worker], opts), {"end": "ready"})

    def test_a_scout_outside_the_table_is_never_scheduled(self) -> None:
        opts = {**self.team.class_options(ini_of(), "complex"), "steps": table("worker gate review"), "scout": False}
        until = "2026-10-25T09:50:00+00:00"
        histories = [[], [run("scout")], [run("scout", "failed")], [run("scout", "skipped")],
                     [run("scout", "limited", until=until)], [run("scout", "limited", until=until, group=0)],
                     [run("scout", "limited", until="2026-10-25T07:00:00+00:00")],
                     [run("scout"), run("worker")], [run("scout"), run("worker"), gate("green")]]
        for steps in histories:
            for fallbacks in ({}, {"alpha": "beta"}):
                with self.subTest(steps=[st["kind"] + ":" + st["outcome"] for st in steps], fallbacks=fallbacks):
                    nxt = self.next(steps, opts, fallbacks)
                    acts = nxt if isinstance(nxt, list) else [nxt]
                    self.assertNotIn("scout", [act.get("do") for act in acts])
        self.assertEqual(self.next([run("scout", "limited", until=until)], opts, {"alpha": "beta"}),
                         {"do": "worker"})

    def test_the_table_decides_what_follows_the_worker(self) -> None:
        opts = self.opts("steps = worker\n")
        self.assertEqual(self.next([run("worker")], opts), {"end": "ready"})
        opts = self.opts("steps = worker gate review*2\n")
        steps = [run("worker"), gate("green"), review("approve")]
        self.assertEqual(self.next(steps, {**opts, "reviews": 1})["index"], 2, "the table's review*2 wins")

    def test_flow_next_walks_the_table_with_one_handler_per_kind(self) -> None:
        self.assertTrue({"scout", "research", "worker", "gate", "review"} <= set(self.team.STEP_HANDLERS))

    def test_flow_py_stays_within_1250_lines(self) -> None:
        text = (SOURCE / "bin" / "teamlib" / "flow.py").read_text(encoding="utf-8")
        self.assertLessEqual(len(text.splitlines()), 1250)

    def test_the_docs_describe_steps_and_rounds(self) -> None:
        root = SOURCE.parent
        for path in (SOURCE / "team" / "config.ini", SOURCE / "skills" / "dispatch.md", root / "docs" / "DESIGN.md",
                     root / "CHANGELOG.md"):
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIn("steps", text)
                self.assertIn("rounds", text)
                self.assertIn("review*2", text)


if __name__ == "__main__":
    unittest.main()
