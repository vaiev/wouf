"""Static checks of the pack: structure, skill sections, links, open-source files and a leak scan."""

from __future__ import annotations

import ipaddress
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / ".agents"

SKILL_SECTIONS = (
    "## When to use",
    "## Required inputs and access",
    "## Sequence of work",
    "## How to validate the result",
    "## What to return",
    "## What requires approval",
)

# Split so this file does not itself contain a contiguous secret marker.
SECRET_MARKERS = (
    "BEGIN " "OPENSSH PRIVATE KEY",
    "BEGIN " "PRIVATE KEY",
    "AGE-SECRET" "-KEY-",
    "x-access-" "token:",
    "gh" "p_",
    "github_" "pat_",
    "sk-" "ant-",
)
# Documentation and loopback ranges only (RFC 5737, loopback).
ALLOWED_NETS = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "127.0.0.0/8")]
IPV4 = re.compile(r"(?<![\d.])(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})(?![\d.])")
# Model names age quickly. The pack routes by capability tier; versions belong on the board.
PINNED_MODEL = re.compile(r"\b(Opus|Sonnet|Haiku|Gemini|GPT|Codex)[ -]?\d")
MD_LINK = re.compile(r"\]\(([^)\s]+)\)")

# rules.md sections 3 and 8 hold the hard limits; as on main at T-0019-29 (d0f8624), byte for byte.
RULES_HARD_LIMITS = {
    "3": "## 3. Reversible is free, irreversible asks\n\n"
         "- Finish without asking: search, read, draft, stage, run tests, work in a sandbox, anything that can be undone "
         "in minutes.\n"
         "- Always through the human (in every autonomy setting): send, publish, deploy, purchase, delete or overwrite "
         "outside a sandbox, merge to main, new access (a new key, MCP server, host or permission), anything that costs "
         "money.\n"
         "- With `autonomy = auto` the lead decides everything else with the decide bot, records it in "
         "`team/decisions.md`, and tells the human in its next report. With `ask`, those decisions go to the human "
         "too.\n\n",
    "8": "## 8. Quality\n\nStart from real failures, not imagined ones. Only checks that can block a result count. Code "
         "reaches main only with an APPROVE from the review bot on the exact commit, green CI and the human's yes "
         "(`skills/review-merge.md`).\n",
}
STATE_SENTENCE = ("State lives in the event log; the store is its cache, board and decisions are its readable export.")
# Bytes of START.md, rules.md, roles/*.md and skills/*.md before T-0019-29 (main d0f8624): the pack only shrinks.
CORE_BYTES_BEFORE = 41837


def text_files():
    for path in ROOT.rglob("*"):
        if path.is_file() and ".git" not in path.parts and "__pycache__" not in path.parts:
            yield path


def markdown_files():
    return [p for p in text_files() if p.suffix == ".md"]


def skills():
    return sorted(p for p in (PACK / "skills").glob("*.md") if not p.name.startswith("_"))


class PackStructureTest(unittest.TestCase):
    def test_every_skill_has_the_required_sections(self) -> None:
        self.assertGreaterEqual(len(skills()), 8)
        for skill in skills():
            text = skill.read_text(encoding="utf-8")
            positions = []
            for section in SKILL_SECTIONS:
                self.assertIn(section, text, f"{skill.name}: missing {section}")
                positions.append(text.index(section))
            self.assertEqual(positions, sorted(positions), f"{skill.name}: sections out of order")

    def test_the_lead_role_names_every_skill(self) -> None:
        lead = (PACK / "roles" / "lead.md").read_text(encoding="utf-8")
        for skill in skills():
            self.assertIn(f"skills/{skill.name}", lead, f"roles/lead.md does not name skills/{skill.name}")
        self.assertIn("`team skill NAME`", lead, "roles/lead.md names the command that prints a skill's steps")

    def test_files_named_in_the_pack_exist(self) -> None:
        for md in list(PACK.rglob("*.md")) + [ROOT / "README.md", ROOT / "INSTALL.md"]:
            for folder, name in re.findall(r"`(?:\.agents/)?(skills|roles)/([\w-]+\.md)`", md.read_text(encoding="utf-8")):
                self.assertTrue((PACK / folder / name).is_file(), f"{md.relative_to(ROOT)} names {folder}/{name}")

    def test_the_pack_is_small(self) -> None:
        core = [PACK / "START.md", PACK / "rules.md", *PACK.glob("roles/*.md"), *PACK.glob("skills/*.md")]
        self.assertLessEqual(len(core), 16, "keep what every agent may load small: merge before you add")

    def test_relative_links_resolve(self) -> None:
        for md in markdown_files():
            for target in MD_LINK.findall(md.read_text(encoding="utf-8")):
                if re.match(r"^[a-z]+:", target) or target.startswith("#"):
                    continue
                path = (md.parent / target.split("#", 1)[0]).resolve()
                self.assertTrue(path.exists(), f"{md.relative_to(ROOT)}: broken link {target}")

    def test_model_routing_uses_tiers_not_versions(self) -> None:
        # Records of a real team (docs/examples/, the README's "Built with WOUF") name the models it ran on;
        # they are history, not routing.
        for md in markdown_files():
            if md.is_relative_to(ROOT / "docs" / "examples"):
                continue
            record = False
            for n, line in enumerate(md.read_text(encoding="utf-8").splitlines(), 1):
                if line.startswith("## "):
                    record = md == ROOT / "README.md" and line == "## Built with WOUF"
                if not record:
                    self.assertIsNone(PINNED_MODEL.search(line), f"{md.relative_to(ROOT)}:{n}: pinned model version")


def core_files() -> list:
    return [PACK / "START.md", PACK / "rules.md", *PACK.glob("roles/*.md"), *PACK.glob("skills/*.md")]


def section(text: str, heading: str) -> str:
    """From a '## ' heading line to the next one (or the end)."""
    start = text.index(heading)
    end = text.find("\n## ", start + 1)
    return text[start:] if end < 0 else text[start:end + 1]


class LeanPackTest(unittest.TestCase):
    """T-0019-29: the prose points at the commands that exist; the boot is one command."""

    def test_rules_section_7_is_the_owners_sentence(self) -> None:
        rules = (PACK / "rules.md").read_text(encoding="utf-8")
        seven = section(rules, "## 7. ")
        lines = [ln for ln in seven.splitlines() if ln.strip()]
        self.assertEqual(lines[0], f"## 7. {STATE_SENTENCE.rstrip('.')}")  # MD026: no full stop in a heading
        self.assertEqual(lines[1], STATE_SENTENCE)
        for command in ("team context", "team task", "team ask"):
            self.assertIn(f"`{command}", seven)

    def test_the_hard_limits_are_unchanged(self) -> None:
        rules = (PACK / "rules.md").read_text(encoding="utf-8")
        for number, text in RULES_HARD_LIMITS.items():
            self.assertEqual(section(rules, f"## {number}. "), text, f"rules.md section {number}")

    def test_the_boot_is_one_command_and_the_greeting(self) -> None:
        start = (PACK / "START.md").read_text(encoding="utf-8")
        boot = start[start.index("## 2. Boot sequence"):start.index("## 3.")]
        self.assertEqual(re.findall(r"`(\.agents/bin/[^`]+)`", boot), [".agents/bin/team context"])
        self.assertEqual(len(re.findall(r"^\d+\. ", boot, re.M)), 2, "one command, then the greeting")
        self.assertLess(len(start.split()), 700)

    def test_brief_and_dispatch_describe_the_four_phases(self) -> None:
        for name in ("brief.md", "dispatch.md"):
            text = (PACK / "skills" / name).read_text(encoding="utf-8")
            steps = section(text, "## Sequence of work")
            for phase in ("INIT", "CONCEPT", "REALIZE", "VERIFY"):
                self.assertIn(phase, steps, f"{name}: {phase}")
        both = "".join((PACK / "skills" / n).read_text(encoding="utf-8") for n in ("brief.md", "dispatch.md"))
        for phrase in ("`team goal", "`team flow", "Class: plan", "`team batch", "APPROVE <sha>", "team merge propose"):
            self.assertIn(phrase, both)

    def test_skills_name_the_bookkeeping_commands(self) -> None:
        text = "".join(p.read_text(encoding="utf-8") for p in skills())
        for command in ("team task", "team question", "team decision", "team ask", "team lesson", "team stats",
                        "bin/federate pull", "bin/federate receive", "team brief new"):
            self.assertIn(command, text, command)

    def test_the_pack_shrank(self) -> None:
        self.assertLessEqual(len(core_files()), 16)
        size = sum(len(p.read_bytes()) for p in core_files())
        self.assertLess(size, CORE_BYTES_BEFORE, f"{size} bytes")

    def test_the_boot_reads_stay_small(self) -> None:
        # START.md, rules.md and roles/lead.md are read at every boot next to `team context` (the M1 proxy of
        # tests/test_team_cli.py BootBudgetTest): 10 286 bytes before T-0019-29; at most 4 320 keeps the busy boot
        # 70 % below the 6 009 tokens of T-0019-01.
        size = sum(len((PACK / name).read_bytes()) for name in ("START.md", "rules.md", "roles/lead.md"))
        self.assertLessEqual(size, 4320, f"{size} bytes")

    def test_the_roadmap_now_is_grouped_by_the_plan_areas(self) -> None:
        # The T-0017 plan's five areas, in order; each lists what it delivered.
        now = section((ROOT / "docs" / "ROADMAP.md").read_text(encoding="utf-8"), "## Now")
        areas = re.findall(r"^### (.+)$", now, re.M)
        self.assertEqual([a.split(":")[0] for a in areas if a[:2] in ("A:", "B:", "C:", "D:", "E:")], list("ABCDE"))
        bodies = {part[0]: part for part in re.split(r"^### ", now, flags=re.M)[1:]}
        for area, words in (("A", ("boot", "receipt", "SSH", "gate cache")),
                            ("B", ("event log", "`team task", "`team ask", "lessons")),
                            ("C", ("`team stats", "tiers", "`team batch", "`team goal", "Session hooks", "staged")),
                            ("D", ("companion",)),
                            ("E", ("envelopes", "`bin/federate", "pull", "receive"))):
            for word in words:
                self.assertIn(word, bodies[area], f"ROADMAP Now {area}: {word}")
        self.assertIn("`team skill NAME`", now)
        self.assertNotIn("team help", now)

    def test_the_pack_names_no_catalog_folder(self) -> None:
        for path in PACK.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                self.assertNotIn(b"catalog/", path.read_bytes(), str(path.relative_to(ROOT)))


class OpenSourceFilesTest(unittest.TestCase):
    def test_private_notes_stay_out_of_git(self) -> None:
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("/private/", ignore)
        self.assertIn("/CLAUDE.local.md", ignore)

    def test_helpers_are_executable(self) -> None:
        import os
        for name in ("team", "log", "secret", "guard", "federate", "project"):
            path = PACK / "bin" / name
            self.assertTrue(os.access(path, os.X_OK), f".agents/bin/{name} must be executable")
            self.assertTrue(path.read_text(encoding="utf-8").startswith("#!/usr/bin/env python3"), name)

    def test_license_is_apache_2(self) -> None:
        text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("Apache License", text)
        self.assertIn("Version 2.0, January 2004", text)
        self.assertIn("Copyright 2026 VAIEV", text)

    def test_project_files_exist(self) -> None:
        for name in ("README.md", "INSTALL.md", "CONTRIBUTING.md", "SECURITY.md", "CODE_OF_CONDUCT.md", "AGENTS.md",
                     "CLAUDE.md"):
            self.assertTrue((ROOT / name).is_file(), name)
        self.assertIn("@AGENTS.md", (ROOT / "CLAUDE.md").read_text(encoding="utf-8"))
        self.assertIn("private vulnerability reporting", (ROOT / "SECURITY.md").read_text(encoding="utf-8").lower())
        self.assertTrue((ROOT / ".github" / "pull_request_template.md").is_file())

    def test_readme_has_quickstart_and_license(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("## Set it up", readme)
        self.assertIn("INSTALL.md", readme)
        self.assertIn("```mermaid", readme)
        self.assertIn("Apache License, Version 2.0", readme)

    def test_ci_runs_the_tests_with_pinned_actions(self) -> None:
        ci = (ROOT / ".github" / "workflows" / "docs-ci.yml").read_text(encoding="utf-8")
        self.assertIn("python3 scripts/test-parallel.py", ci)
        self.assertIn("contents: read", ci)
        self.assertNotRegex(ci, r"uses: [^@\n]+@v\d")


class LeakScanTest(unittest.TestCase):
    def test_no_secrets_or_private_addresses(self) -> None:
        for path in text_files():
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for marker in SECRET_MARKERS:
                self.assertNotIn(marker, text, f"{path.relative_to(ROOT)}: secret marker")
            for match in IPV4.findall(text):
                try:
                    ip = ipaddress.ip_address(match)
                except ValueError:
                    continue
                self.assertTrue(any(ip in net for net in ALLOWED_NETS), f"{path.relative_to(ROOT)}: address {match}")


if __name__ == "__main__":
    unittest.main()
