"""A container run whose ssh stream to the engine's host goes dead (T-0055).

Seen 2026-11-02: a short network drop left the stream dead without an error; the supervisor waited for the kernel's
TCP keepalive (7200 s), the 60-minute limit showed after 2 h 38 min and the return blocks were lost. Here a fake ssh
drops the stream once while the fake engine's container goes on and ends by itself.
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_team_cli import Project, team_module  # noqa: E402
from test_engine_calls import FAKE_ENGINE  # noqa: E402

BLOCK = "WOUF Worker · T-1 · ready-for-review"

# The bot's container, on top of the fake engine of test_engine_calls: with the flag drop, start says GAP-MARK (the
# fake ssh drops the stream there) and the container goes on alone, then ends with its return block in its log.
CONTAINER = r'''
if args[0] == "start" and (state / "drop").exists():
    (state / "start-stdin").write_bytes(sys.stdin.buffer.read())
    (state / "status").write_text("running")
    rest = "during the gap\n" + {block!r} + "\n"
    import subprocess
    subprocess.Popen([sys.executable, "-c", "import sys, time; from pathlib import Path; s = Path(sys.argv[1]); "
                      "time.sleep(float(sys.argv[3])); (s / 'logs').write_text('before the gap\\n' + sys.argv[2]); "
                      "(s / 'status').write_text('exited')", str(state), rest,
                      "60" if (state / "forever").exists() else "1.5"], start_new_session=True)
    print("before the gap\nGAP-MARK", flush=True)
    time.sleep(30)
    sys.exit(0)
if args[0] == "inspect":
    status = state / "status"
    print(status.read_text() if status.exists() else "created")
    sys.exit(0)
if args[0] == "wait":
    while (state / "status").read_text() != "exited":
        time.sleep(0.1)
    print(0)
    sys.exit(0)
if args[0] == "logs":
    sys.stdout.write((state / "logs").read_text())
    sys.exit(0)
'''

# ssh: keeps every call; drops the start pipeline's stream once at GAP-MARK (ends 255, as ssh does when its
# keepalive gives up), and with the flag unreachable fails every later connection.
FAKE_SSH = r'''#!{python}
import json, os, signal, subprocess, sys
from pathlib import Path
state = Path({state!r})
args = sys.argv[1:]
with open(state / "ssh.jsonl", "a") as f:
    f.write(json.dumps({{"argv": args}}) + "\n")
rest = args[args.index("--") + 2:]
env = {{k: v for k, v in os.environ.items() if k not in ("DOCKER_HOST", "CONTAINER_HOST")}}
env["FAKE_REMOTE"] = "1"
starts = "wouf-ready" in rest[0]
if (state / "unreachable").exists() and not starts:
    sys.stderr.write("ssh: connect to host vps port 22: Connection timed out\n")
    sys.exit(255)
if not (starts and (state / "drop").exists()):
    sys.exit(subprocess.run(["sh", "-c", " ".join(rest)], env=env).returncode)
proc = subprocess.Popen(["sh", "-c", " ".join(rest)], env=env, stdout=subprocess.PIPE, start_new_session=True)
for line in proc.stdout:
    if line.startswith(b"GAP-MARK"):
        os.killpg(proc.pid, signal.SIGKILL)
        sys.stderr.write("Timeout, server not responding.\n")
        sys.exit(255)
    sys.stdout.buffer.write(line)
    sys.stdout.buffer.flush()
sys.exit(proc.wait())
'''


class SshLossTest(unittest.TestCase):
    def setUp(self) -> None:
        saved = os.environ.pop("CONTAINER_SSHKEY", None)
        if saved is not None:
            self.addCleanup(os.environ.__setitem__, "CONTAINER_SSHKEY", saved)
        self.p = Project(mode="container", engine_name="fakeengine", runner="alpha")
        self.addCleanup(self.p.close)
        self.p.set("docker_host =", "docker_host = ssh://vps")
        self.state = Path(self.p.tmp.name) / "engine"
        self.state.mkdir()
        fake = self.p.bin / "fakeengine"
        hook = CONTAINER.format(block=BLOCK)
        fake.write_text(FAKE_ENGINE.format(python=sys.executable, state=str(self.state))
                        .replace("head = args[:2]\n", hook + "head = args[:2]\n", 1))
        fake.chmod(0o755)
        ssh = self.p.bin / "ssh"
        ssh.write_text(FAKE_SSH.format(python=sys.executable, state=str(self.state)))
        ssh.chmod(0o755)
        self.stage = self.p.root / ".agents" / "team" / "env" / "worker" / "tasks" / "T-1"
        created = self.p.team("bot", "create", "worker")
        self.assertEqual(created.returncode, 0, created.stderr)

    def ssh_calls(self) -> list[list[str]]:
        path = self.state / "ssh.jsonl"
        return [json.loads(line)["argv"] for line in path.read_text().splitlines()] if path.exists() else []

    def run_worker(self):
        return self.p.team("run", "worker", "T-1", "--brief", str(self.p.brief), "--repo")

    def result(self) -> dict:
        return json.loads((self.stage / "result.json").read_text())

    def test_every_ssh_of_the_engine_layer_ends_a_dead_connection_within_a_minute(self) -> None:
        team_module()
        team = sys.modules["teamlib.engine"]
        self.assertEqual(self.run_worker().returncode, 0)
        calls = self.ssh_calls() + [team.shell({"engine": "podman", "docker_host": "ssh://u@h:22/s"}, "true", [])[1:]]
        self.assertGreaterEqual(len(calls), 3, calls)
        for argv in calls:
            options = argv[:argv.index("--")]
            found = dict(options[i + 1].split("=", 1) for i, a in enumerate(options) if a == "-o")
            interval, count = int(found["ServerAliveInterval"]), int(found["ServerAliveCountMax"])
            self.assertLessEqual(interval * count, 60, argv)
            self.assertLessEqual(int(found["ConnectTimeout"]), 30, argv)

    def test_a_stream_that_drops_once_still_brings_the_return_block_and_the_results(self) -> None:
        self.p.set("timeout_minutes = 30", "timeout_minutes = 0.2")
        (self.state / "drop").write_text("")
        began = time.monotonic()
        done = self.run_worker()
        self.assertLess(time.monotonic() - began, 60)
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        self.assertEqual(self.result()["state"], "done", self.result())
        self.assertIn(BLOCK, (self.stage / "output.log").read_text())
        self.assertIn(BLOCK, done.stdout)
        self.assertEqual((self.stage / "out" / "result.txt").read_text(), "done\n")
        self.assertEqual((self.state / "removed").read_text(), f"wouf-{self.p.root.name}-worker-T-1")

    def test_a_host_that_stays_away_ends_the_run_naming_the_lost_connection(self) -> None:
        self.p.set("timeout_minutes = 30", "timeout_minutes = 0.05")
        for flag in ("drop", "forever", "unreachable"):
            (self.state / flag).write_text("")
        began = time.monotonic()
        done = self.run_worker()
        self.assertLess(time.monotonic() - began, 60)
        self.assertNotEqual(done.returncode, 0)
        result = self.result()
        self.assertEqual(result["state"], "failed", result)
        self.assertIn("connection to the engine host", result["error"])
        self.assertNotIn("timeout", result["error"])
        said = [line for line in done.stderr.splitlines() if "connection to the engine host" in line]
        self.assertEqual(len(said), 1, done.stderr)

    def test_a_healthy_run_makes_no_extra_engine_calls(self) -> None:
        self.assertEqual(self.run_worker().returncode, 0)
        self.assertEqual(len(self.ssh_calls()), 2, self.ssh_calls())
        engine = [json.loads(line)["argv"][0] for line in (self.state / "calls.jsonl").read_text().splitlines()]
        self.assertFalse({"inspect", "wait", "logs", "kill"} & set(engine), engine)


if __name__ == "__main__":
    unittest.main()
