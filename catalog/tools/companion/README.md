# Companion

A read-only cockpit for a WOUF team folder, and a single-file showcase export. Python 3.9+, standard library
only. It reads the secret patterns from the WOUF pack (`bin/log`), so it needs the pack next to the team folder,
at `--pack DIR` or `WOUF_PACK`.

## Run

```sh
cp -r catalog/tools/companion ~/tools/companion
python3 ~/tools/companion/companion.py --team .agents/team           # http://127.0.0.1:8765/
python3 ~/tools/companion/companion.py --team .agents/team --hub ~/.local/share/hub/hub.db --port 8800
python3 ~/tools/companion/companion.py --team .agents/team --json    # the state once, for scripts
python3 ~/tools/companion/companion.py --team .agents/team --actions # owner actions: open the #token=… link it prints
```

`--team` defaults to `WOUF_TEAM_DIR`, then `.agents/team`. The pack is found beside the team folder (`.agents/team`
in `.agents`, or a team folder next to `.agents`), else at `--pack` or `WOUF_PACK`. `--hub` (or `HUB_DB`) adds read-only counts from a
[hub](../hub/README.md) database.

The page wears [VAIEV UI v8](static/v8/) in the wouf brand: the light field, planes and plates for content, a lens
Morph Bar for the views (at the bottom on a phone, a rail on the left from 1024 px). The system setting chooses light
or dark. Its four views are the ones of the wouf app mock that have data here; chat, map and goal and plan have none
and are left out:

- **Today**: what needs you (a count), the running flows with their steps, the **phase gates** (each flow as INIT,
  CONCEPT (waits for a dependency), REALIZE (worker or gate ran), VERIFY (review ran) or done (flow ready)) and the
  **timeline** of the team's log
- **Approvals**: open questions, pending decisions and merge proposals (read from `team/events`), and the board's
  questions for the human; read-only unless companion runs with `--actions`
- **Board**: the board's rows as columns by status, and the **task DAG**: flows in waves from the `Depends:` line of
  their briefs
- **Team**: each declared bot with its status, its last log outcome and, when limited, a countdown to the time the
  flow recorded; **federation** per peer (envelopes sent, held in quarantine, the last one received, type and time
  only); hub counts with `--hub`

It updates live: the server pushes each new state on `/events` (server-sent events, checked once a second, a
keep-alive line every 15 seconds, at most 8 open streams). While the stream is down, the page asks
`/api/state` every three seconds until it is back.

With `--actions`, the Approvals view lets the owner answer several questions at once, approve or reject a decision,
or mark a proposed head for merging. Each button first shows the exact `team` commands, as the server built them, in
the Trusted Bar and runs them only on "Run" (Cancel or Escape runs nothing). [INTERFACE.md](INTERFACE.md) is the binding: paths, bodies, commands and the checks every POST passes.

## Showcase

```sh
python3 companion.py --team .agents/team --export-showcase showcase.html --title "Building our API with WOUF"
```

Writes one HTML file with the data inside: no network requests, no external files, and a
`Content-Security-Policy` of `connect-src 'none'` that runs only the page's own script and style (by hash). The
style is the vendored v8 CSS inlined, without fonts or any `url()`, and the bar shows words instead of icons, so the
file loads nothing. Before
it writes, a last check scans every key and value that came from the team folder for a private path, an e-mail
address, a secret shape or a token-like word; if it finds one, nothing is written and the message names only the
kinds. Read the page before you
share it.

## Showcase of one task or epic

```sh
python3 companion.py showcase T-0019 -o out/ --brief briefs/T-0019.md --title "A parser in an hour"
```

Exports one finished task, or an epic, as an offline replay the owner may publish: `out/showcase.json` and
`out/showcase.html`, and nothing else. `showcase.py` registers the subcommand.

- **Sources:** it reads only what `team snapshot --json` and `team events --since SEQ --json` print, run as
  subprocesses of the pack's `bin/team`, and no file of the team folder.
- **Epic:** the id `T-0019` takes every package named `T-0019-<n>`.
- **Runs:** each run belongs to its package, with its steps, gates, reviews and times. A run may carry its own id:
  a review round `T.r<n>` (whose `n` is the review's round), a scout or research run `T.scout`, a fix round
  `T-fix<n>`, or these combined. Such an id is never a package of its own, even when the store lists it as a task.
- **Refusals:** a package that is not `done` yet, an unknown id or a bad one writes nothing and exits 1.
- **Notes:** the output names what the store does not hold (a package without a review verdict). It also says when
  the title had to be masked.

**What a caller passes.** For an epic, which has no task of its own, pass `--brief` with `Objective:`, an
`Acceptance criteria:` list and `Class:`, and `--title`. A file outside the team folder is fine, and the exporter
reads only that file. Without `--brief` these fields are `null` and `[]`. For a single task, `class` and the title
come from the task. `final_verdict` is never passed: it needs a review verdict for every package in the store.

`showcase.json`, schema `wouf.showcase.v1` (a client ignores fields it does not know; a change of meaning is a new
schema id):

| Field | Content |
|---|---|
| `manifest` | ShowcaseManifest: `v` (always `wouf.showcase.v1`: a constant, never masked), `task_id` (the task or epic), `title` (`--title`, else the task's title, else the epic's id; masked like any value, and the output says so), `class` (the task's, else the brief's `Class:`), `objective`, `acceptance_criteria[]`, `started_at` and `completed_at` (UTC, the first event and the last `done`), `wall_duration_s`, `total_cost` (the snapshot's `cost_total` summed, two decimals, or `null`), `final_verdict` (`approve` when every package's last review approved, else the first other verdict, `null` when a package has no review, which the output names) |
| `tasks[]` | `id`, `title`, `class`, `status` of each task in the selection |
| `steps[]` | ReplayStep, one per event of the selection, by `seq`: `seq`, `at`, `task` (the run's own id, such as `T-0019-13.r1`), `kind` (`task.*`, `run.*`, `flow.step`, `phase.state`, `question.*`, `decision.recorded`, `merge.*`), `bot`, `outcome`, `what` (fixed words and short fields, a decision's title and reason; never a bot's own text or a question's text), `duration_s` (a finished run: the time since its bot's start on the task) |
| `gates[]` | GateResult: `seq`, `at`, `task` (the package), `status` (`green`, `red`, …) of each `gate` step, a fix round's included |
| `reviews[]` | ReviewVerdict: `seq`, `at`, `task` (the package), `bot`, `runner`, `verdict`, `round` (the `n` of `T.r<n>`, else counted from 1). A review is a finished run with a verdict, by a reviewer bot or in a review round |
| `artifacts` | ArtifactSummary: `tasks` (count), `paths` (claimed, relative), `branches` (the runs' refs that are no sha), `tokens` (input/cache read/cache write/output summed, or `null`) |
| `time_saved_s` | the sum of the steps' `duration_s` minus `wall_duration_s`: what side-by-side runs saved; never below 0 |

Every string value is cleaned again, and the text from `--brief` gets the same rules as the snapshot's own
data:

- **Registered values:** bin/log's `REGISTERED` (the secret store, `WOUF_MASK`) and the caller's `WOUF_MASK`
  become `***`.
- **Home paths:** a path with a `home` or `users` segment, in any case, runs to its line end. This holds for
  `/tmp/Users/…`, `/bot/tasks/<x>/home/…` and a path behind `<repo>/`, `<team>/` or `<workspace>/` too. The check
  comes before the workspace roots, which would otherwise keep the segment.
- **Workspace roots:** `/bot/tasks/<x>/` and `/tmp/<x>/` become `<workspace>/`, and the path inside stays.
- **The companion's `redact()`:** key shapes and the token rule, e-mail addresses (`user@<IPv4>` included), IPv4,
  `token=` values, URLs with credentials, and every other absolute path.
- **Ids:** `uid=`, `uid:`, `gid=`, `uid 1000` and `--user 1000:1000`.
- **Sign-offs:** a `Signed-off-by:` or `Co-authored-by:` to its line end.
- **Issue refs:** any token that ends in `#<digits>`, whatever comes before it (`#123`, `widget#45`,
  `github.com/acme/widget#12`, `gitlab.example.com/grp/sub/proj#7`). It never matches an HTML entity (`&#38;`).
  This rule runs before the path rule too.
- **Hosts:** `ssh://` targets, scp-style targets (`git@alias:repo`), an account at a host without a domain
  (`jane@localhost`) and private IPv6 (`fc00::/7`, `fe80::/10`, with a zone such as `%eth0`).
- **Names:** the names `team snapshot` hides, from the pack's own `teamlib.snapshot.names()`. It runs as a
  `python3 -I` subprocess, so the current folder is not on its path. These are user and host names, getpass's user,
  the `docker_host` alias and its user.
  - **Configured names** (the alias and its user) are hidden whatever their length, generic words included.
  - **Names guessed from the environment** are hidden from 3 characters on, and only when they are not a generic
    word (`root`, `lead`, …).
  - A name is a word when no letter or digit touches it, so `db_backup` hides `db` too.

`<repo>/`, `<team>/` and `<workspace>/` keep the relative path behind them. Then a residual check runs on the
final bytes of both files, before either is written:

- **`showcase.json`:** every value. Keys are the schema's own words and are not cleaned; a key outside the
  schema is refused and named by where it sits, never by itself.
- **`showcase.html`:** the embedded data, field by field, and the `<title>`. The page with its data and title cut
  out must be `static/showcase.html` itself.

If any rule still matches, nothing is written, the exit code is 1 and the message names the field
(`showcase.json steps[11].what (an issue ref)`, `showcase.html <title> (an e-mail address)`), never the value.
If the second file cannot be written, the first is removed again.

`showcase.html` is `static/showcase.html` with the JSON embedded (`<`, `>` and `&` escaped) and a
`Content-Security-Policy` meta tag: `default-src 'none'`, the page's own style and script by hash, `connect-src
'none'`, `img-src 'none'`, `base-uri 'none'`, `form-action 'none'`. Its style inlines the v8 wouf tokens (the same
values as `static/v8/css/tokens.css`), the light field, plane and plate, with the system's fonts. It loads nothing,
builds its timeline with `textContent` only and follows the reader's light or dark setting. Neither file is written through a link.

## Safety

- The server listens on 127.0.0.1 only, answers only to `127.0.0.1:<port>` and `localhost:<port>` as `Host` (its own port), and serves
  `/`, `/api/state`, `/events` and the files under `static/` with a known type (`.css`, `.woff2`, `.svg`, `.txt`):
  the list is made once at the start without following a link, a request must name one of them exactly, and each is
  read without following a link. Anything else, `..`, `%2e`, a query or `static/showcase.html` included, gets 404.
  Every response, error pages included, carries the same `Content-Security-Policy` and
  `X-Content-Type-Options: nosniff`: `default-src 'none'`, the page's own script by hash, `'self'` only for the
  vendored styles, fonts and icons (`style-src`, `font-src`, `img-src`) and for `connect-src`, `base-uri 'none'`,
  `form-action 'none'`, `frame-ancestors 'none'`. It never writes to the team folder. The hub database is read from a copy
  (with its `-wal`) in a temporary folder, because SQLite may create a `-shm` file even read-only; the copy is
  made again only when the database changes. Without `--actions` nothing changes anything; with it, only the
  three `team` commands of INTERFACE.md run, behind the launch token and the same-origin checks.
- It reads the team folder without following a link: a file or folder that is a link (or lies in a linked
  folder) is skipped, each part of a path is opened relative to the one before, only regular files are read, and
  at most 4 MiB of each (of a log, its newest lines). A link, a FIFO or a device can neither pull in outside text
  nor hold a request. A system without `O_NOFOLLOW`, `O_DIRECTORY` and `dir_fd` (Windows) gets no fallback by
  path: companion says so and reads nothing from the team folder there (run it in WSL).
- Every string passes through `redact()`, keys included: the pack's `SECRET_PATTERNS` and token rule, then e-mail
  addresses (a quoted local part such as `"jane.doe"@…` and an address literal `user@[…]`, IPv6 included), IPv4
  addresses, `token=` style values, URLs with credentials and every absolute path, by the lead's rule (the same as
  `team snapshot`'s): a root is `/` at a word start or after `:` or `=`, `~`, `X:` with `/` or `\`,
  or `\\` at a word start, and the path runs to the next whitespace, quote, `|`, `<`, `>`, `)`, `]` or the line end.
  A `home` or `users` segment in any case and anywhere in it (`/mnt/c/Users/`, `/var/home/`, `c:\users\`), `~/`,
  `~user`, `D:Users\`, `D:\Documents and Settings\` and a home segment glued to a word run it to the line end, since a
  home folder's name may hold spaces. This also redacts a URL's path and a `/` standing alone in prose, which is
  accepted. Board rows use the pack's column names, never the file's header text. The flow's repository path
  and envelope bodies are never read into the state.
- The page builds its DOM with `textContent` only, so board or log text cannot inject markup, and the embedded
  data escapes `<`.

## The v8 files

`static/v8/` is VAIEV UI v8.0.0 (tag on main, same tree as b9b459f), vendored in the wouf brand only: `css/` (tokens, fonts, base, materials,
components, patterns), the IBM Plex fonts with their licence (`fonts/OFL.txt`), `icons.svg` and
`brand/wouf-icon.svg`. The rules, sections, comments and symbols of other brands are left out, so
`grep -rilE 'r[o]enu|w[u]lew|f[o]ug|v[a]ikor' static` finds nothing. `static/cockpit.css` is the companion's own: phase
gates, waves, the log and the Trusted Bar's command list, on the v8 tokens. There is no build step.

## Tests

```sh
python3 -m unittest discover -s catalog/tools/companion
```
