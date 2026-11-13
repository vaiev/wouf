"""teamlib.receipt: the change receipt's format and signature, schema wouf.receipt.v1 (docs/RECEIPTS.md). A library
without commands: the canonical JSON of profile wouf-cj1, a strict parser of the stored file, the field checks of the
schema, and signing and checking through OpenSSH's ssh-keygen -Y with the namespace wouf-receipt-v1 and the
federation key. Building a receipt, storing it and the verify command use it; none of them lives here.

Every refusal raises Refused, whose message names a field path and a rule, never a value: the bytes of a receipt are
hostile until they are checked, and a value may hold a key. Kernel order: core <- receipt."""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from teamlib.core import (CLASS_NAME, KNOWN, MODEL, config, get, mask_secrets, member_problem, put, secret_in,
                          task_problem, team_dir, visible)

SCHEMA = "wouf.receipt.v1"
PROFILE = "wouf-cj1"
NAMESPACE = "wouf-receipt-v1"  # never federation's wouf-a2a: an envelope signature is no receipt signature
KEY_TYPE = "ssh-ed25519"
UNKNOWN = "unknown"
MAX_FILE = 64 * 1024
MAX_DEPTH = 8          # the receipt object is level 1
MAX_TEXT = 500
MAX_INT = 2 ** 53 - 1  # the integers every JSON reader holds exactly
TIME = "%Y-%m-%dT%H:%M:%SZ"
AT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
# federate's SIG, written again: the armored block of ssh-keygen -Y sign, newlines allowed (tests compare the two).
SIG = re.compile(r"^-----BEGIN SSH SIGNATURE-----\n[A-Za-z0-9+/=\n]{1,4000}-----END SSH SIGNATURE-----\n?\Z")
# log's TOKEN_LIKE, written again (tests compare the two): long runs of key characters.
TOKEN_LIKE = re.compile(r"[A-Za-z0-9+_=]{32,}")
SHA = re.compile(r"[0-9a-f]{40}")           # SHA-1 repositories only in v1
SHA_PREFIX = re.compile(r"[0-9a-f]{7,40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
TEAM = re.compile(r"[a-z][a-z0-9-]{1,30}")  # federate's PEER: a [federation] name
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}")  # a runner, role or outcome name; a CLI's basename, no path
TIER = re.compile(r"(?:light|mid|strong)(?:->(?:light|mid|strong))?")  # mid->strong: ASCII for the fallback
TOKENS = re.compile(r"[0-9]{1,12}/[0-9]{1,12}/[0-9]{1,12}/[0-9]{1,12}")  # usage.TOKENS: in/cache_read/cache_write/out
HOST_PORT = re.compile(r"(?:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?|\[[0-9A-Fa-f:.]{2,45}\]):[0-9]{1,5}")
PATH_KEY = re.compile(r"[a-z][a-z0-9_]{0,39}")  # a key a refusal may name; any other is shown as ?
VENDORS = {"claude": "anthropic", "codex": "openai", "agy": "google", "gemini": "google"}
VERDICTS = ("approve", "changes", "stale", "blocked", UNKNOWN)
NETWORKS = ("none", "bridge", "host", UNKNOWN)
EGRESS_MODES = ("proxy", "bridge", "none", "host", UNKNOWN)
MERGES = ("squash", "fast-forward")

# The keys of each object. bin/team reads COMMANDS and USAGE of every teamlib module: this one defines neither.
TOP_KEYS = ("schema", "team", "signed_at", "resigned_at", "repo", "task", "class", "brief", "change", "gate",
            "reviews", "runs", "usage", "flags")
BRIEF_KEYS = ("sha256", "source")
CHANGE_KEYS = ("base", "reviewed", "landed", "landed_parent", "tree", "files", "merge", "pr")
GATE_KEYS = ("state", "head", "base", "code", "network", "command_sha256", "complexity")
COMPLEXITY_KEYS = ("files", "functions", "failures", "accepted")
REVIEWS_KEYS = ("required", "approved", "rounds")
ROUND_KEYS = ("run", "index", "bot", "runner", "cli", "vendor", "model", "verdict", "head", "approve_sha",
              "counts", "source")
RUN_KEYS = ("run", "role", "bot", "runner", "cli", "vendor", "model", "tier", "commit", "outcome", "tokens",
            "egress")
EGRESS_KEYS = ("mode", "log", "hosts", "private", "refused")
USAGE_KEYS = ("tokens", "known_runs", "unknown_runs", "cost")


class Refused(ValueError):
    """A receipt, or bytes that claim to be one, that does not pass. The message names a field and a rule, never a
    value, so it is safe to print."""


# ---------------------------------------------------------------- canonical JSON (wouf-cj1)

def json_types(value, depth: int = 0) -> None:
    """Only the JSON types, exactly: objects with text keys, arrays, text, integers, true, false and null. A float,
    a tuple or a subclass would have a second byte form, or none."""
    if depth > 4 * MAX_DEPTH:
        raise Refused("the value is nested too deep")
    kind = type(value)
    if kind is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise Refused("an object key that is not text")
            json_types(item, depth + 1)
    elif kind is list:
        for item in value:
            json_types(item, depth + 1)
    elif kind is float:
        raise Refused("a float: wouf-cj1 holds integers only")
    elif value is not None and kind not in (str, int, bool):
        raise Refused("a value that is no JSON type")


def canonical(value) -> bytes:
    """The one byte form of a value (SPEC 2.3, profile wouf-cj1): sorted keys, no spaces, ASCII escapes, no NaN.
    The signed bytes are canonical(receipt); the stored file is canonical({"receipt": ..., "sig": ...}) + newline."""
    json_types(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii")


def child(path: str, key) -> str:
    """The path of an item. A key is named only when it looks like a schema name: other keys are input."""
    if type(key) is int:
        return f"{path}[{key}]"
    return f"{path}.{key}" if PATH_KEY.fullmatch(key) and not secret_kind(key) else f"{path}.?"


def deeper(path: str, depth: int) -> None:
    if depth > MAX_DEPTH:
        raise Refused(f"{path} is nested deeper than {MAX_DEPTH} levels")


def check_domain(receipt) -> None:
    """The value domain of a receipt (SPEC 2.3): objects with text keys, arrays, text of printable ASCII (0x20-0x7e,
    at most 500 characters), integers 0 to 2^53-1 (never a bool for one), true, false, null; nesting at most 8."""
    def walk(value, path: str, depth: int) -> None:
        kind = type(value)
        if kind in (dict, list):
            deeper(path, depth)
            for key, item in (value.items() if kind is dict else enumerate(value)):
                if kind is dict:
                    if type(key) is not str:
                        raise Refused(f"{path} has a key that is not text")
                    text(key, f"a key of {path}")
                walk(item, child(path, key), depth + 1)
        elif kind is str:
            text(value, path)
        elif kind is int:
            if not 0 <= value <= MAX_INT:
                raise Refused(f"{path} is an integer outside 0 to 2^53-1")
        elif kind is float:
            raise Refused(f"{path} is a float: a receipt holds integers only")
        elif value is not None and kind is not bool:
            raise Refused(f"{path} is no JSON value")

    def text(value: str, path: str) -> None:
        if len(value) > MAX_TEXT:
            raise Refused(f"{path} is longer than {MAX_TEXT} characters")
        if not all(" " <= c <= "~" for c in value):
            raise Refused(f"{path} holds a character that is not printable ASCII (a control or non-ASCII character)")

    if type(receipt) is not dict:
        raise Refused("receipt must be an object")
    walk(receipt, "receipt", 1)


# ---------------------------------------------------------------- the secret rule

def token_like(text: str) -> bool:
    """log's token_like: a long run of key characters that mixes upper case, lower case and digits."""
    for match in TOKEN_LIKE.finditer(text):
        word = match.group(0)
        if all(re.search(c, word) for c in ("[A-Z]", "[a-z]", "[0-9]")):
            return True
    return False


def secret_kind(text: str) -> str | None:
    """The rule of log add and federate: a key shape main's patterns know, or a random-looking token; and any value
    registered for this process."""
    if any(value and value in text for value in KNOWN):
        return "a registered secret value"
    return secret_in(text) or ("a token" if token_like(text) else None)


def refuse_secrets(receipt) -> None:
    """Before any other check, so no later diagnostic can stand next to a key. Every text of the receipt, keys
    included, and every integer as the digits it is in the file; sig is never part of a receipt."""
    def walk(value, path: str, depth: int) -> None:
        kind = type(value)
        if kind in (dict, list):
            deeper(path, depth)
            for key, item in (value.items() if kind is dict else enumerate(value)):
                if kind is dict and type(key) is str and secret_kind(key):
                    found(f"a key of {path}", secret_kind(key))
                walk(item, child(path, key) if type(key) in (str, int) else f"{path}.?", depth + 1)
        elif kind in (str, int):  # an integer outside the domain is refused later; its digits need not be made
            kind_found = secret_kind(value if kind is str else str(value) if abs(value) <= MAX_INT else "")
            if kind_found:
                found(path, kind_found)

    def found(where: str, kind: str) -> None:
        raise Refused(f"{where} looks like it contains a secret ({kind}); a receipt holds ids, names, hex and "
                      "counts, never values")

    walk(receipt, "receipt", 1)


# ---------------------------------------------------------------- field checks (SPEC 2.1)

def need(value, keys: tuple, path: str) -> dict:
    """An object with exactly these keys. An unknown key is not named: it is input."""
    if type(value) is not dict:
        raise Refused(f"{path} must be an object")
    if set(value) - set(keys):
        raise Refused(f"{path} has an unknown key")
    missing = [k for k in keys if k not in value]
    if missing:
        raise Refused(f"{path} misses the key {missing[0]}")
    return value


def matches(value, rule: re.Pattern, path: str, what: str, unknown: bool = False) -> str:
    if unknown and value == UNKNOWN:
        return value
    if type(value) is not str or not rule.fullmatch(value):
        raise Refused(f"{path} must be {what}" + (" or unknown" if unknown else ""))
    return value


def sha(value, path: str, unknown: bool = False) -> str:
    return matches(value, SHA, path, "40 lower-case hex (a SHA-1 commit id)", unknown)


def whole(value, path: str, low: int = 0) -> int:
    if type(value) is not int or not low <= value <= MAX_INT:
        raise Refused(f"{path} must be a whole number from {low}")
    return value


def one_of(value, choices: tuple, path: str):
    if type(value) is not str or value not in choices:
        raise Refused(f"{path} must be one of: {', '.join(choices)}")
    return value


def moment(value, path: str) -> datetime:
    try:
        if type(value) is not str or not AT.fullmatch(value):
            raise ValueError
        return datetime.strptime(value, TIME).replace(tzinfo=timezone.utc)
    except ValueError:
        raise Refused(f"{path} must be a real UTC time YYYY-MM-DDTHH:MM:SSZ") from None


def named(value, check, path: str, what: str) -> str:
    if type(value) is not str or check(value):
        raise Refused(f"{path} must be {what}")
    return value


def check_brief(brief) -> None:
    need(brief, BRIEF_KEYS, "receipt.brief")
    matches(brief["sha256"], SHA256, "receipt.brief.sha256", "64 lower-case hex")
    one_of(brief["source"], ("flow", "file"), "receipt.brief.source")


def check_change(change) -> dict:
    path = "receipt.change"
    need(change, CHANGE_KEYS, path)
    for key in ("base", "reviewed", "landed", "landed_parent", "tree"):
        sha(change[key], f"{path}.{key}")
    whole(change["files"], f"{path}.files", 1)  # files = 0: no change to vouch for
    one_of(change["merge"], MERGES, f"{path}.merge")
    if change["pr"] is not None:
        whole(change["pr"], f"{path}.pr", 1)
    if change["merge"] != "fast-forward" and change["landed_parent"] != change["base"]:
        raise Refused(f"{path}.landed_parent must equal {path}.base unless the merge is fast-forward")
    return change


def check_gate(gate, change: dict) -> None:
    path = "receipt.gate"
    need(gate, GATE_KEYS, path)
    one_of(gate["state"], ("green",), f"{path}.state")
    sha(gate["head"], f"{path}.head")
    sha(gate["base"], f"{path}.base")
    if gate["code"] is not None and (type(gate["code"]) is not int or gate["code"] != 0):
        raise Refused(f"{path}.code must be 0 or null (no gate command) for a green gate")
    one_of(gate["network"], NETWORKS, f"{path}.network")
    matches(gate["command_sha256"], SHA256, f"{path}.command_sha256", "64 lower-case hex", unknown=True)
    if gate["complexity"] != UNKNOWN:
        need(gate["complexity"], COMPLEXITY_KEYS, f"{path}.complexity")
        for key in COMPLEXITY_KEYS:
            whole(gate["complexity"][key], f"{path}.complexity.{key}")
    if gate["head"] != change["reviewed"]:
        raise Refused(f"{path}.head must equal receipt.change.reviewed")
    if gate["base"] != change["base"]:
        raise Refused(f"{path}.base must equal receipt.change.base")


def check_tool(item: dict, path: str) -> None:
    """runner, cli, vendor and model of a round or a run: names from the config, the CLI's basename only."""
    matches(item["runner"], NAME, f"{path}.runner", "a runner name")
    matches(item["cli"], NAME, f"{path}.cli", "the basename of the CLI program (no path)")
    if item["vendor"] != VENDORS.get(item["cli"], UNKNOWN):
        raise Refused(f"{path}.vendor must be the vendor of {path}.cli (claude: anthropic, codex: openai, "
                      "agy and gemini: google, else unknown)")
    matches(item["model"], MODEL, f"{path}.model", "a model name, default or unknown")


def counts(item: dict, reviewed: str) -> bool:
    """The rule of flow.py and team merge propose: an approve on exactly the reviewed head, whose typed sha is a
    prefix of it of 7 to 40 hex."""
    typed = item["approve_sha"]
    return (item["verdict"] == "approve" and item["head"] == reviewed and type(typed) is str
            and SHA_PREFIX.fullmatch(typed) is not None and reviewed.startswith(typed))


def check_reviews(reviews, change: dict) -> None:
    path = "receipt.reviews"
    need(reviews, REVIEWS_KEYS, path)
    whole(reviews["required"], f"{path}.required")
    whole(reviews["approved"], f"{path}.approved")
    if type(reviews["rounds"]) is not list:
        raise Refused(f"{path}.rounds must be a list")
    for index, item in enumerate(reviews["rounds"]):
        at = f"{path}.rounds[{index}]"
        need(item, ROUND_KEYS, at)
        named(item["run"], task_problem, f"{at}.run", "a run id")
        whole(item["index"], f"{at}.index", 1)
        named(item["bot"], member_problem, f"{at}.bot", "a bot id")
        check_tool(item, at)
        one_of(item["verdict"], VERDICTS, f"{at}.verdict")
        sha(item["head"], f"{at}.head", unknown=True)
        if item["approve_sha"] is not None:
            matches(item["approve_sha"], SHA_PREFIX, f"{at}.approve_sha", "7 to 40 lower-case hex or null")
        if type(item["counts"]) is not bool:
            raise Refused(f"{at}.counts must be true or false")
        one_of(item["source"], ("flow", "run"), f"{at}.source")
        if item["counts"] and not counts(item, change["reviewed"]):
            raise Refused(f"{at}.counts is true, but only an approve on receipt.change.reviewed whose approve_sha "
                          "is a prefix of it (7 to 40 hex) counts")
    if reviews["approved"] != sum(1 for item in reviews["rounds"] if item["counts"]):
        raise Refused(f"{path}.approved must be the number of rounds that count")


def check_runs(runs) -> list:
    if type(runs) is not list:
        raise Refused("receipt.runs must be a list")
    for index, item in enumerate(runs):
        at = f"receipt.runs[{index}]"
        need(item, RUN_KEYS, at)
        named(item["run"], task_problem, f"{at}.run", "a run id")
        matches(item["role"], NAME, f"{at}.role", "a role name")
        named(item["bot"], member_problem, f"{at}.bot", "a bot id")
        check_tool(item, at)
        matches(item["tier"], TIER, f"{at}.tier", "light, mid, strong or a fallback such as mid->strong",
                unknown=True)
        sha(item["commit"], f"{at}.commit", unknown=True)
        matches(item["outcome"], NAME, f"{at}.outcome", "an outcome name")
        matches(item["tokens"], TOKENS, f"{at}.tokens", "in/cache_read/cache_write/out numbers", unknown=True)
        egress = need(item["egress"], EGRESS_KEYS, f"{at}.egress")
        one_of(egress["mode"], EGRESS_MODES, f"{at}.egress.mode")
        if egress["log"] is not None:
            matches(egress["log"], SHA256, f"{at}.egress.log", "64 lower-case hex, null", unknown=True)
        hosts = egress["hosts"]
        if type(hosts) is not list or not all(type(h) is str and HOST_PORT.fullmatch(h) for h in hosts):
            raise Refused(f"{at}.egress.hosts must be a list of host:port")
        if hosts != sorted(set(hosts)):
            raise Refused(f"{at}.egress.hosts must be sorted, each once")
        whole(egress["private"], f"{at}.egress.private")
        whole(egress["refused"], f"{at}.egress.refused")
    return runs


def check_usage(usage, runs: list) -> None:
    path = "receipt.usage"
    need(usage, USAGE_KEYS, path)
    for key in ("tokens", "known_runs", "unknown_runs"):
        whole(usage[key], f"{path}.{key}")
    if usage["cost"] != UNKNOWN:
        raise Refused(f"{path}.cost must be unknown (v1 has no price table)")
    known = [item["tokens"] for item in runs if item["tokens"] != UNKNOWN]
    if usage["known_runs"] != len(known) or usage["unknown_runs"] != len(runs) - len(known):
        raise Refused(f"{path}.known_runs and unknown_runs must count the runs with and without token numbers")
    if usage["tokens"] != sum(int(n) for tokens in known for n in tokens.split("/")):
        raise Refused(f"{path}.tokens must be the sum of the known token numbers of receipt.runs")


def check_fields(receipt) -> None:
    """The fields of schema wouf.receipt.v1 and their rules, on a receipt that passed refuse_secrets and
    check_domain."""
    need(receipt, TOP_KEYS, "receipt")
    if receipt["schema"] != SCHEMA:
        raise Refused(f"receipt.schema must be {SCHEMA}")
    matches(receipt["team"], TEAM, "receipt.team", "a [federation] name (a-z, 0-9, -)")
    signed = moment(receipt["signed_at"], "receipt.signed_at")
    if receipt["resigned_at"] is not None and moment(receipt["resigned_at"], "receipt.resigned_at") < signed:
        raise Refused("receipt.resigned_at must not be earlier than receipt.signed_at")
    if type(receipt["repo"]) is not str or not receipt["repo"].strip():
        raise Refused("receipt.repo must be a label")
    named(receipt["task"], task_problem, "receipt.task", "a task id")
    matches(receipt["class"], CLASS_NAME, "receipt.class", "a class name")
    check_brief(receipt["brief"])
    change = check_change(receipt["change"])
    check_gate(receipt["gate"], change)
    check_reviews(receipt["reviews"], change)
    check_usage(receipt["usage"], check_runs(receipt["runs"]))
    flags = receipt["flags"]  # only true flags: {"sovereign": 1} is no flag
    if type(flags) is dict and set(flags) - {"sovereign"}:
        raise Refused("receipt.flags has an unknown key")
    if type(flags) is not dict or (flags and flags["sovereign"] is not True):
        raise Refused('receipt.flags must be {} or {"sovereign": true}')


def check_receipt(receipt) -> None:
    """Every rule a receipt meets before it is signed or believed: the secret rule first, the value domain, then the
    fields of wouf.receipt.v1."""
    refuse_secrets(receipt)
    check_domain(receipt)
    check_fields(receipt)


# ---------------------------------------------------------------- the stored file

def unique(pairs: list) -> dict:
    """json object_pairs_hook: a repeated key would be a second reading of one signed object."""
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise Refused("a key appears twice in one object")
    return dict(pairs)


def no_constant(name: str):
    raise Refused("NaN or Infinity: not wouf-cj1")


def no_float(text: str):
    raise Refused("a float: a receipt holds integers only")


def stored(receipt, sig: str) -> bytes:
    """The bytes of the stored file: canonical({"receipt": ..., "sig": ...}) and one newline, at most 64 KiB."""
    check_receipt(receipt)
    if type(sig) is not str or not SIG.match(sig):
        raise Refused("sig is not one armored SSH signature")
    data = canonical({"receipt": receipt, "sig": sig}) + b"\n"
    if len(data) > MAX_FILE:
        raise Refused("the receipt file would be over 64 KiB")
    return data


def bracket_depth(text: str) -> int:
    """The deepest nesting of [ and { outside JSON strings. Checked before json.loads, because whether the
    decoder raises RecursionError on deep input depends on the Python version (3.14 parses 5000 levels)."""
    depth = deepest = 0
    in_string = escaped = False
    for char in text:
        if escaped:
            escaped = False
        elif in_string:
            escaped = char == "\\"
            in_string = char != '"'
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            deepest = max(deepest, depth)
        elif char in "]}":
            depth -= 1
    return deepest


def parse(data: bytes) -> dict:
    """The stored file, read strictly: at most 64 KiB of ASCII, JSON without a repeated key, a float, NaN or
    Infinity; exactly receipt and sig; sig one armored signature; the receipt passing check_receipt; and the bytes
    equal to the canonical form of what they parse to, so a receipt has one byte form. The signature is not checked
    here (signature_ok)."""
    if type(data) is not bytes:
        raise Refused("a receipt file is read as bytes")
    if len(data) > MAX_FILE:
        raise Refused("the receipt file is over 64 KiB")
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        raise Refused("the receipt file holds a byte that is not ASCII") from None
    if bracket_depth(text) > 4 * MAX_DEPTH:
        raise Refused("the receipt file is nested too deep")
    try:
        value = json.loads(text, object_pairs_hook=unique, parse_constant=no_constant, parse_float=no_float)
    except Refused:
        raise
    except RecursionError:
        raise Refused("the receipt file is nested too deep") from None
    except ValueError:
        raise Refused("the receipt file is not JSON") from None
    if type(value) is not dict or set(value) != {"receipt", "sig"}:
        raise Refused("the receipt file must hold exactly receipt and sig")
    if type(value["sig"]) is not str or not SIG.match(value["sig"]):
        raise Refused("sig is not one armored SSH signature")
    refuse_secrets(value["receipt"])
    check_domain(value["receipt"])
    if data != canonical(value) + b"\n":
        raise Refused(f"the receipt file is not in its canonical form ({PROFILE}, one newline at the end)")
    check_fields(value["receipt"])
    return value


# ---------------------------------------------------------------- signature (SPEC 2.4)

def ssh_keygen() -> str:
    found = shutil.which("ssh-keygen")
    if not found:
        raise Refused("ssh-keygen (OpenSSH 8.7 or newer) is needed to sign and check receipts")
    return found


def signing_key(value: str | None = None) -> str:
    """The path of the federation's private key, [federation] signing_key (or value), checked as federate does:
    outside the team folder, its .pub next to it, Ed25519. The private key itself is never opened here; only
    ssh-keygen reads it."""
    if value is None:  # a library refuses, it never exits: no config.ini is no key
        value = get(config(), "federation", "signing_key") if (team_dir() / "config.ini").is_file() else ""
    if not value:
        raise Refused("set [federation] signing_key in team/config.ini to an Ed25519 key outside the team folder")
    path = Path(value).expanduser().resolve()
    team = team_dir()
    if path == team or team in path.parents:
        raise Refused("[federation] signing_key must be outside the team folder, which goes into git")
    if not path.is_file():
        raise Refused("[federation] signing_key: no such file")
    public = Path(f"{path}.pub")  # the key type is read from here
    if not public.is_file():
        raise Refused("[federation] signing_key needs its .pub next to it (ssh-keygen writes both)")
    if public.read_text(encoding="utf-8", errors="replace").split()[:1] != [KEY_TYPE]:
        raise Refused("[federation] signing_key must be an Ed25519 key (ssh-keygen -t ed25519)")
    return str(path)


def sign(receipt, key: str | None = None) -> str:
    """The armored signature of canonical(receipt), namespace wouf-receipt-v1, made by ssh-keygen with the
    federation key. The receipt passes check_receipt first: no key shape reaches the signed bytes."""
    check_receipt(receipt)
    path = signing_key(key)
    try:
        done = subprocess.run([ssh_keygen(), "-Y", "sign", "-q", "-f", path, "-n", NAMESPACE],
                              input=canonical(receipt), capture_output=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise Refused("ssh-keygen did not sign within 120 seconds (a key with a passphrase?)") from None
    signature = done.stdout.decode("ascii", "replace")
    if done.returncode or not SIG.match(signature):
        why = visible(mask_secrets(done.stderr.decode("utf-8", "replace").strip()[:200], KNOWN))
        raise Refused(f"ssh-keygen could not sign the receipt: {why}")
    return signature


def trusted_lines(text: str, principal: str) -> list[str]:
    """The allowed_signers lines that trust a plain Ed25519 key for principal: federate's trusted_lines, written
    again (tests compare the two on one table). Other key types and CA lines never count, whatever ssh-keygen would
    accept. Stricter than federate in one point: ssh-keygen reads option names in any case, so CERT-AUTHORITY is a
    CA line too."""
    found = []
    for line in text.split("\n"):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            words = shlex.split(line)
        except ValueError:
            continue
        if not words:
            continue
        kind = next((w for w in words[1:] if w.startswith(("ssh-", "ecdsa-", "sk-"))), None)
        options = words[1:words.index(kind)] if kind else []
        if principal in words[0].split(",") and kind == KEY_TYPE and \
                not any("cert-authority" in o.lower() for o in options):
            found.append(line)
    return found


def verify_time(stamp: str) -> str:
    """A receipt time as ssh-keygen's verify-time takes it: 2026-11-06T12:00:00Z -> 20261106120000Z. ssh-keygen
    takes only YYYYMMDD[HHMM[SS]][Z] and exits 255 on the ISO form; Z keeps it UTC."""
    t = moment(stamp, "the time")
    return f"{t.year:04d}{t.month:02d}{t.day:02d}{t.hour:02d}{t.minute:02d}{t.second:02d}Z"


def signature_time(receipt: dict) -> str:
    """The time a signature is checked at: resigned_at when set, else signed_at."""
    return receipt["resigned_at"] if receipt.get("resigned_at") is not None else receipt["signed_at"]


def signature_ok(receipt, sig, signers, revoked=None) -> bool:
    """Whether sig is a good wouf-receipt-v1 signature over canonical(receipt) by a key the signers file trusts for
    the receipt's team, at the receipt's time: only plain Ed25519 lines count, their valid-after and valid-before
    hold at that time, and a key listed in revoked (when that file exists) fails. A receipt that does not pass
    check_receipt is never well signed."""
    try:
        check_receipt(receipt)
    except Refused:
        return False
    if type(sig) is not str or not SIG.match(sig):
        return False
    try:
        lines = trusted_lines(Path(signers).read_text(encoding="utf-8"), receipt["team"])
    except (OSError, UnicodeError):
        return False
    if not lines:
        return False
    command = [ssh_keygen(), "-Y", "verify", "-I", receipt["team"], "-n", NAMESPACE,
               "-O", f"verify-time={verify_time(signature_time(receipt))}"]
    if revoked is not None and (Path(revoked).exists() or Path(revoked).is_symlink()):
        command += ["-r", str(revoked)]
    with tempfile.TemporaryDirectory() as folder:
        sig_path, signers_path = Path(folder) / "receipt.sig", Path(folder) / "allowed_signers"
        put(sig_path, sig)  # through the masked sink, as federate does: a mask can only fail the check
        put(signers_path, "".join(f"{line}\n" for line in lines))
        try:
            done = subprocess.run([*command, "-f", str(signers_path), "-s", str(sig_path)],
                                  input=canonical(receipt), capture_output=True, timeout=60)
        except subprocess.TimeoutExpired:
            return False
    return done.returncode == 0
