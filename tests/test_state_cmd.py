"""Tests for .agents/bin/teamlib/state.py: task, question, decision, merge, schedule and state import write events
into the store and regenerate board.md, decisions.md and schedule.md from it; hand edits are detected, never
overwritten, and offered for import (owner Q1)."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from test_team_cli import SOURCE, Project, team_module

sys.path.insert(0, str(SOURCE / "bin"))
from teamlib import state  # noqa: E402

# federate's shape of a decision record (federate:73-74).
DECISION = re.compile(r"^D-[0-9]+ [0-9]{4}-[0-9]{2}-[0-9]{2} \S+ — .+")
RECORD_LINE = re.compile(r"^  \S.*\Z")
# A fabricated GitHub fine-grained token built from parts (as in test_team_cli.ContextTest); it works nowhere.
FAKE_KEY = "github_" "pat_" + "Zx9Kq2Lm7Pw4Rt6Vb8Ny3H" + "_" + "Qw8Er5Ty2Ui7Op4As9Df1Gh6Jk3Lz0Xc5Vb8Nm2Qw7Er4Ty1Ui9Op6As3Df"
PROSE = "template prose: board.md's prose is the template's, rewritten on every export"
UPDATED = "derived: the store sets it to the day of T-1's latest event"
HEAD = "0123456789abcdef0123456789abcdef01234567"
OTHER = "fedcba9876543210fedcba9876543210fedcba98"


def stats_classes(project) -> dict:
    """stats.task_classes() as the project's own commands see the store."""
    code = f"import json, sys; sys.path.insert(0, {str(project.root / '.agents' / 'bin')!r}); from teamlib import stats; " \
           "print(json.dumps(stats.task_classes()))"
    done = subprocess.run([sys.executable, "-c", code], cwd=project.root, env=project.env, capture_output=True,
                          text=True)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


class StateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.p = Project()
        self.addCleanup(self.p.close)
        self.team_dir = self.p.root / ".agents" / "team"

    def team(self, *args: str, code: int = 0) -> str:
        done = self.p.team(*args)
        self.assertEqual(done.returncode, code, done.stdout + done.stderr)
        return done.stdout if code == 0 else done.stderr

    def file(self, name: str) -> str:
        return (self.team_dir / name).read_text(encoding="utf-8")

    def events(self, kind: str | None = None) -> list[dict]:
        found = []
        for path in sorted((self.team_dir / "events").glob("*.jsonl")):
            found += [json.loads(line) for line in path.read_text().splitlines()]
        return [e for e in found if kind is None or e["kind"] == kind]

    def hashes(self) -> dict:
        path = self.team_dir / "env" / "state-exports.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def board_rows(self) -> list[str]:
        return [ln for ln in self.file("board.md").splitlines() if ln.startswith("| T-")]


class TaskTest(StateTest):
    def test_task_new_writes_one_event_the_board_row_and_its_hash(self) -> None:
        out = self.team("task", "new", "write the parser", "--class", "complex", "--owner", "worker")
        self.assertEqual(out.splitlines(), ["T-1 todo (complex, owner worker)"])
        [event] = self.events()
        self.assertEqual((event["kind"], event["task"], event["actor"]), ("task.created", "T-1", "lead"))
        self.assertEqual(event["data"], {"title": "write the parser", "class": "complex", "owner": "worker"})
        today = event["t"][:10]
        self.assertEqual(self.board_rows(), [f"| T-1 | write the parser | worker | todo | - | - | - | {today} |"])
        for name in state.EXPORTS:
            self.assertEqual(self.hashes()[name], hashlib.sha256((self.team_dir / name).read_bytes()).hexdigest())

    def test_states_gate_reason_artifacts_and_context(self) -> None:
        self.team("task", "new", "old thing", "--class", "standard", "--owner", "worker")
        self.team("task", "new", "new thing", "--class", "standard", "--owner", "worker")
        self.assertEqual(self.team("task", "start", "T-2", "--gate", "review").strip(), "T-2 working")
        self.assertIn("block needs --reason", self.team("task", "block", "T-2", code=2))
        self.team("task", "block", "T-1", "--reason", "needs the staging URL")
        self.assertIn("| blocked | - | - | needs the staging URL |", self.board_rows()[0])
        self.team("task", "review", "T-1")
        self.team("task", "done", "T-1", "--artifacts", "out/a.md", "out/b.md")
        today = self.events()[-1]["t"][:10]
        self.assertEqual(self.board_rows(), [f"| T-1 | old thing | worker | done | - | out/a.md, out/b.md | - | {today} |",
                                             f"| T-2 | new thing | worker | working | review | - | - | {today} |"])
        self.assertEqual([e["data"]["state"] for e in self.events("task.state")], ["working", "blocked", "review", "done"])
        text = self.team("context")
        self.assertIn("T-2 | new thing", text)
        self.assertNotIn("old thing", text)
        self.assertIn("T-9 is not in the store", self.team("task", "start", "T-9", code=2))
        self.assertIn("not one of", self.team("task", "new", "x", "--class", "urgent", code=2))
        self.assertIn("member id", self.team("task", "new", "x", "--class", "standard", "--owner", "Bad Id", code=2))

    def test_every_status_change_takes_a_reason(self) -> None:
        self.team("task", "new", "x", "--class", "standard")
        self.assertEqual(self.team("task", "start", "T-1", "--reason", "picked up").strip(), "T-1 working")
        self.assertEqual(self.team("task", "working", "T-1", "--reason", "back on it").strip(), "T-1 working")
        self.assertEqual(self.team("task", "review", "T-1", "--reason", "PR open").strip(), "T-1 review")
        self.assertEqual(self.team("task", "todo", "T-1", "--reason", "parked").strip(), "T-1 todo")
        self.assertIn("| todo | - | - | parked |", self.board_rows()[0])
        self.assertEqual(self.team("task", "done", "T-1", "--reason", "landed #1").strip(), "T-1 done")
        reasons = [(e["data"]["state"], e["data"]["blocked_reason"]) for e in self.events("task.state")]
        self.assertEqual(reasons, [("working", "picked up"), ("working", "back on it"), ("review", "PR open"),
                                   ("todo", "parked"), ("done", "landed #1")])
        # The board's reason column, where a block reason shows.
        self.assertIn("| done | - | - | landed #1 |", self.board_rows()[0])
        # Without --reason a status change clears it, as before; block still needs one.
        self.team("task", "start", "T-1")
        self.assertIn("| working | - | - | - |", self.board_rows()[0])
        self.assertIn("block needs --reason TEXT", self.team("task", "block", "T-1", code=2))

    def test_next_free_id_comes_from_the_store(self) -> None:
        self.team("task", "new", "one", "--class", "standard")
        # An event written elsewhere (another command, another origin) takes T-9: the next id is T-10, not T-2.
        done = subprocess.run([sys.executable, str(self.p.root / ".agents" / "bin" / "log"), "event", "add",
                               "task.created", "--actor", "lead", "--task", "T-9", "--data", '{"title": "nine"}'],
                              env=self.p.env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(self.team("task", "new", "ten", "--class", "standard").startswith("T-10 "))
        self.assertEqual([r.split(" | ")[0] for r in self.board_rows()], ["| T-1", "| T-9", "| T-10"])

    def test_task_new_preserves_existing_padding(self) -> None:
        for ids, expected in ((["T-0155"], "T-0156"), (["T-137", "T-0150"], "T-0151"),
                              (["T-0999"], "T-1000")):
            with self.subTest(ids=ids):
                project = Project()
                try:
                    for task in ids:
                        done = project.team("task", "new", "existing", "--class", "standard", "--id", task)
                        self.assertEqual(done.returncode, 0, done.stderr)
                        self.assertTrue(done.stdout.startswith(task + " "))
                    done = project.team("task", "new", "next", "--class", "standard")
                    self.assertEqual(done.returncode, 0, done.stderr)
                    self.assertTrue(done.stdout.startswith(expected + " "), done.stdout)
                finally:
                    project.close()

    def test_two_task_new_at_once_get_two_ids(self) -> None:
        command = [sys.executable, str(self.p.root / ".agents" / "bin" / "team"), "task", "new"]
        for round_ in range(3):
            runs = [subprocess.Popen([*command, f"race {round_} {i}", "--class", "standard"], env=self.p.env,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(2)]
            outs = [run.communicate(timeout=120) for run in runs]
            self.assertEqual([run.returncode for run in runs], [0, 0], outs)
        ids = [e["task"] for e in self.events("task.created")]
        self.assertEqual(sorted(ids, key=lambda t: int(t[2:])), [f"T-{n}" for n in range(1, 7)])
        self.assertEqual(len(self.board_rows()), 6)
        self.assertNotIn("edited by hand", "".join(o for o, _ in outs))

    def test_reservations_and_explicit_draft_registration(self) -> None:
        lead = self.team_dir / "env" / "lead"
        lead.mkdir(parents=True, exist_ok=True)
        (lead / "T-9.md").write_text("draft")
        self.assertTrue(self.team("task", "new", "draft", "--class", "standard", "--id", "T-9").startswith("T-9 "))
        self.assertIn("already", self.team("task", "new", "duplicate", "--class", "standard", "--id", "T-9", code=2))
        (lead / "T-12.md").write_text("draft")
        self.assertTrue(self.team("task", "new", "next", "--class", "standard").startswith("T-13 "))
        self.assertIn("dot", self.team("task", "new", "bad", "--class", "standard", "--id", "T.1", code=2))

    def classless(self, task: str = "T-5") -> None:
        # A task created before classes existed: its task.created event has a title and no class.
        done = subprocess.run([sys.executable, str(self.p.root / ".agents" / "bin" / "log"), "event", "add",
                               "task.created", "--actor", "lead", "--task", task, "--data", '{"title": "old one"}'],
                              env=self.p.env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)

    def snapshot_class(self, task: str):
        snap = json.loads(self.team("snapshot", "--json"))
        return next(t["class"] for t in snap["tasks"] if t["id"] == task)

    def test_old_store_without_a_class_reads_as_before(self) -> None:
        self.classless()
        self.assertIsNone(self.snapshot_class("T-5"))
        self.assertEqual(stats_classes(self.p), {})
        done = self.p.team("receipt", "need", "T-5")
        self.assertEqual(done.returncode, 1)
        self.assertIn("the class of T-5 is not known", done.stderr)
        self.assertTrue(self.team("task", "new", "next", "--class", "standard").startswith("T-6 "))

    def test_task_class_gives_a_classless_task_its_class_once(self) -> None:
        self.classless()
        before = len(self.events())
        out = self.team("task", "class", "T-5", "security")
        self.assertEqual(out.strip(), "T-5 class security")
        [event] = self.events()[before:]
        self.assertEqual((event["kind"], event["task"], event["actor"]), ("task.created", "T-5", "lead"))
        self.assertEqual(event["data"], {"class": "security"})
        self.assertEqual(self.team("receipt", "need", "T-5").strip(), "reviews 2")
        self.assertEqual(self.snapshot_class("T-5"), "security")
        self.assertEqual(stats_classes(self.p), {"T-5": "security"})
        self.assertIn("old one", self.board_rows()[0])
        # Once: the class is now set, the same command and any other class are refused.
        self.assertIn("T-5 already has the class security", self.team("task", "class", "T-5", "security", code=2))
        self.assertIn("already has the class security", self.team("task", "class", "T-5", "complex", code=2))
        self.assertEqual(len(self.events()), before + 1)

    def test_help_task_lists_task_class_in_one_line(self) -> None:
        lines = [ln for ln in self.team("help", "task").splitlines() if ln.startswith("  team task class ")]
        self.assertEqual(len(lines), 1)

    def test_task_class_refusals_write_nothing(self) -> None:
        self.classless()
        self.team("task", "new", "classed", "--class", "complex")
        before = len(self.events())
        self.assertIn("T-9 is not in the store", self.team("task", "class", "T-9", "standard", code=2))
        self.assertIn("T-6 already has the class complex", self.team("task", "class", "T-6", "security", code=2))
        self.assertIn("not one of", self.team("task", "class", "T-5", "urgent", code=2))
        # standard requires one review: mechanical (1) is equal, decision and analysis (0) are fewer.
        for lighter in ("decision", "analysis", "plan"):
            self.assertIn("fewer reviews", self.team("task", "class", "T-5", lighter, code=2))
        self.assertEqual(len(self.events()), before)
        self.assertIsNone(self.snapshot_class("T-5"))
        self.assertIn("usage", self.team("task", "class", "T-5", code=2))
        self.assertEqual(self.team("task", "class", "T-5", "mechanical").strip(), "T-5 class mechanical")

    def test_task_class_refuses_a_configured_class_with_no_review(self) -> None:
        self.classless()
        with (self.team_dir / "config.ini").open("a", encoding="utf-8") as handle:
            handle.write("\n[class.quick]\ntier = light\nsteps = worker\n")
        self.assertIn("fewer reviews", self.team("task", "class", "T-5", "quick", code=2))

    def test_classes_come_from_core_known_classes(self) -> None:
        self.assertFalse(hasattr(state, "CLASSES"))
        self.assertIs(state.core.known_classes, team_module().known_classes)
        self.assertIn("docs", self.team("task", "new", "x", "--class", "docs", code=2))
        with (self.team_dir / "config.ini").open("a", encoding="utf-8") as handle:
            handle.write("\n[class.docs]\ntier = strong\n")
        self.assertEqual(self.team("task", "new", "x", "--class", "docs").strip(), "T-1 todo (docs, owner lead)")


class SecretTest(StateTest):
    def test_a_key_in_a_title_is_refused_and_nothing_is_written(self) -> None:
        before = {name: self.file(name) for name in state.EXPORTS}
        for args in (("task", "new", f"rotate {FAKE_KEY}", "--class", "standard"),
                     ("decision", "add", "x", "--options", FAKE_KEY, "--why", "w", "--risk", "r", "--authority", "lead"),
                     ("schedule", "add", "2026-11-01T09:00:00+00:00", f"rotate {FAKE_KEY}", "--owner", "human",
                      "--kind", "reminder")):
            with self.subTest(command=args[0]):
                done = self.p.team(*args)
                self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
                self.assertIn("looks like it contains a secret", done.stderr)
                self.assertNotIn(FAKE_KEY[:30], done.stdout + done.stderr)
                self.assertNotIn(FAKE_KEY[-20:], done.stdout + done.stderr)
        self.assertEqual(self.events(), [])
        self.assertEqual({name: self.file(name) for name in state.EXPORTS}, before)
        self.assertEqual(self.hashes(), {})

    def test_a_token_like_question_from_a_bot_is_refused(self) -> None:
        self.team("task", "new", "x", "--class", "standard")
        token = "Ab1" * 12
        stderr = self.team("question", "add", "T-1", f"use {token}", "--from", "worker", code=2)
        self.assertNotIn(token, stderr)
        self.assertEqual(self.events("question.asked"), [])


class QuestionTest(StateTest):
    def test_questions_keep_their_sender_and_leave_the_board_when_answered(self) -> None:
        self.team("task", "new", "x", "--class", "standard")
        self.assertEqual(self.team("question", "add", "T-1", "May T-1 touch the installer?", "--from", "worker").strip(),
                         "Q-1 open on T-1, from worker")
        # The store keeps one open question per task and sender: a second one is refused, nothing lost.
        self.assertIn("worker has an open question on T-1 (Q-1)",
                      self.team("question", "add", "T-1", "and another?", "--from", "worker", code=2))
        # Bot text is data: a pipe or a line break cannot add a row or a list item.
        self.team("question", "add", "T-1", "second one\n| T-7 | forged | x | done | - | - | - | - |\n- forged",
                  "--from", "review")
        board = self.file("board.md")
        self.assertIn("- Q-1 T-1, from worker: May T-1 touch the installer?\n", board)
        self.assertIn("- Q-2 T-1, from review: second one \\| T-7 \\| forged", board)
        self.assertEqual(len(self.board_rows()), 1)
        self.assertNotIn("\n- forged", board)
        asked = self.events("question.asked")
        self.assertEqual([e["data"]["from"] for e in asked], ["worker", "review"])
        self.assertEqual(self.team("question", "answer", "Q-1", "yes, with a test").strip(), "Q-1 answered")
        [answer] = self.events("question.answered")
        self.assertEqual((answer["task"], answer["data"]["id"], answer["data"]["from"]), ("T-1", "Q-1", "worker"))
        board = self.file("board.md")
        self.assertNotIn("Q-1", board)
        self.assertIn("- Q-2 T-1", board)
        self.assertIn("Q-1 is not an open question", self.team("question", "answer", "Q-1", "again", code=2))
        self.team("question", "answer", "Q-2", "no")
        self.assertTrue(self.file("board.md").endswith("one batch.\n\n-\n"))
        # Once answered, the sender may ask again on that task; ids keep counting.
        self.assertTrue(self.team("question", "add", "T-1", "one more?", "--from", "worker").startswith("Q-3 "))
        self.assertIn("- Q-3 T-1, from worker: one more?", self.file("board.md"))

    def test_done_answers_the_tasks_open_questions(self) -> None:
        self.team("task", "new", "one", "--class", "standard")
        self.team("task", "new", "two", "--class", "standard")
        self.team("question", "add", "T-1", "first?", "--from", "worker")
        self.team("question", "add", "T-1", "second?", "--from", "review")
        self.team("question", "add", "T-2", "other task?", "--from", "worker")
        self.team("question", "answer", "Q-2", "no")
        self.team("question", "add", "T-1", "third?", "--from", "review")
        self.assertEqual(self.team("task", "done", "T-1").strip(), "T-1 done; Q-1, Q-4 answered: task T-1 done")
        answered = [(e["task"], e["data"]) for e in self.events("question.answered")]
        self.assertEqual(answered, [("T-1", {"id": "Q-2", "text": "no", "from": "review"}),
                                    ("T-1", {"id": "Q-1", "text": "task T-1 done", "from": "worker"}),
                                    ("T-1", {"id": "Q-4", "text": "task T-1 done", "from": "review"})])
        # One transaction (write_all): the answers and then the task event, committed together.
        self.assertEqual([e["kind"] for e in self.events()][-3:], ["question.answered", "question.answered",
                                                                    "task.state"])
        board = self.file("board.md")
        self.assertNotIn("first?", board)
        self.assertNotIn("third?", board)
        self.assertIn("- Q-3 T-2, from worker: other task?", board)
        context = self.team("context")
        self.assertNotIn("Q-1", context)
        self.assertNotIn("Q-4", context)
        self.assertIn("Q-3 T-2", context)
        # A task done again, with nothing open, says only its state.
        self.assertEqual(self.team("task", "done", "T-1").strip(), "T-1 done")

    def done_cut_after(self, task: str, written: int) -> subprocess.CompletedProcess:
        """team task done TASK whose write stops after `written` events, as a kill or a full disk would stop it; -1
        stops it before its commit, the pending file."""
        driver = ("import sys\n"
                  "sys.path.insert(0, sys.argv[1])\n"
                  "from teamlib import state\n"
                  "log = state.store()\n"
                  "real, left = log.event_append, [int(sys.argv[3])]\n"
                  "def append(*args):\n"
                  "    if not left[0]:\n"
                  "        raise OSError('No space left on device')\n"
                  "    left[0] -= 1\n"
                  "    return real(*args)\n"
                  "def commit(*args):\n"
                  "    raise OSError('No space left on device')\n"
                  "log.event_append = append\n"
                  "if left[0] < 0:\n"
                  "    state.write_json = commit\n"
                  "sys.exit(state.do_task(['done', sys.argv[2]]))\n")
        return subprocess.run([sys.executable, "-c", driver, str(self.p.root / ".agents" / "bin"), task, str(written)],
                              env=self.p.env, capture_output=True, text=True, timeout=300, cwd=self.p.root)

    def test_an_interrupted_done_is_all_or_nothing(self) -> None:
        pending = self.team_dir / "env" / ".state.pending.json"
        self.team("task", "new", "spare", "--class", "standard")
        for written in (-1, 0, 1, 2):
            with self.subTest(written=written):
                task = f"T-{written + 3}"
                self.team("task", "new", task, "--class", "standard")
                self.team("question", "add", task, "first?", "--from", "worker")
                self.team("question", "add", task, "second?", "--from", "review")
                before = self.events()
                cut = self.done_cut_after(task, written)
                self.assertNotEqual(cut.returncode, 0, cut.stdout)
                context = self.team("context")
                if written < 0:
                    # Cut before its commit: nothing was written, the task is open with both questions.
                    self.assertEqual((self.events(), pending.exists()), (before, False))
                    self.assertIn(f"| {task} |", context)
                    self.assertEqual(context.count(f" {task}, from"), 2, context)
                    continue
                # Committed: every reader sees the whole of it, the task done and neither question open.
                self.assertTrue(pending.exists())
                self.assertEqual(len(self.events()), len(before) + written)
                self.assertNotIn(f"| {task} |", context)
                self.assertNotIn(f" {task}, from", context)
                # The next state command writes the rest, once, before its own event.
                self.team("task", "start", "T-1")
                self.assertFalse(pending.exists())
                self.assertEqual([(e["kind"], e["task"]) for e in self.events()[len(before):]],
                                 [("question.answered", task), ("question.answered", task), ("task.state", task),
                                  ("task.state", "T-1")])
                self.assertEqual([e["data"]["text"] for e in self.events("question.answered")[-2:]],
                                 [f"task {task} done"] * 2)
                self.assertIn("| done |", next(r for r in self.board_rows() if r.startswith(f"| {task} |")))

    def test_a_reader_waits_for_a_done_being_written(self) -> None:
        # A done paused after its first event holds the state lock: team context waits for it, then sees all of it.
        task = re.search(r"T-[0-9]+", self.team("task", "new", "read while done", "--class", "standard"))[0]
        self.team("question", "add", task, "first?", "--from", "worker")
        self.team("question", "add", task, "second?", "--from", "review")
        flag, go = Path(self.p.tmp.name) / "half", Path(self.p.tmp.name) / "go"
        driver = ("import sys, time\n"
                  "from pathlib import Path\n"
                  "sys.path.insert(0, sys.argv[1])\n"
                  "from teamlib import state\n"
                  "log = state.store()\n"
                  "real, flag, go = log.event_append, Path(sys.argv[3]), Path(sys.argv[4])\n"
                  "def append(*args):\n"
                  "    out = real(*args)\n"
                  "    if not flag.exists():\n"
                  "        flag.write_text('half')\n"
                  "        while not go.exists():\n"
                  "            time.sleep(0.05)\n"
                  "    return out\n"
                  "log.event_append = append\n"
                  "sys.exit(state.do_task(['done', sys.argv[2]]))\n")
        pipes = {"env": self.p.env, "cwd": self.p.root, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
                 "text": True}
        writer = subprocess.Popen([sys.executable, "-c", driver, str(self.p.root / ".agents" / "bin"), task,
                                   str(flag), str(go)], **pipes)
        self.addCleanup(writer.kill)
        self.addCleanup(go.touch)
        deadline = time.monotonic() + 120
        while not flag.exists() and writer.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(flag.exists(), writer.communicate()[1] if writer.poll() is not None else "no first event")
        reader = subprocess.Popen([sys.executable, str(self.p.root / ".agents" / "bin" / "team"), "context"], **pipes)
        self.addCleanup(reader.kill)
        with self.assertRaises(subprocess.TimeoutExpired):
            reader.wait(timeout=3)
        go.touch()
        out, err = reader.communicate(timeout=300)
        self.assertEqual(reader.returncode, 0, err)
        self.assertEqual(writer.communicate(timeout=300)[0].strip(), f"{task} done; Q-1, Q-2 answered: task {task} done")
        self.assertNotIn(f"| {task} |", out)
        self.assertNotIn(f" {task}, from", out)

    def test_reads_come_from_the_projection_not_the_event_files(self) -> None:
        source = (SOURCE / "bin" / "teamlib" / "state.py").read_text(encoding="utf-8")
        self.assertNotIn("events_dir", source)
        self.assertNotIn("event_parse", source)


class DecisionTest(StateTest):
    def records(self) -> list[list[str]]:
        text = self.file("decisions.md").split("```\n", 2)[-1]
        return [block.strip("\n").split("\n") for block in text.split("\n\n") if block.strip()]

    def test_records_in_todays_format_newest_first(self) -> None:
        self.team("task", "new", "x", "--class", "standard")
        self.assertEqual(self.team("decision", "add", "Use SQLite as cache", "--options", "SQLite", "flat files",
                                   "--why", "fast reads", "--risk", "cache drift", "--authority", "owner",
                                   "--task", "T-1").strip(), "D-1 pending (human)")
        self.team("decision", "add", "Retry once", "--options", "retry", "fail", "--why", "flaky network",
                  "--risk", "a slow run", "--authority", "lead")
        self.team("decision", "add", "Split the module", "--options", "split", "keep", "--why", "size",
                  "--risk", "churn", "--authority", "decide", "--next", "worker, gate")
        today = self.events()[-1]["t"][:10]
        first, second, third = self.records()
        self.assertEqual(first, [f"D-3 {today} - — Split the module", "  Options: split / keep", "  Why: size",
                                 "  Risk: churn", "  Authority: decide bot within the rules (autonomy = auto)",
                                 "  Next: worker, gate"])
        self.assertEqual(second[4], "  Authority: standing yes lead")
        self.assertEqual(third, [f"D-1 {today} T-1 — Use SQLite as cache", "  Options: SQLite / flat files",
                                 "  Why: fast reads", "  Risk: cache drift", "  Authority: human",
                                 "  Next: human, approve or reject", "  Status: pending"])
        for block in self.records():
            self.assertRegex(block[0], DECISION)
            for line in block[1:]:
                self.assertRegex(line, RECORD_LINE)
        self.assertTrue(self.file("decisions.md").startswith(state.DECISIONS_HEAD))

    def test_approve_and_reject_only_a_pending_decision(self) -> None:
        add = ("--options", "a", "b", "--why", "w", "--risk", "r")
        self.team("decision", "add", "ask the human", *add, "--authority", "owner")
        self.team("decision", "add", "lead's call", *add, "--authority", "lead")
        self.team("decision", "add", "ask again", *add, "--authority", "owner")
        self.assertIn("D-2 is not a pending decision (decided)", self.team("decision", "approve", "D-2", code=2))
        self.assertIn("D-9 is not a pending decision", self.team("decision", "reject", "D-9", code=2))
        self.assertEqual(self.team("decision", "approve", "D-1").strip(), "D-1 approved by human")
        self.assertIn("not a pending decision (approved)", self.team("decision", "reject", "D-1", code=2))
        self.team("decision", "reject", "D-3", "--note", "too risky now")
        last = self.events("decision.recorded")[-1]["data"]
        self.assertEqual((last["number"], last["status"], last["decided_by"], last["note"]),
                         (3, "rejected", "human", "too risky now"))
        statuses = [block[-1] for block in self.records()]
        self.assertEqual(statuses, ["  Status: rejected by human; note: too risky now", "  Next: lead, -",
                                    "  Status: approved by human"])
        # The approved record keeps its title and reasons; the store's row keeps its search text.
        self.assertIn("ask the human", self.file("decisions.md"))


class MergeTest(StateTest):
    def flow(self, task: str, **fields) -> None:
        folder = self.team_dir / "flows" / task
        folder.mkdir(parents=True, exist_ok=True)
        flow = {"task": task, "branch": f"wouf/{task}", "class": "standard", "status": "ready", "head": HEAD,
                "steps": [{"kind": "worker"}, {"kind": "gate", "outcome": "green", "head": fields.get("head", HEAD)},
                          {"kind": "review", "index": 1, "verdict": "approve", "head": fields.get("head", HEAD)}], **fields}
        (folder / "state.json").write_text(json.dumps(flow))

    def test_propose_records_head_branch_and_verdict_and_never_merges(self) -> None:
        branches = self.p.git("branch", "--list")
        self.team("task", "new", "x", "--class", "standard")
        self.assertIn("no flow", self.team("merge", "propose", "T-1", "--head", HEAD, code=2))
        self.flow("T-1", status="blocked")
        self.assertIn("has not ended ready (blocked)", self.team("merge", "propose", "T-1", "--head", HEAD, code=2))
        self.flow("T-1", head=None)
        self.assertIn("without commits", self.team("merge", "propose", "T-1", "--head", HEAD, code=2))
        self.flow("T-1")
        self.assertIn("is not the head of the flow", self.team("merge", "propose", "T-1", "--head", OTHER, code=2))
        out = self.team("merge", "propose", "T-1", "--head", HEAD[:12])
        self.assertEqual(out.strip(), f"T-1 proposed: wouf/T-1 {HEAD[:12]}, review approve; nothing merged")
        [event] = self.events("merge.proposed")
        self.assertEqual(event["data"], {"task": "T-1", "head": HEAD, "branch": "wouf/T-1", "verdict": "approve"})
        self.assertEqual(self.p.git("branch", "--list"), branches)

    def test_manual_review_can_propose_current_reviewed_branch(self) -> None:
        self.team("task", "new", "x", "--class", "standard")
        self.p.git("branch", "wouf/T-1")
        head = self.p.git("rev-parse", "wouf/T-1").strip()
        stage = self.team_dir / "env" / "review" / "tasks" / "T-1"
        stage.mkdir(parents=True)
        (stage / "run.json").write_text(json.dumps({"name": "Reviewer", "role": "review", "repo": "wouf/T-1", "commit": head}))
        (stage / "result.json").write_text(json.dumps({"state": "done"}))
        (stage / "output.log").write_text(f"Reviewer · T-1 · APPROVE {head}\n")
        self.assertIn("review approve", self.team("merge", "propose", "T-1", "--head", head))
        (stage / "output.log").write_text(f"Reviewer · T-1 · APPROVE {OTHER}\n")
        self.team("merge", "propose", "T-1", "--head", head, code=2)

    def test_manual_multi_review_explicitly_refused(self) -> None:
        self.team("task", "new", "x", "--class", "security")
        out = self.team("merge", "propose", "T-1", "--head", HEAD, code=2)
        self.assertIn("manual delivery does not support multiple reviews", out)
        self.assertEqual(self.events("merge.proposed"), [])

    def test_proposal_rejects_stale_or_incomplete_approval(self) -> None:
        self.team("task", "new", "x", "--class", "standard")
        for steps in ([{"kind": "gate", "outcome": "green", "head": HEAD},
                       {"kind": "review", "index": 1, "verdict": "approve", "head": OTHER}],
                      [{"kind": "gate", "outcome": "red", "head": HEAD},
                       {"kind": "review", "index": 1, "verdict": "approve", "head": HEAD}],
                      [{"kind": "gate", "outcome": "green", "head": HEAD}]):
            self.flow("T-1", steps=steps)
            self.team("merge", "propose", "T-1", "--head", HEAD, code=2)
        self.assertEqual(self.events("merge.proposed"), [])

    def test_mark_is_refused_for_a_head_other_than_the_proposal(self) -> None:
        self.team("task", "new", "x", "--class", "standard")
        self.assertIn("no merge proposal", self.team("merge", "mark", "T-1", "--head", HEAD, code=2))
        self.flow("T-1")
        self.team("merge", "propose", "T-1", "--head", HEAD)
        self.assertIn("is not the proposed head", self.team("merge", "mark", "T-1", "--head", OTHER, code=2))
        self.assertIn("lowercase hex", self.team("merge", "mark", "T-1", "--head", "main", code=2))
        # A newer proposal wins: the flow ran again and ended at another head.
        self.flow("T-1", head=OTHER)
        self.team("merge", "propose", "T-1", "--head", OTHER)
        self.assertIn("is not the proposed head", self.team("merge", "mark", "T-1", "--head", HEAD, code=2))
        self.assertEqual(self.team("merge", "mark", "T-1", "--head", OTHER).strip(),
                         f"T-1 {OTHER[:12]} marked for the owner; the human merges")
        [event] = self.events("merge.marked")
        self.assertEqual(event["data"], {"task": "T-1", "head": OTHER, "branch": "wouf/T-1", "verdict": "approve"})


class ScheduleTest(StateTest):
    def test_add_and_done_in_the_table_context_reads(self) -> None:
        soon = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat(timespec="seconds")
        self.assertEqual(self.team("schedule", "add", soon, "ship the release", "--owner", "worker",
                                   "--kind", "deadline").strip(), f"S-1 deadline {soon}, owner worker")
        self.team("schedule", "add", "0 9 * * 1", "weekly digest", "--owner", "scout", "--kind", "routine",
                  "--ending", "draft")
        self.assertIn("needs an ending", self.team("schedule", "add", "0 9 * * 2", "x", "--owner", "scout",
                                                   "--kind", "routine", code=2))
        self.assertIn("ISO time with time zone", self.team("schedule", "add", "next Friday", "x", "--owner", "human",
                                                           "--kind", "reminder", code=2))
        self.assertIn("ISO time with time zone", self.team("schedule", "add", "2026-11-01T09:00", "x", "--owner",
                                                           "human", "--kind", "reminder", code=2))
        rows = [ln for ln in self.file("schedule.md").splitlines() if ln.startswith("| ") and "When" not in ln]
        self.assertEqual(rows, [f"| {soon} | ship the release | worker | deadline | | |",
                                "| 0 9 * * 1 | weekly digest | scout | routine | draft | |"])
        event = self.events("schedule.item")[0]["data"]
        self.assertEqual(event, {"id": "S-1", "title": "ship the release", "when": soon, "owner": "worker",
                                 "kind": "deadline", "status": "open"})
        context = self.team("context")
        self.assertRegex(context, r"due.*ship the release")
        self.assertRegex(context, r"routine 0 9 \* \* 1: weekly digest")
        self.assertEqual(self.team("schedule", "done", "S-1").strip(), "S-1 done")
        self.assertNotIn("ship the release", self.file("schedule.md"))
        self.assertIn("S-1 is not an open schedule entry", self.team("schedule", "done", "S-1", code=2))
        self.team("schedule", "done", "S-2")
        self.assertEqual(self.file("schedule.md"), state.render_schedule(state.EMPTY))


class FormatTest(StateTest):
    def test_the_empty_store_renders_the_templates_byte_for_byte(self) -> None:
        for name, render in state.RENDER.items():
            with self.subTest(name=name):
                self.assertEqual(render(state.EMPTY).encode("utf-8"), (SOURCE / "team" / name).read_bytes())

    CONTEXT_BOARD = ("| T-1 | old thing | worker | done | - | - | - | 2026-10-20 |\n"
                     "| T-2 | new thing | worker | working | review | - | - | 2026-10-25 |")

    def context_board(self) -> None:
        """The board of test_team_cli.ContextTest, kept by hand."""
        board = self.team_dir / "board.md"
        board.write_text(board.read_text().replace("| | | | | | | | |", self.CONTEXT_BOARD).replace(
            "one batch.\n\n-", "one batch.\n\n- May T-2 touch the installer?"))

    def test_a_context_test_board_parses_the_same_after_import(self) -> None:
        self.context_board()
        by_hand = self.team("context")
        listed = self.team("state", "import", str(self.team_dir / "board.md"))
        self.assertIn("+ T-1 | old thing | done", listed)
        self.assertIn("+ Q-1 -, from lead: May T-2 touch the installer?", listed)
        self.assertEqual(self.events(), [])
        # The store generates a row's Updated cell: a new row typed by hand leaves it blank, and the import keeps its dates.
        board = self.team_dir / "board.md"
        board.write_text(re.sub(r"\| 2026-10-2[0-9] \|\n", "| - |\n", board.read_text()))
        self.assertIn("regenerated from the store", self.team("state", "import", str(board), "--apply"))
        today = self.events()[-1]["t"][:10]
        self.assertEqual(self.board_rows(), [re.sub(r"2026-10-2\d", today, row)
                                             for row in self.CONTEXT_BOARD.splitlines()])
        generated = self.team("context")
        rows = lambda text: [ln for ln in text.split("## Open on the board\n", 1)[1].splitlines()
                             if ln.startswith("| ")]
        self.assertEqual([re.sub(r"\d{4}-\d\d-\d\d", "D", r) for r in rows(generated)],
                         [re.sub(r"\d{4}-\d\d-\d\d", "D", r) for r in rows(by_hand)])
        self.assertIn("May T-2 touch the installer?", generated)
        self.assertNotIn("old thing", generated)
        # Imported once: listed again, nothing differs.
        self.assertIn("board.md: 0 events to write", self.team("state", "import", str(self.team_dir / "board.md")))


class HandEditTest(StateTest):
    def test_a_hand_edit_is_detected_not_overwritten_then_imported(self) -> None:
        self.team("task", "new", "first", "--class", "standard", "--owner", "worker")
        board = self.team_dir / "board.md"
        edited = board.read_text().replace("| worker | todo |", "| worker | review |").replace(
            "one batch.\n\n-", "one batch.\n\n- Can the human check the copy?")
        board.write_text(edited)
        out = self.team("task", "new", "second", "--class", "standard")
        self.assertEqual(len(out.splitlines()), 1)
        self.assertIn("T-2 todo", out)
        self.assertIn(f"board.md was edited by hand and not overwritten: team state import {board}", out)
        self.assertEqual(board.read_text(), edited)
        self.assertEqual(self.events()[-1]["task"], "T-2")
        # The other exports are still regenerated.
        self.assertNotIn("decisions.md was edited", out)
        listed = self.team("state", "import", str(board))
        self.assertIn('~ T-1 state "todo" -> "review"', listed)
        self.assertIn("+ Q-1 -, from lead: Can the human check the copy?", listed)
        self.assertIn("? T-2 only in the store; it stays", listed)
        self.assertEqual(len(self.events()), 2)
        self.assertIn("1 other lines", listed)  # The absent store row would be restored.
        self.team("state", "import", str(board), "--apply", "--force")
        self.assertEqual([e["data"].get("state") for e in self.events("task.state")], ["review"])
        self.assertEqual([r.split(" | ")[3] for r in self.board_rows()], ["review", "todo"])
        self.assertIn("- Q-1, from lead: Can the human check the copy?", board.read_text())
        self.assertEqual(self.hashes()["board.md"], hashlib.sha256(board.read_bytes()).hexdigest())
        self.assertNotIn("edited by hand", self.team("task", "start", "T-2"))

    def test_unknown_or_refused_lines_keep_the_file_and_write_nothing(self) -> None:
        self.team("task", "new", "first", "--class", "standard")
        board = self.team_dir / "board.md"
        board.write_text(board.read_text().replace(
            "| T-1 |", f"| bad id! | x | lead | todo | - | - | - | - |\n| T-5 | rotate {FAKE_KEY} | lead | todo "
                       "| - | - | - | - |\n| T-6 | x | Bad Owner | todo | - | - | - | - |\n| T-7 | x | lead | late "
                       "| - | - | - | - |\n| T-1 |") + "\nA note the lead wrote.\n")
        kept = board.read_text()
        done = self.p.team("state", "import", str(board), "--apply")
        self.assertEqual(done.returncode, 2)
        self.assertIn("4 lines refused, 6 other lines; nothing written", done.stderr)
        self.assertIn("looks like it contains a secret", done.stdout)
        self.assertIn("member id", done.stdout)
        self.assertIn("status 'late'", done.stdout)
        self.assertNotIn(FAKE_KEY[:30], done.stdout + done.stderr)
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(board.read_text(), kept)

    def test_an_edited_question_is_imported_as_its_new_text(self) -> None:
        self.team("task", "new", "x", "--class", "standard")
        self.team("question", "add", "T-1", "May T-1 touch the installer?", "--from", "worker")
        board = self.team_dir / "board.md"
        board.write_text(board.read_text().replace("touch the installer?", "touch the installer and the docs?"))
        listed = self.team("state", "import", str(board))
        self.assertIn("~ Q-1 text", listed)
        self.assertIn("board.md: 1 events to write", listed)
        self.team("state", "import", str(board), "--apply")
        last = self.events("question.asked")[-1]
        self.assertEqual((last["task"], last["data"]),
                         ("T-1", {"id": "Q-1", "text": "May T-1 touch the installer and the docs?", "from": "worker"}))
        self.assertIn("- Q-1 T-1, from worker: May T-1 touch the installer and the docs?\n", board.read_text())
        self.assertIn("board.md: 0 events to write", self.team("state", "import", str(board)))
        # A known question moved to another sender or task is refused, not silently re-keyed.
        board.write_text(board.read_text().replace("from worker", "from review"))
        done = self.p.team("state", "import", str(board), "--apply")
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn("Q-1 is on T-1 from worker", done.stdout)
        # Its edited text is checked like any other field.
        board.write_text(board.read_text().replace("from review: May", f"from worker: {FAKE_KEY} May"))
        done = self.p.team("state", "import", str(board), "--apply")
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn("looks like it contains a secret", done.stdout)
        self.assertNotIn(FAKE_KEY[:30], done.stdout + done.stderr)

    def test_structural_line_edits_are_refused_and_force_names_each_discard(self) -> None:
        self.team("task", "new", "first", "--class", "standard")
        board = self.team_dir / "board.md"
        original = board.read_text()
        edits = {
            "separator": original.replace("|---|", "|:---|", 1),
            "heading": original.replace("# Board", "# My board", 1),
            "trailing spaces": original.replace("# Board\n", "# Board  \n", 1),
            "extra blank": original.replace("# Board\n", "# Board\n\n", 1),
        }
        for label, edited in edits.items():
            with self.subTest(label=label):
                board.write_text(edited)
                before = self.events()
                listed = self.team("state", "import", str(board))
                self.assertIn("1 other lines", listed)
                self.assertIn(f"not imported ({PROSE}): text", listed)
                refused = self.p.team("state", "import", str(board), "--apply")
                self.assertEqual(refused.returncode, 2)
                self.assertEqual(self.events(), before)
                self.assertEqual(board.read_text(), edited)
                forced = self.team("state", "import", str(board), "--apply", "--force")
                self.assertIn("--force discards, not written: line", forced)
                self.assertEqual(board.read_text(), original)
        self.assertIn("0 events to write, 0 lines refused, 0 other lines",
                      self.team("state", "import", str(board)))

    def test_byte_edits_are_refused_and_force_names_each_discard(self) -> None:
        self.team("task", "new", "first", "--class", "standard")
        board = self.team_dir / "board.md"
        original = board.read_bytes()
        edits = {
            "missing final newline": (original[:-1], "no final newline", 1),
            "extra final newline": (original + b"\n", "(absent)", 1),
            "all CRLF": (original.replace(b"\n", b"\r\n"), "CRLF", original.count(b"\n")),
            "one CRLF": (original.replace(b"\n", b"\r\n", 1), "CRLF", 1),
            "trailing spaces": (original.replace(b"# Board\n", b"# Board  \n"), "# Board  ", 1),
        }
        for label, (edited, detail, count) in edits.items():
            with self.subTest(label=label):
                board.write_bytes(edited)
                before = self.events()
                listed = self.team("state", "import", str(board))
                self.assertIn(f"{count} other lines", listed)
                self.assertIn(detail, listed)
                refused = self.p.team("state", "import", str(board), "--apply")
                self.assertEqual(refused.returncode, 2)
                self.assertEqual(self.events(), before)
                self.assertEqual(board.read_bytes(), edited)
                forced = self.team("state", "import", str(board), "--apply", "--force")
                self.assertEqual(forced.count("--force discards, not written: line"), count)
                self.assertIn(detail, forced)
                self.assertEqual(board.read_bytes(), original)
        before = self.events()
        self.assertIn("0 events written, 0 lines refused, 0 other lines",
                      self.team("state", "import", str(board), "--apply"))
        self.assertEqual(self.events(), before)
        self.assertEqual(board.read_bytes(), original)

    def test_imported_field_does_not_hide_structural_edits_on_its_line(self) -> None:
        self.team("task", "new", "first", "--class", "standard")
        board = self.team_dir / "board.md"
        board.write_text(board.read_text().replace("| first |", "| renamed | "))
        edited = board.read_text()
        before = self.events()
        listed = self.team("state", "import", str(board))
        self.assertIn('title "first" -> "renamed"', listed)
        self.assertIn("1 other lines", listed)
        done = self.p.team("state", "import", str(board), "--apply")
        self.assertEqual(done.returncode, 2)
        self.assertEqual(self.events(), before)
        self.assertEqual(board.read_text(), edited)
        forced = self.team("state", "import", str(board), "--apply", "--force")
        self.assertIn("--force discards, not written: line", forced)
        self.assertIn("| renamed | lead |", board.read_text())

    def test_an_edit_only_in_updated_is_listed_and_refused(self) -> None:
        self.team("task", "new", "first", "--class", "standard")
        board = self.team_dir / "board.md"
        edited = re.sub(r"\| ([0-9]{4}-[0-9]{2}-[0-9]{2}) \|\n", "| 2001-01-01 |\n", board.read_text(), count=1)
        self.assertNotEqual(edited, board.read_text())
        board.write_text(edited)
        listed = self.team("state", "import", str(board))
        self.assertRegex(listed, r'! line [0-9]+ not imported \(derived: the store sets it to the day of T-1\'s latest '
                                 r'event\): Updated of T-1 "[0-9]{4}-[0-9]{2}-[0-9]{2}" -> "2001-01-01"\n')
        self.assertIn("board.md: 0 events to write, 0 lines refused, 1 other lines", listed)
        done = self.p.team("state", "import", str(board), "--apply")
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn("nothing written", done.stderr)
        self.assertEqual(board.read_text(), edited)
        self.assertEqual(len(self.events()), 1)

    def test_edited_template_prose_is_listed_with_old_and_new_and_refused(self) -> None:
        self.team("task", "new", "first", "--class", "standard")
        board = self.team_dir / "board.md"
        board.write_text(board.read_text().replace("Nothing is done until this file says so.",
                                                   "Done when the lead says so."))
        edited = board.read_text()
        listed = self.team("state", "import", str(board))
        self.assertIn(f'not imported ({PROSE}): text "The team\'s state. Nothing is done until this file says so.',
                      listed)
        self.assertIn("Done when the lead says so.", listed)
        self.assertIn("board.md: 0 events to write, 0 lines refused, 1 other lines", listed)
        done = self.p.team("state", "import", str(board), "--apply")
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertEqual(board.read_text(), edited)
        self.assertEqual(len(self.events()), 1)

    def test_force_discards_generated_text_edits_and_says_what(self) -> None:
        self.team("task", "new", "first", "--class", "standard")
        board = self.team_dir / "board.md"
        today = self.events()[-1]["t"][:10]
        edited = re.sub(r"\| ([0-9]{4}-[0-9]{2}-[0-9]{2}) \|\n", "| 2001-01-01 |\n", board.read_text(), count=1)
        edited = edited.replace("| T-1 | first | lead | todo |", "| T-1 | first | lead | review |")
        board.write_text(edited)
        self.assertEqual(self.p.team("state", "import", str(board), "--apply").returncode, 2)
        self.assertEqual(self.p.team("state", "import", str(board), "--force").returncode, 2)
        forced = self.team("state", "import", str(board), "--apply", "--force")
        self.assertIn("1 events written", forced)
        self.assertIn('--force discards, not written: line ', forced)
        self.assertIn(f'--force discards, not written: line 7 ({UPDATED}) Updated of T-1 "{today}" -> "2001-01-01"',
                      forced)
        self.assertEqual(self.events("task.state")[-1]["data"]["state"], "review")
        self.assertNotIn("2001-01-01", self.file("board.md"))
        self.assertIn("| review |", self.file("board.md"))
        self.assertEqual(self.hashes()["board.md"], hashlib.sha256(board.read_bytes()).hexdigest())

    def test_every_changed_cell_is_listed_old_to_new_and_masked(self) -> None:
        self.team("task", "new", "first", "--class", "standard", "--owner", "worker")
        self.team("task", "start", "T-1", "--gate", "deploy-zz9", "--artifacts", "out/a.md")
        self.team("question", "add", "T-1", "May T-1 touch the installer?", "--from", "worker")
        board = self.team_dir / "board.md"
        board.write_text(board.read_text().replace(
            "| T-1 | first | worker | working | deploy-zz9 | out/a.md | - |",
            "| T-1 | renamed | review | blocked | release | out/b.md | waits for the URL |").replace(
            "- Q-1 T-1, from worker: May T-1 touch the installer?\n", "-\n"))
        # A value stored later as a secret is masked on the old side too.
        self.p.store_secret("STAGE_NAME", "deploy-zz9")
        listed = self.team("state", "import", str(board))
        self.assertIn('~ T-1 title "first" -> "renamed", state "working" -> "blocked", owner "worker" -> "review", '
                      'next_gate "***" -> "release", artifacts "out/a.md" -> "out/b.md", '
                      'blocked_reason "" -> "waits for the URL"\n', listed)
        self.assertIn('- Q-1 "May T-1 touch the installer?" removed from the board: answered\n', listed)
        self.assertNotIn("deploy-zz9", listed)
        self.assertIn("board.md: 3 events to write, 0 lines refused, 0 other lines", listed)

    def test_a_row_older_than_a_later_command_shows_what_it_would_undo(self) -> None:
        self.team("task", "new", "first", "--class", "standard")
        board = self.team_dir / "board.md"
        board.write_text(board.read_text().replace("| first |", "| first, edited |"))
        self.team("task", "start", "T-1")
        listed = self.team("state", "import", str(board))
        self.assertIn('~ T-1 title "first" -> "first, edited", state "working" -> "todo"\n', listed)

    def test_an_updated_edit_on_a_changed_row_is_its_own_not_imported_line(self) -> None:
        self.team("task", "new", "first", "--class", "standard")
        today = self.events()[-1]["t"][:10]
        board = self.team_dir / "board.md"
        board.write_text(board.read_text().replace(f"| lead | todo | - | - | - | {today} |",
                                                   "| lead | review | - | - | - | 2001-01-01 |"))
        listed = self.team("state", "import", str(board)).splitlines()
        self.assertIn('~ T-1 state "todo" -> "review"', listed)
        self.assertIn(f'! line 7 not imported ({UPDATED}): Updated of T-1 "{today}" -> "2001-01-01"', listed)
        self.assertEqual([ln for ln in listed if "2001-01-01" in ln and not ln.startswith("! ")], [])
        self.assertIn("board.md: 1 events to write, 0 lines refused, 1 other lines", listed[-1])

    def test_deleted_moved_or_added_prose_is_listed_as_template_prose(self) -> None:
        self.team("task", "new", "first", "--class", "standard")
        board = self.team_dir / "board.md"
        original = board.read_text()
        status = "Status: `todo`, `working`, `blocked`, `review`, `done`."
        edits = {
            "deleted": (original.replace(status + "\n", ""), f'text "{status}" -> (absent)'),
            "moved": (original.replace(status + "\n", "").replace("# Board\n", f"# Board\n{status}\n"),
                      f'text (absent) -> "{status}"'),
            "added": (original + "A note the lead wrote.\n", 'text (absent) -> "A note the lead wrote."'),
        }
        for label, (edited, detail) in edits.items():
            with self.subTest(label=label):
                board.write_text(edited)
                listed = self.team("state", "import", str(board))
                self.assertRegex(listed, rf"! line [0-9]+ not imported \({re.escape(PROSE)}\): {re.escape(detail)}")
                self.assertNotIn("edited by hand, not imported", listed)
                done = self.p.team("state", "import", str(board), "--apply")
                self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
                self.assertEqual(board.read_text(), edited)
                forced = self.team("state", "import", str(board), "--apply", "--force")
                self.assertIn(f"({PROSE}) {detail}", forced)
                self.assertEqual(board.read_text(), original)

    def test_an_export_reset_to_the_template_is_a_hand_edit(self) -> None:
        self.team("task", "new", "first", "--class", "standard")
        board = self.team_dir / "board.md"
        template = state.render_board(state.EMPTY)
        board.write_text(template)
        out = self.team("task", "new", "second", "--class", "standard")
        self.assertIn("board.md was edited by hand and not overwritten", out)
        self.assertEqual(board.read_text(), template)
        listed = self.team("state", "import", str(board))
        self.assertIn("? T-1 only in the store; it stays", listed)

    def test_an_untouched_schedule_with_twin_rows_is_no_hand_edit(self) -> None:
        for _ in range(2):
            self.team("schedule", "add", "2026-11-01T09:00:00+00:00", "pay invoice", "--owner", "human",
                      "--kind", "reminder")
        self.team("schedule", "add", "2026-11-02T09:00:00+00:00", "call back", "--owner", "human", "--kind", "reminder")
        schedule = self.team_dir / "schedule.md"
        self.assertNotIn("edited by hand", self.team("schedule", "add", "2026-11-03T09:00:00+00:00", "x",
                                                     "--owner", "human", "--kind", "reminder"))
        self.assertEqual(self.team("state", "diff", str(schedule)), "schedule.md: the same as the store\n")
        twin = "| 2026-11-01T09:00:00+00:00 | pay invoice | human | reminder | | |\n"
        self.assertEqual(schedule.read_text().count(twin), 2)
        schedule.write_text(schedule.read_text().replace(twin + twin, twin))
        self.assertIn("+| 2026-11-01T09:00:00+00:00 | pay invoice", self.team("state", "diff", str(schedule)))

    def test_a_file_equal_to_the_projection_is_no_hand_edit(self) -> None:
        # The hashes are local (team/env is not in git): after a clone or a pull the file equals the projection.
        self.team("task", "new", "first", "--class", "standard")
        (self.team_dir / "env" / "state-exports.json").unlink()
        self.assertNotIn("edited by hand", self.team("task", "start", "T-1"))
        self.assertIn("| working |", self.board_rows()[0])

    def test_decisions_and_schedule_hand_edits_are_refused_not_imported(self) -> None:
        self.team("decision", "add", "ask", "--options", "a", "b", "--why", "w", "--risk", "r", "--authority", "owner")
        self.team("schedule", "add", "2026-11-01T09:00:00+00:00", "pay invoice", "--owner", "human",
                  "--kind", "reminder")
        decisions, schedule = self.team_dir / "decisions.md", self.team_dir / "schedule.md"
        decisions.write_text(decisions.read_text().replace("  Status: pending", "  Status: approved by human"))
        schedule.write_text(schedule.read_text().replace(
            "| 2026-11-01T09:00:00+00:00 | pay invoice | human | reminder | | |",
            "| 0 8 * * 5 | weekly review | lead | routine | standing-yes | |"))
        out = self.team("task", "new", "x", "--class", "standard")
        self.assertIn("decisions.md was edited by hand and not overwritten: team state diff", out)
        self.assertIn("schedule.md was edited by hand and not overwritten: team state diff", out)
        events = len(self.events())
        for name in ("decisions.md", "schedule.md"):
            with self.subTest(name=name):
                done = self.p.team("state", "import", str(self.team_dir / name), "--apply")
                self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
                self.assertIn(f"{name} is not imported", done.stderr)
        self.assertEqual(len(self.events()), events)
        self.assertIn("  Status: approved by human", decisions.read_text())
        self.assertIn("| 0 8 * * 5 | weekly review |", schedule.read_text())
        diff = self.team("state", "diff", str(decisions))
        self.assertIn("-  Status: approved by human", diff)
        self.assertIn("+  Status: pending", diff)
        self.assertIn("+| 2026-11-01T09:00:00+00:00 | pay invoice", self.team("state", "diff", str(schedule)))

    def test_invalid_utf8_exports_are_preserved_diffed_and_never_imported(self) -> None:
        self.team("task", "new", "initial", "--class", "standard")
        for name in state.EXPORTS:
            with self.subTest(export=name):
                path = self.team_dir / name
                original = path.read_bytes()
                edited = original + b"\xff\x00\x1b"
                path.write_bytes(edited)
                before = len(self.events())
                out = self.team("task", "new", "another", "--class", "standard")
                self.assertIn(f"{name} was edited by hand and not overwritten", out)
                self.assertEqual(len(self.events()), before + 1)
                self.assertEqual(path.read_bytes(), edited)
                diff = self.team("state", "diff", str(path))
                self.assertIn("edited by hand", diff)
                self.assertIn("\ufffd", diff)
                self.assertNotIn("\x00", diff)
                self.assertNotIn("\x1b", diff)
                before_import = self.events()
                for flags in ((), ("--apply",), ("--apply", "--force")):
                    error = self.team("state", "import", str(path), *flags, code=2)
                    self.assertIn(name, error)
                    self.assertIn("invalid UTF-8", error)
                    self.assertEqual(self.events(), before_import)
                    self.assertEqual(path.read_bytes(), edited)
                path.write_bytes(original)

    def test_diff_and_detection_agree_on_export_bytes(self) -> None:
        self.team("task", "new", "first", "--class", "standard")
        self.team("task", "start", "T-1")
        self.team("decision", "add", "sample decision", "--options", "a", "b", "--why", "w", "--risk", "r",
                  "--authority", "owner")
        self.team("schedule", "add", "2026-11-01T09:00:00+00:00", "sample reminder", "--owner", "human",
                  "--kind", "reminder")
        edits = {
            "final newline": lambda raw: raw[:-1],
            "all CRLF": lambda raw: raw.replace(b"\n", b"\r\n"),
            "one CRLF": lambda raw: raw.replace(b"\n", b"\r\n", 1),
            "trailing spaces": lambda raw: raw.replace(b"\n", b"  \n", 1),
            "changed line": lambda raw: raw.replace(b"#", b"##", 1),
            "extra newline": lambda raw: raw + b"\n",
        }
        for name in state.EXPORTS:
            path = self.team_dir / name
            original = path.read_bytes()
            self.assertEqual(self.team("state", "diff", str(path)), f"{name}: the same as the store\n")
            for label, edit in edits.items():
                with self.subTest(name=name, edit=label):
                    edited = edit(original)
                    path.write_bytes(edited)
                    diff = self.team("state", "diff", str(path))
                    self.assertNotIn("the same as the store", diff)
                    self.assertIn("line ", diff)
                    if "CRLF" in label:
                        self.assertEqual(diff.count("line ending LF -> CRLF"), edited.count(b"\r\n"))
                    elif label == "final newline":
                        self.assertIn("line ending LF -> no final newline", diff)
                    elif label == "trailing spaces":
                        self.assertIn('  "; line ending LF -> LF', diff)
                    out = self.team("task", "start", "T-1")
                    self.assertIn(f"{name} was edited by hand and not overwritten", out)
                    self.assertEqual(path.read_bytes(), edited)
                    path.write_bytes(original)
            self.assertNotIn("edited by hand", self.team("task", "start", "T-1"))

    def test_importing_another_board_does_not_overwrite_unrelated_hand_edits(self) -> None:
        self.team("task", "new", "first", "--class", "standard")
        board = self.team_dir / "board.md"
        clean = board.read_text()
        board.write_text(clean.replace("one batch.\n\n", "one batch.\nA hand note the lead wrote.\n\n"))
        edited = board.read_text()
        other = Path(self.p.tmp.name) / "elsewhere"
        other.mkdir()
        copy = other / "board.md"
        copy.write_text(clean.replace("| T-1 | first |", "| T-1 | renamed |"))
        out = self.team("state", "import", str(copy), "--apply")
        self.assertIn("1 events written", out)
        self.assertIn("board.md was edited by hand and not overwritten", out)
        self.assertNotIn("regenerated from the store", out)
        self.assertEqual(board.read_text(), edited)
        self.assertEqual(self.events("task.created")[-1]["data"]["title"], "renamed")

    def test_a_decision_note_deleted_by_hand_is_not_restored_by_import(self) -> None:
        self.team("decision", "add", "ask", "--options", "a", "b", "--why", "w", "--risk", "r", "--authority", "owner")
        self.team("decision", "approve", "D-1", "--note", "keep it")
        decisions = self.team_dir / "decisions.md"
        decisions.write_text(decisions.read_text().replace("; note: keep it", ""))
        edited = decisions.read_text()
        self.assertIn("edited by hand", self.team("task", "new", "x", "--class", "standard"))
        events = len(self.events())
        done = self.p.team("state", "import", str(decisions), "--apply")
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertEqual(len(self.events()), events)
        self.assertEqual(decisions.read_text(), edited)
        self.assertIn("+  Status: approved by human; note: keep it", self.team("state", "diff", str(decisions)))

    def test_duplicate_decision_fields_are_refused_and_the_file_kept(self) -> None:
        self.team("decision", "add", "ask", "--options", "a", "b", "--why", "w", "--risk", "r", "--authority", "owner")
        decisions = self.team_dir / "decisions.md"
        decisions.write_text(decisions.read_text().replace("  Why: w\n", "  Why: w\n  Why: second reason\n"))
        edited = decisions.read_text()
        self.assertIn("edited by hand", self.team("task", "new", "x", "--class", "standard"))
        events = len(self.events())
        done = self.p.team("state", "import", str(decisions), "--apply")
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertEqual(len(self.events()), events)
        self.assertEqual(decisions.read_text(), edited)
        self.assertIn("-  Why: second reason", self.team("state", "diff", str(decisions)))

    def test_an_option_with_a_slash_is_not_split_by_an_edited_why(self) -> None:
        self.team("decision", "add", "ask", "--options", "left / right", "--why", "w", "--risk", "r",
                  "--authority", "owner")
        decisions = self.team_dir / "decisions.md"
        decisions.write_text(decisions.read_text().replace("  Why: w\n", "  Why: changed\n"))
        edited = decisions.read_text()
        self.assertIn("  Options: left / right\n", edited)
        self.assertIn("edited by hand", self.team("task", "new", "x", "--class", "standard"))
        events = len(self.events())
        done = self.p.team("state", "import", str(decisions), "--apply")
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertEqual(len(self.events()), events)
        self.assertEqual(decisions.read_text(), edited)
        diff = self.team("state", "diff", str(decisions))
        self.assertIn("+  Why: w", diff)
        self.assertIn("-  Why: changed", diff)
        self.assertNotIn("Options: left\n", diff)


class MoveInTest(StateTest):
    """An older team's board.md, written before the store, moves in: its well-formed rows become tasks."""

    def old_board(self, rows: str) -> Path:
        board = self.team_dir / "board.md"
        board.write_text(re.sub(r"(\| T-1 \|.*\n)", lambda m: m.group(1) + rows, board.read_text(), count=1))
        return board

    def test_a_well_formed_unknown_row_is_a_new_task_without_force(self) -> None:
        self.team("task", "new", "first", "--class", "standard")
        board = self.old_board("| T-9 | old work | worker | blocked | review | out/a.md | needs a key | 2025-03-01 |\n"
                               "| T-10 | older work | - | todo | - | - | - | - |\n")
        listed = self.team("state", "import", str(board))
        self.assertIn("+ T-9 | old work | blocked", listed)
        self.assertIn("+ T-10 | older work | todo", listed)
        self.assertNotIn("not imported", listed)
        self.assertIn("board.md: 4 events to write, 0 lines refused, 0 other lines", listed)
        self.assertEqual(len(self.events()), 1)
        written = self.team("state", "import", str(board), "--apply")
        self.assertIn("4 events written", written)
        self.assertIn("regenerated from the store", written)
        created = {e["task"]: e["data"] for e in self.events("task.created")}
        self.assertEqual(created["T-9"], {"title": "old work", "owner": "worker"})
        state_ = {e["task"]: e["data"] for e in self.events("task.state")}
        self.assertEqual(state_["T-9"], {"state": "blocked", "owner": "worker", "next_gate": "review",
                                         "artifacts": "out/a.md", "blocked_reason": "needs a key"})
        self.assertEqual(state_["T-10"]["state"], "todo")
        snap = json.loads(self.team("snapshot", "--json"))
        self.assertEqual([t["id"] for t in snap["tasks"] if t["id"] != "T-1"], ["T-9", "T-10"])
        # Twice: the second --apply writes nothing.
        count = len(self.events())
        again = self.team("state", "import", str(board), "--apply")
        self.assertIn("0 events written", again)
        self.assertEqual(len(self.events()), count)

    def test_broken_rows_are_refused_and_text_outside_the_table_is_not_imported(self) -> None:
        self.team("task", "new", "first", "--class", "standard")
        board = self.old_board("| T-9 | fine | worker | done | - | - | - | 2025-03-01 |\n"
                               "| T 9! | broken id | worker | done | - | - | - | 2025-03-01 |\n"
                               "| T-11 | seven cells | worker | done | - | - | - |\n")
        board.write_text(board.read_text() + "\n| T-12 | after the table | worker | done | - | - | - | - |\n")
        kept = board.read_text()
        done = self.p.team("state", "import", str(board), "--apply")
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn("refused: 7 cells, the board has 8", done.stdout)
        self.assertIn("2 lines refused", done.stderr)
        self.assertNotIn("+ T-12", done.stdout)
        self.assertIn("not imported", done.stdout)
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(board.read_text(), kept)

    def test_eighty_rows_move_in_and_a_finished_epic_is_showcased(self) -> None:
        self.team("task", "new", "first", "--class", "standard")
        board = self.old_board("".join(f"| T-50-{n} | step {n} of the old epic | worker | done | - | out/{n}.md | - "
                                       f"| 2025-0{1 + n % 9}-1{n % 10} |\n" for n in range(1, 81)))
        self.assertIn("160 events written", self.team("state", "import", str(board), "--apply"))
        snap = json.loads(self.team("snapshot", "--json"))
        self.assertEqual(len(snap["tasks"]), 81)
        out = Path(self.p.tmp.name) / "showcase"
        done = subprocess.run([sys.executable, str(SOURCE.parent / "catalog" / "tools" / "companion" / "companion.py"),
                               "showcase", "T-50", "-o", str(out), "--team", str(self.team_dir),
                               "--pack", str(self.p.root / ".agents")],
                              env=self.p.env, capture_output=True, text=True, timeout=300)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["showcase.html", "showcase.json"])
        self.assertEqual(json.loads((out / "showcase.json").read_text())["artifacts"]["tasks"], 80)


class TeamDirTest(StateTest):
    def test_events_and_exports_follow_wouf_team_dir(self) -> None:
        other = Path(self.p.tmp.name) / "other-team"
        other.mkdir()
        for name in state.EXPORTS:
            (other / name).write_text(self.file(name))
        done = self.p.team("task", "new", "elsewhere", "--class", "standard", WOUF_TEAM_DIR=str(other))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(list((other / "events").glob("*.jsonl")))
        self.assertIn("| T-1 | elsewhere |", (other / "board.md").read_text())
        self.assertEqual(self.events(), [])
        self.assertNotIn("elsewhere", self.file("board.md"))


if __name__ == "__main__":
    unittest.main()
