"""Tests for .agents/bin/teamlib/snapshot.py: `team snapshot --json` (schema wouf.cockpit.v1) and `team events --since
SEQ --json`, the read-only interface the companion binds to. Fixture events go into a team folder of their own; the
commands read the state cache and the event log, mask every string, strip control characters, cap text and leave no
home path, host alias or user name in what they print."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / ".agents"
sys.path.insert(0, str(SOURCE / "bin"))
from teamlib import snapshot  # noqa: E402

ORIGIN = "a1b2c3d4e5f6"
HEAD = "0123456789abcdef0123456789abcdef01234567"
# Synthetic values built from parts, so this file holds no contiguous key; none of them works anywhere.
SLACK = "xox" "b-1234567890-" + "abcdefghijklmnop"
STORED = "plain-stored-value-4711"
USER = "zorbatest"
ALIAS = "buildbox-alias"

CONFIG = f"""[team]
name = Werk
autonomy = ask
runner = auto

[environment]
mode = host
docker_host = ssh://{USER}@{ALIAS}:2222
"""
BOTS = (("worker", "Worker", "worker", "active", "auto", "strong"),
        ("bugfixer", "Bugfixer", "worker", "active", "auto", "light"),
        ("review", "Review", "review", "active", "auto", "strong"),
        ("decide", "Decide", "decide", "active", "auto", "strong"),
        ("scout", "Scout", "scout", "retired", "auto", "light"))


def run_data(bot: str, runner: str, tier: str, outcome: str, **more) -> dict:
    return {"bot": bot, "runner": runner, "tier": tier, "model": "default", "effort": "high", "outcome": outcome,
            **more}


# One small team: two tasks, a dependency, a phase, runs, a review, a merge proposal, a question, a decision, a limit.
FIXTURE = (
    ("task.created", "lead", "T-1", {"title": "Parser", "class": "complex", "owner": "worker"}),
    ("task.state", "lead", "T-1", {"state": "working", "owner": "worker"}),
    ("task.created", "lead", "T-2", {"title": "Docs", "class": "standard", "owner": "bugfixer"}),
    ("claim.set", "lead", "T-1", {"id": "C-1", "owner": "worker", "paths": ["src/parser.py", "tests/test_parser.py"]}),
    ("dag.edge", "lead", "", {"from": "T-1", "to": "T-2", "relation": "depends_on"}),
    ("phase.state", "lead", "T-1", {"phase": "REALIZE", "state": "active"}),
    ("run.started", "lead", "T-1", run_data("worker", "claude", "strong", "started", ref="0123456")),
    ("run.finished", "lead", "T-1", run_data("worker", "claude", "strong", "done", ref="task/T-1")),
    ("run.started", "lead", "T-1", run_data("review", "claude", "strong", "started")),
    ("run.finished", "lead", "T-1", run_data("review", "claude", "strong", "done", verdict="approve")),
    ("flow.step", "lead", "T-1", {"kind": "end", "status": "ready"}),
    ("merge.proposed", "lead", "T-1", {"task": "T-1", "head": HEAD, "branch": "task/T-1", "verdict": "approve"}),
    ("question.asked", "bugfixer", "T-2", {"id": "Q-1", "text": "Which format?", "from": "bugfixer"}),
    ("decision.recorded", "lead", "T-2", {"number": 1, "title": "Use Markdown", "options": ["md", "rst"],
                                          "why": "simpler to review", "risk": "low", "authority": "owner",
                                          "next": "human, approve or reject", "status": "pending"}),
    ("run.finished", "lead", "T-2", run_data("bugfixer", "codex", "light", "limited")),
    ("flow.step", "lead", "T-2", {"kind": "worker", "status": "limited", "until": "2099-01-01T00:00:00Z"}),
)

ANSWER = 'team question answer Q-1 "\u2026"'
GOLDEN = {
    "schema": "wouf.cockpit.v1",
    "team": {"name": "Werk", "autonomy": "ask", "seq": 16},
    "projects": [{"id": "werk", "name": "Werk"}],
    "phases": [{"id": "T-1:REALIZE", "project": "werk", "task": "T-1", "kind": "REALIZE", "status": "active",
                "reason": None, "updated": "2026-10-29T10:05:00Z"}],
    "tasks": [
        {"id": "T-1", "title": "Parser", "class": "complex", "status": "working", "owner_bot": "worker",
         "paths": ["src/parser.py", "tests/test_parser.py"], "branch": "task/T-1", "head_sha": HEAD,
         "current_step": "end", "step_status": "ready", "round": 1, "cost_total": "0.40",
         "updated": "2026-10-29T10:01:00Z"},
        {"id": "T-2", "title": "Docs", "class": "standard", "status": "todo", "owner_bot": "bugfixer", "paths": [],
         "branch": None, "head_sha": None, "current_step": "worker", "step_status": "limited", "round": 0,
         "cost_total": None, "updated": "2026-10-29T10:02:00Z"},
    ],
    "edges": [{"from": "T-1", "to": "T-2", "kind": "depends_on"}],
    "bots": [
        {"id": "bugfixer", "name": "Bugfixer", "role": "worker", "lifecycle_status": "active", "runner": "codex",
         "tier": "light", "run_state": "limited", "limited_until": "2099-01-01T00:00:00Z", "shared_quota_pool": "codex",
         "task": "T-2"},
        {"id": "decide", "name": "Decide", "role": "decide", "lifecycle_status": "active", "runner": "auto",
         "tier": "strong", "run_state": "idle", "limited_until": None, "shared_quota_pool": None, "task": None},
        {"id": "review", "name": "Review", "role": "review", "lifecycle_status": "active", "runner": "claude",
         "tier": "strong", "run_state": "idle", "limited_until": None, "shared_quota_pool": "claude", "task": "T-1"},
        {"id": "scout", "name": "Scout", "role": "scout", "lifecycle_status": "retired", "runner": "auto",
         "tier": "light", "run_state": "offline", "limited_until": None, "shared_quota_pool": None, "task": None},
        {"id": "worker", "name": "Worker", "role": "worker", "lifecycle_status": "active", "runner": "claude",
         "tier": "strong", "run_state": "idle", "limited_until": None, "shared_quota_pool": "claude", "task": "T-1"},
    ],
    "questions": [{"id": "Q-1", "task": "T-2", "from": "bugfixer", "text": "Which format?", "status": "open",
                   "cli_command": ANSWER}],
    "decisions": [{"id": "D-1", "task": "T-2", "title": "Use Markdown", "status": "pending",
                   "lead_proposal": "simpler to review", "options": ["md", "rst"],
                   "cli_command": "team decision approve D-1", "cli_reject": "team decision reject D-1"}],
    "merges": [{"task": "T-1", "branch": "task/T-1", "head_sha": HEAD, "review_verdict": "approve",
                "pre_gate_status": "green", "status": "proposed", "cli_command": f"team merge mark T-1 --head {HEAD}"}],
}


class Team:
    """A team folder of its own (config, bot declarations, events, receipts, a secret store) and a home folder."""

    def __init__(self, bots=BOTS) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.dir = base / "team"
        self.home = base / "home"
        self.store = base / "secrets"
        for folder in (self.dir / "bots", self.home, self.store):
            folder.mkdir(parents=True)
        (self.dir / "config.ini").write_text(CONFIG)
        for member, name, role, status, runner, tier in bots:
            (self.dir / "bots" / f"{member}.md").write_text(
                f"---\nid: {member}\nname: {name}\nrole: {role}\nstatus: {status}\nrunner: {runner}\ntier: {tier}\n"
                f"connectors: none\n---\n\n# {name}\n")
        # board.md is never read: a row only it has must not appear.
        (self.dir / "board.md").write_text("| T-99 | only on the board | worker | todo | - | - | - | - |\n")
        self.seq = 0
        caller = {k: v for k, v in os.environ.items() if not k.startswith(("WOUF_", "GIT_"))}
        self.env = {**caller, "WOUF_TEAM_DIR": str(self.dir), "WOUF_SECRET_BACKEND": "file",
                    "WOUF_SECRET_DIR": str(self.store), "HOME": str(self.home), "USER": USER, "LOGNAME": USER}

    def close(self) -> None:
        self.tmp.cleanup()

    def event(self, kind: str, actor: str, task: str, data: dict) -> None:
        """One event line as bin/log writes it; written here directly, so a fixture can hold what add refuses."""
        self.seq += 1
        minute = self.seq - 1
        event = {"v": 1, "id": f"{ORIGIN}-{self.seq}", "seq": self.seq,
                 "t": f"2026-10-29T{10 + minute // 60:02d}:{minute % 60:02d}:00Z", "origin": ORIGIN, "actor": actor,
                 "kind": kind, "task": task, "scope": "local", "data": data}
        folder = self.dir / "events"
        folder.mkdir(exist_ok=True)
        with (folder / f"{ORIGIN}.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=True, separators=(",", ":")) + "\n")

    def events(self, rows) -> None:
        for row in rows:
            self.event(*row)

    def receipt(self, who: str, task: str, cost: str) -> None:
        folder = self.dir / "logs"
        folder.mkdir(exist_ok=True)
        with (folder / f"{who}.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"t": "2026-10-29T10:30:00.000+00:00", "who": who, "task": task,
                                     "outcome": "done", "signal": "ok", "cost": cost}) + "\n")

    def secret(self, name: str, value: str) -> None:
        (self.store / name).write_text(value + "\n")
        (self.store / name).chmod(0o600)
        (self.store / ".names").write_text(name + "\n")
        (self.store / ".names").chmod(0o600)

    def team(self, *args: str, code: int = 0) -> subprocess.CompletedProcess:
        done = subprocess.run([sys.executable, str(SOURCE / "bin" / "team"), *args], env=self.env,
                              capture_output=True, text=True, timeout=120, cwd=self.home)
        assert done.returncode == code, done.stdout + done.stderr
        return done

    def snapshot(self, *args: str) -> dict:
        done = self.team("snapshot", *args, "--json")
        self.assertion_one_line(done.stdout)
        return json.loads(done.stdout)

    def feed(self, *args: str) -> list[dict]:
        return [json.loads(line) for line in self.team("events", *args, "--json").stdout.splitlines()]

    @staticmethod
    def assertion_one_line(text: str) -> None:
        assert text.endswith("\n") and text.count("\n") == 1, text


class SnapshotTest(unittest.TestCase):
    def setUp(self) -> None:
        self.t = Team()
        self.addCleanup(self.t.close)

    def test_fixture_events_give_the_golden_snapshot(self) -> None:
        self.t.events(FIXTURE)
        self.t.receipt("worker", "T-1", "$0.25")
        self.t.receipt("worker", "T-1", "0.15")
        snap = self.t.snapshot()
        generated = snap.pop("generated")
        self.assertRegex(generated, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(snap, GOLDEN)
        self.assertNotIn("T-99", json.dumps(snap))

    def test_every_object_has_the_fields_of_the_schema(self) -> None:
        self.t.events(FIXTURE)
        snap = self.t.snapshot()
        schema = json.loads(snapshot.SCHEMA)
        self.assertEqual(schema["properties"]["schema"]["const"], snapshot.SCHEMA_ID)
        self.assertEqual(set(snap), set(schema["required"]))
        for section in ("projects", "phases", "tasks", "edges", "bots", "questions", "decisions", "merges"):
            item = schema["$defs"][schema["properties"][section]["items"]["$ref"].rsplit("/", 1)[1]]
            for obj in snap[section]:
                self.assertEqual(set(obj), set(item["required"]), section)
                self.assertLessEqual(set(obj), set(item["properties"]), section)
        self.assertEqual(set(snap["team"]), set(schema["$defs"]["team"]["required"]))
        # Additive only: a client of v1 ignores fields it does not know.
        self.assertTrue(all(d.get("additionalProperties", True) for d in schema["$defs"].values()))

    def test_task_filter(self) -> None:
        self.t.events(FIXTURE)
        snap = self.t.snapshot("--task", "T-2")
        self.assertEqual([t["id"] for t in snap["tasks"]], ["T-2"])
        self.assertEqual(snap["phases"], [])
        self.assertEqual(snap["merges"], [])
        self.assertEqual([q["id"] for q in snap["questions"]], ["Q-1"])
        self.assertEqual([d["id"] for d in snap["decisions"]], ["D-1"])
        self.assertEqual(snap["edges"], GOLDEN["edges"])
        self.assertEqual(len(snap["bots"]), 5)
        self.assertEqual(self.t.snapshot("--task", "T-1")["questions"], [])
        self.assertIn("task", self.t.team("snapshot", "--task", "../x", "--json", code=2).stderr)

    def test_answered_and_decided_rows_have_no_command(self) -> None:
        self.t.events(FIXTURE)
        self.t.events([
            ("question.answered", "lead", "T-2", {"id": "Q-1", "text": "Markdown", "from": "bugfixer"}),
            ("decision.recorded", "lead", "T-2", {"number": 1, "title": "Use Markdown", "why": "simpler to review",
                                                  "next": "human, approve or reject", "status": "approved",
                                                  "decided_by": "human"}),
            ("merge.marked", "lead", "T-1", {"task": "T-1", "head": HEAD, "branch": "task/T-1", "verdict": "approve"}),
        ])
        snap = self.t.snapshot()
        self.assertEqual((snap["questions"][0]["status"], snap["questions"][0]["cli_command"]), ("answered", None))
        self.assertEqual((snap["decisions"][0]["status"], snap["decisions"][0]["cli_command"],
                          snap["decisions"][0]["cli_reject"]), ("approved", None, None))
        self.assertEqual((snap["merges"][0]["status"], snap["merges"][0]["cli_command"]), ("marked", None))

    def test_a_key_and_a_home_path_in_a_bot_question_are_masked(self) -> None:
        self.t.secret("ALPHA_KEY", STORED)
        home = str(self.t.home)
        lines = [f"see {SLACK} and {STORED}", f"in /home/{USER}/src/notes.md or", f"{home}/.config/x,",
                 f"as {USER} on {ALIAS}", "in /srv/build/out.log\x1b[31m red\x07", f"C:\\Users\\{USER}\\x.txt",
                 "file:///opt/a", "cwd:/srv/x", "path:/srv/build/x", "scp h:/srv/x", "-/srv/x", ".../srv/x"]
        self.t.event("question.asked", "bugfixer", "T-2", {"id": "Q-1", "text": "\n".join(lines), "from": "bugfixer"})
        self.t.event("task.created", "lead", "T-2", {"title": f"read {home}/notes", "class": "standard"})
        for out in (self.t.team("snapshot", "--json").stdout, self.t.team("events", "--since", "0", "--json").stdout):
            for leaked in (SLACK, STORED, home, f"/home/{USER}", USER, ALIAS, "/srv", "/opt", "\\u001b", "\\u0007",
                           "Users"):
                self.assertNotIn(leaked, out)
        said = self.t.snapshot()["questions"][0]["text"]
        self.assertEqual(said, "see *** and *** in <path> <path> as <user> on <host> in <path> red <path> file:<path> "
                               "cwd:<path> path:<path> scp <path> -<path> ...<path>")
        self.assertEqual(self.t.snapshot()["tasks"][0]["title"], "read <path>")
        [event, _] = self.t.feed("--since", "0")
        self.assertTrue(event["data"]["text"].startswith("see *** and *** in <path> <path> as <user>"))

    def test_every_absolute_path_is_masked(self) -> None:
        # A root: / at a word start or after : or =, ~, X: before / or \, \\ or // at a word start; to the next
        # whitespace, quote, |, <, >, ), ] or line end; a home or users segment takes the rest of the line.
        cases = {
            "file:///opt/a": "file:<path>", "cwd:/srv/x": "cwd:<path>", "path:/srv/build/x": "path:<path>",
            "scp h:/srv/x": "scp <path>", "-/srv/x": "-<path>", ".../srv/x": "...<path>", "x=/srv/x y": "x=<path> y",
            "/srv/x": "<path>", "run /opt/tool --flag": "run <path> --flag", "~/notes b": "<path> b",
            "~someone/x b": "<path> b", "C:\\work\\x.txt b": "<path> b", "D:/work/x b": "<path> b",
            "\\\\server\\share b": "<path> b", "//server/share b": "<path> b",
            "\"/srv/x\" '/srv/y' `/srv/z`": "\"<path>\" '<path>' `<path>`",
            "(/opt/y) [/opt/z] a|/b|c </srv/x>": "(<path>) [<path>] a|<path>|c <<path>>",
            "error at /workspace/app.py:12": "error at <path>",
            "a /srv/Home/x b c\nnext": "a <path> next", "a /home/u b\rc": "a <path> c",
            "a C:\\Users\\u\\x b\nc": "a <path> c", "a /bot/USERS b": "a <path>", "a //nas/home b": "a <path>",
            "a /srv/homes/x b": "a <path> b", "https://example.org/x y": "https:<path> y",
            ".agents/bin/teamlib/snapshot.py tests/x.py a/b": ".agents/bin/teamlib/snapshot.py tests/x.py a/b",
            "<team>/bots/worker.md and <repo>/PLAN.md": "<team>/bots/worker.md and <repo>/PLAN.md",
        }
        for raw, want in cases.items():
            self.assertEqual(snapshot.clean(raw, "text"), want, raw)
        self.assertEqual(snapshot.clean(f"{snapshot.PROJECT}/PLAN.md /srv/x", "text"), "<repo>/PLAN.md <path>")
        home = os.environ.get("HOME", "")
        if len(home) > 1:
            self.assertEqual(snapshot.clean(f"a x{home}/b c\nd", "text"), "a x<path> d")

    def test_caps(self) -> None:
        self.t.event("task.created", "lead", "T-1", {"title": "t" * 450, "class": "standard"})
        self.t.event("question.asked", "worker", "T-1", {"id": "Q-1", "text": "q" * 500, "from": "worker"})
        snap = self.t.snapshot()
        self.assertEqual(len(snap["tasks"][0]["title"]), 200)
        self.assertEqual(len(snap["questions"][0]["text"]), 500)
        self.assertEqual(len(snapshot.clean("x" * 5000, "text")), 2000)
        self.assertEqual(len(snapshot.clean("x" * 5000, "title")), 200)
        self.assertEqual(snapshot.clean("a\x00b\nc\u202ed\x9f", "text"), "a b c d")
        # Masked before the cut: a key across the cap is hidden whole, never left as a prefix.
        cut = snapshot.clean("y" * 190 + " " + SLACK, "title")
        self.assertNotIn(SLACK[:8], cut)

    def test_an_empty_team(self) -> None:
        empty = Team(bots=())
        self.addCleanup(empty.close)
        snap = empty.snapshot()
        snap.pop("generated")
        self.assertEqual(snap, {"schema": "wouf.cockpit.v1", "team": {"name": "Werk", "autonomy": "ask", "seq": 0},
                                "projects": [{"id": "werk", "name": "Werk"}], "phases": [], "tasks": [], "edges": [],
                                "bots": [], "questions": [], "decisions": [], "merges": []})
        self.assertEqual(empty.team("events", "--since", "0", "--json").stdout, "")
        self.assertFalse((empty.dir / "events").exists())

    def test_read_only(self) -> None:
        self.t.events(FIXTURE)
        before = (self.t.dir / "events" / f"{ORIGIN}.jsonl").read_bytes()
        board = (self.t.dir / "board.md").read_bytes()
        self.t.snapshot()
        self.t.feed("--since", "0")
        self.assertEqual((self.t.dir / "events" / f"{ORIGIN}.jsonl").read_bytes(), before)
        self.assertEqual(sorted(p.name for p in (self.t.dir / "events").iterdir()), [f"{ORIGIN}.jsonl"])
        self.assertEqual((self.t.dir / "board.md").read_bytes(), board)

    def test_json_is_required_and_arguments_are_checked(self) -> None:
        for args in (("snapshot",), ("events", "--since", "0"), ("events", "--json"), ("events", "--since", "x", "--json"),
                     ("events", "--since", "0", "--limit", "0", "--json"),
                     ("events", "--since", "0", "--limit", "1001", "--json"), ("snapshot", "--json", "extra")):
            with self.subTest(args=args):
                self.assertEqual(self.t.team(*args, code=2).stdout, "")


class EventsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.t = Team()
        self.addCleanup(self.t.close)
        self.t.events(FIXTURE)

    def test_since_pages_oldest_first(self) -> None:
        first = self.t.feed("--since", "0", "--limit", "5")
        self.assertEqual([e["seq"] for e in first], [1, 2, 3, 4, 5])
        second = self.t.feed("--since", str(first[-1]["seq"]), "--limit", "5")
        self.assertEqual([e["seq"] for e in second], [6, 7, 8, 9, 10])
        self.assertEqual([e["seq"] for e in self.t.feed("--since", "10")], [11, 12, 13, 14, 15, 16])
        self.assertEqual(self.t.feed("--since", "16"), [])
        self.assertEqual(set(first[0]), {"v", "id", "seq", "t", "origin", "actor", "kind", "task", "scope", "data"})
        self.assertEqual(first[0]["data"], FIXTURE[0][3])

    def test_the_default_limit_is_200_and_1000_at_most(self) -> None:
        for n in range(250):
            self.t.event("flow.step", "lead", "T-1", {"kind": "worker", "status": f"s{n}"})
        self.assertEqual(len(self.t.feed("--since", "0")), 200)
        self.assertEqual(len(self.t.feed("--since", "0", "--limit", "1000")), 266)
        self.assertEqual(self.t.feed("--since", "260", "--limit", "3")[-1]["seq"], 263)

    def test_snapshot_seq_is_the_cursor_of_the_feed(self) -> None:
        seq = self.t.snapshot()["team"]["seq"]
        self.assertEqual(self.t.feed("--since", str(seq)), [])
        self.t.event("task.state", "lead", "T-2", {"state": "working"})
        self.assertEqual([e["kind"] for e in self.t.feed("--since", str(seq))], ["task.state"])


def busy(team: Team) -> None:
    """20 tasks, 10 decisions, 5 bots, with questions, runs, edges and merge proposals."""
    for n in range(1, 21):
        task = f"T-{n}"
        team.event("task.created", "lead", task, {"title": f"package {n}: " + "w" * 120, "class": "complex",
                                                  "owner": "worker"})
        team.event("task.state", "lead", task, {"state": ("working", "review", "blocked", "done")[n % 4],
                                                "blocked_reason": "waits for input" if n % 4 == 2 else ""})
        team.event("claim.set", "lead", task, {"id": f"C-{n}", "owner": "worker", "paths": [f"src/m{n}.py"]})
        team.event("run.finished", "lead", task, run_data("worker", "claude", "strong", "done", ref=f"task/{task}"))
        team.event("flow.step", "lead", task, {"kind": "review", "status": "approve"})
        if n > 1:
            team.event("dag.edge", "lead", "", {"from": f"T-{n - 1}", "to": task, "relation": "depends_on"})
        if n % 2:
            team.event("question.asked", "worker", task, {"id": f"Q-{n}", "text": "which way? " + "q" * 300,
                                                          "from": "worker"})
        if n % 4 == 1:
            team.event("merge.proposed", "lead", task, {"task": task, "head": HEAD, "branch": f"task/{task}",
                                                        "verdict": "approve"})
    for n in range(1, 11):
        team.event("decision.recorded", "lead", f"T-{n}", {"number": n, "title": f"decision {n}", "options": ["a", "b"],
                                                           "why": "because " + "r" * 300, "risk": "low",
                                                           "authority": "owner", "next": "human", "status": "pending"})


class BusyTest(unittest.TestCase):
    def test_a_busy_team_stays_small_and_fast(self) -> None:
        t = Team()
        self.addCleanup(t.close)
        busy(t)
        t.team("snapshot", "--json")  # the first call builds the cache
        start = time.monotonic()
        out = t.team("snapshot", "--json").stdout
        elapsed = time.monotonic() - start
        snap = json.loads(out)
        self.assertEqual((len(snap["tasks"]), len(snap["decisions"]), len(snap["bots"])), (20, 10, 5))
        self.assertLess(len(out.encode()), 64 * 1024)
        self.assertLess(elapsed, 5)


if __name__ == "__main__":
    unittest.main()
