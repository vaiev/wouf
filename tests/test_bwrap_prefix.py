"""mode = bwrap: the team prefix env/_prefix that stands in for the team image (PLAN T-0060, package 7).

The recipe, the install hosts and the bot sandbox's mount need no bubblewrap; the builds and runs are skipped when
bwrap_usable() is false (the bwrap CI job fails on any skip). The build's proxy is the catalog's egress_proxy.py."""

from __future__ import annotations

import configparser
import os
import socket
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_bwrap_egress import PROXY  # noqa: E402
from test_bwrap_run import USABLE, bwrap_project  # noqa: E402
from test_team_cli import KEY  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".agents" / "bin"))
from teamlib import bwrap  # noqa: E402

# A tool the build writes into the prefix; a bot finds it on its PATH.
TOOL = ("mkdir -p /opt/wouf/bin && printf '#!/bin/sh\\necho prefix-tool-ran\\n' > /opt/wouf/bin/wouf-hello "
        "&& chmod 755 /opt/wouf/bin/wouf-hello")
# A line that starts a process meant to outlive the build.
LINGER = "(sleep 31415 >/dev/null 2>&1 &) ; true"
# A line that writes outside the prefix: it fails for lack of root, so it is skipped.
OUTSIDE = "echo x > /usr/wouf-outside"
# Prints the stored key, built at run time so that the recipe does not hold it.
HALF = len(KEY) // 2
PRINTS_KEY = f"printf '%s%s\\n' '{KEY[:HALF]}' '{KEY[HALF:]}'"
# The bot: runs the prefix's tool, tries to write the prefix.
PREFIX_CLI = """#!/bin/sh
wouf-hello > out/hello.txt 2>&1
echo "$PATH" > out/path.txt
touch /opt/wouf/bot-wrote 2>/dev/null && echo written > out/write.txt || echo refused > out/write.txt
echo "WOUF Worker · T-1 · ready-for-review"
"""
# A line that tries this computer's loopback (the port is filled in) and writes what it got into the prefix.
LOOPBACK = ("python3 -c \"import socket; s = socket.socket(); s.settimeout(5); r = s.connect_ex(('127.0.0.1', {port})); "
            "open('/opt/wouf/loopback.txt', 'w').write('connected' if r == 0 else 'failed %d' % r)\"")


class PrefixRecipeTest(unittest.TestCase):
    """No bubblewrap needed: what a build runs and reaches, and what a bot sandbox mounts."""

    def test_install_hosts_are_url_hosts_and_installer_registries_only(self) -> None:
        hosts = bwrap.install_hosts(["npm install -g @anthropic-ai/claude-code",
                                     "curl -fsSL https://Example.ORG/x.sh | sh; curl http://192.0.2.1/y",
                                     "pip3 install --user requests", "echo npmish"])
        self.assertEqual(hosts, ["registry.npmjs.org", "example.org", "pypi.org", "files.pythonhosted.org"])

    def test_setup_is_skipped_and_the_lines_come_from_runner_and_provision(self) -> None:
        p = bwrap_project()
        self.addCleanup(p.close)
        p.set("[runner.fake]\ninstall =", "[runner.fake]\ninstall = npm install -g fake-cli")
        (p.root / ".agents" / "team" / "provision.sh").write_text("# 2026-01-01\n" + TOOL + "\n\n")
        ini = configparser.ConfigParser(interpolation=None)
        ini.read(p.config)
        saved = os.environ.get("WOUF_TEAM_DIR")
        os.environ["WOUF_TEAM_DIR"] = str(p.root / ".agents" / "team")
        try:
            lines, skipped = bwrap.prefix_recipe(ini)
        finally:
            os.environ.pop("WOUF_TEAM_DIR") if saved is None else os.environ.update(WOUF_TEAM_DIR=saved)
        self.assertEqual(lines, ["npm install -g fake-cli", TOOL])
        self.assertEqual(len(skipped), 1)
        self.assertIn("fake-cli", skipped[0], "[environment] setup is skipped, never run")

    def test_a_bot_sandbox_gets_the_prefix_read_only_and_first_on_path(self) -> None:
        self.assertTrue(bwrap.INSIDE_PATH.startswith("/opt/wouf/bin:"))
        argv = bwrap.box_argv({"prefix": "/"}, "none", (0,))
        self.assertIn(("--ro-bind", "/", "/opt/wouf"), list(zip(argv, argv[1:], argv[2:])))


@unittest.skipUnless(USABLE, "bwrap is not installed or cannot start a sandbox here (user namespaces)")
class BwrapPrefixTest(unittest.TestCase):
    """Real builds in bubblewrap, through bin/team, behind the egress proxy."""

    def setUp(self) -> None:
        self.p = bwrap_project()
        self.addCleanup(self.p.close)
        self.p.store_secret("ALPHA_KEY", KEY)
        proxy = self.p.root / "tools" / "egress_proxy.py"
        proxy.parent.mkdir()
        proxy.write_text(PROXY.read_text(encoding="utf-8"))
        with self.p.config.open("a") as handle:
            handle.write("\n[egress]\nproxy = tools/egress_proxy.py\nallow =\n")
        self.env = self.p.root / ".agents" / "team" / "env"
        self.prefix = self.env / "_prefix"
        self.provision = self.p.root / ".agents" / "team" / "provision.sh"

    def lines(self, *lines: str) -> None:
        self.provision.write_text("".join(f"{line}\n" for line in lines))

    def lingering(self) -> list[int]:
        found = []
        for entry in Path("/proc").iterdir():
            try:
                cmdline = (entry / "cmdline").read_bytes()
            except OSError:
                continue
            if entry.name.isdigit() and b"31415" in cmdline and b"sleep" in cmdline:
                found.append(int(entry.name))
        return found

    def test_a_tool_the_build_writes_runs_in_a_bot_run(self) -> None:
        self.lines(TOOL, LINGER, OUTSIDE)
        made = self.p.team("bot", "create", "worker")
        self.assertEqual(made.returncode, 0, made.stderr + made.stdout)
        self.assertIn("building the team prefix", made.stderr)
        self.assertTrue((self.prefix / "opt" / "bin" / "wouf-hello").is_file())
        self.assertFalse(os.path.lexists("/usr/wouf-outside"))
        self.assertIn(f"skipped {OUTSIDE!r}: {bwrap.NEEDS_ROOT}", made.stderr)
        self.assertRegex(made.stderr, rf"skipped '[^\n]*fake-cli[^\n]*': {bwrap.NEEDS_ROOT}")
        self.assertEqual(self.lingering(), [], "nothing the build started outlives it")
        self.assertFalse((self.prefix / "build" / "home").exists(), "the build's HOME is thrown away")
        (self.env / "worker" / "box" / "fake-cli.sh").write_text(PREFIX_CLI)
        done = self.p.team("run", "worker", "T-1", "--brief", str(self.p.brief))
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        out = self.env / "worker" / "tasks" / "T-1" / "out"
        self.assertEqual((out / "hello.txt").read_text(), "prefix-tool-ran\n")
        self.assertTrue((out / "path.txt").read_text().startswith("/opt/wouf/bin:"))
        self.assertEqual((out / "write.txt").read_text(), "refused\n", "the prefix is read-only for a bot")
        self.assertFalse((self.prefix / "opt" / "bot-wrote").exists())

    def test_an_unchanged_recipe_does_not_rebuild(self) -> None:
        self.lines(TOOL)
        first = self.p.team("image")
        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        built = (self.prefix / "opt").stat().st_ino, (self.prefix / "build.log").stat().st_mtime_ns
        again = self.p.team("bot", "create", "worker")
        self.assertEqual(again.returncode, 0, again.stderr + again.stdout)
        self.assertNotIn("building the team prefix", again.stderr)
        self.assertEqual(((self.prefix / "opt").stat().st_ino, (self.prefix / "build.log").stat().st_mtime_ns),
                         built)
        self.lines(TOOL, "touch /opt/wouf/second")
        changed = self.p.team("image")
        self.assertEqual(changed.returncode, 0, changed.stderr + changed.stdout)
        self.assertIn("building the team prefix", changed.stderr)
        self.assertTrue((self.prefix / "opt" / "second").exists())

    def test_a_failing_line_keeps_the_old_prefix_and_is_named(self) -> None:
        self.lines(TOOL)
        self.assertEqual(self.p.team("image").returncode, 0)
        self.lines(TOOL, "touch /opt/wouf/half-built", "echo broken-line-said; exit 3")
        failed = self.p.team("image")
        self.assertEqual(failed.returncode, 2, failed.stderr + failed.stdout)
        self.assertIn("'echo broken-line-said; exit 3' failed", failed.stderr)
        self.assertIn("broken-line-said", failed.stderr)
        self.assertTrue((self.prefix / "opt" / "bin" / "wouf-hello").is_file(), "the old prefix is kept")
        self.assertFalse((self.prefix / "opt" / "half-built").exists())
        self.assertFalse((self.prefix / "opt.new").exists())
        self.assertNotIn("half-built", (self.prefix / "stamp.json").read_text())

    def test_the_build_sandbox_cannot_reach_host_loopback(self) -> None:
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        self.addCleanup(server.close)
        self.lines(LOOPBACK.format(port=server.getsockname()[1]))
        done = self.p.team("image")
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        self.assertTrue((self.prefix / "opt" / "loopback.txt").read_text().startswith("failed"))
        self.assertIn("egress-proxy: listening",
                      (self.prefix / "build" / "egress.log").read_text(), "the build ran behind the proxy")

    def test_install_output_is_masked_like_run_output(self) -> None:
        self.lines(PRINTS_KEY, PRINTS_KEY + "; exit 3")
        failed = self.p.team("image")
        self.assertEqual(failed.returncode, 2, failed.stderr + failed.stdout)
        log = (self.prefix / "build.log").read_text()
        self.assertIn("***", log)
        self.assertIn("***", failed.stderr)
        self.assertNotIn(KEY, log + failed.stderr + failed.stdout)


if __name__ == "__main__":
    unittest.main()
