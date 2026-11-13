"""Task classes from config.ini (P02): the seven built-ins, and every [class.<name>] section whose name is a lowercase
letter followed by up to 30 of a-z, 0-9 and - adds one. Every class check accepts the same classes."""

from __future__ import annotations

import configparser
import io
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from support import isolate_wouf_env, writable_copy
from test_team_cli import SOURCE, Project

sys.path.insert(0, str(SOURCE / "bin"))
from teamlib import classify, core, flow, goal  # noqa: E402

BUILT_IN = ("mechanical", "standard", "complex", "security", "decision", "analysis", "plan")
DOCS = "\n[class.docs]\ntier = strong\n"
SECTIONS = ("Acceptance criteria: done\nScope: docs\nOut of scope: code\nInputs: none\nConstraints: none\n"
            "Expected output: a commit\n")


def ini_of(text: str) -> configparser.ConfigParser:
    ini = configparser.ConfigParser(interpolation=None)
    ini.read_string(text)
    return ini


def setUpModule() -> None:
    isolate_wouf_env()


class TeamDir(unittest.TestCase):
    """A copy of the shipped team folder, named by WOUF_TEAM_DIR, with text appended to its config.ini."""

    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        self.team = tmp / "team"
        writable_copy(SOURCE / "team", self.team)
        self.base = (self.team / "config.ini").read_text(encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {"WOUF_TEAM_DIR": str(self.team)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def configure(self, extra: str = "") -> configparser.ConfigParser:
        (self.team / "config.ini").write_text(self.base + extra, encoding="utf-8")
        return core.config()

    def refused(self, call, *args) -> str:
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit):
            call(*args)
        return err.getvalue()


class KnownClassesTest(TeamDir):
    def test_the_built_ins_stay_the_tuple(self) -> None:
        self.assertEqual(core.CLASSES, BUILT_IN)
        self.assertEqual(core.known_classes(self.configure()), BUILT_IN)
        self.assertEqual(core.known_classes(), BUILT_IN)

    def test_sections_add_classes_in_config_order_without_duplicates(self) -> None:
        ini = self.configure("\n[class.zeta]\ntier = mid\n[class.standard]\nrunner = claude\n" + DOCS)
        self.assertEqual(core.known_classes(ini), BUILT_IN + ("zeta", "docs"))
        self.assertEqual(core.known_classes(), BUILT_IN + ("zeta", "docs"))

    def test_a_name_outside_the_pattern_is_ignored(self) -> None:
        longest, too_long = "a" + "b" * 30, "a" + "b" * 31
        ini = ini_of("".join(f"[class.{n}]\ntier = strong\n"
                             for n in ("Docs", "a_b", "9x", "-x", "", too_long, longest, "x-1")))
        self.assertEqual(core.known_classes(ini), BUILT_IN + (longest, "x-1"))
        self.assertEqual(core.class_sections(ini)[1], ["Docs", "a_b", "9x", "-x", "", too_long])

    def test_without_a_config_only_the_built_ins(self) -> None:
        (self.team / "config.ini").unlink()
        self.assertEqual(core.known_classes(), BUILT_IN)


class BriefHeaderTest(TeamDir):
    def test_a_configured_class_is_accepted(self) -> None:
        self.configure(DOCS)
        self.assertEqual(core.brief_header("Class: docs\n")["class"], "docs")
        self.assertEqual(core.brief_header("Class: docs\n", core.known_classes())["class"], "docs")

    def test_without_the_section_it_is_refused_naming_the_known_classes(self) -> None:
        self.configure()
        err = self.refused(core.brief_header, "Class: docs\n")
        self.assertIn("'docs' is not one of " + ", ".join(BUILT_IN), err)

    def test_classes_given_are_the_ones_checked(self) -> None:
        self.configure(DOCS)
        self.assertIn("not one of " + ", ".join(BUILT_IN), self.refused(core.brief_header, "Class: docs\n", BUILT_IN))
        self.assertEqual(core.brief_header("Class: Docs\n", ("docs",))["class"], "docs")


class ClassOptionsTest(TeamDir):
    def test_a_configured_class_with_a_tier(self) -> None:
        options = flow.class_options(self.configure(DOCS), "docs")
        self.assertEqual((options["bot"], options["reviews"], options["scout"], options["effort"], options["tier"]),
                         ("worker", 1, False, None, "strong"))

    def test_a_configured_class_without_a_tier_dies_naming_it(self) -> None:
        ini = self.configure("\n[class.docs]\nreviews = 2\n")
        self.assertIn("[class.docs] tier", self.refused(flow.class_options, ini, "docs"))

    def test_an_unknown_class_dies(self) -> None:
        self.assertIn("unknown class 'docs'", self.refused(flow.class_options, self.configure(), "docs"))

    def test_built_in_classes_keep_their_defaults(self) -> None:
        ini = self.configure(DOCS)
        for name, defaults in flow.CLASS_DEFAULTS.items():
            with self.subTest(name=name):
                options = flow.class_options(ini, name)
                self.assertEqual((options["bot"], options["tier"], options["effort"], options["reviews"],
                                  options["scout"]),
                                 (defaults["bot"], defaults["tier"], defaults["effort"], int(defaults["reviews"]),
                                  defaults["scout"] == "yes"))


class LoadPlanTest(TeamDir):
    def plan(self, klass: str) -> Path:
        folder = self.team.parent / "plan"
        (folder / "briefs").mkdir(parents=True)
        (folder / "PLAN.md").write_text("| ID | Wave | Runner | Depends |\n|---|---|---|---|\n| P1 | 1 | w | none |\n")
        (folder / "briefs" / "P1.md").write_text(f"Task: T-5\nClass: {klass}\nPaths: docs/**\nObjective: write\n"
                                                  + SECTIONS)
        return folder

    def test_a_configured_class_is_a_package_class(self) -> None:
        self.configure(DOCS)
        [package] = flow.load_plan(self.plan("docs"))
        self.assertEqual(package["class"], "docs")

    def test_without_the_section_it_is_refused(self) -> None:
        self.configure()
        with self.assertRaisesRegex(ValueError, "executable package class") as caught:
            flow.load_plan(self.plan("docs"))
        message = str(caught.exception)
        for name in ("mechanical", "standard", "complex", "security", "decision", "analysis"):
            self.assertIn(name, message)
        self.assertNotIn("docs", message.split("one of", 1)[1])
        self.assertNotIn("plan", message.split("one of", 1)[1])

    def test_the_refusal_names_a_configured_class(self) -> None:
        self.configure(DOCS)
        with self.assertRaisesRegex(ValueError, "one of .*docs"):
            flow.load_plan(self.plan("nope"))


class ClassifyTest(TeamDir):
    KEYWORDS = "\n[classify]\nkeywords.docs = guide, readme\n"

    def test_rules_have_a_key_for_every_known_class(self) -> None:
        ini = self.configure(DOCS)
        rules = classify.rules_of(ini)
        self.assertEqual(tuple(rules["keywords"]), core.known_classes(ini))
        self.assertEqual(rules["keywords"]["docs"], [])
        self.assertEqual(tuple(classify.rules_of(None)["keywords"]), BUILT_IN)
        self.assertEqual(tuple(classify.rules_of(self.configure())["keywords"]), BUILT_IN)

    def test_configured_keywords_choose_the_class(self) -> None:
        ini = self.configure(DOCS + self.KEYWORDS)
        self.assertEqual(classify.rules_of(ini)["keywords"]["docs"], ["guide", "readme"])
        found = classify.classify("update the readme README.md", ["README.md"], rules=classify.rules_of(ini))
        self.assertEqual(found["class"], "docs")
        self.assertEqual(found["reasons"], ["keyword readme"])

    def test_built_in_classes_are_tried_first(self) -> None:
        rules = classify.rules_of(self.configure(DOCS + self.KEYWORDS))
        self.assertEqual(classify.classify("refactor the readme README.md", ["README.md"], rules=rules)["class"],
                         "complex")
        self.assertEqual(classify.classify("rename the readme README.md", ["README.md"], rules=rules)["class"],
                         "mechanical")

    def test_configured_classes_in_config_order(self) -> None:
        ini = self.configure("\n[class.zeta]\ntier = mid\n" + DOCS + "\n[classify]\nkeywords.docs = readme\n"
                             "keywords.zeta = readme\n")
        rules = classify.rules_of(ini)
        self.assertEqual(classify.classify("the readme README.md", ["README.md"], rules=rules)["class"], "zeta")

    def test_without_the_class_section_the_keywords_are_not_read(self) -> None:
        rules = classify.rules_of(self.configure(self.KEYWORDS))
        self.assertNotIn("docs", rules["keywords"])
        self.assertEqual(classify.classify("update the readme README.md", ["README.md"], rules=rules)["class"],
                         "standard")

    def test_security_words_come_from_the_classify_section(self) -> None:
        self.assertEqual(classify.rules_of(None)["security_words"], list(classify.DEFAULT_SECURITY_WORDS))
        self.assertEqual(classify.rules_of(self.configure())["security_words"], list(classify.DEFAULT_SECURITY_WORDS))
        rules = classify.rules_of(self.configure("\n[classify]\nsecurity_words = *billing*, invoice\n"))
        self.assertEqual(rules["security_words"], ["*billing*", "invoice"])
        self.assertEqual(classify.classify("update the billing readme README.md", ["README.md"], rules=rules)["class"],
                         "security")
        # The former stems stay a floor: configured words add to them, never take them away.
        self.assertEqual(classify.class_of("rotate the token", ["src/api/**"], ["src/api/a.py"], rules)["class"],
                         "security")
        self.assertEqual(classify.classify("rotate the token in src/api/a.py", ["src/api/a.py"], rules=rules)["class"],
                         "security")

    def test_security_globs_alone_keep_the_default_security_words(self) -> None:
        for extra in ("\n[classify]\nsecurity_globs = infra/**\n", "\n[classify]\nsecurity_words =\n"):
            rules = classify.rules_of(self.configure(extra))
            self.assertEqual(rules["security_words"], list(classify.DEFAULT_SECURITY_WORDS))
            for text in ("rotate the api token", "Passwort-Regeln der api"):
                with self.subTest(config=extra, goal=text):
                    found = classify.class_of(text, ["src/api/**"], ["src/api/routes.py"], rules)
                    self.assertEqual(found["class"], "security")
                    self.assertEqual(classify.classify(text, ["src/api/routes.py"], rules=rules)["class"], "security")

    def test_class_of_applies_the_rules_to_given_paths(self) -> None:
        rules = classify.rules_of(self.configure(DOCS + self.KEYWORDS))
        self.assertEqual(classify.class_of("update the readme", ["docs/**"], ["docs/readme.md"], rules)["class"],
                         "docs")
        self.assertEqual(classify.class_of("update it", ["src/auth/**"], ["src/auth/x.py"], rules)["class"],
                         "security")
        # Without paths: bin/project's standard, not plan, unless both rules ask for no review.
        self.assertEqual(classify.class_of("update it", [], [], rules)["class"], "standard")
        self.assertEqual(classify.class_of("decide which one", [], [], rules)["class"], "decision")

    def test_goal_rules_carry_the_reviews_of_a_configured_class(self) -> None:
        ini = self.configure(DOCS + self.KEYWORDS)
        self.assertEqual(goal.rules(ini)["reviews"]["docs"], 1)


class GoalBriefTest(TeamDir):
    BRIEF = "Task: T-3\nClass: docs\nPaths: docs/**\nObjective: write the guide\n" + SECTIONS

    def test_a_configured_class_passes_the_brief_check(self) -> None:
        self.configure(DOCS)
        self.assertEqual(goal.brief_problems(self.BRIEF), [])

    def test_without_the_section_the_check_names_the_known_classes(self) -> None:
        self.configure()
        self.assertEqual(goal.brief_problems(self.BRIEF),
                         [f"the brief needs a Class: line, one of {', '.join(BUILT_IN)}"])


class CommandTest(unittest.TestCase):
    """team task new, team brief new and team doctor in a throwaway project."""

    def setUp(self) -> None:
        self.p = Project()
        self.addCleanup(self.p.close)

    def configure(self, extra: str) -> None:
        with self.p.config.open("a", encoding="utf-8") as handle:
            handle.write(extra)

    def test_task_new_accepts_a_configured_class(self) -> None:
        refused = self.p.team("task", "new", "write the guide", "--class", "docs")
        self.assertEqual(refused.returncode, 2)
        self.assertIn("class 'docs' is not one of " + ", ".join(BUILT_IN), refused.stderr)
        self.configure(DOCS)
        done = self.p.team("task", "new", "write the guide", "--class", "docs")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.splitlines(), ["T-1 todo (docs, owner lead)"])

    def test_brief_new_accepts_a_configured_class(self) -> None:
        refused = self.p.team("brief", "new", "--class", "docs", "write the guide")
        self.assertEqual(refused.returncode, 2)
        self.assertIn("class 'docs' is not one of " + ", ".join(BUILT_IN), refused.stderr)
        self.configure(DOCS)
        done = self.p.team("brief", "new", "--class", "docs", "write the guide")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("Class: docs", done.stdout.splitlines())

    def test_doctor_names_an_ignored_section_once(self) -> None:
        self.configure("\n[class.Docs]\ntier = strong\n[class.a_b]\ntier = strong\n" + DOCS)
        done = self.p.team("doctor")
        for name in ("Docs", "a_b"):
            with self.subTest(name=name):
                self.assertEqual(done.stderr.count(f"[class.{name}]"), 1, done.stderr)
                self.assertNotIn(f"[class.{name}]", done.stdout)
        self.assertNotIn("[class.docs]", done.stderr + done.stdout)

    def test_doctor_says_nothing_of_valid_sections(self) -> None:
        self.configure(DOCS)
        self.assertNotIn("[class.", self.p.team("doctor").stderr)


if __name__ == "__main__":
    unittest.main()
