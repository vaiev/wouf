"""mode = bwrap: the sandbox argv (no bwrap needed) and its isolation, proved against a real bubblewrap."""

from __future__ import annotations

import glob
import hashlib
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".agents" / "bin"))
from teamlib import bwrap  # noqa: E402

# The mount table of PLAN T-0060, written out here on its own so the test does not trust the module's constants.
HOST_DIRS = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32")
HOST_ETC = ("ssl", "ca-certificates", "pki", "alternatives", "ld.so.*", "localtime", "nsswitch.conf", "hosts")
RUN_BINDS = {"passwd": ("--ro-bind", "/etc/passwd"), "group": ("--ro-bind", "/etc/group"),
             "prefix": ("--ro-bind", "/opt/wouf"), "box": ("--bind", "/bot"), "inbox": ("--ro-bind", "/inbox"),
             "pack": ("--ro-bind", "/wouf"), "bundle": ("--ro-bind", "/repo.bundle"),
             "outbox": ("--bind", "/outbox"), "ctl": ("--ro-bind", "/run/wouf")}
PRIVATE = [("--tmpfs", "/tmp"), ("--dev", "/dev"), ("--proc", "/proc")]
ALWAYS = ("--unshare-all", "--clearenv", "--new-session", "--die-with-parent")
TWO = ("--ro-bind", "--bind", "--symlink")
ONE = ("--tmpfs", "--dev", "--proc", "--chdir")
USABLE = bwrap.bwrap_usable()
VERSION = bwrap.bwrap_version() if USABLE else (0,)


def mounts(argv: list[str]) -> list[tuple]:
    """The mount operations of a bwrap argv, in order: (option, source, destination) or (option, destination)."""
    found, at = [], 0
    while at < len(argv):
        if argv[at] in TWO:
            found.append(tuple(argv[at:at + 3]))
            at += 3
        elif argv[at] in ONE:
            found.append(tuple(argv[at:at + 2]))
            at += 2
        else:
            at += 1
    return [m for m in found if m[0] != "--chdir"]


def expected_host() -> list[tuple]:
    rows = []
    for path in HOST_DIRS:
        if os.path.islink(path):
            rows.append(("--symlink", os.readlink(path), path))
        elif os.path.isdir(path):
            rows.append(("--ro-bind", path, path))
    for pattern in HOST_ETC:
        rows += [("--ro-bind", p, p) for p in sorted(glob.glob("/etc/" + pattern)) if os.path.exists(p)]
    return rows


def run_tree(base: Path) -> dict:
    """The run stage and storage of one bot under base: every source of BINDS."""
    base.mkdir(mode=0o700, exist_ok=True)
    paths = {name: base / name for name in RUN_BINDS}
    for name in ("prefix", "box", "inbox", "pack", "outbox", "ctl"):
        paths[name].mkdir(mode=0o700)
    paths["passwd"].write_text("bot:x:1000:1000:bot:/bot/home:/bin/sh\n")
    paths["group"].write_text("bot:x:1000:\n")
    paths["bundle"].write_bytes(b"bundle\n")
    (paths["inbox"] / "BRIEF.md").write_text("Objective: test\n")
    return paths


class BoxArgvTest(unittest.TestCase):
    """The argv alone, no bwrap needed."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.binds = run_tree(self.base)

    def test_the_sandbox_always_unshares_everything_and_never_shares_the_network(self) -> None:
        for version in ((0, 4, 1), (0, 7, 9), (0, 8, 0), (0, 13, 1)):
            with self.subTest(version=version):
                argv = bwrap.box_argv(self.binds, "none", version=version)
                self.assertEqual(argv[0], "bwrap")
                self.assertEqual(argv[-1], "--")
                for flag in ALWAYS:
                    self.assertIn(flag, argv)
                self.assertNotIn("--share-net", argv)
                self.assertEqual("--disable-userns" in argv, version >= (0, 8, 0))

    def test_bridge_and_unknown_networks_and_binds_are_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "bridge is refused"):
            bwrap.box_argv(self.binds, "bridge", version=(0, 8, 0))
        with self.assertRaises(ValueError):
            bwrap.box_argv(self.binds, "host", version=(0, 8, 0))
        with self.assertRaisesRegex(ValueError, "home"):
            bwrap.box_argv({**self.binds, "home": str(Path.home())}, "none", version=(0, 8, 0))

    def test_exactly_the_mount_table_is_bound_and_nothing_else(self) -> None:
        argv = bwrap.box_argv({k: str(v) for k, v in self.binds.items()}, "none", version=(0, 8, 0))
        run = [(option, str(self.binds[name]), inside) for name, (option, inside) in RUN_BINDS.items()]
        self.assertEqual(mounts(argv), expected_host() + run + PRIVATE)

    def test_a_source_that_does_not_exist_is_left_out(self) -> None:
        for name in ("prefix", "bundle", "ctl"):
            path = self.binds[name]
            path.unlink() if path.is_file() else path.rmdir()
        argv = bwrap.box_argv(self.binds, "none", version=(0, 8, 0))
        run = [(option, str(self.binds[name]), inside) for name, (option, inside) in RUN_BINDS.items()
               if name not in ("prefix", "bundle", "ctl")]
        self.assertEqual(mounts(argv), expected_host() + run + PRIVATE)

    def test_no_home_project_or_runtime_folder_is_ever_a_source(self) -> None:
        argv = bwrap.box_argv(self.binds, "none", version=(0, 8, 0))
        sources = [m[1] for m in mounts(argv) if m[0] in ("--ro-bind", "--bind")]
        for path in sources:
            for banned in ("/home", "/root", "/run/user", "/var", str(Path.home()), os.getcwd()):
                self.assertFalse(path == banned or path.startswith(banned.rstrip("/") + "/"), (path, banned))


def snapshot(base: Path) -> dict:
    return {str(p.relative_to(base)): (hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "dir")
            for p in sorted(base.rglob("*")) if not p.is_symlink()}


@unittest.skipUnless(USABLE, "bwrap is not installed or cannot start a sandbox here (user namespaces)")
class BwrapIsolationTest(unittest.TestCase):
    """Real sandboxes, each started the way team will start a bot: from the project folder, with the human's HOME."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.project = self.base / "project"
        self.project.mkdir()
        (self.project / "PROJECT_CANARY").write_text("project-canary-contents\n")
        self.home = self.base / "home"
        (self.home / ".ssh").mkdir(parents=True)
        (self.home / ".ssh" / "id_canary").write_text("ssh-canary-contents\n")
        self.binds = run_tree(self.base / "stage")
        self.env = {**os.environ, "HOME": str(self.home), "WOUF_CANARY": "env-canary-" + secrets.token_hex(8)}

    def box(self, script: str, *args: str, stdin: str = "", timeout: int = 120) -> subprocess.CompletedProcess:
        self.argv = [*bwrap.box_argv(self.binds, "none"), "/bin/sh", "-c",
                     "PATH=/usr/bin:/bin; export PATH\n" + script, "sh", *args]
        return subprocess.run(self.argv, input=stdin, capture_output=True, text=True, timeout=timeout, env=self.env,
                              cwd=self.project)

    def test_a_project_folder_and_home_with_ssh_key_cannot_be_found_or_read(self) -> None:
        canary = self.home / ".ssh" / "id_canary"
        # Compare mountinfo's escaped mount points on both sides, including WSL's
        # inherited /usr submounts. Prune the runner's large read-only /usr tree
        # only after rejecting any additional mount that could hide project/home.
        allowed = [line.split()[4] for line in Path("/proc/self/mountinfo").read_text().splitlines()
                   if line.split()[4].startswith("/usr/")]
        guard = ("import sys\n"
                 "allowed = sys.argv[1].splitlines()\n"
                 "seen = False\n"
                 "for line in sys.stdin:\n"
                 "    mountpoint = line.split()[4]\n"
                 "    if mountpoint == '/usr': seen = True\n"
                 "    if mountpoint.startswith('/usr/') and mountpoint not in allowed:\n"
                 "        print('NESTED-MOUNT ' + mountpoint)\n"
                 "        sys.exit(1)\n"
                 "sys.exit(0 if seen else 1)\n")
        # Exercise the same guard with escaped names and an unexpected mount.
        fixture = "1 0 0:1 / /usr rw - tmpfs tmpfs rw\n"
        inherited = r"/usr/space\040and\134slash"
        fixture += f"2 1 0:2 / {inherited} rw - tmpfs tmpfs rw\n"
        for extra, expected in (("", 0), ("3 1 0:3 / /usr/x rw - tmpfs tmpfs rw\n", 1)):
            checked = subprocess.run([sys.executable, "-I", "-c", guard, inherited],
                                     input=fixture + extra, capture_output=True, text=True, timeout=2)
            self.assertEqual(checked.returncode, expected, checked.stderr)
            self.assertEqual(checked.stdout, "NESTED-MOUNT /usr/x\n" if extra else "")
        # The first arguments belong to the guard; leave only host paths for find.
        script = ('python3 -I -c "$1" "$2" < /proc/self/mountinfo || exit 1\n'
                  'shift 2\n'
                  'for p in "$@"; do [ -e "$p" ] && echo "EXISTS $p"; cat "$p" 2>/dev/null; done\n'
                  'find / \\( -path /proc -o -path /usr \\) -prune -o '
                  '\\( -name id_canary -o -name PROJECT_CANARY -o -name .ssh '
                  '-o -name "${1##*/}" -o -name "${3##*/}" \\) -print 2>/dev/null\necho END')
        done = self.box(script, guard, "\n".join(allowed),
                        str(self.project), str(self.project / "PROJECT_CANARY"), str(self.home), str(canary),
                        timeout=10)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, "END\n", done.stderr)

    def test_a_host_environment_variable_does_not_reach_the_sandbox(self) -> None:
        done = self.box("env")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("WOUF_CANARY", done.stdout)
        self.assertNotIn(self.env["WOUF_CANARY"], done.stdout)
        self.assertNotIn(str(self.home), done.stdout)

    def test_a_key_on_stdin_reaches_the_script_and_no_command_line(self) -> None:
        key = "sk-canary-" + secrets.token_hex(16)
        # printf and case are shell built-ins: the value never becomes an argument of any process.
        script = ('while IFS= read -r line; do export "$line"; done\n'
                  'printf %s "$ALPHA_KEY" | sha256sum\n'
                  'for f in /proc/[0-9]*/cmdline; do c=$(tr "\\0" " " < "$f")\n'
                  '  case "$c" in *"$ALPHA_KEY"*) echo "IN-CMDLINE $f";; esac; done\necho END')
        done = self.box(script, stdin=f"ALPHA_KEY={key}\n")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.split()[0], hashlib.sha256(key.encode()).hexdigest())
        self.assertNotIn("IN-CMDLINE", done.stdout)
        self.assertTrue(done.stdout.endswith("END\n"), done.stdout)
        self.assertFalse(any(key in part for part in self.argv))
        self.assertNotIn(key, done.stdout + done.stderr)

    def test_proc_lists_only_the_sandbox_processes(self) -> None:
        host = subprocess.Popen(["sleep", "300"])
        self.addCleanup(host.wait)
        self.addCleanup(host.kill)
        done = self.box('for p in /proc/[0-9]*; do echo "${p#/proc/} $(cat "$p/comm")"; done')
        self.assertEqual(done.returncode, 0, done.stderr)
        rows = [line.split(" ", 1) for line in done.stdout.splitlines()]
        self.assertLessEqual({comm for _, comm in rows}, {"bwrap", "sh", "cat"}, done.stdout)
        self.assertLessEqual(len(rows), 4, done.stdout)
        self.assertLessEqual(max(int(pid) for pid, _ in rows), 10, done.stdout)

    def test_the_bot_has_no_capabilities_and_cannot_gain_any(self) -> None:
        done = self.box("cat /proc/self/status")
        self.assertEqual(done.returncode, 0, done.stderr)
        status = dict(line.split(":\t", 1) for line in done.stdout.splitlines() if ":\t" in line)
        for cap in ("CapEff", "CapBnd", "CapPrm"):
            self.assertEqual(int(status[cap], 16), 0, cap)
        self.assertEqual(status["NoNewPrivs"].strip(), "1")

    @unittest.skipIf(VERSION < (0, 8, 0), f"bwrap {'.'.join(map(str, VERSION))} lacks --disable-userns (0.8.0)")
    def test_no_user_namespace_can_be_created_inside(self) -> None:
        done = self.box('command -v unshare >/dev/null || { echo NO-UNSHARE; exit 3; }\n'
                        'unshare --user /bin/true; echo "rc=$?"')
        self.assertNotIn("NO-UNSHARE", done.stdout)
        self.assertIn("rc=", done.stdout, done.stderr)
        self.assertNotIn("rc=0", done.stdout, done.stderr)

    def test_no_network_but_own_loopback_and_no_host_socket_is_reachable(self) -> None:
        tcp = socket.socket()
        self.addCleanup(tcp.close)
        tcp.bind(("127.0.0.1", 0))
        tcp.listen(1)
        tcp.settimeout(1)
        name = "wouf-canary-" + secrets.token_hex(8)
        unix = socket.socket(socket.AF_UNIX)
        self.addCleanup(unix.close)
        unix.bind("\0" + name)
        unix.listen(1)
        unix.settimeout(1)
        for listener, address, family in ((tcp, tcp.getsockname(), socket.AF_INET), (unix, "\0" + name,
                                                                                      socket.AF_UNIX)):
            with socket.socket(family) as probe:  # the host itself reaches both: the test is not empty
                probe.connect(address)
                listener.accept()[0].close()
        reach = ("import socket, sys\n"
                 "for fam, addr in ((socket.AF_INET, ('127.0.0.1', int(sys.argv[1]))),"
                 " (socket.AF_UNIX, '\\0' + sys.argv[2])):\n"
                 "    s = socket.socket(fam)\n    s.settimeout(3)\n"
                 "    try:\n        s.connect(addr)\n        print('REACHED', fam)\n"
                 "    except OSError as e:\n        print('REFUSED', type(e).__name__)\n")
        done = self.box('cat /proc/net/dev\nexec python3 -I -c "$1" "$2" "$3"', reach, str(tcp.getsockname()[1]),
                        name)
        self.assertEqual(done.returncode, 0, done.stderr)
        lines = done.stdout.splitlines()
        devices = [line.split(":")[0].strip() for line in lines if ":" in line and "|" not in line]
        self.assertEqual(devices, ["lo"], done.stdout)
        self.assertEqual([line for line in lines if line.startswith(("REACHED", "REFUSED"))],
                         ["REFUSED ConnectionRefusedError"] * 2, done.stdout)
        for listener in (tcp, unix):
            with self.assertRaises(socket.timeout):
                listener.accept()

    def test_only_bot_storage_is_writable_and_nothing_else_of_the_host_changes(self) -> None:
        marker = "wouf-tmp-canary-" + secrets.token_hex(8)
        before = snapshot(self.base)
        script = ('touch /usr/canary 2>/dev/null && echo USR-WRITTEN\n'
                  'for d in /inbox /wouf /opt/wouf /run/wouf /etc; do touch "$d/canary" 2>/dev/null; done\n'
                  'echo written > /bot/written && echo BOT-OK\n'
                  'echo x > "/tmp/$1" && echo TMP-OK')
        done = self.box(script, marker)
        self.assertEqual(done.stdout, "BOT-OK\nTMP-OK\n", done.stderr)
        self.assertEqual((self.binds["box"] / "written").read_text(), "written\n")
        after = snapshot(self.base)
        self.assertEqual({k: v for k, v in after.items() if before.get(k) != v}, {"stage/box/written": after[
            "stage/box/written"]})
        self.assertEqual(set(before) - set(after), set())
        self.assertFalse(Path("/tmp", marker).exists())

    def test_a_process_left_behind_stops_when_the_sandbox_ends(self) -> None:
        heart = self.binds["box"] / "heartbeat"
        script = ('loop=\'n=0; while :; do n=$((n+1)); echo $n > /bot/heartbeat; sleep 0.1; done\'\n'
                  'if command -v setsid >/dev/null; then setsid sh -c "$loop" </dev/null >/dev/null 2>&1 &\n'
                  'else sh -c "$loop" </dev/null >/dev/null 2>&1 & fi\n'
                  'while [ ! -s /bot/heartbeat ]; do sleep 0.1; done; sleep 0.5; echo LEFT')
        done = self.box(script, timeout=60)
        self.assertEqual(done.stdout, "LEFT\n", done.stderr)
        time.sleep(0.5)
        beat = heart.read_text()
        time.sleep(1.5)
        self.assertEqual(heart.read_text(), beat, "the left-behind process still writes")

    def test_home_root_and_user_runtime_folders_do_not_exist_inside(self) -> None:
        runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        done = self.box('for p in "$@"; do if [ -e "$p" ] || [ -L "$p" ]; then echo "EXISTS $p"; fi; done; echo END',
                        "/home", "/root", "/run/user", runtime)
        self.assertEqual(done.stdout, "END\n", done.stderr)


if __name__ == "__main__":
    unittest.main()
