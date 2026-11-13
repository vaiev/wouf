"""Tests for .agents/bin/teamlib/receipt.py: canonical JSON wouf-cj1, the strict parser, the fields of
wouf.receipt.v1, and signing and checking with ssh-keygen -Y in the namespace wouf-receipt-v1."""

from __future__ import annotations

import ast
import builtins
import copy
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / ".agents" / "bin"
RECEIPT = BIN / "teamlib" / "receipt.py"
sys.path.insert(0, str(BIN))
from teamlib import core, receipt  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import isolate_wouf_env  # noqa: E402

Refused = receipt.Refused
BASE = "0123456789abcdef0123456789abcdef01234567"
HEAD = "89abcdef0123456789abcdef0123456789abcdef"
LANDED = "fedcba9876543210fedcba9876543210fedcba98"
TREE = "00112233445566778899aabbccddeeff00112233"
OTHER = "5555555555555555555555555555555555555555"
FAKE_SIG = "-----BEGIN SSH SIGNATURE-----\nU1NIU0lHAAAAAQ==\nAAAA\n-----END SSH SIGNATURE-----\n"
# Built from parts so this file holds no contiguous key; the shapes of an AWS key id and of a random token.
AWS = "AKIA" + "IOSFODNN7EXAMPL0"
TOKEN = "aB3dE5gH7jK9mN1p" + "Q3sT5vW7yZ9bC2dF"


def helper(path: Path, name: str):
    loader = SourceFileLoader(name, str(path))
    module = module_from_spec(spec_from_loader(name, loader))
    loader.exec_module(module)
    return module


def sample() -> dict:
    """A receipt that passes every check: one counting review, a worker run with proxy egress, a review run."""
    return {
        "schema": "wouf.receipt.v1", "team": "werkstatt", "signed_at": "2026-11-06T12:00:00Z", "resigned_at": None,
        "repo": "wouf", "task": "T-0135", "class": "security",
        "brief": {"sha256": "ab" * 32, "source": "flow"},
        "change": {"base": BASE, "reviewed": HEAD, "landed": LANDED, "landed_parent": BASE, "tree": TREE,
                   "files": 5, "merge": "squash", "pr": 142},
        "gate": {"state": "green", "head": HEAD, "base": BASE, "code": 0, "network": "none",
                 "command_sha256": "cd" * 32,
                 "complexity": {"files": 2, "functions": 30, "failures": 0, "accepted": 0}},
        "reviews": {"required": 1, "approved": 1, "rounds": [
            {"run": "T-0135.r1", "index": 1, "bot": "review", "runner": "codex", "cli": "codex", "vendor": "openai",
             "model": "default", "verdict": "approve", "head": HEAD, "approve_sha": HEAD[:12], "counts": True,
             "source": "flow"}]},
        "runs": [
            {"run": "T-0135", "role": "worker", "bot": "worker", "runner": "claude", "cli": "claude",
             "vendor": "anthropic", "model": "default", "tier": "strong", "commit": BASE,
             "outcome": "ready-for-review", "tokens": "10/20/30/40",
             "egress": {"mode": "proxy", "log": "ef" * 32, "hosts": ["api.example.com:443", "pypi.org:443"],
                        "private": 0, "refused": 1}},
            {"run": "T-0135.r1", "role": "review", "bot": "review", "runner": "codex", "cli": "codex",
             "vendor": "openai", "model": "default", "tier": "mid->strong", "commit": HEAD, "outcome": "approve",
             "tokens": "unknown", "egress": {"mode": "none", "log": None, "hosts": [], "private": 0, "refused": 0}}],
        "usage": {"tokens": 100, "known_runs": 1, "unknown_runs": 1, "cost": "unknown"},
        "flags": {},
    }


def file_bytes(value=None, sig: str = FAKE_SIG) -> bytes:
    """The canonical stored file, made with the SPEC's own formula (not the module's)."""
    obj = {"receipt": sample() if value is None else value, "sig": sig}
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode() + b"\n"


def objects(value: dict, path: tuple = ()) -> list[tuple]:
    """The path of every object of a receipt (the receipt itself included)."""
    found = [path]
    for key, item in value.items():
        if isinstance(item, dict):
            found += objects(item, (*path, key))
        elif isinstance(item, list):
            for index, element in enumerate(item):
                if isinstance(element, dict):
                    found += objects(element, (*path, key, index))
    return found


def at(value, path: tuple):
    for key in path:
        value = value[key]
    return value


def setUpModule() -> None:
    isolate_wouf_env()


class CanonicalTest(unittest.TestCase):
    def test_canonical_is_the_spec_formula(self) -> None:
        value = sample()
        self.assertEqual(receipt.canonical(value), json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii"))
        self.assertEqual(receipt.canonical({"b": 1, "a": [True, False, None, "x"]}),
                         b'{"a":[true,false,null,"x"],"b":1}')
        self.assertEqual(receipt.canonical({"k": "café"}), b'{"k":"caf\\u00e9"}')

    def test_canonical_refuses_what_has_no_single_byte_form(self) -> None:
        for value in (1.0, float("nan"), float("inf"), {"a": 1.5}, {1: "a"}, ("a",), {"a": {"b"}}, b"x"):
            with self.subTest(value=repr(value)), self.assertRaises(Refused):
                receipt.canonical(value)
        huge = sample()
        huge["change"]["files"] = 10 ** 5000  # no digits are made for the scan; the domain refuses it
        with self.assertRaises(Refused):
            receipt.check_receipt(huge)

    def test_the_regexes_are_federate_and_log_written_again(self) -> None:
        log = helper(BIN / "log", "wouf_log_for_receipt")
        federate = helper(BIN / "federate", "wouf_federate_for_receipt")
        self.assertEqual(receipt.TOKEN_LIKE.pattern, log.TOKEN_LIKE.pattern)
        self.assertEqual(receipt.SIG.pattern, federate.SIG.pattern)
        for text in ("", TOKEN, TOKEN.lower(), "a" * 64, BASE, "snake_case_name_that_is_long_enough_x1",
                     "x " + TOKEN + " y", "0123456789ABCDEF" * 4):
            self.assertEqual(receipt.token_like(text), log.token_like(text), text)

    def test_a_library_without_commands_that_team_help_still_lists_cleanly(self) -> None:
        # bin/team imports every teamlib module and reads its COMMANDS and USAGE.
        self.assertFalse(hasattr(receipt, "COMMANDS") or hasattr(receipt, "USAGE"))
        done = subprocess.run([sys.executable, str(BIN / "team"), "help"], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_the_module_imports_only_the_standard_library_and_teamlib_core(self) -> None:
        names = set()
        for node in ast.walk(ast.parse(RECEIPT.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names |= {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                names.add(node.module or "")
        teamlib = {n for n in names if n.split(".")[0] == "teamlib"}
        self.assertEqual(teamlib, {"teamlib.core"})
        stdlib = getattr(sys, "stdlib_module_names", None)
        if stdlib is None:  # Python 3.9
            stdlib = {"__future__", "json", "re", "shlex", "shutil", "subprocess", "tempfile", "datetime", "pathlib"}
        self.assertEqual({n for n in names - teamlib if n.split(".")[0] not in stdlib}, set())


class ParseTest(unittest.TestCase):
    def refused(self, data: bytes, words: str) -> None:
        with self.assertRaises(Refused) as caught:
            receipt.parse(data)
        self.assertIn(words, str(caught.exception))

    def test_the_canonical_file_parses_and_round_trips(self) -> None:
        data = file_bytes()
        self.assertEqual(receipt.parse(data), {"receipt": sample(), "sig": FAKE_SIG})
        self.assertEqual(receipt.stored(sample(), FAKE_SIG), data)
        self.assertEqual(receipt.parse(file_bytes(sig=FAKE_SIG.rstrip("\n")))["sig"], FAKE_SIG.rstrip("\n"))

    def test_a_duplicate_key_is_refused(self) -> None:
        data = file_bytes().replace(b'"repo":"wouf"', b'"repo":"wouf","repo":"wouf"')
        self.refused(data, "a key appears twice")
        self.refused(file_bytes().replace(b'{"receipt":', b'{"sig":"x","receipt":'), "a key appears twice")

    def test_a_float_is_refused(self) -> None:
        for number in (b"5.0", b"5e0", b"5E0", b"0.5", b"-1.5"):
            with self.subTest(number=number):
                self.refused(file_bytes().replace(b'"files":5', b'"files":' + number), "a float")

    def test_nan_and_infinity_are_refused(self) -> None:
        for constant in (b"NaN", b"Infinity", b"-Infinity"):
            with self.subTest(constant=constant):
                self.refused(file_bytes().replace(b'"files":5', b'"files":' + constant), "NaN or Infinity")

    def test_a_bool_where_an_int_is_due_is_refused(self) -> None:
        for old, new, words in ((b'"files":5', b'"files":true', "receipt.change.files must be a whole number"),
                                (b'"pr":142', b'"pr":true', "receipt.change.pr must be a whole number"),
                                (b'"code":0', b'"code":false', "receipt.gate.code must be 0 or null"),
                                (b'"private":0', b'"private":false', "egress.private must be a whole number"),
                                (b'"approved":1', b'"approved":true', "receipt.reviews.approved must be a whole"),
                                (b'"tokens":100', b'"tokens":true', "receipt.usage.tokens must be a whole")):
            with self.subTest(new=new):
                data = file_bytes().replace(old, new, 1)
                self.assertNotEqual(data, file_bytes())
                self.refused(data, words)

    def test_a_non_ascii_or_control_character_in_the_receipt_is_refused(self) -> None:
        self.refused(file_bytes().replace(b'"repo":"wouf"', '"repo":"wöuf"'.encode()), "a byte that is not ASCII")
        self.refused(b"\xef\xbb\xbf" + file_bytes(), "a byte that is not ASCII")
        for escape in (b"\\u00f6", b"\\u0007", b"\\n", b"\\t", b"\\r", b"\\u007f", b"\\u2028", b"\\u0000"):
            with self.subTest(escape=escape):
                self.refused(file_bytes().replace(b'"repo":"wouf"', b'"repo":"wo' + escape + b'uf"'),
                             "receipt.repo holds a character that is not printable ASCII")
        self.refused(file_bytes().replace(b'"source":"flow"}', b'"source":"flow","x\\u00e9":1}', 1),
                     "a key of receipt.brief holds a character that is not printable ASCII")

    def test_nesting_over_eight_is_refused(self) -> None:
        deep = sample()
        deep["flags"] = {"a": {"b": {"c": {"d": {"e": {"f": {"g": 1}}}}}}}  # receipt=1, flags=2 ... g's object=8
        with self.assertRaises(Refused) as caught:  # eight levels: the domain passes, the flags rule does not
            receipt.parse(file_bytes(deep))
        self.assertIn("receipt.flags has an unknown key", str(caught.exception))
        at(deep, ("flags", "a", "b", "c", "d", "e", "f"))["g"] = [1]
        self.refused(file_bytes(deep), "is nested deeper than 8 levels")
        self.refused(b"[" * 5000 + b"]" * 5000 + b"\n", "nested too deep")

    def test_over_64_kib_is_refused(self) -> None:
        big = sample()
        big["runs"] = big["runs"] * 200
        big["usage"].update(tokens=20000, known_runs=200, unknown_runs=200)
        data = file_bytes(big)
        self.assertGreater(len(data), 64 * 1024)
        self.refused(data, "over 64 KiB")
        with self.assertRaises(Refused) as caught:
            receipt.stored(big, FAKE_SIG)
        self.assertIn("over 64 KiB", str(caught.exception))
        self.refused(file_bytes()[:-1] + b" " * (64 * 1024) + b"\n", "over 64 KiB")

    def test_bytes_that_are_not_their_canonical_form_are_refused(self) -> None:
        data = file_bytes()
        pretty = json.dumps(json.loads(data), sort_keys=True, ensure_ascii=True).encode() + b"\n"
        unsorted = json.dumps({"sig": FAKE_SIG, "receipt": sample()}, separators=(",", ":")).encode() + b"\n"
        for variant in (data[:-1], data + b"\n", data[:-1] + b"\r\n", b" " + data, data[:-1] + b" \n", pretty,
                        unsorted, data.replace(b'"repo":"wouf"', b'"repo":"\\u0077ouf"'),
                        data.replace(b'"repo":"wouf"', b'"repo":"wou\\f"'.replace(b"\\f", b"\\u0066")),
                        data.replace(b'"repo":"wouf"', b'"repo" : "wouf"'),
                        data.replace(b'"files":5', b'"files":-0'), data.replace(b'"files":5', b'"files":05')):
            with self.subTest(variant=variant[:40]):
                with self.assertRaises(Refused) as caught:
                    receipt.parse(variant)
                self.assertRegex(str(caught.exception), "not in its canonical form|not JSON")

    def test_integers_outside_the_domain_and_long_text_are_refused(self) -> None:
        self.refused(file_bytes().replace(b'"files":5', b'"files":9007199254740992'), "outside 0 to 2^53-1")
        self.refused(file_bytes().replace(b'"files":5', b'"files":-1'), "outside 0 to 2^53-1")
        self.refused(file_bytes().replace(b'"repo":"wouf"', b'"repo":"' + b"w" * 501 + b'"'), "longer than 500")
        self.assertEqual(receipt.parse(file_bytes().replace(b'"repo":"wouf"', b'"repo":"' + b"w" * 500 + b'"'))[
            "receipt"]["repo"], "w" * 500)

    def test_the_file_holds_exactly_receipt_and_sig(self) -> None:
        for data in (b"[]\n", b'"x"\n', b'{"receipt":{}}\n', json.dumps(
                {"extra": 1, "receipt": sample(), "sig": FAKE_SIG}, sort_keys=True, separators=(",", ":")).encode()):
            with self.subTest(data=data[:30]):
                self.refused(data, "exactly receipt and sig")
        self.refused(b"not json\n", "not JSON")
        self.refused("text".encode(), "not JSON")
        with self.assertRaises(Refused):
            receipt.parse("{}")  # text, not bytes

    def test_sig_must_match_federate_sig_newlines_included(self) -> None:
        begin, end = "-----BEGIN SSH SIGNATURE-----\n", "-----END SSH SIGNATURE-----"
        for sig in (FAKE_SIG.replace("\n", "\r\n"), FAKE_SIG + "\n", FAKE_SIG + "x", "x" + FAKE_SIG,
                    begin + "AAAA AAAA\n" + end + "\n", begin + end + "\n", begin + "A" * 4001 + end,
                    FAKE_SIG.replace(begin, "-----BEGIN SSH SIGNATURE-----"), begin + "AAAA\n" + end + "\n\n", ""):
            with self.subTest(sig=sig[-40:]):
                self.refused(file_bytes(sig=sig), "sig is not one armored SSH signature")
        for sig in (1, None, [FAKE_SIG]):
            data = json.dumps({"receipt": sample(), "sig": sig}, sort_keys=True, separators=(",", ":")).encode()
            self.refused(data + b"\n", "sig is not one armored SSH signature")
        self.assertTrue(receipt.parse(file_bytes(sig=begin + "A" * 4000 + end)))


class FieldTest(unittest.TestCase):
    def refused(self, value, words: str) -> None:
        with self.assertRaises(Refused) as caught:
            receipt.check_receipt(value)
        self.assertIn(words, str(caught.exception))

    def changed(self, path: tuple, value) -> dict:
        r = sample()
        at(r, path[:-1])[path[-1]] = value
        return r

    def test_the_sample_passes(self) -> None:
        receipt.check_receipt(sample())

    def test_an_unknown_key_at_every_level_is_refused(self) -> None:
        paths = objects(sample())
        self.assertGreaterEqual(len(paths), 11)
        for path in paths:
            with self.subTest(path=path):
                r = sample()
                at(r, path)["extra"] = 1
                self.refused(r, "has an unknown key")

    def test_a_missing_key_at_every_level_is_refused(self) -> None:
        for path in objects(sample()):
            for key in list(at(sample(), path)):
                with self.subTest(path=path, key=key):
                    r = sample()
                    del at(r, path)[key]
                    self.refused(r, f"misses the key {key}")

    def test_a_sha_that_is_not_40_lower_case_hex_is_refused(self) -> None:
        fields = [("change", k) for k in ("base", "reviewed", "landed", "landed_parent", "tree")] + [
            ("gate", "head"), ("gate", "base"), ("reviews", "rounds", 0, "head"), ("runs", 0, "commit")]
        for path in fields:
            for bad in (HEAD.upper(), HEAD[:39], HEAD + "0", "ab" * 32, "g" * 40, 1, None, "", HEAD[:12]):
                with self.subTest(path=path, bad=bad):
                    self.refused(self.changed(path, bad), "must be 40 lower-case hex")
        receipt.check_receipt(self.changed(("runs", 0, "commit"), "unknown"))
        for path in fields[:7]:
            with self.subTest(path=path):
                self.refused(self.changed(path, "unknown"), "must be 40 lower-case hex")

    def test_files_zero_is_refused(self) -> None:
        for bad in (0, True, "5", None):
            with self.subTest(bad=bad):
                self.refused(self.changed(("change", "files"), bad),
                             "receipt.change.files must be a whole number from 1")
        self.refused(self.changed(("change", "files"), -1), "receipt.change.files is an integer outside 0 to 2^53-1")
        receipt.check_receipt(self.changed(("change", "files"), 1))

    def test_merge_is_squash_or_fast_forward(self) -> None:
        for bad in ("merge", "rebase", "Squash", "fast_forward", "", None, 1):
            with self.subTest(bad=bad):
                self.refused(self.changed(("change", "merge"), bad), "merge must be one of: squash, fast-forward")
        forward = self.changed(("change", "merge"), "fast-forward")
        forward["change"]["landed_parent"] = OTHER  # only a fast-forward may land on another parent
        receipt.check_receipt(forward)
        self.refused(self.changed(("change", "landed_parent"), OTHER), "landed_parent must equal receipt.change.base")

    def test_a_counting_round_must_meet_the_rule(self) -> None:
        breaks = {"verdict": "changes", "head": OTHER, "approve_sha": None}
        cases = [{"verdict": "changes"}, {"verdict": "stale"}, {"head": OTHER, "approve_sha": OTHER[:12]},
                 {"head": "unknown"}, {"approve_sha": None},
                 {"approve_sha": OTHER[:12]}, {"approve_sha": "0" * 7}]
        for case in cases:
            with self.subTest(case=case):
                r = sample()
                r["reviews"]["rounds"][0].update(case)
                self.refused(r, "receipt.reviews.rounds[0].counts is true")
        for typed in (HEAD[:7], HEAD):
            r = sample()
            r["reviews"]["rounds"][0]["approve_sha"] = typed
            receipt.check_receipt(r)
        r = sample()
        for typed in (HEAD[:12].upper(), HEAD[:6], HEAD + "0", 7):
            r["reviews"]["rounds"][0].update(approve_sha=typed)
            self.refused(r, "approve_sha must be 7 to 40 lower-case hex or null")
        # A round that meets the rule may still not count (a later gate or round replaced it).
        r = sample()
        r["reviews"]["rounds"][0]["counts"] = False
        r["reviews"]["approved"] = 0
        receipt.check_receipt(r)
        r = sample()
        r["reviews"]["rounds"][0].update(breaks, counts=False)
        r["reviews"]["approved"] = 0
        receipt.check_receipt(r)

    def test_approved_is_the_number_of_counting_rounds(self) -> None:
        for bad in (0, 2):
            with self.subTest(bad=bad):
                self.refused(self.changed(("reviews", "approved"), bad),
                             "receipt.reviews.approved must be the number of rounds that count")
        r = sample()
        second = dict(r["reviews"]["rounds"][0], run="T-0135.r2", index=2, approve_sha=HEAD)
        r["reviews"]["rounds"].append(second)
        self.refused(r, "approved must be the number of rounds that count")
        r["reviews"]["approved"] = 2
        receipt.check_receipt(r)

    def test_the_other_rules_of_the_fields(self) -> None:
        cases = [
            (("schema",), "wouf.receipt.v2", "receipt.schema must be wouf.receipt.v1"),
            (("team",), "Werkstatt", "receipt.team must be"), (("team",), "w", "receipt.team must be"),
            (("signed_at",), "2026-11-06 12:00:00", "receipt.signed_at must be a real UTC time"),
            (("signed_at",), "2026-02-30T12:00:00Z", "receipt.signed_at must be a real UTC time"),
            (("resigned_at",), "2026-11-05T12:00:00Z", "resigned_at must not be earlier"),
            (("resigned_at",), "", "receipt.resigned_at must be a real UTC time"),
            (("repo",), "", "receipt.repo must be a label"), (("repo",), None, "receipt.repo must be a label"),
            (("task",), "../x", "receipt.task must be a task id"), (("class",), "Security", "receipt.class must be"),
            (("brief", "sha256"), "AB" * 32, "receipt.brief.sha256 must be 64 lower-case hex"),
            (("brief", "source"), "git", "receipt.brief.source must be one of: flow, file"),
            (("change", "pr"), 0, "receipt.change.pr must be a whole number from 1"),
            (("gate", "state"), "red", "receipt.gate.state must be one of: green"),
            (("gate", "head"), OTHER, "receipt.gate.head must equal receipt.change.reviewed"),
            (("gate", "base"), OTHER, "receipt.gate.base must equal receipt.change.base"),
            (("gate", "code"), 1, "receipt.gate.code must be 0 or null"),
            (("gate", "network"), "egress", "receipt.gate.network must be one of"),
            (("gate", "command_sha256"), "x", "command_sha256 must be 64 lower-case hex or unknown"),
            (("gate", "complexity"), "none", "receipt.gate.complexity must be an object"),
            (("gate", "complexity", "failures"), -1, "outside 0 to 2^53-1"),
            (("reviews", "rounds"), {}, "receipt.reviews.rounds must be a list"),
            (("reviews", "rounds", 0, "index"), 0, "rounds[0].index must be a whole number from 1"),
            (("reviews", "rounds", 0, "bot"), "Review", "rounds[0].bot must be a bot id"),
            (("reviews", "rounds", 0, "cli"), "/usr/bin/codex", "rounds[0].cli must be the basename"),
            (("reviews", "rounds", 0, "vendor"), "anthropic", "rounds[0].vendor must be the vendor of"),
            (("reviews", "rounds", 0, "verdict"), "APPROVE", "rounds[0].verdict must be one of"),
            (("reviews", "rounds", 0, "counts"), 1, "rounds[0].counts must be true or false"),
            (("reviews", "rounds", 0, "source"), "file", "rounds[0].source must be one of: flow, run"),
            (("runs",), {}, "receipt.runs must be a list"),
            (("runs", 0, "tier"), "mid→strong", "not printable ASCII"),
            (("runs", 0, "tier"), "max", "receipt.runs[0].tier must be"),
            (("runs", 0, "tokens"), "10/20/30", "receipt.runs[0].tokens must be"),
            (("runs", 0, "egress", "mode"), "proxied", "egress.mode must be one of"),
            (("runs", 0, "egress", "log"), "egress.log", "egress.log must be 64 lower-case hex"),
            (("runs", 0, "egress", "hosts"), ["pypi.org:443", "api.example.com:443"], "must be sorted, each once"),
            (("runs", 0, "egress", "hosts"), ["pypi.org:443", "pypi.org:443"], "must be sorted, each once"),
            (("runs", 0, "egress", "hosts"), ["pypi.org"], "must be a list of host:port"),
            (("usage", "cost"), 0, "receipt.usage.cost must be unknown"),
            (("usage", "tokens"), 99, "receipt.usage.tokens must be the sum"),
            (("usage", "known_runs"), 2, "known_runs and unknown_runs must count"),
            (("flags",), {"sovereign": False}, "receipt.flags must be"),
            (("flags",), {"sovereign": 1}, "receipt.flags must be"),
            (("flags",), {"other": True}, "receipt.flags has an unknown key"),
        ]
        for path, bad, words in cases:
            with self.subTest(path=path, bad=bad):
                self.refused(self.changed(path, bad), words)
        for path, good in ((("flags",), {"sovereign": True}), (("gate", "complexity"), "unknown"),
                           (("gate", "code"), None), (("change", "pr"), None), (("gate", "command_sha256"), "unknown"),
                           (("runs", 0, "egress", "log"), "unknown"), (("resigned_at",), "2026-11-07T08:00:00Z")):
            with self.subTest(path=path, good=good):
                receipt.check_receipt(self.changed(path, good))


class SecretTest(unittest.TestCase):
    def test_a_planted_key_shape_is_refused_with_the_path_and_the_kind_and_never_signed(self) -> None:
        for path, value, kind, where in (
                (("repo",), AWS, "AWS access key", "receipt.repo"),
                (("runs", 0, "model"), "m-" + AWS, "AWS access key", "receipt.runs[0].model"),
                (("reviews", "rounds", 0, "runner"), TOKEN, "a token", "receipt.reviews.rounds[0].runner"),
                (("runs", 1, "egress", "hosts"), [TOKEN + ".example:443"], "a token",
                 "receipt.runs[1].egress.hosts[0]")):
            with self.subTest(where=where):
                r = sample()
                at(r, path[:-1])[path[-1]] = value
                with patch.object(receipt.subprocess, "run", side_effect=AssertionError("ssh-keygen ran")):
                    with self.assertRaises(Refused) as caught:
                        receipt.sign(r, "/nonexistent/key")
                message = str(caught.exception)
                self.assertIn(f"{where} looks like it contains a secret ({kind})", message)
                self.assertNotIn(AWS, message)
                self.assertNotIn(TOKEN, message)
        r = sample()
        r["flags"] = {AWS: True}
        with self.assertRaises(Refused) as caught:
            receipt.check_receipt(r)
        self.assertIn("a key of receipt.flags looks like it contains a secret (AWS access key)", str(caught.exception))
        self.assertNotIn(AWS, str(caught.exception))

    def test_a_registered_value_is_refused_too(self) -> None:
        r = sample()
        r["repo"] = "repo-plain-registered-7731"
        with patch.object(core, "KNOWN", ["plain-registered-7731"]), patch.object(receipt, "KNOWN",
                                                                                  ["plain-registered-7731"]):
            with self.assertRaises(Refused) as caught:
                receipt.check_receipt(r)
        self.assertIn("receipt.repo looks like it contains a secret (a registered secret value)", str(caught.exception))
        self.assertNotIn("7731", str(caught.exception))

    def test_the_sig_is_not_scanned(self) -> None:
        sig = "-----BEGIN SSH SIGNATURE-----\n" + TOKEN + "\n-----END SSH SIGNATURE-----\n"
        self.assertEqual(receipt.parse(file_bytes(sig=sig))["sig"], sig)

    def test_the_value_reaches_no_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            r = sample()
            r["runs"][0]["model"] = AWS
            planted = Path(tmp) / "receipt.json"
            planted.write_text(json.dumps(r))
            (Path(tmp) / "planted.receipt").write_bytes(file_bytes(r))
            script = (
                "import json, sys\n"
                f"sys.path.insert(0, {str(BIN)!r})\n"
                "from teamlib import receipt\n"
                "value = json.loads(open(sys.argv[1]).read())\n"
                "for step in (lambda: receipt.sign(value, sys.argv[2]),\n"
                "             lambda: receipt.parse(open(sys.argv[3], 'rb').read()),\n"
                "             lambda: receipt.stored(value, sys.argv[4])):\n"
                "    try:\n"
                "        step()\n"
                "    except receipt.Refused as problem:\n"
                "        print(problem, file=sys.stderr)\n"
                "        print('refused')\n"
                "    print(receipt.signature_ok(value, sys.argv[4], sys.argv[1]))\n")
            done = subprocess.run([sys.executable, "-c", script, str(planted), str(Path(tmp) / "key"),
                                   str(Path(tmp) / "planted.receipt"), FAKE_SIG], capture_output=True, text=True,
                                  timeout=60, env={**os.environ, "WOUF_TEAM_DIR": str(Path(tmp) / "team")})
        self.assertEqual(done.returncode, 0, done.stderr[-300:])
        self.assertEqual(done.stdout.split(), ["refused", "False"] * 3)
        self.assertEqual(done.stderr.count("receipt.runs[0].model looks like it contains a secret (AWS access key)"),
                         3)
        self.assertNotIn(AWS, done.stdout + done.stderr)


# One table of allowed_signers lines for receipt's and federate's trusted-line rule.
PUB = "AAAAC3NzaC1lZDI1NTE5AAAAIFakeFakeFakeFakeFakeFakeFakeFakeFakeFake"
LINES = [
    f'werkstatt namespaces="wouf-receipt-v1" ssh-ed25519 {PUB}',
    f"werkstatt ssh-ed25519 {PUB} comment",
    f'werkstatt,other valid-before="20261231Z",namespaces="wouf-receipt-v1" ssh-ed25519 {PUB}',
    f"  werkstatt ssh-ed25519 {PUB}",
    f"werkstatt cert-authority ssh-ed25519 {PUB}",
    f'werkstatt cert-authority,namespaces="wouf-receipt-v1" ssh-ed25519 {PUB}',
    f'werkstatt namespaces="x",cert-authority ssh-ed25519 {PUB}',
    f"werkstatt ecdsa-sha2-nistp256 {PUB}",
    f"werkstatt ssh-rsa {PUB}",
    f"werkstatt sk-ssh-ed25519@openssh.com {PUB}",
    f"werkstatt ssh-ed25519-cert-v01@openssh.com {PUB}",
    f"other ssh-ed25519 {PUB}",
    f"werkstatt-2 ssh-ed25519 {PUB}",
    f"*@werkstatt ssh-ed25519 {PUB}",
    f"# werkstatt ssh-ed25519 {PUB}",
    "",
    "   ",
    f'werkstatt namespaces="unclosed ssh-ed25519 {PUB}',
    f"werkstatt {PUB}",
]
# ssh-keygen reads option names in any case: receipt drops these CA lines, federate's rule (case-sensitive) keeps them.
STRICTER = [f"werkstatt CERT-AUTHORITY ssh-ed25519 {PUB}", f"werkstatt Cert-Authority ssh-ed25519 {PUB}"]


class TrustedLinesTest(unittest.TestCase):
    def test_receipt_and_federate_agree_on_one_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            team = Path(tmp) / "team"
            (team / "federation").mkdir(parents=True)
            with patch.dict(os.environ, WOUF_TEAM_DIR=str(team)):
                federate = helper(BIN / "federate", "wouf_federate_trust")
                signers = team / "federation" / "allowed_signers"
                for principal in ("werkstatt", "other"):
                    signers.write_text("\n".join(LINES) + "\n")
                    self.assertEqual(receipt.trusted_lines("\n".join(LINES) + "\n", principal),
                                     federate.trusted_lines(principal))
                    for line in LINES:
                        with self.subTest(principal=principal, line=line):
                            signers.write_text(line + "\n")
                            self.assertEqual(receipt.trusted_lines(line + "\n", principal),
                                             federate.trusted_lines(principal))
                    for line in STRICTER:
                        signers.write_text(line + "\n")
                        self.assertEqual(receipt.trusted_lines(line + "\n", principal), [])
                        self.assertEqual(federate.trusted_lines(principal), [line] if principal == "werkstatt" else [])
        self.assertEqual(receipt.trusted_lines("\n".join(LINES), "werkstatt"), LINES[:4])


def keypair(path: Path, kind: str = "ed25519") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ssh-keygen", "-q", "-t", kind, "-N", "", "-C", "test", "-f", str(path)], check=True)
    return path


def public(key: Path) -> str:
    return " ".join(Path(f"{key}.pub").read_text().split()[:2])


@unittest.skipUnless(shutil.which("ssh-keygen"), "needs OpenSSH's ssh-keygen (8.7 or newer) for -Y sign and verify")
class SignatureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.team = self.tmp / "team"
        self.team.mkdir()
        env = patch.dict(os.environ, WOUF_TEAM_DIR=str(self.team))
        env.start()
        self.addCleanup(env.stop)
        self.key = keypair(self.tmp / "keys" / "receipt")
        self.signers = self.signers_file(f'werkstatt namespaces="wouf-receipt-v1" {public(self.key)}')

    def signers_file(self, *lines: str) -> Path:
        path = self.tmp / f"signers-{len(list(self.tmp.glob('signers-*')))}"
        path.write_text("".join(f"{line}\n" for line in lines))
        return path

    def raw_sign(self, key: Path, data: bytes, namespace: str = "wouf-receipt-v1", env=None) -> str:
        done = subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(key), "-n", namespace], input=data,
                              capture_output=True, check=True, env=env)
        return done.stdout.decode()

    def raw_verify(self, signers: Path, sig: str, data: bytes, *options: str) -> int:
        sig_path = self.tmp / "raw.sig"
        sig_path.write_text(sig)
        return subprocess.run(["ssh-keygen", "-Y", "verify", "-f", str(signers), "-I", "werkstatt", "-n",
                               "wouf-receipt-v1", "-s", str(sig_path), *options], input=data,
                              capture_output=True).returncode

    def test_sign_and_verify(self) -> None:
        r = sample()
        sig = receipt.sign(r, str(self.key))
        self.assertTrue(receipt.SIG.match(sig))
        self.assertTrue(receipt.signature_ok(r, sig, self.signers))
        self.assertTrue(receipt.signature_ok(**receipt.parse(receipt.stored(r, sig)), signers=self.signers))
        self.assertFalse(receipt.signature_ok(r, sig, self.tmp / "no-such-file"))
        self.assertFalse(receipt.signature_ok(r, FAKE_SIG, self.signers))

    def test_the_key_comes_from_the_federation_config(self) -> None:
        (self.team / "config.ini").write_text(f"[federation]\nname = werkstatt\nsigning_key = {self.key}\n")
        r = sample()
        self.assertTrue(receipt.signature_ok(r, receipt.sign(r), self.signers))

    def test_one_changed_byte_of_the_body_fails(self) -> None:
        r = sample()
        sig = receipt.sign(r, str(self.key))
        body = receipt.canonical(r)
        for old, new in ((b'"repo":"wouf"', b'"repo":"wouh"'), (b'"files":5', b'"files":6'),
                         (LANDED.encode(), LANDED[:-1].encode() + b"9"), (b"12:00:00Z", b"12:00:01Z"),
                         (b'"model":"default","outcome":"ready', b'"model":"defaulu","outcome":"ready')):
            with self.subTest(new=new):
                changed = body.replace(old, new, 1)
                self.assertEqual(len(changed), len(body))
                self.assertEqual(sum(a != b for a, b in zip(body, changed)), 1)
                other = json.loads(changed)
                receipt.check_receipt(other)
                self.assertFalse(receipt.signature_ok(other, sig, self.signers))
                self.assertNotEqual(self.raw_verify(self.signers, sig, changed), 0)

    def test_a_signature_in_another_namespace_fails(self) -> None:
        r = sample()
        for namespace in ("wouf-a2a", "wouf-receipt-v2", "file"):
            with self.subTest(namespace=namespace):
                sig = self.raw_sign(self.key, receipt.canonical(r), namespace)
                self.assertFalse(receipt.signature_ok(r, sig, self.signers))
        open_line = self.signers_file(f"werkstatt {public(self.key)}")  # a line without namespaces= limits nothing
        self.assertFalse(receipt.signature_ok(r, self.raw_sign(self.key, receipt.canonical(r), "wouf-a2a"), open_line))
        self.assertTrue(receipt.signature_ok(r, receipt.sign(r, str(self.key)), open_line))
        a2a_line = self.signers_file(f'werkstatt namespaces="wouf-a2a" {public(self.key)}')
        self.assertFalse(receipt.signature_ok(r, receipt.sign(r, str(self.key)), a2a_line))

    def test_a_non_ed25519_line_is_ignored(self) -> None:
        r = sample()
        ecdsa = keypair(self.tmp / "keys" / "ecdsa", "ecdsa")
        sig = self.raw_sign(ecdsa, receipt.canonical(r))
        signers = self.signers_file(f'werkstatt namespaces="wouf-receipt-v1" {public(ecdsa)}')
        self.assertEqual(self.raw_verify(signers, sig, receipt.canonical(r)), 0)  # ssh-keygen alone would accept it
        self.assertFalse(receipt.signature_ok(r, sig, signers))
        with self.assertRaises(Refused) as caught:
            receipt.sign(r, str(ecdsa))
        self.assertIn("must be an Ed25519 key", str(caught.exception))

    @unittest.skipUnless(shutil.which("ssh-agent") and shutil.which("ssh-add"),
                         "needs ssh-agent and ssh-add to sign with a certificate")
    def test_a_cert_authority_line_is_ignored(self) -> None:
        r = sample()
        ca = keypair(self.tmp / "keys" / "ca")
        subprocess.run(["ssh-keygen", "-q", "-s", str(ca), "-I", "test", "-n", "werkstatt", f"{self.key}.pub"],
                       check=True)
        sock = Path(tempfile.mkdtemp(prefix="agent-", dir="/tmp")) / "s"
        self.addCleanup(shutil.rmtree, sock.parent)
        agent = subprocess.Popen(["ssh-agent", "-D", "-a", str(sock)], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        self.addCleanup(agent.wait)
        self.addCleanup(agent.terminate)
        env = {**os.environ, "SSH_AUTH_SOCK": str(sock)}
        for _ in range(100):
            if sock.exists():
                break
            time.sleep(0.05)
        subprocess.run(["ssh-add", "-q", str(self.key)], env=env, check=True, capture_output=True)
        sig = self.raw_sign(Path(f"{self.key}-cert.pub"), receipt.canonical(r), env=env)
        for option in ("cert-authority", "CERT-AUTHORITY"):
            with self.subTest(option=option):
                signers = self.signers_file(f'werkstatt {option},namespaces="wouf-receipt-v1" {public(ca)}')
                self.assertEqual(self.raw_verify(signers, sig, receipt.canonical(r), "-O",
                                                 "verify-time=20261106120000Z"), 0)
                self.assertFalse(receipt.signature_ok(r, sig, signers))

    def test_a_revoked_key_fails(self) -> None:
        r = sample()
        sig = receipt.sign(r, str(self.key))
        revoked = self.tmp / "revoked_keys"
        revoked.write_text(Path(f"{self.key}.pub").read_text())
        self.assertFalse(receipt.signature_ok(r, sig, self.signers, revoked))
        revoked.write_text(Path(f"{keypair(self.tmp / 'keys' / 'other')}.pub").read_text())
        self.assertTrue(receipt.signature_ok(r, sig, self.signers, revoked))
        self.assertTrue(receipt.signature_ok(r, sig, self.signers, self.tmp / "no-revocation-file"))
        (self.tmp / "dangling").symlink_to(self.tmp / "nowhere")
        self.assertFalse(receipt.signature_ok(r, sig, self.signers, self.tmp / "dangling"))

    def test_a_valid_before_earlier_than_the_receipt_time_fails(self) -> None:
        r = sample()
        sig = receipt.sign(r, str(self.key))
        expired = self.signers_file(f'werkstatt valid-before="20261106115959Z",namespaces="wouf-receipt-v1" '
                                    f'{public(self.key)}')
        self.assertFalse(receipt.signature_ok(r, sig, expired))
        still = self.signers_file(f'werkstatt valid-before="20261106120000Z",namespaces="wouf-receipt-v1" '
                                  f'{public(self.key)}')
        self.assertTrue(receipt.signature_ok(r, sig, still))
        later = self.signers_file(f'werkstatt valid-after="20261106120001Z",namespaces="wouf-receipt-v1" '
                                  f'{public(self.key)}')
        self.assertFalse(receipt.signature_ok(r, sig, later))

    def test_resigned_at_is_the_time_used(self) -> None:
        line = f'namespaces="wouf-receipt-v1" {public(self.key)}'
        after = self.signers_file(f'werkstatt valid-after="20261201Z",{line}')
        before = self.signers_file(f'werkstatt valid-before="20261130Z",{line}')
        r = sample()
        self.assertEqual(receipt.signature_time(r), "2026-11-06T12:00:00Z")
        sig = receipt.sign(r, str(self.key))
        self.assertFalse(receipt.signature_ok(r, sig, after))
        self.assertTrue(receipt.signature_ok(r, sig, before))
        r["resigned_at"] = "2026-12-02T00:00:00Z"
        self.assertEqual(receipt.signature_time(r), "2026-12-02T00:00:00Z")
        sig = receipt.sign(r, str(self.key))
        self.assertTrue(receipt.signature_ok(r, sig, after))
        self.assertFalse(receipt.signature_ok(r, sig, before))

    def test_verify_time_is_the_form_ssh_keygen_takes(self) -> None:
        self.assertEqual(receipt.verify_time("2026-11-06T12:00:00Z"), "20261106120000Z")
        self.assertEqual(receipt.verify_time("0999-01-02T03:04:05Z"), "09990102030405Z")
        for bad in ("2026-11-06 12:00:00", "2026-11-06T12:00:00+00:00", "2026-13-01T00:00:00Z", None):
            with self.subTest(bad=bad), self.assertRaises(Refused):
                receipt.verify_time(bad)
        r = sample()
        data = receipt.canonical(r)
        sig = receipt.sign(r, str(self.key))
        self.assertEqual(self.raw_verify(self.signers, sig, data, "-O", "verify-time=20261106120000Z"), 0)
        self.assertNotEqual(self.raw_verify(self.signers, sig, data, "-O", "verify-time=2026-11-06T12:00:00Z"), 0)

    def test_the_signing_key_is_checked_as_federate_does_and_never_opened(self) -> None:
        r = sample()
        inside = keypair(self.team / "keys" / "inside")
        for key, words in ((inside, "outside the team folder"), (self.tmp / "missing", "no such file"),
                           (keypair(self.tmp / "keys" / "rsa", "rsa"), "must be an Ed25519 key")):
            with self.subTest(words=words), self.assertRaises(Refused) as caught:
                receipt.sign(r, str(key))
            self.assertIn(words, str(caught.exception))
        Path(f"{inside}.pub").rename(self.tmp / "moved.pub")
        lone = self.tmp / "keys" / "lone"
        shutil.copy(inside, lone)
        with self.assertRaises(Refused) as caught:
            receipt.sign(r, str(lone))
        self.assertIn(".pub next to it", str(caught.exception))
        with self.assertRaises(Refused) as caught:
            receipt.sign(r)  # no config.ini, so no [federation] signing_key
        self.assertIn("set [federation] signing_key", str(caught.exception))
        opened = []
        real = {"builtins": builtins.open, "io": io.open, "os": os.open}

        def recorder(name):
            def call(file, *args, **kwargs):
                opened.append(os.fspath(file) if isinstance(file, (str, os.PathLike)) else file)
                return real[name](file, *args, **kwargs)
            return call

        with patch.object(builtins, "open", recorder("builtins")), patch.object(io, "open", recorder("io")), \
                patch.object(os, "open", recorder("os")):
            sig = receipt.sign(r, str(self.key))
        self.assertIn(f"{self.key}.pub", opened)
        self.assertNotIn(str(self.key), opened)
        self.assertTrue(receipt.signature_ok(r, sig, self.signers))

    def test_without_ssh_keygen_signing_refuses(self) -> None:
        with patch.object(receipt.shutil, "which", return_value=None), self.assertRaises(Refused) as caught:
            receipt.sign(sample(), str(self.key))
        self.assertIn("ssh-keygen (OpenSSH 8.7 or newer) is needed", str(caught.exception))


class BracketDepthTest(unittest.TestCase):
    def test_brackets_inside_strings_and_escapes_do_not_count(self) -> None:
        cases = [({"a": "[[[\"]]", "b": [1, {"c": 2}]}, 3), ([[[]]], 3), ({"x": "\\"}, 1),
                 ({"x": "\\\"{{{"}, 1), ("plain", 0)]
        for value, depth in cases:
            with self.subTest(value=value):
                self.assertEqual(receipt.bracket_depth(json.dumps(value)), depth)

    def test_depth_is_refused_before_the_decoder_on_every_python(self) -> None:
        # Python 3.14's decoder parses 5000 levels without RecursionError; the pre-check must not depend on it.
        with self.assertRaises(receipt.Refused) as caught:
            receipt.parse(b"[" * 40 + b"]" * 40 + b"\n")
        self.assertIn("nested too deep", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
