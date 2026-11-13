"""Tests for .agents/bin/teamlib/attest.py (receipts R3b): `team receipt make` without --dry-run signs the body of
R3a with the federation key and stores it as SPEC 2.6 says (team/receipts/<landed>.json, the receipt.signed event,
the note refs/notes/wouf-receipts on the landed commit, one set of bytes), and `team receipt resign SHA` re-signs a
stored receipt without changing any other field. A real git repository and real ssh-keygen keys; the records come
from test_receipt_build's fixtures."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from test_receipt import keypair, public
from test_receipt_build import TASK, Records

from teamlib import attest, receipt  # noqa: E402
from support import isolate_wouf_env  # noqa: E402

NOTES = "refs/notes/wouf-receipts"
FIRST, LATER = "2026-11-07T10:00:00Z", "2026-11-08T09:30:00Z"


def setUpModule() -> None:
    isolate_wouf_env()


@unittest.skipUnless(shutil.which("ssh-keygen"), "needs OpenSSH's ssh-keygen (8.7 or newer) for -Y sign and verify")
class Store(Records):
    def setUp(self) -> None:
        super().setUp()
        self.git("config", "user.name", "Test Lead")
        self.git("config", "user.email", "lead@example.invalid")
        self.key = keypair(self.root / "keys" / "receipt")
        self.use_key(self.key)
        self.happy()
        self.landed = self.squash()
        self.file = self.team / "receipts" / f"{self.landed}.json"

    def use_key(self, key: Path | None) -> None:
        """[federation] signing_key of the config the commands get (None: not set)."""
        if key:
            self.ini.set("federation", "signing_key", str(key))
        else:
            self.ini.remove_option("federation", "signing_key")

    # ------------------------------------------------------------ calls

    def make(self, at: str = FIRST, **opts) -> str:
        o = {"--reviewed": self.reviewed, "--landed": self.landed, "--repo": None, "--repo-name": "wouf",
             "--pr": 7, "--brief": None, **opts}
        with mock.patch.object(attest, "stamp", return_value=at):
            return attest.store(self.ini, TASK, o)

    def resign(self, at: str = LATER) -> str:
        with mock.patch.object(attest, "stamp", return_value=at):
            return attest.resign(self.ini, self.landed, {"--repo": None})

    def refused(self, call, text: str) -> None:
        with self.assertRaises(receipt.Refused) as caught:
            call()
        self.assertIn(text, str(caught.exception))
        self.assertEqual(len(str(caught.exception).splitlines()), 1)

    # ------------------------------------------------------------ what is stored

    def note(self) -> bytes | None:
        done = subprocess.run(["git", "-C", str(self.repo), "notes", f"--ref={NOTES}", "show", self.landed],
                              capture_output=True)
        return done.stdout if done.returncode == 0 else None

    def notes_head(self) -> str | None:
        done = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "-q", "--verify", NOTES],
                              capture_output=True, text=True)
        return done.stdout.strip() or None

    def receipt_events(self) -> list:
        folder = self.team / "events"
        lines = []
        for path in folder.glob("*.jsonl"):
            lines += [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        return [(e["kind"], e["task"], e["data"]) for e in lines if e["kind"].startswith("receipt.")]

    def signers(self, key: Path) -> Path:
        path = self.root / f"signers-{key.name}"
        path.write_text(f'werkstatt namespaces="wouf-receipt-v1" {public(key)}\n')
        return path

    def well_signed(self, data: bytes, key: Path) -> dict:
        value = receipt.parse(data)
        self.assertTrue(receipt.signature_ok(value["receipt"], value["sig"], self.signers(key)))
        return value

    def nothing_written(self) -> None:
        self.assertFalse(self.file.exists())
        self.assertEqual(list((self.team / "receipts").glob("*.json")) if (self.team / "receipts").exists() else [],
                         [])
        self.assertIsNone(self.note())
        self.assertEqual(self.receipt_events(), [])

    def event_of(self, kind: str, data: bytes) -> tuple:
        return (kind, TASK, {"task": TASK, "landed": self.landed, "reviewed": self.reviewed,
                             "sha256": hashlib.sha256(data).hexdigest()})


class MakeTest(Store):
    def test_make_stores_file_event_and_note_with_the_same_bytes(self) -> None:
        self.make()
        data = self.file.read_bytes()
        value = self.well_signed(data, self.key)
        self.assertEqual(data, receipt.canonical(value) + b"\n")
        self.assertEqual((value["receipt"]["signed_at"], value["receipt"]["resigned_at"]), (FIRST, None))
        self.assertEqual(value["receipt"]["change"]["landed"], self.landed)
        self.assertEqual(self.note(), data)
        self.assertEqual(self.receipt_events(), [self.event_of("receipt.signed", data)])

    def test_the_same_call_again_changes_nothing(self) -> None:
        self.make()
        data, head, events = self.file.read_bytes(), self.notes_head(), self.receipt_events()
        self.assertIn("unchanged", self.make(at=LATER))
        self.assertEqual((self.file.read_bytes(), self.note(), self.notes_head(), self.receipt_events()),
                         (data, data, head, events))

    def test_a_different_receipt_for_the_same_landed_commit_is_refused(self) -> None:
        self.make()
        data, head, events = self.file.read_bytes(), self.notes_head(), self.receipt_events()
        self.refused(lambda: self.make(**{"--pr": 8}), "a different receipt for the landed commit is stored")
        self.assertEqual((self.file.read_bytes(), self.notes_head(), self.receipt_events()), (data, head, events))

    def test_a_different_note_already_there_is_refused(self) -> None:
        self.git("notes", f"--ref={NOTES}", "add", "-m", "someone else's note", self.landed)
        before = self.note()
        self.refused(self.make, "the receipt note")
        self.assertFalse(self.file.exists())
        self.assertEqual((self.note(), self.receipt_events()), (before, []))

    def test_a_note_that_is_not_the_stored_file_is_refused(self) -> None:
        self.make()
        data = self.file.read_bytes()
        self.git("notes", f"--ref={NOTES}", "add", "-f", "-m", "replaced", self.landed)
        self.refused(self.make, "the receipt note")
        self.assertEqual(self.file.read_bytes(), data)

    def test_a_missing_note_or_event_is_completed_from_the_stored_file(self) -> None:
        self.make()
        data = self.file.read_bytes()
        self.git("notes", f"--ref={NOTES}", "remove", self.landed)
        self.make(at=LATER)
        self.assertEqual((self.file.read_bytes(), self.note()), (data, data))
        self.assertEqual(self.receipt_events(), [self.event_of("receipt.signed", data)])

    def test_a_stored_file_with_a_bad_signature_is_refused(self) -> None:
        self.make()
        value = receipt.parse(self.file.read_bytes())
        other = keypair(self.root / "keys" / "other")
        forged = receipt.stored(value["receipt"], receipt.sign(value["receipt"], str(other)))
        self.file.write_bytes(forged)
        self.git("notes", f"--ref={NOTES}", "add", "-f", "-F", str(self.file), self.landed)
        self.refused(self.make, "not signed by the current key")

    def test_a_hand_edited_stored_file_is_refused(self) -> None:
        self.make()
        self.git("notes", f"--ref={NOTES}", "remove", self.landed)  # the file alone holds it
        self.file.write_bytes(self.file.read_bytes().replace(b'"repo":"wouf"', b'"repo": "wouf"'))
        self.refused(self.make, "canonical")
        self.assertIsNone(self.note())

    def test_a_missing_signing_key_stops_before_anything_is_written(self) -> None:
        self.use_key(None)
        self.refused(self.make, "set [federation] signing_key")
        self.nothing_written()

    def test_a_signing_key_that_is_not_ed25519_stops_before_anything_is_written(self) -> None:
        self.use_key(keypair(self.root / "keys" / "ecdsa", "ecdsa"))
        self.refused(self.make, "must be an Ed25519 key")
        self.nothing_written()

    def test_a_refused_build_writes_nothing(self) -> None:
        self.refused(lambda: self.make(**{"--reviewed": self.base}), "")
        self.nothing_written()

    def test_a_signature_that_does_not_verify_is_never_stored(self) -> None:
        with mock.patch.object(attest, "signed_by", return_value=False):
            self.refused(self.make, "not signed by the current key")
        self.nothing_written()

    def test_the_private_key_is_never_opened(self) -> None:
        opened = []
        real_open, real_path_open, real_os_open = open, Path.open, attest.os.open

        def seen(path):
            opened.append(Path(path).resolve() if isinstance(path, (str, Path)) else None)

        def path_open(path, *args, **kwargs):
            seen(path)
            return real_path_open(path, *args, **kwargs)

        def any_open(path, *args, **kwargs):
            seen(path)
            return real_open(path, *args, **kwargs)

        def os_open(path, *args, **kwargs):
            seen(path)
            return real_os_open(path, *args, **kwargs)
        with mock.patch.object(Path, "open", path_open), mock.patch("builtins.open", any_open), \
                mock.patch.object(attest.os, "open", os_open):
            self.make()
        self.assertNotIn(self.key.resolve(), opened)
        self.assertTrue(self.file.exists())


class ResignTest(Store):
    def test_resign_keeps_signed_at_and_every_other_field_and_signs_with_the_current_key(self) -> None:
        self.make()
        old = receipt.parse(self.file.read_bytes())["receipt"]
        new_key = keypair(self.root / "keys" / "rotated")
        self.use_key(new_key)
        self.resign()
        data = self.file.read_bytes()
        new = self.well_signed(data, new_key)["receipt"]
        self.assertEqual((new["signed_at"], new["resigned_at"]), (FIRST, LATER))
        self.assertEqual({k: v for k, v in new.items() if k != "resigned_at"},
                         {k: v for k, v in old.items() if k != "resigned_at"})
        self.assertEqual(self.note(), data)
        signed = self.receipt_events()[0]
        self.assertEqual(self.receipt_events(), [signed, self.event_of("receipt.resigned", data)])
        self.assertIn("unchanged", self.make(at=LATER))  # make accepts the re-signed receipt as its own

    def test_a_resign_that_changes_another_field_is_refused(self) -> None:
        self.make()
        data, head, events = self.file.read_bytes(), self.notes_head(), self.receipt_events()
        for change in ({"repo": "other"}, {"extra": 1}):
            def smuggled(body, at, change=change):
                return {**body, "resigned_at": at, **change}
            with mock.patch.object(attest, "resigned_of", smuggled):
                self.refused(self.resign, "a resign changes signed_at and resigned_at only")
        self.assertEqual((self.file.read_bytes(), self.notes_head(), self.receipt_events()), (data, head, events))

    def test_resign_without_a_stored_receipt_is_refused(self) -> None:
        self.refused(self.resign, "no stored receipt")
        self.nothing_written()

    def test_resign_with_a_missing_or_wrong_key_writes_nothing(self) -> None:
        self.make()
        data, head, events = self.file.read_bytes(), self.notes_head(), self.receipt_events()
        for key in (None, keypair(self.root / "keys" / "ecdsa", "ecdsa")):
            self.use_key(key)
            self.refused(self.resign, "signing_key")
        self.assertEqual((self.file.read_bytes(), self.notes_head(), self.receipt_events()), (data, head, events))

    def test_resign_refuses_a_hand_edited_file_with_the_old_signature(self) -> None:
        for note in ("none", "forged"):
            with self.subTest(note=note):
                self.make()
                value = receipt.parse(self.file.read_bytes())
                forged = receipt.stored({**value["receipt"], "repo": "forged"}, value["sig"])
                self.file.write_bytes(forged)
                self.git("notes", f"--ref={NOTES}", "remove", self.landed)
                if note == "forged":
                    self.git("notes", f"--ref={NOTES}", "add", "-F", str(self.file), self.landed)
                head, events = self.notes_head(), self.receipt_events()
                self.use_key(keypair(self.root / "keys" / f"rotated-{note}"))
                self.refused(self.resign, "not the one the lead's newest receipt event records")
                self.assertEqual((self.file.read_bytes(), self.notes_head(), self.receipt_events()),
                                 (forged, head, events))
                self.use_key(self.key)
                self.file.unlink()
                self.git("notes", f"--ref={NOTES}", "remove", "--ignore-missing", self.landed)

    def test_resign_twice_signs_the_newest_recorded_receipt(self) -> None:
        self.make()
        self.resign()
        self.resign(at="2026-11-09T08:00:00Z")
        data = self.file.read_bytes()
        self.assertEqual(self.well_signed(data, self.key)["receipt"]["resigned_at"], "2026-11-09T08:00:00Z")
        self.assertEqual(self.receipt_events()[-1], self.event_of("receipt.resigned", data))

    def test_resign_refuses_a_note_with_another_receipt(self) -> None:
        self.make()
        data = self.file.read_bytes()
        self.git("notes", f"--ref={NOTES}", "add", "-f", "-m", "replaced", self.landed)
        self.refused(self.resign, "the receipt note")
        self.assertEqual(self.file.read_bytes(), data)


class CommandTest(Store):
    def run_cmd(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(attest, "config", return_value=self.ini):
            try:
                code = attest.do_receipt(list(args))
            except SystemExit as stop:
                code = stop.code
        return code, out.getvalue()

    def test_make_and_resign_on_the_command_line(self) -> None:
        code, out = self.run_cmd("make", TASK, "--reviewed", self.reviewed, "--landed", self.landed,
                                 "--repo-name", "wouf")
        self.assertEqual(code, 0)
        self.assertIn(f"team/receipts/{self.landed}.json", out)
        code, out = self.run_cmd("resign", self.landed)
        self.assertEqual(code, 0)
        self.assertIn("re-signed", out)
        self.assertIsNotNone(receipt.parse(self.file.read_bytes())["receipt"]["resigned_at"])
        self.assertIn("team receipt resign SHA", attest.USAGE)


if __name__ == "__main__":
    unittest.main()
