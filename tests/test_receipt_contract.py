"""The contract between R2 and R1 (receipts R2.1, T-0138): what teamlib.evidence records at a flow step and in
gate.json, with only the fields R3a adds itself, passes teamlib.receipt.check_receipt. The values come from the
evidence module's real functions on fixture stage folders, never written by hand; the negative cases are the R2
values of before T-0138, which check_receipt must refuse (so this test would have caught the gap)."""

from __future__ import annotations

import configparser
import copy
import importlib
import json
import tempfile
import unittest
from pathlib import Path

from test_flow_evidence import CONTAINER, evidence_module, proxy
from test_receipt import BASE, HEAD, receipt, sample

ev = evidence_module()
engine = importlib.import_module("teamlib.engine")
Refused = receipt.Refused
COMMIT = "a" * 40
INI = "[environment]\nmode = container\n[runner.alpha]\ncommand = env A=1 /opt/tools/claude -p\n"

# The egress.log fixtures: a complete proxy log (every allowed reason=ok line names a host and port), an incomplete
# one (an allowed line whose host is masked), and none.
COMPLETE = [proxy("api.example.com", "443", "allowed", "ok"), proxy("pypi.org", "443", "allowed", "ok"),
            proxy("127.0.0.1", "5432", "allowed", "private"), proxy("evil.example.net", "443", "refused", "nope"),
            "egress-proxy: listening on port 3128"]
INCOMPLETE = [proxy("api.example.com", "443", "allowed", "ok"), proxy("***.example.com", "443", "allowed", "ok")]


def ini_of(text: str = INI) -> configparser.ConfigParser:
    ini = configparser.ConfigParser(interpolation=None)
    ini.read_string(text)
    return ini


class ContractTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.ini = ini_of()

    def stage(self, name: str, lines: list | None, spec: dict = CONTAINER) -> Path:
        """A run folder as the lead collects it: run.json, result.json and, if any, egress.log."""
        stage = self.root / name
        stage.mkdir()
        (stage / "run.json").write_text(json.dumps({**spec, "runner": "alpha", "model": "default",
                                                    "commit": COMMIT}))
        (stage / "result.json").write_text(json.dumps({"state": "done", "tokens": "1/2/3/4"}))
        if lines is not None:
            (stage / "egress.log").write_text("".join(f"{line}\n" for line in lines))
        return stage

    def recorded(self, stage: Path) -> dict:
        """run_record of a stage, as flow_step calls it."""
        read = lambda name: json.loads((stage / name).read_text())  # noqa: E731
        return ev.run_record(self.ini, stage, read("run.json"), read("result.json"), "alpha")

    def run_of(self, evidence: dict, tier: str, role: str = "worker", outcome: str = "ready-for-review") -> dict:
        """A runs[] entry: the step's evidence, and only what R3a adds itself (run, role, bot, runner, vendor,
        tier, outcome)."""
        vendor = receipt.VENDORS.get(evidence["cli"], receipt.UNKNOWN)
        return {"run": "T-0138", "role": role, "bot": role, "runner": "alpha", "vendor": vendor, "tier": tier,
                "outcome": outcome, **evidence}

    def gate_of(self, command: str = "scripts/pre-gate.sh", code=0) -> dict:
        """The receipt's gate from gate.json as team gate writes it (its result keys and gate_record), read back by
        gate_step; state and head as the gate step keeps them."""
        log = self.root / "gate.log"
        log.write_text("ACCEPTED a.py:1 f\n2 files, 30 functions, 0 failures\n")
        result = {"task": "T-0138", "head": HEAD, "base": BASE, "outside": [], "code": code, "started": "x",
                  "state": "green", "why": "gate command passed"}
        result.update(ev.gate_record(self.ini, command, log, "a.py\0"))
        state = self.root / "gate.json"
        state.write_text(json.dumps(result))
        stored = json.loads(state.read_text())
        return {"state": stored["state"], "head": stored["head"], **ev.gate_step(stored)}

    def receipt_with(self, runs: list, gate: dict) -> dict:
        """sample() of tests/test_receipt.py with the recorded runs and gate; usage summed as R3a will."""
        value = sample()
        value["gate"], value["runs"] = gate, runs
        known = [r["tokens"] for r in runs if r["tokens"] != receipt.UNKNOWN]
        value["usage"] = {"tokens": sum(int(n) for t in known for n in t.split("/")), "known_runs": len(known),
                          "unknown_runs": len(runs) - len(known), "cost": "unknown"}
        return value

    def all_runs(self) -> list:
        complete = self.recorded(self.stage("complete", COMPLETE))
        incomplete = self.recorded(self.stage("incomplete", INCOMPLETE))
        none = self.recorded(self.stage("none", None))
        bridge = self.recorded(self.stage("bridge", COMPLETE, {"mode": "container", "network": "bridge"}))
        never = ev.unknown_run(self.ini, "alpha")
        self.assertRegex(complete["egress"]["log"], r"\A[0-9a-f]{64}\Z")
        self.assertEqual((incomplete["egress"]["log"], none["egress"]["log"], bridge["egress"]["log"],
                          never["egress"]["log"]), ("unknown", None, None, None))
        return [self.run_of(complete, engine.tier_label("strong", "strong")),
                self.run_of(incomplete, engine.tier_label("mid", "strong"), "review", "approve"),
                self.run_of(none, engine.tier_label("light", "light")),
                self.run_of(bridge, engine.tier_label("mid", "mid")),
                self.run_of(never, receipt.UNKNOWN, outcome="failed")]

    def test_what_r2_records_passes_check_receipt(self) -> None:
        runs, gate = self.all_runs(), self.gate_of()
        receipt.check_receipt(self.receipt_with(runs, gate))
        for index, run in enumerate(runs):  # each run alone, too
            with self.subTest(run=index):
                receipt.check_receipt(self.receipt_with([run], gate))

    def test_a_gate_without_a_command_passes(self) -> None:
        gate = self.gate_of(command="", code=None)
        self.assertIsNone(gate["code"])
        receipt.check_receipt(self.receipt_with(self.all_runs()[:1], gate))

    def test_a_linked_egress_log_is_null(self) -> None:
        stage = self.stage("linked", None)
        (self.root / "real.log").write_text("".join(f"{line}\n" for line in COMPLETE))
        (stage / "egress.log").symlink_to(self.root / "real.log")
        run = self.recorded(stage)
        self.assertIsNone(run["egress"]["log"])
        receipt.check_receipt(self.receipt_with([self.run_of(run, "strong")], self.gate_of()))

    def test_the_values_of_before_t0138_are_refused(self) -> None:
        good = self.receipt_with(self.all_runs()[:1], self.gate_of())
        receipt.check_receipt(good)
        for what, change in (
                ("a bool log", lambda r: r["runs"][0]["egress"].update(log=True)),
                ("a false log", lambda r: r["runs"][0]["egress"].update(log=False)),
                ("a unicode arrow tier", lambda r: r["runs"][0].update(tier="mid→strong")),
                ("a gate without code", lambda r: r["gate"].pop("code")),
                ("a gate without command_sha256", lambda r: r["gate"].pop("command_sha256"))):
            bad = copy.deepcopy(good)
            change(bad)
            with self.subTest(what), self.assertRaises(Refused):
                receipt.check_receipt(bad)


if __name__ == "__main__":
    unittest.main()
