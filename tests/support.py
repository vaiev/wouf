"""What the tests share: a copy of a tree they can write, and the time factor of elapsed-time bounds.

writable_copy(src, dst, **kwargs) copies a folder (shutil.copytree, with kwargs) or a file (shutil.copy) and then
gives the owner write access to every file and folder of the copy, whatever the modes of the source: a review
sandbox checks the branch out read-only, and copytree keeps those modes. Symbolic links in the copy are left alone,
so a link back into the source never makes the source writable.

TIME_FACTOR is WOUF_TEST_TIME_FACTOR, a finite number from 1 to TIME_FACTOR_CAP (default 1), read here and nowhere
else; anything else raises ValueError. A busy host (several bots on one machine) sets it, say to 3, to multiply the
elapsed-time bounds that guard a hang or a slow path with no fixed failing time. Bounds that sit just under a fixed
failing time (a fake's 30 s sleep, a 30 s grace, two 5 s pass timeouts) are not scaled: scaling them would let that
failure pass. The cap is 5, the largest factor that keeps every scaled bound under the failure it guards: the
tightest is test_demo's 10 s for two demo runs, 50 s at the cap, under the 60 s at which the test stops one run.

wouf_env(**own) is a patch.dict for a test that runs teamlib in this process: os.environ without any WOUF_* variable
of the caller, plus the variables the test sets itself. A bot sandbox sets WOUF_LOG_DIR=/bot/logs and a dev host may
set WOUF_EVENTS_DIR, WOUF_STATE_DB, WOUF_SECRET_BACKEND or WOUF_PASS_PREFIX; patching only the variables a test knows
about let the rest steer teamlib to the caller's folders. Use it as start()/addCleanup(stop) or as a context manager.
A module whose tests use teamlib, a bin script or a subprocess that inherits os.environ calls isolate_wouf_env() from
its setUpModule: no test of it then sees a WOUF_* variable of the caller (its own patch.dict still adds the ones it sets).
Without WOUF_SECRET_DIR, the first read of the secret store makes ~/.config/wouf/secrets in the caller's HOME: a
module whose subprocesses (team, project) read it calls isolate_wouf_env(secret_store=True), which points the file
backend at an empty temp folder for the module's tests.
"""

from __future__ import annotations

import math
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TIME_FACTOR_CAP = 5


def time_factor(text: str | None) -> float:
    """The factor that WOUF_TEST_TIME_FACTOR's text names: 1 when empty or unset, else a number from 1 to the cap."""
    try:
        factor = float(text or 1)
    except ValueError:
        factor = math.nan
    if not 1 <= factor <= TIME_FACTOR_CAP:  # also false for nan; inf is above the cap
        raise ValueError(f"WOUF_TEST_TIME_FACTOR must be a number from 1 to {TIME_FACTOR_CAP}, not {text!r}")
    return factor


TIME_FACTOR = time_factor(os.environ.get("WOUF_TEST_TIME_FACTOR"))


def wouf_env(**own) -> "mock._patch_dict":
    """A patch.dict of os.environ that drops every WOUF_* variable of the caller and then sets those in own."""
    kept = {k: v for k, v in os.environ.items() if not k.startswith("WOUF_")}
    return mock.patch.dict(os.environ, {**kept, **own}, clear=True)


def do_module_cleanups() -> None:
    """Run the module cleanups now. Python 3.9 and 3.10 have unittest.case.doModuleCleanups (the runner calls it) but
    not unittest.doModuleCleanups, which 3.11 added; the same function runs either way."""
    run = getattr(unittest, "doModuleCleanups", None) or unittest.case.doModuleCleanups
    run()


def isolate_wouf_env(secret_store: bool = False) -> None:
    """For setUpModule: drop the caller's WOUF_* variables until the module's tests are done. With secret_store, the
    file backend reads an empty temp folder instead of the default one under HOME."""
    own = {}
    if secret_store:
        store = tempfile.TemporaryDirectory(prefix="wouf-secrets-")
        unittest.addModuleCleanup(store.cleanup)
        own = {"WOUF_SECRET_BACKEND": "file", "WOUF_SECRET_DIR": store.name}
    env = wouf_env(**own)
    env.start()
    unittest.addModuleCleanup(env.stop)


def writable_copy(src, dst, **kwargs) -> Path:
    """The copy of src at dst, as shutil.copytree (a folder) or shutil.copy (a file) makes it, writable by its owner."""
    copied = Path(shutil.copytree(src, dst, **kwargs) if os.path.isdir(src) else shutil.copy(src, dst, **kwargs))
    for path in (copied, *copied.rglob("*")):
        mode = path.lstat().st_mode
        if not stat.S_ISLNK(mode) and not mode & stat.S_IWUSR:
            path.chmod(mode | stat.S_IWUSR)
    return copied
