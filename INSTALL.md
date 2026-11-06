# Install: set up the agent team in this folder

These instructions are for the agent CLI the human runs (Claude Code, Codex CLI, Antigravity (agy), Grok Build, Cursor or another one that reads `AGENTS.md`; Gemini CLI is not tested). The human asked you to set up this team in the folder you were started in. Follow the steps in order, and tell the human in one line what you are doing at each step.

If the human says they are not technical (or seems new to this): talk in plain words, explain each step in one sentence before you do it, give one recommendation as a yes/no question instead of options, and do the technical steps yourself after their yes. Only typing a key is theirs to do; show them exactly what to type.

Ground rules for the whole setup:

- Change only this folder, except the human's secret store (`~/.config/wouf/secrets` or the system keyring, which `.agents/bin/secret` uses in step 4) and software the human asks you to install. Never touch global settings, other files in the human's home directory, or other projects.
- Never overwrite a file that exists. Add to it, or ask.
- Ask before you install software on the human's computer.
- Never ask for a key in chat. Keys are typed by the human in their own terminal (step 4).

## 1. Get the pack

On Windows, run this setup and the lead inside WSL 2 (the helpers need a POSIX environment). If `.agents/START.md` already exists, this folder has the team: go to "Update" below.

Otherwise copy the pack into this folder without overwriting anything, recording the managed pack files in `.agents/team/.pack-files`:

```bash
(
  pack=$(mktemp -d "${TMPDIR:-/tmp}/wouf-pack.XXXXXX") &&
  trap 'rm -rf "$pack"' EXIT &&
  git clone --depth 1 https://github.com/vaiev/wouf.git "$pack/repo" &&
  (cd "$pack/repo" && find .agents -path .agents/team -prune -o -type f -print | sort) |
    while IFS= read -r f; do [ -e "$f" ] || printf '%s\n' "$f"; done > "$pack/files" &&
  cp -Rn "$pack/repo/.agents" . &&
  cp "$pack/files" .agents/team/.pack-files
)
```

If `.agents/` existed before (other tools use it too), the copy only adds files; tell the human about any file name that already existed.

## 2. Adapters for the CLI

Only in this folder:

- **`AGENTS.md`** (read by most agent CLIs): add this line at the end, or create the file with it:
  `This folder runs an agent team. Every agent session reads .agents/START.md first.`
- **Claude Code** reads `CLAUDE.md`: if it does not exist, create it with the single line `@AGENTS.md`; if it exists and does not mention `AGENTS.md`, add that line.
  Then let the helpers run without prompts: add to `.claude/settings.json` (create it, or merge into the existing `permissions.allow` list):

  ```json
  { "permissions": { "allow": ["Bash(.agents/bin/team:*)", "Bash(.agents/bin/log:*)", "Bash(.agents/bin/secret has:*)", "Bash(.agents/bin/secret list)"] } }
  ```

With the human's yes, also merge these session hooks into `.claude/settings.json`.
Append each entry only if it is absent; keep every existing hook in place. The guard hook is optional: with the
human's yes, add `"PreToolUse": [{ "hooks": [{ "type": "command", "command": "python3 .agents/bin/guard hook" }] }]`
(see `[guard]` in `.agents/team/config.ini`); if a `PreToolUse` entry is there, leave it untouched and in place.

  ```json
  { "hooks": {
    "SessionStart": [{ "matcher": "startup|resume|compact", "hooks": [{ "type": "command", "command": ".agents/bin/team context --claude" }] }],
    "SessionEnd": [{ "hooks": [{ "type": "command", "command": ".agents/bin/team session end --claude" }] }]
  } }
  ```

`SessionStart` supplies the current team context; `SessionEnd` records the end state and
usage numbers through `bin/log`, reading only the transcript path supplied by Claude Code.
Transcript text is never stored. All commands also work without hooks: other CLIs follow
`.agents/START.md`. To close manually, run `.agents/bin/team session end`; optionally pass
`--transcript PATH` for numerical measurements from a transcript the human supplies.
Missing measurements are recorded as null, never as zero.

- **Gemini CLI** (not tested) reads `GEMINI.md`: same as `CLAUDE.md`, with the line `@AGENTS.md`.
- **Codex CLI, Antigravity, Grok Build, Cursor, GitHub Copilot** read `AGENTS.md` directly; nothing more to do.

## 3. Check the requirements

- `python3 --version` (3.9 or newer) and `git --version`. The helpers in `.agents/bin/` are standard-library Python.
- A container engine for the bots' sandboxes: `docker info` or `podman info`. If none is installed or running, do not install one yet; the setup in the next step recommends one for this system.
- On Linux, if the human cannot or will not install Docker or Podman: `command -v bwrap` (bubblewrap, a small package most distributions have). The setup then offers `bwrap` before `host`. Tell the human in plain words what it does not do: each bot still gets its own closed box and its own storage and cannot see their home folder or this folder, but a bot may use all of the computer's power unless the system can limit it, the extra safety filter a container has is missing, and a tool that needs the administrator is not installed for the bots: the human installs it. A cloud agent CLI also needs the egress proxy (`network = egress`, with `[egress] proxy` and the CLI's hosts), because bwrap never gives a bot the open network. The proxy comes from the catalog, not from `.agents/`: copy `catalog/tools/egress-proxy/egress_proxy.py` from a new clone of the repository (step 1 removes its clone) into `.agents/team/` and set `[egress] proxy` to that path.

## 4. Run the setup

Read `.agents/START.md`, `.agents/rules.md` and `.agents/roles/lead.md`; from now on you are the team's lead. Then follow `.agents/skills/setup.md`: one round of questions to the human (where bots run, which agent CLI and models, keys, name and autonomy), then create the bots, run `.agents/bin/team doctor` until it says "all good", and run the smoke test.

## 5. Hand over

Tell the human, in at most eight lines:

- They talk to the team's lead by its name (WOUF unless they renamed it), in this folder, with the CLI they already use.
- "without WOUF" as the first message gives a normal session without the team.
- Settings live in `.agents/team/config.ini`; bots in `.agents/team/bots/`; they can change both by asking the lead.
- What was set up, and what is still missing (for example a key or a container engine).
- More is ready when they need it: example bots (research, writer, tester, ops) and connectors (GitHub, Context7, Playwright, Memory), all off until they say yes.

## Update

Run `.agents/bin/team update` to preview an update from the public WOUF repository's
default branch, then `.agents/bin/team update --yes` to apply the displayed plan and
run `team doctor`. Use `--from URL|PATH` for another git repository or local clone,
and `--ref REF` for a tag or commit. Local clones supply committed files only.

The plan shows the current and target release versions, the upgrade path and each
step's dry-run, the source commit, files added, changed and retired, nested user-owned
files kept, and available disk space. Release entries in `.agents/upgrade/` describe
and chain every version's steps; see [.agents/upgrade/README.md](.agents/upgrade/README.md).
Preview writes nothing in the project. The source must be a WOUF pack with a VERSION;
it is cloned outside the project and removed on success or failure. Downgrading
requires both an explicit `--ref` and `--force`; steps do not run backwards.

The command requires the ownership manifest described below, with `.agents/VERSION`
confirmed as pack-owned. It refuses uncommitted changes to managed files or the
manifest; commit or restore them first, or use `--force` to replace them deliberately.
In a plain folder without git, that check is unavailable and the command says so.
User files, including nested files and name collisions, remain user-owned; team
settings and state are preserved. Only caches belonging to changed or retired managed
Python modules are cleaned. Unsafe paths and symlinks are refused even with `--force`.

Stop team activity before applying or rolling back. Before apply, a dated archive in
.agents/team/backups/ saves every managed file and safe team state (board, decisions,
events, config and declarations). Secret stores, keys, bot logins, bot storage and
unknown team files are excluded; recognized secret material stops the update before
backup. `team update --list-backups` lists IDs. `team update --rollback ID` restores a
snapshot; omit ID for the newest. Rollback also restores recorded team state, replacing
later edits to that state, so save later work first. Apply failures automatically
roll back. A failed post-update doctor leaves the applied update and its backup for
diagnosis and returns nonzero. Changed team image inputs offer `team update --bots`:
preview changed runner install lines, tools and affected bots, then use
`team update --bots --yes` to rebuild and check each active bot's `login_check`.
Running tasks block the refresh; add `--wait` to wait for them without stopping them.
Bot storages, logins and keys stay, the secret store is not read, and a failed rebuild
keeps the previous image in use. Repeating the refresh with an unchanged recipe does nothing.
Compare the new pack's default config with `.agents/team/config.ini` separately as
below; the update never changes the team's settings.

Manual fallback (also for installations whose helper predates `team update`):

Update and removal require the ownership manifest from step 1. Older installations
have no manifest: stop before changing files or removing bots. Do not infer ownership
from the current pack or create an empty manifest. Compare the originally installed
pack with this folder, ask the human to resolve overlapping/custom files, and record
only confirmed pack-owned file paths (one `.agents/` path per line, excluding
`.agents/team/`) in `.agents/team/.pack-files`. Include confirmed retired files;
keep unrelated files out. For a pre-version installation, confirm the installed
release before adding `.agents/VERSION` to the manifest and writing its version; do
not guess it from the latest pack. If the installation is the initial unversioned
0.1.0 pack, record `0.1.0`. Back up the folder before resolving ownership. If the
original pack or ownership cannot be established, keep this installation untouched
and set up a fresh folder instead.

Replace the pack and keep the team: clone the new pack to a temporary folder as in step 1, remove only the managed pack files recorded in `.agents/team/.pack-files` so retired pack files do not remain, copy the new `.agents/` over without `.agents/team/` or overwriting pre-existing files (keeping `.agents/team/` and any pre-existing or unrelated top-level and nested files in `.agents/` untouched), and update `.agents/team/.pack-files`:

```bash
(
  [ -f .agents/team/.pack-files ] || {
    echo "ownership manifest missing: stop; follow INSTALL.md Update ownership guidance" >&2
    exit 1
  }
  pack=$(mktemp -d "${TMPDIR:-/tmp}/wouf-pack.XXXXXX") &&
  trap 'rm -rf "$pack"' EXIT &&
  git clone --depth 1 https://github.com/vaiev/wouf.git "$pack/repo" &&
  if [ -f .agents/team/.pack-files ]; then
    while IFS= read -r f; do
      [ -n "$f" ] || continue
      rm -f "$f" || exit 1
      case "$f" in *.py)
        rm -f "${f%/*}/__pycache__/$(basename "${f%.py}")".*.pyc || exit 1 ;;
      esac
    done < .agents/team/.pack-files
  fi &&
  rm -rf "$pack/repo/.agents/team" &&
  (cd "$pack/repo" && find .agents -type f -print | sort) |
    while IFS= read -r f; do [ -e "$f" ] || printf '%s\n' "$f"; done > "$pack/files" &&
  cp -Rn "$pack/repo/.agents" . &&
  cp "$pack/files" .agents/team/.pack-files &&
  { find .agents -depth -type d -exec rmdir {} \; 2>/dev/null || true; }
)
```

Only bytecode caches for recorded Python files are cleaned; unrelated caches remain.

Then compare `.agents/team/config.ini` with the new default: add new sections and keys, keep the human's values. Tell the human what changed.

## Remove

Check the ownership manifest first as described under Update; stop if it is missing. Ask the human first. Then `.agents/bin/team bot remove <id>` for every bot (this deletes their storage and archives their files under `.agents/team/env/` in `_archive/`), move `_archive/` outside `.agents/` if the human wants to keep those backups, remove the team image or prefix (`container`: `<engine> rmi <team>-<folder>-image` with the configured `engine` and `docker_host`; `bwrap`: `rm -rf` of `_prefix` under `.agents/team/env/`), and remove the managed pack files recorded in `.agents/team/.pack-files`, `.agents/team/` and any directories in `.agents/` that are now empty (leaving pre-existing and unrelated top-level and nested files in `.agents/` intact):

```bash
(
  [ -f .agents/team/.pack-files ] || {
    echo "ownership manifest missing: stop; follow INSTALL.md Update ownership guidance" >&2
    exit 1
  }
if [ -f .agents/team/.pack-files ]; then
  while IFS= read -r f; do
      [ -n "$f" ] || continue
      rm -f "$f" || exit 1
      case "$f" in *.py)
        rm -f "${f%/*}/__pycache__/$(basename "${f%.py}")".*.pyc || exit 1 ;;
      esac
    done < .agents/team/.pack-files
fi &&
rm -rf .agents/team &&
{ find .agents -depth -type d -exec rmdir {} \; 2>/dev/null || true; }
)
```

Finally, remove the lines this install added to `AGENTS.md`, `CLAUDE.md`, `GEMINI.md` and `.claude/settings.json`.

## Existing agent setups

If the folder already has agents, rules or skills, do not rewrite them. The team only adds `.agents/START.md`, `rules.md`, `roles/`, `skills/`, `bin/`, `bots/`, `connectors/` and `team/`, plus one line in `AGENTS.md`. Existing rules keep working; where they contradict `.agents/rules.md`, ask the human which one wins and write the answer into `.agents/team/decisions.md`.

## Move to another computer or share

On the old computer: `.agents/bin/team export` (bots and settings, no data, no secrets; `--with-data` adds the readable projections `board.md`, `decisions.md`, `schedule.md`, `knowledge` and `logs`, not raw event logs, active flows, goals or practice files). On the new one: steps 1 to 3, then `.agents/bin/team import <file>` (it only lists what it would add) and `.agents/bin/team import <file> --apply` to write it, copy the needed settings from `team/import/<archive>/` by hand, and run step 4 (imported bots arrive `proposed`; activate and create them after the human's yes).
