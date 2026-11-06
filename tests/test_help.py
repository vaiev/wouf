"""teamlib.help (T-0019-29): `team skill NAME`, one skill's "Sequence of work" instead of the whole file, and
`team brief new`, a brief skeleton with the next free task id from the state cache."""

from __future__ import annotations

import contextlib
import io
import re
import sys
import unittest

from test_team_cli import SOURCE, Project

sys.path.insert(0, str(SOURCE / "bin"))
from teamlib import help as helpmod  # noqa: E402

SKILLS = sorted(p.stem for p in (SOURCE / "skills").glob("*.md") if not p.name.startswith("_"))


def shown(*args: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = helpmod.do_skill(list(args))
        except SystemExit as stop:
            code = stop.code
    return code, out.getvalue(), err.getvalue()


class SkillStepsTest(unittest.TestCase):
    def test_each_skill_shows_only_its_sequence_of_work(self) -> None:
        for name in SKILLS:
            with self.subTest(skill=name):
                code, out, _ = shown(name)
                self.assertEqual(code, 0)
                text = (SOURCE / "skills" / f"{name}.md").read_text(encoding="utf-8")
                start = text.index("## Sequence of work")
                end = text.index("\n## ", start + 1)
                self.assertEqual(out, text[start:end].rstrip("\n") + "\n")
                self.assertEqual(len(re.findall(r"^## ", out, re.M)), 1)

    def test_a_name_that_is_no_skill_is_refused_with_the_list(self) -> None:
        for name in ("nope", "../rules", "_template", "brief.md", "BRIEF", ""):
            with self.subTest(name=name):
                code, out, err = shown(name)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("brief, ", err)

    def test_one_name_only(self) -> None:
        self.assertEqual(shown("brief", "dispatch")[0], 2)
        self.assertEqual(shown()[0], 2)

    def test_it_is_the_registered_command_skill_and_help_stays_the_built_in(self) -> None:
        self.assertEqual(helpmod.COMMANDS["skill"], helpmod.do_skill)
        self.assertNotIn("help", helpmod.COMMANDS)
        self.assertIn("team skill NAME", helpmod.USAGE)


class SkillCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.p = Project()
        self.addCleanup(self.p.close)

    def test_team_skill_prints_the_sequence_of_work_through_the_cli(self) -> None:
        done = self.p.team("skill", "brief")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, shown("brief")[1])
        self.assertTrue(done.stdout.startswith("## Sequence of work\n"))

    def test_an_unknown_skill_fails_with_the_list_through_the_cli(self) -> None:
        done = self.p.team("skill", "nope")
        self.assertEqual((done.returncode, done.stdout), (2, ""))
        self.assertIn(", ".join(SKILLS), done.stderr)

    def test_team_help_lists_team_skill(self) -> None:
        self.assertIn("team skill NAME", self.p.team("help").stdout)


class BriefNewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.p = Project()
        self.addCleanup(self.p.close)

    def test_the_skeleton_takes_the_next_free_id_from_the_store(self) -> None:
        done = self.p.team("brief", "new", "--class", "standard", "Fix the crash in file.txt")
        self.assertEqual(done.returncode, 0, done.stderr)
        lines = done.stdout.splitlines()
        self.assertEqual(lines[:4], ["Task: T-1", "Class: standard", lines[2], "Objective: Fix the crash in file.txt"])
        self.assertTrue(lines[2].startswith("Paths: TO FILL"))
        for name in ("Acceptance criteria", "Scope", "Out of scope", "Inputs", "Constraints", "Expected output"):
            self.assertEqual(len([ln for ln in lines if ln.startswith(f"{name}: TO FILL")]), 1, name)
        self.assertNotIn("Threat model", done.stdout)
        self.assertEqual(self.p.team("task", "new", "x", "--class", "standard").returncode, 0)
        self.assertEqual(self.p.team("task", "new", "y", "--class", "standard").returncode, 0)
        again = self.p.team("brief", "new", "--class", "mechanical", "z")
        self.assertEqual(again.stdout.splitlines()[0], "Task: T-3")
        self.assertFalse((self.p.root / ".agents" / "team" / "env" / "lead" / "T-3.md").exists(), "printed only")

    def test_a_plan_has_no_paths_and_security_a_threat_model(self) -> None:
        plan = self.p.team("brief", "new", "--class", "plan", "Design billing").stdout
        self.assertNotIn("Paths:", plan)
        security = self.p.team("brief", "new", "--class", "security", "Fix tokens").stdout
        self.assertEqual(len([ln for ln in security.splitlines() if ln.startswith("Threat model: TO FILL")]), 1)

    def test_a_draft_brief_of_the_lead_takes_its_id(self) -> None:
        lead = self.p.root / ".agents" / "team" / "env" / "lead"
        lead.mkdir(parents=True)
        (lead / "T-7.md").write_text("Task: T-7\n")
        self.assertEqual(self.p.team("brief", "new", "--class", "standard", "x").stdout.splitlines()[0], "Task: T-8")

    def test_bad_input_is_refused(self) -> None:
        for args in (("new", "x"), ("new", "--class", "huge", "x"), ("new", "--class", "standard"), ("old",),
                     ("new", "--class", "standard", "a", "b"), ("new", "--class", "standard", "two\nlines")):
            with self.subTest(args=args):
                done = self.p.team("brief", *args)
                self.assertEqual((done.returncode, done.stdout), (2, ""), done.stderr)

    def test_a_key_in_the_objective_is_masked(self) -> None:
        key = "sk-" "ant-" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8s9T0"
        done = self.p.team("brief", "new", "--class", "standard", f"rotate {key} now")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn(key, done.stdout)
        self.assertIn("Objective: rotate ***", done.stdout)


if __name__ == "__main__":
    unittest.main()
