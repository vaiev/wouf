"""Regression tests for the context-limit boundary: what `team context --claude` (the SessionStart hook) injects when a
team has far more open work than a session can read.

Drift causes pinned (werkstatt docs/notes/2026-11-10-drift-forensics.md numbering): 0 auto compaction, 6 session
length. A long session is compacted, and the hook injects the state again: it must stay small however much is open,
keep what a new session needs first (the questions for the human, the flows not finished, the newest rows), and say
what it left out and where to read it. Pinned here for both paths `team context` takes, with 200 open rows, 50
questions and 50 flows: a team in the store (events), the path a running team takes, and a team without events, which
reads board.md and the flows' state files. The budget is stated twice: BYTE_BUDGET, a ceiling on the whole hook
payload in bytes (JSON escapes and multi-byte characters counted), and CONTEXT_LIMIT, teamlib's own limit of 4000
characters of context, which sits far below it.

Not proved: any model behaviour, a compaction threshold, or what a host does with a hook's output. No model is called;
every test runs offline in a temp project."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import isolate_wouf_env  # noqa: E402
from test_team_cli import Project  # noqa: E402

BYTE_BUDGET = 16 * 1024  # of the whole `team context --claude` payload, in bytes
CONTEXT_LIMIT = 4000  # characters of context; teamlib.status.CONTEXT_LIMIT
ORIGIN = "0123456789ab"
MORE_ROWS = re.compile(r"^(\d+) more rows: team/board\.md$")
MORE_LINES = re.compile(r"^… (\d+) more lines?$")


def setUpModule() -> None:
    isolate_wouf_env()


class ContextBase(unittest.TestCase):
    def setUp(self) -> None:
        self.p = Project(mode="host", runner="fake")
        self.addCleanup(self.p.close)
        self.team_dir = self.p.root / ".agents" / "team"

    def hook(self) -> tuple[str, int]:
        """The injected context of `team context --claude` and the size in bytes of the whole payload."""
        done = self.p.team("context", "--claude")
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"], len(done.stdout.encode("utf-8"))

    def sections(self, text: str) -> dict[str, list[str]]:
        found, current = {}, None
        for line in text.splitlines():
            if line.startswith("## "):
                current = found.setdefault(line, [])
            elif current is not None:
                current.append(line)
        return found


class ContextLimitTest(ContextBase):
    def store(self, rows: int, questions: int, flows: int, question: str = "q") -> None:
        """A store of `rows` open tasks T-1.., `questions` open questions (on T-1..) from the lead, and `flows`
        blocked flows (on T-1..), written as events, with board.md exported from it as every state command does."""
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        events = []

        def add(kind: str, task: str, data: dict, actor: str = "lead") -> None:
            seq = len(events) + 1
            events.append({"v": 1, "id": f"{ORIGIN}-{seq}", "seq": seq, "t": stamp, "origin": ORIGIN, "actor": actor,
                           "kind": kind, "task": task, "scope": "local", "data": data})

        for i in range(1, rows + 1):
            add("task.created", f"T-{i}", {"title": f"row {i}", "class": "standard", "owner": "worker"})
        for i in range(1, questions + 1):
            add("question.asked", f"T-{i}", {"id": f"Q-{i}", "text": question, "from": "lead"})
        for i in range(1, flows + 1):
            add("flow.step", f"T-{i}", {"kind": "end", "status": "blocked"})
        folder = self.team_dir / "events"
        folder.mkdir()
        (folder / "ORIGIN").write_text(ORIGIN + "\n")
        (folder / f"{ORIGIN}.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
        export = ("import sys; sys.path.insert(0, sys.argv[1]); from teamlib import state; "
                  "state.export(state.EMPTY, state.snapshot(state.store()))")
        subprocess.run([sys.executable, "-c", export, str(self.p.root / ".agents" / "bin")], check=True,
                       capture_output=True, env=self.p.env, cwd=self.p.root)

    def test_a_team_with_200_rows_50_questions_and_50_flows_stays_inside_the_budget(self) -> None:
        self.store(200, 50, 50)
        text, size = self.hook()
        self.assertLessEqual(size, BYTE_BUDGET)
        self.assertLessEqual(len(text), CONTEXT_LIMIT)
        self.assertEqual(text, self.p.team("context").stdout.rstrip("\n"), "the plain text is the same context")

    def test_the_questions_and_flows_stay_whole_and_the_newest_rows_are_kept(self) -> None:
        self.store(200, 50, 50)
        found = self.sections(self.hook()[0])
        self.assertEqual(found["## Questions for the human"], [f"- Q-{i} T-{i}, from lead: q" for i in range(1, 51)])
        self.assertEqual(found["## Flows not finished"][:50], [f"T-{i} blocked" for i in range(1, 51)])
        shown = [int(m.group(1)) for line in found["## Open on the board"]
                 if (m := re.match(r"\| T-(\d+) \| row \1 \| worker \| todo \|", line))]
        self.assertGreaterEqual(len(shown), 10, "room is left for rows after the questions and flows")
        self.assertEqual(shown, list(range(201 - len(shown), 201)), "the newest rows, in order, with no gap")
        for line in found["## Open on the board"]:
            self.assertRegex(line, r"\|$|^\d+ more rows", "a row is shown whole or not at all")

    def test_the_text_ends_with_an_explicit_count_of_the_rows_left_out_and_where_they_are(self) -> None:
        self.store(200, 50, 50)
        text = self.hook()[0]
        last = text.splitlines()[-1]
        self.assertRegex(last, MORE_ROWS)
        found = self.sections(text)
        shown = len([line for line in found["## Open on the board"] if line.startswith("| T-")])
        self.assertEqual(int(MORE_ROWS.match(last).group(1)) + shown, 200, "every row is shown or counted")
        self.assertNotRegex(text, r"(?m)^… \d+ more lines?$", "nothing else was cut, so nothing else is counted")
        # board.md has what the line sends the reader to
        board = (self.team_dir / "board.md").read_text()
        self.assertEqual(len(re.findall(r"(?m)^\| T-\d+ \|", board)), 200)

    def test_nothing_is_said_about_a_team_that_fits(self) -> None:
        self.store(5, 2, 1)
        text = self.hook()[0]
        self.assertNotRegex(text, r"more (rows|lines?)")
        self.assertEqual(len([line for line in text.splitlines() if line.startswith("| T-")]), 5)

    def test_non_ascii_questions_are_counted_in_the_payload_bytes_not_the_characters(self) -> None:
        """JSON writes each emoji as a 12-byte escape. Sixty emoji per question, fifty questions: about 4000
        characters that are about 48 KiB of payload, so the byte budget has to bind, not the character limit."""
        self.store(200, 50, 50, question="\U0001F600" * 60)
        text, size = self.hook()
        self.assertLessEqual(size, BYTE_BUDGET)
        self.assertLessEqual(len(text), CONTEXT_LIMIT)
        self.assertRegex(text.splitlines()[-1], MORE_ROWS, "the rows left out are still counted and named")

    def test_questions_that_fill_the_limit_leave_the_row_count_as_the_last_line(self) -> None:
        """Questions that fill the whole limit by themselves: no row is shown, the questions that do not fit are
        counted, and the last line is still the explicit count of the rows and the board path, never a cut."""
        self.store(200, 50, 0, question="w" * 130)
        text, size = self.hook()
        self.assertLessEqual(size, BYTE_BUDGET)
        self.assertLessEqual(len(text), CONTEXT_LIMIT)
        lines = text.splitlines()
        self.assertEqual(MORE_ROWS.match(lines[-1]).group(1), "200", "every row is left out and counted")
        self.assertRegex(lines[-2], MORE_LINES, "the questions that do not fit are counted too")
        found = self.sections("\n".join(lines[:-2]))
        asked = [line for line in found["## Questions for the human"] if line.startswith("- Q-")]
        self.assertEqual(asked, [f"- Q-{i} T-{i}, from lead: {'w' * 130}" for i in range(1, len(asked) + 1)],
                         "the questions that are shown are whole and in order")
        self.assertEqual(len([line for line in found["## Open on the board"] if line.startswith("| T-")]), 0)


class BoardBackedContextLimitTest(ContextBase):
    """A team without events: board.md, its questions and the flows' state files are all there is to read."""

    def board(self, rows: int, questions: int, flows: int) -> None:
        """board.md with `rows` open rows T-1.., `questions` open questions and `flows` blocked flows (T-1..)."""
        head = "| ID | Title | Owner | State | | | | |\n|---|---|---|---|---|---|---|---|\n"
        body = "".join(f"| T-{i} | row {i} | worker | todo | | | | |\n" for i in range(1, rows + 1))
        asked = "".join(f"- Q-{i} T-{i}, from lead: q\n" for i in range(1, questions + 1))
        (self.team_dir / "board.md").write_text(f"# Board\n\n{head}{body}\n## Questions for the human\n\n{asked}")
        for i in range(1, flows + 1):
            folder = self.team_dir / "flows" / f"T-{i}"
            folder.mkdir(parents=True)
            (folder / "state.json").write_text(json.dumps({"task": f"T-{i}", "status": "blocked", "steps": []}))

    def test_200_rows_50_questions_and_50_flows_keep_T_200_and_every_question_inside_the_budget(self) -> None:
        self.board(200, 50, 50)
        self.assertFalse(any((self.team_dir / "events").glob("*.jsonl")), "a team without events")
        text, size = self.hook()
        self.assertLessEqual(size, BYTE_BUDGET)
        self.assertLessEqual(len(text), CONTEXT_LIMIT)
        self.assertEqual(text, self.p.team("context").stdout.rstrip("\n"), "the plain text is the same context")
        lines = text.splitlines()
        self.assertRegex(lines[-1], MORE_ROWS)
        self.assertNotRegex(text, r"(?m)^… \d+ more lines?$", "the old form never appears")
        found = self.sections(text)
        self.assertEqual(found["## Questions for the human"], [f"- Q-{i} T-{i}, from lead: q" for i in range(1, 51)])
        shown = [int(m.group(1)) for line in found["## Open on the board"]
                 if (m := re.match(r"\| T-(\d+) \| row \1 \| worker \| todo \|", line))]
        self.assertIn(200, shown, "the newest row stays")
        self.assertEqual(shown, list(range(201 - len(shown), 201)), "the newest rows, in order, with no gap")
        self.assertEqual(int(MORE_ROWS.match(lines[-1]).group(1)) + len(shown), 200, "every row is shown or counted")
        self.assertEqual(sorted(found["## Flows not finished"][:50]), sorted(f"T-{i} blocked" for i in range(1, 51)),
                         "every flow is shown (in the folders' name order)")

    def test_the_newest_rows_are_chosen_by_task_number_not_by_the_order_of_the_file(self) -> None:
        """A board written newest first (T-200 down to T-1, an S- row on top) keeps the same rows as one written
        oldest first: T-2 < T-10 < T-200, and an S- row comes after the T- rows."""
        head = "| ID | Title | Owner | State | | | | |\n|---|---|---|---|---|---|---|---|\n"
        ids = ["S-1", *(f"T-{i}" for i in range(200, 0, -1))]
        body = "".join(f"| {i} | row {i} | worker | todo | | | | |\n" for i in ids)
        (self.team_dir / "board.md").write_text(f"# Board\n\n{head}{body}")
        text = self.hook()[0]
        lines = text.splitlines()
        self.assertRegex(lines[-1], MORE_ROWS)
        shown = [m.group(1) for line in self.sections(text)["## Open on the board"]
                 if (m := re.match(r"\| ([ST]-\d+) \| row \1 \|", line))]
        self.assertGreater(len(shown), 10, "the budget leaves room for rows")
        self.assertEqual(shown[-1], "S-1", "an S- row comes after the T- rows")
        numbers = [int(i[2:]) for i in shown[:-1]]
        self.assertEqual(numbers, list(range(201 - len(numbers), 201)), "T-200 stays, the rows follow in number order")
        self.assertEqual(int(MORE_ROWS.match(lines[-1]).group(1)) + len(shown), 201, "every row is shown or counted")

    def test_nothing_is_said_about_a_board_that_fits(self) -> None:
        self.board(5, 2, 1)
        text = self.hook()[0]
        self.assertNotRegex(text, r"more (rows|lines?)")
        self.assertEqual(len([line for line in text.splitlines() if line.startswith("| T-")]), 5)


if __name__ == "__main__":
    unittest.main()
