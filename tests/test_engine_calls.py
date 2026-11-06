"""The engine calls of one container `team run`, counted with a fake engine and a fake ssh that record every call.

A call counts as one round trip to the engine's host: each ssh connection, plus each engine call made from this
computer (with DOCKER_HOST set, that is an ssh connection of the engine's own). Calls the remote shell makes on the
engine's host itself are recorded too, but are not round trips.
"""

from __future__ import annotations

import io
import json
import os
import shlex
import subprocess
import sys
import tarfile
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_team_cli import Project, team_module  # noqa: E402

MARKER = "plain-engine-marker-7713"

# The engine: keeps every call (argv, whether the remote shell made it, its environment), and acts like one with
# an image, volumes and one container, all as files in its state folder.
FAKE_ENGINE = r'''#!{python}
import io, json, os, sys, tarfile, time
from pathlib import Path
state = Path({state!r})
args = sys.argv[1:]
with open(state / "calls.jsonl", "a") as f:
    f.write(json.dumps({{"argv": args, "remote": os.environ.get("FAKE_REMOTE") == "1",
                        "env": " ".join(os.environ.values())}}) + "\n")
def fails(name):
    flag = state / ("fail-" + name)
    if flag.exists():
        flag.unlink()
        sys.stderr.write("fake " + name + " failed\n")
        sys.exit(1)
head = args[:2]
if args[0] == "build":
    label = next(a for a in args if a.startswith("wouf.recipe="))
    sys.stdin.read()
    (state / "image").write_text(label.split("=", 1)[1])
elif head == ["image", "inspect"]:
    if not (state / "image").exists():
        sys.exit(1)
    print((state / "image").read_text() if "-f" in args else "[]")
elif head == ["volume", "inspect"]:
    sys.exit(0 if (state / ("volume-" + args[-1])).exists() else 1)
elif head == ["volume", "create"]:
    (state / ("volume-" + args[-1])).write_text(" ".join(args))
elif head == ["volume", "rm"]:
    (state / ("volume-" + args[-1])).unlink(missing_ok=True)
elif args[0] == "create":
    fails("create")
    if (state / "slow-create").exists():
        (state / "creating").write_text("")
        end = time.monotonic() + 20
        while not (state / "release").exists() and time.monotonic() < end:
            time.sleep(0.1)
    (state / "created").write_text(json.dumps(args))
elif args[0] == "cp" and args[1] == "-":
    (state / "inbound.tar").write_bytes(sys.stdin.buffer.read())
elif args[0] == "start":
    (state / "start-stdin").write_bytes(sys.stdin.buffer.read())
    if (state / "slow").exists():
        end = time.monotonic() + 20
        while not (state / "stop-asked").exists() and time.monotonic() < end:
            time.sleep(0.1)
    print("WOUF Worker · T-1 · ready-for-review")
elif args[0] == "stop":
    (state / "stop-asked").write_text("")
elif args[0] == "cp" and args[1].endswith(":/outbox") and args[2] == "-":
    emit = state / "outbox.py"
    if emit.exists():
        exec(emit.read_text())
    else:
        with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as tar:
            data = b"done\n"
            info = tarfile.TarInfo("outbox/out/result.txt"); info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
elif args[0] == "cp" and args[1].endswith(":/bot") and args[2] == "-":
    tarfile.open(fileobj=sys.stdout.buffer, mode="w|").close()
elif args[0] == "rm":
    (state / "removed").write_text(args[-1])
sys.exit(0)
'''

# ssh: keeps every call, then runs the remote command with sh on this computer, marked as remote.
FAKE_SSH = r'''#!{python}
import json, os, subprocess, sys
args = sys.argv[1:]
with open({calls!r}, "a") as f:
    f.write(json.dumps({{"argv": args, "env": " ".join(os.environ.values())}}) + "\n")
rest = args[args.index("--") + 2:]
env = {{k: v for k, v in os.environ.items() if k not in ("DOCKER_HOST", "CONTAINER_HOST")}}
env["FAKE_REMOTE"] = "1"
sys.exit(subprocess.run(["sh", "-c", " ".join(rest)], env=env).returncode)
'''


class EngineCallsTest(unittest.TestCase):
    def setUp(self) -> None:
        # The lead's own engine key would change the ssh line these tests count; a test sets it where it needs one.
        saved = os.environ.pop("CONTAINER_SSHKEY", None)
        if saved is not None:
            self.addCleanup(os.environ.__setitem__, "CONTAINER_SSHKEY", saved)
        self.p = Project(mode="container", engine_name="fakeengine", runner="alpha")
        self.addCleanup(self.p.close)
        self.p.set("docker_host =", "docker_host = ssh://vps")
        self.p.store_secret("ALPHA_KEY", MARKER)
        self.state = Path(self.p.tmp.name) / "engine"
        self.state.mkdir()
        fake = self.p.bin / "fakeengine"
        fake.write_text(FAKE_ENGINE.format(python=sys.executable, state=str(self.state)))
        fake.chmod(0o755)
        ssh = self.p.bin / "ssh"
        ssh.write_text(FAKE_SSH.format(python=sys.executable, calls=str(self.state / "ssh.jsonl")))
        ssh.chmod(0o755)
        self.env = self.p.root / ".agents" / "team" / "env"
        self.stage = self.env / "worker" / "tasks" / "T-1"

    def calls(self, name: str = "calls") -> list[dict]:
        path = self.state / f"{name}.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def forget(self) -> None:
        for name in ("calls", "ssh"):
            (self.state / f"{name}.jsonl").unlink(missing_ok=True)

    def round_trips(self) -> list[list[str]]:
        return [["ssh", *c["argv"]] for c in self.calls("ssh")] + [["fakeengine", *c["argv"]] for c in self.calls()
                                                                    if not c["remote"]]

    def warm(self) -> None:
        created = self.p.team("bot", "create", "worker")
        self.assertEqual(created.returncode, 0, created.stderr)
        self.forget()

    def run_worker(self, *extra: str) -> subprocess.CompletedProcess:
        return self.p.team("run", "worker", "T-1", "--brief", str(self.p.brief), "--repo", *extra)

    def result(self) -> dict:
        return json.loads((self.stage / "result.json").read_text())

    def stamp(self) -> dict:
        return json.loads((self.env / "engine-stamp.json").read_text())

    # ---- the count

    def test_a_warm_container_run_takes_at_most_three_round_trips(self) -> None:
        self.warm()
        done = self.run_worker()
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        trips = self.round_trips()
        self.assertLessEqual(len(trips), 3, json.dumps(trips, indent=1)[:3000])
        self.assertEqual([t[0] for t in trips], ["ssh", "ssh", "fakeengine"], trips)
        self.assertEqual(trips[-1], ["fakeengine", "version"])
        self.assertTrue(all(t[1] == "-T" and t[-3:-1] == ["--", "vps"] for t in trips[:2]), trips)
        # The calls on the engine's host: one pipeline to start the bot, one to collect it.
        remote = [c["argv"][0] if c["argv"][0] not in ("image", "volume") else " ".join(c["argv"][:2])
                  for c in self.calls() if c["remote"]]
        self.assertEqual(remote, ["rm", "image inspect", "volume inspect", "create", "cp", "start", "cp", "rm"])
        self.assertNotIn(["image", "inspect"], [c["argv"][:2] for c in self.calls() if not c["remote"]],
                         "the stage needs no image inspect of its own")
        self.assertEqual((self.state / "removed").read_text(), f"wouf-{self.p.root.name}-worker-T-1")
        self.assertEqual(self.result()["state"], "done")

    def test_a_cold_run_checks_the_image_and_storage_once_and_keeps_the_answer(self) -> None:
        done = self.run_worker()
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        local = [c["argv"][:2] for c in self.calls() if not c["remote"]]
        self.assertEqual(local.count(["image", "inspect"]), 1, local)
        self.assertEqual(local.count(["volume", "inspect"]), 1, local)
        self.assertIn("build", [c[0] for c in local])
        stamp = self.stamp()
        name = f"wouf-{self.p.root.name}"
        self.assertEqual(stamp["at"], "fakeengine ssh://vps")
        self.assertEqual(stamp["images"], {f"{name}-image": (self.state / "image").read_text()})
        self.assertEqual(stamp["volumes"], [f"{name}-worker"])

    # ---- keys

    def test_keys_reach_the_bot_only_as_lines_on_its_stdin(self) -> None:
        self.warm()
        self.run_worker()
        taken = (self.state / "start-stdin").read_text().split("\n")
        self.assertIn(f"ALPHA_KEY={MARKER}", taken)
        self.assertEqual(len([line for line in taken if line.startswith("WOUF_MASK=")]), 1)
        for call in self.calls() + self.calls("ssh"):
            self.assertNotIn(MARKER, json.dumps(call["argv"]), "no key in an argument or the pipeline text")
            self.assertNotIn(MARKER, call["env"], "no key in an environment")
        self.assertNotIn(MARKER.encode(), (self.state / "inbound.tar").read_bytes())
        for path in self.env.rglob("*"):
            if path.is_file():
                self.assertNotIn(MARKER.encode(), path.read_bytes(), path)

    # ---- the stamp

    def test_the_stamp_is_cleared_by_team_image_and_by_reset_and_remove(self) -> None:
        self.warm()
        name = f"wouf-{self.p.root.name}"
        stamp = self.env / "engine-stamp.json"
        stamp.write_text(json.dumps({**self.stamp(), "images": {f"{name}-image": "0" * 16},
                                     "volumes": [f"{name}-worker", f"{name}-gone"]}))
        self.assertEqual(self.p.team("image").returncode, 0)
        self.assertEqual(self.stamp()["images"], {f"{name}-image": (self.state / "image").read_text()})
        self.assertEqual(self.stamp()["volumes"], [], "team image clears the whole stamp")
        self.p.team("bot", "create", "worker")
        self.assertEqual(self.stamp()["volumes"], [f"{name}-worker"])
        self.forget()
        self.assertEqual(self.p.team("bot", "reset", "worker").returncode, 0)
        self.assertIn(["volume", "rm", "-f", f"{name}-worker"], [c["argv"] for c in self.calls()])
        self.assertEqual(self.stamp()["volumes"], [f"{name}-worker"], "reset gives new storage and notes it")
        self.assertEqual(self.p.team("bot", "remove", "worker").returncode, 0)
        self.assertEqual(self.stamp()["volumes"], [])

    def test_a_stale_stamp_never_starts_a_run_without_its_image_or_storage(self) -> None:
        self.warm()
        name = f"wouf-{self.p.root.name}"
        (self.state / "image").unlink()
        (self.state / f"volume-{name}-worker").unlink()
        done = self.run_worker()
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        local = [c["argv"] for c in self.calls() if not c["remote"]]
        self.assertIn("build", [c[0] for c in local])
        self.assertIn(["volume", "create", "--label", f"wouf.team={name}", "--label", "wouf.bot=worker",
                       f"{name}-worker"], local)
        self.assertEqual([c["argv"][0] for c in self.calls() if c["remote"]].count("create"), 1,
                         "the container is created only once both are there")

    def test_a_failed_start_clears_the_stamp_and_says_why(self) -> None:
        self.warm()
        (self.state / "fail-create").write_text("")
        done = self.run_worker()
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(self.result()["state"], "failed")
        self.assertIn("fake create failed", (self.stage / "run.log").read_text() + self.result()["error"])
        self.assertFalse((self.env / "engine-stamp.json").exists())
        self.assertNotIn("start", [c["argv"][0] for c in self.calls()])

    # ---- the pipeline

    def test_values_in_the_remote_pipeline_are_quoted(self) -> None:
        team_module()
        team = sys.modules["teamlib.engine"]
        nasty = ["$(touch pwned)", "a'b", "x; touch pwned2", "`touch pwned3`", "-oProxyCommand=x"]
        command = team.shell({"engine": "fakeengine", "docker_host": "ssh://user@vps:2222"}, 'printf "%s\\n" "$@"',
                             nasty)
        self.assertEqual(command[:6], ["ssh", "-T", "-l", "user", "-p", "2222"])
        self.assertEqual(command[-3:-1], ["--", "vps"])
        self.assertEqual(shlex.split(command[-1]), ["sh", "-c", 'printf "%s\\n" "$@"', "sh", *nasty])
        done = subprocess.run(command, env=self.p.env, cwd=self.state, capture_output=True, text=True)
        self.assertEqual(done.stdout.splitlines(), nasty)
        self.assertEqual(sorted(p.name for p in self.state.glob("pwned*")), [])

    def test_the_pipeline_uses_the_engine_ssh_key(self) -> None:
        # Seen 2026-10-29 after #39: without the key, ssh fell back to the default identities, the host refused it
        # and every container run ended 255 with a broken pipe.
        team_module()
        team = sys.modules["teamlib.engine"]
        with mock.patch.dict(os.environ, {"CONTAINER_SSHKEY": "/keys/engine id"}):
            command = team.shell({"engine": "podman", "docker_host": "ssh://wouf@203.0.113.5:22/run/podman.sock"},
                                 "true", ["x"])
        self.assertEqual(command[:9], ["ssh", "-T", "-l", "wouf", "-p", "22", "-i", "/keys/engine id", "-o"])
        self.assertEqual(command[9], "IdentitiesOnly=yes")
        self.assertEqual(command[-3:-1], ["--", "203.0.113.5"])

    def test_an_engine_host_that_is_not_a_plain_ssh_alias_runs_the_pipeline_here(self) -> None:
        team_module()
        team = sys.modules["teamlib.engine"]
        for host in ("ssh://-oProxyCommand=x", "ssh://a b", "tcp://vps:2375", "", "ssh://vps;id"):
            with self.subTest(host=host):
                command = team.shell({"engine": "docker", "docker_host": host}, "true", ["x"])
                self.assertEqual(command, ["sh", "-c", "true", "sh", "x"])

    def test_one_tar_brings_the_inputs_and_the_pack_and_the_bundle_travels_once(self) -> None:
        self.warm()
        self.run_worker()
        with tarfile.open(self.state / "inbound.tar") as tar:
            members = {m.name: m for m in tar.getmembers()}
        for name in ("inbox/BRIEF.md", "wouf/rules.md", "wouf/me.md", "repo.bundle"):
            self.assertIn(name, members)
        self.assertNotIn("inbox/repo.bundle", members)
        self.assertEqual({(m.uid, m.gid) for m in members.values()}, {(0, 0)})
        self.assertTrue(all(m.isfile() or m.isdir() for m in members.values()))
        script = json.loads((self.stage / "run.json").read_text())["script"]
        self.assertEqual(script.count("cp -R /inbox"), 1)
        self.assertIn("git fetch -q /repo.bundle", script)
        self.assertNotIn("../in/repo.bundle", script)

    def test_an_input_named_like_the_bundle_stays_an_input_without_a_repository(self) -> None:
        self.warm()
        given = self.p.root / "repo.bundle"
        given.write_text("not a bundle\n")
        done = self.p.team("run", "worker", "T-1", "--brief", str(self.p.brief), "--input", str(given))
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        with tarfile.open(self.state / "inbound.tar") as tar:
            names = tar.getnames()
        self.assertIn("inbox/repo.bundle", names)
        self.assertNotIn("repo.bundle", names)

    def test_a_stop_while_the_container_starts_still_stops_it(self) -> None:
        self.warm()
        (self.state / "slow").write_text("")
        started = self.run_worker("--detach")
        self.assertEqual(started.returncode, 0, started.stderr)
        deadline = time.monotonic() + 20
        while not (self.state / "start-stdin").exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.p.team("bot", "stop", "worker")
        waited = self.p.team("result", "worker", "T-1", "--wait", "30")
        self.assertIn("stopped", waited.stderr + waited.stdout)
        self.assertIn(["stop", "-t", "20", f"wouf-{self.p.root.name}-worker-T-1"],
                      [c["argv"] for c in self.calls() if not c["remote"]])

    def test_a_stop_while_the_container_is_created_never_starts_it(self) -> None:
        self.warm()
        (self.state / "slow-create").write_text("")
        started = self.run_worker("--detach")
        self.assertEqual(started.returncode, 0, started.stderr)
        deadline = time.monotonic() + 20
        while not (self.state / "creating").exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertTrue((self.state / "creating").exists())
        self.p.team("bot", "stop", "worker")
        (self.state / "release").write_text("")
        waited = self.p.team("result", "worker", "T-1", "--wait", "30")
        self.assertIn("stopped", waited.stderr + waited.stdout)
        self.assertEqual(self.result()["state"], "stopped")
        self.assertNotIn("start", [c["argv"][0] for c in self.calls()], "a stopped run never starts its container")
        self.assertFalse((self.state / "start-stdin").exists())
        self.assertEqual((self.state / "removed").read_text(), f"wouf-{self.p.root.name}-worker-T-1")

    # ---- the way back

    def test_results_are_masked_straight_into_out_and_receipt_values_never_reach_the_host_index(self) -> None:
        self.warm()
        logs = self.p.root / ".agents" / "team" / "logs"
        log, log_env = str(self.p.root / ".agents" / "bin" / "log"), self.p.env
        for args in (["add", "lead", "T-0", "done", "a receipt before the run"], ["find", "receipt"]):
            found = subprocess.run([sys.executable, log, *args], env=log_env, cwd=self.p.root,
                                   capture_output=True, text=True)
            self.assertEqual(found.returncode, 0, found.stderr)
        host_index = sorted(logs.glob(".index.sqlite*"))
        self.assertTrue(host_index, "the host index exists before the run")
        mine = self.env / "worker" / "logs"
        mine.mkdir(parents=True)
        (mine / ".index.sqlite").write_bytes(b"stale index " + MARKER.encode())
        receipt = json.dumps({"t": "2026-10-29T09:00:00.000+00:00", "who": "worker", "task": "T-1",
                              "outcome": "done", "signal": f"saw {MARKER}"})
        (self.state / "outbox.py").write_text(
            "def add(tar, name, data):\n"
            "    info = tarfile.TarInfo(name); info.size = len(data); tar.addfile(info, io.BytesIO(data))\n"
            "with tarfile.open(fileobj=sys.stdout.buffer, mode='w|') as tar:\n"
            f"    add(tar, 'outbox/out/notes.txt', b'note {MARKER}\\n')\n"
            f"    add(tar, 'outbox/out/sub/blob.bin', b'\\xff\\xfe {MARKER}')\n"
            f"    add(tar, 'outbox/logs/worker.jsonl', {(receipt + chr(10)).encode()!r})\n"
            f"    add(tar, 'outbox/logs/.index.sqlite', b'index {MARKER}')\n"
            "    link = tarfile.TarInfo('outbox/out/link'); link.type = tarfile.SYMTYPE; link.linkname = '/etc'\n"
            "    tar.addfile(link)\n")
        done = self.run_worker()
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        out = self.stage / "out"
        self.assertEqual((out / "notes.txt").read_text(), "note ***\n")
        self.assertEqual((out / "sub" / "blob.bin").read_bytes(), b"\xff\xfe ***")
        self.assertFalse(os.path.lexists(out / "link"))
        self.assertEqual(self.result()["skipped"], ["out/link"])
        self.assertEqual(self.result()["binary"], ["out/sub/blob.bin"])
        self.assertEqual(list(self.env.rglob("outbox")), [], "no outbox copy on this computer")
        self.assertEqual(list(mine.glob(".index.sqlite*")), [], "the bot's own index copy is removed")
        self.assertIn("saw ***", (mine / "worker.jsonl").read_text())
        self.assertTrue(all(p.exists() for p in host_index), "the host index is not removed by a run")
        found = subprocess.run([sys.executable, log, "find", "saw"], env=log_env, cwd=self.p.root,
                               capture_output=True, text=True)
        self.assertEqual(found.returncode, 0, found.stderr)
        self.assertIn("saw ***", found.stdout)
        for path in [*self.p.root.rglob(".index.sqlite*"), *self.env.rglob("*")]:
            if path.is_file():
                self.assertNotIn(MARKER.encode(), path.read_bytes(), path)


if __name__ == "__main__":
    unittest.main()
