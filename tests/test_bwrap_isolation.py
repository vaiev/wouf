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
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".agents" / "bin"))
from teamlib import bwrap, taskstore  # noqa: E402

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


class TaskBindsTest(unittest.TestCase):
    """bwrap.task_binds (plan T-0167, option B) alone, no bwrap needed: one task's storage and the login storage."""

    def test_the_three_task_folders_are_bound_writable_and_login_only_when_given(self) -> None:
        stage = Path("/env/worker/tasks/T-1")
        own = ["--bind", "/env/worker/tasks/T-1/box/work", "/bot/tasks/T-1",
               "--bind", "/env/worker/tasks/T-1/box/home", "/bot/home",
               "--bind", "/env/worker/tasks/T-1/box/logs", "/bot/logs"]
        self.assertEqual(bwrap.task_binds(stage, "T-1", None), own)
        self.assertEqual(bwrap.task_binds(str(stage), "T-1", Path("/env/worker/login")),
                         own + ["--bind", "/env/worker/login", "/bot/login"])
        self.assertNotIn("/bot", bwrap.task_binds(stage, "T-1", "/env/worker/login"), "/bot itself stays private")

    def test_it_makes_no_file_and_starts_no_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stage, login = Path(tmp) / "env" / "worker" / "tasks" / "T-1", Path(tmp) / "env" / "worker" / "login"
            with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("a process was started")), \
                    mock.patch.object(os, "fork", side_effect=AssertionError("a process was started")):
                bwrap.task_binds(stage, "T-1", login)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_a_task_id_that_could_name_another_path_is_refused(self) -> None:
        for task in ("../T-2", "T-1/../../home", "/bot", "", ".", "..", ".hidden", "-x", "a..b", "x/y", "T-1\n"):
            with self.subTest(task=task):
                with self.assertRaises(ValueError):
                    bwrap.task_binds("/env/worker/tasks/T-1", task, None)


def task_storage(env: Path, task: str, token: str) -> tuple[Path, dict]:
    """The stage of one task of the member worker under env, as option B lays it out, with a canary in each of its
    three folders; and its binds without the old member-wide box."""
    stage = env / "worker" / "tasks" / task
    stage.parent.mkdir(parents=True, exist_ok=True)
    binds = run_tree(stage)
    del binds["box"]
    for sub in ("work", "home", "logs"):
        (stage / "box" / sub).mkdir(mode=0o700)
        (stage / "box" / sub / f"canary-{task}-{sub}").write_text(f"canary-{token}-{sub}\n")
    return stage, {name: str(path) for name, path in binds.items()}


# What a sandbox of one task tries against another task ($1) of the same member: list /bot and every way up or
# around (.., /proc/*/root, /proc/*/cwd), read every file under /bot and /tmp, find a canary or the other id
# anywhere, and open the other task's paths by name. Errors go nowhere, so stdout holds only what was reached.
PROBE = r"""b=$1
for p in /bot /bot/tasks /bot/home/.. /bot/logs/.. /bot/tasks/.. /bot/login/.. /proc/self/root/bot/tasks \
         /proc/[0-9]*/root/bot /proc/[0-9]*/root/bot/tasks /proc/[0-9]*/cwd; do
  echo "LIST $p"; ls -a "$p" 2>/dev/null
done
echo FILES
find -H /bot /tmp /proc/[0-9]*/root/bot -type f 2>/dev/null | while IFS= read -r f; do cat "$f" 2>/dev/null; done
echo NAMES
find / \( -path /proc -o -path /sys -o -path /dev -o -path /usr \) -prune -o \( -name "*$b*" -o -name 'canary-*' \) \
  -print 2>/dev/null
echo GUESSES
for d in "/bot/tasks/$b" "/bot/home/../tasks/$b" "/bot/tasks/../tasks/$b" /proc/[0-9]*/root/bot/tasks/"$b"; do
  ls -a "$d" 2>/dev/null
done
for sub in work home logs; do
  for d in "/bot/tasks/$b" /bot/home /bot/logs /bot/home/../home /proc/[0-9]*/root/bot/home \
           /proc/[0-9]*/root/bot/logs; do
    cat "$d/canary-$b-$sub" 2>/dev/null
  done
done
echo END
"""
# A task's own three folders, written; $1 the task, $2 what to write.
WRITE = (r'printf "%s\n" "work-$2" > "/bot/tasks/$1/written" && printf "%s\n" "home-$2" > /bot/home/written && '
         r'printf "%s\n" "logs-$2" > /bot/logs/written && echo WROTE')
# os.rename, as gnome-keyring replaces its keyring: a new file beside the old one, then one rename over it.
RENAME = "import os, sys\nos.rename(sys.argv[1], sys.argv[2])\nprint('RENAMED')\n"


@unittest.skipUnless(USABLE, "bwrap is not installed or cannot start a sandbox here (user namespaces)")
class TaskIsolationTest(unittest.TestCase):
    """Option B against a real bwrap: two task storages of one member, each run in its own sandbox. A run sees its
    own folder, HOME and receipts, never another task's, before, after or at the same time; the login storage alone
    is shared."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        env = self.base / "env"
        hexes = secrets.token_hex(4)
        # Two task ids of one member, with ".", "_" and "-", unlikely to turn up anywhere by chance.
        self.a, self.b = f"T-{hexes}_a.1", f"T-{hexes}_b.1"
        self.tokens = {self.a: "a-" + secrets.token_hex(8), self.b: "b-" + secrets.token_hex(8)}
        self.stages, self.binds = {}, {}
        for task in (self.a, self.b):
            self.stages[task], self.binds[task] = task_storage(env, task, self.tokens[task])
        self.login = env / "worker" / "login"
        self.login.mkdir(mode=0o700)

    def argv(self, task: str, script: str, *args: str, login: bool = True) -> list[str]:
        """The sandbox of one run of task: the run's usual binds without the old box, then task_binds."""
        box = bwrap.box_argv(self.binds[task], "none")
        self.assertEqual(box[-1], "--")
        binds = bwrap.task_binds(self.stages[task], task, self.login if login else None)
        return [*box[:-1], *binds, "--", "/bin/sh", "-c", "PATH=/usr/bin:/bin; export PATH\n" + script, "sh", *args]

    def run_in(self, task: str, script: str, *args: str, login: bool = True) -> subprocess.CompletedProcess:
        done = subprocess.run(self.argv(task, script, *args, login=login), capture_output=True, text=True,
                              timeout=120, cwd=self.base, stdin=subprocess.DEVNULL)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done

    def written(self, task: str) -> dict:
        box = self.stages[task] / "box"
        return {sub: (box / sub / "written").read_text() for sub in ("work", "home", "logs")
                if (box / sub / "written").exists()}

    def assert_alone(self, me: str, other: str, login: bool = True) -> None:
        """A run of me reaches its own canaries and its earlier writes, and nothing of other by any path."""
        done = self.run_in(me, PROBE, other, login=login)
        out = done.stdout
        self.assertTrue(out.endswith("END\n"), done.stderr)
        listing = out.split("LIST /bot\n", 1)[1].split("LIST ", 1)[0].split()
        self.assertEqual(sorted(listing), sorted([".", "..", "home", "logs", "tasks"] + (["login"] if login else [])))
        self.assertEqual(out.split("LIST /bot/tasks\n", 1)[1].split("LIST ", 1)[0].split(), [".", "..", me])
        self.assertIn(f"LIST /proc/self/root/bot/tasks\n.\n..\n{me}\n", out, "the /proc/*/root path works")
        for sub in ("work", "home", "logs"):
            self.assertIn(f"canary-{self.tokens[me]}-{sub}\n", out, f"its own {sub} is readable")
            self.assertIn(f"/canary-{me}-{sub}\n", out.split("NAMES\n", 1)[1], f"its own {sub} is found")
        self.assertNotIn(other, out)
        self.assertNotIn(self.tokens[other], out)

    def test_a_task_never_reaches_another_tasks_folder_home_or_receipts_one_after_the_other(self) -> None:
        for task in (self.a, self.b):
            self.assertEqual(self.run_in(task, WRITE, task, self.tokens[task]).stdout, "WROTE\n")
        for me, other in ((self.a, self.b), (self.b, self.a)):
            with self.subTest(me=me):
                self.assert_alone(me, other)
                self.assert_alone(me, other, login=False)
        # The three folders keep what each run wrote after its sandbox ended, and a later run of the task reads it.
        for task in (self.a, self.b):
            self.assertEqual(self.written(task), {sub: f"{sub}-{self.tokens[task]}\n" for sub in
                                                  ("work", "home", "logs")})
            again = self.run_in(task, 'cat "/bot/tasks/$1/written" /bot/home/written /bot/logs/written', task)
            self.assertEqual(again.stdout, "".join(f"{sub}-{self.tokens[task]}\n" for sub in ("work", "home", "logs")))

    def test_a_task_never_reaches_another_task_running_at_the_same_time(self) -> None:
        # B writes into its three folders, says it is ready, then waits inside its task folder until told to stop.
        live = (WRITE + ' >/dev/null && touch "/bot/tasks/$1/ready" && cd "/bot/tasks/$1" && n=0\n'
                'while [ ! -e stop ] && [ $n -lt 900 ]; do sleep 0.1; n=$((n+1)); done; echo STOPPED')
        b = subprocess.Popen(self.argv(self.b, live, self.b, self.tokens[self.b]), stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, cwd=self.base, stdin=subprocess.DEVNULL)
        for step in (b.stdout.close, b.stderr.close, b.wait, b.kill):
            self.addCleanup(step)
        ready = self.stages[self.b] / "box" / "work" / "ready"
        deadline = time.monotonic() + 60
        while not ready.exists() and b.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertTrue(ready.exists(), b.stderr.read() if b.poll() is not None else "B never got ready")
        try:
            self.assertEqual(self.run_in(self.a, WRITE, self.a, self.tokens[self.a]).stdout, "WROTE\n")
            self.assert_alone(self.a, self.b)
            self.assertIsNone(b.poll(), "B ran all the while")
        finally:
            (self.stages[self.b] / "box" / "work" / "stop").touch()
        out, err = b.communicate(timeout=60)
        self.assertEqual((b.returncode, out), (0, "STOPPED\n"), err)
        for task in (self.a, self.b):
            self.assertEqual(self.written(task), {sub: f"{sub}-{self.tokens[task]}\n" for sub in
                                                  ("work", "home", "logs")})

    def test_the_login_folder_is_shared_and_a_rename_inside_it_succeeds(self) -> None:
        shared = "login-" + secrets.token_hex(8)
        put = self.run_in(self.a, 'mkdir -p /bot/login/keyrings && printf "%s\\n" "$1" > /bot/login/shared && '
                                  'printf old > /bot/login/keyrings/login.keyring && echo PUT', shared)
        self.assertEqual(put.stdout, "PUT\n")
        got = self.run_in(self.b, "cat /bot/login/shared /bot/login/keyrings/login.keyring")
        self.assertEqual(got.stdout, f"{shared}\nold")
        renamed = self.run_in(self.b, 'printf new > /bot/login/keyrings/login.keyring.TMP1 && python3 -I -c "$1" '
                                      '/bot/login/keyrings/login.keyring.TMP1 /bot/login/keyrings/login.keyring',
                              RENAME)
        self.assertEqual(renamed.stdout, "RENAMED\n")
        self.assertEqual(self.run_in(self.a, "cat /bot/login/keyrings/login.keyring").stdout, "new")
        self.assertEqual(sorted(p.name for p in (self.login / "keyrings").iterdir()), ["login.keyring"])
        self.assertEqual((self.login / "shared").read_text(), f"{shared}\n")

    def test_a_login_folder_planted_as_a_link_to_the_task_folder_gets_no_copy_back(self) -> None:
        """Review r1 of T-0167-2: a run of task B links /bot/login/.codex to task A's folder, where A keeps a file
        of the login file's name, and /bot/login/.gemini to A's out/. A's run with the login steps as run.py
        writes them copies nothing through either before its CLI and has no step after it (review r3: no sandbox
        writes /bot/login); nothing lands in its task folder, the CLI's files stay in its HOME, and each step says
        so on stderr."""
        paths = [".codex/auth.json", ".gemini/oauth.json"]
        before, after = taskstore.login_link_steps(paths)
        self.assertEqual(after, [])
        plant = 'ln -s "/bot/tasks/$1" /bot/login/.codex && ln -s "/bot/tasks/$1/out" /bot/login/.gemini && echo OK'
        self.assertEqual(self.run_in(self.b, plant, self.a).stdout, "OK\n")
        work, home = self.stages[self.a] / "box" / "work", self.stages[self.a] / "box" / "home"
        (work / "out").mkdir()
        (work / "auth.json").write_text("task file\n")

        def listing() -> dict:
            return {str(p.relative_to(work)): os.readlink(p) if p.is_symlink() else p.read_bytes() if p.is_file()
                    else "dir" for p in sorted(work.rglob("*"))}

        planted = listing()
        # The CLI signs in: each file new, written beside and renamed over its place, as gnome-keyring does.
        cli = ('r=$1; shift; for f in "$@"; do mkdir -p "$HOME/${f%/*}"; printf "secret %s\\n" "$f" > "$HOME/$f.tmp"; '
               'python3 -I -c "$r" "$HOME/$f.tmp" "$HOME/$f" >/dev/null; done')
        script = "\n".join(["set -e", "export HOME=/bot/home", *before, cli, "set +e", *after, "echo DONE"])
        done = self.run_in(self.a, script, RENAME, *paths)
        self.assertEqual(done.stdout, "DONE\n", done.stderr)
        self.assertEqual(listing(), planted)  # nothing new or changed in the task folder, out/ included
        for path in paths:
            self.assertFalse((home / path).is_symlink(), path)
            self.assertEqual((home / path).read_text(), f"secret {path}\n")
        self.assertEqual(sorted(p.name for p in self.login.iterdir()), [".codex", ".gemini"])
        self.assertEqual(done.stderr.splitlines(), [f"wouf: login path {path}: a folder above it in HOME or "
                                                    "/bot/login is a link; skipped" for path in paths])

if __name__ == "__main__":
    unittest.main()
