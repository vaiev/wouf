"""Checks of the optional catalog: skill frontmatter, no private or retired references, and the tool tests."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "catalog"
SKILLS = sorted(p.parent for p in (CATALOG / "skills").glob("*/SKILL.md"))
# Each tool's own test folder, run in a child process so its module names and environment stay apart.
TOOL_TESTS = ("catalog/tools/hub", "catalog/tools/control-mcp/tests", "catalog/tools/companion",
              "catalog/tools/egress-proxy")

# Documentation and loopback ranges only (RFC 5737, loopback).
ALLOWED_NETS = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "127.0.0.0/8")]
IPV4 = re.compile(r"(?<![\d.])(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})(?![\d.])")
# Retired repositories, the SideComputer and lead-kit setups, private host names.
FORBIDDEN = re.compile(
    r"vaiev/(?:skills|mcp|guardrails)|lead-kit|side ?computer|"
    r"(?<![\w.-])(?!(?:host\.docker\.internal|host\.containers\.internal)(?![\w-]|\.[\w-]))"
    r"[\w-]+(?:\.[\w-]+)*\.(?:internal|lan|corp|home\.arpa)\b",
    re.IGNORECASE,
)


def catalog_files():
    for path in CATALOG.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts:
            yield path


def frontmatter(text: str) -> dict:
    match = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
    if not match:
        return {}
    fields, key = {}, None
    for line in match.group(1).splitlines():
        if re.match(r"^\w[\w-]*:", line):
            key, _, value = line.partition(":")
            fields[key] = value.strip().lstrip(">-").strip()
        elif key:
            fields[key] = (fields[key] + " " + line.strip()).strip()
    return fields


class CatalogSkillsTest(unittest.TestCase):
    def test_the_catalog_has_skills_and_tools(self) -> None:
        self.assertEqual(len(SKILLS), 6)
        for tool in ("hub", "control-mcp", "companion", "egress-proxy"):
            self.assertTrue((CATALOG / "tools" / tool / "README.md").is_file(), tool)
        self.assertTrue((CATALOG / "README.md").is_file())

    def test_every_skill_has_name_and_description(self) -> None:
        for skill in SKILLS:
            fields = frontmatter((skill / "SKILL.md").read_text(encoding="utf-8"))
            self.assertEqual(fields.get("name"), skill.name, f"{skill.name}: frontmatter name")
            self.assertTrue(fields.get("description"), f"{skill.name}: frontmatter description")

    def test_the_catalog_readme_lists_every_entry(self) -> None:
        readme = (CATALOG / "README.md").read_text(encoding="utf-8")
        for skill in SKILLS:
            self.assertIn(f"skills/{skill.name}/SKILL.md", readme)
        for tool in ("tools/hub/README.md", "tools/control-mcp/README.md", "tools/companion/README.md",
                     "tools/egress-proxy/README.md"):
            self.assertIn(tool, readme)

    def test_the_catalog_is_not_loaded_by_the_pack(self) -> None:
        for path in (ROOT / ".agents").rglob("*"):
            if path.is_file() and path.suffix in (".md", ".py", ".ini", ""):
                try:
                    text = path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                self.assertNotIn("catalog/", text, f"{path.relative_to(ROOT)} refers to the catalog")


class CatalogLeakTest(unittest.TestCase):
    def test_only_documented_container_hostnames_are_allowed(self) -> None:
        for host in ("host.docker.internal", "host.containers.internal"):
            with self.subTest(host=host):
                self.assertIsNone(FORBIDDEN.search(host))
        for host in ("foo.internal", "foo.lan", "foo.corp", "foo.home.arpa",
                     "other.host.docker.internal", "host.docker.internal.evil.internal"):
            with self.subTest(host=host):
                self.assertIsNotNone(FORBIDDEN.search(host))

    def test_no_private_hosts_addresses_or_retired_repos(self) -> None:
        for path in catalog_files():
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            name = path.relative_to(ROOT)
            found = FORBIDDEN.search(text)
            self.assertIsNone(found, f"{name}: {found and found.group(0)}")
            for match in IPV4.findall(text):
                try:
                    ip = ipaddress.ip_address(match)
                except ValueError:
                    continue
                self.assertTrue(any(ip in net for net in ALLOWED_NETS), f"{name}: address {match}")


class CatalogToolTests(unittest.TestCase):
    def test_tool_tests_pass(self) -> None:
        for folder in TOOL_TESTS:
            with self.subTest(tool=folder):
                result = subprocess.run(
                    [sys.executable, "-m", "unittest", "discover", "-s", folder],
                    cwd=ROOT, capture_output=True, text=True, timeout=300,
                )
                self.assertEqual(result.returncode, 0, result.stderr[-2000:])
                self.assertIn("Ran ", result.stderr)


LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")
HEADING = re.compile(r"^#{1,6}\s")
FENCE = re.compile(r"^\s*(?:```|~~~)")
UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")


def markdown_block_problems(text: str) -> list[str]:
    """Blank lines around headings, lists, fences and tables, and table cell counts (the markdownlint rules MD022, MD031, MD032, MD056, MD058 that CI runs)."""
    lines = text.split("\n")
    start = 0
    if lines and lines[0] == "---":
        start = lines.index("---", 1) + 1
    problems, fence, table_cells = [], None, None
    for i in range(start, len(lines)):
        line = lines[i]
        prev = lines[i - 1] if i > start else ""
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        where = f"line {i + 1}"
        if fence:
            if FENCE.match(line) and line.strip().startswith(fence):
                fence = None
                if nxt.strip():
                    problems.append(f"{where}: code fence not followed by a blank line")
            continue
        if FENCE.match(line):
            fence = line.strip()[:3]
            if prev.strip():
                problems.append(f"{where}: code fence not preceded by a blank line")
        elif HEADING.match(line):
            if prev.strip():
                problems.append(f"{where}: heading not preceded by a blank line")
            if nxt.strip():
                problems.append(f"{where}: heading not followed by a blank line")
        elif LIST_ITEM.match(line):
            if prev.strip() and not (LIST_ITEM.match(prev) or prev.startswith(" ")):
                problems.append(f"{where}: list not preceded by a blank line")
        elif line.startswith("|"):
            if prev.strip() and not prev.startswith("|"):
                problems.append(f"{where}: table not preceded by a blank line")
            if nxt.strip() and not nxt.startswith("|"):
                problems.append(f"{where}: table not followed by a blank line")
            pipes = len(UNESCAPED_PIPE.findall(line))
            if not prev.startswith("|"):
                table_cells = pipes
            elif pipes != table_cells:
                problems.append(f"{where}: {pipes} unescaped pipes, header has {table_cells}")
        elif line.strip() and not line.startswith(" ") and LIST_ITEM.match(prev):
            problems.append(f"{where}: list not followed by a blank line")
    return problems


class CatalogMarkdownTest(unittest.TestCase):
    def test_blocks_are_separated_and_tables_are_well_formed(self) -> None:
        for path in sorted(CATALOG.rglob("*.md")):
            with self.subTest(file=str(path.relative_to(ROOT))):
                self.assertEqual(markdown_block_problems(path.read_text(encoding="utf-8")), [])

    def test_the_checker_flags_what_markdownlint_flags(self) -> None:
        self.assertEqual(markdown_block_problems("text\n## A\n\nmore\n"), ["line 2: heading not preceded by a blank line"])
        self.assertTrue(markdown_block_problems("text\n- item\n"))
        self.assertTrue(markdown_block_problems("| a | b |\n|---|---|\n| x | y | z |\n").__len__())
        self.assertEqual(markdown_block_problems("| a | b |\n|---|---|\n| `x\\|y` | z |\n"), [])


# The launcher in the control-mcp README must work from the project root, where the agent CLI starts the server.
CONTROL_README = CATALOG / "tools" / "control-mcp" / "README.md"


class CatalogLauncherTest(unittest.TestCase):
    def test_the_documented_paths_resolve_from_the_project_root(self) -> None:
        text = CONTROL_README.read_text(encoding="utf-8")
        for command in (
            "cp -r catalog/tools/control-mcp control-mcp",
            "python3 -m venv control-mcp/.venv",
            "cp control-mcp/control.example.toml control-mcp/control.toml",
        ):
            self.assertIn(command, text)
        block = re.search(r"```json\n(.*?)\n```", text, re.DOTALL)
        self.assertIsNotNone(block, "the README shows the launcher as JSON")
        server = json.loads(block.group(1))["mcpServers"]["control"]
        with tempfile.TemporaryDirectory() as project:
            root = Path(project)
            shutil.copytree(CATALOG / "tools" / "control-mcp", root / "control-mcp",
                            ignore=shutil.ignore_patterns("__pycache__", "tests"))
            subprocess.run([sys.executable, "-m", "venv", "--without-pip", "control-mcp/.venv"],
                           cwd=root, check=True, capture_output=True)
            shutil.copy(root / "control-mcp" / "control.example.toml", root / "control-mcp" / "control.toml")
            self.assertTrue((root / server["command"]).is_file(), server["command"])
            self.assertTrue((root / server["args"][0]).is_file(), server["args"][0])
            config = root / server["env"]["CONTROL_CONFIG"]
            self.assertTrue(config.is_file(), config)
            if tomllib is None:
                self.skipTest("needs tomllib (Python 3.11+) or tomli")
            check ="import os, sys; sys.path.insert(0, 'control-mcp'); import control_core as cc; cc.load(os.environ['CONTROL_CONFIG'])"
            loaded = subprocess.run([sys.executable, "-I", "-c", check], cwd=root, capture_output=True, text=True,
                                    env={"CONTROL_CONFIG": server["env"]["CONTROL_CONFIG"], "PATH": os.environ.get("PATH", "")})
            self.assertEqual(loaded.returncode, 0, loaded.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
