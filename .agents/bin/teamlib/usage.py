"""teamlib.usage: a run's token numbers. The adapter line the run script prints after the CLI (builtin:claude), the
out/usage.json a `[runner.X] usage =` command writes in the sandbox (schema wouf.usage.v1), which of them a run's
result takes, and the receipt that carries them. Kernel order: core <- engine <- usage <- run."""

from __future__ import annotations

import inspect
import math
import shlex

from teamlib.core import *  # noqa: F401,F403

# ---------------------------------------------------------------- the adapter line

# The usage adapter for Claude Code (builtin:claude): it runs inside the sandbox as its own program (its source is the
# run script's step), so it imports what it needs itself.
def claude_project(work: str) -> str:
    """The folder Claude Code keeps a working folder's sessions in, below ~/.claude/projects."""
    return re.sub(r"[^A-Za-z0-9]", "-", work)


def claude_usage(home, since, project):
    """Run inside the sandbox, as its own program: the token numbers of the Claude Code sessions of this task's
    folder written since the run began, as 'tokens=in/cache_read/cache_write/out skipped=N'. One message in several
    records counts once; a line that is not JSON or holds no plain numbers is skipped and counted. Only digits are
    returned: no text, name or path of a transcript. Only regular files are opened (a FIFO would block forever)."""
    import json, os, stat  # noqa: E401
    keys = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")
    try:
        start = os.stat(since).st_mtime
    except OSError:
        return "tokens=unknown skipped=0"
    turns, skipped, found = {}, 0, False
    for root, _, files in os.walk(os.path.join(home, ".claude", "projects", project)):
        for name in sorted(files):
            path = os.path.join(root, name)
            try:
                if not name.endswith(".jsonl") or not stat.S_ISREG((info := os.lstat(path)).st_mode) or info.st_mtime < start:
                    continue
                handle = open(path, encoding="utf-8", errors="replace")
            except OSError:
                continue
            found = True
            with handle:
                for number, line in enumerate(handle):
                    try:
                        row = json.loads(line)
                        if row.get("type") != "assistant":
                            continue
                        message = row["message"]
                        values = [message["usage"].get(key, 0) for key in keys]
                    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
                        skipped += 1
                        continue
                    ident = message.get("id")
                    if all(type(v) is int and 0 <= v < 10 ** 12 for v in values):
                        turns[(path, ident if isinstance(ident, str) else number)] = values
                    else:
                        skipped += 1
    totals = "/".join(str(min(sum(turn[i] for turn in turns.values()), 10 ** 12 - 1)) for i in range(4))
    return f"tokens={totals if found else 'unknown'} skipped={skipped}"


# A run's token numbers: an adapter, chosen by the runner's CLI, reads the CLI's own session record in the bot's
# storage inside the container and prints one line of numbers. A runner without an adapter, a host run: unknown.
USAGE_MARK = "wouf-usage "
TOKENS = re.compile(r"[0-9]{1,12}/[0-9]{1,12}/[0-9]{1,12}/[0-9]{1,12}")
USAGE_LINE = re.compile(r"wouf-usage tokens=(" + TOKENS.pattern + r"|unknown) skipped=([0-9]{1,9})")
USAGE_SINCE = "/tmp/wouf-usage-since"

USAGE_ADAPTERS = {"claude": claude_usage}

def usage_adapter(cli: str | None) -> str | None:
    """The adapter for a runner's CLI (its program name), or None: that runner reports unknown."""
    name = Path(cli).name if cli else ""
    return name if name in USAGE_ADAPTERS else None

def usage_step(adapter: str, work: str) -> str:
    """The run script's line that prints the run's numbers, unknown when the adapter fails or runs over 60 s."""
    program = inspect.getsource(USAGE_ADAPTERS[adapter]) + (
        "\nimport sys\nsys.stdout.write(%s(*sys.argv[1:4]) + '\\n')\n" % USAGE_ADAPTERS[adapter].__name__)
    project = claude_project(work)
    return (f"printf '{USAGE_MARK}%s\\n' \"$(timeout 60 python3 -I -c {shlex.quote(program)} "
            f"\"$HOME\" \"$USAGE_SINCE\" {shlex.quote(project)} 2>/dev/null || echo 'tokens=unknown skipped=0')\"")

def run_steps_usage(adapter: str | None, work: str, command: str | None = None) -> tuple[list[str], list[str]]:
    """The script's steps before the bot starts (the time mark, also for a `usage =` command) and after it ended (the
    adapter's numbers; a command's steps are usage_command_steps)."""
    if not adapter and not command:
        return [], []
    return [f"USAGE_SINCE={USAGE_SINCE}", "touch \"$USAGE_SINCE\""], [usage_step(adapter, work)] if adapter else []

def usage_of(lines: list[str], adapter: str | None) -> tuple[str, int]:
    """The numbers of a run from its collected, masked output: the last usage line, which the script prints after the
    bot ended. Digits only, each below 10^12; anything else, or a run without an adapter, is unknown."""
    if not adapter:
        return "unknown", 0
    for line in reversed(lines):
        if line.startswith(USAGE_MARK.strip()):
            found = USAGE_LINE.fullmatch(line)
            return (found.group(1), int(found.group(2))) if found else ("unknown", 0)
    return "unknown", 0

def receipt(task: str, outcome: str, signal: str, tokens: str) -> None:
    """The lead's receipt of a run's end, as lead_log, with the run's numbers as its cost."""
    cost = f"tokens={tokens if TOKENS.fullmatch(tokens) else 'unknown'}"
    done = subprocess.run([sys.executable, str(PACK / "bin" / "log"), "add", "lead", task, outcome,
                           mask_secrets(signal, KNOWN)[:120], "--cost", cost], capture_output=True, text=True,
                          env={**os.environ, "WOUF_LOG_DIR": str(log_dir()), "WOUF_MASK": json.dumps(KNOWN)})
    if done.returncode:
        say(f"team: receipt not written: {done.stderr.strip()[:200]}", err=True)


# ---------------------------------------------------------------- out/usage.json and `usage =`

# A runner's own report, written into the task folder's out/ in the sandbox and masked on the way back like every
# file. Only `tokens` is required; anything that is not the schema makes the run's numbers come from the adapter
# line, or unknown, and is named in result.json (usage_error).
USAGE_FILE = "usage.json"
USAGE_SCHEMA = "wouf.usage.v1"
USAGE_KEYS = ("input", "cache_read", "cache_write", "output")
USAGE_TOP = ("schema", "tokens", "seconds", "unknown_runs", "runs")
USAGE_RUN = ("role", "runner", "tier", "model", "tokens", "seconds")


def count(value) -> bool:
    return type(value) is int and 0 <= value < 10 ** 12


def tokens_of(value) -> bool:
    return type(value) is dict and set(value) == set(USAGE_KEYS) and all(count(value[k]) for k in USAGE_KEYS)


def seconds_of(value) -> bool:
    return count(value) or type(value) is float and math.isfinite(value) and 0 <= value < 10 ** 12


def usage_problem(data) -> str | None:
    """What keeps data from being wouf.usage.v1, or None."""
    if type(data) is not dict:
        return "not a JSON object"
    if not set(data) <= set(USAGE_TOP) or data.get("schema", USAGE_SCHEMA) != USAGE_SCHEMA:
        return f"keys other than {', '.join(USAGE_TOP)}, or another schema"
    if not tokens_of(data.get("tokens")):
        return f"tokens is not {', '.join(USAGE_KEYS)} as integers from 0 to 10^12 - 1"
    if "seconds" in data and not seconds_of(data["seconds"]) or "unknown_runs" in data and not count(
            data["unknown_runs"]):
        return "seconds or unknown_runs is not a number from 0 to 10^12 - 1"
    runs = data.get("runs", [])
    if type(runs) is not list:
        return "runs is not a list"
    for run in runs:
        if type(run) is not dict or not set(run) <= set(USAGE_RUN) or not tokens_of(run.get("tokens")) or (
                "seconds" in run and not seconds_of(run["seconds"])) or not all(
                type(run[k]) is str for k in ("role", "runner", "tier", "model") if k in run):
            return "a run is not {role, runner, tier, model, tokens, seconds}"
    return None


def usage_file(path) -> tuple[dict | None, str | None]:
    """A run's out/usage.json: (the dict, None) when it is wouf.usage.v1, (None, why) when it is not, (None, None)
    when there is none. Only a regular file is read."""
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None, None
    except OSError:
        return None, f"out/{USAGE_FILE} could not be read"
    if not stat.S_ISREG(info.st_mode):
        return None, f"out/{USAGE_FILE} is not a regular file"

    def refuse(constant):
        raise ValueError(constant)
    try:
        data = json.loads(path.read_text(encoding="utf-8"), parse_constant=refuse)
    except (OSError, ValueError, RecursionError):
        return None, f"out/{USAGE_FILE} is not JSON"
    problem = usage_problem(data)
    return (None, f"out/{USAGE_FILE} is not {USAGE_SCHEMA}: {problem}") if problem else (data, None)


def read_usage(path) -> dict | None:
    """The dict of a wouf.usage.v1 file, or None: missing, not a regular file, not JSON, not the schema."""
    return usage_file(path)[0]


def usage_config(ini, section: str, cli: str | None, boxed: bool) -> tuple[str | None, str | None]:
    """[runner.X] usage, checked before the run takes its lock: (adapter, command). builtin:<adapter>, or a one-line
    shell command; empty or missing: the adapter the CLI's name chooses, as before the key. Only a boxed run reports
    numbers (a host run would read this computer's own session records)."""
    value = get(ini, section, "usage")
    builtin = value[len("builtin:"):] if value.startswith("builtin:") else None
    if builtin is not None and builtin not in USAGE_ADAPTERS or not value.isprintable():
        known = " or ".join(f"builtin:{name}" for name in USAGE_ADAPTERS)
        die(f"[{section}] usage = {visible(value)}: {known}, a one-line shell command, or empty")
    if not boxed:
        return None, None
    if not value:
        return usage_adapter(cli), None
    return (builtin, None) if builtin is not None else (None, value)


def usage_command_steps(command: str | None) -> list[str]:
    """The run script's steps for a `usage =` command, after the CLI and before out/ is copied out. The command runs in
    the task folder with $T and $USAGE_SINCE, at most 60 s (a KILL 5 s after the TERM), its output dropped. The bot's
    own out/usage.json is kept: a command that exits 0 replaces it (or leaves it), and one that fails gets its
    report back, so a failed command never costs a valid report. Whatever it does, the run's exit code stays the
    CLI's."""
    if not command:
        return []
    prev = f"\"$T/{USAGE_FILE}.prev\""
    kept = f"\"$T/out/{USAGE_FILE}\""
    return [f"rm -f {prev}; if [ -f {kept} ] && [ ! -L {kept} ]; then cp -f {kept} {prev} 2>/dev/null || true; fi",
            f"(cd \"$T\" && T=\"$T\" USAGE_SINCE=\"$USAGE_SINCE\" timeout -k 5 60 sh -c {shlex.quote(command)}) "
            f"</dev/null >/dev/null 2>&1 || {{ [ -f {prev} ] && mv -f {prev} {kept}; }}; rm -f {prev}"]


def run_numbers(stage: Path, lines: list[str], spec: dict) -> tuple[str, int, dict, list[str]]:
    """A run's numbers for its result: (tokens, skipped, fields, the output without its usage lines). A valid
    out/usage.json first, for every runner (a `usage =` command's, or the one an agency or a bot writes itself; the
    stage's out/ is new for each run), else the adapter line, else unknown. fields hold usage (the dict or None),
    usage_source (file, adapter, none) and, for a file that is not the schema, usage_error."""
    tokens, unread = usage_of(lines, spec.get("usage"))
    rest = [line for line in lines if not line.startswith(USAGE_MARK.strip())]
    found, problem = usage_file(Path(stage) / "out" / USAGE_FILE)
    if found:
        return "/".join(str(found["tokens"][k]) for k in USAGE_KEYS), 0, {"usage": found, "usage_source": "file"}, rest
    fields = {"usage": None, "usage_source": "adapter" if TOKENS.fullmatch(tokens) else "none"}
    return tokens, unread, {**fields, **({"usage_error": problem} if problem else {})}, rest
