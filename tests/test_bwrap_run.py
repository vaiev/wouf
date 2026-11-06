"""mode = bwrap: team run in a bubblewrap sandbox with network = none, end to end with the stand-in agent CLI.

The refusals and the plan need no bubblewrap; the runs are skipped when bwrap_usable() is false (the bwrap CI job
fails on any skip). A recording bwrap first on PATH writes each argv and environment it was started with, then
starts the real one."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_team_cli import FAKE_CLI, KEY, Project  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".agents" / "bin"))
from teamlib import bwrap  # noqa: E402

USABLE = bwrap.bwrap_usable()
REAL = shutil.which("bwrap")
OTHER = "other-stored-value-456"
CANARY = "host-env-canary-789"
# The stand-in CLI, plus what a bot may try in bwrap mode: show its environment, link its receipts to a host file.
BOX_CLI = FAKE_CLI.replace("#!/bin/sh\n", """#!/bin/sh
env > /bot/seen-env
if grep -q RLINK in/BRIEF.md 2>/dev/null; then mkdir -p /bot/logs; rm -f /bot/logs/worker.jsonl; \
  ln -s "$(sed -n 's/^Canary: //p' in/BRIEF.md)" /bot/logs/worker.jsonl; fi
""", 1)
# A review bot that tries to write its inputs (its copy and the staged one) and its repository before out/.
REVIEW_TAMPER = """#!/bin/sh
echo tamper >> in/BRIEF.md 2>/dev/null && echo "in written" >> out/writes.txt
echo tamper >> /inbox/BRIEF.md 2>/dev/null && echo "inbox written" >> out/writes.txt
echo tamper >> repo/file.txt 2>/dev/null && echo "repo written" >> out/writes.txt
(cd repo && git commit -q -am tamper) 2>/dev/null && echo "repo committed" >> out/writes.txt
echo "# Review" > out/REVIEW.md
echo "WOUF Review · T-12 · approve"
"""
# A bot that is still working when it is stopped: work in out/ and uncommitted in its repository, then a long sleep.
STOP_CLI = """#!/bin/sh
echo partial > out/partial.txt
echo wip >> repo/file.txt
: > /bot/working
sleep 300
echo "WOUF Worker · T-20 · ready-for-review"
"""
# A login command and a login check that write what they saw, where only a sandbox on the box can write it.
FAKE_LOGIN = """#!/bin/sh
mkdir -p "$HOME/.fake" && echo signed-in > "$HOME/.fake/auth.json"
{ echo "home $HOME"; test -e "$1" && echo SEES-PROJECT; test -t 0 && echo TTY; } > /bot/login-saw
"""
LOGIN_RUNNER = ("light =\nauth = login\nlogin = sh /bot/fake-login.sh {root}\n"
                "login_check = test -s \"$HOME/.fake/auth.json\" && echo \"$HOME\" > /bot/checked\n")


def bwrap_project(network: str = "none") -> Project:
    p = Project(mode="bwrap", runner="fake")
    p.set("network = bridge", f"network = {network}")
    p.set("command = fake-cli {prompt}", "command = sh /bot/fake-cli.sh {prompt}")
    return p


class BwrapPlanTest(unittest.TestCase):
    """No bubblewrap needed: mode = bwrap is a valid environment, and the networks it cannot keep are refused."""

    def setUp(self) -> None:
        self.p = bwrap_project()
        self.addCleanup(self.p.close)

    def test_the_plan_is_a_boxed_one(self) -> None:
        plan = self.p.plan("worker", "T-1", "--brief", str(self.p.brief))
        self.assertEqual(plan["mode"], "bwrap")
        self.assertEqual(plan["workdir"], "/bot/tasks/T-1")
        self.assertEqual(plan["argv"][:2], ["sh", "/bot/fake-cli.sh"])
        self.assertIn("Read /bot/tasks/T-1/in/LOGIN.md first, then /wouf/rules.md", plan["argv"][2])

    def test_doctor_takes_bwrap_as_an_environment(self) -> None:
        self.assertIn("ok   bot environment: bwrap", self.p.team("doctor").stdout)

    def test_bridge_is_refused_before_anything_is_staged(self) -> None:
        stage = self.p.root / ".agents" / "team" / "env" / "worker" / "tasks" / "T-1"
        for network, said in (("bridge", "shares this computer's network and loopback"), ("host", "use none")):
            with self.subTest(network=network):
                self.p.set("network = none", f"network = {network}")
                try:
                    result = self.p.team("run", "worker", "T-1", "--brief", str(self.p.brief))
                finally:
                    self.p.set(f"network = {network}", "network = none")
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn(said, result.stderr)
                self.assertFalse(stage.exists(), "nothing is staged")


class BwrapLifecycleTest(unittest.TestCase):
    """team bot create, inspect, reset and remove on env/<member>/box; none of them starts a sandbox."""

    def setUp(self) -> None:
        self.p = bwrap_project()
        self.addCleanup(self.p.close)
        self.member = self.p.root / ".agents" / "team" / "env" / "worker"
        self.box = self.member / "box"

    def test_create_makes_a_private_box(self) -> None:
        result = self.p.team("bot", "create", "worker")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(stat.S_IMODE(self.box.stat().st_mode), 0o700)
        self.assertIn(str(self.box), result.stdout)

    def test_inspect_shows_the_box_as_bot_and_follows_no_link(self) -> None:
        self.p.team("bot", "create", "worker")
        (self.box / "home").mkdir()
        (self.box / "home" / "notes.txt").write_text("x")
        (self.box / "escape").symlink_to(self.p.root)
        listed = self.p.team("bot", "inspect", "worker")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(listed.stdout.split(), ["/bot/escape@", "/bot/home/"])
        self.assertEqual(self.p.team("bot", "inspect", "worker", "/home").stdout.split(), ["/bot/home/notes.txt"])
        for path in ("escape", "escape/.agents", "../..", "nothing"):
            with self.subTest(path=path):
                refused = self.p.team("bot", "inspect", "worker", path)
                self.assertEqual(refused.returncode, 2, refused.stdout)
                self.assertNotIn(".agents", refused.stdout)

    def test_reset_archives_the_box_and_gives_a_clean_one(self) -> None:
        canary = Path(self.p.tmp.name) / "host-file.txt"
        canary.write_text("host-canary")
        self.p.team("bot", "create", "worker")
        (self.box / "old.txt").write_text("old work")
        (self.box / "hostfile").symlink_to(canary)
        # The bot owns its box: a folder it made read-only (with a forged file in it) does not survive the reset.
        (self.box / "home" / "deep").mkdir(parents=True)
        (self.box / "home" / ".profile").write_text("forged")
        (self.box / "home" / "deep").chmod(0o500)
        (self.box / "home").chmod(0o500)
        result = self.p.team("bot", "reset", "worker")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(self.box.iterdir()), [])
        self.assertEqual(stat.S_IMODE(self.box.stat().st_mode), 0o700)
        saved = list((self.member.parent / "_archive").glob("worker-*/box"))
        self.assertEqual(len(saved), 1, saved)
        self.assertEqual((saved[0] / "old.txt").read_text(), "old work")
        self.assertTrue((saved[0] / "hostfile").is_symlink(), "a link stays a link")
        self.assertNotIn("host-canary", "".join(p.read_text() for p in saved[0].rglob("*")
                                                 if p.is_file() and not p.is_symlink()))

    def test_remove_archives_the_box_and_removes_it(self) -> None:
        self.p.team("bot", "create", "worker")
        (self.box / "old.txt").write_text("old work")
        (self.box / "home").mkdir()
        (self.box / "home").chmod(0o500)
        self.box.chmod(0o500)
        result = self.p.team("bot", "remove", "worker")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.member.exists())
        self.assertEqual([p.read_text() for p in (self.member.parent / "_archive").glob("worker-*/box/old.txt")],
                         ["old work"])
        self.assertIn("status: retired", self.p.bot_file("worker").read_text())


@unittest.skipUnless(USABLE, "bwrap is not installed or cannot start a sandbox here (user namespaces)")
class BwrapRunTest(unittest.TestCase):
    """Real runs in bubblewrap, started as the human starts them: through bin/team, with the human's environment."""

    def setUp(self) -> None:
        self.p = bwrap_project()
        self.addCleanup(self.p.close)
        self.p.store_secret("ALPHA_KEY", KEY)
        self.p.store_secret("OTHER_KEY", OTHER)
        self.env = self.p.root / ".agents" / "team" / "env"
        for member, script in (("worker", BOX_CLI), ("review", REVIEW_TAMPER)):
            (self.env / member / "box").mkdir(parents=True)
            (self.env / member / "box" / "fake-cli.sh").write_text(script)
        self.calls = Path(self.p.tmp.name) / "bwrap-calls"
        recorder = self.p.bin / "bwrap"
        recorder.write_text(f"#!{sys.executable}\nimport json, os, sys\n"
                            f"with open({str(self.calls)!r}, 'a') as f:\n"
                            "    f.write(json.dumps({'argv': sys.argv[1:], 'env': dict(os.environ)}) + '\\n')\n"
                            f"os.execv({REAL!r}, [{REAL!r}, *sys.argv[1:]])\n")
        recorder.chmod(0o755)

    def stage(self, task: str, member: str = "worker") -> Path:
        return self.env / member / "tasks" / task

    def brief(self, text: str) -> Path:
        path = self.p.root / f"brief-{len(list(self.p.root.glob('brief-*')))}.md"
        path.write_text(text)
        return path

    def short(self) -> None:
        self.p.set("timeout_minutes = 30", "timeout_minutes = 0.05")
        self.addCleanup(self.p.set, "timeout_minutes = 0.05", "timeout_minutes = 30")

    def started(self) -> list[dict]:
        """The recorded bwrap calls that ran a bot (the trial sandbox of bwrap_usable aside)."""
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        return [c for c in calls if "/bot" in c["argv"]]

    def test_a_run_returns_its_result_and_commits(self) -> None:
        result = self.p.team("run", "worker", "T-1", "--brief", str(self.p.brief), "--repo", WOUF_CANARY=CANARY)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("WOUF Worker · T-1 · ready-for-review", result.stdout)
        self.assertEqual(result.stderr.count("no syscall filter"), 1, result.stderr)
        stage = self.stage("T-1")
        output = (stage / "output.log").read_text()
        self.assertIn("key length 22", output)
        for unseen in ("SEES-PROJECT", "SEES-SSH", KEY):
            self.assertNotIn(unseen, output)
        self.assertIn("change by worker", self.p.git("log", "--oneline", "wouf/T-1"))
        self.assertEqual((stage / "out" / "result.txt").read_text(), "done\n")
        # The key reached the bot on stdin, with WOUF_MASK of this run's value only; nothing of the host's environment.
        seen = (self.env / "worker" / "box" / "seen-env").read_text()
        self.assertIn(f"ALPHA_KEY={KEY}\n", seen)
        self.assertIn(f"WOUF_MASK={json.dumps([KEY])}\n", seen)
        for unseen in (OTHER, CANARY, str(self.p.root), "WOUF_SECRET_DIR"):
            self.assertNotIn(unseen, seen)

    def test_the_key_is_in_neither_the_argv_nor_the_environment_of_bwrap(self) -> None:
        result = self.p.team("run", "worker", "T-2", "--brief", str(self.p.brief), WOUF_CANARY=CANARY)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        calls = self.started()
        self.assertEqual(len(calls), 1, calls)
        recorded = json.dumps(calls)
        for unseen in (KEY, OTHER, CANARY):
            self.assertNotIn(unseen, recorded)
        self.assertEqual([k for k in calls[0]["env"] if k != "LC_CTYPE"], [])  # LC_CTYPE: the recorder's Python

    def test_only_the_mount_table_of_the_run_is_bound(self) -> None:
        result = self.p.team("run", "worker", "T-3", "--brief", str(self.p.brief), "--repo")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        argv, stage = self.started()[0]["argv"], self.stage("T-3")
        binds = {argv[i + 2]: (argv[i], argv[i + 1]) for i in range(len(argv) - 2) if argv[i] in ("--bind", "--ro-bind")}
        self.assertEqual(binds["/bot"], ("--bind", str(self.env / "worker" / "box")))
        self.assertEqual(binds["/inbox"], ("--ro-bind", str(stage / "in")))
        self.assertEqual(binds["/wouf"], ("--ro-bind", str(stage / "pack")))
        self.assertEqual(binds["/repo.bundle"], ("--ro-bind", str(stage / "in" / "repo.bundle")))
        self.assertEqual(binds["/outbox"], ("--bind", str(stage / "outbox")))
        self.assertEqual(binds["/run/wouf"], ("--ro-bind", str(stage / "ctl")))
        sources = [source for _, source in binds.values()]
        for never in ("run.json", "result.json", "output.log", "run.lock"):
            self.assertNotIn(str(stage / never), sources)
        self.assertFalse(any(s == str(stage) or s.startswith(str(self.env / "worker" / "logs")) for s in sources))
        self.assertFalse(any(s == str(self.p.root) or s.startswith(str(self.p.root / ".agents" / "team" / "logs"))
                             for s in sources))

    def test_links_and_forged_receipts_do_not_come_back(self) -> None:
        canary = Path(self.p.tmp.name) / "host-receipts.jsonl"
        canary.write_text('{"t":"2026-10-19T09:00:00.000+00:00","who":"worker","task":"T-9","outcome":"done",'
                          '"signal":"host-canary"}\n')
        result = self.p.team("run", "worker", "T-9", "--brief", str(self.brief(
            f"Objective: LINK and FORGE and RLINK\nCanary: {canary}\n")))
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        out = self.stage("T-9") / "out"
        self.assertTrue((out / "result.txt").is_file())
        for name in ("leak", "root"):
            self.assertFalse((out / name).exists() or (out / name).is_symlink(), name)
        self.assertIn("skipped 2 links", result.stderr)
        logs = self.env / "worker" / "logs"
        self.assertFalse((logs / "review.jsonl").exists(), "another member's receipt")
        mine = logs / "worker.jsonl"
        self.assertNotIn("host-canary", mine.read_text() if mine.is_file() else "")
        self.assertEqual(json.loads((self.stage("T-9") / "result.json").read_text())["state"], "done")

    def test_the_time_limit_keeps_the_work(self) -> None:
        self.short()
        result = self.p.team("run", "worker", "T-6", "--brief", str(self.brief("Objective: SLEEP\n")), "--repo")
        self.assertEqual(result.returncode, 124, result.stderr + result.stdout)
        self.assertTrue((self.stage("T-6") / "out" / "result.txt").is_file(), "the work comes back after the limit")
        self.assertIn("change by worker", self.p.git("log", "--oneline", "wouf/T-6"))

    def test_detach_and_a_killed_caller_leave_the_run_going(self) -> None:
        self.short()
        slow = self.brief("Objective: SLEEP\n")
        detached = self.p.team("run", "worker", "T-7", "--brief", str(slow), "--detach")
        self.assertEqual(detached.returncode, 0, detached.stderr)
        self.assertIn("in the background", detached.stdout)
        caller = subprocess.Popen([sys.executable, str(self.p.root / ".agents" / "bin" / "team"), "run", "worker",
                                   "T-8", "--brief", str(slow)], env=self.p.env, cwd=self.p.root,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not any(str(self.stage("T-8")) in " ".join(c["argv"])
                                                      for c in self.started()):
            time.sleep(0.2)  # until the caller's bot works: its sandbox was started
        caller.kill()
        caller.wait()
        for task in ("T-7", "T-8"):
            result = self.p.team("result", "worker", task, "--wait", "120")
            self.assertEqual(result.returncode, 124, task + result.stderr + result.stdout)
            self.assertTrue((self.stage(task) / "out" / "result.txt").is_file(), task)

    def test_a_review_run_cannot_write_its_inputs_or_its_repository(self) -> None:
        result = self.p.team("run", "review", "T-12", "--brief", str(self.p.brief), "--repo")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        stage = self.stage("T-12", "review")
        self.assertTrue((stage / "out" / "REVIEW.md").is_file(), "out/ stays writable")
        self.assertFalse((stage / "out" / "writes.txt").exists(), (stage / "out" / "writes.txt").read_text()
                         if (stage / "out" / "writes.txt").exists() else "")
        self.assertEqual((stage / "in" / "BRIEF.md").read_text().strip(), self.p.brief.read_text().strip())
        self.assertNotIn("tamper", self.p.git("log", "--all", "--oneline"))

    def test_stop_ends_the_cli_and_keeps_the_work(self) -> None:
        (self.env / "worker" / "box" / "fake-cli.sh").write_text(STOP_CLI)
        result = self.p.team("run", "worker", "T-20", "--brief", str(self.p.brief), "--repo", "--detach")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not (self.env / "worker" / "box" / "working").exists():
            time.sleep(0.2)  # until the bot works
        began = time.monotonic()
        stopped = self.p.team("bot", "stop", "worker")
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertIn("stopped 1 running", stopped.stdout)
        self.assertLess(time.monotonic() - began, bwrap.STOP_GRACE, "TERM ended it, not the KILL after the grace")
        self.p.team("result", "worker", "T-20", "--wait", "120")
        stage = self.stage("T-20")
        self.assertEqual(json.loads((stage / "result.json").read_text())["state"], "stopped")
        self.assertEqual((stage / "out" / "partial.txt").read_text(), "partial\n")
        self.assertTrue((stage / "out" / "result.bundle").is_file())
        self.assertIn("wip(T-20)", self.p.git("log", "--oneline", "wouf/T-20"))

    def test_login_and_its_check_run_in_a_sandbox_on_the_box(self) -> None:
        self.p.set("light =\nauth = ALPHA_KEY\n", LOGIN_RUNNER.format(root=self.p.root))
        (self.env / "worker" / "box" / "fake-login.sh").write_text(FAKE_LOGIN)
        master, slave = os.openpty()
        try:
            done = subprocess.run([sys.executable, str(self.p.root / ".agents" / "bin" / "team"), "bot", "login",
                                   "worker"], env=self.p.env, cwd=self.p.root, stdin=slave, stdout=slave,
                                  stderr=subprocess.PIPE, text=True, timeout=300)
        finally:
            os.close(slave)
        shown = b""
        try:
            while True:
                chunk = os.read(master, 4096)
                if not chunk:
                    break
                shown += chunk
        except OSError:
            pass
        os.close(master)
        self.assertEqual(done.returncode, 0, done.stderr + shown.decode())
        self.assertIn("login done", shown.decode())
        box = self.env / "worker" / "box"
        self.assertEqual((box / "login-saw").read_text(), "home /bot/home\nTTY\n")
        self.assertEqual((box / "checked").read_text(), "/bot/home\n")
        self.assertEqual((box / "home" / ".fake" / "auth.json").read_text(), "signed-in\n")
        calls = self.started()
        self.assertEqual(len(calls), 2, calls)
        for call in calls:
            argv = call["argv"]
            binds = {argv[i + 2]: (argv[i], argv[i + 1]) for i in range(len(argv) - 2)
                     if argv[i] in ("--bind", "--ro-bind")}
            self.assertEqual(binds["/bot"], ("--bind", str(box)))
            self.assertIn("--unshare-all", argv)
            self.assertIn("HOME /bot/home", " ".join(argv))
            mine = [s for _, s in binds.values() if s == str(self.p.root) or s.startswith(str(self.p.root) + "/")]
            self.assertEqual(mine, [str(box)], "of the project, only the box")


if __name__ == "__main__":
    unittest.main()
