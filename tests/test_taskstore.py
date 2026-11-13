"""taskstore.keep_login (plan T-0167, option B, board T-0148): after a run, trusted host code writes the sign-in files
the run changed in its HOME into the member's login storage, opening every folder on both sides without following a
link; no sandbox writes the login storage. The trees here are temporary folders; nothing runs in a sandbox."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".agents" / "bin"))
from teamlib import taskstore  # noqa: E402

AUTH = ".codex/auth.json"
KEYRINGS = ".local/share/keyrings"


def tree(base: Path) -> dict:
    """Every entry under base, a link not followed: its target, a file's bytes, or 'dir'."""
    return {str(p.relative_to(base)): ("-> " + os.readlink(p) if p.is_symlink() else p.read_bytes() if p.is_file()
                                       else "dir") for p in sorted(base.rglob("*"))}


def put(base: Path, path: str, data: bytes) -> Path:
    (base / path).parent.mkdir(parents=True, exist_ok=True)
    (base / path).write_bytes(data)
    return base / path


class KeepLoginTest(unittest.TestCase):
    """keep_login(home_dir, login_dir, paths) -> (kept, skipped)."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.home, self.login, self.outside = base / "home", base / "login", base / "outside"
        for folder in (self.home, self.login, self.outside):
            folder.mkdir()

    def keep(self, *paths: str) -> tuple[list[str], list[str]]:
        return taskstore.keep_login(str(self.home), str(self.login), list(paths))

    def stored(self) -> dict:
        return tree(self.login)

    def test_a_changed_file_is_kept_by_a_new_file_renamed_over_the_old_one(self) -> None:
        old = put(self.login, AUTH, b"v1")
        data = b"v2 \x00\xff\xfe\r\n\xc3("  # a keyring is binary: kept byte for byte
        put(self.home, AUTH, data)
        inode = old.stat().st_ino
        self.assertEqual(self.keep(AUTH), ([AUTH], []))
        self.assertEqual(self.stored(), {".codex": "dir", AUTH: data})
        self.assertNotEqual((self.login / AUTH).stat().st_ino, inode)  # replaced, not written in place
        self.assertEqual((self.login / AUTH).stat().st_mode & 0o777, 0o600)
        self.assertTrue((self.login.parent / f".{self.login.name}.keep.lock").is_file())
        self.assertNotIn(".keep.lock", " ".join(tree(self.login)))  # a sandbox mounting the storage sees no lock

    def test_a_new_file_is_kept_with_its_folders(self) -> None:
        path = ".config/agy/deep/auth.json"
        put(self.home, path, b"new")
        (self.login / ".config").mkdir()  # one folder there already, the others are made
        self.assertEqual(self.keep(path), ([path], []))
        self.assertEqual(self.stored(), {".config": "dir", ".config/agy": "dir", ".config/agy/deep": "dir",
                                         path: b"new"})
        self.assertEqual((self.login / ".config" / "agy").stat().st_mode & 0o777, 0o700)

    def test_an_unchanged_file_and_a_path_not_in_home_are_left_alone(self) -> None:
        put(self.login, AUTH, b"same")
        put(self.login, "token.json", b"stored")
        before = os.stat(self.login / AUTH)
        self.assertEqual(self.keep(AUTH, "token.json", ".gemini/oauth.json"), ([], []))
        after = os.stat(self.login / AUTH)
        self.assertEqual((after.st_ino, after.st_mtime_ns), (before.st_ino, before.st_mtime_ns))
        self.assertEqual(self.stored(), {".codex": "dir", AUTH: b"same", "token.json": b"stored"})

    def test_a_stale_new_file_is_replaced_and_never_followed(self) -> None:
        put(self.login, AUTH, b"v1")
        os.symlink(self.outside / "planted", self.login / (AUTH + ".new"))
        put(self.home, AUTH, b"v2")
        self.assertEqual(self.keep(AUTH), ([AUTH], []))
        self.assertEqual(self.stored(), {".codex": "dir", AUTH: b"v2"})
        self.assertEqual(tree(self.outside), {})

    def test_a_leaf_link_on_either_side_is_skipped(self) -> None:
        secret = put(self.outside, "auth.json", b"outside")
        for side in ("home", "login"):
            with self.subTest(side=side):
                shutil.rmtree(self.home)
                shutil.rmtree(self.login)
                self.home.mkdir()
                self.login.mkdir()
                if side == "home":  # the run's copy is a link: never read through it
                    (self.home / ".codex").mkdir()
                    (self.home / AUTH).symlink_to(secret)
                    put(self.login, AUTH, b"v1")
                else:  # the stored one is a link: never written through it, nor replaced
                    put(self.home, AUTH, b"v2")
                    (self.login / ".codex").mkdir()
                    (self.login / AUTH).symlink_to(secret)
                stored = self.stored()
                self.assertEqual(self.keep(AUTH), ([], [AUTH]))
                self.assertEqual(self.stored(), stored)
                self.assertEqual(tree(self.outside), {"auth.json": b"outside"})

    def test_a_parent_link_on_either_side_is_skipped(self) -> None:
        for side in ("home", "login"):
            for path, linked in ((AUTH, ".codex"), (".config/agy/auth.json", ".config/agy")):
                with self.subTest(side=side, path=path):
                    shutil.rmtree(self.home)
                    shutil.rmtree(self.login)
                    shutil.rmtree(self.outside)
                    self.home.mkdir()
                    self.login.mkdir()
                    self.outside.mkdir()
                    put(self.outside, "auth.json", b"outside")
                    if side == "home":  # the run's copy is reached through a link in HOME
                        (self.home / linked).parent.mkdir(parents=True, exist_ok=True)
                        (self.home / linked).symlink_to(self.outside)
                        put(self.login, path, b"v1")
                    else:  # the stored one's folder leads outside the login storage
                        put(self.home, path, b"v2")
                        (self.login / linked).parent.mkdir(parents=True, exist_ok=True)
                        (self.login / linked).symlink_to(self.outside)
                    stored = self.stored()
                    self.assertEqual(self.keep(path), ([], [path]))
                    self.assertEqual(self.stored(), stored)
                    self.assertEqual(tree(self.outside), {"auth.json": b"outside"})

    def test_a_folder_path_keeps_its_regular_files_only(self) -> None:
        put(self.home, f"{KEYRINGS}/login.keyring", b"k2")
        put(self.home, f"{KEYRINGS}/user.keystore", b"same")
        put(self.home, f"{KEYRINGS}/new.keyring", b"n1")
        (self.home / KEYRINGS / "sub").mkdir()
        (self.home / KEYRINGS / "link").symlink_to(self.outside)
        os.mkfifo(self.home / KEYRINGS / "pipe")
        put(self.login, f"{KEYRINGS}/login.keyring", b"k1")
        put(self.login, f"{KEYRINGS}/user.keystore", b"same")
        kept, skipped = self.keep(KEYRINGS)
        self.assertEqual(sorted(kept), [f"{KEYRINGS}/login.keyring", f"{KEYRINGS}/new.keyring"])
        self.assertEqual(sorted(skipped), [f"{KEYRINGS}/link", f"{KEYRINGS}/pipe", f"{KEYRINGS}/sub"])
        self.assertEqual({k: v for k, v in self.stored().items() if k.startswith(KEYRINGS + "/")},
                         {f"{KEYRINGS}/login.keyring": b"k2", f"{KEYRINGS}/new.keyring": b"n1",
                          f"{KEYRINGS}/user.keystore": b"same"})
        # A folder that is a link on either side is skipped as a whole.
        shutil.rmtree(self.login / KEYRINGS)
        (self.login / KEYRINGS).symlink_to(self.outside)
        self.assertEqual(self.keep(KEYRINGS), ([], [KEYRINGS]))
        self.assertEqual(tree(self.outside), {})

    def test_a_link_as_home_or_login_storage_or_lock_skips_every_path(self) -> None:
        put(self.home, AUTH, b"v2")
        lock = self.login.parent / f".{self.login.name}.keep.lock"
        lock.symlink_to(self.outside / "lock")
        self.assertEqual(self.keep(AUTH, "token.json"), ([], [AUTH, "token.json"]))
        lock.unlink()
        real = self.login.with_name("real-login")
        self.login.rename(real)
        self.login.symlink_to(real)
        self.assertEqual(self.keep(AUTH), ([], [AUTH]))
        self.assertEqual((tree(real), tree(self.outside)), ({}, {}))

    def test_a_path_that_could_leave_either_tree_is_refused(self) -> None:
        for path in ("../outside", "/etc/passwd", "a//b", "./x", ""):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    self.keep(AUTH, path)
        self.assertEqual(tree(self.login), {})

    def test_a_parent_swapped_for_a_link_in_a_tight_loop_never_leads_a_write_outside(self) -> None:
        """Review r3 of T-0167-2: a concurrent run swaps a checked parent in the login storage for a link to an
        outside folder while the copy-back runs. Here a thread swaps login/.codex for a link to outside and back,
        again and again, while keep_login runs 200 times, each time with a new file to keep."""
        parent, aside = self.login / ".codex", self.login / ".codex-aside"
        put(self.login, AUTH, b"v0")
        stop, swaps = threading.Event(), []

        def swap() -> None:
            while not stop.is_set():
                try:
                    parent.rename(aside)
                except OSError:
                    continue
                try:
                    parent.symlink_to(self.outside, target_is_directory=True)
                    swaps.append(1)
                    parent.unlink()
                except OSError:
                    pass
                try:
                    aside.rename(parent)
                except OSError:  # keep_login made a new folder meanwhile
                    shutil.rmtree(aside)

        old = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        self.addCleanup(sys.setswitchinterval, old)
        swapper = threading.Thread(target=swap, daemon=True)
        swapper.start()
        outcomes = []
        try:
            for n in range(200):
                put(self.home, AUTH, b"v%d" % (n + 1))
                outcomes.append(self.keep(AUTH))
                self.assertEqual(tree(self.outside), {}, f"run {n}")
        finally:
            stop.set()
            swapper.join(10)
        self.assertFalse(swapper.is_alive())
        self.assertGreater(len(swaps), 0, "the parent was never a link")
        self.assertTrue(all(outcome in (([AUTH], []), ([], [AUTH])) for outcome in outcomes), outcomes)
        self.assertEqual(tree(self.outside), {})


if __name__ == "__main__":
    unittest.main()
