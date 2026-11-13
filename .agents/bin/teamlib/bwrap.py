"""mode = bwrap: the bubblewrap argv of one bot sandbox, whether bubblewrap works on this computer, the run path
of team run with network = none (PLAN T-0060, package 4) or egress (package 6), which teamlib.run reaches only through
run.BOX, the bot lifecycle on env/<member>/box (package 5) and the team prefix env/_prefix that stands in for the team
image (package 7), which teamlib.engine reaches only through engine.BOX.

Importing this module starts no process and writes no file. tests/test_bwrap_isolation.py proves the sandbox against
a real bwrap, tests/test_bwrap_run.py the runs, tests/test_bwrap_egress.py the runs with network = egress,
tests/test_bwrap_prefix.py the prefix."""

from __future__ import annotations

import glob
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from teamlib import engine, gate, run
from teamlib.core import (PROJECT, active_bots, config, copy_regular, die, display, env_dir, get, lead_log, mask_secrets, put,
                          read_json, say, team_dir, visible, words)
from teamlib.core import task_problem

# Always on: every namespace of its own (user, PID, network, IPC, UTS, cgroup), no host environment variable, no
# controlling terminal, no capability, and the sandbox dies with the process that started it. --unshare-user is
# explicit because --disable-userns needs it and --unshare-all only tries it. --share-net is never used for a bot.
FLAGS = ("--unshare-all", "--unshare-user", "--clearenv", "--new-session", "--die-with-parent", "--cap-drop", "ALL")
# Bubblewrap 0.8.0 brought --disable-userns: no user namespace can be created inside the sandbox.
DISABLE_USERNS = (0, 8, 0)
# The host's programs, read-only: each kept as the symlink the host has, else bound.
HOST_DIRS = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32")
# The only parts of the host's /etc a bot sees, read-only (glob patterns, each match bound on its own).
HOST_ETC = ("ssl", "ca-certificates", "pki", "alternatives", "ld.so.*", "localtime", "nsswitch.conf", "hosts")
# The run's own paths: the name a caller uses in binds, the path in the sandbox, and whether the bot may write it.
BINDS = {"passwd": ("/etc/passwd", False), "group": ("/etc/group", False), "prefix": ("/opt/wouf", False),
         "box": ("/bot", True), "inbox": ("/inbox", False), "pack": ("/wouf", False),
         "bundle": ("/repo.bundle", False), "outbox": ("/outbox", True), "ctl": ("/run/wouf", False)}
# New for each sandbox, nothing of the host's: a private /tmp, a minimal /dev and the sandbox's own /proc.
PRIVATE = ("--tmpfs", "/tmp", "--dev", "/dev", "--proc", "/proc")
BRIDGE = ("network = bridge is refused in bwrap mode: bwrap shares this computer's network and loopback with the bot "
          "(its local services and session bus); use none or egress")
# Said at every start of a bwrap run: what it does not have that a container run has.
WEAKER = ("team: mode = bwrap: no syscall filter (no seccomp); "
          "the bot has its own namespaces, no network and only its own folders")
# The bot's PATH: the team prefix first (env/_prefix, read-only), then the host's programs, read-only (HOST_DIRS);
# nothing else of this computer's environment.
INSIDE_PATH = "/opt/wouf/bin:/usr/local/bin:/usr/bin:/bin"
UNUSABLE = ("mode = bwrap: bubblewrap cannot start a sandbox on this computer (is it installed, and are unprivileged "
            "user namespaces allowed?)")
# team bot stop: the run script's watchdog sends TERM to the agent CLI once ctl/stopped is there (/run/wouf/stopped
# inside), so the run still hands back its work; a sandbox still running this many seconds later is killed.
STOP_GRACE = 30
STOP_WATCH = ('( while [ ! -e /run/wouf/stopped ]; do sleep 1; done; kill -TERM "$p" 2>/dev/null ) >/dev/null 2>&1 &',
              "q=$!")
# The lines of teamlib.run's script the stop watchdog starts after (the CLI is $p) and ends with (the time limit's).
STARTED, ENDED = "\nw=$!\n", '\nkill "$w" 2>/dev/null\n'


def bwrap_version() -> tuple:
    """The installed bwrap's version as numbers, (0,) when there is none or it does not say; runs bwrap --version."""
    path = shutil.which("bwrap")
    if not path:
        return (0,)
    try:
        said = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return (0,)
    found = re.search(r"(\d+)\.(\d+)\.(\d+)", said)
    return tuple(int(part) for part in found.groups()) if found else (0,)



def scope_usable() -> bool:
    """Test the user manager, not merely whether systemd-run is installed."""
    try:
        return subprocess.run(["systemd-run", "--user", "--scope", "--quiet", "true"],
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@lru_cache(maxsize=1)
def scope_no_expand_supported() -> bool:
    """Probe once per process whether systemd-run can leave command arguments literal."""
    try:
        help_text = subprocess.run(["systemd-run", "--help"], stdin=subprocess.DEVNULL,
                                   capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "--expand-environment=" in help_text


def scope_environment() -> dict:
    return {name: os.environ[name] for name in ("PATH", "HOME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")
            if name in os.environ}


MEMORY = re.compile(r"(\d+(?:\.\d+)?) ?([kmgtp]?)(?:i?b)?", re.IGNORECASE)


def systemd_memory(memory: str) -> str:
    """A container memory size (4g, 512m, 2048) as a byte count: systemd rejects lowercase suffixes like 4g."""
    match = MEMORY.fullmatch(memory.strip())
    if not match:
        die("invalid [environment] memory; set a size like 4g or 512m")
    return str(int(Decimal(match.group(1)) * 1024 ** "bkmgtp".index(match.group(2).lower() or "b")))


def limit_argv(argv: list[str], memory: str, cpus: str) -> list[str]:
    if not scope_usable():
        say("team: no CPU, memory or process limit on this computer", err=True)
        return argv
    try:
        quota = format(Decimal(cpus) * 100, "f")
        if "." in quota:
            quota = quota.rstrip("0").rstrip(".")
    except InvalidOperation:
        die("invalid [environment] cpus; set a number")
    if scope_no_expand_supported():
        expand = ["--expand-environment=no"]
    else:
        expand = []
        # Older systemd-run expands variables; $$ passes a literal $ through to the command.
        argv = [arg.replace("$", "$$") for arg in argv]
    return ["systemd-run", "--user", "--scope", "-p", f"MemoryMax={systemd_memory(memory)}", "-p", f"CPUQuota={quota}%",
            "-p", "TasksMax=1024", *expand, "env", "-i", *argv]

def host_mounts() -> list[str]:
    """The host's programs and the fixed /etc list, read-only, each only if it exists here."""
    argv: list[str] = []
    for path in HOST_DIRS:
        if os.path.islink(path):
            argv += ["--symlink", os.readlink(path), path]
        elif os.path.isdir(path):
            argv += ["--ro-bind", path, path]
    for pattern in HOST_ETC:
        for path in sorted(glob.glob(os.path.join("/etc", pattern))):
            if os.path.exists(path):
                argv += ["--ro-bind", path, path]
    return argv


def run_mounts(binds: dict) -> list[str]:
    """The run's own paths (BINDS names to host paths), each only if its source exists."""
    unknown = sorted(set(binds) - set(BINDS))
    if unknown:
        raise ValueError(f"unknown bwrap bind: {', '.join(unknown)}")
    argv: list[str] = []
    for name, (inside, writable) in BINDS.items():
        source = binds.get(name)
        if source is not None and os.path.exists(source):
            argv += ["--bind" if writable else "--ro-bind", os.fspath(source), inside]
    return argv


def task_binds(stage_dir, task: str, login_dir=None) -> list[str]:
    """The bwrap mounts of one run with a storage per task (plan T-0167, option B), all writable: the task's
    <stage_dir>/box/work at /bot/tasks/<task>, box/home at /bot/home, box/logs at /bot/logs, and login_dir at
    /bot/login only when it is not None. /bot itself stays part of the sandbox's own root, which is new for each run,
    so no other task's folder, HOME or receipts is there. A task id that could name another path is refused. Builds
    the arguments only: no file is made and no process started."""
    problem = task_problem(task)
    if problem:
        raise ValueError(problem)
    box = Path(stage_dir) / "box"
    argv = ["--bind", os.fspath(box / "work"), f"/bot/tasks/{task}", "--bind", os.fspath(box / "home"), "/bot/home",
            "--bind", os.fspath(box / "logs"), "/bot/logs"]
    if login_dir is not None:
        argv += ["--bind", os.fspath(login_dir), "/bot/login"]
    return argv


def box_argv(binds: dict, network: str, version: tuple | None = None) -> list[str]:
    """The bwrap argv of one bot sandbox, up to and including "--": the command follows. binds maps BINDS names to
    host paths; nothing else of the host is mounted. The bot has no network with none and with egress (it reaches
    the proxy through the relay on its own loopback); bridge is refused. version is the installed bwrap's (asked when
    not given), for --disable-userns."""
    if network == "bridge":
        raise ValueError(BRIDGE)
    if network not in ("none", "egress"):
        raise ValueError(f"network = {network} is not available in bwrap mode; use none or egress")
    found = bwrap_version() if version is None else version
    disable = ["--disable-userns"] if tuple(found) >= DISABLE_USERNS else []
    return ["bwrap", *FLAGS, *disable, *host_mounts(), *run_mounts(binds), *PRIVATE, "--chdir", "/", "--"]


def bwrap_usable() -> bool:
    """Whether one trial sandbox starts and exits cleanly here: bwrap installed and unprivileged user namespaces
    allowed. The version number alone proves neither (PLAN A1). Starts a process only when called."""
    if not shutil.which("bwrap"):
        return False
    try:
        done = subprocess.run([*box_argv({}, "none"), "/bin/sh", "-c", "exit 0"], stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


# ---------------------------------------------------------------- the run path (teamlib.run, through run.BOX)

def check(ini, dry: bool) -> None:
    """Before anything is staged: network = none or egress (bridge refused, saying why), then, outside a dry run, a
    trial sandbox."""
    network = get(ini, "environment", "network", "bridge")
    if network == "bridge":
        die(BRIDGE)
    if network not in ("none", "egress"):
        die(f"network = {visible(network)} is not available in bwrap mode; use none or egress")
    if not dry and not bwrap_usable():
        die(UNUSABLE)


def stage(member: str, connectors: list, stage_dir) -> dict:
    """The run's own folders, next to what teamlib.run staged: the bot's storage (made on its first run), the pack,
    a fresh outbox/, an empty ctl/ and the bot's passwd and group; with network = egress, the proxy in ctl/ and an
    empty egress/ for its log. Says what the sandbox lacks, once per run."""
    box = env_dir() / member / "box"
    box.mkdir(mode=0o700, parents=True, exist_ok=True)
    for sub in ("outbox", "ctl", "etc", "egress"):
        run.remove_tree(stage_dir / sub)  # what an earlier bot left there, made unreadable included
        (stage_dir / sub).mkdir(mode=0o700)
    put(stage_dir / "etc" / "passwd", f"bot:x:{os.getuid()}:{os.getgid()}:bot:/bot/home:/bin/sh\n")
    put(stage_dir / "etc" / "group", f"bot:x:{os.getgid()}:\n")
    say(WEAKER, err=True)
    ini = config()
    staged = {"network": "none", "box": str(box), "pack": str(run.pack_stage(member, connectors, stage_dir)),
              "memory": get(ini, "environment", "memory", "4g"), "cpus": get(ini, "environment", "cpus", "2")}
    return {**staged, **egress_stage(stage_dir)}


def launch(spec: dict) -> tuple:
    """(argv, environment) of the run's sandbox: the mount table of BINDS from the stage's own folders only (never
    run.json, result.json, output.log or the bot's logs), the run script, and an empty environment. The keys and
    WOUF_MASK of this run's values come on stdin (teamlib.run.execute). With network = egress the proxy is started
    first, and the script starts the relay before anything else."""
    at = Path(spec["stage"])
    binds = {"passwd": at / "etc" / "passwd", "group": at / "etc" / "group", "box": spec["box"], "inbox": at / "in",
             "pack": spec["pack"], "outbox": at / "outbox", "ctl": at / "ctl", "prefix": prefix_dir() / "opt"}
    if spec.get("repo"):
        binds["bundle"] = at / "in" / "repo.bundle"
    argv = box_argv({name: str(path) for name, path in binds.items()}, spec["network"])
    script, proxy = egress_launch(spec, watched(spec["script"]))
    command = [shutil.which("bwrap") or "bwrap", *argv[1:-1], "--setenv", "PATH", INSIDE_PATH, *proxy, "--", "sh", "-c",
               script, "sh", *spec["argv"]]
    limited = limit_argv(command, spec.get("memory", "4g"), spec.get("cpus", "2"))
    return limited, scope_environment() if limited is not command else {}


def watched(script: str) -> str:
    """The run script with the stop watchdog: started with the time limit's, ended with it."""
    if script.count(STARTED) != 1 or script.count(ENDED) != 1:
        raise ValueError("the run script has no single place for the stop watchdog")
    script = script.replace(STARTED, STARTED + "\n".join(STOP_WATCH) + "\n")
    return script.replace(ENDED, '\nkill "$w" "$q" 2>/dev/null\n')


def collect(spec: dict, binary: list, skipped: list, masks=()) -> None:
    """After bwrap has exited: the proxy stopped and its log masked into egress.log (network = egress), outbox/out
    into out/ (regular files and real folders only, masked), and of outbox/logs only the bot's own receipts, a regular
    file, merged into its logs. Nothing else there is read."""
    proxy_down(spec, masks)
    at, member = Path(spec["stage"]), spec["member"]
    out, logs = at / "out", at / "outbox" / "logs"
    run.remove_tree(out)
    out.mkdir()
    skipped += ["out" if name == "." else f"out/{name}" for name in copy_regular(at / "outbox" / "out", out, binary)]
    receipts = logs / f"{member}.jsonl"
    if logs.is_symlink() or not os.path.lexists(receipts) or not stat.S_ISREG(os.lstat(receipts).st_mode):
        return
    target = Path(spec["log_dir"])
    target.mkdir(parents=True, exist_ok=True)
    run.merge_log(receipts, target / f"{member}.jsonl", masks)


# ---------------------------------------------------------------- network = egress (the run path)

# The bot has no network. The egress proxy, the project's copy named in [egress] proxy, listens on ctl/egress.sock in
# a sandbox of its own that shares this computer's network; in the bot's sandbox the same program with --relay takes
# 127.0.0.1:3128 to that socket (ctl/ is /run/wouf in both, read-only in the bot's), and only loopback goes around
# it, as in a container run (teamlib.engine.EGRESS_ENV).
EGRESS: list = []  # this run's allowlist and proxy (teamlib.engine.egress_prepare), for stage in the same process
PROXY: list = []  # the running proxy's process, in the supervisor, from launch to collect
SOCKET, PROGRAM = "egress.sock", "egress_proxy.py"
BOT_PROXY = {name: value.replace("//proxy:", "//127.0.0.1:") for name, value in engine.EGRESS_ENV.items()}
# The relay, before the run script, waited for until it listens (/proc/net/tcp: the port in hex, 0A listening).
RELAY = (f"python3 -I /run/wouf/{PROGRAM} --relay /run/wouf/{SOCKET} --bind 127.0.0.1 --port {engine.EGRESS_PORT} "
         "</dev/null >/dev/null 2>&1 &\n"
         f"i=0; while [ $i -lt 100 ] && ! grep -q ':{engine.EGRESS_PORT:04X} 00000000:0000 0A' /proc/net/tcp; "
         "do i=$((i+1)); sleep 0.1; done\n")
# In the proxy's sandbox: the proxy until the supervisor's end of its stdin closes (collect, or the supervisor was
# killed), then TERM, on which the proxy removes its socket. Nothing is ever written to that stdin; fd 3 keeps it,
# since a background list reads /dev/null.
LIFELINE = ('exec 3<&0\npython3 -I "$0" "$@" </dev/null 3<&- & p=$!\n'
            '( cat <&3 >/dev/null; kill -TERM "$p" 2>/dev/null ) &\nexec 3<&-\nwait "$p"\n')
# Of /etc, the proxy sees HOST_ETC and the resolver's configuration.
PROXY_ETC = ("resolv.conf", "gai.conf", "host.conf")
PROXY_WAIT = 20


def egress_chosen(egress: dict) -> dict:
    """teamlib.engine.egress_prepare in bwrap mode: no container engine to check; the list is kept for stage."""
    EGRESS[:] = [egress]
    return egress


def egress_stage(stage_dir: Path) -> dict:
    """With network = egress: the proxy program into ctl/ (both sandboxes run it from there, read-only) and what
    the run's spec gets; nothing with none. bwrap_egress is the proxy sandbox's input; egress records the lists in
    run.json as container mode does (egress.private), which teamlib.engine.egress_end leaves alone in bwrap mode."""
    if not EGRESS:
        return {}
    source = Path(EGRESS[0]["source"])
    if source.is_symlink() or not source.is_file():
        die(f"the egress proxy {source} is not a regular file; name egress_proxy.py in [egress] proxy")
    put(stage_dir / "ctl" / PROGRAM, source.read_text(encoding="utf-8"))
    lists = {"allow": list(EGRESS[0]["allow"]), "private": list(EGRESS[0].get("private", []))}
    return {"network": "egress", "bwrap_egress": lists, "egress": dict(lists)}


def egress_launch(spec: dict, script: str) -> tuple[str, list[str]]:
    """With network = egress: the proxy started, then the script with the relay first and the bot's proxy
    variables; the script as it is without."""
    if not spec.get("bwrap_egress"):
        return script, []
    proxy_up(spec)
    return RELAY + script, [arg for name, value in BOT_PROXY.items() for arg in ("--setenv", name, value)]


def proxy_argv(spec: dict) -> list[str]:
    """The proxy's sandbox: its own namespaces but this computer's network, no variable, the host's programs and
    resolver read-only, ctl/ writable at /run/wouf for the socket with the program in it read-only. Its socket is
    /run/wouf/egress.sock there, whatever the length of the stage's host path (AF_UNIX takes 108 bytes). No
    --die-with-parent: LIFELINE ends it, so that its socket goes too."""
    ctl, inside = os.path.join(spec["stage"], "ctl"), BINDS["ctl"][0]
    program = f"{inside}/{PROGRAM}"
    disable = ["--disable-userns"] if bwrap_version() >= DISABLE_USERNS else []
    etc = [arg for name in PROXY_ETC if os.path.exists(f"/etc/{name}") for arg in ("--ro-bind", f"/etc/{name}",
                                                                                   f"/etc/{name}")]
    allow = [arg for host in spec["bwrap_egress"]["allow"] for arg in ("--allow", host)]
    allow += [arg for pair in spec["bwrap_egress"].get("private", []) for arg in ("--private", pair)]
    return [shutil.which("bwrap") or "bwrap", "--unshare-all", "--share-net", "--unshare-user", "--clearenv",
            "--new-session", "--cap-drop", "ALL", *disable, *host_mounts(), *etc, *PRIVATE, "--bind", ctl, inside,
            "--ro-bind", os.path.join(ctl, PROGRAM), program, "--chdir", "/", "--setenv", "PATH", INSIDE_PATH, "--",
            "sh", "-c", LIFELINE, program, "--unix", f"{inside}/{SOCKET}", *allow]


def proxy_up(spec: dict) -> None:
    """The proxy started and listening before the bot's sandbox is: its output (its banner and one line per
    connection) into egress/proxy.log, an empty environment, nothing on its stdin."""
    at = Path(spec["stage"])
    sock, raw = at / "ctl" / SOCKET, at / "egress" / "proxy.log"
    sock.unlink(missing_ok=True)
    with raw.open("w", encoding="utf-8") as log:
        PROXY[:] = [subprocess.Popen(proxy_argv(spec), env={}, stdin=subprocess.PIPE, stdout=log,
                                     stderr=subprocess.STDOUT, start_new_session=True)]
    deadline = time.monotonic() + PROXY_WAIT
    while engine.EGRESS_READY not in raw.read_text(encoding="utf-8", errors="replace"):
        if time.monotonic() > deadline or PROXY[0].poll() is not None:
            said = visible(raw.read_text(encoding="utf-8", errors="replace").strip())[-300:]
            proxy_down(spec)
            die(f"the egress proxy did not start: {said}")
        time.sleep(0.1)


def proxy_down(spec: dict, masks=()) -> None:
    """The proxy ended (its stdin closed; its process group killed 10 s later), its socket gone, and its log into
    egress.log, masked; the raw one is removed. No-op without egress."""
    if not spec.get("bwrap_egress"):
        return
    for proc in PROXY:
        proc.stdin.close()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            signal_group(proc.pid, 9)
            proc.wait()
    PROXY.clear()
    at = Path(spec["stage"])
    (at / "ctl" / SOCKET).unlink(missing_ok=True)
    raw = at / "egress" / "proxy.log"
    if raw.is_file() and not raw.is_symlink():
        lines = [line for line in raw.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
        put(at / "egress.log", mask_secrets("".join(f"{line}\n" for line in lines), masks))
        raw.unlink()


# ---------------------------------------------------------------- the bot lifecycle (teamlib.engine, engine.BOX)

# What team bot does here in bwrap mode; reset and remove stay engine's (archive and remove env/<member>, box/ in it,
# links kept as links), and call stop, drop and create here.
ACTIONS = ("create", "stop", "inspect", "login")


def bot_command(ini, action: str, member: str, decl: dict, rest: list[str]) -> None:
    {"create": create, "stop": stop, "inspect": inspect, "login": login}[action](ini, member, decl, rest)


def make_box(member: str) -> Path:
    """env/<member>/box, the bot's /bot, readable by this user only; logs/ next to it."""
    box = env_dir() / member / "box"
    (env_dir() / member / "logs").mkdir(parents=True, exist_ok=True)
    box.mkdir(mode=0o700, exist_ok=True)
    box.chmod(0o700)
    return box


def create(ini, member: str, decl: dict, rest: list[str]) -> None:
    if decl.get("status", "active") not in ("active", "trial"):
        die(f"{member} is {decl.get('status')}; the human approves it first (set status: active)")
    box = make_box(member)
    ensure_prefix(ini)
    lead_log("-", "done", f"created {member}")
    say(f"{display(ini, decl)} is ready: folder {box} (bwrap sandbox, seen as /bot)")


def stop(ini, member: str, decl: dict, rest: list[str]) -> None:
    """stopped and ctl/stopped for each running task: the run script ends the agent CLI with TERM and still hands
    back what it did. Each sandbox still running STOP_GRACE s later is killed with its process group."""
    groups, stopped = [], 0
    for state in (env_dir() / member / "tasks").glob("*/result.json"):
        result = read_json(state)
        if result.get("state") != "running":
            continue
        put(state.parent / "stopped", "")
        ctl = state.parent / "ctl"
        if ctl.is_dir() and not ctl.is_symlink():
            put(ctl / "stopped", "")
        stopped += 1
        groups += [result["group"]] if isinstance(result.get("group"), int) else []
    deadline = time.monotonic() + STOP_GRACE
    while groups and time.monotonic() < deadline:
        time.sleep(0.2)
        groups = [group for group in groups if signal_group(group, 0)]
    for group in groups:
        signal_group(group, 9)
    lead_log("-", "done", f"stopped {member} ({stopped} running)")
    say(f"{display(ini, decl)}: stopped {stopped} running task(s)")


def drop(member: str) -> None:
    """Remove env/<member> after engine archived it, for reset and remove: the bot owns its box and may have made
    folders in it read-only, so they are opened first (teamlib.run.remove_tree, no link followed); a storage that
    stays is an error, never a done."""
    try:
        run.remove_tree(env_dir() / member)
    except OSError as error:
        die(f"the storage of {member} could not be removed ({type(error).__name__}); its archive is kept")
    if os.path.lexists(env_dir() / member):
        die(f"the storage of {member} could not be removed; its archive is kept")


def signal_group(group: int, signal: int) -> bool:
    """Whether the process group was there for the signal (0: only asks)."""
    try:
        os.killpg(group, signal)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def inspect(ini, member: str, decl: dict, rest: list[str]) -> None:
    """The names in a folder of the box (or the one file), shown as /bot/...: a folder ends in /, a link in @. No
    link is followed, so nothing outside the box is listed."""
    box = env_dir() / member / "box"
    parts = [part for part in (rest[0] if rest else "").split("/") if part not in ("", ".")]
    lead_log("-", "done", f"inspected {member} /bot/{'/'.join(parts)}")
    if not box.is_dir() or box.is_symlink():
        die(f"{member} has no storage yet: team bot create {member}")
    here = box
    for part in parts:
        here = here / part
        if part == ".." or here.is_symlink() or not here.exists():
            die(f"/bot/{visible('/'.join(parts))}: not in the storage of {member} (links are not followed)")
    found = sorted(here.iterdir()) if here.is_dir() else [here]
    say("\n".join("/bot/" + visible("/".join(path.relative_to(box).parts))
                  + ("@" if path.is_symlink() else "/" if path.is_dir() else "") for path in found))


def in_box(member: str, script: str, **streams) -> subprocess.CompletedProcess:
    """script in a sandbox on the bot's storage alone: box as /bot, HOME=/bot/home, its own passwd, no network and
    no variable of this computer's environment but TERM."""
    box = make_box(member)
    with tempfile.TemporaryDirectory() as etc:
        put(Path(etc) / "passwd", f"bot:x:{os.getuid()}:{os.getgid()}:bot:/bot/home:/bin/sh\n")
        put(Path(etc) / "group", f"bot:x:{os.getgid()}:\n")
        argv = box_argv({"passwd": str(Path(etc) / "passwd"), "group": str(Path(etc) / "group"), "box": str(box),
                         "prefix": str(prefix_dir() / "opt")}, "none")
        term = os.environ.get("TERM", "")
        shown = ["--setenv", "TERM", term] if re.fullmatch(r"[A-Za-z0-9._+-]{1,64}", term) else []
        return subprocess.run([shutil.which("bwrap") or "bwrap", *argv[1:-1], "--setenv", "PATH", INSIDE_PATH,
                               "--setenv", "HOME", "/bot/home", *shown, "--", "sh", "-c", script], env={}, **streams)


def login_check(member: str, test: str) -> bool:
    """teamlib.engine.login_check in bwrap mode: the runner's login_check in a sandbox on the box."""
    try:
        return in_box(member, test, stdin=subprocess.DEVNULL, capture_output=True).returncode == 0
    except OSError:
        return False


def login(ini, member: str, decl: dict, rest: list[str]) -> None:
    """The runner's login, interactive, in a sandbox on the bot's own storage; the login stays there and never
    reaches this computer. Then its login_check, the same way."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        die(f"'team bot login {member}' needs a person: run it in your own terminal, not through an agent")
    runner = engine.pick_runner(ini, decl.get("runner"))
    command = get(ini, f"runner.{runner}", "login")
    if not engine.uses_login(ini, runner) or not command:
        die(f"[runner.{runner}] needs auth = login and a login command for this")
    if not bwrap_usable():
        die(UNUSABLE)
    done = in_box(member, f'mkdir -p "$HOME" && {command}')
    ok = engine.login_check(ini, member, runner)
    lead_log("-", "done" if done.returncode == 0 and ok is not False else "failed", f"login {member} ({runner})")
    say(f"{display(ini, decl)}: login {'done' if ok else 'not confirmed' if ok is None else 'missing'}")


# ---------------------------------------------------------------- the team prefix (team image, bot create)

# env/_prefix stands in for the team image: opt/ is /opt/wouf in every bot sandbox (read-only, /opt/wouf/bin first
# on PATH), stamp.json the recipe hash it was built from, build.log the last build's output, masked. The recipe is
# the image's without root: the runners' install lines, the connectors' and team/provision.sh, each run as this user
# in a build sandbox with no network of its own, behind the egress proxy ([egress] allow and the install hosts), the
# new prefix writable at /opt/wouf and a HOME that is thrown away. [environment] setup and a line that fails for lack
# of root are skipped and named; nothing is ever retried as root. Its output is masked like a run's: bin/team gave the
# sinks every stored value before any command.
PREFIX = "_prefix"
NEEDS_ROOT = "needs root: ask the human to install it on this computer"
# What a line says when it failed for lack of root: a host folder it may not write, a package manager's lock, sudo.
ROOT_SAID = re.compile(r"permission denied|read-only file system|operation not permitted|are you root|must be root|"
                       r"need to be root|run as root|superuser|root privileges|eacces|could not open lock file|"
                       r"unable to acquire|no new privileges|^sudo: ", re.IGNORECASE | re.MULTILINE)
# The registries of the installers a line names, besides each URL's host.
INSTALL_HOSTS = {"npm": ("registry.npmjs.org",), "npx": ("registry.npmjs.org",),
                 "pip": ("pypi.org", "files.pythonhosted.org"), "pip3": ("pypi.org", "files.pythonhosted.org")}
BUILD_HOME, LINE_LIMIT = "/home/wouf", 1800


def prefix_dir() -> Path:
    return env_dir() / PREFIX


def prefix_recipe(ini) -> tuple[list[str], list[str]]:
    """(the lines a build runs, the lines it skips): the image recipe's RUN lines, setup skipped."""
    runners, warnings = engine.image_runners(ini)
    for warning in warnings:
        say(f"team: {warning}", err=True)
    if "auto" in runners:
        die(engine.NEEDS_RUNNER)
    lines = [get(ini, f"runner.{runner}", "install") for runner in sorted(runners)]
    for name in sorted({c for decl in active_bots() for c in words(decl.get("connectors", ""))}):
        try:
            lines.append(engine.connector(name).get("install", ""))
        except SystemExit:  # already named on stderr; only the bot that lists it is stopped, when it runs
            continue
    provision = team_dir() / "provision.sh"
    if provision.is_file():
        lines += [line for line in provision.read_text(encoding="utf-8").splitlines()
                  if not line.lstrip().startswith("#")]
    setup = get(ini, "environment", "setup")
    return [line.strip() for line in lines if line and line.strip()], [setup.strip()] if setup else []


def install_hosts(lines: list[str]) -> list[str]:
    """The hosts the lines name: each URL's host name (no address), and the registry of an installer they use."""
    hosts: list[str] = []
    for line in lines:
        found = [host.lower().rstrip(".") for host in re.findall(r"https?://([A-Za-z0-9.-]+)", line)]
        found += [host for word in re.findall(r"[\w.-]+", line) for host in INSTALL_HOSTS.get(word, ())]
        hosts += [host for host in found if engine.EGRESS_HOST.fullmatch(host)
                  and not host.rsplit(".", 1)[-1][:1].isdigit() and host not in hosts]
    return hosts


def prefix_command(ini) -> int:
    """team image in bwrap mode."""
    say(str(ensure_prefix(ini)))
    return 0


def ensure_prefix(ini) -> Path:
    """env/_prefix/opt, built when its stamp does not match the recipe. A failed line keeps the prefix there was."""
    lines, skipped = prefix_recipe(ini)
    home, digest = prefix_dir(), hashlib.sha256(json.dumps(lines).encode()).hexdigest()[:16]
    stamp = read_json(home / "stamp.json")
    if stamp.get("recipe") == digest and (home / "opt").is_dir() and not (home / "opt").is_symlink():
        skipped_lines(stamp.get("skipped", skipped))
        return home / "opt"
    say("team: building the team prefix env/_prefix (only when its recipe changes)", err=True)
    home.mkdir(parents=True, exist_ok=True)
    new = home / "opt.new"
    run.remove_tree(new)
    new.mkdir(mode=0o755)
    log: list[str] = []
    try:
        failed = build_lines(ini, lines, new, skipped, log)
    finally:
        put(home / "build.log", "".join(log))
    if failed:
        run.remove_tree(new)
        lead_log("-", "failed", "team prefix")
        die(f"the team prefix was not built: {visible(failed)!r} failed; the prefix there was is kept, the output "
            f"is in {home / 'build.log'}:\n" + "\n".join(visible(line) for line in log[-1][-1500:].splitlines()))
    run.remove_tree(home / "opt.old")
    if (home / "opt").exists() or (home / "opt").is_symlink():
        (home / "opt").rename(home / "opt.old")
    new.rename(home / "opt")
    run.remove_tree(home / "opt.old")
    put(home / "stamp.json", json.dumps({"recipe": digest, "skipped": skipped}, indent=2) + "\n")
    lead_log("-", "done", "team prefix built")
    skipped_lines(skipped)
    return home / "opt"


def skipped_lines(skipped: list[str]) -> None:
    for line in skipped:
        say(f"team: skipped {visible(line)!r}: {NEEDS_ROOT}", err=True)


def build_lines(ini, lines: list[str], new: Path, skipped: list[str], log: list[str]) -> str | None:
    """Each line in a build sandbox, behind the proxy when there is one; the first line that failed, not for lack
    of root (it joins skipped), or None."""
    if not lines:
        return None
    stage = prefix_dir() / "build"
    run.remove_tree(stage)
    for sub in ("ctl", "egress", "etc", "home"):
        (stage / sub).mkdir(mode=0o700, parents=True)
    put(stage / "etc" / "passwd", f"wouf:x:{os.getuid()}:{os.getgid()}:wouf:{BUILD_HOME}:/bin/sh\n")
    put(stage / "etc" / "group", f"wouf:x:{os.getgid()}:\n")
    spec = {"stage": str(stage), "bwrap_egress": build_proxy(ini, lines, stage)}
    if spec["bwrap_egress"]:
        proxy_up(spec)
    try:
        for line in lines:
            code, said = build_line(stage, new, line, bool(spec["bwrap_egress"]))
            log.append(f"$ {line}\n{said}\n[exit {code}]\n")
            if code and ROOT_SAID.search(said):
                skipped.append(line)
            elif code:
                return line
    finally:
        proxy_down(spec)  # its log masked by the sinks, which bin/team gave every stored value
        run.remove_tree(stage / "home")
    return None


def build_proxy(ini, lines: list[str], stage: Path) -> dict | None:
    """The proxy of a build: [egress] proxy into ctl/, the list [egress] allow plus the install hosts; None without
    [egress] proxy (the lines then have no network at all)."""
    proxy = get(ini, "egress", "proxy")
    source = (PROJECT / proxy).resolve() if proxy else None
    if source is None or source.is_symlink() or not source.is_file():
        say("team: no [egress] proxy: the prefix build has no network", err=True)
        return None
    put(stage / "ctl" / PROGRAM, source.read_text(encoding="utf-8"))
    allow = engine.egress_base(ini)
    allow += [host for host in install_hosts(lines) if host not in allow]
    say(f"team: the prefix build reaches: {' '.join(allow) or 'nothing but the proxy'}", err=True)
    return {"allow": allow}


def build_line(stage: Path, new: Path, line: str, egress: bool) -> tuple[int, str]:
    """(exit code, output) of one line in the build sandbox: the new prefix writable at /opt/wouf, HOME writable,
    the host's programs read-only, no network but the relay to the proxy, an empty environment but PATH, HOME,
    NPM_CONFIG_PREFIX and the proxy's variables. Its own PID namespace: nothing it starts outlives it."""
    binds = {"passwd": stage / "etc" / "passwd", "group": stage / "etc" / "group", "ctl": stage / "ctl"}
    argv = box_argv({name: str(path) for name, path in binds.items()}, "none")
    proxy = [arg for name, value in BOT_PROXY.items() for arg in ("--setenv", name, value)] if egress else []
    argv = [shutil.which("bwrap") or "bwrap", *argv[1:-1], "--bind", str(new), "/opt/wouf", "--bind",
            str(stage / "home"), BUILD_HOME, "--chdir", BUILD_HOME, "--setenv", "PATH", INSIDE_PATH, "--setenv", "HOME",
            BUILD_HOME, "--setenv", "NPM_CONFIG_PREFIX", "/opt/wouf", *proxy, "--", "sh", "-c",
            (RELAY if egress else "") + line]
    try:
        done = subprocess.run(argv, env={}, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=LINE_LIMIT)
    except subprocess.TimeoutExpired as late:
        return 124, (late.stdout or b"").decode("utf-8", "replace") + f"\n(stopped after {LINE_LIMIT} s)"
    return done.returncode, done.stdout.decode("utf-8", "replace")


# ---------------------------------------------------------------- the gate (teamlib.gate.BOX)

GATE_LIMITS = ["4g", "2"]


def gate_check(ini) -> None:
    GATE_LIMITS[:] = [get(ini, "environment", "memory", "4g"), get(ini, "environment", "cpus", "2")]
    network = get(ini, "flow", "gate_network", "none") or "none"
    if network != "none":
        die(f"gate_network = {visible(network)} is refused in bwrap mode; use none")
    if not bwrap_usable():
        die(UNUSABLE)


def gate_run(stage, cache, ref, head, command, start, script, limit, log) -> int:
    """A disposable checkout and home, with only the copied cache writable on the host."""
    hooks = stage / "hooks"
    hooks.mkdir()
    hooks.chmod(0o555)
    argv = box_argv({"bundle": str(stage / "repo.bundle")}, "none")
    env = {"PATH": INSIDE_PATH, "HOME": "/bot/home", "BUNDLE": "/repo.bundle", "GATE": command,
           "WOUF_BASE": start, "WOUF_HEAD": head, "WOUF_CACHE": "/bot/cache",
           "GIT_CONFIG_GLOBAL": "/bot/home/.gitconfig", "GIT_CONFIG_NOSYSTEM": "1", "LANG": "C.UTF-8"}
    setup = 'mkdir -p /bot/home /bot/work && : > "$GIT_CONFIG_GLOBAL" && cd /bot/work && ' + script
    argv = [shutil.which("bwrap") or "bwrap", *argv[1:-1], "--tmpfs", "/bot", "--ro-bind", str(hooks),
            "/bot/hooks", "--bind", str(cache), "/bot/cache",
            *(arg for key, value in env.items() for arg in ("--setenv", key, value)),
            "--", "timeout", "-k", "1", str(limit), "sh", "-c", setup]
    limited = limit_argv(argv, *GATE_LIMITS)
    return gate.run_masked(limited, log, limit + 10, env=scope_environment() if limited is not argv else {})


getattr(gate, "BOX", [])[:] = [sys.modules[__name__]]


# team run reaches this module only through this hook (teamlib.run.sandbox); a run.py without it (main's, which
# tests/test_run_prepare swaps in) is left as it is.
getattr(run, "BOX", [])[:] = [sys.modules[__name__]]
# team bot the same way, through engine.BOX.
getattr(engine, "BOX", [])[:] = [sys.modules[__name__]]
