"""teamlib.engine: the bot environment, runners and connectors, the team image and the bot lifecycle, with the
commands bot, install and image. The run, gate, flow and status modules use its helpers."""

from __future__ import annotations

import os
import shlex
import tempfile
import time
from datetime import timedelta

from teamlib import core
from teamlib.core import *  # noqa: F401,F403

# ---------------------------------------------------------------- environment

def mode(ini) -> str:
    value = get(ini, "environment", "mode", "undeclared")
    if value not in ("container", "host", "bwrap"):  # bwrap: teamlib.bwrap plugs into run and here
        die("the bot environment is not set up yet (mode = undeclared): follow .agents/skills/setup.md")
    return value

def engine(ini) -> str:
    return get(ini, "environment", "engine", "docker") or "docker"

def where(ini) -> dict:
    """The container engine and where it runs; a run keeps a copy so it never re-reads the settings.
    Every container command starts here, so image and user are checked first."""
    image(ini), user(ini)
    return {"engine": engine(ini), "docker_host": get(ini, "environment", "docker_host")}

def engine_env(at) -> dict:
    at = at if isinstance(at, dict) else where(at)
    env = dict(os.environ)
    if at["docker_host"]:
        env["DOCKER_HOST"] = at["docker_host"]
        env["CONTAINER_HOST"] = at["docker_host"]
    return env

# fetch_masked in the kernel reaches the engine through these: the kernel imports no command module.
core.where, core.engine_env = where, engine_env
BOX: list = []  # mode = bwrap: teamlib.bwrap, as in teamlib.run.BOX: team bot create, stop, inspect and login

def sh(at, args: list[str], check: bool = True, input_text: str | None = None):
    """Run the container engine; `at` is the settings or a run's copy of where()."""
    at = at if isinstance(at, dict) else where(at)
    result = subprocess.run([at["engine"], *args], env=engine_env(at), text=True, capture_output=True, input=input_text)
    if check and result.returncode:
        die(f"{at['engine']} {args[0]} failed: {(result.stderr or result.stdout or '').strip()[-400:]}",
            result.returncode)
    return result

# docker_host = ssh://[user@]alias[:port][/socket]: a pipeline runs there with one ssh connection. Only a plain alias,
# user and port are used, so no part of it can become an ssh option; anything else runs the pipeline here.
SSH_HOST = re.compile(r"ssh://(?:([A-Za-z0-9_][A-Za-z0-9._-]{0,31})@)?([A-Za-z0-9][A-Za-z0-9._-]{0,252})"
                      r"(?::([0-9]{1,5}))?(?:/[A-Za-z0-9._/-]*)?")
# One argument of a process may not be longer than this (Linux: 128 KiB); a longer pipeline runs here.
SSH_COMMAND = 120_000
# A dead connection ends in 45 s, an unreachable host fails in 20 s (TCP keepalive alone took 2 h, 2026-11-02).
SSH_ALIVE = ["-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", "-o", "ConnectTimeout=20"]

def shell(at: dict, script: str, args: list[str]) -> list[str]:
    """The command that runs a fixed sh script with args as its positional parameters where the engine is: over one
    ssh connection when docker_host is an ssh alias, else here (the engine then reaches its host itself). Every value
    is quoted for the remote shell; nothing secret is ever part of it (keys go on stdin). The ssh key is the one the
    engine itself uses (CONTAINER_SSHKEY), so the pipeline reaches the same host the engine's ssh:// address does."""
    found = SSH_HOST.fullmatch(at.get("docker_host") or "")
    command = shlex.join(["sh", "-c", script, "sh", *args])
    if not found or len(command) > SSH_COMMAND:
        return ["sh", "-c", script, "sh", *args]
    user, host, port = found.groups()
    key = os.environ.get("CONTAINER_SSHKEY")
    return ["ssh", "-T", *(["-l", user] if user else []), *(["-p", port] if port else []),
            *(["-i", key, "-o", "IdentitiesOnly=yes"] if key else []), *SSH_ALIVE, "--", host, command]

# ---------------------------------------------------------------- the stamp

# What the engine's host is known to hold, so a run does not ask it again: the recipe digest of the team image and
# the bot storages that exist. Names and digests only. A run checks both again in its own pipeline; when one is
# missing it clears the stamp and makes them again. team image, bot reset and bot remove clear it too.
STAMP = "engine-stamp.json"

def stamp_of(at: dict) -> dict:
    data = read_json(env_dir() / STAMP)
    if data.get("at") != f"{at['engine']} {at['docker_host']}":
        data = {}
    images, volumes = data.get("images"), data.get("volumes")
    return {"images": images if isinstance(images, dict) else {},
            "volumes": volumes if isinstance(volumes, list) else []}

def stamp_note(at: dict, image: str | None = None, digest: str | None = None, storage: str | None = None,
               drop: bool = False) -> None:
    """Note (or with drop, forget) an image digest or a storage in the stamp."""
    data = stamp_of(at)
    if image:
        data["images"].pop(image, None)
        if not drop:
            data["images"][image] = digest
    if storage:
        data["volumes"] = [v for v in data["volumes"] if v != storage] + ([] if drop else [storage])
    env_dir().mkdir(parents=True, exist_ok=True)
    write_json(env_dir() / STAMP, {"at": f"{at['engine']} {at['docker_host']}", **data})

def stamp_clear() -> None:
    (env_dir() / STAMP).unlink(missing_ok=True)

def plain(ini, key: str, default: str, pattern: re.Pattern, shape: str) -> str:
    """[environment] key (or what [DEFAULT] passes on), only when it is a plain value: it becomes part of the
    Dockerfile and of container commands, where a line break or shell character would add commands."""
    value = get(ini, "environment", key, default)
    if not pattern.fullmatch(value):
        die(f"team/config.ini [environment] {key} must be a plain {shape}, not '{visible(value)}'; fix it there")
    return value

def image(ini) -> str:
    return plain(ini, "image", "node:lts", IMAGE, "image reference (registry/name[:tag][@sha256:digest])")

def user(ini) -> str:
    return plain(ini, "user", "node", USER, "user name or uid[:gid]")

def image_tag(ini) -> str:
    return f"{base(ini)}-image"

def volume(ini, member: str) -> str:
    return f"{base(ini)}-{member}"

def labels(ini, member: str | None = None, task: str | None = None) -> list[str]:
    out = ["--label", f"wouf.team={base(ini)}"]
    if member:
        out += ["--label", f"wouf.bot={member}"]
    if task:
        out += ["--label", f"wouf.task={task}"]
    return out

# ---------------------------------------------------------------- runners and connectors

def runner_names(ini) -> list[str]:
    return [s.split(".", 1)[1] for s in ini.sections() if s.startswith("runner.")]

def runner_tools(command: str) -> list[str]:
    """Wrappers then CLI; sh -c uses the catalog's SCRIPT CLI ARGS convention,
    or a simple 'exec CLI ARGS' script without a separate name."""
    words, tools = shlex.split(command), []
    while words:
        first = words.pop(0)
        if first in ("exec", "--") or ("=" in first and first.split("=", 1)[0].isidentifier()):
            continue
        tools.append(first)
        if Path(first).name in ("dbus-run-session", "env", "nice"):
            continue
        if Path(first).name in ("sh", "bash") and words[:1] == ["-c"] and len(words) >= 2:
            words = words[2:] if len(words) > 2 else shlex.split(words[1])
            continue
        return list(dict.fromkeys(tools))
    return []  # A wrapper without a program is not a configured runner.

def cli_of(ini, name: str) -> str | None:
    """The host CLI, falling back to the container command, with wrappers removed."""
    command = get(ini, f"runner.{name}", "host_command") or get(ini, f"runner.{name}", "command")
    return (runner_tools(command) or [None])[-1]

def image_tools(ini, runners) -> list[str]:
    """The container command's wrappers and real CLI, then git and python3."""
    clis = []
    for name in sorted(runners):
        command = get(ini, f"runner.{name}", "command") or get(ini, f"runner.{name}", "host_command")
        clis.extend(runner_tools(command))
    return list(dict.fromkeys(clis + ["git", "python3"]))

# The tiers a run, a bot or a class names; mid is optional per runner.
TIERS = ("light", "mid", "strong")

def tier_model(ini, runner: str, tier: str, model: str | None = None) -> tuple[str, str]:
    """The model a tier names on a runner and the tier actually used, as the receipt names it: a runner without a
    mid model runs mid on strong (mid->strong), so a missing setting never lowers quality. An empty model is the
    CLI's own default; a model given for the run wins and the tier stays as named."""
    if tier not in TIERS:
        die(f"tier must be light, mid or strong, not {tier!r}")
    if model:
        return model, tier
    section = f"runner.{runner}"
    if tier == "mid" and not get(ini, section, "mid"):
        return get(ini, section, "strong"), tier_label(tier, "strong")
    return get(ini, section, tier), tier

def tier_label(asked: str, used: str) -> str:
    """The tier as a receipt names it: mid->strong (ASCII) when mid fell back."""
    return asked if asked == used else f"{asked}->{used}"

def tier_models(ini, runner: str) -> str:
    """The model per tier of a runner, as team runners shows it: mid=strong when mid falls back."""
    shown = {tier: get(ini, f"runner.{runner}", tier) or "default" for tier in ("strong", "light")}
    shown["mid"] = get(ini, f"runner.{runner}", "mid") or "strong"
    return " ".join(f"{tier}={shown[tier]}" for tier in ("strong", "mid", "light"))

def pick_runner(ini, *wishes: str | None) -> str:
    for wish in (*wishes, os.environ.get("WOUF_RUNNER"), get(ini, "team", "runner", "auto")):
        if wish and wish != "auto":
            if wish not in runner_names(ini):
                die(f"unknown runner {wish!r}; configured: {', '.join(runner_names(ini))}")
            return wish
    for variable, hinted in SESSION_HINTS:
        if os.environ.get(variable) and hinted in runner_names(ini):
            return hinted
    for candidate in runner_names(ini):
        cli = cli_of(ini, candidate)
        if cli and shutil.which(cli):
            return candidate
    die("no runner found: name one in [team] runner in team/config.ini (claude, codex, agy, local)")
    return ""

def connector(name: str) -> dict:
    # A bare catalog name only: a path such as ../team/import/<archive>/connectors/x would reach files nobody activated.
    if not CONNECTOR.match(name):
        die(f"unknown connector {name!r}: connector names are catalog names (lowercase letters, digits and -)")
    for folder in (team_dir() / "connectors", PACK / "connectors"):
        path = folder / f"{name}.md"
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            block = re.search(r"```json\n(.*?)\n```", text, re.S)
            if not block:
                die(f"connector {name}: no ```json server block in {path}")
            fields = frontmatter(path)
            fields.update({"name": name, "path": path, "server": json.loads(block.group(1))})
            return fields
    die(f"unknown connector {name!r}: add .agents/connectors/{name}.md or team/connectors/{name}.md")
    return {}

def mcp_entry(server: dict, fmt: str) -> dict:
    """Translate the neutral server block into the MCP config shape a CLI reads."""
    if server.get("transport") == "http":
        headers = server.get("headers", {})
        if fmt == "gemini":
            return {"httpUrl": server["url"], "headers": headers}
        return {"type": "http", "url": server["url"], "headers": headers}
    entry = {"command": server["command"], "args": server.get("args", [])}
    if server.get("env"):
        entry["env"] = server["env"]
    return entry

# ---------------------------------------------------------------- the team image

NEEDS_RUNNER = ("the team image needs to know the agent CLI: set [team] runner in .agents/team/config.ini "
                "(claude, codex, agy, local)")

def image_runners(ini) -> tuple[set, list]:
    """The agent CLIs the team image holds, from the settings only (never the session, PATH or WOUF_RUNNER):
    each active bot's runner, where auto means [team] runner. A broken declaration only gives a warning."""
    team = get(ini, "team", "runner", "auto") or "auto"
    found, warnings = set(), []
    for decl in active_bots() or [{"id": "team", "runner": team}]:
        wish = decl.get("runner") or "auto"
        name = team if wish == "auto" else wish
        if name != "auto" and name not in runner_names(ini):
            warnings.append(f"{decl['id']}: unknown runner {name!r}; it is left out of the team image")
            continue
        found.add(name)
    return found, warnings

def recipe(ini) -> str:
    base_image, who = image(ini), user(ini)
    runners, warnings = image_runners(ini)
    for warning in warnings:
        say(f"team: {warning}", err=True)
    if "auto" in runners:
        die(NEEDS_RUNNER)
    lines = [f"FROM {base_image}", "USER root"]
    setup = get(ini, "environment", "setup")
    if setup:
        lines.append(f"RUN {setup}")
    for runner in sorted(runners):
        install = get(ini, f"runner.{runner}", "install")
        if install:
            lines.append(f"RUN {install}")
    connectors = sorted({c for decl in active_bots() for c in words(decl.get("connectors", ""))})
    for name in connectors:
        try:
            install = connector(name).get("install", "")
        except SystemExit:
            # Already named on stderr; only the bot that lists it is stopped, when it runs.
            continue
        if install:
            lines.append(f"RUN {install}")
    provision = team_dir() / "provision.sh"
    if provision.is_file():
        for line in provision.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                lines.append(f"RUN {line.strip()}")
    # /bot/cache: a new gate cache volume takes its owner from the folder it is mounted on.
    lines += [f"RUN mkdir -p /bot/cache /wouf /outbox && chown {who} /bot /bot/cache /outbox", f"USER {who}",
              "WORKDIR /bot"]
    return "\n".join(lines) + "\n"

def ensure_image(ini, force: bool = False) -> str:
    return build_image(where(ini), image_tag(ini), recipe(ini), labels(ini), force)

def build_image(at: dict, tag: str, text: str, label_args: list[str], force: bool = False) -> str:
    digest = hashlib.sha256(text.encode()).hexdigest()[:16]
    current = sh(at, ["image", "inspect", "-f", '{{index .Config.Labels "wouf.recipe"}}', tag], check=False)
    if force or current.returncode or current.stdout.strip() != digest:
        say(f"team: building the team image {tag} (only when its recipe changes; this takes minutes once)", err=True)
        sh(at, ["build", "-q", "-t", tag, "--label", f"wouf.recipe={digest}", *label_args, "-"], input_text=text)
    stamp_note(at, image=tag, digest=digest)
    return tag

def ensure_volume(ini, member: str) -> str:
    return ensure_storage(where(ini), volume(ini, member), labels(ini, member))

def ensure_storage(at: dict, name: str, label_args: list[str]) -> str:
    if sh(at, ["volume", "inspect", name], check=False).returncode:
        sh(at, ["volume", "create", *label_args, name])
    stamp_note(at, storage=name)
    return name

# ---------------------------------------------------------------- a run's container, in one call each way

# What runs where the engine is to start a bot: one shell, fixed text, every value a positional parameter. The
# image and the storage are checked again (the stamp may be stale), the container is created, the keys are read
# from stdin up to the empty line (a shell's read takes no byte more, and no other call reads stdin), then the size
# of the one archive of the inputs and the pack, then exactly that many bytes of it (head -c reads no byte more;
# what cp leaves of them is read away, so the next line is the host's).
# The shell then says wouf-ready on stderr and waits for one line: go starts the bot, anything else (a stop came,
# or stdin was cut) removes the container and exits 130, so a stop before this point never lets the bot start. The
# keys go to the container on its stdin and stay in a variable of this shell: printf is a builtin, so they are in no
# argument, no environment and no file. A step that fails says which one on stderr and exits 125 before the bot starts.
START = """E=$1 C=$2 I=$3 V=$4
shift 4
fail() { printf 'wouf-setup %s\\n' "$1" >&2; exit 125; }
"$E" rm -f "$C" </dev/null >/dev/null 2>&1
"$E" image inspect "$I" </dev/null >/dev/null 2>&1 || fail image
"$E" volume inspect "$V" </dev/null >/dev/null 2>&1 || fail storage
"$E" create "$@" </dev/null >/dev/null || fail create
keys=
while IFS= read -r line; do keys="$keys$line
"; [ -z "$line" ] && break; done
drop() { "$E" rm -f "$C" </dev/null >/dev/null 2>&1; }
IFS= read -r size
case $size in ''|*[!0-9]*) drop; fail copy;; esac
head -c "$size" | ("$E" cp - "$C:/" >/dev/null; s=$?; cat >/dev/null; exit $s) || { drop; fail copy; }
printf 'wouf-ready\\n' >&2
IFS= read -r go
[ "$go" = go ] || { drop; exit 130; }
printf '%s' "$keys" | "$E" start -ai "$C" 2>&1
"""
READY = "wouf-ready\n"
# What runs there to bring the results back: the archive of /outbox on stdout, then the container is removed.
COLLECT = """E=$1 C=$2
"$E" cp "$C:/outbox" - </dev/null
s=$?
"$E" rm -f "$C" </dev/null >/dev/null 2>&1
exit $s
"""
SETUP = re.compile(r"^wouf-setup (\w+)$", re.M)
# After a lost stream: the exit code and log of the bot's container once it ends; 3: never started, 124: killed at $3 s.
REJOIN = """E=$1 C=$2
case $("$E" inspect -f '{{.State.Status}}' "$C" </dev/null 2>/dev/null) in running|exited|stopped) ;; *) exit 3;; esac
timeout "$3" "$E" wait "$C" </dev/null || { s=$?; [ $s = 124 ] && "$E" kill "$C" </dev/null >/dev/null 2>&1; exit $s; }
"$E" logs "$C" </dev/null 2>&1
"""

def box_ready(spec: dict, fresh: bool) -> None:
    """The image and the bot's storage, made only when the stamp does not know them (or fresh: it was wrong)."""
    at = spec["where"]
    known = {"images": {}, "volumes": []} if fresh else stamp_of(at)
    if known["images"].get(spec["image"]) != hashlib.sha256(spec["recipe"].encode()).hexdigest()[:16]:
        build_image(at, spec["image"], spec["recipe"], spec["image_labels"])
    if spec["storage"] not in known["volumes"]:
        ensure_storage(at, spec["storage"], spec["volume_labels"])
    if spec.get("egress"):
        # The proxy runs on the team image, so it starts after it; a stale stamp may name an image that is gone.
        if not fresh:
            egress_clean(at, spec["egress"]["team"])  # what killed supervisors left
        try:
            egress_up(spec)
        except SystemExit:
            if fresh:
                raise
            stamp_clear()
            box_ready(spec, True)

def box_command(spec: dict) -> list[str]:
    proxy = [a for v in EGRESS_ENV for a in ("-e", f"{v}={EGRESS_ENV[v]}")] if spec.get("egress") else []
    create = ["-i", "--name", spec["container"], *spec["run_labels"], *HARDEN, *proxy, "--network", spec["network"],
              "--cpus", spec["cpus"], "--memory", spec["memory"], "-v", f"{spec['storage']}:/bot", spec["image"],
              "sh", "-c", spec["script"], "sh", *spec["argv"]]
    return shell(at := spec["where"], START, [at["engine"], spec["container"], spec["image"], spec["storage"], *create])

def inbound(stage: Path, pack: Path, bundle: bool) -> list[tuple[Path, str]]:
    """Every folder and regular file a container gets, by its name in the archive: in/ as /inbox, the pack as /wouf,
    the run's bundle (bundle) as /repo.bundle. Links are not followed and not sent."""
    found = []
    for top, name in ((stage / "in", "inbox"), (pack, "wouf")):
        for root, dirs, files in os.walk(top):
            dirs.sort()
            here = Path(root)
            rel = here.relative_to(top).parts
            found.append((here, "/".join((name, *rel))))
            found += [(here / f, "/".join((name, *rel, f))) for f in sorted(files)]
    return [(path, "repo.bundle" if bundle and name == "inbox/repo.bundle" else name) for path, name in found]

def box_archive(spool, stage: Path, pack: Path, bundle: bool) -> int:
    """One archive of what the container gets, owned by root and readable, into spool. Returns its size."""
    with tarfile.open(fileobj=spool, mode="w|") as tar:
        for path, name in inbound(stage, pack, bundle):
            info = tar.gettarinfo(str(path), arcname=name)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode |= 0o555 if info.isdir() else 0o444
            if info.isdir():
                tar.addfile(info)
            elif info.isfile():
                with path.open("rb") as handle:
                    tar.addfile(info, handle)
    return spool.tell()

def send_box(proc, stdin_text: str, stage: Path, pack: Path | None, bundle: bool = False, ready=None) -> None:
    """The keys, then (container) the size of the archive and the archive down the pipe; then, once the other side
    is ready (ready is set), go, or stop when the run was stopped meanwhile. A pipe the other side closed ends it
    quietly: the run's exit code tells what happened."""
    try:
        proc.stdin.write(stdin_text.encode())
        if pack is not None:
            with tempfile.TemporaryFile() as spool:
                proc.stdin.write(f"{box_archive(spool, stage, pack, bundle)}\n".encode())
                spool.seek(0)
                for chunk in iter(lambda: spool.read(1 << 16), b""):
                    proc.stdin.write(chunk)
            proc.stdin.flush()
            while not ready.wait(0.2):
                if proc.poll() is not None:
                    return
            # Decided here, after the archive is in the container: a stop up to now never starts the bot.
            proc.stdin.write(b"stop\n" if (stage / "stopped").exists() else b"go\n")
        proc.stdin.close()
    except (OSError, ValueError, tarfile.TarError):
        pass

def watch_stop(proc, spec: dict) -> None:
    """A stop that came between go and the start finds no running container to stop: it is stopped here, again,
    until the pipeline ends (stop does nothing to a container that is only created)."""
    stopped = Path(spec["stage"]) / "stopped"
    while proc.poll() is None:
        if stopped.exists():
            sh(spec["where"], ["stop", "-t", "20", spec["container"]], check=False)
        time.sleep(0.5)

def rejoin(spec: dict, code: int, keep) -> int:
    """After ssh ended the stream (255) of a ready container, its exit code once it ends and its whole log into
    output.log, within the run's limit and a margin; a host that stays away ends the run naming the lost connection."""
    at, stage = spec["where"], Path(spec["stage"])
    began = datetime.fromisoformat(read_json(stage / "result.json")["started"]).timestamp()
    end = began + spec["limit"] + min(60, spec["limit"])  # the margin: what the script may add to the limit
    while time.time() < end:
        got = subprocess.run(shell(at, REJOIN, [at["engine"], spec["container"], str(int(end - time.time()) + 1)]),
                             env=engine_env(at), stdin=subprocess.DEVNULL, capture_output=True)
        first, _, log = got.stdout.partition(b"\n")
        if got.returncode == 0 and first.strip().isdigit():
            with (stage / "output.log").open("w", encoding="utf-8") as handle:
                keep(log.splitlines(True), handle)
            return int(first)
        if got.returncode in (3, 124):  # 3: the stream went before the bot started
            return code if got.returncode == 3 else 124
        time.sleep(min(5, max(0, end - time.time())))
    die(f"the connection to the engine host was lost and did not come back within {int(end - began)} s of the start; "
        f"{spec['container']} may still run on {at['docker_host']}, its results were not collected")

def refuse_out_input(item: str, out: Path, task: str) -> None:
    """team run's --input inside the task's own out/, which the run empties first (stage_run, fetch_results): refused
    before anything is deleted."""
    if Path(item).resolve().is_relative_to(out.resolve()):
        die(f"input {item} lies in {task}'s out/, which this run empties: copy it elsewhere first")

def relative_repo(value: str | None, path_start: tuple) -> str | None:
    """team run's --repo: a relative PATH[@REF] (not starting like a path, path_start) as ./PATH[@REF], read from the
    caller's folder. A path is a working tree there, or, when it is no commit of this folder (origin/main stays a ref),
    a name with a slash or an @REF, or a folder there (a bare repository); HEAD and reflog forms (@{) stay refs."""
    path = value.rpartition("@")[0] if value and "@" in value else value
    if not path or value.startswith(path_start) or value in (".", "..", "HEAD") or "@{" in value:
        return value
    ref = not git("rev-parse", "-q", "--verify", f"{value}^{{commit}}", check=False).returncode
    if (Path(path) / ".git").exists() or (not ref and ("/" in path or "@" in value or Path(path).is_dir())):
        return "./" + value
    return value if ref else die(f"--repo {value}: no such commit in this repository (is this folder a git "
                                 f"repository?); no folder {Path(path).resolve()} either")

def fetch_results(spec: dict, out: Path, binary: list, skipped: list) -> tuple[bool, str | None]:
    """/outbox of the container, read as one archive while the same call removes the container, masked in memory
    straight into out (names included, all checked for collisions as they come). Returns (ok, the bot's receipts).
    Any error removes out whole and returns False: no partial copy stays."""
    at = spec["where"]
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    proc = subprocess.Popen(shell(at, COLLECT, [at["engine"], spec["container"]]), env=engine_env(at),
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    ok, collision, held = False, False, {}
    try:
        seen: dict = {}
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            for member in tar:
                names = claimed(seen, member)
                if names:
                    keep_result(tar, member, *names, out, held, binary, skipped)
        proc.stdout.read()
        ok = proc.wait() == 0
    except Exception as problem:
        ok, collision = False, isinstance(problem, Collision)
    except BaseException:
        shutil.rmtree(out, ignore_errors=True)
        raise
    finally:
        end_fetch(proc, spec)
    if not ok:
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True, exist_ok=True)
        binary.clear()
        skipped.clear()
    if collision:
        die(f"refusing the results: {COLLISION}")
    receipts = held.get(("logs", f"{spec['member']}.jsonl"))
    return ok, receipts.decode("utf-8", "replace") if ok and isinstance(receipts, bytes) else None

def others_step(task: str) -> str:
    """The sandbox step that bundles every other branch (base too, which the bot can move), and a detached HEAD
    (as refs/wouf/detached), that has commits on neither the run's start (refs/wouf/start, pinned at the checkout)
    nor task/<task> (when it still exists: a renamed or deleted one excludes nothing), into out/other.bundle; the
    bundle needs the start commit only."""
    return ("(cd repo; git symbolic-ref -q HEAD >/dev/null || git update-ref refs/wouf/detached HEAD; keep=; "
            f"t=$(git rev-parse -q --verify refs/heads/task/{task}); "
            f"for r in $(git for-each-ref --format='%(refname)' refs/heads refs/wouf | grep -Fxv -e refs/wouf/start "
            f"-e refs/heads/task/{task}); do [ -n \"$(git rev-list -1 \"$r\" --not refs/wouf/start $t)\" ] "
            "&& keep=\"$keep $r\"; done; [ -z \"$keep\" ] || git bundle create \"$T/out/other.bundle\" $keep --not "
            "refs/wouf/start) >/dev/null 2>&1 || true")

def keep_others(spec: dict) -> dict:
    """The bot's commits outside its task branch, brought back as <result branch>--<name, made safe> (never
    forced). Returns kept (the new branches), reason (set when its task branch has no new commits: then the sandbox
    wrote no result.bundle) and error."""
    out, at, base = Path(spec["stage"]) / "out", spec.get("repo_dir"), spec.get("branch")
    bundle = out / "other.bundle"
    if not base or not bundle.is_file():
        return {"kept": [], "reason": None, "error": None}
    heads = git("bundle", "list-heads", str(bundle), check=False, at=at).stdout.split()[1::2]
    kept, names, failed = [], [], []
    for ref in heads:
        name = "detached HEAD" if ref == "refs/wouf/detached" else ref[len("refs/heads/"):]
        safe = "detached" if ref == "refs/wouf/detached" else re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-")[:60]
        target = f"{base}--{safe or 'branch'}"
        target += f"-{len(names)}" if target in kept + failed else ""
        fetched = git("fetch", "-q", str(bundle), f"{ref}:refs/heads/{target}", check=False, at=at)
        (failed if fetched.returncode else kept).append(target)
        names.append(name)
    error = (f"the commits of {', '.join(names)} did not reach {', '.join(failed)}; they are in {bundle}"
             if failed else None)
    reason = (f"no new commits on its task branch, only on {', '.join(names)} (kept as {', '.join(kept) or 'none'})"
              if names and not (out / "result.bundle").is_file() else None)
    return {"kept": kept, "reason": reason, "error": error}

def others_signal(member: str, branch: str | None, others: dict) -> str:
    """The lead's receipt of a run that returned: its branch, the kept other branches, or why it needs attention."""
    if others["reason"]:
        return f"{member} needs attention: {others['reason']}"
    return (f"{member} returned" + (f", branch {branch}" if branch else "")
            + (f", also kept {', '.join(others['kept'])}" if others["kept"] else ""))

def others_lines(name: str, result: dict) -> list[str]:
    """What team run and team result say about commits kept outside the task branch."""
    lines = [f"team: {name}'s commits outside its task branch were kept on {', '.join(result['kept'])}"
             ] if result.get("kept") else []
    if result.get("state") == "needs-attention":
        lines.append(f"team: needs attention: {result.get('reason')}")
    return lines

class Collision(ValueError):
    pass

def tar_path(name: str) -> tuple:
    """An archive name below its first component (the folder that was copied), as parts."""
    return tuple(p for p in PurePosixPath(name).parts[1:] if p not in ("", ".", "/"))

def claimed(seen: dict, member) -> tuple | None:
    """(original parts, masked parts below the top folder) of an archive member, or None for one to leave out;
    a name that would take another one's place once masked raises Collision."""
    original = tar_path(member.name)
    if ".." in original or not original:
        return None
    parts = masked_parts(original)
    if (member.isdir() or member.isfile() or member.islnk()) and not claim(seen, original, parts, not member.isdir()):
        raise Collision(COLLISION)
    return original, parts[1:]

def end_fetch(proc, spec: dict) -> None:
    """Not killed: the same shell removes the container once the archive is read or abandoned; only a shell that
    hangs is ended, and the container removed by a call of its own."""
    proc.stdout.close()
    try:
        proc.wait(60)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        sh(spec["where"], ["rm", "-f", spec["container"]], check=False)

def keep_result(tar, member, original: tuple, rest: tuple, out: Path, held: dict, binary: list, skipped: list) -> None:
    """One member of /outbox. Under out/: a folder or a regular file is written (masked), a hard link gets the
    masked contents of the file it links to, anything else is skipped (its masked name in skipped). Files under
    logs/ are kept in memory, masked (held), for the bot's receipts and hard links; the rest is not kept."""
    under, target = original[0], out / Path(*rest)
    if not (member.isfile() or member.islnk()):
        if under == "out" and member.isdir() and rest:
            target.mkdir(parents=True, exist_ok=True)
        elif under == "out" and not member.isdir():
            skipped.append("/".join(("out", *rest)))
        return
    data = member_bytes(tar, member, held)
    if under == "out" and not rest:
        skipped.append("out")
    elif under == "out":
        target.parent.mkdir(parents=True, exist_ok=True)
        if not put_bytes(target, data, member.mode & 0o777):
            binary.append("/".join(rest))
        held[original] = target
    elif under == "logs":
        held[original] = mask_bytes(data)

def member_bytes(tar, member, held: dict) -> bytes:
    """The contents of a file member; of a hard link, the masked contents of the file of the copy it links to."""
    if not member.islnk():
        handle = tar.extractfile(member)
        return handle.read() if handle else b""
    linked = tar_path(member.linkname)
    if linked not in held:
        raise ValueError(f"a hard link to {member.linkname}, which is not a file of the copy")
    return held[linked].read_bytes() if isinstance(held[linked], Path) else held[linked]

def running(ini, member: str | None = None) -> list[tuple[str, str, str]]:
    filters = ["--filter", f"label=wouf.team={base(ini)}"]
    if member:
        filters += ["--filter", f"label=wouf.bot={member}"]
    result = sh(ini, ["ps", *filters, "--format", '{{.Names}}|{{.Label "wouf.bot"}}|{{.Label "wouf.task"}}'],
                check=False)
    return [tuple(line.split("|")) for line in result.stdout.split() if line.count("|") == 2]

def uses_login(ini, runner: str) -> bool:
    """auth = login: the agent CLI is signed in to a subscription inside the bot's storage; no key is passed."""
    return words(get(ini, f"runner.{runner}", "auth")) == ["login"]

def login_paths(ini, runner: str) -> list[str]:  # [runner.X] login_paths, checked: teamlib.taskstore (line budget)
    return importlib.import_module("teamlib.taskstore").login_paths(ini, runner)

def login_link_steps(paths: list[str]) -> tuple[list[str], list[str]]:  # the run script's steps: teamlib.taskstore
    return importlib.import_module("teamlib.taskstore").login_link_steps(paths)

def login_check(ini, member: str, runner: str, read_only: bool = False) -> bool | None:
    """Check the stored login (optionally read-only); None when the runner names no login_check."""
    test = get(ini, f"runner.{runner}", "login_check")
    if not test:
        return None
    if mode(ini) == "bwrap":
        return BOX[0].login_check(member, test)
    return sh(ini, ["run", "--rm", *HARDEN, "--network", "none", "-e", "HOME=/bot/home", "-v",
                    f"{volume(ini, member)}:/bot{':ro' if read_only else ''}", image_tag(ini), "sh", "-c", test], check=False).returncode == 0

def throwaway(ini, member: str, *command: str) -> subprocess.CompletedProcess:
    """A short-lived container on the bot's storage, for listing and archiving."""
    return sh(ini, ["run", "--rm", *HARDEN, "-v", f"{volume(ini, member)}:/bot", image_tag(ini), *command], check=False)

# ---------------------------------------------------------------- bot lifecycle

def archive(ini, member: str) -> Path:
    target = env_dir() / "_archive" / f"{member}-{datetime.now():%Y%m%d-%H%M%S}"
    target.mkdir(parents=True, exist_ok=True)
    if mode(ini) == "container":
        if sh(ini, ["volume", "inspect", volume(ini, member)], check=False).returncode == 0:
            name = f"{base(ini)}-{member}-archive"
            sh(ini, ["rm", "-f", name], check=False)
            sh(ini, ["create", "--name", name, *HARDEN, "-v", f"{volume(ini, member)}:/bot", image_tag(ini), "true"])
            try:
                complete = fetch_masked(ini, name, "/bot", target, keep_links=True)
            finally:
                sh(ini, ["rm", "-f", name], check=False)
            if not complete:
                shutil.rmtree(target, ignore_errors=True)
                die(f"the storage of {member} could not be archived; no partial copy was kept and nothing was removed")
    else:
        source = env_dir() / member
        if source.is_dir():
            try:
                copy_archive_tree(source, target)
            except BaseException:
                shutil.rmtree(target, ignore_errors=True)
                raise
    return target

def bot_command(ini, action: str, member: str, rest: list[str]) -> None:
    check_member(member)
    if action == "add":
        source = PACK / "bots" / f"{member}.md"
        target = team_dir() / "bots" / f"{member}.md"
        if not source.is_file():
            die(f"no example bot {member!r}; examples: {', '.join(p.stem for p in sorted((PACK / 'bots').glob('*.md')))}")
        if target.exists():
            die(f"team/bots/{member}.md exists already")
        text = source.read_text(encoding="utf-8")
        declared = frontmatter(source).get("id", member)
        if secret_in(declared):
            # The file name is the id (it passed check_member above): a key-shaped value in the declaration is
            # replaced by it, never copied and never shown.
            text = re.sub(r"(?m)^id:.*$", lambda _: f"id: {member}", text, count=1)
        else:
            declared_id(declared, member)
        text = re.sub(r"(?m)^status:.*$", "status: proposed", text, count=1)
        put(target, text)
        say(f"added team/bots/{member}.md as proposed; on the human's yes set status: active, then "
            f"'team bot create {member}'")
        return
    decl = bot(member)
    if mode(ini) == "bwrap" and action in BOX[0].ACTIONS:
        return BOX[0].bot_command(ini, action, member, decl, rest)
    if action == "create":
        if decl.get("status", "active") not in ("active", "trial"):
            die(f"{member} is {decl.get('status')}; the human approves it first (set status: active)")
        if mode(ini) == "container":
            ensure_image(ini)
            ensure_volume(ini, member)
            where = f"storage {volume(ini, member)}, image {image_tag(ini)}"
        else:
            (env_dir() / member / "logs").mkdir(parents=True, exist_ok=True)
            where = f"folder {env_dir() / member} (host mode, no sandbox)"
        lead_log("-", "done", f"created {member}")
        say(f"{display(ini, decl)} is ready: {where}")
    elif action == "stop":
        stopped = 0
        for state in (env_dir() / member / "tasks").glob("*/result.json"):
            if read_json(state).get("state") == "running":
                (state.parent / "stopped").touch()
        if mode(ini) == "container":
            for name, _, _ in running(ini, member):
                # TERM first: the run script ends the agent CLI and still hands back what it did.
                sh(ini, ["stop", "-t", "20", name], check=False)
                stopped += 1
        elif os.name == "posix":
            # Host runs: end the process group of every running task; its supervisor then collects as usual.
            for state in (env_dir() / member / "tasks").glob("*/result.json"):
                result = read_json(state)
                if result.get("state") == "running" and isinstance(result.get("group"), int):
                    try:
                        os.killpg(result["group"], 9)
                        stopped += 1
                    except (ProcessLookupError, PermissionError):
                        pass
        lead_log("-", "done", f"stopped {member} ({stopped} running)")
        say(f"{display(ini, decl)}: stopped {stopped} running task(s)")
    elif action in ("reset", "remove"):
        bot_command(ini, "stop", member, [])
        saved = archive(ini, member)
        if mode(ini) == "container":
            sh(ini, ["volume", "rm", "-f", volume(ini, member)], check=False)
            stamp_note(core.where(ini), storage=volume(ini, member), drop=True)
        else:  # bwrap: the bot may have made folders of its box read-only, so teamlib.bwrap removes it
            BOX[0].drop(member) if mode(ini) == "bwrap" else shutil.rmtree(env_dir() / member, ignore_errors=True)
        if action == "reset":
            bot_command(ini, "create", member, [])
        else:
            text = decl["path"].read_text(encoding="utf-8")
            put(decl["path"], re.sub(r"(?m)^status:.*$", "status: retired", text, count=1))
        lead_log("-", "done", f"{action} {member}, files archived")
        say(f"{display(ini, decl)}: {action} done; its files are in {saved}")
    elif action == "login":
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            die(f"'team bot login {member}' needs a person: run it in your own terminal, not through an agent")
        if mode(ini) != "container":
            die("host mode: sign in with the agent CLI itself on this computer")
        runner = pick_runner(ini, decl.get("runner"))
        command = get(ini, f"runner.{runner}", "login")
        if not uses_login(ini, runner) or not command:
            die(f"[runner.{runner}] needs auth = login and a login command for this")
        ensure_image(ini), ensure_volume(ini, member)
        # Interactive, on the bot's own storage; the login stays there and never reaches this computer.
        done = subprocess.run([engine(ini), "run", "--rm", "-it", *HARDEN, "-e", "HOME=/bot/home", "-v",
                               f"{volume(ini, member)}:/bot", image_tag(ini), "sh", "-c",
                               f'mkdir -p "$HOME" && {command}'], env=engine_env(ini))
        ok = login_check(ini, member, runner)
        lead_log("-", "done" if done.returncode == 0 and ok is not False else "failed", f"login {member} ({runner})")
        say(f"{display(ini, decl)}: login {'done' if ok else 'not confirmed' if ok is None else 'missing'}")
        return
    elif action == "inspect":
        path = "/bot/" + (rest[0].lstrip("/") if rest else "")
        lead_log("-", "done", f"inspected {member} {path}")
        if mode(ini) == "container":
            say(throwaway(ini, member, "ls", "-la", path).stdout)
        else:
            say("\n".join(sorted(str(p) for p in (env_dir() / member).glob("*"))))
    else:
        die(f"unknown bot action {action!r} (see: team help)")

def cmd_install(ini, rest: list[str]) -> None:
    if len(rest) < 2 or rest[0] != "--":
        die("usage: team install -- COMMAND")
    command = rest[1] if len(rest) == 2 else shlex.join(rest[1:])
    put(team_dir() / "provision.sh", f"# {datetime.now():%Y-%m-%d}\n{command}\n", "a")
    lead_log("-", "done", f"install: {command[:80]}")
    if mode(ini) == "container":
        ensure_image(ini)
    say(f"added to team/provision.sh: {command}")

# ---------------------------------------------------------------- collected lessons

def event_problem(state, log, kind: str, actor: str, task: str, data: dict) -> str | None:
    """Why an event cannot be stored (state.check's tests, without stopping the caller), or None."""
    for label, value in state.strings(data, "field"):
        found = state.secret_kind(value)
        if found:
            return f"the {label} looks like it contains a secret ({found})"
    try:
        for value in (kind, actor, task):
            if log.event_value(value) != value or log.mask(value) != value:
                return "a name holds a stored value"
        log.event_validate(kind, actor, task, data, "local")
        log.event_data(kind, data)
    except (ValueError, RecursionError):
        return "a field is invalid or too long"
    return None

def lesson_outputs(folder: Path, skip, masks, bot_text) -> list:
    """Read at most twenty regular Markdown files, each at most 64 KiB, without following links."""
    proposals = []
    try:
        paths = sorted(folder.iterdir())
    except OSError:
        skip("knowledge", "unreadable folder")
        return []
    for path in paths:
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or path.suffix != ".md":
                skip(path.name, "not a regular .md file")
                continue
            if info.st_size > 65536:
                skip(path.name, "over 64 KiB")
                continue
            if len(proposals) >= 20:
                skip(path.name, "over 20 files")
                continue
            body = mask_secrets(path.read_text(encoding="utf-8"), [*KNOWN, *masks])
        except (OSError, UnicodeError):
            skip(path.name, "unreadable Markdown")
            continue
        heading = re.search(r"^ {0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", body, re.M)
        title = bot_text(heading.group(1) if heading else path.name, masks)
        proposals.append((path.name, title, body))
    return proposals

def collect_lessons(spec: dict, store: list, record, bot_text, masks=(), skipped=()) -> None:
    """Propose bounded Markdown outputs through the shared store; a run's nonce and path identify a proposal.
    The state lock covers the duplicate check and append, including a retry after an interrupted collection.
    The caller supplies the store, writer and text sanitizer to keep engine independent of run and state."""
    stage, member, task = Path(spec["stage"]), spec["member"], spec["task"]
    folder = stage / "out" / "knowledge"

    def skip(name, reason):
        put(stage / "run.log", f"team: lesson skipped {bot_text(name, masks)}: {reason}\n", "a")

    for name in skipped:
        if name == "out/knowledge" or name.startswith("out/knowledge/"):
            skip(name, "link or special file")
    if not os.path.lexists(folder):
        return
    if (stage / "out").is_symlink() or folder.is_symlink() or not folder.is_dir():
        skip("knowledge", "not a regular folder")
        return
    proposals = lesson_outputs(folder, skip, masks, bot_text)

    def prepare(snap):
        state = store[0]
        log = state.store()
        lessons = state.snapshot(log, ("lessons",))["lessons"]
        have = {(row["task"], row["source"], row["topic"]) for row in lessons}
        events = []
        for name, title, body in proposals:
            topic = mask_secrets(f"{task}/{spec.get('nonce', '')}/knowledge/{name}", [*KNOWN, *masks])
            data = {"title": title, "body": body, "topic": topic, "source": member}
            if (task, member, data["topic"]) in have:
                continue
            if len(body) > 2000:
                suffix = mask_secrets(
                    f"\n… (cut at 2,000 characters; the whole file is out/knowledge/{name} of run {task})",
                    [*KNOWN, *masks])
                data["body"] = body[:2000 - len(suffix)] + suffix
                put(stage / "run.log", f"team: lesson truncated {bot_text(name, masks)}: over 2,000 characters\n", "a")
            problem = event_problem(state, log, "lesson.proposed", member, task, data)
            if problem:
                skip(name, problem)
            else:
                events.append(("lesson.proposed", member, task, data))
        return events
    record([], prepare)

# ---------------------------------------------------------------- the budget

# [budget] in config.ini: token caps, off by default. A run's tokens are the numbers its receipt records (teamlib.run
# writes them into the lead's receipt at the end of every collected run), summed; the day is the UTC day.
RECEIPT_TOKENS = re.compile(r"tokens=([0-9]{1,12})/([0-9]{1,12})/([0-9]{1,12})/([0-9]{1,12})")

def budget_caps(ini) -> dict:
    """The caps of [budget] in tokens, by key; a key left empty is no cap. Every value is checked, another bot's
    per_day.<bot> too, before team run takes its lock: anything but digits stops the command and leaves no lock."""
    caps = {}
    for key in ini.options("budget") if ini.has_section("budget") else ():
        value = get(ini, "budget", key)
        if value and not re.fullmatch(r"[0-9]{1,15}", value):
            die(f"[budget] {visible(key)} = {visible(value)}: a number of tokens, or empty for no cap")
        if value:
            caps[key] = int(value)
    return caps

def next_midnight() -> str:
    """When today's budget ends: the next UTC midnight, as now() writes a time."""
    day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return (day + timedelta(days=1)).isoformat(timespec="seconds")

def tokens_today(member: str | None = None) -> int:
    """The tokens of today's runs in the lead's receipts, of one bot (the signal's first word) or of all. A receipt
    without numbers (unknown, or no run) counts nothing; a line that is not a receipt is passed over."""
    path = log_dir() / "lead.jsonl"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines() if path.is_file() else []
    except OSError:
        lines = []
    day, used = datetime.now(timezone.utc).date(), 0
    for line in lines:
        try:
            entry = json.loads(line)
            found = RECEIPT_TOKENS.fullmatch(entry["cost"])
            when = datetime.fromisoformat(entry["t"].replace("Z", "+00:00"))
            bot = entry["signal"].split(" ", 1)[0]
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            continue
        if found and when.tzinfo and when.astimezone(timezone.utc).date() == day and member in (None, bot):
            used += sum(int(n) for n in found.groups())
    return used

def budget_skip(caps: dict, member: str, task: str, stage: Path, name: str) -> tuple | None:
    """Before a run starts: once today's tokens reach the bot's own per_day or the team's, the run is not started.
    It ends skipped until the next UTC midnight, in run.json and result.json (team result, the flow) and in a receipt
    without numbers. Returns (spec, result) of the skipped run, or None: the run starts."""
    for key, who in ((f"per_day.{member}", member), ("per_day", None)):
        cap = caps.get(key)
        used = tokens_today(who) if cap is not None else 0
        if cap is not None and used >= cap:
            why, until = f"over budget: {used}/{cap} tokens today", next_midnight()
            spec = {"member": member, "task": task, "name": name, "stage": str(stage)}
            result = {"state": "skipped", "error": why, "until": until, "started": now(), "ended": now()}
            write_json(stage / "run.json", spec)
            write_json(stage / "result.json", result)
            lead_log(task, "limited", f"{member} skipped ({key}): {why}, until {until}")
            return spec, result
    return None

def over_run(spec: dict, tokens: str) -> str | None:
    """'<used>/<cap>' when a run's own numbers pass its per_run cap (read when it started), else None."""
    found, cap = RECEIPT_TOKENS.fullmatch(f"tokens={tokens}"), spec.get("per_run")
    used = sum(int(n) for n in found.groups()) if found else 0
    return f"{used}/{cap}" if isinstance(cap, int) and used > cap else None

def budget_mark(signal: str, over: str | None) -> str:
    """A receipt's signal marked over-budget, after the bot's id (its first word, which team stats reads)."""
    member, _, rest = signal.partition(" ")
    return f"{member} over-budget {over} tokens, {rest}" if over else signal

# ---------------------------------------------------------------- egress

# [environment] network = egress: each run gets two networks, <run>-in (--internal, no gateway) and <run>-out, and
# a proxy container (the egress proxy tool) on both; the bot joins only -in and reaches the proxy as `proxy`. The
# allowlist is a list of host names (exact) and .suffix entries, the proxy tool's own format, checked here first.
# The list of a run (config, brief, lead decisions) comes from teamlib.flow, which plugs it in here when loaded.
EGRESS: list = []
EGRESS_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
EGRESS_HOST = re.compile(rf"\.?(?=.{{1,253}}$){EGRESS_LABEL}(?:\.{EGRESS_LABEL})*", re.ASCII)
EGRESS_PORT = 3128
EGRESS_ENV = {**{v: f"http://proxy:{EGRESS_PORT}" for v in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")},
              "NO_PROXY": "localhost,127.0.0.1,::1", "no_proxy": "localhost,127.0.0.1,::1"}  # loopback only; fixed
EGRESS_READY = "egress-proxy: listening"
# aardvark-dns forwards unknown names out of an internal network before 1.11.0; Moby before the CVE-2024-29018 fix.
AARDVARK_MIN = (1, 11, 0)
MOBY_FIXED = (((23, 0, 11), (24, 0, 0)), ((25, 0, 5), (26, 0, 0)), ((26, 0, 0), (10 ** 6, 0, 0)))
VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:-rc\.?(\d+)|(-[0-9A-Za-z.-]+))?")

def egress_hosts(value: str, where_from: str) -> list[str]:
    """The host names of a list (comma or space separated), lower case and without a trailing dot; an entry that is
    not a host name or .suffix (an address, a port, a pattern, a URL) stops here, so a typo never widens a list."""
    hosts = []
    for raw in words(value):
        host = raw.lower()[:-1] if raw.endswith(".") else raw.lower()
        last = host.rsplit(".", 1)[-1]
        if not EGRESS_HOST.fullmatch(host) or last[:1].isdigit():
            die(f"{where_from}: {visible(raw)!r} is not a host name or .suffix (no address, port, pattern or URL)")
        if host not in hosts:
            hosts.append(host)
    return hosts

def egress_base(ini) -> list[str]:
    return egress_hosts(get(ini, "egress", "allow"), "[egress] allow in team/config.ini")

# The model API and subscription login hosts of a CLI, by its program name; UNVERIFIED until the lead's trial.
EGRESS_NEEDS = {"claude": "api.anthropic.com claude.ai platform.claude.com",
                "codex": "api.openai.com chatgpt.com auth.openai.com"}

def egress_needs(ini, runner: str) -> list[str]:
    """The hosts a runner needs for its model API and login: [egress] needs.<runner>, else EGRESS_NEEDS by its CLI."""
    value = get(ini, "egress", f"needs.{runner}") or EGRESS_NEEDS.get(Path(cli_of(ini, runner) or "").name, "")
    return egress_hosts(value, f"[egress] needs.{runner} in team/config.ini")

def egress_allows(allow: list[str], host: str) -> bool:
    """Whether the list lets host through, as the proxy matches: exactly, or below a .suffix entry."""
    return any(host.endswith(e) if e.startswith(".") else host == e for e in allow)

def version_of(text: str) -> tuple | None:
    """(major, minor, patch, pre) of text's first version; pre: None for a release, an rc's number, else -1."""
    found = VERSION.search(text or "")
    if not found:
        return None
    pre = int(found.group(4)) if found.group(4) else (-1 if found.group(5) else None)
    return int(found.group(1)), int(found.group(2)), int(found.group(3)), pre

def moby_fixed(found: tuple) -> bool:
    """Moby 23.0.11+, 25.0.5+ or 26.0.0-rc3+ (GHSA-mq39-4gv4-mvpx); 24.x never got the fix."""
    release, pre = found[:3], found[3]
    if release == (26, 0, 0) and pre is not None:
        return pre >= 3
    return pre is None and any(low <= release < high for low, high in MOBY_FIXED)

def egress_engine_check(at: dict) -> None:
    """Refuse an engine whose internal networks let DNS out: Podman needs netavark with aardvark-dns 1.11.0 or
    newer, Docker the fix for CVE-2024-29018. An answer that cannot be read refuses too."""
    name = at["engine"]
    if "podman" in Path(name).name:
        info = sh(at, ["info", "--format", "{{.Host.NetworkBackend}} {{.Host.NetworkBackendInfo.DNS.Version}}"],
                  check=False)
        backend, _, dns = info.stdout.strip().partition(" ")
        found = version_of(dns)
        if info.returncode or backend != "netavark" or not found or found[:3] < AARDVARK_MIN or found[3] is not None:
            shown = visible(info.stdout.strip() or (info.stderr or "").strip())[-200:] or "no answer"
            die(f"network = egress needs Podman with netavark and aardvark-dns {'.'.join(map(str, AARDVARK_MIN))} or "
                f"newer (older ones forward DNS out of an internal network); {name} says: {shown}. Update "
                f"aardvark-dns, or set network = bridge")
        return
    info = sh(at, ["version", "--format", "{{.Server.Version}}"], check=False)
    found = version_of(info.stdout.strip())
    if info.returncode or not found or not moby_fixed(found):
        shown = visible(info.stdout.strip() or (info.stderr or "").strip())[-200:] or "no answer"
        die(f"network = egress needs Docker Engine 26.0.0, 25.0.5, 23.0.11 or newer in its line (the fix for "
            f"CVE-2024-29018: older ones forward DNS out of an internal network); {name} says: {shown}. Update "
            f"Docker, or set network = bridge")

def egress_prepare(ini, o: dict, decl: dict, current: str, runner: str) -> dict | None:
    """network = egress: the run's allowlist (teamlib.flow), then the engine's versions; None for bridge and none."""
    if get(ini, "environment", "network", "bridge") != "egress":
        return None
    if not EGRESS:
        die("network = egress needs teamlib.flow, which bin/team loads; run it through .agents/bin/team")
    egress = EGRESS[0](ini, o, decl, current, runner)
    if current == "bwrap":  # no engine to check: teamlib.bwrap (BOX) runs the proxy in a sandbox of its own
        return BOX[0].egress_chosen(egress)
    egress_engine_check(where(ini))
    return egress

def egress_spec(egress: dict | None, container: str, team: str) -> dict:
    """What a run's spec gets for egress: the bot's network (the internal one) and the names; nothing without."""
    if not egress:
        return {}
    names = {"in": f"wouf-{container}-in", "out": f"wouf-{container}-out", "proxy": f"{container}-proxy"}
    return {"network": names["in"], "egress": {**names, **egress, "team": team}}

def egress_up(spec: dict) -> None:
    """The run's two networks and its proxy, started and listening, before the bot is created. Leftovers of an
    earlier run of the same name go first; every object carries the run's labels and wouf.egress=<container>."""
    at, egress = spec["where"], spec["egress"]
    egress_end(spec, box=True, log=False)
    tag = [*spec["run_labels"], "--label", f"wouf.egress={spec['container']}"]
    source = Path(egress["source"])
    if source.is_symlink() or not source.is_file():
        die(f"the egress proxy {source} is not a regular file; name egress_proxy.py in [egress] proxy")
    program = source.read_text(encoding="utf-8")
    sh(at, ["network", "create", "--internal", *tag, egress["in"]])
    sh(at, ["network", "create", *tag, egress["out"]])
    allow = [arg for host in egress["allow"] for arg in ("--allow", host)]
    allow += [arg for pair in egress.get("private", []) for arg in ("--private", pair)]  # [egress] private, if any
    sh(at, ["create", "--name", egress["proxy"], *tag, *HARDEN, "--read-only", "--network", egress["in"],
            "--network-alias", "proxy", "--cpus", "0.5", "--memory", "256m", spec["image"],
            "python3", "-I", "-c", program, "--bind", "*", "--port", str(EGRESS_PORT), *allow])
    sh(at, ["network", "connect", egress["out"], egress["proxy"]])
    sh(at, ["start", egress["proxy"]])
    deadline = time.monotonic() + 20
    while True:
        logs = sh(at, ["logs", egress["proxy"]], check=False)
        if EGRESS_READY in logs.stdout + logs.stderr:
            return
        if time.monotonic() > deadline:
            die(f"the egress proxy did not start: {visible((logs.stderr + logs.stdout).strip())[-300:]}")
        time.sleep(0.2)

def egress_end(spec: dict, box: bool = False, log: bool = True) -> None:
    """The proxy's log into the run folder (egress.log, masked on the way), then the proxy and the networks removed;
    box: the bot's container first, when nothing collects it any more. No-op without egress; never fails a run."""
    egress = spec.get("egress")
    if not egress or spec.get("mode") == "bwrap":  # bwrap: only a record in run.json; teamlib.bwrap ends its proxy
        return
    at = spec["where"]
    logs = sh(at, ["logs", egress["proxy"]], check=False) if log else None
    if logs is not None and not logs.returncode:
        lines = [line for line in (logs.stdout + logs.stderr).splitlines() if line.strip()]
        put(Path(spec["stage"]) / "egress.log", "".join(f"{line}\n" for line in lines))
    for name in ([spec["container"]] if box else []) + [egress["proxy"]]:
        sh(at, ["rm", "-f", name], check=False)
    for name in (egress["in"], egress["out"]):
        if sh(at, ["network", "inspect", name], check=False).returncode == 0:
            sh(at, ["network", "rm", name], check=False)

def egress_report(stage: Path, task: str, nonce: str, last: int = 40) -> None:
    """The proxy's log of the run, masked and framed as data: a count, then its last lines."""
    path = stage / "egress.log"
    if not path.is_file():
        return
    rows = [visible(line)[:500] for line in mask_secrets(path.read_text(encoding="utf-8", errors="replace")).splitlines()]
    refused = sum(1 for row in rows if " result=refused " in row)
    say(f"--- egress log of {task}: {len(rows)} lines, {refused} refused; data from the proxy [{nonce}] ---")
    say("\n".join(rows[-last:] + ([f"(earlier lines in {path})"] if len(rows) > last else [])))
    say(f"--- end of egress log [{nonce}] ---")

def egress_clean(at: dict, team: str) -> list[str]:
    """What killed supervisors left (labels wouf.team and wouf.egress): each dead run's proxy and networks go, a
    running run's stay. Returns what was removed; team run calls it before an egress run, team doctor --clean too."""
    filters = ["--filter", f"label=wouf.team={team}", "--filter", "label=wouf.egress"]
    form = '{{.Names}}|{{.Label "wouf.bot"}}|{{.Label "wouf.task"}}'
    boxes = sh(at, ["ps", "-a", *filters, "--format", form], check=False).stdout.split()
    nets = sh(at, ["network", "ls", *filters, "--format", "{{.Name}}"], check=False).stdout.split()
    gone = []
    for line in boxes:
        name, bot, task = (line.split("|") + ["", ""])[:3]
        if not member_problem(bot) and not task_problem(task) and alive_run(bot, task):
            continue
        sh(at, ["rm", "-f", name], check=False)
        gone.append(name)
    for name in nets:
        labels = sh(at, ["network", "inspect", "--format", '{{index .Labels "wouf.bot"}}|{{index .Labels "wouf.task"}}',
                         name], check=False).stdout.strip()
        bot, _, task = labels.partition("|")
        if not member_problem(bot) and not task_problem(task) and alive_run(bot, task):
            continue
        sh(at, ["network", "rm", name], check=False)
        gone.append(name)
    return gone

def alive_run(member: str, task: str) -> bool:
    """Whether the supervisor of a run of member on task is still there (its result says running, its pid lives)."""
    result = read_json(env_dir() / member / "tasks" / task / "result.json")
    return result.get("state") == "running" and alive(result.get("pid"))

# ---------------------------------------------------------------- commands

def unknown(argv: list[str]) -> None:
    """What main says to a command it does not know, and to one with arguments it does not take."""
    die(f"unknown command: {' '.join(argv)} (see: team help)")

def do_bot(rest: list[str]) -> int:
    ini = config()
    if len(rest) < 2:
        unknown(["bot", *rest])
    bot_command(ini, rest[0], rest[1], rest[2:])
    return 0

def do_install(rest: list[str]) -> int:
    cmd_install(config(), rest)
    return 0

def do_image(rest: list[str]) -> int:
    ini = config()
    if rest:
        unknown(["image", *rest])
    if mode(ini) == "bwrap":  # the team prefix env/_prefix stands in for the image (teamlib.bwrap, BOX)
        return BOX[0].prefix_command(ini)
    stamp_clear(), say(ensure_image(ini, force=True))
    return 0

COMMANDS = {"bot": do_bot, "install": do_install, "image": do_image}
USAGE = """
  team bot add ID                     copy an example bot from .agents/bots/ into the team (status: proposed)
  team bot create ID                  give an active bot its own storage (and build the team image if needed)
  team bot stop ID                    stop the bot's running task at once
  team bot reset ID                   archive the bot's files, then give it clean storage
  team bot remove ID                  archive its files, delete its storage, mark it retired
  team bot inspect ID [PATH]          list files in the bot's storage (read only; ask the bot first)
  team bot login ID                   sign the bot's agent CLI in to a subscription, once, in its own storage
                                      (runners with auth = login; only in your own terminal)

  team install -- COMMAND             add a system package or tool to the team image (all bots get it)
  team image                          build the team image now (otherwise it is built when needed)

  team run ID TASK --repo PATH[@REF]  a relative PATH is read from the folder team is called in (ref/wouf@main);
                                      an --input in the task's own out/ is refused: the run empties that folder
"""
