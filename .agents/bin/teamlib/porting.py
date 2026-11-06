"""teamlib.porting: the commands export and import: a team's declarations and settings as an archive, and what
an archive may add here (data only; every other file is kept aside, secrets masked, for the human to read)."""

from __future__ import annotations

import gzip
import io
import importlib.machinery

from teamlib.core import *  # noqa: F401,F403


# ---------------------------------------------------------------- export, import

def has_secret(path: Path) -> bool:
    """Whether a file holds a key shape or a registered value (a stored secret, a key passed to a run). Bytes that
    are not UTF-8 are scanned too: a binary file is no hiding place."""
    try:
        text = path.read_bytes().decode("utf-8", "surrogateescape")
    except OSError:
        return False
    return secret_in(text) is not None or mask_secrets(text, KNOWN) != text


def cmd_export(ini, args: list[str]) -> None:
    out, with_data, ids = None, False, []
    it = iter(args)
    for arg in it:
        if arg == "-o":
            out = next(it, None)
        elif arg == "--with-data":
            with_data = True
        else:
            ids.append(arg)
    for i in ids:
        check_member(i)
    team = team_dir()
    bots = [team / "bots" / f"{i}.md" for i in ids] if ids else sorted((team / "bots").glob("*.md"))
    for b in bots:
        check_member(b.stem)
    missing = [b for b in bots if not b.is_file()]
    if missing:
        die(f"no such bot: {', '.join(m.stem for m in missing)}")
    files = [team / "config.ini", team / "tools.md", team / "secrets.md", team / "provision.sh", *bots]
    folders = [team / n for n in ("skills", "connectors")]
    migration_event_files: set[Path] = set()
    for folder in folders:
        files += [p for p in folder.rglob("*") if p.is_file() or p.is_symlink()] if folder.is_dir() else []
    if with_data:
        files += [team / n for n in ("board.md", "decisions.md", "schedule.md")]
        folders += [team / "knowledge", team / "logs"]
        for folder in folders[2:]:
            files += [p for p in folder.rglob("*") if (p.is_file() or p.is_symlink()) and p.name != ".index.sqlite"]
        for b in bots:
            files += list((env_dir() / b.stem / "logs").glob("*.jsonl"))
        # The versioned payload is a snapshot: caches and the local event origin are deliberately excluded.
        event_folder = Path(os.environ.get("WOUF_EVENTS_DIR") or team / "events")
        folders.append(event_folder)
        migration_event_files = {p for p in event_folder.glob("*.jsonl") if p.is_file()} \
            if event_folder.is_dir() else set()
        files += sorted(migration_event_files)
        migration_folders = [team / n for n in ("practice", "flows", "env/goals", "env/lead")]
        folders += migration_folders
        for folder in migration_folders:
            if folder.is_dir():
                files += [p for p in folder.rglob("*") if p.is_file() or p.is_symlink()]
    # A link stores its target's name in the archive, and the target may be named like a key: links are refused.
    links = [f for f in dict.fromkeys([*folders, *files]) if f.is_symlink()]
    if links:
        die("refusing to export links (an archive keeps their targets' names): " +
            ", ".join(masked_name("events" if f in migration_event_files else str(f.relative_to(team)))
                      for f in links))
    files = [f for f in dict.fromkeys(files) if f.is_file()]
    flagged = [f for f in files if has_secret(f)]
    if flagged:
        die("refusing to export files that look like they contain secrets: " + ", ".join(
            str(Path("events") / f.name) if f in migration_event_files else str(f.relative_to(team))
            for f in flagged))
    target = Path(out or f"{slug(team_name(ini))}-team-{datetime.now():%Y%m%d}.tar.gz")
    seen: dict = {}
    for f in files:
        archive_rel = Path("migration/v1/events") / f.name if f in migration_event_files else f.relative_to(team)
        parts = Path("team", *archive_rel.parts).parts
        if not claim(seen, parts, masked_parts(parts), True):
            die(f"refusing to export: {COLLISION}")
    # The gzip header would carry the destination's file name: it is written without one.
    with open(target, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as packed, \
            tarfile.open(fileobj=packed, mode="w") as tar:
        if with_data:
            marker = b"wouf-team-migration-v1\n"
            info = tarfile.TarInfo("team/migration/v1/FORMAT")
            info.size = len(marker)
            tar.addfile(info, io.BytesIO(marker))
        for f in files:
            if f in migration_event_files:
                archive_rel = Path("migration/v1/events") / f.name
            else:
                rel = f.relative_to(team)
                migration = with_data and any(rel == p or p in rel.parents for p in
                                              map(Path, ("practice", "flows", "env/goals", "env/lead")))
                archive_rel = Path("migration/v1") / rel if migration else rel
            archive_add(tar, f, str(Path("team") / archive_rel))
    say(f"exported {len(files)} files to {target}" + (" (with data)" if with_data else " (no data, no secrets)"))


# What import applies: data only. Every other file can carry a command or reach the image build (settings,
# provision.sh, connectors, anything new), so it goes to team/import/<archive>/ as it was, with its secrets masked.
DATA_FILES = ("board.md", "decisions.md", "schedule.md", "tools.md")
DATA_FOLDERS = ("skills/", "knowledge/")
BOT_KEYS = ("id", "name", "role", "status", "runner", "tier", "effort", "connectors", "commits")


def is_bot(rel: str) -> bool:
    parts = rel.split("/")
    return len(parts) == 2 and parts[0] == "bots" and parts[1].endswith(".md")


def is_data(rel: str) -> bool:
    return rel in DATA_FILES or rel.startswith(DATA_FOLDERS) or is_bot(rel)


def proposed(text: str) -> str:
    """A bot declaration with only the known frontmatter keys and status proposed: the human activates it."""
    head, body = "", text
    end = text.find("\n---", 4) if text.startswith("---\n") else -1
    if end != -1:
        head, body = text[4:end], text[end + 4:]
    fields = {}
    for line in head.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() in BOT_KEYS:
            fields[key.strip()] = value.strip()
    fields["status"] = "proposed"
    return "---\n" + "".join(f"{key}: {value}\n" for key, value in fields.items()) + "---" + \
        (body if body.startswith("\n") else "\n" + body)


def unsafe_member(item: tarfile.TarInfo) -> str:
    parts = Path(item.name).parts
    if item.name.startswith("/") or Path(item.name).is_absolute():
        return "absolute path"
    if ".." in item.name.replace("\\", "/").split("/"):
        return "contains '..'"
    if item.issym():
        return "symlink"
    if item.islnk():
        return "hard link"
    if item.isdev():
        return "device"
    if not (item.isfile() or item.isdir()):
        return "not a regular file"
    if not parts or parts[0] != "team" or (item.isfile() and len(parts) < 2):
        return "outside team/"
    return ""


def archive_folder(file: str) -> str:
    """team/import/<this>: the archive's file name without .tar.gz or .tgz, as one safe folder name."""
    name = re.sub(r"(\.tar\.gz|\.tgz)$", "", Path(file).name, flags=re.I)
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")[:60] or "team"


def cmd_import(args: list[str]) -> None:
    usage = "usage: team import FILE [--apply [--force]]"
    apply, force = "--apply" in args, "--force" in args
    names = [a for a in args if a not in ("--apply", "--force")]
    if len(names) != 1 or names[0].startswith("-") or not Path(names[0]).is_file():
        die(usage)
    if force and not apply:
        die("--force only works together with --apply: team import FILE --apply --force")
    shown_file = visible(names[0])
    try:
        with tarfile.open(names[0], "r:gz") as tar:
            members = tar.getmembers()
            refused = [f"{visible(m.name)} ({why})" for m in members for why in [unsafe_member(m)] if why]
            if refused:
                die(f"refusing {shown_file}, it has unsafe members: " + ", ".join(refused))
            files: dict[str, bytes] = {}
            for item in members:
                if item.isdir():
                    continue
                rel = "/".join(Path(item.name).parts[1:])
                if rel in files:
                    die(f"refusing {shown_file}: team/{visible(rel)} is in it more than once")
                source = tar.extractfile(item)
                assert source is not None
                files[rel] = source.read()
    except (tarfile.TarError, OSError, EOFError) as error:
        die(f"{shown_file} is not a team file (.tar.gz): {visible(str(error))}")
    team = team_dir()
    event_folder = Path(os.environ.get("WOUF_EVENTS_DIR") or team / "events")
    prefix = "migration/v1/"
    marker = prefix + "FORMAT"
    migration = marker in files
    migration_members: set[str] = set()
    if migration:
        if files.pop(marker) != b"wouf-team-migration-v1\n":
            die(f"refusing {shown_file}: unsupported migration format")
        allowed = ("events/", "practice/", "flows/", "env/goals/", "env/lead/")
        payload = {rel: data for rel, data in files.items() if rel.startswith(prefix)}
        log = importlib.machinery.SourceFileLoader("wouf_migration_events", str(PACK / "bin" / "log")).load_module()
        for rel, data in payload.items():
            target = rel[len(prefix):]
            if not target.startswith(allowed) or target in files or masked_parts(Path(target).parts) != Path(target).parts:
                die(f"refusing {shown_file}: invalid migration member {visible(rel)}")
            if secret_in(data.decode("utf-8", "surrogateescape")) or mask_secrets(data.decode("utf-8", "surrogateescape"), KNOWN) != data.decode("utf-8", "surrogateescape"):
                die(f"refusing {shown_file}: migration member contains a secret")
            if target.startswith("events/"):
                name = Path(target)
                if len(name.parts) != 2 or name.suffix != ".jsonl" or not log.EVENT_ORIGIN.fullmatch(name.stem):
                    die(f"refusing {shown_file}: invalid event filename")
                try:
                    previous = 0
                    for raw in data.splitlines(keepends=True):
                        event = log.event_parse(raw, name.stem, masked=False)
                        if event["seq"] <= previous or log.event_text_problem(log.event_line(event)):
                            raise ValueError("invalid event sequence or secret")
                        previous = event["seq"]
                except (ValueError, TypeError, UnicodeError, RecursionError):
                    die(f"refusing {shown_file}: invalid event stream")
        # A snapshot cannot be merged safely with another team's state, even with --force.
        if apply and (event_folder.exists() or any((team / p).exists() for p in
                                                   ("env/goals", "env/lead", "flows", "practice"))):
            die("refusing migration into a team with existing events or migration metadata")
        for rel, data in payload.items():
            del files[rel]
            target = rel[len(prefix):]
            files[target] = data
            migration_members.add(target)
    elif any(rel.startswith("migration/") for rel in files):
        die(f"refusing {shown_file}: unknown migration format")
    held = f"import/{archive_folder(names[0])}"
    # Where each member goes, relative to the team folder: data in place, everything else to the quarantine.
    # Names that look like keys are written masked; path and collision checks below run on the masked names.
    # Only members validated under migration/v1/ are authoritative migration data. Unprefixed
    # state-looking paths in the same archive remain ordinary members and are quarantined.
    is_migration = lambda rel: rel in migration_members
    applied = lambda rel: is_data(rel) or is_migration(rel)
    dest = {rel: "/".join(masked_parts(Path(rel if applied(rel) else f"{held}/{rel}").parts)) for rel in files}
    target_path = lambda rel, to: event_folder / Path(rel).relative_to("events") \
        if rel in migration_members and rel.startswith("events/") else team / to
    # How many secrets each file holds, counted on its bytes as they came (receipts: below, on their fields).
    masks = {rel: secret_count(data.decode("utf-8", "surrogateescape"), KNOWN) for rel, data in files.items()}
    seen: dict = {}
    for rel in files:
        parts = Path(rel if applied(rel) else f"{held}/{rel}").parts
        if not claim(seen, parts, tuple(dest[rel].split("/")), True):
            die(f"refusing {shown_file}: two members would have the same name, or one a folder of another, once "
                "the names that look like keys are masked")
    for rel in files:
        if is_bot(rel) and member_problem(Path(rel).stem):
            die(f"refusing {shown_file}: a bot declaration is named {visible(Path(rel).stem)}, which is no safe bot id")
    for rel in files:
        if is_bot(rel):
            try:
                files[rel] = proposed(files[rel].decode("utf-8")).encode("utf-8")
            except UnicodeDecodeError:
                die(f"refusing {shown_file}: team/{visible(rel)} is not UTF-8 text")
        elif rel.endswith(".jsonl") and not is_migration(rel):
            # Receipts from elsewhere never passed this team's log add: they arrive with any key masked.
            # The places are counted on the decoded fields, where the masks go: the bytes hold escaped quotes.
            text = files[rel].decode("utf-8", errors="replace")
            found = [0]
            files[rel] = "".join(mask_receipt(line, found=found) + "\n" for line in text.splitlines()).encode("utf-8")
            masks[rel] = found[0]
    for rel, to in dest.items():
        parts = Path(to).parts
        if rel in migration_members and rel.startswith("events/"):
            target = target_path(rel, to)
            if event_folder.is_symlink() or target.is_symlink():
                die(f"refusing {shown_file}: event destination is a link in this team folder")
        elif any(team.joinpath(*parts[:n]).is_symlink() for n in range(1, len(parts) + 1)):
            # A write through a link would change a file nobody was shown (e.g. tools.md -> config.ini).
            die(f"refusing {shown_file}: team/{visible(to)} is or goes through a link in this team folder")
        target = target_path(rel, to)
        if target.is_dir() or (target.parent.exists() and not target.parent.is_dir()):
            die(f"refusing {shown_file}: team/{visible(to)} would replace a folder or go inside a file")

    exists = {rel for rel, to in dest.items() if target_path(rel, to).exists()}

    def line(rel: str) -> str:
        label = "add" if rel not in exists else ("replace" if force or not apply else "keep")
        shown = visible(rel) if applied(rel) else f"team/{visible(dest[rel])}"
        note = " (as proposed)" if is_bot(rel) else ""
        note += "" if rel not in exists or apply else "   (kept by --apply, replaced by --apply --force)"
        if masks[rel]:
            note += f"   ({masks[rel]} secret{'s' if masks[rel] > 1 else ''} masked)"
        return f"  {label:<8} {shown}{note}"

    data = sorted(rel for rel in files if applied(rel))
    held_back = sorted(rel for rel in files if not applied(rel))
    say(f"team import {shown_file}: " + ("writing" if apply else "nothing written; this is what --apply would do"))
    say("data" + (":" if data else ": none"))
    for rel in data:
        say(line(rel))
    if data and any(is_bot(rel) for rel in data):
        say("  bots arrive as proposed, with only their known keys; the human activates each one")
    say(f"kept for review, not applied (as they were in team/{held}/, secrets masked)"
        + (":" if held_back else ": none"))
    for rel in held_back:
        say(line(rel))
    if held_back:
        say("settings, provision.sh, connectors and other files are never applied: the human reads them in "
            f"team/{held}/ and copies settings over by hand; a file named with masked secrets had them replaced")
    if not apply:
        say(f"to write: team import {shown_file} --apply (add --force to replace existing files)")
        return

    added, kept = 0, 0
    for rel, to in dest.items():
        if rel in exists and not force:
            kept += 1
            continue
        target = target_path(rel, to)
        target.parent.mkdir(parents=True, exist_ok=True)
        put_bytes(target, files[rel])
        added += 1
    say(f"imported {added} files, kept {kept} existing; imported bots are proposed: on the human's yes set "
        f"status: active, then team bot create <id>")


# ---------------------------------------------------------------- commands

def do_export(rest: list[str]) -> int:
    cmd_export(config(), rest)
    return 0


def do_import(rest: list[str]) -> int:
    # No config is read first: import needs none.
    cmd_import(rest)
    return 0


COMMANDS = {"export": do_export, "import": do_import}
USAGE = """
  team export [ID...] [--with-data] [-o FILE]   bot declarations and settings as .tar.gz; never secrets
                                                --with-data includes a v1 state and goal/practice snapshot
  team import FILE [--apply [--force]]          list what an exported team adds; --apply writes only data (bots
                                                as proposed, skills, knowledge, board, decisions, schedule,
                                                tools.md) and puts every other file in team/import/<archive>/
                                                as it was, secrets masked, for the human to read; never applied;
                                                --force replaces existing files
                                                migration requires empty destination state; goal paths may need adjustment
"""
