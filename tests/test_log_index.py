"""Warm receipt cache regressions and a reproducible 5,000-receipt benchmark.

Run ``python3 tests/test_log_index.py --benchmark [path/to/log]`` for five
warm CLI timings and a count of cached rows re-masked by a second query.
"""
import contextlib
import errno
import io
import hashlib
import hmac
import socket
import stat
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

if __package__:
    from .test_log import LOG, helper
else:
    from test_log import LOG, helper


def fixture(directory, count=5000, signal="invoice migration"):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "worker.jsonl").open("w", encoding="utf-8") as handle:
        for n in range(count):
            handle.write(json.dumps(dict(t="2026-10-19T09:00:00+00:00", who="worker",
                                         task=f"T-{n}", outcome="done", signal=signal,
                                         ref="", cost="")) + "\n")


def counted_find(module):
    """Count cells only inside masked_out; each receipt has seven string cells."""
    calls = []
    original, mask = module.masked_out, module.mask

    def scan(db):
        def counted(text):
            calls.append(text)
            return mask(text)
        with patch.object(module, "mask", counted):
            return original(db)

    with patch.object(module, "masked_out", scan), contextlib.redirect_stdout(io.StringIO()):
        module.cmd_find(["invoice"])
    return len(calls) // len(module.FIELDS)


class LogIndexTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.logs, self.store = self.root / "logs", self.root / "store"
        self.store.mkdir()
        caller = {k: v for k, v in os.environ.items() if not k.startswith("WOUF_")}
        self.env = dict(caller, WOUF_LOG_DIR=str(self.logs),
                        WOUF_SECRET_DIR=str(self.store), WOUF_SECRET_BACKEND="file", WOUF_MASK="[]")
        environment = patch.dict(os.environ, self.env, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        fixture(self.logs)
        self.module = helper(LOG)

    def index(self):
        db = self.module.index()
        self.addCleanup(db.close)
        return db

    def test_setup_clears_inherited_wouf_environment(self):
        with patch.dict(os.environ, WOUF_KNOWLEDGE_DB="/nonexistent/x.sqlite",
                        WOUF_CALLER_ONLY="inherited"):
            case = LogIndexTest("test_installation_salt_and_all_hashes")
            try:
                case.setUp()
                self.assertFalse("WOUF_KNOWLEDGE_DB" in os.environ)
                self.assertFalse("WOUF_CALLER_ONLY" in os.environ)
                self.assertFalse("WOUF_KNOWLEDGE_DB" in case.env)
                case.test_installation_salt_and_all_hashes()
            finally:
                case.doCleanups()
            self.assertEqual(os.environ["WOUF_KNOWLEDGE_DB"], "/nonexistent/x.sqlite")
            self.assertEqual(os.environ["WOUF_CALLER_ONLY"], "inherited")

    def test_installation_salt_and_all_hashes(self):
        self.module.STORED = self.module.REGISTERED = ["fake-low-entropy-value"]
        first = self.module.cache_hash(repr(sorted(set(self.module.REGISTERED))).encode())
        salt = self.root / "env" / ".salt"
        self.assertEqual(len(salt.read_bytes()), 32)
        self.assertEqual(stat.S_IMODE(salt.stat().st_mode), 0o600)
        data = repr(sorted(set(self.module.REGISTERED))).encode()
        self.assertEqual(first, hmac.new(salt.read_bytes(), data, "sha256").hexdigest())
        self.assertNotEqual(first, hashlib.sha256(data).hexdigest())
        db = self.index()
        receipt_stamp = db.execute("SELECT v FROM meta WHERE k='stamp'").fetchone()[0]
        self.assertEqual(receipt_stamp.rsplit(":", 1)[1], first[:16])
        self.assertNotEqual(receipt_stamp.rsplit(":", 1)[1], hashlib.sha256(data).hexdigest()[:16])
        db.close()
        with patch.dict(os.environ, WOUF_LOG_DIR=str(self.root / "second" / "logs")):
            second = self.module.cache_hash(data)
            self.assertNotEqual(first, second)
            db = self.index()
            stamp = db.execute("SELECT v FROM meta WHERE k='stamp'").fetchone()[0]
            self.assertEqual(stamp.rsplit(":", 1)[1], second[:16])
            self.assertNotEqual(stamp.rsplit(":", 1)[1], receipt_stamp.rsplit(":", 1)[1])
            db.close()
        # All disk digests, including source contents, must use the keyed helper.
        self.assertNotIn("hashlib.sha256(", LOG.read_text())

    def fresh(self):
        """The salt is read once per process, so a changed file is seen by a new module (a new process)."""
        self.module = helper(LOG)
        return self.module

    def assert_private_salt(self):
        salt = self.root / "env" / ".salt"
        self.assertTrue(stat.S_ISREG(salt.lstat().st_mode))
        self.assertEqual(len(salt.read_bytes()), 32)
        self.assertEqual(stat.S_IMODE(salt.stat().st_mode), 0o600)
        return salt.read_bytes()

    def test_empty_directory_at_salt_is_removed_and_replaced(self):
        self.index().close()
        salt = self.root / "env" / ".salt"
        salt.unlink()
        salt.mkdir()
        self.fresh()
        self.index().close()
        self.assert_private_salt()
        self.assertFalse(list(salt.parent.glob(".salt.broken-*")))

    def test_non_empty_directory_at_salt_is_moved_aside_and_replaced(self):
        self.index().close()
        salt = self.root / "env" / ".salt"
        salt.unlink()
        salt.mkdir()
        (salt / "keep").write_text("kept")
        self.fresh()
        self.index().close()
        self.assert_private_salt()
        broken = self.root / "env" / ".salt.broken-1"
        self.assertTrue(broken.is_dir())
        self.assertEqual((broken / "keep").read_text(), "kept")

    def test_fifo_at_salt_is_replaced_without_blocking(self):
        self.index().close()
        salt = self.root / "env" / ".salt"
        salt.unlink()
        os.mkfifo(salt)
        self.fresh()
        self.index().close()
        self.assert_private_salt()

    def test_unix_socket_at_salt_is_replaced_without_failing(self):
        self.index().close()
        salt = self.root / "env" / ".salt"
        salt.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        previous_cwd = os.getcwd()
        try:
            os.chdir(salt.parent)
            listener.bind(salt.name)
        finally:
            os.chdir(previous_cwd)
        self.fresh()
        self.index().close()
        self.assert_private_salt()
        self.assertFalse(list((self.root / "env").glob(".salt.broken-*")))

    def test_unreadable_salt_is_replaced(self):
        self.index().close()
        salt = self.root / "env" / ".salt"
        salt.chmod(0o000)
        self.fresh()
        self.index().close()
        self.assert_private_salt()

    def test_read_error_of_any_errno_replaces_salt(self):
        first = self.fresh().installation_salt()
        with patch.object(os, "read", side_effect=OSError(errno.EIO, "injected")):
            second = self.fresh().installation_salt()
        self.assertNotEqual(first, second)
        self.assert_private_salt()
        self.assertFalse(list((self.root / "env").glob(".salt.broken-*")))

    def test_write_error_is_reported_without_traceback(self):
        self.index().close()
        salt = self.root / "env" / ".salt"
        salt.unlink()
        self.fresh()
        stderr = io.StringIO()
        with patch.object(os, "fsync", side_effect=OSError(errno.ENOSPC, "no space")), \
                contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as ended:
            self.module.installation_salt()
        self.assertEqual(ended.exception.code, 2)
        self.assertIn("cannot write the installation salt", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_salt_is_read_once_per_process(self):
        salt = self.root / "env" / ".salt"
        first = self.fresh().installation_salt()
        salt.unlink()
        self.assertEqual(self.module.installation_salt(), first)
        self.assertNotEqual(self.fresh().installation_salt(), first)

    def test_symlinked_and_broken_salt_recreated_without_following(self):
        self.index().close()
        salt = self.root / "env" / ".salt"
        target = self.root / "target"
        target.write_bytes(b"x" * 32)
        salt.unlink()
        salt.symlink_to(target)
        self.fresh().index().close()
        self.assertFalse(salt.is_symlink())
        self.assertEqual(target.read_bytes(), b"x" * 32)
        self.assertNotEqual(salt.read_bytes(), target.read_bytes())
        for mode in (0o644, 0o000):
            salt.chmod(mode)
            self.fresh().index().close()
            self.assertEqual(stat.S_IMODE(salt.stat().st_mode), 0o600)
        for content in (b"", b"x" * 31, b"x" * 33):
            salt.write_bytes(content)
            self.fresh().index().close()
            self.assertEqual(len(salt.read_bytes()), 32)
            self.assertEqual(stat.S_IMODE(salt.stat().st_mode), 0o600)

    def test_symlinked_env_directory_is_resolved_and_salt_lands_in_target(self):
        real = self.root / "real-env"
        real.mkdir()
        (self.root / "env").symlink_to(real, target_is_directory=True)
        for args in (["find", "invoice"], ["ask", "invoice"], ["state", "rebuild", "tasks"]):
            result = subprocess.run([sys.executable, str(LOG), *args], env=self.env,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        salt = real / ".salt"
        self.assertTrue(stat.S_ISREG(salt.lstat().st_mode))
        self.assertEqual(len(salt.read_bytes()), 32)
        self.assertEqual(stat.S_IMODE(salt.stat().st_mode), 0o600)
        self.assertTrue((self.root / "env").is_symlink())

    def test_salt_loss_and_old_format_rebuild_caches(self):
        for cache in ("receipt", "state", "knowledge"):
            with self.subTest(cache=cache):
                def refresh():
                    if cache == "receipt":
                        return self.module.index(), "meta", "stamp"
                    if cache == "state":
                        return self.module.state_refresh(self.root / "env" / "state.sqlite", False), "meta", "mask_hash"
                    return self.module.knowledge_db(), "knowledge_meta", "mask"
                db, table, key = refresh()
                original = db.execute(f"SELECT v FROM {table} WHERE k=?", (key,)).fetchone()[0]
                db.execute(f"UPDATE {table} SET v='old-unsalted-format' WHERE k=?", (key,))
                db.commit()
                db.close()
                db, _, _ = refresh()
                self.assertEqual(db.execute(f"SELECT v FROM {table} WHERE k=?", (key,)).fetchone()[0], original)
                self.assertEqual(db.execute("PRAGMA secure_delete").fetchone()[0], 1)
                db.close()
                (self.root / "env" / ".salt").unlink()
                self.fresh()
                db, _, _ = refresh()
                self.assertNotEqual(db.execute(f"SELECT v FROM {table} WHERE k=?", (key,)).fetchone()[0], original)
                db.close()

    def test_second_find_remasks_zero_of_5000_rows(self):
        self.index().close()
        self.assertEqual(counted_find(self.module), 0)

    def test_unchanged_stamp_and_mask_hash_reuse_same_file(self):
        db = self.index()
        metadata = dict(db.execute("SELECT k, v FROM meta"))
        self.assertIn("mask_hash", metadata)
        db.close()
        path = self.logs / ".index.sqlite"
        before = path.stat()
        self.index().close()
        after = path.stat()
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))

    def test_rotated_value_rebuilds_with_secure_delete_and_no_cached_value(self):
        old, rotated = "fake-previous-value-1024", "fake-rotated-value-1025"
        fixture(self.logs, count=3, signal=f"invoice {rotated}")
        entry = self.store / "ALPHA_KEY"
        entry.write_text(old + "\n")
        (self.store / ".names").write_text("ALPHA_KEY\n")
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        executable = fake_bin / "pass"
        executable.write_text("#!/bin/sh\nprintf '%s\\n' \"$FAKE_PASS_VALUE\"\n")
        executable.chmod(0o755)
        for same_files in (False, True):
            backend = "pass" if same_files else "file"
            with self.subTest(backend=backend), patch.dict(os.environ, {
                    "WOUF_SECRET_BACKEND": backend, "FAKE_PASS_VALUE": old,
                    "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"]}):
                entry.write_text(old + "\n")
                self.module = helper(LOG)
                self.assertEqual(self.module.STORED, [old])
                self.index().close()
                self.assertTrue(any(rotated.encode() in p.read_bytes() for p in self.logs.glob(".index.sqlite*")))
                state = self.module.store_state()
                if same_files:
                    # The pass backend returns a new value while every store file stays unchanged.
                    os.environ["FAKE_PASS_VALUE"] = rotated
                else:
                    entry.write_text(rotated + "\n")
                self.module = helper(LOG)
                self.assertEqual(self.module.STORED, [rotated])
                if same_files:
                    self.assertEqual(self.module.store_state(), state)
                db = self.index()
                self.assertEqual(db.execute("PRAGMA secure_delete").fetchone()[0], 1)
                self.assertIn("***", db.execute("SELECT signal FROM log").fetchone()[0])
                db.close()
                self.assertFalse(any(rotated.encode() in p.read_bytes() for p in self.logs.glob(".index.sqlite*")))

    def test_passed_value_uses_memory_and_removes_index_files(self):
        self.index().close()
        (self.logs / ".index.sqlite-journal").touch()
        self.module.PASSED = self.module.REGISTERED = ["invoice"]
        db = self.index()
        self.assertEqual(db.execute("PRAGMA database_list").fetchone()[2], "")
        self.assertEqual(db.execute("SELECT count(*) FROM log").fetchone()[0], 5000)
        self.assertFalse(list(self.logs.glob(".index.sqlite*")))

    def test_missing_mask_hash_scans_once_then_stays_warm(self):
        db = self.index()
        db.execute("DELETE FROM meta WHERE k = 'mask_hash'")
        db.commit()
        db.close()
        with patch.object(self.module, "masked_out", wraps=self.module.masked_out) as scan:
            self.index().close()
            self.assertEqual(scan.call_count, 1)
            self.index().close()
            self.assertEqual(scan.call_count, 1)

    def test_missing_hash_with_unmasked_row_rebuilds(self):
        db = self.index()
        db.execute("DELETE FROM meta WHERE k = 'mask_hash'")
        db.execute("UPDATE log SET signal = ?", ("gh" + "p_" + "Ab12" * 9,))
        db.commit()
        db.close()
        with patch.object(self.module, "build", wraps=self.module.build) as build:
            self.index().close()
            self.assertEqual(build.call_count, 1)
        self.assertFalse(any(("gh" + "p_" + "Ab12" * 9).encode() in p.read_bytes()
                             for p in self.logs.glob(".index.sqlite*")))

    def test_mask_set_order_does_not_rebuild(self):
        self.module.STORED = self.module.REGISTERED = ["fake-value-one", "fake-value-two"]
        self.index().close()
        self.module.STORED = self.module.REGISTERED = list(reversed(self.module.STORED))
        with patch.object(self.module, "build", wraps=self.module.build) as build:
            self.index().close()
            self.assertEqual(build.call_count, 0)

    def test_mask_format_change_rebuilds(self):
        self.index().close()
        with patch.object(self.module, "MASK", "[masked]"):
            with patch.object(self.module, "build", wraps=self.module.build) as build:
                self.index().close()
                self.assertEqual(build.call_count, 1)


def benchmark(path):
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        fixture(root / "logs")
        env = dict(os.environ, WOUF_LOG_DIR=str(root / "logs"), WOUF_SECRET_DIR=str(root / "store"),
                   WOUF_SECRET_BACKEND="file", WOUF_MASK="[]")
        command = [sys.executable, str(path), "find", "invoice"]
        subprocess.run(command, env=env, stdout=subprocess.DEVNULL, check=True)
        times = []
        for _ in range(5):
            start = time.perf_counter()
            subprocess.run(command, env=env, stdout=subprocess.DEVNULL, check=True)
            times.append((time.perf_counter() - start) * 1000)
        with patch.dict(os.environ, env):
            rows = counted_find(helper(path))
        print(json.dumps(dict(runs_ms=times, median_ms=statistics.median(times), remasked_rows=rows)))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--benchmark":
        benchmark(Path(sys.argv[2]) if len(sys.argv) > 2 else LOG)
    else:
        unittest.main()
