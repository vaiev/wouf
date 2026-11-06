# Upgrade contract

`.agents/VERSION` is the pack's release version, `MAJOR.MINOR.PATCH` (three
nonnegative integers, no leading zeroes). `team --version` and `team doctor`
report it. The installation's existing `.agents/team/.pack-files` remains the
ownership authority; its format is unchanged. A missing manifest or unowned
VERSION requires the ownership recovery in INSTALL.md, never guessed ownership.

Each release keeps every earlier release entry and script. Add
`.agents/upgrade/<version>.md` with a description of the changes and these exact,
unindented metadata lines, outside code fences:

```text
From: 0.1.0
Step: .agents/upgrade/0.1.1/01-normalize.py
```

`From:` names the immediately preceding release, even when no scripts are needed.
The initial 0.1.0 entry has `From: none` and no steps. Every `.py` file
under the release directory must appear exactly once as a `Step:` line; list
steps in execution order. Do not put Python helpers in that directory. The runner
sorts release entries numerically and requires a complete chain from the installed
version to the target; it refuses missing links and undeclared scripts. Updating
the same version runs no steps. Downgrades need both an explicit `--ref` and
`--force`; no scripts run backwards. Use rollback to restore a prior installation.

Scripts use Python 3.9+ and the standard library only, and define `plan()` and
`apply()` with no arguments. `plan()` returns a list of short, human-readable
descriptions, without side effects. Every step's own plan runs during preflight.
`apply()` must be idempotent: rerunning it on its resulting content changes nothing.
Steps receive an injected `pack` API:

```python
def plan():
    return ['Normalize the managed example']


def apply():
    name = '.agents/skills/example.md'
    text = pack.read_text(name)
    if not text.endswith('\n'):
        pack.write_text(name, text + '\n')
```

Only target files confirmed as pack-owned are available. Steps can read UTF-8
text and replace its contents, preserving modes. They cannot add ownership, retire
files or change VERSION; the target git tree and updater handle those operations.
A user-owned collision is unavailable, even if the release tree includes that path.
Plans see the proposed target files; apply steps see the preceding step's changes.
The runner replaces managed files, runs the scripts in order, writes script results,
then writes VERSION and the manifest. A step failure restores the backup.

Steps run in a fresh interpreter outside the project, without the caller's
environment, with a 30-second limit. They exchange managed bytes through the API;
direct filesystem IO, network and subprocess operations are refused. Do not use
raw IO, environment lookups, imports outside the standard library, or process APIs.
The guard catches accidental misuse; it is not a security sandbox for hostile
Python. Release scripts and the pack's executable code must be trusted and reviewed.
Never access user projects, team state, secrets, keys, bot logins or storages.

Preflight lists versions, the complete path, each step's plan, managed additions,
changes and retirements, nested user-owned files, and disk space. Dirty managed
files or the manifest require `--force`; user changes do not. A preview writes
nothing inside the project. The temporary git clone is outside it and is removed.

Before apply, `.agents/team/backups/<UTC-date>-<id>.tar.gz` records all existing
managed files, their modes and missing paths, the manifest, and safe team state:
board, decisions, config, schedule, tools, provision script, event records/origin,
and Markdown/JSONL declarations, skills, knowledge and logs. Unknown team files,
`env/`, backups, secret stores, key files, logins and storages are excluded. Files
containing recognized or registered secret values cause preflight to stop rather
than archive the values. Backups are private files (0600); the directory is 0700
when created. Keep team activity stopped during apply and rollback.

`team update --list-backups` lists IDs; `team update --rollback [ID]` restores the
chosen backup or the newest one, including recorded team state, removing files
introduced by that update. It never extracts arbitrary archive paths. Rollback
replaces recorded state with the snapshot, so save later work first. Backups stay
available after rollback. An apply failure rolls back automatically and reports
the backup ID. A failed post-update doctor returns nonzero and leaves the applied
pack and its backup available for diagnosis. Changed image inputs prompt
`team update --bots` or `team image`; plain `team update` does not rebuild images.
`team update --bots` (container mode) previews a rebuild of the team image when its
recipe changed; with `--yes` it builds under a temporary tag, puts the new image in
use only after a successful build while no bot task runs (`--wait` waits, nothing
is stopped), then checks each active bot's login read-only. Bot storages, logins
and keys stay as they are.
