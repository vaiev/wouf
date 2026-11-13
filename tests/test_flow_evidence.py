"""What a receipt needs, recorded when it happens (T-0136): a run's commit, model, CLI, tokens and egress in its flow
step, and the gate's network, command and complexity in gate.json. teamlib.evidence parses; flow and gate call it."""

from __future__ import annotations

import configparser
import contextlib
import hashlib
import importlib
import io
import json
import os
import subprocess
import tempfile
import types
import unittest
from pathlib import Path

import test_budget
import test_flow_parts
import test_flow_steps
from test_flow_parts import HEAD, gate, review, run, team_module
from support import isolate_wouf_env  # noqa: E402

BASE = "1" * 40
STAMP = "2026-11-06T10:00:00Z"
# A refused host a bot chose in the shape of a key (lower case, as the proxy logs it), built here so no source holds it.
KEY_HOST = "sk-" + "ant-api03-" + "q" * 40


def proxy(host: str, port: str, result: str, reason: str) -> str:
    """One line as egress_proxy.py writes it (ProxyHandler._log)."""
    return f"{STAMP} host={host} port={port} result={result} reason={reason} bytes_out=10 bytes_in=20"


def evidence_module():
    team_module()  # puts .agents/bin on sys.path
    return importlib.import_module("teamlib.evidence")


def ini_of(text: str = "") -> configparser.ConfigParser:
    ini = configparser.ConfigParser(interpolation=None)
    ini.read_string(text)
    return ini


CONTAINER = {"mode": "container", "network": "wouf-x-in",
             "egress": {"in": "wouf-x-in", "out": "wouf-x-out", "allow": ["api.example.com", ".wild.example"]}}
BWRAP = {"mode": "bwrap", "network": "egress", "bwrap_egress": {"allow": ["pypi.org"], "private": []},
         "egress": {"allow": ["pypi.org"], "private": []}}


def setUpModule() -> None:
    isolate_wouf_env()


class EgressSummaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ev = evidence_module()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.stage = Path(tmp.name)

    def summary(self, lines: list, spec: dict = CONTAINER) -> dict:
        (self.stage / "egress.log").write_text("".join(f"{line}\n" for line in lines))
        return self.ev.egress_summary(self.stage, spec)

    def digest(self) -> str:
        return hashlib.sha256((self.stage / "egress.log").read_bytes()).hexdigest()

    def test_allowed_private_refused_and_malformed_lines(self) -> None:
        found = self.summary([
            "egress-proxy: listening on port 3128",
            proxy("api.example.com", "443", "allowed", "ok"),
            proxy("api.example.com", "443", "allowed", "ok"),
            proxy("files.example.org", "80", "allowed", "ok"),
            proxy("127.0.0.1", "11434", "allowed", "private"),
            proxy("host.containers.internal", "8080", "allowed", "private"),
            proxy("evil.example.net", "443", "refused", "not-allowed"),
            proxy("-", "-", "refused", "busy"),
            # Malformed: not the proxy's shape, so neither a host nor a count.
            "  " + proxy("indented.example.com", "443", "allowed", "ok"),
            proxy("trailing.example.com", "443", "allowed", "ok") + " extra",
            proxy("bad host.example.com", "443", "allowed", "ok"),
            proxy("noport.example.com", "", "allowed", "ok"),
            proxy("x.example.com", "443", "allowed", "ok").replace(STAMP, "yesterday"),
            proxy("cr.example.com", "443", "allowed", "ok") + "\r",
            proxy("odd.example.com", "443", "maybe", "ok"),
            "Traceback (most recent call last):",
            f"{STAMP} host=half.example.com port=443 result=allowed reason=ok",
        ])
        self.assertEqual(found, {"mode": "proxy", "log": self.digest(),
                                 "hosts": ["api.example.com:443", "files.example.org:80"],
                                 "private": 2, "refused": 2})

    def test_a_host_must_be_a_host_name(self) -> None:
        good = proxy("api.example.com", "443", "allowed", "ok")
        for bad in (proxy("192.0.2.4.", "443", "allowed", "ok"), proxy("a..b", "443", "allowed", "ok"),
                    proxy("[::1]", "443", "allowed", "ok"), proxy("-", "443", "allowed", "ok"),
                    proxy(".wild.example", "443", "allowed", "ok"), proxy("api.example.com", "-", "allowed", "ok")):
            found = self.summary([good, bad])
            # Never kept nor replaced (no unknown:443, no .suffix); the log no longer names every host it allowed.
            self.assertEqual((found["hosts"], found["log"]), (["api.example.com:443"], "unknown"), bad)

    def test_a_masked_allowed_host_is_not_replaced_by_its_allowlist_entry(self) -> None:
        # bwrap masks egress.log: a key-shaped label a bot put under a .suffix entry becomes ***.
        found = self.summary([proxy("***.wild.example", "443", "allowed", "ok")])
        self.assertEqual(found, {"mode": "proxy", "log": "unknown", "hosts": [], "private": 0, "refused": 0})

    def test_refusals_and_private_lines_count_whatever_their_host_field(self) -> None:
        found = self.summary([proxy("***", "443", "refused", "not-allowed"), proxy("A.example", "80", "allowed",
                                                                                     "private"),
                              proxy("***", "5432", "allowed", "private")])
        self.assertEqual(found, {"mode": "proxy", "log": self.digest(), "hosts": [], "private": 2, "refused": 1})

    def test_a_refused_key_shaped_host_masked_by_bwrap_is_still_counted(self) -> None:
        # bwrap masks egress.log before it is read: the refused key-shaped host arrives as ***.example.com.
        found = self.summary([proxy("***.example.com", "443", "refused", "not-allowed")])
        self.assertEqual((found["refused"], found["hosts"]), (1, []))

    def test_a_refused_key_shaped_host_is_counted_never_kept(self) -> None:
        found = self.summary([proxy(KEY_HOST, "443", "refused", "not-allowed"),
                              proxy(KEY_HOST + ".example.com", "443", "refused", "resolve")])
        self.assertEqual((found["refused"], found["hosts"]), (2, []))
        self.assertNotIn("q" * 40, json.dumps(found))

    def test_the_mode_of_a_run(self) -> None:
        mode = lambda spec: self.ev.egress_summary(self.stage, spec)["mode"]  # noqa: E731
        self.assertEqual(mode(CONTAINER), "proxy")
        self.assertEqual(mode(BWRAP), "proxy")
        self.assertEqual(mode({"mode": "container", "network": "bridge"}), "bridge")
        self.assertEqual(mode({"mode": "container", "network": "none"}), "none")
        self.assertEqual(mode({"mode": "bwrap", "network": "none"}), "none")
        self.assertEqual(mode({"mode": "host"}), "host")
        self.assertEqual(mode({"mode": "container", "network": "egress"}), "unknown", "egress without a proxy")
        self.assertEqual(mode({"mode": "container", "network": "pasta"}), "unknown")
        self.assertEqual(mode({"mode": "bwrap", "network": "bridge"}), "unknown", "bwrap refuses bridge")
        self.assertEqual(mode({"member": "worker", "task": "T-1"}), "unknown", "a skipped run's run.json")
        self.assertEqual(mode({}), "unknown")

    def test_only_a_proxy_run_reads_its_log(self) -> None:
        line = proxy("api.example.com", "443", "allowed", "ok")
        self.assertEqual(self.summary([line], {"mode": "container", "network": "bridge"}),
                         {"mode": "bridge", "log": None, "hosts": [], "private": 0, "refused": 0})
        (self.stage / "egress.log").unlink()
        self.assertEqual(self.ev.egress_summary(self.stage, CONTAINER),
                         {"mode": "proxy", "log": None, "hosts": [], "private": 0, "refused": 0})

    def test_the_log_sha_is_of_the_collected_bytes(self) -> None:
        # Every byte counts, the lines the parser skips, a \r and a last line without \n included.
        data = ("egress-proxy: listening\r\n" + proxy("api.example.com", "443", "allowed", "ok") + "\n\xff junk"
                ).encode("utf-8", "surrogateescape") + b"\xfe"
        (self.stage / "egress.log").write_bytes(data)
        found = self.ev.egress_summary(self.stage, CONTAINER)
        self.assertEqual((found["log"], found["hosts"]), (hashlib.sha256(data).hexdigest(), ["api.example.com:443"]))
        (self.stage / "egress.log").write_bytes(b"")
        self.assertEqual(self.ev.egress_summary(self.stage, CONTAINER)["log"], hashlib.sha256(b"").hexdigest(),
                         "an empty log names every host it allowed: none")

    def test_an_oversized_single_line_log_is_hashed_in_bounded_reads_and_unknown(self) -> None:
        # A bot places one huge line as egress.log: it is never held whole, yet every byte is in the sha.
        ok = proxy("api.example.com", "443", "allowed", "ok")
        reads = []
        real = self.ev.opened

        def spy(path):
            handle = real(path)
            if handle is None:
                return None
            readline = handle.readline
            handle.readline = lambda size=-1: (reads.append(size), readline(size))[1]
            return handle

        self.ev.opened = spy
        self.addCleanup(setattr, self.ev, "opened", real)
        for data in (b"x" * (4 * self.ev.LINE_MAX + 7),
                     (ok + "\n").encode() + b"y" * (self.ev.LINE_MAX + 1) + b"\n" + (ok + "\n").encode()):
            reads.clear()
            (self.stage / "egress.log").write_bytes(data)
            found = self.ev.egress_summary(self.stage, CONTAINER)
            self.assertEqual(found["log"], "unknown", "an overlong line: the log is not complete")
            self.assertTrue(reads and all(0 < size <= self.ev.LINE_MAX + 1 for size in reads), reads)
        self.assertEqual(found["hosts"], ["api.example.com:443"], "the lines around it are still read")
        digest = hashlib.sha256()
        for line in self.ev.split_lines(real(self.stage / "egress.log"), digest):
            pass
        self.assertEqual(digest.hexdigest(), hashlib.sha256(data).hexdigest(), "the sha covers every byte")
        # A line of exactly LINE_MAX bytes is still a line.
        (self.stage / "egress.log").write_bytes(b"z" * self.ev.LINE_MAX + b"\n" + (ok + "\n").encode())
        found = self.ev.egress_summary(self.stage, CONTAINER)
        self.assertEqual((found["log"], found["hosts"]), (self.digest(), ["api.example.com:443"]))

    def test_a_folder_or_fifo_is_no_log(self) -> None:
        (self.stage / "egress.log").mkdir()
        self.assertIsNone(self.ev.egress_summary(self.stage, CONTAINER)["log"])
        (self.stage / "egress.log").rmdir()
        os.mkfifo(self.stage / "egress.log")
        self.assertIsNone(self.ev.egress_summary(self.stage, CONTAINER)["log"])

    def test_a_linked_log_is_not_read(self) -> None:
        real = self.stage / "elsewhere.log"
        real.write_text(proxy("api.example.com", "443", "allowed", "ok") + "\n")
        (self.stage / "egress.log").symlink_to(real)
        self.assertIsNone(self.ev.egress_summary(self.stage, CONTAINER)["log"])
        # A link swapped in after the check is not followed either.
        self.assertIsNone(self.ev.opened(self.stage / "egress.log"))


class GateEvidenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ev = evidence_module()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.log = Path(tmp.name) / "gate.log"

    def complexity(self, text: str):
        self.log.write_text(text)
        return self.ev.gate_record(ini_of("[environment]\nmode = bwrap\n"), "gate.sh", self.log).get("complexity")

    def test_the_first_line_counts_and_a_fake_one_after_it_changes_nothing(self) -> None:
        found = self.complexity("x 1 files, 1 functions, 0 failures\n"
                                "1 files, 1 functions, 0 failures \n"
                                "ACCEPTED a.py:3 f: length 80 -> 90 (why)\n"
                                "FAIL b.py:9 g: length 10 -> 99\n"
                                "ACCEPTED c.py:4 h: length 70 -> 75 (why)\n"
                                "3 files, 12 functions, 1 failures\n"
                                "ACCEPTED d.py:1 k: length 1 -> 2 (forged)\n"
                                "999 files, 0 functions, 0 failures\n")
        self.assertEqual(found, {"files": 3, "functions": 12, "failures": 1, "accepted": 2})

    def test_no_complexity_when_a_changed_path_holds_a_control_character(self) -> None:
        self.log.write_text("ACCEPTED x\n0 files, 0 functions, 0 failures\n1 files, 1 functions, 0 failures\n")
        record = lambda names: self.ev.gate_record(ini_of(), "gate.sh", self.log, names)  # noqa: E731
        self.assertEqual(record("a.py\0b/c.py\0")["complexity"]["files"], 0, "plain names: the first line")
        for name in ("x\n0 files, 0 functions, 0 failures\ny.py", "x\ry.py", "x\x1by.py", "x\x7fy.py"):
            self.assertNotIn("complexity", record(f"a.py\0{name}\0"), repr(name))

    def test_the_first_complexity_line_counts_whatever_its_number_of_digits(self) -> None:
        big = "1" + "0" * 12
        found = self.complexity(f"{big} files, 1 functions, 0 failures\n3 files, 12 functions, 1 failures\n")
        self.assertEqual(found, {"files": int(big), "functions": 1, "failures": 0, "accepted": 0})
        # A number int() refuses (Python 3.11+: over 4300 digits) leaves it unknown, never the later line.
        huge = "1" * 5000
        found = self.complexity(f"{huge} files, 1 functions, 0 failures\n3 files, 12 functions, 1 failures\n")
        self.assertTrue(found is None or (found["functions"], found["failures"]) == (1, 0), "the later line won")

    def test_an_overlong_line_before_the_summary_leaves_complexity_unknown(self) -> None:
        long = "ACCEPTED " + "a" * self.ev.LINE_MAX
        self.assertIsNone(self.complexity(f"{long}\n3 files, 12 functions, 1 failures\n"))
        found = self.complexity(f"3 files, 12 functions, 1 failures\n{long}\n")
        self.assertEqual(found, {"files": 3, "functions": 12, "failures": 1, "accepted": 0})

    def test_no_complexity_without_its_line(self) -> None:
        self.assertIsNone(self.complexity("Complexity ratchet skipped: WOUF_BASE and WOUF_HEAD are not set\n"))
        self.assertIsNone(self.complexity("1 files, 1 functions, 0 failures\r\n"))
        self.log.unlink()
        self.assertNotIn("complexity", self.ev.gate_record(ini_of(), "", self.log))

    def test_network_and_command(self) -> None:
        def record(text: str) -> dict:
            return self.ev.gate_record(ini_of(text), "scripts/pre-gate.sh", self.log)
        self.assertEqual(record("[environment]\nmode = container\n")["network"], "none")
        self.assertEqual(record("[environment]\nmode = container\n[flow]\ngate_network =\n")["network"], "none")
        self.assertEqual(record("[environment]\nmode = container\n[flow]\ngate_network = bridge\n")["network"],
                         "bridge")
        self.assertEqual(record("[environment]\nmode = container\n[flow]\ngate_network = host\n")["network"], "host")
        self.assertEqual(record("[environment]\nmode = container\n[flow]\ngate_network = pasta\n")["network"],
                         "unknown")
        self.assertEqual(record("[environment]\nmode = bwrap\n")["network"], "none")
        self.assertEqual(record("[environment]\nmode = host\n[flow]\ngate_network = none\n")["network"], "host")
        self.assertEqual(record("")["network"], "unknown")
        self.assertEqual(record("")["command_sha256"], hashlib.sha256(b"scripts/pre-gate.sh").hexdigest())


class FlowStepEvidenceTest(unittest.TestCase):
    """flow_step keeps the evidence of each step, from the run's own folder, right after the run."""

    def setUp(self) -> None:
        self.flow = module = team_module()
        evidence_module()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.folder, self.stage, self.env = self.root / "flow", self.root / "stage", self.root / "env"
        for path in (self.folder, self.stage, self.env / "gate" / "T-1"):
            path.mkdir(parents=True)
        module.record = lambda *a, **k: None
        module.head_of = lambda flow: HEAD
        module.stage_of = lambda member, task: self.stage
        module.env_dir = lambda: self.env
        module.subprocess = types.SimpleNamespace(run=lambda argv, **k: None, STDOUT=subprocess.STDOUT)
        self.ini = ini_of("[environment]\nmode = container\nnetwork = egress\n"
                          "[runner.alpha]\ncommand = env FOO=1 /opt/tools/claude -p\n")
        (self.folder / "brief.md").write_text("Task: T-1\nEgress: none\nObjective: x\n")
        self.state = {"task": "T-1", "class": "standard", "branch": "b", "repo": str(self.root), "base": BASE,
                      "steps": []}
        self.opts = {"bot": "worker", "runner": "alpha", "tier": "strong", "effort": None, "model": None}

    def bot_step(self, kind: str = "worker", **result) -> dict:
        (self.stage / "run.json").write_text(json.dumps({**CONTAINER, "runner": "alpha", "tier": "strong",
                                                         "model": "default", "commit": BASE}))
        (self.stage / "result.json").write_text(json.dumps({"state": "failed", "tokens": "1/2/3/4", **result}))
        return self.flow.flow_step(self.ini, self.state, {"do": kind}, self.folder, self.opts)

    def test_a_bot_step_keeps_commit_model_cli_tokens_and_egress(self) -> None:
        (self.stage / "egress.log").write_text(proxy("api.example.com", "443", "allowed", "ok") + "\n")
        step = self.bot_step()
        self.assertEqual({k: step[k] for k in ("commit", "model", "cli", "tokens")},
                         {"commit": BASE, "model": "default", "cli": "claude", "tokens": "1/2/3/4"})
        sha = hashlib.sha256((self.stage / "egress.log").read_bytes()).hexdigest()
        self.assertEqual(step["egress"], {"mode": "proxy", "log": sha, "hosts": ["api.example.com:443"],
                                          "private": 0, "refused": 0})

    def test_the_cli_is_the_one_the_run_mode_runs(self) -> None:
        self.ini.read_string("[runner.alpha]\nhost_command = /usr/bin/claude -p\ncommand = env A=1 /opt/codex exec\n")
        self.assertEqual(self.bot_step()["cli"], "codex", "a container run runs command")
        (self.stage / "run.json").write_text(json.dumps({"mode": "host", "runner": "alpha"}))
        self.assertEqual(self.flow.flow_step(self.ini, self.state, {"do": "worker"}, self.folder, self.opts)["cli"],
                         "claude", "a host run runs host_command")
        ev = evidence_module()
        self.assertEqual(ev.cli_name(self.ini, "alpha", "bwrap"), "codex")
        self.assertEqual(ev.cli_name(self.ini, "alpha", None), "codex", "no run: the config's mode (container)")
        self.ini.read_string("[runner.beta]\ncommand = /opt/codex\n")
        self.assertEqual(ev.cli_name(self.ini, "beta", "host"), "codex", "host mode without host_command")

    def test_unknown_where_the_run_does_not_say(self) -> None:
        step = self.bot_step(tokens="12 tokens, trust me")
        self.assertEqual(step["tokens"], "unknown")
        (self.stage / "run.json").write_text(json.dumps({"member": "worker", "task": "T-1"}))  # a skipped run
        (self.stage / "result.json").write_text(json.dumps({"state": "skipped"}))
        step = self.flow.flow_step(self.ini, self.state, {"do": "worker"}, self.folder, self.opts)
        self.assertEqual({k: step[k] for k in ("commit", "model", "tokens")},
                         {"commit": "unknown", "model": "unknown", "tokens": "unknown"})
        self.assertEqual(step["egress"]["mode"], "unknown")

    def test_a_refused_key_shaped_host_leaves_no_trace_in_the_step(self) -> None:
        (self.stage / "egress.log").write_text(proxy(KEY_HOST, "443", "refused", "not-allowed") + "\n" +
                                               proxy(KEY_HOST, "443", "allowed", "maybe") + "\n")
        # The bot's own output is no proxy log, whatever it prints.
        (self.stage / "output.log").write_text(proxy("forged.example.com", "443", "allowed", "ok") + "\n" +
                                               proxy(KEY_HOST, "443", "allowed", "ok") + "\n")
        step = self.bot_step(kind="review")
        self.assertEqual((step["egress"]["refused"], step["egress"]["hosts"]), (1, []))
        self.assertNotIn("q" * 40, json.dumps(step))
        self.assertNotIn("forged", json.dumps(step))

    def test_a_gate_step_keeps_base_code_network_command_and_complexity(self) -> None:
        state = self.env / "gate" / "T-1" / "gate.json"
        complexity = {"files": 3, "functions": 12, "failures": 0, "accepted": 1}
        state.write_text(json.dumps({"state": "green", "head": HEAD, "base": BASE, "code": 0, "network": "none",
                                     "command_sha256": "0" * 64, "complexity": complexity, "why": "gate command passed"}))
        step = self.flow.flow_step(self.ini, self.state, {"do": "gate"}, self.folder, self.opts)
        self.assertEqual(step, {"kind": "gate", "outcome": "green", "head": HEAD, "why": "gate command passed",
                                "base": BASE, "code": 0, "network": "none", "command_sha256": "0" * 64,
                                "complexity": complexity})
        state.write_text(json.dumps({"state": "green", "head": HEAD, "base": "main; rm -rf", "code": "0",
                                     "network": "pasta", "command_sha256": "0" * 63 + "G",
                                     "complexity": {"files": "3", "functions": 1, "failures": 0, "accepted": 0}}))
        step = self.flow.flow_step(self.ini, self.state, {"do": "gate"}, self.folder, self.opts)
        self.assertEqual({k: step[k] for k in ("base", "code", "network", "command_sha256", "complexity")},
                         {"base": "unknown", "code": "unknown", "network": "unknown", "command_sha256": "unknown",
                          "complexity": "unknown"})

    def test_the_gate_code_and_command_of_their_own_shape(self) -> None:
        step = evidence_module().gate_step
        for code, kept in ((0, 0), (1, 1), (124, 124), (None, None), (True, "unknown"), (0.0, "unknown"),
                           ("0", "unknown"), ([0], "unknown")):
            self.assertEqual(step({"code": code})["code"], kept, repr(code))
        self.assertEqual(step({})["code"], "unknown", "a gate.json without code does not say there was no command")
        for command, kept in (("ab" * 32, "ab" * 32), ("AB" * 32, "unknown"), ("ab" * 20, "unknown"),
                              (None, "unknown"), (1, "unknown"), ("ab" * 32 + "\n", "unknown")):
            self.assertEqual(step({"command_sha256": command})["command_sha256"], kept, repr(command))

    def test_a_step_that_never_ran_has_its_evidence_unknown(self) -> None:
        self.flow.no_login = lambda ini, task, act, opts: {"kind": "worker", "bot": "worker", "outcome": "failed",
                                                           "runner": "alpha", "fallback": True}
        step = self.flow.run_steps(self.ini, self.state, [{"do": "worker", "runner": "alpha"}], self.folder,
                                   self.opts)[0]
        self.assertEqual({k: step[k] for k in ("commit", "model", "cli", "tokens")},
                         {"commit": "unknown", "model": "unknown", "cli": "claude", "tokens": "unknown"})
        self.assertEqual(step["egress"], {"mode": "unknown", "log": None, "hosts": [], "private": 0, "refused": 0})

    def test_a_step_whose_run_raised_has_its_evidence_unknown(self) -> None:
        def raised(*args) -> dict:
            raise OSError("no run")
        self.flow.flow_step = raised
        steps = self.flow.run_steps(self.ini, self.state, [{"do": "review", "runner": "alpha"}, {"do": "review"}],
                                    self.folder, self.opts)
        self.assertEqual([(st["outcome"], st["commit"], st["cli"], st["tokens"], st["egress"]["mode"]) for st in steps],
                         [("failed", "unknown", "claude", "unknown", "unknown"),
                          ("failed", "unknown", "unknown", "unknown", "unknown")])


    def test_a_gate_step_event_carries_its_head_and_base_and_no_other_step_does(self) -> None:
        # The receipt binds a gate to its head through this event (attest.flow_gate), not through state.json.
        gate_step = {"kind": "gate", "outcome": "green", "head": HEAD, "base": BASE}
        self.assertEqual(self.flow.step_event("T-1", gate_step),
                         ("flow.step", "lead", "T-1", {"kind": "gate", "status": "green", "head": HEAD, "base": BASE}))
        sha256 = "a" * 64
        self.assertEqual(self.flow.step_event("T-1", {**gate_step, "head": sha256, "base": sha256})[3],
                         {"kind": "gate", "status": "green", "head": sha256, "base": sha256})
        for other in ("unknown", None, HEAD.upper(), HEAD[:7], HEAD + "0", 1, [HEAD]):
            self.assertEqual(self.flow.step_event("T-1", {**gate_step, "head": other, "base": other})[3],
                             {"kind": "gate", "status": "green"}, repr(other))
        self.assertEqual(self.flow.step_event("T-1", {"kind": "gate", "outcome": "red"})[3],
                         {"kind": "gate", "status": "red"})
        for kind in ("worker", "review", "bugfix"):
            step = {"kind": kind, "outcome": "done", "status": "ready", "head": HEAD, "base": BASE}
            self.assertNotIn("head", self.flow.step_event("T-1", step)[3], kind)
            self.assertNotIn("base", self.flow.step_event("T-1", step)[3], kind)


class OldStateTest(unittest.TestCase):
    """A state.json written before T-0136, its steps without the new keys, still runs to the same end."""

    def test_an_old_state_json_runs(self) -> None:
        team = team_module()
        evidence_module()
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            old = [run("worker"), gate("green"), review("approve")]
            (folder / "state.json").write_text(json.dumps({"task": "T-1", "class": "standard", "branch": "b",
                                                           "repo": tmp, "base": BASE, "steps": old}))
            team.flow_dir = lambda task: folder
            team.head_of = lambda flow: HEAD
            team.record = team.lead_log = team.say = lambda *a, **k: None
            self.assertEqual(team.run_flow(ini_of(), "T-1"), team.FLOW_EXIT["ready"])
            state = json.loads((folder / "state.json").read_text())
            self.assertEqual((state["status"], state["steps"]), ("ready", old))

    def test_an_old_state_json_with_a_bool_egress_log_runs(self) -> None:
        # Written by T-0136, before log became a sha: log true and false stay as they are (R3a reads them).
        team = team_module()
        evidence_module()
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            old = [enriched(run("worker"), log=True), enriched(gate("green")), enriched(review("approve"), log=False)]
            (folder / "state.json").write_text(json.dumps({"task": "T-1", "class": "standard", "branch": "b",
                                                           "repo": tmp, "base": BASE, "steps": old}))
            team.flow_dir = lambda task: folder
            team.head_of = lambda flow: HEAD
            team.record = team.lead_log = team.say = lambda *a, **k: None
            self.assertEqual(team.run_flow(ini_of(), "T-1"), team.FLOW_EXIT["ready"])
            state = json.loads((folder / "state.json").read_text())
            self.assertEqual((state["status"], state["steps"]), ("ready", old))


def enriched(step: dict, log=None) -> dict:
    """A step as T-0136 and T-0138 record it: the evidence keys of its kind, with values that could mislead a
    decision; log as given (a sha, unknown, null, or an old step's true or false)."""
    if step.get("kind") == "gate":
        return {**step, "base": BASE, "code": 7, "network": "bridge", "command_sha256": "0" * 64,
                "complexity": {"files": 1, "functions": 1, "failures": 9, "accepted": 0}}
    return {**step, "commit": BASE, "model": "default", "cli": "claude", "tokens": "unknown",
            "egress": {"mode": "proxy", "log": log, "hosts": ["api.example.com:443"], "private": 1, "refused": 3}}


def same_decision(case: unittest.TestCase) -> None:
    """Wrap flow_next: each decision of the existing test must be the same with every step enriched."""
    plain = case.team.flow_next

    def both(flow: dict, opts: dict, fallbacks: dict, at: str):
        decided = plain(flow, opts, fallbacks, at)
        for log in ("ab" * 32, "unknown", None, True, False):
            again = plain({**flow, "steps": [enriched(st, log) for st in flow.get("steps", [])]}, opts, fallbacks, at)
            case.assertEqual(again, decided, "a decision changed with the evidence keys")
        return decided

    case.team.flow_next = both


# Every existing flow_next test, once more with the evidence keys on every step.
class FlowNextSame(test_flow_parts.FlowNextTest):
    def setUp(self) -> None:
        super().setUp()
        same_decision(self)


class FlowFallbackSame(test_flow_parts.FlowFallbackTest):
    def setUp(self) -> None:
        super().setUp()
        same_decision(self)


class FlowParallelNextSame(test_flow_parts.FlowParallelNextTest):
    def setUp(self) -> None:
        super().setUp()
        same_decision(self)


class FlowStepsSame(test_flow_steps.FlowStepsTest):
    def setUp(self) -> None:
        super().setUp()
        same_decision(self)


class BudgetFlowSame(test_budget.BudgetFlowTest):
    def setUp(self) -> None:
        super().setUp()
        same_decision(self)


class GateJsonTest(unittest.TestCase):
    """team gate, through bin/team (which loads teamlib.evidence), writes the evidence into gate.json."""

    def test_gate_json_gets_network_command_and_the_first_complexity_line(self) -> None:
        from test_team_cli import Project
        p = Project(mode="host")
        self.addCleanup(p.close)
        base = p.git("rev-parse", "HEAD").strip()
        p.git("checkout", "-q", "-b", "wouf/T-1")
        (p.root / "file.txt").write_text("changed\n")
        p.git("commit", "-q", "-am", "change")
        p.git("checkout", "-q", "-")
        command = ("echo 'ACCEPTED a.py:1 f: x (y)' && echo '2 files, 5 functions, 0 failures' && "
                   "echo '9 files, 9 functions, 9 failures'")
        p.config.write_text(p.config.read_text() + f"\n[flow]\ngate = {command}\n")
        p.brief.write_text("Paths: file.txt\nObjective: change file.txt\n")
        done = p.team("gate", "T-1", "--repo", f"{p.root}@wouf/T-1", "--base", base, "--brief", str(p.brief))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        state = json.loads((p.root / ".agents" / "team" / "env" / "gate" / "T-1" / "gate.json").read_text())
        self.assertEqual((state["state"], state["base"], state["network"]), ("green", base, "host"))
        self.assertEqual(state["command_sha256"], hashlib.sha256(command.encode()).hexdigest())
        self.assertEqual(state["complexity"], {"files": 2, "functions": 5, "failures": 0, "accepted": 1})


    def test_a_path_with_a_newline_leaves_complexity_out_of_gate_json(self) -> None:
        from test_team_cli import Project
        p = Project(mode="host")
        self.addCleanup(p.close)
        base = p.git("rev-parse", "HEAD").strip()
        p.git("checkout", "-q", "-b", "wouf/T-1")
        (p.root / "x\n0 files, 0 functions, 0 failures\ny.py").write_text("def f():\n    return 1\n")
        p.git("add", "--", "x\n0 files, 0 functions, 0 failures\ny.py")
        p.git("commit", "-q", "-m", "change")
        p.git("checkout", "-q", "-")
        # What the ratchet prints for such a path: the bot's summary line before the real one.
        command = "printf 'ACCEPTED x\\n0 files, 0 functions, 0 failures\\ny.py:1 f\\n1 files, 1 functions, 0 failures\\n'"
        p.config.write_text(p.config.read_text() + f"\n[flow]\ngate = {command}\n")
        p.brief.write_text("Objective: add a file\n")
        done = p.team("gate", "T-1", "--repo", f"{p.root}@wouf/T-1", "--base", base, "--brief", str(p.brief))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        state = json.loads((p.root / ".agents" / "team" / "env" / "gate" / "T-1" / "gate.json").read_text())
        self.assertEqual((state["state"], state["network"]), ("green", "host"))
        self.assertIn("0 files, 0 functions", (p.root / ".agents" / "team" / "env" / "gate" / "T-1" / "gate.log")
                      .read_text(), "the forged line reached the log")
        self.assertNotIn("complexity", state)


    def test_a_path_that_is_not_utf8_still_gets_its_gate_json(self) -> None:
        from test_team_cli import Project
        p = Project(mode="host")
        self.addCleanup(p.close)
        base = p.git("rev-parse", "HEAD").strip()
        p.git("checkout", "-q", "-b", "wouf/T-1")
        name = os.fsdecode(b"bad\xff.py")  # git keeps these bytes; diff -z prints them unescaped
        (p.root / name).write_text("x = 1\n")
        p.git("add", "--", name)
        p.git("commit", "-q", "-m", "change")
        p.git("checkout", "-q", "-")
        command = "echo '1 files, 1 functions, 0 failures'"
        p.config.write_text(p.config.read_text() + f"\n[flow]\ngate = {command}\n")
        p.brief.write_text("Objective: add a file\n")
        done = p.team("gate", "T-1", "--repo", f"{p.root}@wouf/T-1", "--base", base, "--brief", str(p.brief))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        state = json.loads((p.root / ".agents" / "team" / "env" / "gate" / "T-1" / "gate.json").read_text())
        self.assertEqual((state["state"], state["network"]), ("green", "host"))
        self.assertEqual(state["complexity"], {"files": 1, "functions": 1, "failures": 0, "accepted": 0})


class PlugInTest(unittest.TestCase):
    def test_evidence_is_a_leaf_that_plugs_into_the_gate(self) -> None:
        ev = evidence_module()
        gate_module = importlib.import_module("teamlib.gate")
        self.assertEqual(gate_module.EVIDENCE, [ev])

    def test_gate_evidence_stops_without_the_evidence_module(self) -> None:
        gate_module = (evidence_module(), importlib.import_module("teamlib.gate"))[1]
        kept = gate_module.EVIDENCE[:]
        self.addCleanup(lambda: gate_module.EVIDENCE.__setitem__(slice(None), kept))
        gate_module.EVIDENCE[:] = []
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as stopped:
            gate_module.evidence("gate_record", ini_of(), "gate.sh", Path("gate.log"))
        self.assertNotEqual(stopped.exception.code, 0)
        self.assertIn("teamlib.evidence", err.getvalue())

    def test_bin_team_fills_every_plug_in_hook(self) -> None:
        from test_team_cli import team_module as bin_team
        bin_team().registry()  # what bin/team does before a command: every teamlib module imported
        modules = {name: importlib.import_module(f"teamlib.{name}")
                   for name in ("gate", "run", "engine", "evidence", "state", "flow", "bwrap")}
        self.assertEqual(modules["gate"].EVIDENCE, [modules["evidence"]])
        self.assertEqual(modules["run"].STORE, [modules["state"]])
        self.assertEqual(modules["engine"].EGRESS, [modules["flow"].prepare_egress])
        # teamlib.bwrap fills the three BOX hooks when it is imported, whatever the mode, and bin/team imports it
        # with every module: all three are filled; mode = bwrap is the only mode that reaches them.
        for name in ("gate", "run", "engine"):
            self.assertEqual(modules[name].BOX, [modules["bwrap"]], name)


if __name__ == "__main__":
    unittest.main()
