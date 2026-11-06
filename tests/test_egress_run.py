"""team run with [environment] network = egress (T-0046, stage 1 of T-0029), on a fake engine that keeps networks and
containers as files: two networks per run, the proxy on both, the bot on the internal one only; the allowlist from
the config, the brief and the lead's decisions; the clean-up after a normal end, a timeout and a crash; the engine
versions egress refuses; and with bridge or none, a spec and engine calls as before."""

from __future__ import annotations

import configparser
import http.server
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import urllib.request
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_flow_parts import HEAD, team_module as flow_module  # noqa: E402
from test_team_cli import Project, team_module  # noqa: E402

MARKER = "plain-egress-marker-5521"

# A Podman or Docker: every call kept; networks, containers and their attachments as files in the state folder. A
# network with a container on it cannot be removed (as Podman refuses without -f), so the order of the clean-up shows.
FAKE = r'''#!{python}
import io, json, os, sys, tarfile
from pathlib import Path
state = Path({state!r})
args = sys.argv[1:]
with open(state / "calls.jsonl", "a") as f:
    f.write(json.dumps(args) + "\n")
def read(name, default=""):
    p = state / name
    return p.read_text() if p.exists() else default
def boxes():
    return {{p.name[4:]: json.loads(p.read_text()) for p in state.glob("box-*")}}
def label_of(argv, key):
    for i, a in enumerate(argv):
        if a == "--label" and argv[i + 1].startswith(key + "="):
            return argv[i + 1].split("=", 1)[1]
    return ""
head = args[:2]
if args == ["version"]:
    pass
elif head == ["version", "--format"]:
    print(read("docker-version", "27.3.1"))
elif head == ["info", "--format"]:
    print(read("podman-info", "netavark aardvark-dns 1.14.0"))
elif args[0] == "build":
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
elif head == ["network", "create"]:
    if (state / "fail-network").exists():
        sys.exit(1)
    (state / ("net-" + args[-1])).write_text(json.dumps(args))
elif head == ["network", "inspect"]:
    net = state / ("net-" + args[-1])
    if not net.exists():
        sys.exit(1)
    made = json.loads(net.read_text())
    print(label_of(made, "wouf.bot") + "|" + label_of(made, "wouf.task"))
elif head == ["network", "ls"]:
    team = [a.split("=", 2)[2] for a in args if a.startswith("label=wouf.team=")][0]
    for net in sorted(state.glob("net-*")):
        made = json.loads(net.read_text())
        if label_of(made, "wouf.team") == team and label_of(made, "wouf.egress"):
            print(net.name[4:])
elif head == ["network", "connect"]:
    box = state / ("box-" + args[-1])
    data = json.loads(box.read_text())
    data["nets"].append(args[2])
    box.write_text(json.dumps(data))
elif head == ["network", "rm"]:
    if any(args[-1] in b["nets"] for b in boxes().values()):
        sys.stderr.write("network is in use\n")
        sys.exit(2)
    (state / ("net-" + args[-1])).unlink(missing_ok=True)
elif args[0] == "create":
    name = args[args.index("--name") + 1]
    if "-i" in args and (state / "fail-create").exists():
        sys.stderr.write("fake create failed\n")
        sys.exit(1)
    nets = [args[i + 1] for i, a in enumerate(args) if a == "--network"]
    (state / ("box-" + name)).write_text(json.dumps({{"argv": args, "nets": nets}}))
elif args[0] == "cp" and args[1] == "-":
    (state / "inbound.tar").write_bytes(sys.stdin.buffer.read())
elif args[0] == "start" and "-ai" in args:
    (state / "start-stdin").write_bytes(sys.stdin.buffer.read())
    (state / "seen-at-start").write_text(json.dumps(sorted(p.name for p in state.glob("*-*"))))
    print("WOUF Worker · T-1 · ready-for-review")
    sys.exit(int(read("bot-exit", "0")))
elif args[0] == "start":
    (state / ("running-" + args[-1])).write_text("")
elif args[0] == "logs":
    if (state / ("box-" + args[-1])).exists():
        sys.stderr.write("egress-proxy: listening on *:3128, 2 allowlist entries\n")
        print(read("proxy-log"), end="")
    else:
        sys.exit(1)
elif head == ["ps", "-a"]:
    team = [a.split("=", 2)[2] for a in args if a.startswith("label=wouf.team=")][0]
    for name, b in sorted(boxes().items()):
        if label_of(b["argv"], "wouf.team") == team and label_of(b["argv"], "wouf.egress"):
            print(name + "|" + label_of(b["argv"], "wouf.bot") + "|" + label_of(b["argv"], "wouf.task"))
elif args[0] == "cp" and args[1].endswith(":/outbox") and args[2] == "-":
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as tar:
        data = b"done\n"
        info = tarfile.TarInfo("outbox/out/result.txt"); info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
elif args[0] == "rm":
    (state / ("box-" + args[-1])).unlink(missing_ok=True)
sys.exit(0)
'''


class EgressCase(unittest.TestCase):
    engine = "podman"
    network = "egress"

    def setUp(self) -> None:
        self.p = Project(mode="container", engine_name=self.engine, runner="alpha")
        self.addCleanup(self.p.close)
        self.p.store_secret("ALPHA_KEY", MARKER)
        self.state = Path(self.p.tmp.name) / "engine"
        self.state.mkdir()
        fake = self.p.bin / self.engine
        fake.write_text(FAKE.format(python=sys.executable, state=str(self.state)))
        fake.chmod(0o755)
        proxy = self.p.root / "tools" / "egress_proxy.py"
        proxy.parent.mkdir()
        proxy.write_text("print('the egress proxy')\n")
        self.p.set("network = bridge", f"network = {self.network}")
        with self.p.config.open("a") as handle:
            handle.write("\n[egress]\nproxy = tools/egress_proxy.py\nallow = pypi.org, .pythonhosted.org\n"
                         "needs.alpha = api.alpha.example\n")
        self.env = self.p.root / ".agents" / "team" / "env"
        self.stage = self.env / "worker" / "tasks" / "T-1"
        self.name = f"wouf-{self.p.root.name}"
        self.box = f"{self.name}-worker-T-1"
        self.nets = [f"wouf-{self.box}-in", f"wouf-{self.box}-out"]
        self.proxy = f"{self.box}-proxy"
        created = self.p.team("bot", "create", "worker")
        self.assertEqual(created.returncode, 0, created.stderr)
        (self.state / "calls.jsonl").unlink(missing_ok=True)

    def calls(self) -> list[list[str]]:
        path = self.state / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def run_worker(self, *extra: str, brief: str = "Objective: change file.txt\n") -> subprocess.CompletedProcess:
        self.p.brief.write_text(brief)
        return self.p.team("run", "worker", "T-1", "--brief", str(self.p.brief), "--repo", *extra)

    def spec(self) -> dict:
        return json.loads((self.stage / "run.json").read_text())

    def result(self) -> dict:
        return json.loads((self.stage / "result.json").read_text())

    def left(self) -> list[str]:
        return sorted(p.name for p in self.state.glob("*") if p.name.startswith(("net-", "box-")))

    def made(self, name: str) -> list[str]:
        return json.loads((self.state / name).read_text())


class TopologyTest(EgressCase):
    def test_two_networks_the_proxy_on_both_and_the_bot_on_the_internal_one_only(self) -> None:
        done = self.run_worker()
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        calls = self.calls()
        made = [c for c in calls if c[:2] == ["network", "create"]]
        self.assertEqual([c[-1] for c in made], self.nets)
        self.assertIn("--internal", made[0])
        self.assertNotIn("--internal", made[1])
        for call in made:
            self.assertIn(f"wouf.egress={self.box}", call)
            self.assertIn("wouf.task=T-1", call)
        proxy = next(c for c in calls if c[0] == "create" and self.proxy in c)
        self.assertEqual(proxy[proxy.index("--network") + 1], self.nets[0])
        self.assertEqual(proxy[proxy.index("--network-alias") + 1], "proxy")
        self.assertIn("--read-only", proxy)
        self.assertEqual([proxy[i + 1] for i, a in enumerate(proxy) if a == "--allow"],
                         ["pypi.org", ".pythonhosted.org", "api.alpha.example"])
        self.assertIn("print('the egress proxy')\n", proxy)
        self.assertIn(["network", "connect", self.nets[1], self.proxy], calls)
        bot = next(c for c in calls if c[0] == "create" and "-i" in c)
        self.assertEqual([bot[i + 1] for i, a in enumerate(bot) if a == "--network"], [self.nets[0]])
        # The proxy runs before the bot starts; the bot's script sends everything through it.
        seen = json.loads((self.state / "seen-at-start").read_text())
        self.assertIn(f"running-{self.proxy}", seen)
        for name in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
            self.assertIn(f"{name}=http://proxy:3128", bot)
            self.assertEqual(bot[bot.index(f"{name}=http://proxy:3128") - 1], "-e")
        self.assertEqual(self.spec()["network"], self.nets[0])

    def test_keys_reach_the_bot_as_today_and_never_the_proxy(self) -> None:
        self.run_worker()
        self.assertIn(f"ALPHA_KEY={MARKER}", (self.state / "start-stdin").read_text().split("\n"))
        for call in self.calls():
            self.assertNotIn(MARKER, json.dumps(call))

    def test_the_runners_needs_join_its_list_and_no_lack_is_reported(self) -> None:
        done = self.run_worker()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("the list lacks", done.stderr)
        self.assertNotIn("has no model hosts", done.stderr)


SIX = ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "NO_PROXY", "no_proxy")
LOOPBACK = "localhost,127.0.0.1,::1"


def box_env(bot: list[str]) -> dict:
    """The -e pairs of the bot's create argv."""
    return dict(bot[i + 1].split("=", 1) for i, a in enumerate(bot) if a == "-e")


class Recorder(http.server.ThreadingHTTPServer):
    """A local HTTP server that keeps the request line of every request and answers with its own name."""

    def __init__(self, name: str) -> None:
        self.name, self.seen = name, []
        recorder = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                recorder.seen.append((self.path, self.headers.get("Host")))
                body = recorder.name.encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                pass

        super().__init__(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.shutdown()
        self.server_close()


class LoopbackTest(EgressCase):
    """T-0066: a bot's call to its own 127.0.0.1 went to the proxy and was refused as an IP literal (403)."""

    def bot(self, brief: str = "Objective: change file.txt\n") -> list[str]:
        done = self.run_worker(brief=brief)
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        return next(c for c in self.calls() if c[0] == "create" and "-i" in c)

    def test_the_bot_gets_no_proxy_for_its_loopback_only_next_to_the_four_proxy_variables(self) -> None:
        bot = self.bot()
        env = box_env(bot)
        self.assertEqual({k: v for k, v in env.items() if k.lower().endswith("_proxy")},
                         {**{k: "http://proxy:3128" for k in SIX[:4]}, "NO_PROXY": LOOPBACK, "no_proxy": LOOPBACK})
        for name in ("NO_PROXY", "no_proxy"):
            self.assertEqual(bot[bot.index(f"{name}={LOOPBACK}") - 1], "-e")

    def test_the_loopback_exception_is_fixed_in_code_and_does_not_widen(self) -> None:
        # Hosts in the config, the brief's Egress line and a no_proxy key in the config change nothing.
        team_module()
        env = sys.modules["teamlib.engine"].EGRESS_ENV
        self.assertEqual((env["NO_PROXY"], env["no_proxy"]), (LOOPBACK, LOOPBACK))
        with self.p.config.open("a") as handle:
            handle.write("no_proxy = .example.org\nno-proxy = *\n")
        bot = self.bot(brief="Egress: pypi.org\nNo-Proxy: *\nObjective: change file.txt\n")
        env = box_env(bot)
        self.assertEqual((env["NO_PROXY"], env["no_proxy"]), (LOOPBACK, LOOPBACK))
        self.assertEqual(sum(a.lower().startswith("no_proxy=") for a in bot), 2)

    def test_urllib_reaches_loopback_directly_and_an_outside_host_through_the_proxy(self) -> None:
        env = box_env(self.bot())
        proxy, direct = Recorder("proxy"), Recorder("direct")
        self.addCleanup(proxy.close)
        self.addCleanup(direct.close)
        # The bot's six variables, with the proxy's address replaced by the local fake; nothing else of the host's.
        local = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy") and k != "REQUEST_METHOD"}
        for name in SIX:
            local[name] = env[name].replace("http://proxy:3128", f"http://127.0.0.1:{proxy.server_port}")
        port = direct.server_port
        with mock.patch.dict(os.environ, local, clear=True):
            opener = urllib.request.build_opener()
            for host in ("127.0.0.1", "localhost"):
                with opener.open(f"http://{host}:{port}/own", timeout=10) as answer:
                    self.assertEqual(answer.read(), b"direct", host)
            for host in ("pypi.org", "localhost.example.org", "127.0.0.1.example.org"):
                with opener.open(f"http://{host}/out", timeout=10) as answer:
                    self.assertEqual(answer.read(), b"proxy", host)
        self.assertEqual(direct.seen, [("/own", f"127.0.0.1:{port}"), ("/own", f"localhost:{port}")])
        self.assertEqual(proxy.seen, [("http://pypi.org/out", "pypi.org"),
                                      ("http://localhost.example.org/out", "localhost.example.org"),
                                      ("http://127.0.0.1.example.org/out", "127.0.0.1.example.org")])


class CleanUpTest(EgressCase):
    def test_a_normal_end_removes_the_networks_and_the_proxy_before_the_result(self) -> None:
        self.assertEqual(self.run_worker().returncode, 0)
        self.assertEqual(self.left(), [])
        self.assertEqual(self.result()["state"], "done")
        calls = self.calls()
        # Removed before the result was written: the last network rm comes before nothing but the receipt.
        self.assertLess(calls.index(["rm", "-f", self.proxy]), calls.index(["network", "rm", self.nets[0]]))

    def test_a_timeout_removes_them(self) -> None:
        (self.state / "bot-exit").write_text("124")
        done = self.run_worker()
        self.assertEqual(done.returncode, 124, done.stderr)
        self.assertEqual(self.result()["state"], "timeout")
        self.assertEqual(self.left(), [])

    def test_a_crash_removes_them_and_the_bots_container(self) -> None:
        (self.state / "fail-create").write_text("")
        done = self.run_worker()
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(self.result()["state"], "failed")
        self.assertEqual(self.left(), [])

    def test_a_failed_network_create_leaves_nothing(self) -> None:
        (self.state / "fail-network").write_text("")
        done = self.run_worker()
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(self.result()["state"], "failed")
        self.assertEqual(self.left(), [])

    def test_what_a_killed_supervisor_left_is_removed_by_label_a_running_runs_is_kept(self) -> None:
        def leftover(member: str, task: str, pid: int) -> None:
            box = f"{self.name}-{member}-{task}"
            tag = ["--label", f"wouf.team={self.name}", "--label", f"wouf.bot={member}", "--label",
                   f"wouf.task={task}", "--label", f"wouf.egress={box}"]
            net = f"wouf-{box}-in"
            (self.state / f"net-{net}").write_text(json.dumps(["network", "create", *tag, net]))
            (self.state / f"box-{box}-proxy").write_text(json.dumps({"argv": ["create", *tag], "nets": [net]}))
            stage = self.env / member / "tasks" / task
            stage.mkdir(parents=True)
            (stage / "result.json").write_text(json.dumps({"state": "running", "pid": pid}))
        live = subprocess.Popen(["sleep", "300"])
        self.addCleanup(live.wait)
        self.addCleanup(live.kill)
        leftover("worker", "T-dead", 2 ** 22 + 7)
        leftover("review", "T-live", live.pid)
        self.assertEqual(self.run_worker().returncode, 0)
        self.assertEqual(self.left(), [f"box-{self.name}-review-T-live-proxy", f"net-wouf-{self.name}-review-T-live-in"])

    def test_doctor_clean_keeps_live_objects_and_reports_removed_objects(self) -> None:
        tag = ["--label", f"wouf.team={self.name}", "--label", "wouf.bot=worker", "--label",
               "wouf.task=T-dead", "--label", "wouf.egress=dead"]
        (self.state / "net-dead").write_text(json.dumps(["network", "create", *tag, "dead"]))
        (self.state / "box-dead-proxy").write_text(json.dumps({"argv": ["create", *tag], "nets": ["dead"]}))
        live_tag = [v.replace("T-dead", "T-live") for v in tag]
        (self.state / "net-live").write_text(json.dumps(["network", "create", *live_tag, "live"]))
        (self.state / "box-live-proxy").write_text(json.dumps({"argv": ["create", *live_tag], "nets": ["live"]}))
        live = subprocess.Popen(["sleep", "300"])
        self.addCleanup(live.wait)
        self.addCleanup(live.kill)
        stage = self.env / "worker" / "tasks" / "T-live"
        stage.mkdir(parents=True)
        (stage / "result.json").write_text(json.dumps({"state": "running", "pid": live.pid}))
        before = self.left()
        plain = self.p.team("doctor")
        self.assertEqual(plain.returncode, 0, plain.stderr)
        self.assertIn("all good", plain.stdout)
        self.assertEqual(self.left(), before)
        self.assertFalse(any(c[0] == "rm" or c[:2] == ["network", "rm"] for c in self.calls()))
        refused = self.p.team("doctor", "--clean", "--unknown")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("unknown", refused.stderr)
        self.assertEqual(self.left(), before)
        cleaned = self.p.team("doctor", "--clean")
        self.assertEqual(cleaned.returncode, plain.returncode, cleaned.stderr)
        self.assertIn("removed dead-proxy", cleaned.stdout)
        self.assertIn("removed dead", cleaned.stdout)
        self.assertEqual(self.left(), ["box-live-proxy", "net-live"])
        again = self.p.team("doctor", "--clean")
        self.assertEqual(again.returncode, plain.returncode, again.stderr)
        self.assertIn("egress: nothing left to clean", again.stdout)
        self.assertEqual(self.left(), ["box-live-proxy", "net-live"])

    def test_help_names_the_run_egress_option(self) -> None:
        help_text = self.p.team("--help")
        self.assertEqual(help_text.returncode, 0, help_text.stderr)
        run_usage = help_text.stdout.split("team run ID TASK", 1)[1].split("team result", 1)[0]
        self.assertIn("[--egress HOSTS|none]", run_usage)

    def test_the_clean_up_without_a_run(self) -> None:
        # What team doctor --clean would call: the same sweep, by label.
        team_module()
        engine = sys.modules["teamlib.engine"]
        tag = ["--label", f"wouf.team={self.name}", "--label", "wouf.bot=worker", "--label", "wouf.task=T-9",
               "--label", "wouf.egress=x"]
        (self.state / "net-wouf-x-in").write_text(json.dumps(["network", "create", *tag, "wouf-x-in"]))
        with mock.patch.dict(os.environ, {"PATH": f"{self.p.bin}{os.pathsep}{os.environ['PATH']}"}):
            gone = engine.egress_clean({"engine": self.engine, "docker_host": ""}, self.name)
        self.assertEqual(gone, ["wouf-x-in"])
        self.assertEqual(self.left(), [])


class ProxyLogTest(EgressCase):
    def test_the_proxy_log_comes_back_and_team_result_shows_it_masked(self) -> None:
        (self.state / "proxy-log").write_text(
            "2026-11-02T10:15:04Z host=pypi.org port=443 result=allowed reason=ok bytes_out=1 bytes_in=2\n"
            f"2026-11-02T10:15:05Z host={MARKER} port=443 result=refused reason=not-allowed bytes_out=0 bytes_in=0\n")
        done = self.run_worker()
        self.assertEqual(done.returncode, 0, done.stderr)
        log = (self.stage / "egress.log").read_text()
        self.assertIn("host=pypi.org port=443 result=allowed", log)
        self.assertNotIn(MARKER, log)
        shown = self.p.team("result", "worker", "T-1")
        self.assertIn("egress log of T-1: 3 lines, 1 refused", shown.stdout)
        self.assertIn("host=pypi.org port=443 result=allowed", shown.stdout)
        self.assertNotIn(MARKER, shown.stdout + shown.stderr)


class AllowlistTest(EgressCase):
    def proxy_allow(self) -> list[str]:
        proxy = next(c for c in self.calls() if c[0] == "create" and self.proxy in c)
        return [proxy[i + 1] for i, a in enumerate(proxy) if a == "--allow"]

    def test_a_brief_host_in_the_base_list_needs_no_decision(self) -> None:
        done = self.run_worker(brief="Egress: files.pythonhosted.org, pypi.org\nObjective: x\n")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.proxy_allow(),
                         ["pypi.org", ".pythonhosted.org", "api.alpha.example", "files.pythonhosted.org"])

    def test_a_host_outside_the_base_list_without_a_lead_decision_refuses_the_run(self) -> None:
        done = self.run_worker(brief="Egress: registry.npmjs.org\nObjective: x\n")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("registry.npmjs.org is not in [egress] allow and has no lead decision for T-1", done.stderr)
        self.assertEqual(self.calls(), [["version"]], "refused before the engine is asked for more than its version")
        self.assertFalse((self.stage / "run.json").exists())

    def test_a_recorded_lead_decision_lets_the_host_through_for_that_task_only(self) -> None:
        added = self.p.team("decision", "add", "Egress: registry.npmjs.org", "--task", "T-1", "--authority", "lead",
                            "--options", "yes", "no", "--why", "npm install", "--risk", "uploads")
        self.assertEqual(added.returncode, 0, added.stderr)
        done = self.run_worker(brief="Egress: registry.npmjs.org\nObjective: x\n")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("registry.npmjs.org", self.proxy_allow())
        other = self.p.team("run", "worker", "T-2", "--brief", str(self.p.brief), "--repo")
        self.assertIn("has no lead decision for T-2", other.stderr)

    def test_a_decide_bots_decision_or_a_pending_one_is_not_the_leads_yes(self) -> None:
        for authority in ("decide", "owner"):
            added = self.p.team("decision", "add", "Egress: registry.npmjs.org", "--task", "T-1", "--authority",
                                authority, "--options", "yes", "no", "--why", "w", "--risk", "r")
            self.assertEqual(added.returncode, 0, added.stderr)
        done = self.run_worker(brief="Egress: registry.npmjs.org\nObjective: x\n")
        self.assertIn("has no lead decision for T-1", done.stderr)

    def test_review_and_decide_get_allow_and_their_runners_needs_no_brief_hosts(self) -> None:
        self.p.brief.write_text("Egress: registry.npmjs.org\nObjective: x\n")
        for member in ("review", "decide"):
            with self.subTest(member=member):
                self.p.team("bot", "create", member)
                (self.state / "calls.jsonl").unlink(missing_ok=True)
                done = self.p.team("run", member, "T-1.r1", "--brief", str(self.p.brief), "--repo")
                self.assertNotIn("has no lead decision", done.stderr)
                proxy = next(c for c in self.calls() if c[0] == "create" and "--allow" in c)
                self.assertEqual([proxy[i + 1] for i, a in enumerate(proxy) if a == "--allow"],
                                 ["pypi.org", ".pythonhosted.org", "api.alpha.example"])

    def test_egress_given_by_the_flow_replaces_the_brief_line(self) -> None:
        done = self.run_worker("--egress", "none", brief="Egress: registry.npmjs.org\nObjective: x\n")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.proxy_allow(), ["pypi.org", ".pythonhosted.org", "api.alpha.example"])

    def test_entries_that_are_not_host_names_are_refused(self) -> None:
        for bad in ("203.0.113.7", "pypi.org:443", "*.pypi.org", "https://pypi.org", "a..b", "0x7f.1"):
            with self.subTest(bad=bad):
                done = self.run_worker(brief=f"Egress: {bad}\nObjective: x\n")
                self.assertNotEqual(done.returncode, 0)
                self.assertIn("is not a host name or .suffix", done.stderr)


class RunnerNeedsTest(EgressCase):
    """[egress] needs.<runner> (or its CLI's built-in list) joins the runs of that runner only (T-0062)."""

    def setUp(self) -> None:
        super().setUp()
        with self.p.config.open("a") as handle:
            handle.write("\n[runner.claude]\ncommand = claude -p {prompt}\nhost_command = claude -p {prompt}\n"
                         "auth = ALPHA_KEY\n\n[runner.codex]\ncommand = codex exec {prompt}\n"
                         "host_command = codex exec {prompt}\nauth = ALPHA_KEY\n")

    def use(self, runner: str) -> None:
        bot = self.p.bot_file("worker")
        bot.write_text(re.sub(r"(?m)^runner: .*$", f"runner: {runner}", bot.read_text()))

    def allow_of(self, runner: str) -> list[str]:
        self.use(runner)
        (self.state / "calls.jsonl").unlink(missing_ok=True)
        done = self.run_worker()
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        self.assertNotIn("the list lacks", done.stderr)
        proxy = next(c for c in self.calls() if c[0] == "create" and self.proxy in c)
        allow = [proxy[i + 1] for i, a in enumerate(proxy) if a == "--allow"]
        self.assertIn(f"team: egress: the list of this run ({runner}): {' '.join(allow)}\n", done.stderr)
        self.assertEqual(self.spec()["egress"]["allow"], allow)
        return allow

    def test_a_claude_run_and_a_codex_run_on_one_config_each_get_their_own_hosts(self) -> None:
        claude, codex = self.allow_of("claude"), self.allow_of("codex")
        self.assertIn("api.anthropic.com", claude)
        self.assertNotIn("chatgpt.com", claude)
        self.assertIn("chatgpt.com", codex)
        self.assertNotIn("api.anthropic.com", codex)
        self.assertEqual(claude[:2], codex[:2], "[egress] allow is the base of both")

    def test_needs_of_a_runner_replace_its_built_in_list(self) -> None:
        self.p.set("needs.alpha = api.alpha.example\n", "needs.alpha = api.alpha.example\nneeds.codex = chatgpt.com\n")
        self.assertEqual(self.allow_of("codex"), ["pypi.org", ".pythonhosted.org", "chatgpt.com"])

    def test_a_runner_without_model_hosts_says_so_once_at_start(self) -> None:
        self.use("beta")
        done = self.run_worker()
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        self.assertEqual(done.stderr.count("beta has no model hosts"), 1, done.stderr)
        self.assertIn("set [egress] needs.beta", done.stderr)
        self.assertNotIn("the list lacks", done.stderr)

    def test_a_needs_value_that_is_not_a_host_stops_the_run_and_names_the_key(self) -> None:
        for bad in ("203.0.113.7", "api.alpha.example:443", "*.alpha.example", "https://api.alpha.example"):
            with self.subTest(bad=bad):
                self.p.set("needs.alpha = api.alpha.example\n", f"needs.alpha = {bad}\n")
                done = self.run_worker()
                self.assertEqual(done.returncode, 2, done.stderr)
                self.assertIn("[egress] needs.alpha", done.stderr)
                self.assertIn("is not a host name or .suffix", done.stderr)
                self.p.set(f"needs.alpha = {bad}\n", "needs.alpha = api.alpha.example\n")


class RefusalTest(EgressCase):
    def test_host_mode_refuses_egress(self) -> None:
        self.p.set("mode = container", "mode = host")
        done = self.run_worker()
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("network = egress needs mode = container", done.stderr)

    def test_an_old_aardvark_dns_or_cni_is_refused(self) -> None:
        for info in ("netavark aardvark-dns 1.10.1", "cni ", "netavark ", "netavark aardvark-dns 1.11.0-dev"):
            with self.subTest(info=info):
                (self.state / "podman-info").write_text(info)
                done = self.run_worker()
                self.assertNotEqual(done.returncode, 0)
                self.assertIn("needs Podman with netavark and aardvark-dns 1.11.0 or newer", done.stderr)
                self.assertFalse(any(c[:2] == ["network", "create"] for c in self.calls()))
        (self.state / "podman-info").write_text("netavark aardvark-dns 1.11.0")
        self.assertEqual(self.run_worker().returncode, 0)

    def test_a_proxy_that_is_not_there_is_refused(self) -> None:
        (self.p.root / "tools" / "egress_proxy.py").unlink()
        done = self.run_worker()
        self.assertIn("network = egress needs the egress proxy", done.stderr)


class DockerTest(EgressCase):
    engine = "docker"

    def test_a_docker_without_the_cve_2024_29018_fix_is_refused(self) -> None:
        for version in ("24.0.9", "25.0.4", "23.0.10", "26.0.0-rc2", "20.10.24", ""):
            with self.subTest(version=version):
                (self.state / "docker-version").write_text(version)
                done = self.run_worker()
                self.assertNotEqual(done.returncode, 0)
                self.assertIn("CVE-2024-29018", done.stderr)
        (self.state / "docker-version").write_text("25.0.5")
        done = self.run_worker()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.left(), [])


class VersionTest(unittest.TestCase):
    def test_moby_versions(self) -> None:
        team_module()
        engine = sys.modules["teamlib.engine"]
        fixed = ("23.0.11", "23.0.15", "25.0.5", "25.0.8", "26.0.0-rc3", "26.0.0", "26.1.4", "27.3.1", "28.0.0")
        open_ = ("23.0.10", "24.0.9", "25.0.4", "26.0.0-rc2", "26.0.0-beta.1", "25.0.5-rc1", "20.10.24")
        for text in fixed:
            self.assertTrue(engine.moby_fixed(engine.version_of(text)), text)
        for text in open_:
            self.assertFalse(engine.moby_fixed(engine.version_of(text)), text)


class BridgeTest(EgressCase):
    """With bridge or none nothing of egress shows: the same spec keys and values, no network or proxy call."""
    network = "bridge"
    KEYS = {"mode", "member", "task", "name", "lead", "role", "runner", "tier", "model", "effort", "stage", "script",
            "argv", "binary_inputs", "limit", "repo", "branch", "commit", "repo_dir", "limit_patterns", "container",
            "log_dir", "nonce", "usage", "per_run", "staged", "build", "where", "image", "recipe", "image_labels",
            "storage", "volume_labels", "run_labels", "network", "cpus", "memory", "pack"}

    def check(self, network: str) -> None:
        done = self.run_worker(brief="Egress: registry.npmjs.org\nObjective: change file.txt\n")
        self.assertEqual(done.returncode, 0, done.stderr)
        spec = self.spec()
        self.assertEqual(set(spec), self.KEYS)
        self.assertEqual(spec["network"], network)
        self.assertNotIn("PROXY", json.dumps(self.calls()))
        bot = next(c for c in self.calls() if c[0] == "create")
        for name in SIX:
            self.assertFalse(any(a.startswith(f"{name}=") for a in bot), name)
        self.assertEqual([c[0] for c in self.calls()],
                         ["version", "rm", "image", "volume", "create", "cp", "start", "cp", "rm"])
        bot = next(c for c in self.calls() if c[0] == "create")
        self.assertEqual(bot[bot.index("--network") + 1], network)
        self.assertFalse((self.stage / "egress.log").exists())
        self.assertNotIn("egress", self.p.team("result", "worker", "T-1").stdout)

    def test_bridge(self) -> None:
        self.check("bridge")

    def test_none(self) -> None:
        self.p.set("network = bridge", "network = none")
        self.check("none")


class FlowEgressTest(unittest.TestCase):
    """team flow passes the task's Egress: line to every step as --egress; review and decide get the base list."""

    def setUp(self) -> None:
        self.flow = module = flow_module()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        (self.folder / "brief.md").write_text("Task: T-1\nEgress: pypi.org, registry.npmjs.org\nObjective: x\n")
        self.argv: list = []
        module.record = lambda *a, **k: None
        module.head_of = lambda flow: HEAD
        module.subprocess = types.SimpleNamespace(run=lambda argv, **k: self.argv.append(argv),
                                                  STDOUT=subprocess.STDOUT)
        self.ini = configparser.ConfigParser(interpolation=None)
        self.ini.read_string("[environment]\nnetwork = egress\n")
        self.state = {"task": "T-1", "class": "standard", "branch": "b", "repo": str(self.folder), "base": "main",
                      "steps": []}
        self.opts = {"bot": "worker", "runner": None, "tier": "strong", "effort": None, "model": None}

    def egress_of(self, act: dict) -> str:
        self.argv.clear()
        self.flow.flow_step(self.ini, self.state, act, self.folder, self.opts)
        argv = self.argv[0]
        return argv[argv.index("--egress") + 1]

    def test_each_step_gets_the_tasks_list_and_review_and_decide_the_base_list(self) -> None:
        self.assertEqual(self.egress_of({"do": "worker"}), "pypi.org, registry.npmjs.org")
        self.assertEqual(self.egress_of({"do": "scout"}), "pypi.org, registry.npmjs.org")
        self.assertEqual(self.egress_of({"do": "review"}), "none")
        self.assertEqual(self.egress_of({"do": "worker", "bot": "decide"}), "none")

    def test_bridge_passes_nothing(self) -> None:
        self.ini.set("environment", "network", "bridge")
        self.flow.flow_step(self.ini, self.state, {"do": "worker"}, self.folder, self.opts)
        self.assertNotIn("--egress", self.argv[0])


if __name__ == "__main__":
    unittest.main()
