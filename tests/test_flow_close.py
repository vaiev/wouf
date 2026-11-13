"""Flow closing preserves evidence, records an end and prevents accidental resumption."""

from __future__ import annotations

import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

from test_snapshot import ORIGIN, Team
sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import isolate_wouf_env  # noqa: E402

SOURCE = Path(__file__).resolve().parents[1] / ".agents"


def setUpModule() -> None:
    isolate_wouf_env()


class FlowCloseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.t = Team()
        self.addCleanup(self.t.close)
        self.folder = self.t.dir / "flows" / "T-1"
        self.folder.mkdir(parents=True)
        self.path = self.folder / "state.json"
        (self.t.dir / "events").mkdir()
        (self.t.dir / "events" / "ORIGIN").write_text(ORIGIN + "\n")
        self.steps = [{"kind": "worker", "outcome": "done", "status": "blocked", "why": "keep this"},
                      {"kind": "gate", "outcome": "red", "head": "a" * 40}]
        self.seed()

    def seed(self, status: str = "blocked") -> None:
        self.path.write_text(json.dumps({"task": "T-1", "status": status, "class": "standard",
                                        "steps": self.steps, "why": "old reason", "branch": "werk/T-1"}))
        (self.folder / "brief.md").write_text("Class: standard\nObjective: original\n")

    def state(self) -> dict:
        return json.loads(self.path.read_text())

    def close_flow(self, *args: str, code: int = 0):
        return self.t.team("flow", "T-1", "--close", *args, code=code)

    def test_close_each_closable_status_keeps_every_step_and_records_one_event(self) -> None:
        for status in ("blocked", "limited", "question", "retry"):
            with self.subTest(status=status):
                self.seed(status)
                before = len(self.t.feed("--since", "0"))
                original = self.state()
                brief = (self.folder / "brief.md").read_bytes()
                self.close_flow("--reason", "landed as T-0102b")
                closed = self.state()
                self.assertEqual(closed, {**original, "status": "closed", "closed": closed["closed"]})
                self.assertEqual(closed["closed"]["reason"], "landed as T-0102b")
                self.assertEqual(closed["closed"]["from"], status)
                self.assertRegex(closed["closed"]["at"], r"^\d{4}-\d\d-\d\dT")
                events = self.t.feed("--since", "0")
                self.assertEqual(len(events), before + 1)
                self.assertEqual((events[-1]["kind"], events[-1]["data"]),
                                 ("flow.step", {"kind": "end", "status": "closed"}))
                self.assertEqual((self.folder / "brief.md").read_bytes(), brief)
                receipt = json.loads((self.t.dir / "logs" / "lead.jsonl").read_text().splitlines()[-1])
                self.assertIn("flow closed", receipt["signal"])
                self.assertIn("landed as T-0102b", receipt["signal"])
                self.assertFalse((self.folder / "flow.lock").exists())

    def test_a_reason_is_required(self) -> None:
        before = self.path.read_bytes()
        for args in ((), ("--reason",), ("--reason", " \n\t")):
            with self.subTest(args=args):
                done = self.close_flow(*args, code=2)
                self.assertIn("reason", done.stderr)
                self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(list((self.t.dir / "events").glob("*.jsonl")))

    def test_running_ready_and_other_states_refuse_without_changes(self) -> None:
        for status in ("running", "ready", "new", "answered"):
            with self.subTest(status=status):
                self.seed(status)
                before = self.path.read_bytes()
                done = self.close_flow("--reason", "done elsewhere", code=2)
                self.assertIn(status, done.stderr)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertFalse((self.folder / "flow.lock").exists())
        self.assertFalse(list((self.t.dir / "events").glob("*.jsonl")))

    def test_a_live_lock_or_worker_prevents_close_even_with_old_status(self) -> None:
        lock = self.folder / "flow.lock"
        lock.write_text(json.dumps({"pid": os.getpid()}))
        before = self.path.read_bytes()
        self.assertIn("running", self.close_flow("--reason", "done", code=2).stderr)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(json.loads(lock.read_text()), {"pid": os.getpid()})
        lock.unlink()
        flow = self.state()
        flow["pid"] = os.getpid()
        self.path.write_text(json.dumps(flow))
        before = self.path.read_bytes()
        self.assertIn("running", self.close_flow("--reason", "done", code=2).stderr)
        self.assertEqual(self.path.read_bytes(), before)

    def test_second_close_is_idempotent(self) -> None:
        self.close_flow("--reason", "done elsewhere")
        before = self.path.read_bytes()
        events = self.t.feed("--since", "0")
        receipts = (self.t.dir / "logs" / "lead.jsonl").read_bytes()
        self.assertIn("already closed", self.close_flow("--reason", "another reason").stdout)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.t.feed("--since", "0"), events)
        self.assertEqual((self.t.dir / "logs" / "lead.jsonl").read_bytes(), receipts)

    def test_closed_cannot_resume_or_change_with_retry_answer_or_staged(self) -> None:
        self.close_flow("--reason", "done elsewhere")
        before = self.path.read_bytes()
        brief = (self.folder / "brief.md").read_bytes()
        for flags in ((), ("--retry",), ("--answer", "continue"), ("--staged",), ("--detach",)):
            with self.subTest(flags=flags):
                self.assertIn("closed", self.t.team("flow", "T-1", *flags, code=6).stdout)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual((self.folder / "brief.md").read_bytes(), brief)

    def test_reason_is_masked_before_capping_and_controls_are_removed(self) -> None:
        secret = "test-stored-value-4711"
        self.t.secret("CLOSE_TEST", secret)
        reason = "landed\n\t" + secret + " " + "detail " * 70
        self.close_flow("--reason", reason)
        clean = self.state()["closed"]["reason"]
        self.assertNotIn(secret, clean)
        self.assertNotIn("\n", clean)
        self.assertNotIn("\t", clean)
        self.assertLessEqual(len(clean), 200)
        self.assertIn("***", clean)
        self.assertNotIn(secret, self.path.read_text())
        self.assertNotIn(secret, (self.t.dir / "logs" / "lead.jsonl").read_text())

    def test_context_hides_closed_in_store_and_file_fallback(self) -> None:
        self.t.events((("flow.step", "lead", "T-1", {"kind": "end", "status": "blocked"}),))
        self.assertIn("T-1 blocked", self.t.team("context").stdout)
        self.close_flow("--reason", "done elsewhere")
        self.assertNotIn("Flows not finished", self.t.team("context").stdout)
        self.assertNotIn("Flows not finished", self.t.team("status").stdout)
        # No events: context uses state.json instead of the cache.
        for path in (self.t.dir / "events").glob("*.jsonl"):
            path.unlink()
        self.assertNotIn("Flows not finished", self.t.team("context").stdout)

    def test_snapshot_keeps_v1_and_shows_closed_without_editing_the_board(self) -> None:
        self.t.events((("task.created", "lead", "T-1", {"title": "Parser"}),
                       ("task.state", "lead", "T-1", {"state": "blocked"})))
        board = (self.t.dir / "board.md").read_bytes()
        self.close_flow("--reason", "done elsewhere")
        snap = self.t.snapshot()
        self.assertEqual(snap["schema"], "wouf.cockpit.v1")
        self.assertEqual((snap["tasks"][0]["status"], snap["tasks"][0]["step_status"]), ("closed", "closed"))
        self.assertEqual((self.t.dir / "board.md").read_bytes(), board)
        sys.path.insert(0, str(SOURCE / "bin"))
        from teamlib.snapshot import SCHEMA
        self.assertIn("closed", json.loads(SCHEMA)["$defs"]["task"]["properties"]["status"]["enum"])

    def test_old_state_without_closed_reads_as_before(self) -> None:
        self.assertNotIn("closed", self.state())
        before = self.path.read_bytes()
        self.assertIn("blocked old reason", self.t.team("flow", "T-1", code=1).stdout)
        self.assertIn("blocked old reason", self.t.team("flow", "T-1", "--status", code=1).stdout)
        self.assertEqual(self.path.read_bytes(), before)

    def test_snapshot_includes_closed_flow_without_a_board_task(self) -> None:
        self.close_flow("--reason", "done elsewhere")
        task = self.t.snapshot()["tasks"][0]
        self.assertEqual((task["id"], task["status"]), ("T-1", "closed"))

    def test_run_flow_rechecks_closed_after_taking_the_lock(self) -> None:
        self.close_flow("--reason", "done elsewhere")
        before = self.path.read_bytes()
        sys.path.insert(0, str(SOURCE / "bin"))
        from teamlib import flow
        with patch.object(flow, "flow_dir", return_value=self.folder):
            self.assertEqual(flow.run_flow(None, "T-1"), 6)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.folder / "flow.lock").exists())

    def test_new_brief_restarts_with_history_and_no_old_steps(self) -> None:
        self.t.events((("task.created", "lead", "T-1", {"title": "Original"}),))
        self.close_flow("--reason", "done elsewhere")
        old = self.state()
        old.update(repo=str(self.t.home), base="a" * 40)
        self.path.write_text(json.dumps(old))
        brief = self.t.home / "new.md"
        brief.write_text("Class: analysis\nObjective: new work\n")
        sys.path.insert(0, str(SOURCE / "bin"))
        from teamlib import flow, flowend, state  # Register the store hook, as the CLI registry does.
        with patch.dict(os.environ, self.t.env), patch.object(flow, "flow_dir", return_value=self.folder), \
                patch.object(flow, "bot", return_value={}), patch.object(flow, "run_flow", return_value=0) as run, \
                patch.object(flowend, "git", return_value=SimpleNamespace(stdout="b" * 40)):
            self.assertEqual(flow.cmd_flow(flow.config(), ["T-1", "--brief", str(brief)]), 0)
        fresh = self.state()
        self.assertEqual((fresh["status"], fresh["class"], fresh["steps"]), ("new", "analysis", []))
        self.assertNotIn("closed", fresh)
        self.assertEqual(fresh["history"][0]["steps"], self.steps)
        self.assertEqual(fresh["history"][0]["closed"], old["closed"])
        self.assertEqual(fresh["history"][0]["brief"], "Class: standard\nObjective: original\n")
        self.assertEqual((self.folder / "brief.md").read_text(), brief.read_text())
        run.assert_called_once()
        self.assertNotEqual(self.t.snapshot()["tasks"][0]["status"], "closed")

    def test_retry_cannot_overwrite_a_close_between_read_and_write(self) -> None:
        sys.path.insert(0, str(SOURCE / "bin"))
        from teamlib import flow, flowend
        original = flow.write_json
        close_codes = []

        def writing(path, data):
            if path == self.path and data.get("status") == "retry" and not close_codes:
                try:
                    close_codes.append(flowend.close("T-1", self.folder, "landed elsewhere"))
                except SystemExit as error:
                    close_codes.append(error.code)
            original(path, data)

        with patch.dict(os.environ, self.t.env), patch.object(flow, "flow_dir", return_value=self.folder), \
                patch.object(flow, "write_json", side_effect=writing), \
                patch.object(flowend, "write_json", side_effect=writing), \
                patch.object(flow, "run_flow", return_value=0):
            flow.cmd_flow(flow.config(), ["T-1", "--retry"])
        self.assertEqual(len(close_codes), 1)
        if close_codes[0] == 0:
            self.assertEqual(self.state()["status"], "closed", "retry overwrote a successful close")
        else:
            self.assertEqual(close_codes, [2], "the live lock must refuse a simultaneous close")

    def test_close_wins_before_a_stale_retry_write(self) -> None:
        sys.path.insert(0, str(SOURCE / "bin"))
        from teamlib import flow, flowend
        original = flowend.write_state

        def stale_write(folder, data):
            flowend.close("T-1", folder, "landed elsewhere")
            original(folder, data)

        with patch.dict(os.environ, self.t.env), patch.object(flow, "flow_dir", return_value=self.folder), \
                patch.object(flowend, "write_state", side_effect=stale_write), \
                patch.object(flow, "run_flow") as run:
            with self.assertRaises(SystemExit) as error:
                flow.cmd_flow(flow.config(), ["T-1", "--retry"])
        self.assertEqual(error.exception.code, 6)
        self.assertEqual(self.state()["status"], "closed")
        self.assertEqual(self.state()["steps"], self.steps)
        run.assert_not_called()

    def test_help_explains_close_and_restart(self) -> None:
        help_text = self.t.team("help").stdout
        self.assertIn("--close --reason TEXT", help_text)
        self.assertIn("--brief", help_text)
        self.assertIn("fresh flow", help_text)


if __name__ == "__main__":
    unittest.main()
