"""Team-written skills are staged only when their toolkit approval allows it."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import unittest

from test_team_cli import Project


STAGE = """
import sys
from pathlib import Path
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
loader = SourceFileLoader("team_cli", sys.argv[1])
team = module_from_spec(spec_from_loader("team_cli", loader))
loader.exec_module(team)
team.pack_stage("worker", [], Path(sys.argv[2]))
"""


class TeamSkillRouting(unittest.TestCase):
    def setUp(self) -> None:
        self.project = Project()
        self.addCleanup(self.project.close)
        self.skills = self.project.root / ".agents/team/skills"
        self.skills.mkdir(parents=True, exist_ok=True)
        self.rows: list[str] = []

    def add_skill(self, name: str, file_status: str, row_status: str | None = None, used_by: str = "") -> None:
        (self.skills / f"{name}.md").write_text(
            f"---\nname: {name}\nstatus: {file_status}\n---\n\nSkill {name}.\n")
        status = row_status if row_status is not None else file_status
        self.rows.append(f"| {name} | skill | {status} | {used_by} | Test skill | | | | |")

    def add_folder_skill(self, name: str, file_status: str, row_status: str | None = None,
                         used_by: str = "") -> None:
        skill = self.skills / name
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\nstatus: {file_status}\n---\n\nSkill {name}.\n")
        (skill / "reference.md").write_text(f"Reference for {name}.\n")
        status = row_status if row_status is not None else file_status
        self.rows.append(f"| {name} | skill | {status} | {used_by} | Test skill | | | | |")

    def write_tools(self) -> None:
        path = self.project.root / ".agents/team/tools.md"
        path.write_text("| Name | Kind | Status | Used by | Purpose | Configured in | Secrets | Since | Last used |\n"
                        "|---|---|---|---|---|---|---|---|---|\n" + "\n".join(self.rows) + "\n")

    def staged_skills(self, task: str) -> Path:
        stage = self.project.root / ".agents/team/env/worker/tasks" / task
        stage.mkdir(parents=True, exist_ok=True)
        done = subprocess.run(
            [sys.executable, "-c", STAGE, str(self.project.root / ".agents/bin/team"), str(stage)],
            env={**self.project.env, "WOUF_TEAM_DIR": str(self.project.root / ".agents/team")},
            capture_output=True, text=True, cwd=self.project.root)
        self.assertEqual(done.returncode, 0, done.stderr)
        return stage / "pack/team-skills"

    def test_active_skills_are_global_and_trial_skills_are_task_scoped(self) -> None:
        self.add_skill("active-guide", "active")
        self.add_skill("trial-guide", "trial", used_by="T-1, T-3")
        self.add_skill("proposed-guide", "proposed")
        self.add_skill("retired-guide", "retired")
        self.write_tools()

        first = self.staged_skills("T-1")
        self.assertTrue((first / "active-guide.md").is_file())
        self.assertTrue((first / "trial-guide.md").is_file())
        self.assertFalse((first / "proposed-guide.md").exists())
        self.assertFalse((first / "retired-guide.md").exists())

        other = self.staged_skills("T-2")
        self.assertTrue((other / "active-guide.md").is_file())
        self.assertFalse((other / "trial-guide.md").exists())
        self.assertFalse((other / "proposed-guide.md").exists())
        self.assertFalse((other / "retired-guide.md").exists())

    def test_skill_requires_a_matching_tools_row_status(self) -> None:
        self.add_skill("mismatched-guide", "active", row_status="proposed")
        self.add_skill("unlisted-guide", "active")
        self.rows.pop()  # A skill without its required toolkit row is not approved for staging.
        self.write_tools()

        staged = self.staged_skills("T-1")
        self.assertFalse((staged / "mismatched-guide.md").exists())
        self.assertFalse((staged / "unlisted-guide.md").exists())

    def test_catalog_folder_skills_follow_the_same_status_routing(self) -> None:
        self.add_folder_skill("active-catalog", "active")
        self.add_folder_skill("trial-catalog", "trial", used_by="T-1, T-3")
        self.add_folder_skill("proposed-catalog", "proposed")
        self.add_folder_skill("retired-catalog", "retired")
        self.write_tools()

        first = self.staged_skills("T-1")
        self.assertTrue((first / "active-catalog/SKILL.md").is_file())
        self.assertTrue((first / "active-catalog/reference.md").is_file())
        self.assertTrue((first / "trial-catalog/SKILL.md").is_file())
        self.assertTrue((first / "trial-catalog/reference.md").is_file())
        self.assertFalse((first / "proposed-catalog").exists())
        self.assertFalse((first / "retired-catalog").exists())

        other = self.staged_skills("T-2")
        self.assertTrue((other / "active-catalog/SKILL.md").is_file())
        self.assertFalse((other / "trial-catalog").exists())
        self.assertFalse((other / "proposed-catalog").exists())
        self.assertFalse((other / "retired-catalog").exists())

    def test_catalog_skill_without_file_status_uses_its_tools_row(self) -> None:
        catalog = Path(__file__).resolve().parents[1] / "catalog/skills"
        for name, status in (("dns-cutover", "active"), ("openstack-cloud", "proposed")):
            skill = self.skills / name
            skill.mkdir()
            source = catalog / name / "SKILL.md"
            (skill / "SKILL.md").write_text(source.read_text())
            self.rows.append(f"| {name} | skill | {status} | | Test catalog skill | | | | |")
        self.write_tools()

        staged = self.staged_skills("T-1")
        self.assertEqual((staged / "dns-cutover/SKILL.md").read_text(),
                         (catalog / "dns-cutover/SKILL.md").read_text())
        self.assertFalse((staged / "openstack-cloud").exists())


if __name__ == "__main__":
    unittest.main()
