"""Regression tests for the handover boundary: what `team context` (and `team context --claude`, the SessionStart
hook) hands a new lead session.

Drift causes pinned (werkstatt docs/notes/2026-11-10-drift-forensics.md numbering): 0 auto compaction, 3 stale
autonomy memories read as live, 6 session length. A session that restarts after a compaction or a long run gets its
state from the store, not from what it remembers: exactly the open board rows, the open questions and the flows not
finished, never a done row, an answered question or a closed flow, and one line when board.md was edited by hand.

Not proved: any model behaviour. These tests show what the pack prints, not that a model reads it, believes it or
acts on it. No model is called; every test runs offline in a temp project."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import isolate_wouf_env  # noqa: E402
from test_team_cli import Project  # noqa: E402


def setUpModule() -> None:
    isolate_wouf_env()


# The flow of a task as the store keeps it: its last flow.step, key by key as `team flow` writes it.
LIMITED = {"kind": "end", "status": "limited", "until": "2026-10-25T09:50:00+00:00"}
CLOSED = {"kind": "end", "status": "closed"}
HEADERS = ("## Due and routines", "## Open on the board", "## Questions for the human", "## Flows not finished")
DIFFERS = "state: board.md differs from the store; team state import .agents/team/board.md offers its edits"


def sections(text: str) -> dict[str, list[str]]:
    """The lines under each ## header of a context text."""
    found, current = {}, None
    for line in text.splitlines():
        if line in HEADERS:
            current = found.setdefault(line, [])
        elif current is not None:
            current.append(line)
    return found


def row_ids(lines: list[str]) -> list[str]:
    return [m.group(1) for line in lines if (m := re.match(r"\| (T-\d+) \|", line))]


class HandoverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.p = Project(mode="host", runner="fake")
        self.addCleanup(self.p.close)
        self.team_dir = self.p.root / ".agents" / "team"

    def team(self, *args: str) -> str:
        done = self.p.team(*args)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return done.stdout

    def flow_step(self, task: str, data: dict) -> None:
        done = subprocess.run([sys.executable, str(self.p.root / ".agents" / "bin" / "log"), "event", "add",
                               "flow.step", "--actor", "lead", "--task", task, "--data", json.dumps(data)],
                              env=self.p.env, capture_output=True, text=True, cwd=self.p.root)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def busy_team(self) -> None:
        """T-1 todo, T-2 working, T-3 review, T-4 blocked, T-5 done; one open question (Q-2 on T-1) and one answered
        (Q-1 on T-5, closed when the task is done); T-2's flow limited (not finished), T-5's flow closed."""
        for title in ("todo-row", "working-row", "review-row", "blocked-row", "done-row"):
            self.team("task", "new", title, "--class", "standard", "--owner", "worker")
        self.team("task", "start", "T-2")
        self.team("task", "review", "T-3")
        self.team("task", "block", "T-4", "--reason", "needs input")
        self.team("question", "add", "T-5", "answered-question")
        self.team("task", "done", "T-5")
        self.team("question", "add", "T-1", "open-question")
        self.flow_step("T-2", LIMITED)
        self.flow_step("T-5", CLOSED)

    def state_bytes(self) -> dict[str, str]:
        """What a read must leave alone: the event log and the exports, by hash (the cache under env/ is not state)."""
        paths = [*(self.team_dir / "events").glob("*"), *(self.team_dir / n for n in ("board.md", "decisions.md",
                                                                                      "schedule.md"))]
        return {str(p.relative_to(self.team_dir)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in paths if p.is_file()}

    def test_lists_exactly_the_open_rows_the_open_question_and_the_unfinished_flow(self) -> None:
        self.busy_team()
        found = sections(self.team("context"))
        self.assertEqual(row_ids(found["## Open on the board"]), ["T-1", "T-2", "T-3", "T-4"])
        states = [line.split("|")[4].strip() for line in found["## Open on the board"]]
        self.assertEqual(states, ["todo", "working", "review", "blocked"])
        self.assertEqual(found["## Questions for the human"], ["- Q-2 T-1, from lead: open-question"])
        self.assertEqual(found["## Flows not finished"], ["T-2 limited until 2026-10-25T09:50:00+00:00"])

    def test_never_lists_the_done_row_the_answered_question_or_the_closed_flow(self) -> None:
        self.busy_team()
        text = self.team("context")
        self.assertNotIn("done-row", text)
        self.assertNotIn("answered-question", text)
        self.assertNotIn("T-5", text, "neither the done row nor its closed flow")
        self.assertNotIn("closed", text)

    def test_claude_payload_carries_the_same_text(self) -> None:
        self.busy_team()
        plain = self.team("context")
        hook = json.loads(self.team("context", "--claude"))["hookSpecificOutput"]
        self.assertEqual(hook["hookEventName"], "SessionStart")
        self.assertEqual(hook["additionalContext"], plain.rstrip("\n"))

    def test_a_board_edited_by_hand_and_not_imported_is_reported_not_read(self) -> None:
        self.busy_team()
        self.assertNotIn("differs from the store", self.team("context"), "the exports are the store's own")
        board = self.team_dir / "board.md"
        board.write_text(board.read_text() + "| T-77 | typed-by-hand | lead | todo | | | | |\n")
        text = self.team("context")
        self.assertIn(DIFFERS, text.splitlines())
        self.assertNotIn("typed-by-hand", text, "an edit nobody imported is not the state")
        self.assertEqual(row_ids(sections(text)["## Open on the board"]), ["T-1", "T-2", "T-3", "T-4"])
        self.assertIn(DIFFERS, json.loads(self.team("context", "--claude"))["hookSpecificOutput"]
                      ["additionalContext"].splitlines())

    def test_context_twice_is_the_same_text_and_changes_no_state(self) -> None:
        self.busy_team()
        board = self.team_dir / "board.md"
        board.write_text(board.read_text() + "| T-77 | typed-by-hand | lead | todo | | | | |\n")
        before = self.state_bytes()
        first = [self.team("context"), self.team("context", "--claude")]
        second = [self.team("context"), self.team("context", "--claude")]
        self.assertEqual(first, second)
        self.assertEqual(self.state_bytes(), before)
        self.assertIn("board.md", before)

    def test_a_team_without_events_reads_the_same_promise_from_board_md(self) -> None:
        """The other path: no store yet, so board.md and the flows' state files are read. Same rows, same exclusions."""
        board = self.team_dir / "board.md"
        rows = "".join(f"| T-{n} | {title} | worker | {state} | - | - | - | 2026-11-10 |\n" for n, (title, state) in
                       enumerate((("todo-row", "todo"), ("working-row", "working"), ("review-row", "review"),
                                  ("blocked-row", "blocked"), ("done-row", "done")), 1))
        board.write_text(board.read_text().replace("| | | | | | | | |\n", rows).replace(
            "asks them in one batch.\n\n-", "asks them in one batch.\n\n- Q-1 T-1, from lead: open-question"))
        for task, state in (("T-2", {"status": "limited", "until": "2026-10-25T09:50:00+00:00"}),
                            ("T-5", {"status": "closed"})):
            (self.team_dir / "flows" / task).mkdir(parents=True)
            (self.team_dir / "flows" / task / "state.json").write_text(json.dumps(
                {"task": task, "branch": f"wouf/{task}", "steps": [], **state}))
        text = self.team("context")
        found = sections(text)
        self.assertEqual(row_ids(found["## Open on the board"]), ["T-1", "T-2", "T-3", "T-4"])
        self.assertEqual(len(found["## Questions for the human"]), 1)
        self.assertIn("open-question", found["## Questions for the human"][0])
        self.assertEqual(found["## Flows not finished"], ["T-2 limited 2026-10-25T09:50:00+00:00"])
        self.assertNotIn("done-row", text)
        self.assertNotIn("T-5", text)
        self.assertEqual(self.team("context"), text)


if __name__ == "__main__":
    unittest.main()
