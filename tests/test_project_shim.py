"""bin/project is a shim over teamlib: `project brief` writes the class teamlib's rules give the goal and the paths it
found, `project check` reports exactly teamlib's brief problems."""

from __future__ import annotations

import configparser
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import isolate_wouf_env, writable_copy  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / ".agents" / "bin" / "project"
sys.path.insert(0, str(ROOT / ".agents" / "bin"))
from teamlib import classify, goal  # noqa: E402
from teamlib.flow import class_options  # noqa: E402  (the reviews team receipt need reports)

FILES = ("src/api/status.py", "src/api/routes.py", "src/auth/login.py", "docs/readme.md", "web/billing/invoice.ts",
         ".agents/bin/team", "web/sandbox/preview.ts")
# One goal per class, each with the class it gets; the security ones by a path, by a word only and by a security
# word in the name of a path (web/sandbox/**, as bin/project's own classifier did).
GOALS = (
    ("fix a typo in the docs readme", "mechanical"),
    ("Add a status endpoint to the api", "standard"),
    ("refactor the billing invoice", "complex"),
    ("investigate why invoices are slow", "analysis"),
    ("decide which billing provider we use", "decision"),
    ("harden the bin scripts", "security"),
    ("change the login flow", "security"),
    ("rotate the api token", "security"),
    ("Passwort-Regeln verschärfen", "security"),
    ("speed up the preview", "security"),
    ("redesign the api", "complex"),
)
SECTIONS = ("Acceptance criteria: the endpoint answers 200\nScope: src/api\nOut of scope: auth\nInputs: the repository\n"
            "Constraints: stdlib only\nExpected output: commits and the return block\n")
BROKEN = {
    "no Class": "Task: T-1\nPaths: src/**\nObjective: do it\n" + SECTIONS,
    "no Paths": "Task: T-1\nClass: standard\nObjective: do it\n" + SECTIONS,
    "no criteria": "Task: T-1\nClass: standard\nPaths: src/**\nObjective: do it\n" + SECTIONS.split("\n", 1)[1],
    "unknown class": "Task: T-1\nClass: bogus\nPaths: src/**\nObjective: do it\n" + SECTIONS,
}
# Each goal with a path set (what bin/project's finder names on FILES, or none, as team goal's finder may) and the class
# main 5f85f8e gave it in bin/project and in teamlib, computed once from main's code and written here. The one
# classifier may name another class, never one that requires fewer reviews than the stricter of the two.
LONG = "design the api " + "lorem ipsum dolor sit amet " * 80 + "and rotate the token"
# A secret-shaped string (built here, never a real key) that holds a former stem: masking hides "token", the floor
# must not drop.
SECRET_SHAPED = "gh" + "p_" + "token" + "a" * 31
TABLE = (
    ("fix a typo in the docs readme", ["docs/**"], "mechanical", "mechanical"),
    ("Add a status endpoint to the api", ["src/api/**"], "standard", "standard"),
    ("refactor the billing invoice", ["web/billing/**"], "complex", "complex"),
    ("investigate why invoices are slow", [], "analysis", "analysis"),
    ("decide which billing provider we use", ["web/billing/**"], "decision", "decision"),
    ("harden the bin scripts", [".agents/bin/**"], "standard", "security"),
    ("change the login flow", ["src/auth/**"], "security", "security"),
    ("rotate the api token", ["src/api/**"], "security", "standard"),
    ("Passwort-Regeln verschärfen", [], "security", "plan"),
    ("speed up the preview", ["web/sandbox/**"], "security", "standard"),
    ("redesign the api", ["src/api/**"], "complex", "plan"),
    ("Passwort-Regeln der api", ["src/api/**"], "security", "standard"),
    ("design the api", ["src/api/**"], "standard", "plan"),
    ("design the api", [], "standard", "plan"),
    ("compare api and billing", ["src/api/**", "web/billing/**"], "standard", "analysis"),
    ("compare api and billing", [], "standard", "analysis"),
    ("research the api", ["src/api/**"], "standard", "analysis"),
    ("research the api", [], "standard", "analysis"),
    ("update the api, the billing and the docs readme",
     ["docs/**", "src/api/**", "web/billing/**"], "standard", "plan"),
    ("migrate the billing module to the new architecture", ["web/billing/**"], "complex", "plan"),
    ("write a poem", [], "standard", "plan"),
    ("plan the billing migration in packages", ["web/billing/**"], "plan", "standard"),
    (LONG, ["src/api/**"], "security", "plan"),
    (LONG, [], "security", "plan"),
    ("add auth to the status endpoint of the api", ["src/api/**", "src/auth/**"], "security", "security"),
    ("fix the logout of the api", ["src/api/**"], "security", "standard"),
    ("move the secret out of the api config", ["src/api/**"], "security", "mechanical"),
    ("store the credentials of the billing provider", ["web/billing/**"], "security", "standard"),
    ("add OAuth to the api", ["src/api/**"], "security", "standard"),
    ("expire the session after an hour in the api", ["src/api/**"], "security", "standard"),
    ("set the cookie flags in the api", ["src/api/**"], "security", "standard"),
    ("add CSRF protection to the billing form", ["web/billing/**"], "security", "standard"),
    ("allow CORS for the docs readme", ["docs/**"], "security", "standard"),
    ("encrypt the billing invoice", ["web/billing/**"], "security", "standard"),
    ("renew the certificates of the api", ["src/api/**"], "security", "standard"),
    ("require TLS for the api", ["src/api/**"], "security", "standard"),
    ("drop SSLv3 in the api", ["src/api/**"], "security", "standard"),
    ("allow ssh deploys of the docs", ["docs/**"], "security", "standard"),
    ("open the firewall for the billing webhooks", ["web/billing/**"], "security", "standard"),
    ("extend the guard to quoted paths", [], "security", "plan"),
    ("check permissions before the billing export", ["web/billing/**"], "security", "standard"),
    ("design the sandbox for the preview", ["web/sandbox/**"], "security", "plan"),
    ("design the network layout of the api", ["src/api/**"], "security", "plan"),
    ("Netzwerk-Zugriffe der Vorschau begrenzen", [], "security", "plan"),
    ("Anmeldung über die api erneuern", ["src/api/**"], "security", "standard"),
    ("Schlüssel der Rechnungen tauschen", [], "security", "plan"),
    ("Berechtigungen der api prüfen", ["src/api/**"], "security", "standard"),
    ("Zugang zur billing api sperren", ["src/api/**", "web/billing/**"], "security", "standard"),
    ("which key signs the billing invoice", ["web/billing/**"], "security", "standard"),
    ("compare the api keys", ["src/api/**"], "security", "analysis"),
    ("rotate the token", ["src/api/**"], "security", "standard"),
    ("design the api token", ["src/api/**"], "security", "plan"),
    ("inspect " + SECRET_SHAPED, ["src/api/**"], "security", "standard"),
)
# [classify] with security_globs only, the default globs (main's classes above hold for it): the words stay.
GLOBS_ONLY = "\n[classify]\nsecurity_globs = .agents/bin/**, **/auth/**, **/secret*, .github/workflows/**\n"
# [classify] with security_words only: main's bin/project applied its stems whatever the config said, main's teamlib
# read no security_words, so main's classes above hold for it too.
WORDS_ONLY = "\n[classify]\nsecurity_words = *billing*\n"
# A config whose plan class requires more reviews than security (review*3, T-0150-fix1.r3): main's teamlib gave plan.
STRICT_PLAN = "\n[class.plan]\nsteps = worker gate review*3\n"


def setUpModule() -> None:
    isolate_wouf_env()


@unittest.skipUnless(shutil.which("git"), "no git here")
class ShimTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.team = self.tmp / "team"
        writable_copy(ROOT / ".agents" / "team", self.team)
        self.repo = self.tmp / "repo"
        for rel in FILES:
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text("x\n")
        for args in (["init", "-q"], ["add", "."]):
            subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)
        self.env = {**os.environ, "WOUF_TEAM_DIR": str(self.team)}

    def run_project(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(PROJECT), *args], env=self.env, capture_output=True, text=True,
                              timeout=60)

    def rules(self) -> dict:
        ini = configparser.ConfigParser(interpolation=None)
        ini.read(self.team / "config.ini", encoding="utf-8")
        return classify.rules_of(ini)

    def test_brief_writes_teamlib_class_for_the_goal_and_its_paths(self) -> None:
        files = sorted(FILES)
        for text, expected in GOALS:
            with self.subTest(goal=text):
                done = self.run_project("brief", text, "--repo", str(self.repo), "--task", "T-3")
                self.assertEqual(done.returncode, 0, done.stderr)
                head = dict(re.findall(r"^(Class|Paths): (.*)$", done.stdout, re.M))
                paths = [] if head.get("Paths", "TO FILL") == "TO FILL" else head["Paths"].split()
                self.assertEqual(head["Class"], classify.class_of(text, paths, files, self.rules())["class"])
                self.assertEqual(head["Class"], expected)
                self.assertEqual("Threat model:" in done.stdout, expected == "security")

    def test_a_bin_path_is_security(self) -> None:
        done = self.run_project("brief", "harden the bin scripts", "--repo", str(self.repo))
        self.assertIn("Paths: .agents/bin/**\n", done.stdout)
        self.assertIn("Class: security\n", done.stdout)

    def test_check_reports_teamlib_brief_problems(self) -> None:
        brief = self.tmp / "b.md"
        for name, text in BROKEN.items():
            with self.subTest(brief=name):
                brief.write_text(text)
                done = self.run_project("check", str(brief))
                with mock.patch.dict(os.environ, {"WOUF_TEAM_DIR": str(self.team)}):
                    expected = goal.brief_problems(text)
                self.assertTrue(expected)
                self.assertEqual(done.returncode, 1)
                self.assertEqual(done.stdout.splitlines(), [f"open: {p}" for p in expected])

    def test_project_keeps_no_classifier_or_brief_parser(self) -> None:
        source = PROJECT.read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"def (classify|configured_words|brief_problems|fields|sections)\b", source))
        self.assertIn('importlib.import_module(f"teamlib.{name}")', source)
        self.assertLess(len(source.splitlines()), 200)


class ReviewFloorTest(unittest.TestCase):
    """Per row: the reviews of the new class (project brief's class_for; team goal's classify where its finder names
    the row's paths) are at least those of main's stricter class, as team receipt need counts them."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.team = self.tmp / "team"
        writable_copy(ROOT / ".agents" / "team", self.team)
        patcher = mock.patch.dict(os.environ, {"WOUF_TEAM_DIR": str(self.team)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def check_table(self, extra: str) -> None:
        with (self.team / "config.ini").open("a", encoding="utf-8") as handle:
            handle.write(extra)
        ini = configparser.ConfigParser(interpolation=None)
        ini.read(self.team / "config.ini", encoding="utf-8")
        reviews = {k: class_options(ini, k)["reviews"] for k in goal.known_classes(ini)}
        self.assertGreaterEqual(len(TABLE), 40)
        self.assertGreater(LONG.index("token"), classify.CAP)
        through_goal = 0
        for text, paths, old_project, old_teamlib in TABLE:
            need = max(reviews[old_project], reviews[old_teamlib])
            with self.subTest(goal=text[:60], paths=paths):
                new = goal.class_for(text, paths, list(FILES))
                self.assertGreaterEqual(reviews[new], need, f"{new} after {old_project}, {old_teamlib}")
                found = classify.classify(text, FILES, rules=goal.rules(ini))
                if found["paths"] == paths:
                    through_goal += 1
                    self.assertGreaterEqual(reviews[found["class"]], need, found["reasons"])
        self.assertGreaterEqual(through_goal, 10)

    def test_no_row_requires_fewer_reviews_than_main(self) -> None:
        self.check_table("")

    def test_no_row_requires_fewer_reviews_with_security_globs_only(self) -> None:
        self.check_table(GLOBS_ONLY)

    def test_no_row_requires_fewer_reviews_with_security_words_set(self) -> None:
        self.check_table(WORDS_ONLY)

    def test_no_row_requires_fewer_reviews_with_a_strict_plan_class(self) -> None:
        self.check_table(STRICT_PLAN)

    def test_a_masked_secret_keeps_the_floor_and_stays_hidden(self) -> None:
        text = "inspect " + SECRET_SHAPED
        self.assertEqual(goal.class_for(text, ["src/api/**"], list(FILES)), "security")
        found = classify.classify(text, FILES)
        self.assertEqual(found["class"], "security")
        self.assertNotIn(SECRET_SHAPED, repr(found))


if __name__ == "__main__":
    unittest.main()
