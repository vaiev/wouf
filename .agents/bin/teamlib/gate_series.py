"""teamlib.gate_series: the completeness gate of a change that comes in packages (T-0189). A brief's line
`Series: NAME K/N` (NAME a task id, 1 <= K <= N <= 20) says it is package K of N of one change. On a base on main
(`[gate] main`, default main; a base commit on that branch counts as main) package K < N is red: a series lands on a
feature branch, then as one complete change. Package N/N is red there unless its diff holds the migration
.agents/upgrade/<VERSION>.md (VERSION unchanged: or the brief's `Migration: none REASON`) and an upgrade test
tests/test_upgrade_*.py, which the gate runs itself on the head, in a sandbox run of its own, after a green suite.
gate.json keeps series, migration and upgrade_test. A brief without a Series line gates as before, with none of them.
A leaf: teamlib.gate reaches it through gate.SERIES, which this module fills when bin/team loads it (as
teamlib.evidence fills gate.EVIDENCE)."""

from __future__ import annotations

import shlex

from teamlib.core import *  # noqa: F401,F403
from teamlib import gate
from teamlib.run import bundle_commit

MAX_PACKAGES = 20
# K and N: one or two ASCII digits before int() (N <= 20), so a longer number, a sign, another script's digit or
# whitespace inside them is a malformed line, never an exception (int() refuses more than 4300 digits).
SERIES_VALUE = re.compile(r"(\S+)[ \t]+([0-9]{1,2})/([0-9]{1,2})")
SERIES_RULE = f"Series: NAME K/N, NAME a task id, 1 <= K <= N <= {MAX_PACKAGES}"
VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
# A module unittest can import by name: the gate runs it as python3 -m unittest tests.test_upgrade_<name>.
UPGRADE_TEST = re.compile(r"tests/test_upgrade_[A-Za-z0-9_]+\.py")
NO_MIGRATION_REASON = re.compile(r"none\b[\s,.:;()–—-]*\w.*")
NOT_CHECKED = "not checked"
NO_TEST = "series complete but no green upgrade test"
# python3 -m unittest exits 0 for a module without tests and for skipped tests only: the upgrade test must run at least
# one test that passes, or the gate has verified nothing. Its log header still names python3 -m unittest.
RUN_UNITTEST = shlex.quote("import sys, unittest; r = unittest.main(module=None, argv=['unittest', *sys.argv[1:]], "
                           "exit=False).result; sys.exit(0 if r.wasSuccessful() and r.testsRun > len(r.skipped) "
                           "else 1)")


def lines_of(text: str, name: str) -> list[str]:
    """The values of the brief's lines `NAME: value`, at the start of a line, as brief_header reads its own."""
    return [line[len(name) + 1:].strip() for line in text.splitlines() if line.startswith(f"{name}:")]


def series_of(values: list[str]) -> tuple[dict | None, str | None]:
    """The one Series line as {name, package, of}, or why it is malformed (no value of it repeated but numbers)."""
    if len(values) > 1:
        return None, f"{len(values)} Series lines, a brief has one"
    found = SERIES_VALUE.fullmatch(values[0])
    if not found:
        return None, "not NAME K/N"
    name, k, n = found.group(1), int(found.group(2)), int(found.group(3))
    checks = ((bool(task_problem(name)), "NAME is no task id"), (k < 1 or n < 1, "K and N count from 1"),
              (k > n, f"K {k} is greater than N {n}"), (n > MAX_PACKAGES, f"N {n} is more than {MAX_PACKAGES}"))
    problem = next((why for bad, why in checks if bad), None)
    return (None, problem) if problem else ({"name": name, "package": k, "of": n}, None)


def on_main(ini, repo_dir, start: str) -> tuple[str, bool | None]:
    """[gate] main and whether the base commit is on that branch; None when the repository has no such branch.
    A git error other than "not an ancestor" counts as on main: the stricter verdict."""
    branch = get(ini, "gate", "main", "main") or "main"
    tip = git("rev-parse", "-q", "--verify", f"refs/heads/{branch}^{{commit}}", check=False, at=repo_dir).stdout.strip()
    if not tip:
        return branch, None
    return branch, git("merge-base", "--is-ancestor", start, tip, check=False, at=repo_dir).returncode != 1


def git_bytes(repo_dir, *args: str) -> str | None:
    """git's output as text (surrogateescape: a name or file a bot chose may be no UTF-8), None when git fails."""
    done = subprocess.run(["git", "-C", str(repo_dir or PROJECT), *args], capture_output=True)
    return done.stdout.decode("utf-8", "surrogateescape") if done.returncode == 0 else None


def version_at(repo_dir, commit: str) -> str:
    """.agents/VERSION at commit, stripped; empty when there is none."""
    return (git_bytes(repo_dir, "cat-file", "blob", f"{commit}:.agents/VERSION") or "").strip()


def red(fields: dict, why: str) -> dict:
    return {"why": why, "fields": fields, "tests": []}


def plan(ini, brief: str | None, repo_dir, start: str, head: str) -> dict:
    """What the brief's Series line asks of this gate, decided before anything runs: why (a red reason, else None),
    the gate.json fields (none without a Series line) and the upgrade tests finish runs."""
    text = Path(brief).read_text(encoding="utf-8") if brief else ""
    values = lines_of(text, "Series")
    if not values:
        return {"why": None, "fields": {}, "tests": []}
    series, problem = series_of(values)
    fields = {"series": series or "invalid", "migration": NOT_CHECKED, "upgrade_test": NOT_CHECKED}
    if problem:
        return red(fields, f"malformed Series line: {problem} ({SERIES_RULE})")
    branch, main = on_main(ini, repo_dir, start)
    series["main"] = bool(main)
    if main is None:
        return red(fields, f"series: no branch {visible(branch)} ([gate] main) in the repository, so the gate cannot "
                           "tell whether the base is on main")
    if not main:
        return {"why": None, "fields": fields, "tests": []}
    if series["package"] < series["of"]:
        return red(fields, f"package {series['package']} of {series['of']} cannot land on main alone; land the "
                           "series on a feature branch, then one complete change")
    return complete(fields, text, repo_dir, start, head)


def complete(fields: dict, text: str, repo_dir, start: str, head: str) -> dict:
    """Package N of N on main: the migration and the upgrade tests must be in the diff (present at the head)."""
    names = git_bytes(repo_dir, "diff", "-z", "--name-only", "--no-renames", "--diff-filter=d", start, head) or ""
    changed = {name for name in names.split("\0") if name}
    old, new = version_at(repo_dir, start), version_at(repo_dir, head)
    migration = f".agents/upgrade/{new}.md" if VERSION.fullmatch(new) else ".agents/upgrade/<VERSION>.md"
    if VERSION.fullmatch(new) and migration in changed:
        fields["migration"] = migration
    elif old == new and [bool(NO_MIGRATION_REASON.fullmatch(v)) for v in lines_of(text, "Migration")] == [True]:
        fields["migration"] = "none"
    else:
        fields["migration"] = "missing"
        unchanged = " (VERSION is unchanged: or the brief's line Migration: none REASON)" if old == new else ""
        return red(fields, f"series complete but no migration {migration}{unchanged}")
    tests = sorted(name for name in changed if UPGRADE_TEST.fullmatch(name))
    fields["upgrade_test"] = {"files": tests, "code": None} if tests else "missing"
    return {"why": None, "fields": fields, "tests": tests} if tests else red(fields, NO_TEST)


def finish(ini, planned: dict, result: dict, stage: Path, repo_dir, task: str) -> dict:
    """The gate.json fields of the series. With upgrade tests to run and the gate green so far, the gate runs them
    itself on the head: gate_execute, the sandbox of the suite, in a run of its own (stage/upgrade/<task>: a fresh
    checkout, HOME and cache, its own exit code), its output appended to gate.log; red when they fail."""
    fields = planned["fields"]
    if not planned["tests"] or result["state"] != "green":
        return fields
    run = stage / "upgrade" / task
    run.mkdir(parents=True)
    ref = bundle_commit(repo_dir, run / "repo.bundle", result["head"], f"gate-{task}-upgrade")
    modules = " ".join(name[:-3].replace("/", ".") for name in planned["tests"])
    command = f"PYTHONDONTWRITEBYTECODE=1 python3 -c {RUN_UNITTEST} {modules}"
    code = gate.gate_execute(ini, run, ref, result["head"], command, result["base"])
    copied = (run / "gate.log").read_text(encoding="utf-8", errors="replace")  # masked as it was written
    put(stage / "gate.log", f"--- upgrade test: python3 -m unittest {modules}: exit {code} ---\n{copied}", mode="a")
    fields["upgrade_test"] = {"files": planned["tests"], "code": code}
    return fields if code == 0 else {**fields, "state": "red", "why": NO_TEST}


# teamlib.gate reaches this module only through this hook (gate.series); a gate.py without it is left as it is.
getattr(gate, "SERIES", [])[:] = [sys.modules[__name__]]
