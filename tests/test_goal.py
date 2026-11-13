"""team goal (T-0019-25): a goal in plain words through INIT, CONCEPT, REALIZE and VERIFY. INIT runs a deterministic
classifier (no model) that proposes class, paths, a threat model skeleton and the gate checks; every phase writes a
phase.state event; leaving a phase needs the lead's --yes, and under autonomy = ask the human's yes first."""

from __future__ import annotations

import configparser
import json
import os
import random
import shutil
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from unittest import mock

from test_team_cli import SOURCE, Project

sys.path.insert(0, str(SOURCE / "bin"))
from teamlib import classify, state  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import isolate_wouf_env  # noqa: E402

# A fabricated GitHub fine-grained token built from parts; it works nowhere.
FAKE_KEY = "github_" "pat_" + "Zx9Kq2Lm7Pw4Rt6Vb8Ny3H" + "_" + "Qw8Er5Ty2Ui7Op4As9Df1Gh6Jk3Lz0Xc5Vb8Nm2Qw7Er4Ty1Ui9Op6As3Df"
HEAD = "0123456789abcdef0123456789abcdef01234567"

FILES = [
    ".agents/START.md", ".agents/bin/log", ".agents/bin/team", ".agents/bin/teamlib/batch.py",
    ".agents/bin/teamlib/core.py", ".agents/bin/teamlib/flow.py", ".agents/bin/teamlib/graph.py",
    ".agents/skills/brief.md", ".github/workflows/ci.yml", "Makefile", "README.md", "config/secrets.example.env",
    "docs/DESIGN.md", "docs/usage.md", "scripts/release.sh", "src/api/handlers.py", "src/api/routes.py",
    "src/auth/login.py", "src/auth/tokens.py", "src/billing/invoice.py", "src/ui/app.ts", "src/ui/theme.css",
    "tests/test_batch.py", "tests/test_graph.py", "tests/test_routes.py",
]
# What the context graph gives for the identifiers a goal names: the files that define them.
SYMBOLS = {"parse_route": ["src/api/routes.py"], "render_invoice": ["src/billing/invoice.py"],
           "glob_regex": [".agents/bin/teamlib/core.py"]}

# Twenty goals with the class and paths a lead would write for them; never a class with fewer reviews than
# bin/project's former rule gives (design, compare, a goal without paths or over three areas: standard).
FIXTURES = [
    ("Fix the typo in README", "mechanical", ["README.md"]),
    ("Rename `parse_route` to match_route in src/api/routes.py", "mechanical", ["src/api/routes.py"]),
    ("Fix the crash in src/api/handlers.py when the body is empty", "standard", ["src/api/handlers.py"]),
    ("Add a dark mode toggle to src/ui/app.ts and src/ui/theme.css", "standard", ["src/ui/app.ts", "src/ui/theme.css"]),
    ("Fix token expiry in src/auth/tokens.py", "security", ["src/auth/tokens.py"]),
    ("Pin the action versions in .github/workflows/ci.yml", "security", [".github/workflows/ci.yml"]),
    ("Update config/secrets.example.env with the new variable names", "security", ["config/secrets.example.env"]),
    ("Implement a retry for `render_invoice` with tests", "complex", ["src/billing/invoice.py"]),
    ("Refactor the overlap check in batch.py and add tests", "security",
     [".agents/bin/teamlib/batch.py", "tests/test_batch.py"]),
    ("Design the billing module: invoices, payments and refunds", "standard", []),
    ("Decide whether we keep the Makefile or move to scripts/", "decision", ["Makefile", "scripts/**"]),
    ("Investigate why the routes in src/api are slow", "analysis", ["src/api/**"]),
    ("Compare docs/usage.md with docs/DESIGN.md and report the gaps", "standard", ["docs/DESIGN.md", "docs/usage.md"]),
    ("Add rate limiting to every endpoint", "standard", []),
    ("Update src/api/routes.py, src/ui/app.ts and docs/usage.md for the new endpoint", "standard",
     ["docs/usage.md", "src/api/routes.py", "src/ui/app.ts"]),
    ("Bump the version in the Makefile", "mechanical", ["Makefile"]),
    ("Make the release script scripts/release.sh stop on the first error", "standard", ["scripts/release.sh"]),
    ("Migrate src/auth/login.py to the new session store", "security", ["src/auth/login.py"]),
    ("Fix the handling of empty lines in tests/test_routes.py", "standard", ["tests/test_routes.py"]),
    ("Choose a chart library for src/ui", "decision", ["src/ui/**"]),
]


def score() -> tuple[int, list]:
    misses = []
    for text, klass, paths in FIXTURES:
        found = classify.classify(text, FILES, SYMBOLS)
        if (found["class"], found["paths"]) != (klass, paths):
            misses.append((text, found["class"], found["paths"]))
    return len(FIXTURES) - len(misses), misses


def setUpModule() -> None:
    isolate_wouf_env()


class ClassifierTest(unittest.TestCase):
    def test_twenty_fixture_goals_at_least_eighteen_match(self) -> None:
        self.assertEqual(len(FIXTURES), 20)
        hits, misses = score()
        self.assertGreaterEqual(hits, 18, misses)

    def test_same_input_same_output_whatever_the_order_of_the_files(self) -> None:
        for text, _, _ in FIXTURES:
            first = classify.classify(text, FILES, SYMBOLS)
            shuffled = FILES[:]
            random.Random(7).shuffle(shuffled)
            self.assertEqual(classify.classify(text, shuffled, dict(reversed(list(SYMBOLS.items())))), first)
            self.assertEqual(json.dumps(classify.classify(text, FILES, SYMBOLS), sort_keys=True),
                             json.dumps(first, sort_keys=True))

    def test_a_security_path_brings_a_threat_model_skeleton(self) -> None:
        found = classify.classify("Fix token expiry in src/auth/tokens.py", FILES)
        self.assertEqual(found["security"], ["src/auth/tokens.py"])
        self.assertIn("src/auth/tokens.py", found["threat_model"])
        self.assertTrue(any("2 reviews" in g for g in found["gates"]), found["gates"])
        plain = classify.classify("Fix the crash in src/api/handlers.py", FILES)
        self.assertEqual((plain["security"], plain["threat_model"]), ([], None))

    def test_default_globs_are_in_code_and_work_without_a_section(self) -> None:
        self.assertEqual(classify.DEFAULT_SECURITY_GLOBS,
                         (".agents/bin/**", "**/auth/**", "**/secret*", ".github/workflows/**"))
        self.assertEqual(classify.rules_of(configparser.ConfigParser()), classify.rules_of(None))
        self.assertEqual(classify.rules_of(None)["security_globs"], list(classify.DEFAULT_SECURITY_GLOBS))

    def test_the_classify_section_overrides_globs_and_keywords(self) -> None:
        ini = configparser.ConfigParser(interpolation=None)
        ini.read_string("[classify]\nsecurity_globs = src/billing/**\nkeywords.mechanical = retry, polish\n")
        rules = classify.rules_of(ini)
        self.assertEqual(rules["security_globs"], ["src/billing/**"])
        found = classify.classify("Polish src/billing/invoice.py", FILES, rules=rules)
        self.assertEqual((found["class"], found["security"]), ("security", ["src/billing/invoice.py"]))
        # Own globs keep the default security words: the word token, not the path, makes it security.
        found = classify.classify("Fix token expiry in src/auth/tokens.py", FILES, rules=rules)
        self.assertEqual((found["class"], found["security"]), ("security", []))
        found = classify.classify("Polish docs/usage.md", FILES, rules=rules)
        self.assertEqual(found["class"], "mechanical")

    def test_the_goal_is_masked_capped_and_only_data(self) -> None:
        found = classify.classify(f"Fix README; the key {FAKE_KEY} leaked " + "x" * 3000, FILES)
        self.assertNotIn(FAKE_KEY, json.dumps(found))
        self.assertLessEqual(len(found["text"]), classify.CAP)
        self.assertEqual(classify.CAP, 2000)
        found = classify.classify("Fix `$(rm -rf /)` in README; src/api/routes.py; ../../etc/passwd", FILES)
        self.assertEqual(found["paths"], ["README.md", "src/api/routes.py"])

    def test_security_words_only_raise_the_class(self) -> None:
        for text in ("rotate the api token", "Passwort-Regeln verschärfen", "Design the auth flow for src/ui",
                     "Decide which api key we use"):
            with self.subTest(goal=text):
                found = classify.classify(text, FILES, SYMBOLS)
                self.assertEqual(found["class"], "security")
                self.assertTrue(found["threat_model"])
                self.assertTrue(any(g.startswith("threat model:") for g in found["gates"]), found["gates"])
        plain = classify.classify("Fix the crash in src/api/handlers.py; the keyword list stays", FILES)
        self.assertEqual((plain["class"], plain["threat_model"]), ("standard", None))
        found = classify.class_of("speed up the preview", ["web/sandbox/**"], ["web/sandbox/preview.ts"])
        self.assertEqual((found["class"], found["reasons"]), ("security", ["security word sandbox"]))

    def test_a_new_file_in_a_known_folder_is_a_path(self) -> None:
        found = classify.classify("Implement the goal command in .agents/bin/teamlib/goal.py", FILES)
        self.assertEqual(found["paths"], [".agents/bin/teamlib/goal.py"])
        self.assertEqual(found["class"], "security")


def goal_module():
    loader = SourceFileLoader("wouf_team_goal_test", str(SOURCE / "bin" / "teamlib" / "goal.py"))
    module = module_from_spec(spec_from_loader("wouf_team_goal_test", loader))
    loader.exec_module(module)
    return module


class GoalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.p = Project()
        self.addCleanup(self.p.close)
        self.team_dir = self.p.root / ".agents" / "team"
        (self.p.root / "src" / "auth").mkdir(parents=True)
        (self.p.root / "src" / "auth" / "login.py").write_text("def check_session(token):\n    return bool(token)\n")
        self.p.git("add", "src")
        self.p.git("commit", "-q", "-m", "auth")

    def team(self, *args: str, code: int = 0) -> str:
        done = self.p.team(*args)
        self.assertEqual(done.returncode, code, done.stdout + done.stderr)
        return done.stdout if code == 0 else done.stderr

    def events(self, kind: str = "phase.state") -> list[dict]:
        found = []
        for path in sorted((self.team_dir / "events").glob("*.jsonl")):
            found += [json.loads(line) for line in path.read_text().splitlines()]
        return [e for e in found if e["kind"] == kind]

    def phases(self) -> list[tuple]:
        return [(e["task"], e["data"]["phase"], e["data"]["state"]) for e in self.events()]

    def test_new_writes_the_plan_brief_prints_it_and_the_init_event(self) -> None:
        out = self.team("goal", "new", "Fix the session check in src/auth/login.py", "--repo", str(self.p.root))
        brief = (self.team_dir / "env" / "lead" / "T-1.md").read_text()
        self.assertEqual(out.strip(), brief.strip())
        self.assertIn("Task: T-1\n", brief)
        self.assertIn("Class: security\n", brief)
        self.assertIn("Paths: src/auth/login.py\n", brief)
        self.assertIn("Threat model: ", brief)
        self.assertIn("Gate checks:", brief)
        self.assertIn("Objective: Fix the session check in src/auth/login.py\n", brief)
        self.assertEqual(self.phases(), [("T-1", "INIT", "active")])
        self.assertEqual(self.team("goal", "status", "T-1").splitlines()[0], "T-1 phase INIT (active)")
        self.team("goal", "new", "Fix the typo in file.txt", "--repo", str(self.p.root))
        brief = (self.team_dir / "env" / "lead" / "T-2.md").read_text()
        self.assertIn("Paths: file.txt\n", brief)
        self.assertNotIn("Threat model:", brief)

    def test_goal_new_preserves_existing_padding(self) -> None:
        for ids, expected in ((["T-0155"], "T-0156"), (["T-137", "T-0150"], "T-0151"),
                              (["T-0999"], "T-1000")):
            with self.subTest(ids=ids):
                project = Project()
                try:
                    for task in ids:
                        done = project.team("task", "new", "existing", "--class", "standard", "--id", task)
                        self.assertEqual(done.returncode, 0, done.stderr)
                    done = project.team("goal", "new", "Fix file.txt", "--repo", str(project.root))
                    self.assertEqual(done.returncode, 0, done.stderr)
                    self.assertIn(f"Task: {expected}\n", done.stdout)
                finally:
                    project.close()

    def test_board_next_task_preserves_existing_padding(self) -> None:
        goal = goal_module()
        board = self.team_dir / "board.md"
        for ids, expected in (([], "T-1"), (["T-0155"], "T-0156"),
                              (["T-137", "T-0150"], "T-0151"), (["T-0999"], "T-1000")):
            with self.subTest(ids=ids), mock.patch.object(goal, "team_dir", return_value=self.team_dir):
                board.write_text("".join(f"| {task} | title |\n" for task in ids))
                self.assertEqual(goal.board_next_task(), expected)

    def test_a_security_word_makes_the_goal_security_even_without_paths(self) -> None:
        # The security words read the whole goal, past the cap that keywords and paths stop at.
        long = "design the api " + "lorem ipsum dolor sit amet " * 80 + "and rotate the token"
        for n, text in enumerate(("rotate the api token", "Passwort-Regeln verschärfen", long), 1):
            with self.subTest(goal=text):
                self.team("goal", "new", text, "--repo", str(self.p.root))
                brief = (self.team_dir / "env" / "lead" / f"T-{n}.md").read_text()
                self.assertIn("Class: security\n", brief)
                self.assertIn("Paths: TO FILL\n", brief)
                self.assertIn("Threat model: ", brief)

    def test_symbols_from_the_context_graph_name_paths(self) -> None:
        self.team("goal", "new", "Fix `check_session` for empty tokens", "--repo", str(self.p.root))
        self.assertIn("Paths: src/auth/login.py\n", (self.team_dir / "env" / "lead" / "T-1.md").read_text())

    def test_the_goal_text_is_masked_and_capped(self) -> None:
        out = self.team("goal", "new", f"Fix file.txt, the key {FAKE_KEY} " + "y" * 3000, "--repo", str(self.p.root))
        brief = (self.team_dir / "env" / "lead" / "T-1.md").read_text()
        self.assertNotIn(FAKE_KEY, out + brief + json.dumps(self.events()))
        self.assertNotIn("y" * 2001, brief)

    def test_a_masked_secret_keeps_the_review_floor(self) -> None:
        # A secret-shaped string that holds a former stem ("token"): masked wherever it is shown, still security.
        shaped = "gh" + "p_" + "token" + "a" * 31
        out = self.team("goal", "new", f"inspect {shaped} in file.txt", "--repo", str(self.p.root))
        brief = (self.team_dir / "env" / "lead" / "T-1.md").read_text()
        self.assertIn("Class: security\n", brief)
        self.assertNotIn(shaped, out + brief + json.dumps(self.events()))

    def test_leaving_a_phase_needs_the_lead_and_under_ask_the_human_first(self) -> None:
        self.team("goal", "new", "Fix the typo in file.txt", "--repo", str(self.p.root))
        err = self.team("goal", "concept", "T-1", code=2)
        self.assertIn("human's yes", err)
        self.assertIn("--yes", err)
        self.p.set("autonomy = ask", "autonomy = auto")
        err = self.team("goal", "concept", "T-1", code=2)
        self.assertNotIn("human's yes", err)
        self.assertIn("--yes", err)
        self.assertEqual(self.phases(), [("T-1", "INIT", "active")])
        self.assertIn("no phase is skipped", self.team("goal", "realize", "T-1", "--wave", "1", "--yes", code=2))

    def test_status_of_an_unknown_goal(self) -> None:
        self.assertIn("no goal", self.team("goal", "status", "T-9", code=2))

    def test_readonly_source_fixture_is_copied_writable(self) -> None:
        real_copytree = shutil.copytree

        def copytree_readonly(src, dst, *a, **kw):
            out = real_copytree(src, dst, *a, **kw)
            if Path(src) == SOURCE:
                (Path(out) / "team" / "config.ini").chmod(0o444)
            return out

        with mock.patch.object(shutil, "copytree", side_effect=copytree_readonly):
            proj = Project()
            self.addCleanup(proj.close)
            self.assertTrue(os.access(proj.config, os.W_OK))


class PhaseTest(unittest.TestCase):
    """CONCEPT, REALIZE and VERIFY in process, with the call of team flow and team batch recorded instead."""

    def setUp(self) -> None:
        self.p = Project()
        self.addCleanup(self.p.close)
        self.team_dir = self.p.root / ".agents" / "team"
        (self.p.root / "src" / "auth").mkdir(parents=True)
        (self.p.root / "src" / "auth" / "login.py").write_text("def check_session(token):\n    return bool(token)\n")
        self.p.git("add", "src")
        self.p.git("commit", "-q", "-m", "auth")
        env = {"WOUF_TEAM_DIR": str(self.team_dir), "WOUF_EVENTS_DIR": str(self.team_dir / "events"),
               "WOUF_LOG_DIR": str(self.team_dir / "logs"), "WOUF_STATE_DB": str(self.team_dir / "env" / "state.sqlite")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.g = goal_module()
        self.calls: list = []
        self.g.team_call = lambda args: self.calls.append(list(args)) or 0

    def run_goal(self, *args: str) -> tuple[int, str, str]:
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = self.g.do_goal(list(args))
            except SystemExit as exit:
                code = exit.code
        return code, out.getvalue(), err.getvalue()

    def phases(self) -> list[tuple]:
        found = []
        for path in sorted((self.team_dir / "events").glob("*.jsonl")):
            found += [json.loads(line) for line in path.read_text().splitlines()]
        return [(e["data"]["phase"], e["data"]["state"]) for e in found if e["kind"] == "phase.state"]

    def plan(self) -> Path:
        folder = self.p.root / "plan"
        (folder / "briefs").mkdir(parents=True)
        (folder / "PLAN.md").write_text("| ID | Title | Runner | Wave | Depends |\n|---|---|---|---|---|\n| a | One | w | 1 | - |\n"
                                        "| b | Two | w2 | 1 | - |\n| c | Three | w | 2 | - |\n")
        for name in "abc":
            (folder / "briefs" / f"{name}.md").write_text(f"Task: P-{name}\nClass: standard\nPaths: {name}.txt\n"
                f"Objective: update {name}.txt\nAcceptance criteria: the file is updated\n"
                "Scope: the named file\nOut of scope: other files\nInputs: repository\n"
                "Constraints: standard library only\nExpected output: a commit\n")
        return folder

    def ready_brief(self, task: str = "T-1") -> Path:
        path = self.team_dir / "env" / "lead" / f"{task}.md"
        path.write_text(path.read_text().replace("TO FILL", "filled"))
        return path

    def test_the_four_phases_with_their_events_and_calls(self) -> None:
        code, out, err = self.run_goal("new", "Fix the typo in file.txt", "--repo", str(self.p.root))
        self.assertEqual(code, 0, err)
        self.ready_brief()
        code, out, err = self.run_goal("concept", "T-1", "--yes")
        self.assertEqual(code, 0, err)
        concept = self.team_dir / "env" / "lead" / "T-1.concept.md"
        self.assertEqual(self.calls, [["flow", "T-1", "--brief", str(concept), "--repo", str(self.p.root)]])
        self.assertIn("Class: plan\n", concept.read_text())
        self.assertIn("Proposed class: mechanical", concept.read_text())
        self.assertEqual(self.phases(), [("INIT", "active"), ("INIT", "done"), ("CONCEPT", "active"), ("CONCEPT", "ready")])

        plan = self.plan()
        code, out, err = self.run_goal("realize", "T-1", "--wave", "1", "--plan", str(plan))
        self.assertEqual(code, 2)
        self.assertIn("human's yes", err)
        code, out, err = self.run_goal("realize", "T-1", "--wave", "1", "--plan", str(plan), "--yes")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.calls[-1], ["batch", str(plan), "--wave", "1"])
        # The next wave stays in REALIZE: no phase is left, no --yes needed; the plan folder is remembered.
        code, out, err = self.run_goal("realize", "T-1", "--wave", "2")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.calls[-1], ["batch", str(plan), "--wave", "2"])
        self.assertEqual(self.phases()[4:], [("CONCEPT", "done"), ("REALIZE", "active"), ("REALIZE", "ready"), ("REALIZE", "active"), ("REALIZE", "ready")])

        flows = self.team_dir / "flows" / "P-a"
        flows.mkdir(parents=True)
        (flows / "state.json").write_text(json.dumps({"task": "P-a", "status": "ready", "head": HEAD, "steps": [
            {"kind": "review", "verdict": "approve", "head": HEAD}]}))
        log = state.store()
        log.event_append("merge.proposed", "lead", "P-a", {"task": "P-a", "head": HEAD, "branch": "wouf/P-a",
                                                           "verdict": "approve"})
        log.event_append("task.state", "lead", "P-a", {"state": "review"})
        log.event_append("task.state", "lead", "P-b", {"state": "working"})
        code, out, err = self.run_goal("verify", "T-1", "--yes")
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertIn(f"a P-a review approve {HEAD[:12]} · merge proposal {HEAD[:12]}", lines)
        self.assertIn("b P-b review none · no merge proposal", lines)
        self.assertIn("c P-c review none · no merge proposal", lines)
        self.assertEqual(self.phases()[-2:], [("REALIZE", "done"), ("VERIFY", "active")])

        code, out, err = self.run_goal("status", "T-1")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines(), ["T-1 phase VERIFY (active)", "a (P-a): task review; flow not-started",
                                            "b (P-b): task working; flow not-started",
                                            "c (P-c): task not-started; flow not-started"])

    def test_recovery_status_and_failed_execution(self) -> None:
        self.run_goal("new", "Fix file.txt", "--repo", str(self.p.root))
        self.ready_brief()
        self.g.team_call = lambda args: 1
        self.assertEqual(self.run_goal("concept", "T-1", "--yes")[0], 1)
        self.assertEqual(self.phases()[-1], ("CONCEPT", "blocked"))
        plan = self.plan()
        self.assertNotEqual(self.run_goal("realize", "T-1", "--wave", "1", "--plan", str(plan), "--yes")[0], 0)
        self.g.team_call = lambda args: 0
        self.assertEqual(self.run_goal("concept", "T-1")[0], 0)
        self.assertEqual(self.run_goal("realize", "T-1", "--wave", "1", "--plan", str(plan), "--yes")[0], 0)
        (self.team_dir / "env" / "goals" / "T-1.json").unlink()
        recovered = self.g.load_goal("T-1")
        self.assertEqual(recovered["repo"], str(self.p.root))
        self.assertEqual(recovered["plan"], str(plan))
        state.store().event_append("task.state", "lead", "P-a", {"state": "review"})
        state.store().event_append("flow.step", "lead", "P-a", {"kind": "end", "status": "blocked"})
        out = self.run_goal("status", "T-1")[1]
        self.assertIn("task review", out)
        self.assertIn("flow blocked", out)
        self.assertEqual(self.run_goal("verify", "T-1", "--yes")[0], 0)
        self.assertEqual(self.run_goal("complete", "T-1", "--yes")[0], 0)
        self.assertEqual(self.phases()[-1], ("VERIFY", "done"))
        self.assertNotEqual(self.run_goal("verify", "T-1")[0], 0)

    def test_blocked_wave_cannot_be_masked_by_a_later_wave(self) -> None:
        self.run_goal("new", "Fix file.txt", "--repo", str(self.p.root))
        self.ready_brief()
        self.run_goal("concept", "T-1", "--yes")
        plan = self.plan()
        self.g.team_call = lambda args: 1
        self.assertEqual(self.run_goal("realize", "T-1", "--wave", "1", "--plan", str(plan), "--yes")[0], 1)
        self.g.team_call = lambda args: 0
        code, _, err = self.run_goal("realize", "T-1", "--wave", "2")
        self.assertNotEqual(code, 0)
        self.assertIn("wave 1", err)
        self.assertEqual(self.phases()[-1], ("REALIZE", "blocked"))
        self.assertNotEqual(self.run_goal("verify", "T-1", "--yes")[0], 0)
        self.assertEqual(self.g.load_goal("T-1")["wave"], "1")
        other_plan = self.p.root / "other-plan"
        other_plan.mkdir()
        (other_plan / "PLAN.md").write_text((plan / "PLAN.md").read_text())
        self.assertNotEqual(self.run_goal("realize", "T-1", "--wave", "1", "--plan", str(other_plan))[0], 0)
        self.assertEqual(self.run_goal("realize", "T-1", "--wave", "1")[0], 0)
        self.assertEqual(self.run_goal("realize", "T-1", "--wave", "2")[0], 0)

    def test_recover_wave_requires_all_flows_ready(self) -> None:
        self.run_goal("new", "Fix file.txt", "--repo", str(self.p.root))
        self.ready_brief()
        self.run_goal("concept", "T-1", "--yes")
        plan = self.plan()
        self.g.team_call = lambda args: 1
        self.assertEqual(self.run_goal("realize", "T-1", "--wave", "1", "--plan", str(plan), "--yes")[0], 1)
        self.assertNotEqual(self.run_goal("recover", "T-1", "--yes")[0], 0)
        for task in ("P-a", "P-b"):
            folder = self.team_dir / "flows" / task
            folder.mkdir(parents=True)
            (folder / "state.json").write_text(json.dumps({"status": "ready"}))
        self.assertNotEqual(self.run_goal("recover", "T-1")[0], 0)
        self.assertEqual(self.run_goal("recover", "T-1", "--yes")[0], 0)
        self.assertEqual(self.phases()[-1], ("REALIZE", "ready"))
        self.assertEqual(self.run_goal("verify", "T-1", "--yes")[0], 0)

    def test_interrupted_batch_is_recorded_as_blocked(self) -> None:
        self.run_goal("new", "Fix file.txt", "--repo", str(self.p.root))
        self.ready_brief()
        self.run_goal("concept", "T-1", "--yes")
        plan = self.plan()
        with mock.patch.object(self.g, "batch_call", side_effect=SystemExit(2)):
            self.assertEqual(self.run_goal("realize", "T-1", "--wave", "1", "--plan", str(plan), "--yes")[0], 2)
        self.assertEqual(self.phases()[-1], ("REALIZE", "blocked"))
        self.assertNotEqual(self.run_goal("verify", "T-1", "--yes")[0], 0)

    def test_verify_reports_stale_heads_and_security_review_count(self) -> None:
        self.run_goal("new", "Fix file.txt", "--repo", str(self.p.root))
        self.ready_brief()
        self.run_goal("concept", "T-1", "--yes")
        plan = self.plan()
        self.run_goal("realize", "T-1", "--wave", "1", "--plan", str(plan), "--yes")
        folder = self.team_dir / "flows" / "P-a"
        folder.mkdir(parents=True)
        (folder / "state.json").write_text(json.dumps({"status": "blocked", "class": "security", "head": "b" * 40, "branch": "test", "repo": str(self.p.root),
            "steps": [{"kind": "review", "index": 1, "head": "a" * 40, "verdict": "approve"}]}))
        state.store().event_append("merge.proposed", "lead", "P-a", {"head": "c" * 40, "branch": "test", "verdict": "approve"})
        with mock.patch.object(self.g.leaf("flow"), "head_of", return_value="d" * 40):
            out = self.run_goal("verify", "T-1", "--yes")[1]
        self.assertIn("branch/flow head mismatch", out)
        self.assertIn("d" * 40, out)
        self.assertIn("inspection", out)
        self.assertIn("mismatch", out)
        self.assertIn("0/2", out)
        self.assertIn("bbbbbbbbbbbb", out)
        self.assertIn("aaaaaaaaaaaa", out)
        self.assertIn("cccccccccccc", out)

    def test_explicit_goal_id_rejects_task_reservations(self) -> None:
        state.store().event_append("task.created", "lead", "T-8", {"title": "Existing", "class": "standard", "owner": "lead"})
        self.assertNotEqual(self.run_goal("new", "Fix file.txt", "--task", "t-8", "--repo", str(self.p.root))[0], 0)
        self.assertNotEqual(self.run_goal("new", "Fix file.txt", "--task", "T.8", "--repo", str(self.p.root))[0], 0)

    def test_a_phase_is_never_skipped(self) -> None:
        self.run_goal("new", "Fix the typo in file.txt", "--repo", str(self.p.root))
        code, out, err = self.run_goal("verify", "T-1", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("INIT", err)
        self.assertEqual(self.calls, [])

    def test_an_untouched_or_incomplete_draft_brief_is_refused_before_concept(self) -> None:
        self.run_goal("new", "Fix the session check in src/auth/login.py", "--repo", str(self.p.root))
        draft = (self.team_dir / "env" / "lead" / "T-1.md").read_text()
        self.assertIn("TO FILL", draft)
        self.assertGreaterEqual(len(self.g.brief_problems(draft)), 6)
        code, out, err = self.run_goal("concept", "T-1", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("TO FILL", err)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.phases(), [("INIT", "active")])
        # Security draft with filled sections but missing Threat model is still refused.
        path = self.ready_brief()
        path.write_text( "\n".join(ln for ln in path.read_text().splitlines() if not ln.startswith("Threat model:")))
        code, out, err = self.run_goal("concept", "T-1", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("Threat model", err)
        self.assertEqual(self.phases(), [("INIT", "active")])
        # Restoring Threat model lets concept proceed.
        path.write_text(path.read_text() + "\nThreat model: session fixation via login\n")
        code, out, err = self.run_goal("concept", "T-1", "--yes")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.phases(), [("INIT", "active"), ("INIT", "done"), ("CONCEPT", "active"), ("CONCEPT", "ready")])

    def test_header_syntax_is_aligned_with_conversion_and_flow_parsing(self) -> None:
        self.run_goal("new", "Fix the session check in src/auth/login.py", "--repo", str(self.p.root))
        path = self.ready_brief()
        base = path.read_text()
        # Non-exact or multiline header keys that flow's brief_header ignores are refused before conversion.
        for bad, needle in (
            (base.replace("Class: security", "class: security"), "Class:"),
            (base.replace("Class: security", "## Class\nsecurity"), "Class:"),
            (base.replace("Class: security", "Class:\nsecurity"), "Class:"),
            (base.replace("Task: T-1", "task: T-1"), "Task:"),
            (base.replace("Task: T-1", "## Task\nT-1"), "Task:"),
            (base.replace("Task: T-1", "Task:\nT-1"), "Task:"),
            (base.replace("Paths: src/auth/login.py", "paths: src/auth/login.py"), "Paths"),
            (base.replace("Paths: src/auth/login.py", "## Paths\nsrc/auth/login.py"), "Paths"),
            (base.replace("Paths: src/auth/login.py", "Paths:\nsrc/auth/login.py"), "Paths"),
            (base.replace("Paths: src/auth/login.py", "Paths: , "), "Paths"),
        ):
            with self.subTest(bad=bad.splitlines()[:4]):
                path.write_text(bad)
                self.assertTrue(self.g.brief_problems(bad), bad)
                code, out, err = self.run_goal("concept", "T-1", "--yes")
                self.assertEqual(code, 2)
                self.assertIn(needle, err)
                self.assertEqual(self.calls, [])
                self.assertEqual(self.phases(), [("INIT", "active")])
        # Accepted header and section variants: exact header keys with tabs/spaces and case-insensitive Class value,
        # plus markdown headings and multiline values for body sections.
        accepted = (
            "Task:\tT-1  \n"
            "Class:\tSECURITY  \n"
            "Paths: src/auth/login.py, README.md\n"
            "Objective: Fix the session check in src/auth/login.py\n\n"
            "## Acceptance criteria\n- session check rejects empty tokens\n\n"
            "Scope:\n  src/auth/login.py\n"
            "Out of scope: billing\n"
            "Inputs: the repository\n"
            "Constraints: stdlib only\n"
            "## Expected output\ncommit and return block\n"
            "## threat model\nsession fixation via login\n"
        )
        # If Threat model is omitted on Class: SECURITY, it is still recognized as security and refused.
        no_threat = accepted.rsplit("## threat model", 1)[0]
        self.assertIn("the brief needs Threat model (non-empty)", self.g.brief_problems(no_threat))
        path.write_text(accepted)
        self.assertEqual(self.g.brief_problems(accepted), [])
        code, out, err = self.run_goal("concept", "T-1", "--yes")
        self.assertEqual(code, 0, err)
        concept = (self.team_dir / "env" / "lead" / "T-1.concept.md").read_text()
        self.assertIn("Class: plan\nProposed class: security\n", concept)
        self.assertEqual(self.g.brief_header(concept), {
            "task": "T-1", "class": "plan", "paths": ["src/auth/login.py", "README.md"], "scout": None,
        })


# Stand-in CLI for live CONCEPT and REALIZE flow/batch runs without stubbing team_call.
GOAL_CLI = r"""#!/bin/sh
sign=$(printf '%s\n' "$*" | sed -n 's/.*starting with the line `\([^`]*\) ·`.*/\1/p' | head -1)
mode=$(cat ../../mode 2>/dev/null || echo ok)
case "$sign" in
  *Scout*)
    echo "context" > out/CONTEXT.md
    echo "$sign · ready-for-review"
    echo "Questions: none"
    ;;
  *Planner*)
    if [ "$mode" = "ask" ] && ! grep -q "Answer from the lead" in/BRIEF.md; then
      echo "$sign · blocked"
      echo "Questions: which wave?"
      exit 0
    fi
    if [ "$mode" = "limit" ]; then
      echo "You've hit your usage limit. Try again in 2 hours."
      exit 1
    fi
    if [ "$mode" = "fail" ]; then
      exit 1
    fi
    if [ "$mode" = "noplan" ]; then
      echo "$sign · ready-for-review"
      echo "Questions: none"
      exit 0
    fi
    mkdir -p out/briefs
    printf '| ID | Title | Runner | Wave | Depends |\n|---|---|---|---|---|\n| a | First | w | 1 | - |\n| b | Second | w2 | 1 | - |\n' > out/PLAN.md
    printf 'Task: P-a\nClass: standard\nPaths: ext_a.txt\nObjective: update ext_a.txt\nAcceptance criteria: the file is updated\nScope: the named file\nOut of scope: other files\nInputs: repository\nConstraints: standard library only\nExpected output: a commit\n' > out/briefs/a.md
    printf 'Task: P-b\nClass: standard\nPaths: ext_b.txt\nObjective: update ext_b.txt\nAcceptance criteria: the file is updated\nScope: the named file\nOut of scope: other files\nInputs: repository\nConstraints: standard library only\nExpected output: a commit\n' > out/briefs/b.md
    echo "$sign · ready-for-review"
    echo "Questions: none"
    ;;
  *Review*)
    echo "$sign · APPROVE $(cd repo && git rev-parse HEAD)"
    echo "Blocking: none"
    ;;
  *Worker*)
    path=$(sed -n 's/^Paths: *\([^ ]*\).*/\1/p' in/BRIEF.md | head -1)
    (cd repo && echo work >> "$path" && git add "$path" && git commit -q -m "work on $path")
    echo "$sign · ready-for-review"
    echo "Questions: none"
    ;;
esac
"""


class LiveGoalTest(unittest.TestCase):
    """CONCEPT resumption (questions, limits, failures, interrupted runs) and REALIZE on an external repository,
    exercised through the real flow and batch commands without stubbing team_call."""

    def setUp(self) -> None:
        self.p = Project(mode="host", runner="fake")
        self.addCleanup(self.p.close)
        self.team_dir = self.p.root / ".agents" / "team"
        bots = self.team_dir / "bots"
        for member, source in (("worker2", "worker"), ("scout2", "scout")):
            text = (bots / f"{source}.md").read_text().replace(f"id: {source}\n", f"id: {member}\n")
            (bots / f"{member}.md").write_text(text)
        for member in ("worker", "worker2", "scout", "scout2", "planner", "review"):
            (self.team_dir / "env" / member).mkdir(parents=True, exist_ok=True)
            (self.team_dir / "env" / member / "fake-cli.sh").write_text(GOAL_CLI)
        self.p.config.write_text(self.p.config.read_text() + "\n[flow]\ngate = true\n")
        self.p.store_secret("ALPHA_KEY", "alpha-secret-value-123")
        # External repository separate from self.p.root (PROJECT).
        self.ext = Path(self.p.tmp.name) / "ext-repo"
        self.ext.mkdir()
        for args in (["init", "-q"], ["config", "user.name", "Test"], ["config", "user.email", "t@example.invalid"]):
            subprocess.run(["git", "-C", str(self.ext), *args], check=True, capture_output=True)
        for name in ("ext_a.txt", "ext_b.txt"):
            (self.ext / name).write_text("init\n")
        subprocess.run(["git", "-C", str(self.ext), "add", "ext_a.txt", "ext_b.txt"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.ext), "commit", "-q", "-m", "ext init"], check=True, capture_output=True)

    def set_planner_mode(self, mode: str) -> None:
        (self.team_dir / "env" / "planner" / "mode").write_text(mode)

    def fill_draft(self, task: str = "T-1") -> None:
        path = self.team_dir / "env" / "lead" / f"{task}.md"
        path.write_text(path.read_text().replace("TO FILL", "filled"))

    def test_concept_resumes_question_limit_failure_and_interrupted_flow(self) -> None:
        done = self.p.team("goal", "new", "Update ext_a.txt and ext_b.txt", "--repo", str(self.ext))
        self.assertEqual(done.returncode, 0, done.stderr)
        # Untouched draft is refused by CLI before concept starts.
        refused = self.p.team("goal", "concept", "T-1", "--yes")
        self.assertEqual(refused.returncode, 2)
        self.assertIn("TO FILL", refused.stderr)
        self.fill_draft()
        draft_file = self.team_dir / "env" / "lead" / "T-1.md"
        filled_text = draft_file.read_text()
        # Lowercase 'class: security' is refused by CLI instead of dispatching standard flow.
        draft_file.write_text(
            filled_text.replace("Class: standard", "class: security") + "\nThreat model: filled\n"
        )
        bad_header = self.p.team("goal", "concept", "T-1", "--yes")
        self.assertEqual(bad_header.returncode, 2)
        self.assertIn("Class:", bad_header.stderr)
        self.assertFalse((self.team_dir / "flows" / "T-1" / "state.json").exists())
        # Accepted variant 'Class: SECURITY' with Threat model converts to Class: plan and runs planner.
        draft_file.write_text(
            filled_text.replace("Class: standard", "Class: SECURITY") + "\nThreat model: filled\n"
        )

        # 1. Question -> status -> resume with --answer (no --brief/--repo refusal).
        self.set_planner_mode("ask")
        asked = self.p.team("goal", "concept", "T-1", "--yes")
        self.assertEqual(asked.returncode, 5, asked.stdout + asked.stderr)
        self.assertIn("question which wave?", asked.stdout)
        status = self.p.team("goal", "concept", "T-1", "--status")
        self.assertEqual(status.returncode, 5, status.stdout + status.stderr)
        self.assertIn("question which wave?", status.stdout)

        # 2. Next run hits usage limit (exit 4), then resumes with --retry.
        self.set_planner_mode("limit")
        limited = self.p.team("goal", "concept", "T-1", "--answer", "wave 1")
        self.assertEqual(limited.returncode, 4, limited.stdout + limited.stderr)
        self.assertIn("limited", limited.stdout)
        still_limited = self.p.team("goal", "concept", "T-1")
        self.assertEqual(still_limited.returncode, 4, still_limited.stdout + still_limited.stderr)

        # 3. Retry hits a failed/blocked planner (no PLAN.md -> exit 1), then --retry succeeds.
        self.set_planner_mode("noplan")
        blocked = self.p.team("goal", "concept", "T-1", "--retry")
        self.assertEqual(blocked.returncode, 1, blocked.stdout + blocked.stderr)
        self.assertIn("no out/PLAN.md", blocked.stdout)

        # 4. Detached resume (--detach) runs the flow in the background to ready.
        detached = self.p.team("goal", "concept", "T-1", "--retry", "--detach")
        self.assertEqual(detached.returncode, 0, detached.stdout + detached.stderr)
        self.assertIn("runs in the background", detached.stdout)
        self.set_planner_mode("ok")
        for _ in range(100):
            status = self.p.team("goal", "concept", "T-1", "--status")
            if status.returncode != 3:
                break
            import time
            time.sleep(0.1)
        # Wait for the detached process to exit cleanly before manipulating state.json.
        state_file = self.team_dir / "flows" / "T-1" / "state.json"
        pid = json.loads(state_file.read_text()).get("pid")
        if isinstance(pid, int) and pid > 0:
            for _ in range(100):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                import time
                time.sleep(0.05)

        # 5. Interrupted / pre-existing flow state (status running with scout done) resumes cleanly without --retry.
        flow_state = json.loads(state_file.read_text())
        flow_state["status"] = "running"
        flow_state["steps"] = [st for st in flow_state["steps"] if st["kind"] == "scout"]
        state_file.write_text(json.dumps(flow_state))
        self.set_planner_mode("ok")
        resumed = self.p.team("goal", "concept", "T-1")
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertIn("ready T-1", resumed.stdout)

        # 6. REALIZE runs batch on the external repository, not self.p.root.
        realized = self.p.team("goal", "realize", "T-1", "--wave", "1", "--yes")
        self.assertEqual(realized.returncode, 0, realized.stdout + realized.stderr)
        ext_branches = subprocess.run(["git", "-C", str(self.ext), "branch", "--list", "wouf/*"],
                                      check=True, capture_output=True, text=True).stdout
        self.assertIn("wouf/P-a", ext_branches)
        self.assertIn("wouf/P-b", ext_branches)
        root_branches = self.p.git("branch", "--list", "wouf/P-*")
        self.assertEqual(root_branches.strip(), "")
        for task in ("P-a", "P-b"):
            pkg_flow = json.loads((self.team_dir / "flows" / task / "state.json").read_text())
            self.assertEqual(Path(pkg_flow["repo"]).resolve(), self.ext.resolve())
            self.assertEqual(pkg_flow["status"], "ready")


if __name__ == "__main__":
    unittest.main()
