# Change receipts (wouf.receipt.v1)

Status: on main. R1: the format and signature, in `.agents/bin/teamlib/receipt.py` (`tests/test_receipt.py`). R2: the evidence each flow step and `gate.json` keep, in `.agents/bin/teamlib/evidence.py` (`tests/test_flow_evidence.py`, `tests/test_receipt_contract.py`).
R3: `team receipt make` builds, signs and stores a receipt with its git note, and `team receipt resign` signs a stored one again, in `.agents/bin/teamlib/attest.py` (`tests/test_receipt_build.py`, `tests/test_receipt_store.py`). R4: `team verify` checks a receipt offline against its local Git objects, in `.agents/bin/teamlib/verify.py` (`tests/test_verify.py`). R5: `team receipt export --in-toto`, in `.agents/bin/teamlib/intoto.py` (`tests/test_receipt_export.py`).
Nothing makes a receipt by itself: the lead runs `team receipt make` after a landing (`.agents/skills/review-merge.md`).

`team demo` shows a receipt end to end in a disposable repository: made with `team receipt make`, signed with a throwaway key, verified offline with `team verify`, and a tampered copy refused.

A change receipt is a signed record of one landed change. It says which commit was reviewed (H) and which one landed
(L), with the green gate, the review rounds that count, every run of the task and its egress, all
signed with the team's federation key. Anyone with the team's public key and its published fingerprint can check it
offline. The trust root of WOUF's own receipts is the ED25519 key with fingerprint `SHA256:kQfxHJ5w0JRzMT4JIburS5E0zGHlwk47+Zvauk5Ac1w`. A receipt holds only ids, names from the config, hex, counts and hosts that passed the allowlist. No bot
text and no secret value goes into it.

## Canonical form: wouf-cj1

The signed bytes are `canonical(receipt)`:

```python
json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")
```

This is federation's canonical form (`docs/FEDERATION.md`) with two tightenings: ASCII only, and no floats. For this
value domain the bytes equal RFC 8785 (JCS), so any JCS library can check them.

Value domain of `receipt`: objects with text keys, arrays, text of printable ASCII (0x20 to 0x7e, at most 500
characters), integers 0 to 2^53-1 (a bool never counts as one), `true`, `false` and `null`. The receipt object is level 1,
and nesting goes to 8 levels at most.

The stored file is `canonical({"receipt": ..., "sig": ...})` followed by one `\n`, at most 64 KiB. `sig` is the
armored block of `ssh-keygen -Y sign`. It must match federation's `SIG` regex, newlines included. The value domain,
the field checks and the secret scan apply to `receipt` only.

`parse(data)` refuses, each with a message that names a field and a rule but never a value:

- a file over 64 KiB, or a byte that is not ASCII;
- text that is not JSON, a repeated key in any object, a float (`5.0`, `5e0`), `NaN`, `Infinity`;
- a file that does not hold exactly `receipt` and `sig`, or a `sig` that is not one armored signature;
- a key shape or a token anywhere in `receipt` (below), a value outside the domain, nesting over 8;
- bytes that are not byte-equal to the canonical form of what they parse to, such as a space, other key order,
  an escape like `\u0077` for `w`, `-0`, or a missing or second final newline. A receipt has exactly one byte form;
- a receipt that fails a field check.

`parse` does not check the signature. That is `signature_ok`.

## Fields

All keys are required at every level. A key that is unknown or missing at any level is refused. A value that is
not known is the string `"unknown"`, never a guess. The table lists what `check_receipt` checks. For where each
value comes from, see `teamlib/attest.py::build`. A closed flow (its `state.json` holds `closed`) is history, not evidence: the gate and
`change.base` then come from `gate.json`, and the runs and rounds from the run events, as without a flow.

| Field | Rule |
|---|---|
| `schema` | `"wouf.receipt.v1"` |
| `team` | The signer principal, `[federation] name`: `[a-z][a-z0-9-]{1,30}` |
| `signed_at`, `resigned_at` | A real UTC time `YYYY-MM-DDTHH:MM:SSZ`. `resigned_at` is `null`, or no earlier than `signed_at` |
| `repo` | A non-empty label |
| `task`, `class` | A task id (the shared task-id rules) and a class name |
| `brief` | `{sha256: 64 hex, source: "flow" or "file"}` |
| `change` | `base`, `reviewed`, `landed`, `landed_parent`, `tree`: 40 lower-case hex. `files`: from 1 (0 is refused: no change to vouch for). `merge`: `squash` or `fast-forward`. `pr`: from 1, or `null`. `landed_parent` equals `base` unless the merge is fast-forward |
| `gate` | `state` `green`. `head` equals `change.reviewed`, `base` equals `change.base`. `code` is `0`, or `null` (no gate command). `network`: `none`, `bridge`, `host`, `unknown`. `command_sha256`: 64 hex or `unknown`. `complexity`: `unknown` or `{files, functions, failures, accepted}` counts |
| `reviews` | `required` and `approved` are counts. Each round has `run`, `index` (from 1), `bot`, `runner`, `cli`, `vendor`, `model`, `verdict` (`approve`, `changes`, `stale`, `blocked`, `unknown`), `head` (40 hex or `unknown`), `approve_sha` (7 to 40 hex, or `null`), `counts` (a bool), `source` (`flow` or `run`) |
| `runs` | Each run has `run`, `role`, `bot`, `runner`, `cli`, `vendor`, `model`, `tier`, `commit` (40 hex or `unknown`), `outcome`, `tokens` (`a/b/c/d` or `unknown`), `egress` |
| `runs[].egress` | `mode`: `proxy`, `bridge`, `none`, `host`, `unknown`. `log`: the sha256 of the run's egress log (64 hex), `null` or `unknown`. `hosts`: sorted, unique `host:port`. `private`, `refused`: counts |
| `usage` | `known_runs` and `unknown_runs` count the runs with and without token numbers. `tokens` is the sum of their `a/b/c/d`. `cost` is `"unknown"` |
| `flags` | `{}` or `{"sovereign": true}` |

Rules across fields:

- A round with `counts: true` must be an `approve` whose `head` equals `change.reviewed` and whose `approve_sha` is a
  prefix of it, 7 to 40 hex. This is the rule of `flow.py` and of `team merge propose`. A round that meets the rule
  may still have `counts: false`, for example when a later gate or round replaced it.
- `approved` equals the number of rounds with `counts: true`.
- `cli` is a basename, never a path. `vendor` is the fixed map of it: `claude` is `anthropic`, `codex` is `openai`,
  `agy` and `gemini` are `google`, and anything else is `unknown`.
- `tier` is `light`, `mid`, `strong` or `unknown`. A fallback is written in ASCII as `mid->strong`: the run's own
  label `mid→strong` is not ASCII.

R1 fixed two values: the form of `runs[].egress.log` and the words of `verdict`. None of
the accepted forms can carry bot text.

## Secret rule

Before any other check, every text of `receipt` (keys included), and every integer as its digits, passes the
rule of `log add` and `federate`. It must hold no key shape that `secret_in` knows, no token-like run (receipt.py
writes `log`'s `TOKEN_LIKE` again, and a test compares the two), and no value registered for the process. A refusal
names the field path and the kind, such as `receipt.runs[0].model looks like it contains a secret (AWS access key)`,
and never the value. `sign` refuses before `ssh-keygen` runs, so no key shape reaches the signed bytes. `sig` is not
scanned: its base64 looks random by design.

## Signature and trust

- **Sign:** `ssh-keygen -Y sign -q -f KEY -n wouf-receipt-v1`, with `canonical(receipt)` on stdin. `KEY` is
  `[federation] signing_key`, checked as `federate` checks it: outside the team folder, its `.pub` next to it, and
  `ssh-ed25519`. The pack never opens the private key; only `ssh-keygen` reads it.
- **Verify:** `ssh-keygen -Y verify -f SIGNERS -I <team> -n wouf-receipt-v1 -s SIG -O verify-time=T`, plus
  `-r REVOKED` when a revocation file exists. A key listed there fails.
- **Namespace:** `wouf-receipt-v1`, never federation's `wouf-a2a`. An envelope signature never checks as a receipt
  signature, and a receipt signature never checks as an envelope signature.
- **Signers:** lines look like `<team> namespaces="wouf-receipt-v1" ssh-ed25519 AAAA...`. Only plain Ed25519 lines
  for the team count. Lines with other key types, and `cert-authority` lines, are dropped before `ssh-keygen`
  sees them, even where `ssh-keygen` would accept them. This is `federate`'s `trusted_lines` rule, and a test
  compares the two on one table of lines. In one point receipt.py is stricter: `ssh-keygen` reads option names in any
  case, so `CERT-AUTHORITY` also marks a CA line. receipt.py drops such lines, but `federate` still counts them.
  Options are comma-separated without spaces, as in
  `werkstatt valid-before="20271231Z",namespaces="wouf-receipt-v1" ssh-ed25519 AAAA...`. Write times with `Z`:
  without it, `ssh-keygen` reads them in local time.
- **Verify time:** `T` is `resigned_at` when set, else `signed_at`, converted from `2026-11-06T12:00:00Z` to
  `20261106120000Z` (`verify_time`). `ssh-keygen` takes only `YYYYMMDD[HHMM[SS]][Z]` and exits 255 on the ISO form.
  The tests check both forms. A key's `valid-after` and `valid-before` must hold at `T`.
- **OpenSSH 8.7 or newer.** `verify-time` and the `valid-after`/`valid-before` options came with upstream commit
  d0bb1ce ("Let allowed signers files used by ssh-keygen(1) signatures support key lifetimes, and allow the
  verification mode to specify a signature time to check at", 2021-07-23). OpenSSH 8.7 (2021-08-20) was the first
  release to ship it. Source: the upstream changelog that Debian ships, `/usr/share/doc/openssh-client/changelog.gz`.
  The tests ran on OpenSSH 9.2p1. Federation alone needs only 8.1.
- **Trust root:** the key's fingerprint (the `SHA256:` form `ssh-keygen -l` prints), published in this repository (the README and this page) and on
  wouf.dev. Those two copies come from the same repository, so a careful user compares the fingerprint with a second
  copy, for example the release note of the tag, before trusting it. For WOUF's own receipts it is
  `SHA256:kQfxHJ5w0JRzMT4JIburS5E0zGHlwk47+Zvauk5Ac1w` (key type ED25519). An `allowed_signers` file in a repository
  is a convenience only, because whoever can write the repository can write that file.

## Events

`receipt.signed` and `receipt.resigned` go through `event_append`, with exactly
`{task, landed, reviewed, sha256}`. `task` is a task id, `landed` and `reviewed` are 40 lower-case hex, and
`sha256` (of the stored file) is 64 lower-case hex. Any other key is refused, and so is a missing one. The event
cannot hold the receipt itself, since lines are capped at 4096 bytes and the signature's base64 is token-like.
`bin/log` keeps these kinds in `RECEIPT_EVENTS`, apart from `EVENT_DATA_KEYS`, because their keys are exact while
the kinds of `EVENT_DATA_KEYS` take any subset of their keys.

## Library

| Function | Does |
|---|---|
| `canonical(value)` | The wouf-cj1 bytes. Refuses a float, a non-text key or a non-JSON type |
| `check_receipt(receipt)` | Secret rule, value domain, field checks. Raises `Refused` |
| `parse(data)` | The strict parser of the stored file. Returns `{"receipt": ..., "sig": ...}` |
| `stored(receipt, sig)` | The bytes of the stored file |
| `signing_key(value=None)` | The checked path of `[federation] signing_key` |
| `sign(receipt, key=None)` | The armored signature |
| `trusted_lines(text, team)` | The signers lines that count |
| `verify_time(stamp)`, `signature_time(receipt)` | The `verify-time` form, and the time a signature is checked at |
| `signature_ok(receipt, sig, signers, revoked=None)` | Whether the signature is good for the team's trusted key at `T` |

`Refused` messages are safe to print: they name fields and rules, never values.

## Export as in-toto

`team receipt export SHA --in-toto [--repo PATH]` prints a deterministic in-toto Statement v1. Its subject names the receipt's repository label and uses `change.landed` as its `gitCommit` digest. The predicate type is `https://wouf.dev/receipt/v1`; the predicate contains `receipt`, `sig`, and `namespace` (`wouf-receipt-v1`). The signed truth remains the receipt and its SSH signature, both unchanged by export.

Optionally, use your own tooling to sign a Statement predicate saved in a file, for example:

```sh
cosign attest-blob --type https://wouf.dev/receipt/v1 --predicate <file with .predicate>
```

Wouf does not run this command or sign the Statement.

## Offline verification and limits

Fetch the receipt notes before going offline:

```sh
git fetch origin 'refs/notes/wouf-receipts:refs/notes/wouf-receipts'
team verify COMMIT --signers /path/to/allowed_signers
```

After a squash merge the reviewed commit H is not on main, and no WOUF command publishes it. A clone of main alone
then checks the landed commit but not H, and `team verify` ends `partial` (exit 3), naming `reviewed` as not
checked here. A clone that also holds H gets the full check (exit 0).

The receipt parser rejects a red or mismatched gate and forged counting approvals as `FAIL form`; the later gate and reviews checks also defend those rules.

`team verify` never fetches. It also accepts `pr:N` from `team/receipts`, or a receipt file; use `--commit` with a file to check its subject. Exit 0 means both landed and reviewed objects were checked, 3 means a partial check, 1 means a refusal, and 2 means a usage or setup error. Compare the final line's key fingerprint with the fingerprint of the signer. For WOUF's own receipts it is `SHA256:kQfxHJ5w0JRzMT4JIburS5E0zGHlwk47+Zvauk5Ac1w`, published in this repository and on wouf.dev; compare it with a second copy too, for example the release note of the tag, because the repository copy alone is not independent of the repository. A signers file in the repository is only a convenience and is not a trust root.

A receipt proves what the team recorded and signed, not code correctness, model honesty, or that a reviewer read every line. The signer is trusted: a compromised lead can sign anything. There is no independent witness or transparency log, and a team may withhold a receipt. It proves facts about the landed commit, not that it is on a repository's main branch; `repo` is a label.

Token counts are reported inside bot storage and can be changed there. Complexity is printed by the gate command, which runs the head's scripts. Egress records show what passed the proxy, not how the lead configured the sandbox, and a private host may relay traffic. Signature time is the signer's clock, which key validity windows trust. Vendor is inferred from the CLI name; a CLI can use another backend, and outside-flow runs use configuration at receipt time.
