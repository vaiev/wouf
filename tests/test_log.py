"""Tests for .agents/bin/log: JSONL receipts, status per member, task timeline and indexed search."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import shutil
import sqlite3
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import isolate_wouf_env  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / ".agents" / "bin" / "log"
# bin/team keeps the shared blocks in its kernel module.
TEAM = ROOT / ".agents" / "bin" / "teamlib" / "core.py"
BLOCK_START = "# ---- secret patterns"
BLOCK_END = "# ---- end of secret patterns ----"
TASK_START = "# ---- task ids"
TASK_END = "# ---- end of task ids ----"


def helper(path: Path):
    loader = SourceFileLoader(f"wouf_{path.name}", str(path))
    module = module_from_spec(spec_from_loader(f"wouf_{path.name}", loader))
    loader.exec_module(module)
    return module


def pattern_block(path: Path, first: str = BLOCK_START, last: str = BLOCK_END) -> str:
    text = path.read_text(encoding="utf-8")
    start = text.find(first)
    end = text.find(last, start)
    assert start >= 0 and end > start, f"{path.name}: no {first} block"
    return text[start:end]


# Built from parts so this file holds no contiguous key. Each value has the shape of a real key, nothing more.
A36 = "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bC2dF4gH6"
AWS = "AKIA" + "IOSFODNN7EXAMPL0"
# GitHub's documented alphabet is [A-Za-z0-9_]; Slack documents the workflow token prefix only.
GITHUB_UNDERSCORE = "gh" "p_" + A36[:17] + "_" + A36[17:]
SLACK_WORKFLOW = "xw" "fp-" + "7-A0123456789-" + "aBcDeF0123456789"
PEM_BODY = ("SYNTHETICBODYLINE1" + "Ab0+" * 11 + "Q", "SYNTHETICBODYLINE2" + "Cd1/" * 11 + "R", "SYNTHETICTAIL==")
PEM = "-----BEGIN RSA " "PRIVATE KEY-----\n" + "\n".join(PEM_BODY) + "\n-----END RSA " "PRIVATE KEY-----"
HITS = {
    "GitHub classic": ["gh" "p_" + A36, "token=gh" "s_" + A36 + " ok", "gh" "o_" + A36, "gh" "u_" + A36,
                       "gh" "r_" + A36, "https://x-access-" "token:gh" "s_" + A36 + "@github.com/o/r",
                       GITHUB_UNDERSCORE, "gh" "s_" + "_" * 3 + A36],
    "GitHub fine-grained": ["github_" "pat_" + "11ABCDEFG0" + "a" * 12 + "_" + "Zx9" * 19 + "Q0"],
    "Anthropic": ["sk-" "ant-api03-" + A36 + "-" + A36 + "AA", "key: sk-" "ant-oat01-" + A36],
    "OpenAI": ["sk-" + "Ab12" * 5 + "T3Blbk" "FJ" + "Cd34" * 5, "sk-" "proj-" + A36 + "_" + A36,
               "sk-" "svcacct-" + A36 + "-" + A36, "sk-" "admin-" + A36 + A36],
    "AWS access key": ["AKIA" + "IOSFODNN7EXAMPL0", "id ASIA" + "Q3EGRI4XEXAMPLE1,", "A3TX" + "ABCDEFGHIJ012345"],
    "Google API key": ["AIza" + "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6_", "key=AIza" + "Sy" + "x-Y_z" * 6 + "abc"],
    "Slack token": ["xox" "b-1234567890-1234567890123-" + "AbCdEfGhIjKlMnOpQrStUvWx",
                    "xox" "p-1-" + "Mi0yLTEyMzQ1Njc4OTAtMTIzNDU2Nzg5", "xap" "p-1-A0123456789-" + "1234567890-abcdef0123",
                    "https://hooks.slack.com/services/" "T0123ABCD/B0123ABCD/" + "aBcDeFgHiJkLmNoPqRsTuVwX",
                    SLACK_WORKFLOW, "token " + "xw" "fp-" + "aBcDeFgHiJkLmNoP" + " expires"],
    "age secret key": ["AGE-SECRET" "-KEY-1" + ("QPZRY9X8GF2TVDW0S3JN54KHCE6MUA7L" * 2)[:58]],
    "private key": ["-----BEGIN " "PRIVATE KEY-----\nMIIEvQ", "-----BEGIN RSA " "PRIVATE KEY-----",
                    "-----BEGIN EC " "PRIVATE KEY-----", "-----BEGIN OPENSSH " "PRIVATE KEY-----",
                    "-----BEGIN ENCRYPTED " "PRIVATE KEY-----", "-----BEGIN PGP " "PRIVATE KEY BLOCK-----",
                    "---- BEGIN SSH2 ENCRYPTED " "PRIVATE KEY ----", "x-----BEGIN DSA " "PRIVATE KEY-----MIIB", PEM,
                    "key:\r\n" + PEM.replace("\n", "\r\n") + "\r\nafter"],
}
NON_HITS = [
    "0123456789abcdef0123456789abcdef01234567", "e4ab2da", "a" * 64, "0123456789ABCDEF" * 4,
    "gh" "p_", "gh" "p_tooshort", "github_" "pat_ tokens are documented", "github_" "pat_" + "a" * 30,
    "sk-" "ant-", "sk-" "ant-api03-...", "sk-learn-compatible-pipelines-for-everyone", "task-" + "a" * 40,
    "risk-" + A36, "sk-" "proj-short", "AKIA", "AKIA" + "IOSFODNN7EXAMPLE0123", "akia" + "iosfodnn7example",
    "AIza", "AIza" + "Sy-too-short", "xoxo-hugs-and-kisses-for-everyone", "xox" "b-", "xox" "b-notdigits-abc",
    "xw" "fp-", "xw" "fp-short", "AGE-SECRET" "-KEY-", "AGE-SECRET" "-KEY-1" + "Q" * 10,
    "BEGIN " "PRIVATE KEY in prose",
    "-----BEGIN PUBLIC KEY-----", "-----BEGIN CERTIFICATE-----", "-----BEGIN PGP PUBLIC KEY BLOCK-----",
    "worker returned, branch wouf/T-27-refactor-the-authentication-module", "SECRET_KEY_BASE", "private key",
    "test_3_the_key_is_not_in_the_image_or_the_storage passed", "team/env/worker/tasks/T-1/output.log",
]


def setUpModule() -> None:
    isolate_wouf_env()


class LogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "logs"
        self.store = Path(self.tmp.name) / "secrets"
        self.env = {**os.environ, "WOUF_LOG_DIR": str(self.dir), "WOUF_SECRET_DIR": str(self.store),
                    "WOUF_SECRET_BACKEND": "file"}
        self.env.pop("WOUF_MASK", None)

    def test_task_timeline_rounds_prefixes_and_empty(self) -> None:
        for task in ("T-1", "T-1.r1", "T-12", "T-0156", "T-01567"):
            self.add("lead", task, "done", task)
        result = self.log("task", "T-1")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(result.stdout.splitlines()), 2)
        self.assertIn("T-1.r1", result.stdout)
        self.assertNotIn("T-12", result.stdout)
        self.assertNotIn("T-01567", self.log("task", "T-0156").stdout)
        empty = self.log("task", "missing")
        self.assertEqual((empty.returncode, empty.stdout), (0, "no lines for missing\n"))

    def test_task_uses_team_dir_without_log_override(self) -> None:
        team = Path(self.tmp.name) / "team"
        self.env.pop("WOUF_LOG_DIR")
        self.env["WOUF_TEAM_DIR"] = str(team)
        logs = team / "logs"
        logs.mkdir(parents=True)
        (logs / "lead.jsonl").write_text(json.dumps({"t": "2026-01-01T00:00:00+00:00", "who": "lead",
                                                   "task": "T-1", "outcome": "done", "signal": "found"}) + "\n")
        result = self.log("task", "T-1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("found", result.stdout)

    def test_task_gate_record_uses_team_dir_and_accepts_unfinished(self) -> None:
        team = Path(self.tmp.name) / "team"
        self.env["WOUF_TEAM_DIR"] = str(team)
        folder = team / "env" / "gate" / "T-1"
        folder.mkdir(parents=True)
        self.add("lead", "T-1", "done", "receipt")
        for record in ({"state": "running", "ended": None}, {"state": "running"}, [], "[" * 100000 + "]" * 100000):
            with self.subTest(record=str(record)[:40]):
                (folder / "gate.json").write_text(record if isinstance(record, str) else json.dumps(record))
                result = self.log("task", "T-1")
                self.assertEqual(result.returncode, 0, result.stderr)  # a record json cannot read is skipped
                self.assertIn("receipt", result.stdout)
                self.assertNotIn("Traceback", result.stderr)
                if isinstance(record, dict):
                    self.assertIn("running", result.stdout)

    def test_task_timeline_run_event(self) -> None:
        self.env["WOUF_EVENTS_DIR"] = str(Path(self.tmp.name) / "events")
        data = json.dumps({"bot": "worker", "runner": "fake", "tier": "strong", "model": "x",
                           "effort": "medium", "outcome": "started"})
        event = self.log("event", "add", "run.started", "--actor", "lead", "--task", "T-1", "--data", data)
        self.assertEqual(event.returncode, 0, event.stderr)
        self.assertIn("worker", self.log("task", "T-1").stdout)

    def test_task_fix_rounds_and_their_review_rounds(self) -> None:
        for task in ("T-1", "T-1-fix1", "T-1-fix2.r1", "T-1-fixer", "T-1-fix", "T-1-fix1x", "T-1.r1-fix1", "T-12-fix1"):
            self.add("lead", task, "done", task)
        listed = {line.split()[2] for line in self.log("task", "T-1").stdout.splitlines()}
        self.assertEqual(listed, {"T-1", "T-1-fix1", "T-1-fix2.r1"})
        self.assertEqual({line.split()[2] for line in self.log("task", "T-1-fix1").stdout.splitlines()}, {"T-1-fix1"})

    def test_task_does_not_read_stage_folders(self) -> None:
        team = Path(self.tmp.name) / "team"
        self.env["WOUF_TEAM_DIR"] = str(team)
        folder = team / "env" / "worker" / "tasks" / "T-1"
        folder.mkdir(parents=True)
        (folder / "run.json").write_text(json.dumps({"member": "worker", "task": "T-1"}))
        (folder / "result.json").write_text(json.dumps({"state": "done", "ended": "2026-01-02"}))
        self.add("lead", "T-1", "done", "worker returned")
        self.assertEqual(self.log("task", "T-1").stdout.splitlines()[-1].split()[1:4], ["lead", "T-1", "done"])
        self.assertEqual(len(self.log("task", "T-1").stdout.splitlines()), 1)

    def test_task_gate_rounds_and_unfinished_order(self) -> None:
        team = Path(self.tmp.name) / "team"
        self.env["WOUF_TEAM_DIR"] = str(team)
        for task, record in (("T-1", {"state": "running", "started": "2026-01-02"}),
                             ("T-1.r1", {"state": "done", "ended": "2026-01-03"}),
                             ("T-1.r2", {"state": "running"}), ("T-1.r1x", {"state": "wrong"}),
                             ("T-12", {"state": "wrong"})):
            folder = team / "env" / "gate" / task
            folder.mkdir(parents=True)
            (folder / "gate.json").write_text(json.dumps(record))
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "lead.jsonl").write_text(json.dumps({"t": "2026-01-01", "who": "lead", "task": "T-1",
                                                         "outcome": "done", "signal": "first"}) + "\n")
        lines = self.log("task", "T-1").stdout.splitlines()
        self.assertEqual(len(lines), 4)
        self.assertIn("first", lines[0])
        self.assertIn("running", lines[1])
        self.assertIn("T-1.r1", lines[2])
        self.assertIn("T-1.r2", lines[3])

    def store_secret(self, name: str, value: str) -> None:
        self.store.mkdir(mode=0o700, exist_ok=True)
        (self.store / name).write_text(value + "\n")
        known = (self.store / ".names").read_text().split() if (self.store / ".names").exists() else []
        (self.store / ".names").write_text("".join(f"{n}\n" for n in sorted({*known, name})))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def log(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(LOG), *args], env=self.env, capture_output=True, text=True,
                              timeout=20, cwd=self.tmp.name)

    def add(self, *args: str) -> None:
        result = self.log("add", *args)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_add_writes_one_json_line_per_run(self) -> None:
        self.add("wouf-worker", "T-12", "done", "tests 14/14, 3 files", "--ref", "PR 7", "--cost", "12k tok")
        lines = (self.dir / "wouf-worker.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["who"], "wouf-worker")
        self.assertEqual(entry["task"], "T-12")
        self.assertEqual(entry["outcome"], "done")
        self.assertEqual(entry["ref"], "PR 7")
        self.assertIn("T", entry["t"])

    def test_stored_values_are_masked_without_being_told(self) -> None:
        # log run by hand: it reads the store itself, so no sink (receipt, screen, index) holds a stored value
        value = "plain-stored-value-4417"
        self.store_secret("ALPHA_KEY", value)
        self.dir.mkdir(parents=True)
        (self.dir / "worker.jsonl").write_text(json.dumps(
            {"t": "2026-10-19T09:00:00.000+00:00", "who": "worker", "task": "T-1", "outcome": "done",
             "signal": f"saw {value}", "ref": "", "cost": ""}) + "\n")
        self.add("worker", "T-2", "done", value)
        for args in (("find", "result"), ("find", "saw"), ("status",), ("task", "T-1"), ("tail",)):
            result = self.log(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn(value, result.stdout + result.stderr)
        self.assertIn("saw ***", self.log("find", "saw").stdout)
        self.assertNotIn(value, (self.dir / "worker.jsonl").read_text().split("\n", 1)[1])
        self.assertNotIn(value.encode(), (self.dir / ".index.sqlite").read_bytes())

    def test_a_value_stored_after_the_index_was_built_builds_it_again(self) -> None:
        value = "plain-later-value-4418"
        self.dir.mkdir(parents=True)
        (self.dir / "worker.jsonl").write_text(json.dumps(
            {"t": "2026-10-19T09:00:00.000+00:00", "who": "worker", "task": "T-1", "outcome": "done",
             "signal": f"saw {value}", "ref": "", "cost": ""}) + "\n")
        self.log("find", "saw")
        self.assertIn(value.encode(), (self.dir / ".index.sqlite").read_bytes())
        self.store_secret("ALPHA_KEY", value)
        self.assertNotIn(value, self.log("find", "saw").stdout)
        self.assertNotIn(value.encode(), (self.dir / ".index.sqlite").read_bytes())

    def test_a_value_rotated_without_a_change_the_index_stamp_sees_builds_the_index_again(self) -> None:
        # The stamp sees the store's files, not the values in them: a rotation that keeps the file's size and time
        # (as a backend outside the folder may) must still leave no cached row with the value that is masked now.
        old, new = "plain-rotated-old-4420", "plain-rotated-new-4421"
        self.store_secret("ALPHA_KEY", old)
        self.dir.mkdir(parents=True)
        (self.dir / "worker.jsonl").write_text(json.dumps(
            {"t": "2026-10-19T09:00:00.000+00:00", "who": "worker", "task": "T-1", "outcome": "done",
             "signal": f"saw {new}", "ref": "", "cost": ""}) + "\n")
        self.log("find", "saw")
        self.assertIn(new.encode(), (self.dir / ".index.sqlite").read_bytes())
        entry = self.store / "ALPHA_KEY"
        before = entry.stat()
        entry.write_text(new + "\n")
        os.utime(entry, ns=(before.st_atime_ns, before.st_mtime_ns))
        result = self.log("find", "saw")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("saw ***", result.stdout)
        self.assertNotIn(new, result.stdout + result.stderr)
        self.assertFalse(any(new.encode() in p.read_bytes() for p in self.dir.glob(".index.sqlite*")))

    def test_keys_passed_for_one_run_never_reach_the_index_file(self) -> None:
        value = "plain-passed-value-4419"
        self.dir.mkdir(parents=True)
        (self.dir / "worker.jsonl").write_text(json.dumps(
            {"t": "2026-10-19T09:00:00.000+00:00", "who": "worker", "task": "T-1", "outcome": "done",
             "signal": f"saw {value}", "ref": "", "cost": ""}) + "\n")
        self.log("find", "saw")  # an index left by an earlier run, built before the value was known
        self.env["WOUF_MASK"] = json.dumps([value])
        result = self.log("find", "saw")
        self.assertIn("saw ***", result.stdout)
        self.assertFalse(any(value.encode() in p.read_bytes() for p in self.dir.glob(".index.sqlite*")))

    def test_the_index_names_its_sources_by_hash(self) -> None:
        name = "xox" "b-1234567890-" + "abcdefghijklmnop"
        self.dir.mkdir(parents=True)
        (self.dir / f"{name}.jsonl").write_text(json.dumps(
            {"t": "2026-10-19T09:00:00.000+00:00", "who": "worker", "task": "T-1", "outcome": "done",
             "signal": "ok", "ref": "", "cost": ""}) + "\n")
        self.assertEqual(self.log("find", "ok").returncode, 0)
        db = sqlite3.connect(self.dir / ".index.sqlite")
        try:
            stamp = db.execute("SELECT v FROM meta WHERE k = 'stamp'").fetchone()[0]
        finally:
            db.close()
        self.assertNotIn(name, stamp)
        self.assertNotIn(str(self.dir), stamp)
        self.assertNotIn(name.encode(), (self.dir / ".index.sqlite").read_bytes())

    def test_status_shows_where_each_member_stands(self) -> None:
        self.add("wouf-worker", "T-12", "started", "brief read")
        self.add("wouf-worker", "T-12", "done", "tests green")
        self.add("wouf-review", "T-12", "started", "reviewing head 1a2b3c4")
        result = self.log("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.strip().splitlines()
        self.assertEqual(len(lines), 2)
        worker = next(line for line in lines if "wouf-worker" in line)
        self.assertIn("done", worker)
        self.assertNotIn("brief read", result.stdout)

    def test_task_shows_the_timeline_across_members(self) -> None:
        self.add("wouf-worker", "T-12", "done", "tests green")
        self.add("wouf-review", "T-12", "approve", "head 1a2b3c4")
        self.add("wouf-worker", "T-13", "started", "other task")
        result = self.log("task", "T-12")
        self.assertEqual(result.stdout.count("\n"), 2)
        self.assertLess(result.stdout.index("wouf-worker"), result.stdout.index("wouf-review"))
        self.assertNotIn("T-13", result.stdout)

    def test_find_searches_the_index_and_limits_output(self) -> None:
        for n in range(30):
            self.add("wouf-worker", f"T-{n}", "done", f"migrated invoice table step {n}")
        self.add("wouf-review", "T-99", "changes", "missing rollback for the invoice migration")
        result = self.log("find", "rollback")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.strip().splitlines()), 1)
        self.assertIn("T-99", result.stdout)
        limited = self.log("find", "invoice", "--limit", "5")
        self.assertEqual(len(limited.stdout.strip().splitlines()), 5)
        self.assertTrue((self.dir / ".index.sqlite").is_file())

    def test_index_follows_new_lines(self) -> None:
        self.add("wouf-worker", "T-1", "done", "first")
        self.log("find", "first")
        self.add("wouf-worker", "T-2", "blocked", "needs vpn access")
        self.assertIn("T-2", self.log("find", "vpn").stdout)

    def test_member_filter(self) -> None:
        self.add("wouf-worker", "T-1", "done", "shared word")
        self.add("wouf-review", "T-1", "approve", "shared word")
        result = self.log("find", "shared", "--member", "wouf-review")
        self.assertIn("wouf-review", result.stdout)
        self.assertNotIn("wouf-worker", result.stdout)

    def test_refuses_secret_looking_text(self) -> None:
        token = "gh" "p_" + "a" * 36
        for signal in (token, "key " + "Zm9vYmFyYmF6cXV4cXV1eHF1dXhxdXV4cXV1eHF1dXg"):
            result = self.log("add", "wouf-worker", "T-1", "done", signal)
            self.assertNotEqual(result.returncode, 0, signal)
            self.assertIn("secret", result.stderr)
        self.assertFalse((self.dir / "wouf-worker.jsonl").exists())

    def test_shas_paths_and_branch_names_are_not_secrets(self) -> None:
        self.add("review", "T-1", "approve", "head checked", "--ref", "0123456789abcdef0123456789abcdef01234567")
        self.add("lead", "T-27-refactor-the-auth-module", "done",
                 "worker returned, branch wouf/T-27-refactor-the-authentication-module")
        self.add("lead", "-", "done", "test_3_the_key_is_not_in_the_image_or_the_storage passed")
        self.add("lead", "-", "done", "install: npm install -g @modelcontextprotocol/server-filesystem",
                 "--ref", "team/env/worker/tasks/T-1/output.log")

    def test_times_are_stored_in_utc(self) -> None:
        self.env["TZ"] = "Europe/Zurich"
        self.add("worker", "T-1", "done", "x")
        entry = json.loads((self.dir / "worker.jsonl").read_text())
        self.assertTrue(entry["t"].endswith("+00:00"), entry["t"])

    def test_add_never_appends_through_a_link(self) -> None:
        outside = self.dir.parent / "outside.jsonl"
        outside.write_text("keep\n")
        self.dir.mkdir()
        (self.dir / "lead.jsonl").symlink_to(outside)
        result = self.log("add", "lead", "T-1", "done", "receipt")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("link", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(outside.read_text(), "keep\n")

    def test_a_bot_cannot_write_receipts_for_another_member(self) -> None:
        bot_logs = self.dir.parent / "env" / "worker" / "logs"
        bot_logs.mkdir(parents=True)
        forged = {"t": "2026-10-19T09:00:00.000+00:00", "task": "T-1", "outcome": "approve", "signal": "approved",
                  "ref": "", "cost": ""}
        (bot_logs / "review.jsonl").write_text(json.dumps({**forged, "who": "review"}) + "\n")
        (bot_logs / "worker.jsonl").write_text(
            json.dumps({**forged, "who": "review"}) + "\n" +
            json.dumps({**forged, "who": "worker", "outcome": "done", "signal": "real\x1b[2Jline"}) + "\n")
        result = self.log("task", "T-1")
        self.assertNotIn("review", result.stdout)
        self.assertIn("worker T-1 done", result.stdout)
        self.assertNotIn("\x1b", result.stdout)

    def test_odd_lines_in_a_bot_log_do_not_break_reading(self) -> None:
        bot_logs = self.dir.parent / "env" / "worker" / "logs"
        bot_logs.mkdir(parents=True)
        (bot_logs / "worker.jsonl").write_text(
            json.dumps({"t": 1, "who": "worker", "task": "T-1", "outcome": "done", "signal": {"a": 1}}) + "\n")
        self.add("lead", "T-1", "started", "dispatched worker")
        for args in (("task", "T-1"), ("tail",), ("find", "dispatched"), ("status",)):
            result = self.log(*args)
            self.assertEqual(result.returncode, 0, f"{args}: {result.stderr}")

    def test_the_lead_logs_as_wouf(self) -> None:
        self.add("wouf", "T-1", "started", "dispatched wouf-worker")
        self.assertIn("wouf T-1 started", self.log("status").stdout)

    def test_rejects_bad_input(self) -> None:
        self.assertNotEqual(self.log("add", "Worker", "T-1", "done", "x").returncode, 0)
        self.assertNotEqual(self.log("add", "wouf-worker", "T-1", "finished", "x").returncode, 0)
        self.assertNotEqual(self.log("add", "wouf-worker", "T-1", "done", "x" * 200).returncode, 0)

    def test_reserved_task_ids_are_refused(self) -> None:
        for task in (".", "..", ".hidden", "-x", "-1", "T-1\n", "a/b", "x" * 41, ""):
            with self.subTest(task=task):
                result = self.log("add", "worker", task, "done", "x")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("task", result.stderr)
        self.assertFalse((self.dir / "worker.jsonl").exists())
        self.add("lead", "-", "done", "no task")
        self.add("lead", "_x.1", "done", "underscore start")

    def test_a_task_id_that_differs_only_in_case_is_refused(self) -> None:
        self.add("lead", "T-1", "started", "dispatched worker")
        result = self.log("add", "worker", "t-1", "done", "x")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("differs only in case from the existing task 'T-1'", result.stderr)
        self.add("worker", "T-1", "done", "same id is fine")
        self.assertNotIn("t-1", self.log("tail").stdout)

    def test_a_case_twin_of_a_task_folder_is_refused_without_any_receipt(self) -> None:
        # team/logs sits next to team/env and team/flows; no receipt exists for these tasks.
        team = Path(self.tmp.name)
        for kind, folder in (("bot stage", team / "env" / "worker" / "tasks" / "T-77"),
                             ("gate", team / "env" / "gate" / "T-77"), ("flow", team / "flows" / "T-77")):
            with self.subTest(kind=kind):
                folder.mkdir(parents=True)
                result = self.log("add", "lead", "t-77", "started", "dispatch")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("task 't-77' differs only in case from the existing task 'T-77'", result.stderr)
                self.assertFalse((self.dir / "lead.jsonl").exists())
                self.add("lead", "T-77", "started", "the exact id is fine")
                (self.dir / "lead.jsonl").unlink()
                shutil.rmtree(folder)
        self.add("lead", "t-77", "started", "no folder, no twin")

    def test_task_ids_that_are_no_git_branch_or_a_windows_device_are_refused(self) -> None:
        for task in ("T-1.", "T..1", "T.lock", "T.LOCK", "CON", "con", "PRN", "AUX", "NUL", "COM1", "COM9", "LPT1",
                     "LPT9", "CON.txt", "nul.1", "com1.scout", "Aux.r1"):
            with self.subTest(task=task):
                result = self.log("add", "worker", task, "done", "x")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(f"task {task!r}", result.stderr)
        self.assertFalse((self.dir / "worker.jsonl").exists())
        for task in ("T-1.lockfile", "T.1", "CONSOLE", "COM10", "LPT0", "nulls", "x.con"):
            self.add("worker", task, "done", "a valid id")

    def test_a_trailing_dot_alias_of_an_existing_task_is_refused(self) -> None:
        # On Windows T-1. and T-1 are one folder; git refuses the first as a branch name.
        self.add("lead", "T-1", "started", "dispatch")
        result = self.log("add", "lead", "T-1.", "done", "x")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(len((self.dir / "lead.jsonl").read_text().splitlines()), 1)

    def test_a_key_in_the_task_or_member_field_is_refused_and_never_written(self) -> None:
        slack = "xox" "b-12345-" + "abcdefghijklmn"
        for args, label, kind in ((("worker", AWS), "task id", "AWS access key"),
                                  (("worker", GITHUB_UNDERSCORE), "task id", "GitHub classic"),
                                  (("worker", SLACK_WORKFLOW), "task id", "Slack token"),
                                  ((slack, "T-1"), "member id", "Slack token")):
            with self.subTest(kind=kind, field=label):
                result = self.log("add", *args, "done", "x")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(f"{label} looks like it contains a secret ({kind})", result.stderr)
                for value in args:
                    if value not in ("worker", "T-1"):
                        self.assertNotIn(value, result.stderr)
        self.assertEqual(list(self.dir.glob("*.jsonl")) if self.dir.exists() else [], [])
        self.assertNotIn(AWS, self.log("status").stdout)

    def test_member_ids_follow_the_ref_and_portability_rules_of_task_ids(self) -> None:
        for who in ("aux", "con", "com1", "lpt9", "nul", "a--b", "worker-", "Worker", "w", "x" * 32, "a.b", "-x",
                    "../w", "worker\n"):
            with self.subTest(who=who):
                result = self.log("add", who, "T-1", "done", "x")
                self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(list(self.dir.glob("*.jsonl")) if self.dir.exists() else [], [])
        for who in ("lead", "worker", "review", "a-b", "bot-2"):
            self.add(who, "T-1", "done", "a valid id")

    def write_receipts(self, path: Path, who: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = "2026-10-19T09:00:0{}.000+00:00"
        lines = [{"t": stamp.format(0), "who": who, "task": AWS, "outcome": "done", "signal": "id " + AWS,
                  "ref": "", "cost": ""},
                 {"t": stamp.format(1), "who": who, "task": "T-5", "outcome": "done",
                  "signal": "workflow " + SLACK_WORKFLOW + " and " + GITHUB_UNDERSCORE, "ref": PEM, "cost": ""},
                 {"t": stamp.format(2), "who": who, "task": "T-5", "outcome": "done", "signal": "key follows",
                  "ref": "see\n" + PEM + "\nend", "cost": AWS}]
        path.write_text("".join(json.dumps(line) + "\n" for line in lines))

    def assert_masked(self, text: str) -> None:
        for value in (AWS, SLACK_WORKFLOW, GITHUB_UNDERSCORE, *PEM_BODY, "PRIVATE KEY"):
            self.assertNotIn(value, text)

    def test_keys_in_receipts_that_never_passed_add_are_masked_before_screen_and_index(self) -> None:
        # A bot's own file and a receipt file copied in from another team: neither went through log add.
        for path, who in ((self.dir.parent / "env" / "worker" / "logs" / "worker.jsonl", "worker"),
                          (self.dir / "imported.jsonl", "imported")):
            with self.subTest(file=str(path.relative_to(self.dir.parent))):
                self.write_receipts(path, who)
                outputs = [self.log(*args) for args in (("status",), ("task", "T-5"), ("tail",), ("tail", who),
                                                         ("find", "workflow"), ("find", "key"))]
                for result in outputs:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assert_masked(result.stdout)
                self.assertIn("***", outputs[0].stdout)
                self.assertIn(f"{who} T-5 done | key follows | see *** end | ~***", outputs[2].stdout)
                self.assertIn("workflow *** and ***", outputs[4].stdout)
                for word in (AWS, SLACK_WORKFLOW, "SYNTHETICBODYLINE1" + "Ab0"):
                    self.assertEqual(self.log("find", word).stdout, "", word)
                db = sqlite3.connect(self.dir / ".index.sqlite")
                try:
                    self.assert_masked(json.dumps(db.execute("SELECT * FROM log").fetchall()))
                finally:
                    db.close()
                path.unlink()

    def test_an_index_built_before_masking_is_built_again(self) -> None:
        self.write_receipts(self.dir / "imported.jsonl", "imported")
        self.log("find", "workflow")
        db = sqlite3.connect(self.dir / ".index.sqlite")
        stamp = db.execute("SELECT v FROM meta WHERE k = 'stamp'").fetchone()[0]
        # What an older reader left: raw rows under a stamp without the reader's marker.
        db.execute("INSERT INTO log VALUES ('2026-10-19T09:00:09.000+00:00', 'imported', 'T-9', 'done', ?, '', '')",
                   ("stale " + AWS,))
        db.execute("UPDATE meta SET v = ? WHERE k = 'stamp'", (stamp.split(";", 1)[1],))
        db.commit()
        db.close()
        self.assertEqual(self.log("find", "stale").stdout, "")


class TaskIdBlockTest(unittest.TestCase):
    def test_team_and_log_carry_the_same_block(self) -> None:
        log = pattern_block(LOG, TASK_START, TASK_END)
        for name in ("TASK = ", "DEVICE = ", "def task_problem", "def case_twin"):
            self.assertIn(name, log)
        self.assertEqual(log, pattern_block(TEAM, TASK_START, TASK_END),
                         "keep the task id block identical in .agents/bin/team and .agents/bin/log")

    @unittest.skipUnless(shutil.which("git"), "needs git, which is not installed")
    def test_every_accepted_id_is_a_git_branch_name_and_no_device(self) -> None:
        module = helper(LOG)
        samples = ["T-1", "T-1.", "T..1", "T.lock", "t.LoCk", "T-1.lockfile", "_x.1", "a", "x" * 40, "T-1.scout",
                   "T-1.r2", "a.b.c", "CON", "con.txt", "COM1", "lpt9.x", "COM10", "LPT0", "CONSOLE", "x.con",
                   "@", "a@{b", "T 1", "T\t1", "a~b", "a^b", "a:b", "a?b", "a*b", "a[b", "a\\b", "a/b", "-a", ".a",
                   "a.", "x" * 41, "T-1\n"]
        for task in samples:
            with self.subTest(task=task):
                ok = module.task_problem(task) is None
                if ok:
                    for ref in (f"refs/heads/wouf/{task}", f"refs/heads/task/{task}", f"refs/wouf/gate-{task}"):
                        self.assertEqual(subprocess.run(["git", "check-ref-format", ref]).returncode, 0, ref)
                    self.assertIsNone(module.DEVICE.fullmatch(task.split(".")[0]))
                else:
                    self.assertTrue(task in ("T-1.", "T..1", "T.lock", "t.LoCk", "a.") or
                                    module.DEVICE.fullmatch(task) or not module.TASK.fullmatch(task), task)
        for task in ("T-1.", "T..1", "T.lock"):
            self.assertNotEqual(subprocess.run(["git", "check-ref-format", f"refs/heads/wouf/{task}"]).returncode, 0)


class SecretPatternsTest(unittest.TestCase):
    """One list of anchored patterns, the same block in team and log."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.helpers = {"log": helper(LOG), "team": helper(TEAM)}

    def test_team_and_log_carry_the_same_block(self) -> None:
        log, team = pattern_block(LOG), pattern_block(TEAM)
        self.assertIn("SECRET_PATTERNS", log)
        self.assertEqual(log, team, "keep the secret pattern block identical in .agents/bin/team and .agents/bin/log")

    def test_every_kind_is_found_and_named(self) -> None:
        for name, module in self.helpers.items():
            kinds = [kind for kind, _ in module.SECRET_PATTERNS]
            for kind, samples in HITS.items():
                self.assertIn(kind, kinds)
                for sample in samples:
                    with self.subTest(helper=name, kind=kind, sample=sample[:20]):
                        self.assertEqual(module.secret_in(sample), kind)

    def test_words_paths_and_commit_ids_are_not_secrets(self) -> None:
        for name, module in self.helpers.items():
            for sample in NON_HITS:
                with self.subTest(helper=name, sample=sample):
                    self.assertIsNone(module.secret_in(sample))

    def test_every_kind_is_masked_whole(self) -> None:
        for name, module in self.helpers.items():
            for kind, samples in HITS.items():
                for sample in samples:
                    with self.subTest(helper=name, kind=kind, sample=sample[:20]):
                        masked = module.mask_secrets(sample)
                        self.assertIn("***", masked)
                        self.assertIsNone(module.secret_in(masked))
            masked = module.mask_secrets("before\n" + PEM + "\nafter " + AWS + " and a passed value", ["passed value"])
            self.assertEqual(masked, "before\n***\nafter *** and a ***")
            # Without its END line: the header and the key lines under it, not the prose that follows.
            self.assertEqual(module.mask_secrets(PEM.rsplit("\n", 1)[0] + "\nnext line here"), "***\nnext line here")
            for sample in NON_HITS:
                self.assertEqual(module.mask_secrets(sample), sample)

    def test_a_passed_value_that_overlaps_a_key_cannot_leave_its_body(self) -> None:
        for name, module in self.helpers.items():
            with self.subTest(helper=name):
                # The value takes the key's prefix, its suffix or its PEM header: the key is still one mask.
                self.assertEqual(module.mask_secrets(AWS + " end", ["AKIAIOSF"]), "*** end")
                self.assertEqual(module.mask_secrets(AWS + " end", ["EXAMPL0 end"]), "***")
                self.assertEqual(module.mask_secrets(PEM + "\nafter", ["-----BEGIN RSA"]), "***\nafter")
                self.assertEqual(module.mask_secrets("id " + AWS, ["id AKIA", "IOSFODNN"]), "***")

    def test_log_refuses_each_kind_and_names_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "WOUF_LOG_DIR": tmp}
            for kind, samples in HITS.items():
                result = subprocess.run([sys.executable, str(LOG), "add", "worker", "T-1", "done", "x",
                                         "--ref", samples[0]], env=env, capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 2, kind)
                self.assertIn(f"ref looks like it contains a secret ({kind})", result.stderr)
                self.assertNotIn(samples[0], result.stderr)
            self.assertFalse((Path(tmp) / "worker.jsonl").exists())


class SafeJsonTest(unittest.TestCase):
    def test_no_serializers_outside_safe_json(self):
        import ast
        tree = ast.parse(LOG.read_text())
        def visit(node, owner=None):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                owner = node.name
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == 'json'
                    and node.func.attr in ('dump', 'dumps')):
                self.assertEqual(owner, 'safe_json', f'direct JSON serializer at {node.lineno}')
            for child in ast.iter_child_nodes(node):
                visit(child, owner)
        visit(tree)

    def test_raw_keys_values_and_syntax_are_checked(self):
        from unittest.mock import patch
        log = helper(LOG)
        for value in ('hits', 'true', '":"', '"'):
            with self.subTest(value=value), patch.object(log, 'REGISTERED', [value]):
                result = log.safe_json({value: [value, {'nested': value}]})
                if result is not None:
                    json.loads(result)
                    self.assertNotIn(value, result)
                if value == 'true':
                    self.assertIsNone(log.safe_json(True))
                elif value in ('"', '":"'):
                    self.assertIsNone(log.safe_json({'a': 'b'}))

    def test_ask_json_registered_literals(self):
        from unittest.mock import patch
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as tmp:
            team = Path(tmp)
            (team / 'knowledge').mkdir()
            (team / 'knowledge' / 'a.md').write_text('# Heading\nneedle ordinary words')
            for value in ('hits', 'true', '":"', '"'):
                with self.subTest(value=value), patch.dict(os.environ, WOUF_LOG_DIR=str(team / 'logs'),
                        WOUF_EVENTS_DIR=str(team / 'events'), WOUF_KNOWLEDGE_DB='',
                        WOUF_MASK=json.dumps([value])):
                    log = helper(LOG)
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        log.cmd_ask(['needle', '--json'])
                    json.loads(output.getvalue())
                    self.assertNotIn(value, output.getvalue())


class ReceiptEventTest(unittest.TestCase):
    """receipt.signed and receipt.resigned (docs/RECEIPTS.md): exactly task, landed, reviewed and sha256."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.events = Path(tmp.name) / "events"
        env = patch.dict(os.environ, WOUF_EVENTS_DIR=str(self.events), WOUF_LOG_DIR=str(Path(tmp.name) / "logs"),
                         WOUF_MASK="[]")
        env.start()
        self.addCleanup(env.stop)
        self.log = helper(LOG)

    @staticmethod
    def data(**changes) -> dict:
        data = {"task": "T-1", "landed": "a" * 40, "reviewed": "b" * 40, "sha256": "c" * 64, **changes}
        return {k: v for k, v in data.items() if v is not ...}

    def lines(self) -> list[bytes]:
        return [line for path in self.events.glob("*.jsonl") for line in path.read_bytes().splitlines()]

    def test_both_kinds_take_exactly_their_four_keys(self) -> None:
        for kind in ("receipt.signed", "receipt.resigned"):
            event = self.log.event_append(kind, "lead", "T-1", self.data())
            self.assertEqual(event["data"], self.data())
        done = subprocess.run([sys.executable, str(LOG), "event", "add", "receipt.signed", "--actor", "lead",
                               "--task", "T-1", "--data", json.dumps(self.data(landed="0" * 40))],
                              capture_output=True, text=True, timeout=20)
        self.assertEqual(done.returncode, 0, done.stderr)
        events, bad = self.log.event_read(next(self.events.glob("*.jsonl")))
        self.assertEqual(([e["kind"] for e in events], bad), (["receipt.signed", "receipt.resigned",
                                                               "receipt.signed"], 0))

    def test_other_keys_and_values_that_are_not_hex_are_refused(self) -> None:
        bad = [self.data(task=...), self.data(landed=...), self.data(reviewed=...), self.data(sha256=...),
               self.data(head="a" * 40), self.data(sig="x"), {}]
        for key, size in (("landed", 40), ("reviewed", 40), ("sha256", 64)):
            bad += [self.data(**{key: value}) for value in ("a" * (size - 1), "a" * (size + 1), "A" * size,
                                                              "g" * size, " " + "a" * (size - 1), 1, None,
                                                              ["a" * size], "a" * (104 - size))]
        bad += [self.data(task="../x"), self.data(task=1), self.data(task="")]
        for kind in ("receipt.signed", "receipt.resigned"):
            for data in bad:
                with self.subTest(kind=kind, data=data), self.assertRaises(SystemExit):
                    self.log.event_append(kind, "lead", "T-1", data)
        self.assertEqual(self.lines(), [])
        done = subprocess.run([sys.executable, str(LOG), "event", "add", "receipt.resigned", "--actor", "lead",
                               "--task", "T-1", "--data", json.dumps(self.data(extra="a" * 40))],
                              capture_output=True, text=True, timeout=20)
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(self.lines(), [])

    def test_a_stored_receipt_event_with_another_key_is_malformed_when_read(self) -> None:
        self.log.event_append("receipt.signed", "lead", "T-1", self.data())
        path = next(self.events.glob("*.jsonl"))
        event = json.loads(path.read_bytes())
        event.update(seq=2, id=f"{event['origin']}-2", data={**event["data"], "branch": "main"})
        with path.open("a") as handle:
            handle.write(json.dumps(event, ensure_ascii=True, separators=(",", ":")) + "\n")
        events, bad = self.log.event_read(path)
        self.assertEqual((len(events), bad), (1, 1))
        with self.assertRaises(ValueError):
            self.log.event_validate("receipt.signed", "lead", "T-1", self.data(sha256="c" * 40), "local")


    def test_a_gate_flow_step_takes_a_head_and_base_only_as_hex(self) -> None:
        # attest.flow_gate binds a gate to its head through these keys: only a commit id is written or read back.
        sha1, sha256 = "a" * 40, "b" * 64
        for data in ({"kind": "gate", "status": "green", "head": sha1, "base": sha1},
                     {"kind": "gate", "status": "green", "head": sha256, "base": sha256},
                     {"kind": "gate", "status": "green", "head": sha1}, {"kind": "gate", "status": "green"}):
            self.assertEqual(self.log.event_append("flow.step", "lead", "T-1", data)["data"], data)
        for key in ("head", "base"):
            for value in ("a" * 39, "a" * 41, "a" * 63, "A" * 40, "g" * 40, " " + "a" * 39, "unknown", "", 1,
                          None, ["a" * 40]):
                with self.subTest(key=key, value=value), self.assertRaises(SystemExit):
                    self.log.event_append("flow.step", "lead", "T-1", {"kind": "gate", "status": "green",
                                                                       key: value})
        self.assertEqual(len(self.lines()), 4)
        path = next(self.events.glob("*.jsonl"))
        event = json.loads(path.read_bytes().splitlines()[-1])
        event.update(seq=5, id=f"{event['origin']}-5", data={**event["data"], "head": "HEAD"})
        with path.open("a") as handle:
            handle.write(json.dumps(event, ensure_ascii=True, separators=(",", ":")) + "\n")
        events, bad = self.log.event_read(path)
        self.assertEqual((len(events), bad), (4, 1))


if __name__ == "__main__":
    unittest.main()
