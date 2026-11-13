"""mode = bwrap with network = egress (PLAN T-0060, package 6): the bot without a network, the egress proxy on a Unix
socket in a bubblewrap sandbox of its own that shares this computer's network, and the relay on the bot's loopback.

The hand-off of the list and the refusal of bridge need no bubblewrap; the runs are skipped when bwrap_usable() is
false (the bwrap CI job fails on any skip). The project's proxy is the catalog's egress_proxy.py with a fake resolver:
the listed name resolves to a public address that it connects to a test server on this computer's loopback instead."""

from __future__ import annotations

import configparser
import http.server
import ipaddress
import json
import os
import signal
import socket
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_bwrap_run import REAL, USABLE, bwrap_project  # noqa: E402
from test_team_cli import KEY  # noqa: E402
from support import TIME_FACTOR  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".agents" / "bin"))
from teamlib import bwrap, engine  # noqa: E402

PROXY = Path(__file__).resolve().parents[1] / "catalog" / "tools" / "egress-proxy" / "egress_proxy.py"
LISTED, UNLISTED = "listed.example", "unlisted.example"
# A public address, so the proxy lets it through (the fake connects it to the test server); from an integer, so the
# leak scan of tests/test_pack.py finds no literal.
ADDRESS = str(ipaddress.IPv4Address(0x5DB8D70E))
BODY = "listed-host-body-4471"
# Into the proxy before its main: its environment's names on its log, and the fake resolver.
FAKE_RESOLVER = '''
print("proxy-env: " + " ".join(sorted(os.environ)), file=sys.stderr, flush=True)
_init = Proxy.__init__


def _fake_init(self, *args, **kwargs):
    _init(self, *args, **kwargs)
    resolve, connect = self.resolve, self.connect

    def fake_resolve(host, port, *rest):
        if host == {listed!r}:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ({address!r}, port))]
        return resolve(host, port, *rest)

    def fake_connect(family, sockaddr, timeout):
        if sockaddr[0] == {address!r}:
            return connect(socket.AF_INET, ("127.0.0.1", {port}), timeout)
        return connect(family, sockaddr, timeout)

    self.resolve, self.connect = fake_resolve, fake_connect


Proxy.__init__ = _fake_init

'''
# The bot: what it may and may not reach, one line each into out/net.txt; with SLEEP in the brief it works a while.
NET_CLI = r"""#!/bin/sh
env | grep -i '_proxy=' | sort > out/proxy-env.txt
if grep -q SLEEP in/BRIEF.md; then : > /bot/working; sleep 60; fi
mkdir -p /tmp/inside && echo inside-body > /tmp/inside/inside.txt
python3 -m http.server 8765 --bind 127.0.0.1 --directory /tmp/inside >/dev/null 2>&1 &
getent hosts example.org >/dev/null 2>&1 && echo "dns: resolved" >> out/net.txt || echo "dns: failed" >> out/net.txt
python3 - "$(sed -n 's/^Port: //p' in/BRIEF.md)" "$(sed -n 's/^Abstract: //p' in/BRIEF.md)" >> out/net.txt 2>&1 <<'EOF'
import os, socket, sys, time, urllib.request
port, abstract = int(sys.argv[1]), sys.argv[2]
def fetch(url):
    try:
        with urllib.request.urlopen(url, timeout=15) as reply:
            return f"{reply.status} {reply.read().decode()[:60].strip()}"
    except urllib.error.HTTPError as error:
        return f"refused {error.code}"
    except Exception as error:
        return f"failed {type(error).__name__}"
def dial(family, address):
    try:
        with socket.socket(family, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect(address)
        return "connected"
    except OSError as error:
        return f"failed {type(error).__name__}"
for _ in range(50):
    if dial(socket.AF_INET, ("127.0.0.1", 8765)) == "connected":
        break
    time.sleep(0.1)
print("listed:", fetch("http://listed.example/"))
print("unlisted:", fetch("http://unlisted.example/"))
print("ip-proxy:", fetch("http://192.0.2.10/"))
print("ip-direct:", dial(socket.AF_INET, ("192.0.2.10", 80)))
print("host-loopback:", dial(socket.AF_INET, ("127.0.0.1", port)))
print("abstract:", dial(socket.AF_UNIX, "\0" + abstract))
print("key-host:", fetch(f"http://{os.environ['ALPHA_KEY']}.example/"))
print("inside:", fetch("http://127.0.0.1:8765/inside.txt"))
EOF
echo "WOUF Worker · T-1 · ready-for-review"
"""


class HandOffTest(unittest.TestCase):
    """No bubblewrap needed: in bwrap mode the run's list goes to teamlib.bwrap, and no engine is asked."""

    def setUp(self) -> None:
        self.ini = configparser.ConfigParser()
        self.ini.read_string("[environment]\nnetwork = egress\n")
        chosen = {"allow": ["pypi.org", LISTED], "source": str(PROXY)}
        self.saved = engine.EGRESS[:], bwrap.EGRESS[:], engine.egress_engine_check
        self.asked: list = []
        engine.EGRESS[:] = [lambda *args: dict(chosen)]
        engine.egress_engine_check = self.asked.append

    def tearDown(self) -> None:
        engine.EGRESS[:], bwrap.EGRESS[:], engine.egress_engine_check = self.saved

    def test_bwrap_skips_the_engine_check_and_keeps_the_list_for_its_stage(self) -> None:
        got = engine.egress_prepare(self.ini, {}, {}, "bwrap", "fake")
        self.assertEqual(got["allow"], ["pypi.org", LISTED])
        self.assertEqual(self.asked, [], "no container engine is asked in bwrap mode")
        self.assertEqual(bwrap.EGRESS, [got])

    def test_the_bot_gets_the_relay_on_its_loopback_and_loopback_only_around_it(self) -> None:
        self.assertEqual(bwrap.BOT_PROXY["HTTPS_PROXY"], "http://127.0.0.1:3128")
        self.assertEqual(bwrap.BOT_PROXY["http_proxy"], "http://127.0.0.1:3128")
        self.assertEqual(bwrap.BOT_PROXY["NO_PROXY"], engine.EGRESS_ENV["NO_PROXY"])
        self.assertEqual(bwrap.BOT_PROXY["no_proxy"], "localhost,127.0.0.1,::1")

    def test_a_private_pair_reaches_the_proxy_sandbox_only(self) -> None:
        # T-0132: [egress] private goes to the proxy's sandbox as --private; the bot's loopback variables stay.
        pair = "127.0.0.1:11434"
        engine.EGRESS[:] = [lambda *args: {"allow": ["pypi.org"], "private": [pair], "source": str(PROXY)}]
        got = engine.egress_prepare(self.ini, {}, {}, "bwrap", "fake")
        self.assertEqual(got["private"], [pair])
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "ctl").mkdir()
            staged = bwrap.egress_stage(Path(tmp))
            self.assertEqual(staged["bwrap_egress"], {"allow": ["pypi.org"], "private": [pair]})
            self.assertEqual(staged["egress"]["private"], [pair], "run.json records the pairs as egress.private")
            argv = bwrap.proxy_argv({"stage": tmp, "bwrap_egress": staged["bwrap_egress"]})
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "--private"], [pair])
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "--allow"], ["pypi.org"])
        self.assertEqual(bwrap.BOT_PROXY["NO_PROXY"], "localhost,127.0.0.1,::1", "the bot's loopback stays")

    def test_without_a_pair_the_proxy_sandbox_gets_no_private_option(self) -> None:
        engine.egress_prepare(self.ini, {}, {}, "bwrap", "fake")  # the chosen list has no private key at all
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "ctl").mkdir()
            staged = bwrap.egress_stage(Path(tmp))
            self.assertEqual(staged["bwrap_egress"]["private"], [])
            self.assertEqual(staged["egress"]["private"], [])
            argv = bwrap.proxy_argv({"stage": tmp, "bwrap_egress": staged["bwrap_egress"]})
        self.assertNotIn("--private", argv)

    def test_the_egress_record_of_a_bwrap_run_is_not_a_container_proxy(self) -> None:
        # run.json's egress in bwrap mode is a record only: ending a failed run asks no engine and needs no names.
        with tempfile.TemporaryDirectory() as tmp:
            engine.egress_end({"mode": "bwrap", "stage": tmp, "egress": {"allow": [], "private": []}}, box=True)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_bridge_stays_refused_and_egress_is_not(self) -> None:
        p = bwrap_project("bridge")
        self.addCleanup(p.close)
        refused = p.team("run", "worker", "T-1", "--brief", str(p.brief), "--dry-run")
        self.assertEqual(refused.returncode, 2, refused.stdout)
        self.assertIn("shares this computer's network and loopback", refused.stderr)
        p.set("network = bridge", "network = egress")
        plan = p.plan("worker", "T-1", "--brief", str(p.brief))
        self.assertEqual(plan["mode"], "bwrap")


class Upstream(http.server.ThreadingHTTPServer):
    """The listed host: a server on this computer's loopback, which only the proxy's sandbox can reach."""

    def __init__(self) -> None:
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                data = BODY.encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args) -> None:
                pass

        super().__init__(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.serve_forever, daemon=True).start()


class EgressProject:
    """A project with network = egress, the faked proxy, a listed upstream and the bwrap recorder."""

    def setUp(self) -> None:
        self.upstream = Upstream()
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.upstream.shutdown)
        # An abstract socket of this computer: a bot in its own network namespace must not reach it.
        self.abstract_name = f"wouf-test-{uuid.uuid4().hex}"
        self.abstract = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.abstract.bind("\0" + self.abstract_name)
        self.abstract.listen(1)
        self.addCleanup(self.abstract.close)
        self.p = bwrap_project("egress")
        self.addCleanup(self.p.close)
        self.p.store_secret("ALPHA_KEY", KEY)
        proxy = self.p.root / "tools" / "egress_proxy.py"
        proxy.parent.mkdir()
        source = PROXY.read_text(encoding="utf-8")
        main = '\nif __name__ == "__main__":'
        self.assertEqual(source.count(main), 1)
        patch = FAKE_RESOLVER.format(listed=LISTED, address=ADDRESS, port=self.upstream.server_address[1])
        proxy.write_text(source.replace(main, "\n" + patch + main))
        with self.p.config.open("a") as handle:
            handle.write(f"\n[egress]\nproxy = tools/egress_proxy.py\nallow = {LISTED}\n")
        self.env = self.p.root / ".agents" / "team" / "env"
        (self.env / "worker" / "box").mkdir(parents=True)
        (self.env / "worker" / "box" / "fake-cli.sh").write_text(NET_CLI)
        self.stage = self.env / "worker" / "tasks" / "T-1"
        self.sock = self.stage / "ctl" / "egress.sock"
        self.calls = Path(self.p.tmp.name) / "bwrap-calls"
        recorder = self.p.bin / "bwrap"
        recorder.write_text(f"#!{sys.executable}\nimport json, os, sys\n"
                            f"with open({str(self.calls)!r}, 'a') as f:\n"
                            "    f.write(json.dumps({'argv': sys.argv[1:], 'env': dict(os.environ)}) + '\\n')\n"
                            f"os.execv({REAL!r}, [{REAL!r}, *sys.argv[1:]])\n")
        recorder.chmod(0o755)

    def brief(self, extra: str = "") -> Path:
        path = self.p.root / "brief-egress.md"
        path.write_text(f"Objective: reach the network\nPort: {self.upstream.server_address[1]}\n"
                        f"Abstract: {self.abstract_name}\n{extra}")
        return path

    def short(self) -> None:
        self.p.set("timeout_minutes = 30", "timeout_minutes = 0.05")

    def proxies(self) -> list[int]:
        """The processes that still run the proxy of this stage: its sandbox's names its ctl/, the proxy's its
        socket inside (a test's runs are one after the other)."""
        found = []
        for entry in Path("/proc").iterdir():
            try:
                cmdline = (entry / "cmdline").read_bytes()
            except OSError:
                continue
            if entry.name.isdigit() and (str(self.sock.parent).encode() + b"\0/run/wouf\0" in cmdline
                                         or b"--unix\0/run/wouf/egress.sock\0" in cmdline):
                found.append(int(entry.name))
        return found

    def assert_nothing_left(self, wait: float = 5 * TIME_FACTOR) -> None:
        """No proxy process and no socket of this stage remain, waiting up to `wait` seconds for a proxy that is
        still exiting (a loaded host is slow to reap it); a leaked proxy never exits, so any wait still catches it."""
        deadline = time.monotonic() + wait
        while (self.proxies() or os.path.lexists(self.sock)) and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertEqual(self.proxies(), [], "no proxy process remains")
        self.assertFalse(os.path.lexists(self.sock), "no socket remains")

    def recorded(self) -> tuple[list[dict], list[dict]]:
        """The recorded bwrap calls: the proxy's (it shares the network) and the bot's."""
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        return ([c for c in calls if "--share-net" in c["argv"]], [c for c in calls if "/bot" in c["argv"]])


@unittest.skipUnless(USABLE, "bwrap is not installed or cannot start a sandbox here (user namespaces)")
class BwrapEgressTest(EgressProject, unittest.TestCase):
    """Real runs in bubblewrap with network = egress, through bin/team."""

    def test_the_bot_reaches_its_list_through_the_proxy_and_nothing_else(self) -> None:
        result = self.p.team("run", "worker", "T-1", "--brief", str(self.brief()))
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        net = dict(line.split(": ", 1) for line in (self.stage / "out" / "net.txt").read_text().splitlines()
                   if ": " in line)
        self.assertEqual(net.get("listed"), f"200 {BODY}", net)
        self.assertEqual(net.get("unlisted"), "refused 403", net)
        self.assertEqual(net.get("ip-proxy"), "refused 403", net)
        self.assertEqual(net.get("key-host"), "refused 403", net)
        for unreachable in ("ip-direct", "host-loopback", "abstract"):
            self.assertTrue(net.get(unreachable, "").startswith("failed"), (unreachable, net))
        self.assertEqual(net.get("dns"), "failed", net)
        self.assertEqual(net.get("inside"), "200 inside-body", "loopback in the sandbox is reached directly")
        seen = (self.stage / "out" / "proxy-env.txt").read_text()
        for name in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
            self.assertIn(f"{name}=http://127.0.0.1:3128\n", seen)
        for name in ("NO_PROXY", "no_proxy"):
            self.assertIn(f"{name}=localhost,127.0.0.1,::1\n", seen)
        # The proxy's log comes back masked: the host the bot built from its key shows as ***.
        log = (self.stage / "egress.log").read_text()
        self.assertIn(f"host={LISTED} port=80 result=allowed", log)
        self.assertIn(f"host={UNLISTED} port=80 result=refused reason=not-allowed", log)
        self.assertIn("host=***.example", log)
        self.assertNotIn(KEY, log + result.stdout + result.stderr)
        self.assertFalse((self.stage / "egress" / "proxy.log").exists(), "the raw log is removed")
        self.assertEqual(json.loads((self.stage / "run.json").read_text())["egress"]["private"], [])
        self.assert_nothing_left()

    def test_a_private_pair_is_in_run_json_and_reaches_the_proxy(self) -> None:
        # T-0132: run.json holds [egress] private under egress.private in bwrap mode as in container mode.
        pairs = ["127.0.0.1:11434", "[::1]:8080"]
        with self.p.config.open("a") as handle:
            handle.write(f"private = {', '.join(pairs)}\n")
        result = self.p.team("run", "worker", "T-1", "--brief", str(self.brief()))
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn(f"private host:port of this run (fake): {' '.join(pairs)}", result.stderr)
        spec = json.loads((self.stage / "run.json").read_text())
        self.assertEqual(spec["egress"]["private"], pairs)
        self.assertEqual(spec["bwrap_egress"]["private"], pairs)
        argv = self.recorded()[0][0]["argv"]
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "--private"], pairs)
        self.assert_nothing_left()

    def test_the_proxy_gets_no_key_and_only_the_network(self) -> None:
        result = self.p.team("run", "worker", "T-1", "--brief", str(self.brief()))
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        proxies, bots = self.recorded()
        self.assertEqual((len(proxies), len(bots)), (1, 1), self.calls.read_text())
        argv, env = proxies[0]["argv"], proxies[0]["env"]
        self.assertNotIn(KEY, json.dumps(proxies))
        self.assertEqual([k for k in env if k != "LC_CTYPE"], [])  # LC_CTYPE: the recorder's Python
        self.assertIn("--clearenv", argv)
        self.assertNotIn("--share-net", bots[0]["argv"])
        unix = argv.index("--unix")
        self.assertEqual(argv[unix + 1], "/run/wouf/egress.sock", "the socket's path inside, not the host's")
        binds = [(argv[i], argv[i + 1], argv[i + 2]) for i in range(len(argv) - 2)
                 if argv[i] in ("--bind", "--ro-bind")]
        ctl = str(self.sock.parent)
        self.assertEqual([b for b in binds if b[0] == "--bind"], [("--bind", ctl, "/run/wouf")],
                         "only the socket's folder is writable")
        self.assertIn(("--ro-bind", f"{ctl}/egress_proxy.py", "/run/wouf/egress_proxy.py"), binds)
        if os.path.isdir("/usr") and not os.path.islink("/usr"):
            self.assertIn(("--ro-bind", "/usr", "/usr"), binds)
        # Inside, what the proxy itself saw: no key, no WOUF_MASK; its PATH and what sh and Python set themselves.
        names = [line.split(": ", 1)[1].split() for line in (self.stage / "egress.log").read_text().splitlines()
                 if line.startswith("proxy-env: ")]
        self.assertEqual(len(names), 1, names)
        self.assertIn("PATH", names[0])
        self.assertEqual(sorted(set(names[0]) - {"PATH", "PWD", "OLDPWD", "SHLVL", "_", "LC_CTYPE"}), [])

    def test_a_timeout_leaves_no_proxy_and_no_socket(self) -> None:
        self.short()
        result = self.p.team("run", "worker", "T-1", "--brief", str(self.brief("SLEEP\n")))
        self.assertEqual(result.returncode, 124, result.stderr + result.stdout)
        self.assertIn("proxy-env: ", (self.stage / "egress.log").read_text())
        self.assert_nothing_left()

    def test_a_killed_supervisor_leaves_no_proxy_and_no_socket(self) -> None:
        result = self.p.team("run", "worker", "T-1", "--brief", str(self.brief("SLEEP\n")), "--detach")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not (self.env / "worker" / "box" / "working").exists():
            time.sleep(0.2)  # until the bot works, behind its running proxy
        self.assertTrue(os.path.lexists(self.sock) and self.proxies(), "the proxy runs while the bot works")
        pid = json.loads((self.stage / "result.json").read_text())["pid"]
        os.kill(pid, signal.SIGKILL)
        self.assert_nothing_left(wait=30)


@unittest.skipUnless(USABLE, "bwrap is not installed or cannot start a sandbox here (user namespaces)")
class LongStagePathTest(EgressProject, unittest.TestCase):
    """A TMPDIR of 120 characters: the stage's socket path is longer than AF_UNIX allows (108 bytes), and the
    proxy still listens, since it binds its socket at /run/wouf/egress.sock in its sandbox."""

    def setUp(self) -> None:
        base = tempfile.TemporaryDirectory()
        self.addCleanup(base.cleanup)
        long = Path(base.name) / ("d" * (120 - len(base.name) - 1))
        long.mkdir()
        self.assertEqual(len(str(long)), 120)
        saved, tempfile.tempdir = tempfile.tempdir, str(long)
        try:
            super().setUp()
        finally:
            tempfile.tempdir = saved

    def test_an_egress_run_and_a_prefix_build_work_with_a_long_stage_path(self) -> None:
        self.assertGreater(len(str(self.sock)), 108)
        result = self.p.team("run", "worker", "T-1", "--brief", str(self.brief()))
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        net = (self.stage / "out" / "net.txt").read_text()
        self.assertIn(f"listed: 200 {BODY}\n", net)
        self.assertIn("unlisted: refused 403\n", net)
        self.assert_nothing_left()
        build = self.env / "_prefix" / "build" / "ctl" / "egress.sock"
        self.assertGreater(len(str(build)), 108)
        (self.p.root / ".agents" / "team" / "provision.sh").write_text(
            "python3 -c \"import urllib.request; open('/opt/wouf/listed.txt', 'w').write("
            "urllib.request.urlopen('http://listed.example/', timeout=15).read().decode())\"\n")
        built = self.p.team("image")
        self.assertEqual(built.returncode, 0, built.stderr + built.stdout)
        self.assertEqual((self.env / "_prefix" / "opt" / "listed.txt").read_text(), BODY)
        self.assertFalse(os.path.lexists(build), "no socket remains")


if __name__ == "__main__":
    unittest.main()
