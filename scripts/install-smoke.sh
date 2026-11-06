#!/usr/bin/env bash
# Install smoke test: follows INSTALL.md in empty folders and checks every step that needs no model.
#
#   scripts/install-smoke.sh SRC
#
# SRC is a checkout of the repository (it holds INSTALL.md and the pack to check). The pack is cloned as step 1
# of INSTALL.md says, from $WOUF_INSTALL_SRC (default https://github.com/vaiev/wouf.git; CI points it at a bare
# clone of the checked-out commit). Needs bash, python3 (3.9+) and git. Changes nothing outside a temp folder.
# Each step prints one "ok" line; a failure prints the step name and exits non-zero.
#
# THE RULE: every command runs through check (below), which judges the exit status first and the output second,
# so no step passes on output alone. Only shell builtins and file tests stay inline; a probe that is the condition
# of an if (grep) goes through probe, which tells a match (0) from no match (1) and from an error (>1).
# No step accepts "any" status: each states the one it expects (default 0).
set -uo pipefail

SRC=${1:-}
[ -n "$SRC" ] && [ -f "$SRC/INSTALL.md" ] || { echo "usage: install-smoke.sh SRC (a checkout with INSTALL.md)" >&2; exit 2; }
SRC=$(cd "$SRC" && pwd)
GUIDE=$SRC/INSTALL.md
PACK_URL=${WOUF_INSTALL_SRC:-https://github.com/vaiev/wouf.git}

# The helpers read these; a developer's own settings must not leak into the run.
unset WOUF_LOG_DIR
export WOUF_SECRET_BACKEND=file
export PYTHONDONTWRITEBYTECODE=1

WORK=$(mktemp -d "${TMPDIR:-/tmp}/wouf-install-smoke.XXXXXX") || exit 2
trap 'rm -rf "$WORK"' EXIT
export WOUF_SECRET_DIR=$WORK/secrets
STEP=start

fail() { echo "FAIL  $STEP: $*" >&2; exit 1; }
ok() { echo "ok    $RUN: $STEP"; }
step() { STEP=$1; }
OUT=$WORK/out.txt
OUTPUT=""
# probe NAME FILE GREP-OPTIONS...: grep in FILE; returns 0 on a match, 1 on none, and fails the run on any other status.
probe() {
  local name=$1 file=$2 status
  shift 2
  grep "$@" "$file" >/dev/null 2>"$OUT.err"; status=$?
  [ "$status" -le 1 ] && return "$status"
  {
    echo "FAIL  $STEP: $name"
    echo "      probe: grep $* $file"
    echo "      exit status: $status (grep failed: 0 is a match, 1 no match, anything else an error)"
    sed 's/^/        /' "$OUT.err"
  } >&2
  exit 1
}
# check NAME [-s STATUS] [-m REGEX]... [-n REGEX]... -- CMD...
# Runs CMD with stdout and stderr captured in $OUT (and $OUTPUT). Fails unless the exit status is the expected
# one (-s: a number or a comma list such as 0,1; default 0), then every -m REGEX (ERE) matches the output and no
# -n REGEX does (a grep that errors, status >1, is a failure of its own). A failure prints the step, NAME, the
# command, the status and the last output.
check() {
  local name=$1 want=0 status pat why="" shown
  local -a must=() mustnot=()
  shift
  while [ $# -gt 0 ] && [ "$1" != -- ]; do
    [ $# -ge 2 ] || fail "check '$name': option $1 needs a value"
    case $1 in
      -s) want=$2;;
      -m) must+=("$2");;
      -n) mustnot+=("$2");;
      *) fail "check '$name': unknown option $1";;
    esac
    shift 2
  done
  [ "${1:-}" = -- ] && [ $# -gt 1 ] || fail "check '$name': no command after --"
  shift
  "$@" >"$OUT" 2>&1; status=$?
  OUTPUT=$(<"$OUT")
  case $want in
    *[!0-9,]*|"") fail "check '$name': -s takes a number or a comma list, got '$want'";;
  esac
  case ",$want," in *",$status,"*) ;; *) why="exit status $status, expected $want";; esac
  if [ -z "$why" ]; then
    for pat in ${must[@]+"${must[@]}"}; do
      probe "$name" "$OUT" -Eq -e "$pat" || { why="output lacks: $pat"; break; }
    done
  fi
  if [ -z "$why" ]; then
    for pat in ${mustnot[@]+"${mustnot[@]}"}; do
      if probe "$name" "$OUT" -Eq -e "$pat"; then why="output has: $pat"; break; fi
    done
  fi
  [ -z "$why" ] && return 0
  shown=$(tail -n 15 "$OUT")
  {
    echo "FAIL  $STEP: $name"
    echo "      command: $*"
    echo "      exit status: $status ($why)"
    echo "      last lines of output:"
    printf '%s\n' "$shown" | sed 's/^/        /'
  } >&2
  exit 1
}
same() { check "$3" -- cmp -s "$1" "$2"; }

# ---- what INSTALL.md says, read from the file itself

# The nth fenced block of a language inside the section "## $1".
block() { # sets BLOCK
  check "read INSTALL.md section $1" -- awk -v head="## $1" -v lang="$2" -v want="${3:-1}" '
    /^## / { insec = (index($0, head) == 1) }
    insec && $0 ~ "^ *```" lang "$" { inblock = 1; seen++; next }
    insec && /^ *```$/ { inblock = 0 }
    insec && inblock && seen == want { sub(/^  /, ""); print }
  ' "$GUIDE"
  BLOCK=$OUTPUT
}
STEP="INSTALL.md"
check "read INSTALL.md" -s 0,1 -- grep -o '`This folder runs an agent team[^`]*`' "$GUIDE"
ADAPTER_LINE=${OUTPUT%%$'\n'*}
ADAPTER_LINE=${ADAPTER_LINE//\`/}
block "1." bash; CLONE_BLOCK=$BLOCK
block "2." json; SETTINGS_JSON=$BLOCK
block "2." json 2; HOOKS_JSON=$BLOCK
block "Update" bash; UPDATE_BLOCK=$BLOCK
block "Remove" bash; REMOVE_BLOCK=$BLOCK
[ -n "$ADAPTER_LINE" ] || { STEP="INSTALL.md: the AGENTS.md line"; fail "not found"; }
[ -n "$CLONE_BLOCK" ] || { STEP="INSTALL.md: step 1 commands"; fail "not found"; }
[ -n "$SETTINGS_JSON" ] || { STEP="INSTALL.md: step 2 settings"; fail "not found"; }
[ -n "$UPDATE_BLOCK" ] || { STEP="INSTALL.md: Update commands"; fail "not found"; }
[ -n "$REMOVE_BLOCK" ] || { STEP="INSTALL.md: Remove commands"; fail "not found"; }

# ---- steps

get_pack() { # get_pack DIR: step 1 of INSTALL.md in DIR; sets EXISTED to the pack files that were already there
  local dir=$1 tmp="$WORK/pack-$RUN" script
  cd "$dir" || fail "cannot enter $dir"
  [ -e .agents/START.md ] && fail "this folder already has the team"
  # The commands as written, with the source swapped in, running with TMPDIR=$tmp so mktemp creates inside it.
  script=${CLONE_BLOCK//https:\/\/github.com\/vaiev\/wouf.git/$PACK_URL}
  [ "$script" != "$CLONE_BLOCK" ] || fail "INSTALL.md step 1 lacks the clone URL"
  case $script in *mktemp*trap*rm\ -rf*"$PACK_URL"*cp\ -Rn*.agents/team/.pack-files*) ;; *) fail "INSTALL.md step 1 changed shape; update this script";; esac
  check "create step 1 tmp root" -- mkdir -p "$tmp"
  if [ "$RUN" = existing ]; then
    # A pre-existing /tmp/wouf-pack or a failing clone must not break or copy stale files.
    check "create a stale wouf-pack folder" -- mkdir -p "$tmp/wouf-pack/.agents"
    echo "stale" > "$tmp/wouf-pack/.agents/START.md"
    check "failing clone stops step 1 before cp" -s 128 -- env TMPDIR="$tmp" bash -eu -c "${CLONE_BLOCK//https:\/\/github.com\/vaiev\/wouf.git/file://$WORK/no-such-repo.git}"
    [ ! -e .agents/START.md ] || fail "failing clone copied stale files"
  fi
  # Which pack files exist here already (INSTALL.md: tell the human about each).
  EXISTED=""
  if [ -d .agents ]; then
    check "clone of $PACK_URL" -- git clone -q --depth 1 "$PACK_URL" "$tmp.peek"
    check "list the pack files" -- find "$tmp.peek/.agents" -type f
    check "sort the pack files" -- sort <<<"$OUTPUT"
    while IFS= read -r f; do
      f=${f#"$tmp.peek"/}
      if [ -e "$dir/$f" ]; then EXISTED=${EXISTED:+$EXISTED$'\n'}$f; fi
    done <<<"$OUTPUT"
    check "remove the peek clone" -- rm -rf "$tmp.peek"
  fi
  check "step 1 commands" -- env TMPDIR="$tmp" bash -eu -c "$script"
  [ -f .agents/START.md ] || fail ".agents/START.md missing after the copy"
  [ -f .agents/team/.pack-files ] || fail ".agents/team/.pack-files missing after step 1"
  check "stale wouf-pack was untouched and new mktemp clone was removed" -- find "$tmp" -mindepth 1 -maxdepth 1 ! -name wouf-pack
  [ -z "$OUTPUT" ] || fail "the temporary clone was not removed: $OUTPUT"
  check "clean step 1 tmp root" -- rm -rf "$tmp"
}

end_line() { # end_line FILE: FILE, if it has text, ends with a newline
  [ -s "$1" ] || return 0
  check "read the last byte of $1" -- tail -c1 "$1"
  [ -z "$OUTPUT" ] || echo >> "$1"
}

ADDED=""   # adapters this install added to, for Remove
adapters() { # step 2 in the current folder; records what was added
  ADDED=""
  if ! { [ -f AGENTS.md ] && probe "AGENTS.md team line" AGENTS.md -qxF "$ADAPTER_LINE"; }; then
    end_line AGENTS.md
    echo "$ADAPTER_LINE" >> AGENTS.md; ADDED="$ADDED AGENTS.md"
  fi
  for f in CLAUDE.md GEMINI.md; do
    if [ ! -e "$f" ]; then echo "@AGENTS.md" > "$f"; ADDED="$ADDED $f"
    elif ! probe "$f mentions AGENTS.md" "$f" -q 'AGENTS\.md'; then
      end_line "$f"
      echo "@AGENTS.md" >> "$f"; ADDED="$ADDED $f"
    fi
  done
  check "create .claude" -- mkdir -p .claude
  printf '%s\n' "$SETTINGS_JSON" > "$WORK/allow.json"
  printf '%s\n' "$HOOKS_JSON" > "$WORK/hooks.json"
  check "merging the allow list into .claude/settings.json" -- python3 - "$WORK/allow.json" .claude/settings.json "$WORK/hooks.json" <<'PY'
import json, os, sys
new = json.load(open(sys.argv[1]))["permissions"]["allow"]
path = sys.argv[2]
data = json.load(open(path)) if os.path.exists(path) else {}
allow = data.setdefault("permissions", {}).setdefault("allow", [])
allow.extend(e for e in new if e not in allow)
for event, entries in json.load(open(sys.argv[3]))["hooks"].items():
    have = data.setdefault("hooks", {}).setdefault(event, [])
    have.extend(entry for entry in entries if entry not in have)
with open(path, "w") as fh:
    json.dump(data, fh, indent=2)
    fh.write("\n")
PY
}

verify_adapters() {
  check "AGENTS.md lacks the team line" -- grep -qxF "$ADAPTER_LINE" AGENTS.md
  check "AGENTS.md has the team line twice" -m '^1$' -- grep -cxF "$ADAPTER_LINE" AGENTS.md
  check "CLAUDE.md lacks @AGENTS.md" -- grep -q '^@AGENTS\.md$' CLAUDE.md
  check "GEMINI.md lacks @AGENTS.md" -- grep -q '^@AGENTS\.md$' GEMINI.md
  check ".claude/settings.json lacks the allow list" -- python3 - "$WOUF_ALLOW" <<'PY'
import json, sys
have = json.load(open(".claude/settings.json"))["permissions"]["allow"]
assert all(e in have for e in json.loads(sys.argv[1])), have
assert len(have) == len(set(have)), have
PY
}

requirements() {
  check "python3 --version" -- python3 --version
  check "python3 is older than 3.9" -- python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))'
  check "git --version" -- git --version
}

# Step 4, the parts that need no real model: missing engine diagnosis, stand-in setup, bots, doctor and first signed run.
helpers() {
  # The setup would choose containers; doctor then has to name the engine when there is none.
  check "set the container engine in config.ini" -- sed -i.bak 's/^mode = undeclared/mode = container/; s/^engine = .*/engine = wouf-smoke-no-such-engine/' .agents/team/config.ini
  check "remove config.ini.bak" -- rm -f .agents/team/config.ini.bak
  check "team doctor names the missing engine and a fix" -s 1 \
    -m 'container engine: wouf-smoke-no-such-engine \(not installed\)' -m '→ .*start Docker or Podman' -- .agents/bin/team doctor
  ok

  step "4 team status"
  check "team status lists the bots" -m 'Worker' -- .agents/bin/team status
  ok

  step "4 log add, log status"
  check "log add" -- .agents/bin/log add lead T-1 started smoke
  check "log status shows the line" -m 'lead T-1 started \| smoke' -- .agents/bin/log status
  [ -f .agents/team/logs/lead.jsonl ] || fail "the log went outside .agents/team/logs"
  ok

  step "4 secret list (file backend)"
  check "secret list is empty in a new store" -n '[^[:space:]]' -- .agents/bin/secret list
  ok
}

standin_run() {
  step "4 setup, git init, bot create, doctor all good and first signed stand-in run"
  check "create stand-in CLI" -- mkdir -p "$WORK/standin-$RUN"
  cat > "$WORK/standin-$RUN/standin-cli" <<'EOF'
#!/usr/bin/env bash
echo "ready"
printf 'WOUF Worker · %s · ready-for-review\nEvidence: smoke -> ready\nArtifacts: none\nQuestions: none\nFlags: none\n' "${WOUF_TASK:-T-smoke}"
EOF
  check "chmod stand-in CLI" -- chmod 755 "$WORK/standin-$RUN/standin-cli"
  check "configure host stand-in runner in config.ini" -- python3 - "$WORK/standin-$RUN/standin-cli" <<'PY'
import sys
cli = sys.argv[1]
p = ".agents/team/config.ini"
text = open(p, encoding="utf-8").read()
text = text.replace("mode = container", "mode = host").replace("runner = auto", "runner = local")
old = "[runner.local]\n# Any agent CLI that talks to a local model server (Ollama, llama.cpp, LM Studio). From a container the\n# host is usually reachable as host.docker.internal. Fill in install, command and the model names.\ninstall =\ncommand =\nhost_command =\nmodel_flag =\nstrong =\nmid =\nlight =\n"
new = f"[runner.local]\ninstall =\ncommand = {cli} {{prompt}}\nhost_command = {cli} {{prompt}}\nmodel_flag =\nstrong = smoke-strong\nmid =\nlight = smoke-light\n"
assert old in text, "runner.local section changed"
open(p, "w", encoding="utf-8").write(text.replace(old, new, 1))
PY
  if [ ! -d .git ]; then
    check "git init" -- git init -q
    check "git config user.name" -- git config user.name "Smoke Human"
    check "git config user.email" -- git config user.email "smoke@example.com"
    check "git commit" -- git commit -q --allow-empty -m "initial commit"
  fi
  check "team bot create worker" -m 'is ready' -- .agents/bin/team bot create worker
  check "team doctor all good" -m '^all good$' -- .agents/bin/team doctor
  check "create lead brief folder" -- mkdir -p .agents/team/env/lead
  printf 'Objective: Reply with the word ready and your return block.\n' > .agents/team/env/lead/T-smoke.md
  check "team run worker T-smoke --detach" -m 'team result worker T-smoke' -- \
    env WOUF_TASK=T-smoke .agents/bin/team run worker T-smoke --brief .agents/team/env/lead/T-smoke.md --tier light --detach
  check "team result worker T-smoke --wait 100" -m 'WOUF Worker · T-smoke · ready-for-review' -- \
    .agents/bin/team result worker T-smoke --wait 100
  ok
}

export_import() {
  step "4 team export"
  check "team export" -- .agents/bin/team export -o "$WORK/team.tar.gz"
  check "the export has the bot declarations" -m '/bots/worker\.md$' -- tar tzf "$WORK/team.tar.gz"
  check "the export has no logs or secrets" -n 'logs|secret.*\.names' -- tar tzf "$WORK/team.tar.gz"
  ok
  step "4 team import (list) in a second fresh folder"
  local other="$WORK/second"
  check "create the second folder" -- mkdir "$other"
  RUN_SAVED=$RUN; RUN=second
  get_pack "$other"
  RUN=$RUN_SAVED
  check "team import lists the bots and writes nothing" -m 'bots/worker\.md' -m 'nothing written' -- .agents/bin/team import "$WORK/team.tar.gz"
  cd "$other" || fail "cannot enter $other"
  check "team import in the second folder" -- .agents/bin/team import "$WORK/team.tar.gz"
  ok
}

# Every command, file and path INSTALL.md names must exist in the pack.
names_exist() {
  local pack=$1 words span first rest path sub help cmd allow here=$PWD spans
  # Paths the "Existing agent setups" line lists relative to .agents/.
  for path in START.md rules.md roles skills bin bots connectors team; do
    [ -e "$pack/.agents/$path" ] || fail ".agents/$path (named in INSTALL.md) is not in the pack"
  done
  check "list the .agents/ paths INSTALL.md names" -s 0,1 -- grep -o '`\.agents/[^`]*`' "$GUIDE"
  check "sort the .agents/ paths INSTALL.md names" -- sort -u <<<"${OUTPUT//\`/}"
  spans=$OUTPUT
  while IFS= read -r -u 3 span; do
    [ -n "$span" ] || continue
    first=${span%% *}; rest=${span#"$first"}; rest=${rest# }
    path=${first%/}
    [ -e "$pack/$path" ] || fail "$first (named in INSTALL.md) is not in the pack"
    case $first in .agents/bin/?*)
      [ -x "$pack/$first" ] || fail "$first is not executable"
      cd "$pack" || fail "cannot enter $pack"
      check "usage of $first" -- "$first"
      cd "$here" || fail "cannot enter $here"
      help=$OUTPUT
      sub=""
      for w in $rest; do case $w in -*|\<*|*\<*|*\>*) break;; esac; sub="$sub $w"; done
      sub=${sub# }
      [ -z "$sub" ] || check "'${first##*/} $sub' (named in INSTALL.md) is not a command of $first" -- grep -Eq "^ +${first##*/} $sub( |\$)" <<<"$help";;
    esac
  done 3<<<"$spans"
  # Allow-list entries: Bash(<command>:*) or Bash(<command>). Parsed into a variable first, then looped over in
  # the main shell: a loop fed by a pipe runs in a subshell, and a fail inside it would only end that subshell.
  check "allow list: $WOUF_ALLOW does not parse" -- python3 - "$WOUF_ALLOW" <<'PY'
import json, sys
for entry in json.loads(sys.argv[1]):
    body = entry[5:-1]
    body = body[:-2] if body.endswith(":*") else body
    cmd, _, sub = body.partition(" ")
    print(cmd + "|" + sub)
PY
  allow=$OUTPUT
  while IFS='|' read -r -u 3 cmd sub; do
    [ -x "$pack/$cmd" ] || fail "allow list: $cmd is not an executable of the pack"
    cd "$pack" || fail "cannot enter $pack"
    check "usage of $cmd" -- "$cmd"
    cd "$here" || fail "cannot enter $here"
    help=$OUTPUT
    [ -z "$sub" ] || check "allow list: '${cmd##*/} $sub' is not a command" -- grep -Eq "^ +${cmd##*/} $sub( |\$)" <<<"$help"
  done 3<<<"$allow"
  # Example bots and connectors the hand-over names, and the skill the setup follows.
  for path in bots/research.md bots/writer.md bots/tester.md bots/ops.md connectors/github.md connectors/context7.md \
              connectors/playwright.md connectors/memory.md; do
    [ -f "$pack/.agents/$path" ] || fail ".agents/$path (example named in INSTALL.md) is not in the pack"
  done
  for w in research writer tester ops GitHub Context7 Playwright Memory; do
    check "INSTALL.md no longer names $w; update this script" -- grep -q "$w" "$GUIDE"
  done
  # The folder names Remove and the hand-over mention.
  check "INSTALL.md no longer names team bot remove" -- grep -q 'team bot remove' "$GUIDE"
  cd "$pack" || fail "cannot enter $pack"
  check "team bot remove is a command of team" -m '^  team bot remove ID' -- .agents/bin/team
  cd "$here" || fail "cannot enter $here"
}

update_pack() { # Update: run INSTALL.md's Update block against a newer pack repo, keeping team/ and pre-existing files
  local newer="$WORK/newer-$RUN" tmp="$WORK/update-$RUN" script
  script=${UPDATE_BLOCK//https:\/\/github.com\/vaiev\/wouf.git/file://$newer}
  [ "$script" != "$UPDATE_BLOCK" ] || fail "INSTALL.md Update lacks the clone URL"
  case $script in *mktemp*trap*rm\ -rf*"file://$newer"*.agents/team/.pack-files*cp\ -Rn*) ;; *) fail "INSTALL.md Update changed shape; update this script";; esac
  check "clone the pack for the newer version" -- git clone -q "$PACK_URL" "$newer"
  echo "# newer start" >> "$newer/.agents/START.md"
  echo "# newer rules" >> "$newer/.agents/rules.md"
  echo "new skill" > "$newer/.agents/skills/smoke-new.md"
  echo "changed by the update" >> "$newer/.agents/team/config.ini"
  check "stage the newer pack version" -- git -C "$newer" add -A
  check "commit the newer pack version" -- env GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.com \
    GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.com \
    git -C "$newer" commit -q -m "newer"
  # Simulate retired files in the old pack, including an obsolete teamlib command module.
  echo "retired skill" > .agents/skills/retired-smoke.md
  printf '.agents/skills/retired-smoke.md\n' >> .agents/team/.pack-files
  if [ "$RUN" = existing ]; then
    cat > .agents/bin/teamlib/retired_smoke.py <<'PY'
COMMANDS = {"retired-smoke": lambda rest: 0}
USAGE = "  team retired-smoke"
PY
    printf '.agents/bin/teamlib/retired_smoke.py\n' >> .agents/team/.pack-files
    check "retired command is discoverable before update" -m 'team retired-smoke' -- .agents/bin/team help
  fi
  check "keep config.ini for comparison" -- cp .agents/team/config.ini "$WORK/config.before"
  check "keep the lead log for comparison" -- cp .agents/team/logs/lead.jsonl "$WORK/lead.before"
  check "create update tmp root" -- mkdir -p "$tmp"
  check "Update commands" -- env TMPDIR="$tmp" bash -eu -c "$script"
  check "update temporary clone was removed" -- find "$tmp" -mindepth 1 -maxdepth 1
  [ -z "$OUTPUT" ] || fail "the update temporary clone was not removed: $OUTPUT"
  check "clean update tmp root" -- rm -rf "$tmp"
  check "START.md was not updated" -m '^# newer start$' -- tail -n 1 .agents/START.md
  if [ "$RUN" = fresh ]; then
    check "rules.md was not updated" -m '^# newer rules$' -- tail -n 1 .agents/rules.md
  else
    same .agents/rules.md "$WORK/orig-.agents_rules.md" "the update overwrote pre-existing .agents/rules.md"
    same .agents/skills/custom.md "$WORK/orig-.agents_skills_custom.md" "the update removed or changed pre-existing .agents/skills/custom.md"
  fi
  [ -f .agents/skills/smoke-new.md ] || fail "a new file did not arrive"
  [ ! -e .agents/skills/retired-smoke.md ] || fail "a retired skill remained after the update"
  if [ "$RUN" = existing ]; then
    [ ! -e .agents/bin/teamlib/retired_smoke.py ] || fail "a retired teamlib module remained after the update"
    check "retired command is gone after update" -n 'retired-smoke' -- .agents/bin/team help
  fi
  same .agents/team/config.ini "$WORK/config.before" "team/config.ini was replaced by the update"
  same .agents/team/logs/lead.jsonl "$WORK/lead.before" "the team's logs were touched by the update"
  [ -f .agents/team/bots/worker.md ] || fail "the bots are gone after the update"
}

remove_install() { # Remove: archive bots, preserve requested archives outside .agents, run INSTALL.md's Remove block and clean adapter lines
  local f line l
  case $REMOVE_BLOCK in *.agents/team/.pack-files*rm\ -rf\ .agents/team*rmdir*) ;; *) fail "INSTALL.md Remove changed shape; update this script";; esac
  if [ -d .agents/team/env/worker ]; then
    check "team bot remove worker" -m 'remove done; its files are in' -- .agents/bin/team bot remove worker
    [ -d .agents/team/env/_archive ] || fail "bot archives were not created in .agents/team/env/_archive"
    check "preserve bot archives outside .agents" -- mv .agents/team/env/_archive "$WORK/archive-$RUN"
    [ -d "$WORK/archive-$RUN" ] || fail "bot archives were not moved outside .agents"
  fi
  check "Remove commands" -- bash -eu -c "$REMOVE_BLOCK"
  for f in $ADDED; do
    case $f in AGENTS.md) line=$ADAPTER_LINE;; *) line=@AGENTS.md;; esac
    # The file without the lines this install added (builtins only: grep -v exits 1 when nothing is left).
    while IFS= read -r l || [ -n "$l" ]; do
      [ "$l" = "$line" ] || printf '%s\n' "$l"
    done < "$f" > "$f.new"
    check "restore $f" -- cp "$f.new" "$f"
    check "remove $f.new" -- rm -f "$f.new"
    [ -s "$f" ] || check "remove the empty $f" -- rm -f "$f"
  done
  check "removing the allow list entries" -- python3 - "$WOUF_ALLOW" "$PRE" "$WORK/hooks.json" <<'PY'
import json, os, sys
mine, PRE = json.loads(sys.argv[1]), json.loads(sys.argv[2])
path = ".claude/settings.json"
data = json.load(open(path))
data["permissions"]["allow"] = [e for e in data["permissions"]["allow"] if e not in mine or e in PRE]
if not data["permissions"]["allow"]:
    del data["permissions"]["allow"]
if not data["permissions"]:
    del data["permissions"]
for event, entries in json.load(open(sys.argv[3]))["hooks"].items():
    data["hooks"][event] = [e for e in data["hooks"].get(event, []) if e not in entries]
    if not data["hooks"][event]:
        del data["hooks"][event]
if not data["hooks"]:
    del data["hooks"]
with open(path, "w") as fh:
    json.dump(data, fh, indent=2)
    fh.write("\n")
PY
}

# One whole install in DIR. RUN names it in the output.
run_install() {
  local dir=$1
  step "1 get the pack"
  get_pack "$dir"
  PACK=$dir
  ok
  step "2 adapters"
  adapters
  verify_adapters
  ok
  step "3 requirements"
  requirements
  ok
  step "4 team doctor (no container engine)"
  helpers
}

STEP="INSTALL.md: step 2 settings"
check "the settings block is not valid JSON" -- python3 -c 'import json, sys; print(json.dumps(json.loads(sys.stdin.read())["permissions"]["allow"]))' <<<"$SETTINGS_JSON"
WOUF_ALLOW=$OUTPUT
export WOUF_ALLOW

# ---- run 1: an empty folder
RUN=fresh
FRESH=$WORK/fresh
check "create the fresh folder" -- mkdir "$FRESH"
run_install "$FRESH"
export_import
cd "$FRESH" || exit 2

step "names in INSTALL.md exist in the pack"
names_exist "$FRESH"
ok

step "Update"
update_pack
ok

step "Remove"
PRE='[]'
remove_install
[ ! -e .agents ] || fail ".agents still exists"
for f in AGENTS.md CLAUDE.md GEMINI.md; do
  [ ! -e "$f" ] || fail "$f was created by the install and is still there"
done
check ".claude/settings.json still has install entries" -n 'wouf|AGENTS|agents/bin' -- cat .claude/settings.json
ok

# ---- run 2: a folder that already has a setup
RUN=existing
EXIST=$WORK/existing
check "create the existing setup" -- mkdir -p "$EXIST/.claude" "$EXIST/.agents/skills"
cd "$EXIST" || exit 2
printf '# Our rules\nBe kind.\n' > AGENTS.md
printf '# Claude notes\nUse tabs.\n' > CLAUDE.md
echo 'my own tool' > .agents/mytool.sh
echo 'our own skill' > .agents/skills/custom.md
echo 'our own rules, not the pack'"'"'s' > .agents/rules.md
printf '%s\n' '{' '  "model": "x",' \
  '  "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": ".agents/bin/guard"}]}], "SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "echo existing"}]}]},' \
  '  "permissions": { "allow": ["Bash(ls:*)", "Bash(.agents/bin/log:*)"], "deny": ["Bash(rm:*)"] }' '}' > .claude/settings.json
for f in AGENTS.md CLAUDE.md .agents/mytool.sh .agents/skills/custom.md .agents/rules.md .claude/settings.json; do
  check "keep $f for comparison" -- cp "$f" "$WORK/orig-${f//\//_}"
done

step "1 get the pack (existing .agents/)"
get_pack "$EXIST"
PACK=$EXIST
same .agents/rules.md "$WORK/orig-.agents_rules.md" "an existing .agents/rules.md was overwritten"
same .agents/mytool.sh "$WORK/orig-.agents_mytool.sh" "an existing .agents file was changed"
same .agents/skills/custom.md "$WORK/orig-.agents_skills_custom.md" "an existing .agents/skills/custom.md was changed"
[ "$EXISTED" = ".agents/rules.md" ] || fail "pre-existing pack files should be exactly .agents/rules.md, got: $EXISTED"
echo "      already existed (tell the human): $EXISTED; also kept, not part of the pack: .agents/mytool.sh, .agents/skills/custom.md"
ok

step "2 adapters (existing files)"
adapters
verify_adapters
check "AGENTS.md lost its own content" -m '^# Our rules$' -m '^Be kind\.$' -- head -n 2 AGENTS.md
check "CLAUDE.md lost its own content" -m '^# Claude notes$' -m '^Use tabs\.$' -- head -n 2 CLAUDE.md
check ".claude/settings.json: existing entries lost or not merged" -- python3 - <<'PY'
import json
d = json.load(open(".claude/settings.json"))
assert d["model"] == "x" and d["permissions"]["deny"] == ["Bash(rm:*)"], d
allow = d["permissions"]["allow"]
assert allow[:2] == ["Bash(ls:*)", "Bash(.agents/bin/log:*)"], allow
assert allow.count("Bash(.agents/bin/log:*)") == 1 and len(allow) == 5, allow
PY
[ "$ADDED" = " AGENTS.md CLAUDE.md GEMINI.md" ] || fail "unexpected set of changed adapters:$ADDED"
check "keep adapters after first install" -- cp .claude/settings.json "$WORK/first-settings.json"
# Repeating the merge is free; preserve ADDED for the later removal check.
FIRST_ADDED=$ADDED
adapters
ADDED=$FIRST_ADDED
same .claude/settings.json "$WORK/first-settings.json" "a second install added settings"
check "session hook merge and guard preservation" -- python3 - "$WORK/hooks.json" "$WORK/orig-.claude_settings.json" <<'PYHOOK'
import json, sys
wanted = json.load(open(sys.argv[1]))["hooks"]
old = json.load(open(sys.argv[2]))["hooks"]
have = json.load(open(".claude/settings.json"))["hooks"]
assert list(have)[0] == "PreToolUse", have
assert have["PreToolUse"] == old["PreToolUse"], have
for event, entries in wanted.items():
    assert have[event] == old.get(event, []) + entries, have
    assert all(have[event].count(e) == 1 for e in entries), have
assert wanted["SessionStart"][0]["matcher"] == "startup|resume|compact"
assert wanted["SessionStart"][0]["hooks"][0]["command"] == ".agents/bin/team context --claude"
assert wanted["SessionEnd"][0]["hooks"][0]["command"] == ".agents/bin/team session end --claude"
PYHOOK
echo "session hooks merged once; existing hooks and guard preserved"
ok

step "3 requirements"
requirements
ok
step "4 team doctor (no container engine)"
helpers
standin_run

step "Update (existing setup)"
update_pack
same .agents/mytool.sh "$WORK/orig-.agents_mytool.sh" "the update changed an unrelated .agents file"
ok

step "Remove (existing setup)"
PRE='["Bash(ls:*)", "Bash(.agents/bin/log:*)"]'
remove_install
[ -f .agents/mytool.sh ] || fail "unrelated .agents/mytool.sh was removed"
same .agents/mytool.sh "$WORK/orig-.agents_mytool.sh" "unrelated .agents/mytool.sh was changed by Remove"
[ -f .agents/skills/custom.md ] || fail "pre-existing .agents/skills/custom.md was removed"
same .agents/skills/custom.md "$WORK/orig-.agents_skills_custom.md" "pre-existing .agents/skills/custom.md was changed by Remove"
[ -f .agents/rules.md ] || fail "pre-existing .agents/rules.md was removed"
same .agents/rules.md "$WORK/orig-.agents_rules.md" "pre-existing .agents/rules.md was changed by Remove"
[ ! -e .agents/team ] && [ ! -e .agents/START.md ] && [ ! -e .agents/bin ] && [ ! -e .agents/roles ] && [ ! -e .agents/skills/setup.md ] || fail "installed pack files still exist after Remove"
same AGENTS.md "$WORK/orig-AGENTS.md" "AGENTS.md is not back to its original"
same CLAUDE.md "$WORK/orig-CLAUDE.md" "CLAUDE.md is not back to its original"
[ ! -e GEMINI.md ] || fail "GEMINI.md should be gone"
check ".claude/settings.json is not back to its original entries" -- python3 - <<'PY'
import json
d = json.load(open(".claude/settings.json"))
assert d["permissions"]["allow"] == ["Bash(ls:*)", "Bash(.agents/bin/log:*)"] and d["permissions"]["deny"] == ["Bash(rm:*)"], d
PY
ok

echo "all install steps ok"
