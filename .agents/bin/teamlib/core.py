"""teamlib.core: the kernel bin/team and its command modules share: constants, the secret patterns and the sinks,
masking and copy helpers, config and bot declarations, the receipt and the secret store, task ids, git and JSON files,
the brief header and the path scope.

Mutable state (KNOWN, LAST_ERROR) is only changed in place, so every module that imported it sees each change.
"""

from __future__ import annotations

import configparser
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
from datetime import date, datetime, timezone
from importlib.machinery import SourceFileLoader
from pathlib import Path, PurePosixPath

PACK = Path(__file__).resolve().parent.parent.parent
PROJECT = PACK.parent
TAIL_LINES = 40
RETURN_LINES = 60
SESSION_HINTS = (("CLAUDECODE", "claude"),)
CONNECTOR = re.compile(r"^[a-z][a-z0-9-]{0,62}\Z")
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
LEVEL = re.compile(r"^[a-z][a-z0-9-]{0,15}$")
MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,99}$")
# [environment] image and user go into the Dockerfile and container commands: plain values only (fullmatch).
IMAGE = re.compile(r"(?:[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::[0-9]{1,5})?/)?"
                   r"[a-z0-9]+(?:[._-]+[a-z0-9]+)*(?:/[a-z0-9]+(?:[._-]+[a-z0-9]+)*)*"
                   r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?(?:@sha256:[0-9a-f]{64})?")
USER = re.compile(r"(?:[a-z_][a-z0-9_-]{0,31}|[0-9]{1,10})(?::(?:[a-z_][a-z0-9_-]{0,31}|[0-9]{1,10}))?")
# Every container the helper creates: no capabilities, no way to gain them, a bound on processes.
HARDEN = ["--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "1024"]

# ---- secret patterns: the same block in bin/team and bin/log (tests/test_log.py compares them) ----
# Anchored: each key's own prefix, alphabet and length, with no key character right before or after it, so
# words, paths, branch names and hex commit ids never match. Prefixes are split so this file holds none.
KEY_BEGIN = r"-{4,5} ?BEGIN [A-Z0-9 _-]{0,40}?PRIVATE KEY(?: BLOCK)? ?-{4,5}"
KEY_END = KEY_BEGIN.replace("BEGIN", "END")
SECRET_PATTERNS = tuple((kind, re.compile(pattern)) for kind, pattern in (
    ("GitHub classic", r"(?<![A-Za-z0-9_])gh[pousr]_[A-Za-z0-9_]{36,251}(?![A-Za-z0-9_])"),
    ("GitHub fine-grained", r"(?<![A-Za-z0-9_])github_" r"pat_[A-Za-z0-9]{22}_[A-Za-z0-9]{59}(?![A-Za-z0-9_])"),
    ("Anthropic", r"(?<![A-Za-z0-9_-])sk-" r"ant-[A-Za-z0-9_-]{20,}"),
    ("OpenAI", r"(?<![A-Za-z0-9_-])sk-(?:(?:proj|svcacct|admin)-[A-Za-z0-9_-]{40,}|"
               r"[A-Za-z0-9_-]{16,}T3Blbk" r"FJ[A-Za-z0-9_-]{16,})(?![A-Za-z0-9_-])"),
    ("AWS access key", r"(?<![A-Za-z0-9_])(?:AKIA|ASIA|ABIA|ACCA|A3T[A-Z0-9])[A-Z0-9]{16}(?![A-Za-z0-9_])"),
    ("Google API key", r"(?<![A-Za-z0-9_-])AIza[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])"),
    ("Slack token", r"(?<![A-Za-z0-9_-])(?:(?:xox[abeprs]|xapp)-[0-9]{1,16}|xw" r"fp)-[A-Za-z0-9-]{10,}|"
                    r"hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]{20,}"),
    ("age secret key", r"(?<![A-Za-z0-9_-])AGE-SECRET" r"-KEY-1[0-9A-Z]{58}(?![A-Za-z0-9_])"),
    # The whole block up to its END line; without one, the header and the key lines right under it.
    ("private key", KEY_BEGIN + r"(?:[\s\S]*?" + KEY_END +
                    r"|[A-Za-z0-9+/=]*(?:\r?\n[A-Za-z0-9+/=]{4,}(?=\r?\n|\Z))*)"),
))
MASK = "***"


def secret_in(text: str) -> str | None:
    """The kind of the first key found in text, or None; never the value."""
    return next((kind for kind, pattern in SECRET_PATTERNS if pattern.search(text)), None)


def mask_secrets(text: str, values=()) -> str:
    """text with every key a pattern recognizes and each passed value replaced by ***. All matches are found in
    the original text, so a passed value that overlaps a key cannot hide its prefix or header; overlaps are one mask."""
    spans = [m.span() for _, pattern in SECRET_PATTERNS for m in pattern.finditer(text)]
    spans += [m.span() for value in values if value for m in re.finditer(re.escape(value), text)]
    merged: list[list[int]] = []
    for start, stop in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], stop)
        else:
            merged.append([start, stop])
    out, pos = [], 0
    for start, stop in merged:
        out += [text[pos:start], MASK]
        pos = stop
    return "".join(out) + text[pos:]

# A store that cannot be read is recorded here, not raised: the caller stops at its first command (usage text aside).
UNREADABLE: list[str] = []
STORE_UNREADABLE = ("the secret store is locked or not answering, so its keys cannot be masked; "
                    "unlock it in your own terminal and run again")


def stored_values() -> list[str]:
    """The values of every secret in the store (bin/secret next to this file), longest first; never shown. A listed
    secret that cannot be read gives none and sets UNREADABLE: nothing is printed or written past that point. A team
    process has read its store already (teamlib.core keeps those reads): they are used, so a value is read once."""
    kept = getattr(sys.modules.get("teamlib.core"), "STORE_READS", None)
    if kept:
        return sorted((v for v in kept.values() if v), key=len, reverse=True)
    try:
        loader = SourceFileLoader("wouf_secret_store", str(Path(__file__).resolve().parent / "secret"))
        spec = importlib.util.spec_from_loader("wouf_secret_store", loader)
        store = importlib.util.module_from_spec(spec)
        loader.exec_module(store)
        names = store.names()
    except (Exception, SystemExit):
        return []
    found = set()
    for name in names:
        try:
            found.add(store.read_value(name))
        except (Exception, SystemExit):
            UNREADABLE.append(STORE_UNREADABLE)
            return []
    return sorted((v for v in found if v), key=len, reverse=True)
# ---- end of secret patterns ----


def substantive_question(item: str) -> bool:
    """Whether a Questions item contains a question rather than an empty placeholder."""
    return item.strip(" .").lower() not in {"", "none", "no", "n/a", "na", "-", "nothing", "no questions", "keine"}


def secret_count(text: str, values=()) -> int:
    """How many places mask_secrets replaces in text: the same spans, overlaps merged as one; never the values."""
    spans = sorted([m.span() for _, pattern in SECRET_PATTERNS for m in pattern.finditer(text)]
                   + [m.span() for value in values if value for m in re.finditer(re.escape(value), text)])
    places, end = 0, -1
    for start, stop in spans:
        if start > end:
            places += 1
        end = max(end, stop)
    return places


# ---------------------------------------------------------------- basics

LAST_ERROR = [""]


# The exact values of the keys passed to a run, masked at every sink next to the recognized key shapes.
KNOWN: list[str] = []

def register(values) -> None:
    """Add exact secret values to the sinks' mask list, longest first; earlier ones stay."""
    KNOWN[:] = sorted({*KNOWN, *(v for v in values if v)}, key=len, reverse=True)


def say(text: str = "", err: bool = False) -> None:
    """The one way out to stdout and stderr: nothing reaches a terminal unmasked."""
    stream = sys.stderr if err else sys.stdout
    stream.write(mask_secrets(text, KNOWN) + "\n")


def sink(handle, text: str, values=()) -> None:
    """The one way into an open file: the text is masked on the way."""
    handle.write(mask_secrets(text, [*KNOWN, *values]))


def put(path: Path, text: str, mode: str = "w") -> None:
    """The one way to write a text file: masked on the way, whatever the field it came from."""
    with path.open(mode, encoding="utf-8") as handle:
        sink(handle, text)


def is_text(data: bytes) -> bool:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def mask_bytes(data: bytes) -> bytes:
    """The bytes with every key and registered value masked, text or not. Bytes that are not UTF-8 are scanned as
    well (surrogateescape keeps them exact), so only the bytes of a secret change in a binary file."""
    return mask_secrets(data.decode("utf-8", "surrogateescape"), KNOWN).encode("utf-8", "surrogateescape")


def put_bytes(path: Path, data: bytes, mode: int = 0o644) -> bool:
    """A file from an archive or a bot, written without following a link; keys and registered values are masked
    in text and in other data alike. Returns whether it was text."""
    text = is_text(data)
    data = mask_bytes(data)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags, 0o644), "wb") as handle:
        handle.write(data)
    os.chmod(path, mode)
    return text


def short_hash(name: str) -> str:
    return hashlib.sha256(name.encode("utf-8", "surrogateescape")).hexdigest()[:8]


def masked_name(name: str) -> str:
    """One file or folder name with every key masked. A changed name keeps a short hash of the original after its
    first word (***-1a2b3c4d.md), so two different keys never become one name."""
    masked = mask_secrets(name, KNOWN)
    if masked == name:
        return name
    stem, dot, ext = masked.partition(".")
    return f"{stem}-{short_hash(name)}{dot}{ext}"


def masked_parts(parts) -> tuple:
    """The masked components of a path. A key that spans components is not found in any one of them: then every
    component is replaced, so the joined path holds nothing of it."""
    out = tuple(masked_name(part) for part in parts)
    if mask_secrets("/".join(out), KNOWN) == "/".join(out):
        return out
    return tuple(f"***-{short_hash(part)}" for part in parts)


def claim(seen: dict, original: tuple, parts: tuple, is_file: bool) -> bool:
    """Record that the original path goes to the masked path parts (a file or a folder, with its ancestors as
    folders). False when another original already holds one of those names, or as another kind: a collision."""
    ok = True
    for n in range(1, len(parts) + 1):
        kind = "file" if is_file and n == len(parts) else "dir"
        if seen.setdefault(parts[:n], (kind, original[:n])) != (kind, original[:n]):
            ok = False
    return ok


COLLISION = "two names would be the same once the names that look like keys are masked"


def copy_masked(src, dst) -> bool:
    """shutil.copy2 for one file: its text is masked on the way, its mode kept. False: binary, copied unchanged."""
    return put_bytes(Path(dst), Path(src).read_bytes(), stat.S_IMODE(os.stat(src).st_mode))


def copy_regular(src: Path, dst: Path, binary: list | None = None) -> list[str]:
    """Copy what a bot handed back: regular files and real folders only. Links are never followed, so nothing
    of this computer can be pulled in through them. Returns what was skipped, relative to src;
    files that are not text are copied unchanged and added to binary (relative names, masked).
    The masked names are checked first: two names that become one refuse the copy before anything is written."""
    if not os.path.lexists(src):
        return []
    if src.is_symlink() or not src.is_dir():
        return ["."]
    skipped = []
    seen: dict = {}
    plan, folders = [], []
    for root, dirs, files in os.walk(src, followlinks=False):
        here = Path(root)
        rel = here.relative_to(src)
        for name in list(dirs):
            if (here / name).is_symlink():
                dirs.remove(name)
                skipped.append(str(rel / name) if str(rel) != "." else name)
        if not claim(seen, rel.parts, masked_parts(rel.parts), False):
            die(f"refusing the results: {COLLISION}")
        folders.append(masked_parts(rel.parts))
        for name in files:
            label = str(rel / name) if str(rel) != "." else name
            try:
                regular = stat.S_ISREG(os.lstat(here / name).st_mode)
            except OSError:
                regular = False
            if not regular:
                skipped.append(label)
            elif claim(seen, (*rel.parts, name), masked_parts((*rel.parts, name)), True):
                plan.append((here / name, masked_parts((*rel.parts, name)), label))
            else:
                die(f"refusing the results: {COLLISION}")
    for parts in folders:
        (dst / Path(*parts)).mkdir(parents=True, exist_ok=True)
    for path, parts, label in plan:
        target = dst / Path(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            if not copy_masked(path, target) and binary is not None:
                binary.append("/".join(parts))
        except OSError:
            # A copy that failed half-way leaves nothing behind: the file is reported as skipped, not kept partly.
            target.unlink(missing_ok=True)
            skipped.append(label)
    return skipped


def masked_link(target: str) -> str:
    """The target of a symbolic link, written as text only (never followed), every component masked like a name.
    Its syntax stays as written: slashes (leading, doubled, trailing), `.` and `..` are kept, never normalized."""
    pieces = target.split("/")
    names = [i for i, piece in enumerate(pieces) if piece not in ("", ".", "..")]
    for i, masked in zip(names, masked_parts([pieces[i] for i in names])):
        pieces[i] = masked
    return "/".join(pieces)


def fetch_masked(at, container: str, src: str, dst: Path, links: bool = False, keep_links: bool = False) -> bool:
    """The folder src of a container, brought to dst as `cp` would, but never as the bot wrote it: the engine's
    archive is read as a stream, and each file is masked in memory before it is written, names included (all
    checked for collisions as they come). Only regular files and folders are written; with links, anything else
    leaves a placeholder link that copy_regular reports as skipped; with keep_links (archival), a symbolic link is
    written as a link with its name and target masked, and never followed. An error of any kind, a file that vanished
    or cannot be read, a failing engine, a collision, removes dst whole and returns False: no partial copy stays."""
    at = at if isinstance(at, dict) else where(at)
    shutil.rmtree(dst, ignore_errors=True)
    dst.mkdir(parents=True)
    proc = subprocess.Popen([at["engine"], "cp", f"{container}:{src}", "-"], env=engine_env(at),
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    ok, collision = False, False
    try:
        seen: dict = {}
        written: dict = {}
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            for member in tar:
                original = tuple(p for p in PurePosixPath(member.name).parts[1:] if p not in ("", ".", "/"))
                if ".." in original or not original:
                    continue
                parts = masked_parts(original)
                if member.isdir() or member.isfile() or member.islnk() or (keep_links and member.issym()):
                    if not claim(seen, original, parts, not member.isdir()):
                        collision = True
                        raise ValueError(COLLISION)
                target = dst / Path(*parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    handle = tar.extractfile(member)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    put_bytes(target, handle.read() if handle else b"", member.mode & 0o777)
                    written[original] = target
                elif member.islnk():
                    # A hard link: the same masked contents under this name, from the copy of the file it links to.
                    linked = tuple(p for p in PurePosixPath(member.linkname).parts[1:] if p not in ("", ".", "/"))
                    if linked not in written:
                        raise ValueError(f"a hard link to {member.linkname}, which is not a file of the copy")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    put_bytes(target, written[linked].read_bytes(), member.mode & 0o777)
                    written[original] = target
                elif keep_links and member.issym():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.symlink(masked_link(member.linkname), target)
                elif links:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.symlink("-", target)
        proc.stdout.read()
        ok = proc.wait() == 0
    except Exception:
        ok = False
    except BaseException:
        # An interrupt is not an error to report: whatever was written so far goes, then it propagates.
        shutil.rmtree(dst, ignore_errors=True)
        raise
    finally:
        if proc.stdout:
            proc.stdout.close()
        if proc.poll() is None:
            proc.kill()
        proc.wait()
    if not ok:
        shutil.rmtree(dst, ignore_errors=True)
        dst.mkdir(parents=True, exist_ok=True)
    if collision:
        die(f"refusing the results: {COLLISION}")
    return ok


def plan_tree(src: Path, follow: bool, label: str) -> list:
    """What a copy of src would write: (folder parts, file name or None, masked parts) for every folder and
    regular file, checked for collisions before anything is written. Links to folders are skipped unless follow."""
    seen: dict = {}
    plan = []
    for root, dirs, files in os.walk(src, followlinks=follow):
        here = Path(root)
        rel = here.relative_to(src).parts
        if not follow:
            dirs[:] = [d for d in dirs if not (here / d).is_symlink()]
        entries = [(rel, None)] + [(rel + (name,), name) for name in files]
        for original, name in entries:
            parts = masked_parts(original)
            if not claim(seen, original, parts, name is not None):
                die(f"refusing to copy {label}: {COLLISION}")
            plan.append((here, name, parts))
    return plan


def copy_tree(src: Path, dst: Path, binary: list | None = None) -> None:
    """shutil.copytree for every copy in and out: names and text masked, links followed as copytree follows them.
    Files that are not text are copied unchanged and added to binary (relative names, masked)."""
    plan = plan_tree(src, True, "an input")
    for here, name, parts in plan:
        if name is None:
            (dst / Path(*parts)).mkdir(parents=True, exist_ok=True)
        elif not copy_masked(here / name, dst / Path(*parts)) and binary is not None:
            binary.append("/".join(parts))


def copy_archive_tree(src: Path, dst: Path) -> None:
    """Host mode archival: like copy_tree, but a symbolic link stays a link (name and target masked) and is never
    followed, so nothing outside src is read. Names are checked for collisions before anything is written."""
    seen: dict = {}
    plan = []
    for root, dirs, files in os.walk(src, followlinks=False):
        here = Path(root)
        rel = here.relative_to(src).parts
        entries = [(rel, "dir")] + [(rel + (n,), "link" if (here / n).is_symlink() else "file")
                                    for n in sorted(files + [d for d in dirs if (here / d).is_symlink()])]
        dirs[:] = [d for d in dirs if not (here / d).is_symlink()]
        for original, kind in entries:
            parts = masked_parts(original)
            if not claim(seen, original, parts, kind != "dir"):
                die(f"refusing to archive: {COLLISION}")
            plan.append((src.joinpath(*original), kind, parts))
    for path, kind, parts in plan:
        target = dst / Path(*parts)
        if kind == "dir":
            target.mkdir(parents=True, exist_ok=True)
        elif kind == "link":
            target.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(masked_link(os.readlink(path)), target)
        elif stat.S_ISREG(os.lstat(path).st_mode):
            target.parent.mkdir(parents=True, exist_ok=True)
            copy_masked(path, target)


def mask_tree(folder: Path) -> list[str]:
    """Host mode: the bot wrote its results in place, so they are masked where they stand, names and text, as
    copy_regular masks what it brings back. Links are left alone; unchanged files are not written again.
    Names are checked first: a masked name that is already taken refuses the whole collection, nothing is renamed.
    Files that are not text are scanned as bytes; returns them, relative and masked."""
    binary: list[str] = []
    if not folder.is_dir() or folder.is_symlink():
        return binary
    for root, dirs, files in os.walk(folder):
        taken = {*dirs, *files}
        new = {n: masked_name(n) for n in taken if not (Path(root) / n).is_symlink()}
        final = [new.get(n, n) for n in taken]
        if len(set(final)) != len(final):
            die(f"refusing the results: {COLLISION}")
    for root, dirs, files in os.walk(folder, topdown=False):
        here = Path(root)
        for name in files:
            path = here / name
            if path.is_symlink() or not path.is_file():
                continue
            data = path.read_bytes()
            if not is_text(data):
                binary.append(masked_name(str((here / name).relative_to(folder))))
            if mask_bytes(data) != data:
                put_bytes(path, data, stat.S_IMODE(os.stat(path).st_mode))
            if masked_name(name) != name:
                os.rename(path, here / masked_name(name))
        for name in dirs:
            if not (here / name).is_symlink() and masked_name(name) != name:
                os.rename(here / name, here / masked_name(name))
    return binary


def archive_add(tar, path: Path, arcname: str) -> None:
    """The one way into an export archive: the member's name is masked (its contents were checked before)."""
    def plain(info):
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        return info

    tar.add(path, arcname="/".join(masked_parts(Path(arcname).parts)), filter=plain)


def dumps(data, **kwargs) -> str:
    """JSON text of data with every string masked first, so an escaped key block cannot slip past."""
    return json.dumps(masked_value(data, KNOWN), **kwargs)


def die(message: str, code: int = 2) -> None:
    LAST_ERROR[0] = mask_secrets(message, KNOWN)
    say(f"team: {message}", err=True)
    raise SystemExit(code)


def visible(text: str) -> str:
    """Text as the human sees it: control and invisible characters escaped, so nothing hides on a terminal."""
    return "".join(c if c.isprintable() else c.encode("unicode_escape").decode("ascii") for c in text)


def team_dir() -> Path:
    return Path(os.environ.get("WOUF_TEAM_DIR") or PACK / "team").resolve()


def env_dir() -> Path:
    return team_dir() / "env"


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:30] or "team"


def config() -> configparser.ConfigParser:
    path = team_dir() / "config.ini"
    if not path.is_file():
        die(f"{path} is missing; set the team up with INSTALL.md")
    ini = configparser.ConfigParser(interpolation=None)
    ini.read(path)
    return ini


def get(ini, section: str, key: str, default: str = "") -> str:
    return ini.get(section, key, fallback=default).strip()


def team_name(ini) -> str:
    return get(ini, "team", "name", "WOUF") or "WOUF"


def base(ini) -> str:
    return f"{slug(team_name(ini))}-{slug(PROJECT.name)}"


def frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    fields = {}
    if text.startswith("---\n"):
        for line in text[4:text.find("\n---", 4)].splitlines():
            key, sep, value = line.partition(":")
            if sep:
                fields[key.strip()] = value.split(" #", 1)[0].strip()
    return fields


def words(value: str) -> list[str]:
    return [w for w in re.split(r"[,\s]+", value or "") if w and w != "none"]


def check_member(member: str) -> None:
    """Refuse an id that is no safe bot id, before anything is created or echoed."""
    problem = member_problem(member)
    if problem:
        die(problem)


def declared_id(declared: str, member: str) -> None:
    """The id a declaration states must be a safe bot id and the one its file name gives."""
    check_member(declared)
    if declared != member:
        die("a bot declaration states an id other than its file name; make them the same")


def bot(member: str) -> dict:
    check_member(member)
    if member == "lead":
        die("bot id 'lead': the lead is this session")
    path = team_dir() / "bots" / f"{member}.md"
    if not path.is_file():
        hint = f"'team bot add {member}'" if (PACK / "bots" / f"{member}.md").is_file() else \
            "write it from .agents/roles/specialist.md"
        die(f"no declaration team/bots/{member}.md; {hint}")
    fields = frontmatter(path)
    fields.setdefault("id", member)
    declared_id(fields["id"], member)
    fields["path"] = path
    return fields


def local_today() -> date:
    return date.today()


def until_days(decl: dict, path: Path) -> int | None:
    """Days through the declared last day, validating even an empty value."""
    if "until" not in decl:
        return None
    value = decl["until"]
    try:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError
        return (date.fromisoformat(value) - local_today()).days
    except ValueError:
        die(f"{path}: until must be a YYYY-MM-DD date")


def until_notice(member: str, decl: dict, days: int | None) -> str:
    return f"{member} ends on {decl['until']} (in {days} days)" if days is not None and 0 <= days <= 3 else ""


def active_bots() -> list[dict]:
    found = []
    for path in sorted((team_dir() / "bots").glob("*.md")):
        check_member(path.stem)
        decl = frontmatter(path)
        days = until_days(decl, path)
        if decl.get("status", "active") in ("active", "trial") and (days is None or days >= 0):
            decl.setdefault("id", path.stem)
            declared_id(decl["id"], path.stem)
            decl["path"] = path
            found.append(decl)
    return found


def display(ini, decl: dict) -> str:
    return f"{team_name(ini)} {decl.get('name') or decl['id'].title()}"


def lead_log(task: str, outcome: str, signal: str) -> None:
    # Masked before it is cut to length and before it is passed on the command line: a cut value is no longer found.
    signal = mask_secrets(signal, KNOWN)[:120]
    done = subprocess.run([sys.executable, str(PACK / "bin" / "log"), "add", "lead", task, outcome, signal],
                          capture_output=True, text=True,
                          env={**os.environ, "WOUF_LOG_DIR": str(team_dir() / "logs"), "WOUF_MASK": json.dumps(KNOWN)})
    if done.returncode:
        say(f"team: receipt not written: {done.stderr.strip()[:200]}", err=True)


def secrets_module():
    loader = SourceFileLoader("wouf_secret", str(PACK / "bin" / "secret"))
    spec = importlib.util.spec_from_loader("wouf_secret", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


# A stored value is read at most once per process. A read that fails is reported once and no read follows it: a store
# that cannot be read cannot mask its keys, so register_stored stops the command (usage text aside).
STORE_READS: dict[str, str | None] = {}
STORE_FAULT: list[str] = []
USAGE_WORDS = ("-h", "--help", "help")


def store_fault_line(store) -> str:
    try:
        kind = store.backend()
    except (Exception, SystemExit):
        kind = "secret"
    return (f"team: the {kind} store cannot be read (locked or not answering), so its keys cannot be masked; "
            "unlock it in your own terminal and run again")


def read_once(store, name: str) -> str | None:
    """The stored value of name, read once per process; None when it is not stored or the store cannot be read."""
    if name in STORE_READS:
        return STORE_READS[name]
    if STORE_FAULT:
        return None
    try:
        value = store.read_value(name)
    except (Exception, SystemExit):
        STORE_FAULT.append(store_fault_line(store))
        return None
    STORE_READS[name] = value
    return value


def stored(names: list[str]) -> dict:
    """Values of the named secrets that are stored; never printed."""
    if not names:
        return {}
    store = secrets_module()
    found = {}
    for name in names:
        value = read_once(store, name)
        if value:
            found[name] = value
    return found


def register_stored() -> None:
    """Every stored secret is a sink value from the first line on, before any argument is checked or echoed.
    A store that cannot be read ends the command with one line, unless the command is the usage text."""
    # The store's names first: they need no config, so a broken config cannot keep their values from the sinks.
    # Read through secrets_module(): stored_values() looks for the store next to its own file, not in PACK/bin.
    try:
        store = secrets_module()
        names = store.names()
    except (Exception, SystemExit):
        names = []
    found = set()
    for name in names:
        found.add(read_once(store, name))
    register(found)
    try:
        # Inside a container the values passed for the run arrive in WOUF_MASK, as they do for log.
        passed = json.loads(os.environ.get("WOUF_MASK") or "[]")
        if isinstance(passed, list):
            register(v for v in passed if isinstance(v, str))
    except ValueError:
        pass
    try:
        ini = config()
        names = [n for section in ini.sections() if section.startswith("runner.")
                 for n in words(get(ini, section, "auth")) if ENV_NAME.match(n)]
        register(stored(sorted(set(names))).values())
    except (Exception, SystemExit):
        # A broken config is reported by the command that needs it, with every value masked by then.
        pass
    if STORE_FAULT:
        say(STORE_FAULT[0], err=True)
        argv = sys.argv[1:]
        if argv and argv[0] not in USAGE_WORDS:
            raise SystemExit(1)


# ---- task ids: the same block in bin/team and bin/log (tests/test_log.py compares them) ----
# Task ids name folders and branches: no ".", "..", hidden or option-like names (fullmatch, so no newline).
TASK = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,39}")
# Names Windows keeps for devices, with or without an extension: no folder can carry them there.
DEVICE = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", re.IGNORECASE)


def task_problem(task: str) -> str | None:
    """Why task cannot name a folder on every host and one component of a git ref, or None. A task id that
    looks like a key is refused without repeating it."""
    kind = secret_in(task)
    if kind:
        return f"the task id looks like it contains a secret ({kind}); name the task, never a value"
    if not TASK.fullmatch(task):
        return f"task {task!r}: 1 to 40 letters, digits, . _ -, not starting with . or -"
    if task.endswith(".") or ".." in task or task.lower().endswith(".lock"):
        return f"task {task!r} cannot be a git branch name: no '.' at the end, no '..', no '.lock' at the end"
    if DEVICE.fullmatch(task):
        return f"task {task!r} is a device name on Windows (CON, PRN, AUX, NUL, COM1-9, LPT1-9); use another id"
    return None


def case_twin(task: str, known) -> str | None:
    """A known task id that differs from task only in case: T-1 and t-1 are one folder on a case-insensitive disk."""
    return next((n for n in sorted(set(known)) if n != task and n.lower() == task.lower() and TASK.fullmatch(n)), None)

# Member (bot) ids name storage, folders, branches and log files: the same ref and portability rules as task ids.
MEMBER_ID = re.compile(r"[a-z][a-z0-9-]{1,30}")


def member_problem(member: str) -> str | None:
    """Why member cannot be a bot id (a folder, a branch part and a log file name on every host), or None. An id
    that looks like a key is refused without repeating it."""
    kind = secret_in(member)
    if kind:
        return f"the member id looks like it contains a secret ({kind}); name the bot, never a value"
    if not MEMBER_ID.fullmatch(member) or member.endswith("-") or "--" in member:
        return f"member id {member!r}: 2 to 31 lowercase letters, digits and -, starting with a letter, no '--', not ending with -"
    if DEVICE.fullmatch(member):
        return f"member id {member!r} is a device name on Windows (CON, PRN, AUX, NUL, COM1-9, LPT1-9); use another id"
    return None
# ---- end of task ids ----


def git(*args: str, check: bool = True, at: Path | str | None = None) -> subprocess.CompletedProcess:
    """git in `at` (a run's repository), by default this folder."""
    result = subprocess.run(["git", "-C", str(at or PROJECT), *args], capture_output=True, text=True)
    if check and result.returncode:
        die(f"git {args[0]} failed: {result.stderr.strip()[:300]}")
    return result


def masked_value(value, values=(), found: list[int] | None = None):
    """A JSON value with every key masked in its strings. found, a one-item list, gets the places replaced added."""
    if isinstance(value, str):
        if found is not None:
            found[0] += secret_count(value, values)
        return mask_secrets(value, values)
    if isinstance(value, list):
        return [masked_value(v, values, found) for v in value]
    if isinstance(value, dict):
        return {masked_value(str(k), values, found): masked_value(v, values, found) for k, v in value.items()}
    if value is not None and not isinstance(value, bool) and mask_secrets(str(value), values) != str(value):
        if found is not None:
            found[0] += 1
        return MASK
    return value


def mask_receipt(line: str, values=(), found: list[int] | None = None) -> str:
    """One receipt line with any key masked in every field, still one JSON line (unchanged when it holds none).
    found, a one-item list, gets the places replaced added, counted on the decoded fields."""
    # The registered values too, on the decoded fields: serialized, a value with a quote or a backslash is escaped.
    values = [*KNOWN, *values]
    try:
        entry = json.loads(line)
    except ValueError:
        if found is not None:
            found[0] += secret_count(line, values)
        return mask_secrets(line, values)
    masked = masked_value(entry, values, found)
    return line if masked == entry else dumps(masked, ensure_ascii=False)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_json(path: Path, data: dict) -> None:
    """Atomic: a reader sees the old or the new file, never half of one. A link left at either name (a gate run
    writes in the folder of its gate.json) is replaced, never followed."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    put(tmp, dumps(data, indent=1), "x")
    os.replace(tmp, path)


def alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name != "posix":
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ---------------------------------------------------------------- briefs and scope
# Here, not in teamlib.flow: the gate reads the brief header and checks the paths too, and flow imports the gate.

CLASSES = ("mechanical", "standard", "complex", "security", "decision", "analysis", "plan")


def brief_header(text: str) -> dict:
    """The lines of a brief that steer a flow: Task, Class (default standard), Paths (globs), Scout (yes/no)."""
    fields: dict = {}
    for line in text.splitlines():
        found = re.match(r"^(Task|Class|Paths|Scout):[ \t]*(.*?)\s*$", line)
        if found and found.group(1).lower() not in fields:
            fields[found.group(1).lower()] = found.group(2)
    klass = (fields.get("class") or "standard").lower()
    if klass not in CLASSES:
        die(f"Class: {klass!r} is not one of {', '.join(CLASSES)}")
    scout = (fields.get("scout") or "").lower()
    return {"task": fields.get("task", ""), "class": klass, "paths": words(fields.get("paths", "")),
            "scout": True if scout == "yes" else False if scout == "no" else None}


def glob_regex(pattern: str) -> re.Pattern:
    """A path glob: ** any depth, * and ? within one folder, a trailing / for everything below a folder."""
    if pattern.endswith("/"):
        pattern += "**"
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(f"^{out}$")


def outside_paths(changed: list[str], allowed: list[str]) -> list[str]:
    """Changed files that no allowed glob covers; no globs means no limit."""
    if not allowed:
        return []
    globs = [glob_regex(a) for a in allowed]
    return [path for path in changed if not any(g.match(path) for g in globs)]
