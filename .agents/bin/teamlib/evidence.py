"""teamlib.evidence: what a receipt needs, read when it happens (receipts v1, SPEC 2.1). A flow step keeps its run's
commit, model, CLI, tokens and egress before a rerun deletes them; gate.json keeps the gate's network, command and
complexity. All the parsing is here. Only names from the config, hex, counts and hosts the egress proxy allowed come
out, never a string a bot chose. A leaf: teamlib.gate and teamlib.flow reach it through gate.EVIDENCE, which this
module fills when bin/team loads it (as teamlib.bwrap fills gate.BOX)."""

from __future__ import annotations

from teamlib.core import *  # noqa: F401,F403
from teamlib import gate
from teamlib.engine import EGRESS_HOST, runner_tools
from teamlib.run import BOXED
from teamlib.usage import TOKENS

UNKNOWN = "unknown"
SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
SHA256 = re.compile(r"[0-9a-f]{64}")
# The networks a receipt names (SPEC 2.1 gate.network); any other value is unknown.
NETWORKS = ("none", "bridge", "host")
# One line of the egress proxy (egress_proxy.py, ProxyHandler._log) and nothing else: a line of any other shape (the
# proxy's own messages, a traceback, a line cut short) is not read. Its host field is the proxy's LOGGABLE (which
# holds `-`); a line of this shape with any other host field (a host masked to ***) is not the proxy's either.
PROXY_LINE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z host=(\S+) port=([0-9]{1,5}|-) "
                        r"result=(allowed|refused) reason=([a-z0-9-]+) bytes_out=[0-9]+ bytes_in=[0-9]+", re.ASCII)
LOGGABLE = re.compile(r"[a-z0-9.:\[\]-]{1,255}", re.ASCII)  # egress_proxy.py LOGGABLE
# The summary line of scripts/complexity.py compare; its ACCEPTED lines (the escapes) come before it.
COMPLEXITY = re.compile(r"([0-9]+) files, ([0-9]+) functions, ([0-9]+) failures", re.ASCII)  # spec: ^(\d+) ...$
COMPLEXITY_KEYS = ("files", "functions", "failures", "accepted")
# The longest line split_lines holds, in bytes; a proxy line is under 400 (its host field is at most 255).
LINE_MAX = 64 * 1024


def opened(path: Path):
    """A binary handle on a regular file the lead wrote, opened without following a link; None for a link, a missing
    file or anything else (a FIFO, a folder, a link swapped in after the check)."""
    if path.is_symlink() or not path.is_file():
        return None
    try:
        handle = os.fdopen(os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), "rb")
    except OSError:
        return None
    if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
        handle.close()
        return None
    return handle


def split_lines(handle, digest=None):
    """The lines of an opened file, split at \\n only (a \\r stays in its line), decoded as UTF-8 with errors
    replaced; one pass, each line's bytes added to digest as read, so digest covers exactly the bytes of the file.
    Memory stays bounded whatever the file (a bot may place an oversized egress.log): a line is read LINE_MAX bytes
    at a time, and a line longer than LINE_MAX comes out as None, its bytes digested chunk by chunk, never held."""
    with handle:
        while True:
            raw = handle.readline(LINE_MAX + 1)
            if not raw:
                return
            if digest is not None:
                digest.update(raw)
            if len(raw) > LINE_MAX and not raw.endswith(b"\n"):  # an overlong line: digest the rest, keep nothing
                while raw and not raw.endswith(b"\n"):
                    raw = handle.readline(LINE_MAX + 1)
                    if digest is not None:
                        digest.update(raw)
                yield None
                continue
            line = raw.decode("utf-8", "replace")
            yield line[:-1] if line.endswith("\n") else line


def lines_of(path: Path):
    """The lines of a file the lead wrote (split_lines, an overlong line as None); none for a link."""
    handle = opened(path)
    if handle is not None:
        yield from split_lines(handle)


# ---------------------------------------------------------------- a run

def egress_mode(spec: dict) -> str:
    """How a run reached the network, from its run.json: proxy with network = egress (container: egress; bwrap:
    bwrap_egress), else its network (container: bridge or none; bwrap: none), host for host mode, else unknown (a
    skipped run's run.json has no mode)."""
    current = spec.get("mode")
    if spec.get("bwrap_egress") or (current == "container" and spec.get("egress")):
        return "proxy"
    if current == "host":
        return "host"
    network = spec.get("network")
    return network if network in {"container": ("bridge", "none"), "bwrap": ("none",)}.get(current, ()) else UNKNOWN


def egress_summary(stage: Path, spec: dict) -> dict:
    """A run's egress (SPEC 2.1 runs[].egress): {mode, log, hosts, private, refused}. Only a proxy run reads its log,
    the egress.log the lead collected into the run folder, never the bot's output: hosts are the sorted host:port
    pairs the proxy allowed with reason=ok, private counts its reason=private lines, refused its refusals. A refused
    host is a name a bot chose: it is counted, never kept. log is the sha256 (64 lower-case hex) of the egress.log
    bytes, read in the same single pass, when the log is complete: every allowed reason=ok line names a host and
    port. An allowed line whose host:port is no host name and port (masked, or not the proxy's LOGGABLE) is never
    kept nor replaced; it makes log "unknown", as the log does not name every public host the run reached (so no
    sovereign flag rests on it); so does a line longer than LINE_MAX, which is never held. log is null (None)
    without a proxy, without a log file, or when egress.log is a link."""
    mode = egress_mode(spec)
    summary = {"mode": mode, "log": None, "hosts": [], "private": 0, "refused": 0}
    handle = opened(Path(stage) / "egress.log") if mode == "proxy" else None
    if handle is None:
        return summary
    hosts, whole, digest = set(), True, hashlib.sha256()
    for line in split_lines(handle, digest):  # every line is read: the digest covers the whole file
        if line is None:  # longer than any proxy line: the log is not read whole, so it is not complete
            whole = False
            continue
        found = PROXY_LINE.fullmatch(line)
        if not found:
            continue
        host, port, result, reason = found.groups()
        named = EGRESS_HOST.fullmatch(host) and not host.startswith(".") and port != "-"
        if result == "refused":  # counted whatever its host field (the log is masked: ***), never kept
            summary["refused"] += 1
        elif reason == "private":  # the same for a private connection
            summary["private"] += 1
        elif result == "allowed" and reason == "ok" and not named:
            whole = False
        elif not LOGGABLE.fullmatch(host):
            continue
        elif reason == "ok":
            hosts.add(f"{host}:{port}")
    summary.update(log=digest.hexdigest() if whole else UNKNOWN, hosts=sorted(hosts))
    return summary


def cli_name(ini, runner: str | None, current) -> str:
    """The program name of the CLI a runner runs in mode current, as the config has it now, never a path: as
    run.py picks it, command in a box (container, bwrap), else host_command, else command; wrappers removed."""
    section = f"runner.{runner}"
    current = current if current in ("container", "bwrap", "host") else get(ini, "environment", "mode")
    try:
        command = runner and (get(ini, section, "command" if current in BOXED else "host_command")
                              or get(ini, section, "command"))
        program = (runner_tools(command) or [None])[-1] if command else None
    except ValueError:  # a command shlex cannot split
        program = None
    return (Path(program).name or UNKNOWN) if program else UNKNOWN


def run_record(ini, stage: Path, spec: dict, result: dict, runner: str | None) -> dict:
    """What a flow step keeps of its run (SPEC 2.1 runs[]): the commit it was given and its model, as run.json holds
    them; its CLI; its tokens as result.json holds them (a usage file, else the adapter line), else unknown; and its
    egress. Read right after the run, before a rerun of the same bot and task deletes them."""
    commit, tokens = str(spec.get("commit") or ""), str(result.get("tokens") or "")
    return {"commit": commit if SHA.fullmatch(commit) else UNKNOWN, "model": str(spec.get("model") or UNKNOWN),
            "cli": cli_name(ini, runner, spec.get("mode")), "tokens": tokens if TOKENS.fullmatch(tokens) else UNKNOWN,
            "egress": egress_summary(stage, spec)}


def unknown_run(ini, runner: str | None) -> dict:
    """A bot step that never ran (no login, or its run raised): nothing of a run is known but its runner's CLI; its
    egress log is null (no log was collected)."""
    return {"commit": UNKNOWN, "model": UNKNOWN, "cli": cli_name(ini, runner, None), "tokens": UNKNOWN,
            "egress": {"mode": UNKNOWN, "log": None, "hosts": [], "private": 0, "refused": 0}}


def never_ran(ini, step: dict, act: dict) -> dict:
    """What a bot step lacks when it never ran (no login, or its run raised; flow_step gives every run its
    run_record): unknown_run, with the runner of the step, else of its act. Nothing for a gate step or one that has
    its evidence."""
    if step.get("kind") == "gate" or "egress" in step:
        return {}
    return unknown_run(ini, step.get("runner") or act.get("runner"))


# ---------------------------------------------------------------- the gate

def gate_network(ini) -> str:
    """The network the gate command runs on: container mode passes --network [flow] gate_network (none by default),
    bwrap mode refuses any but none, host mode runs it on this computer's own; else unknown."""
    current = get(ini, "environment", "mode")
    if current == "container":
        network = get(ini, "flow", "gate_network", "none") or "none"
        return network if network in NETWORKS else UNKNOWN
    return {"bwrap": "none", "host": "host"}.get(current, UNKNOWN)


def complexity_of(log: Path) -> dict | None:
    """The complexity ratchet's numbers in the gate's log: the first line of its summary's shape, and the ACCEPTED
    lines before it. Only the first counts: the gate runs the ratchet before the tests of the code a bot wrote
    (scripts/pre-gate.sh), so a line that code prints comes later and changes nothing. None without such a line, or
    after a line longer than LINE_MAX (no ratchet line is; the count before it would not be sure)."""
    accepted = 0
    for line in lines_of(log):
        if line is None:
            return None
        found = COMPLEXITY.fullmatch(line)
        if found:  # the first such line, whatever its numbers; one int() refuses (too many digits) leaves it unknown
            try:
                return dict(zip(COMPLEXITY_KEYS, [*(int(n) for n in found.groups()), accepted]))
            except ValueError:
                return None
        accepted += line.startswith("ACCEPTED ")
    return None


def plain_names(names: str) -> bool:
    """Whether the changed paths (git diff -z --name-only, NUL-separated) hold no control character. The ratchet
    prints paths unescaped, so a path with a newline a bot chose could put a summary line before the real one."""
    return not any(ord(c) < 0x20 and c != "\0" or ord(c) == 0x7F for c in names)


def gate_record(ini, command: str, log: Path, names: str = "") -> dict:
    """What gate.json keeps (SPEC 2.1 gate): network, the sha256 of [flow] gate, and complexity when the log has it
    and no changed path (names, NUL-separated) can forge its lines."""
    found = complexity_of(log) if plain_names(names) else None
    return {"network": gate_network(ini), "command_sha256": hashlib.sha256(command.encode("utf-8")).hexdigest(),
            **({"complexity": found} if found else {})}


def hex_of(value, rule: re.Pattern) -> str:
    """value when it is text of rule's shape (lower-case hex), else unknown."""
    return value if isinstance(value, str) and rule.fullmatch(value) else UNKNOWN


def complexity_kept(found) -> dict | str:
    """gate.json's complexity when it has exactly the four whole numbers, else unknown."""
    whole = isinstance(found, dict) and set(found) == set(COMPLEXITY_KEYS) and \
        all(type(found[k]) is int and found[k] >= 0 for k in COMPLEXITY_KEYS)
    return {k: found[k] for k in COMPLEXITY_KEYS} if whole else UNKNOWN


def gate_step(state: dict) -> dict:
    """What a flow's gate step keeps of gate.json: base, code, network, command_sha256 and complexity, each of its own
    shape, else unknown (the next gate run overwrites gate.json). code is the gate command's exit (an int), None when
    the gate had no command (gate.json holds null); command_sha256 is 64 lower-case hex."""
    network, code = state.get("network"), state.get("code", UNKNOWN)
    return {"base": hex_of(state.get("base"), SHA),
            "code": code if code is None or type(code) is int else UNKNOWN,
            "network": network if isinstance(network, str) and network in NETWORKS else UNKNOWN,
            "command_sha256": hex_of(state.get("command_sha256"), SHA256),
            "complexity": complexity_kept(state.get("complexity"))}


# teamlib.gate and teamlib.flow reach this module only through this hook (gate.evidence); a gate.py without it is
# left as it is.
getattr(gate, "EVIDENCE", [])[:] = [sys.modules[__name__]]
