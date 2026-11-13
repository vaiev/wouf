"""Storage per task (plan T-0167, option B, board T-0148): what one run of a bot mounts, so it sees only its own
task; which login paths it copies from /bot/login into its own HOME; and keep_login, the host's copy back. In this
version a run calls only login_paths and login_link_steps: no run mounts a login storage (task_mounts and
bwrap.task_binds are not called) and none calls keep_login, so nothing is copied back. A leaf: of teamlib it imports
core and engine only; engine and run (kernel modules) reach it at call time, as run reaches bwrap. engine.py and
run.py are at their 1200-line budget, so option B grows here."""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import posixpath
import re
import stat

from teamlib import engine
from teamlib.core import die, get, member_problem, sink, task_problem, visible, words

LOGIN_STORE = "/bot/login"  # the member's login storage inside a run, shared by its tasks (plan T-0167 §3)
LOGIN_PATH = re.compile(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*")
KEEP_LOCK = ".{}.keep.lock"  # beside the login storage, never in it: a sandbox that mounts the storage cannot flock it
FOLDER = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC
NEW = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
LOCK = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC


def task_mounts(ini, member: str, task: str, login: bool) -> list[str]:
    """The -v arguments of one run with a storage per task (plan T-0167, option B): the task's own volume
    <base>-<member>--<task> at /bot, and the member's login volume <base>-<member>.login at /bot/login only when
    login is true. The member's old volume (engine.volume) is never named. A member id has no '--' and no '.',
    and a task id has no '/' and no ':', so no pair of ids makes another task's name, the login name or a host
    path; an id outside those rules is refused. Builds the arguments only: no engine call, no file."""
    problem = member_problem(member) or task_problem(task)
    if problem:
        raise ValueError(problem)
    mounts = ["-v", f"{engine.base(ini)}-{member}--{task}:/bot"]
    if login:
        mounts += ["-v", f"{engine.base(ini)}-{member}.login:/bot/login"]
    return mounts


def login_path_problem(path: str) -> str | None:
    """Why a login path could reach outside HOME or the login storage, or None. A login path is relative to HOME,
    holds no '..' and no backslash, has no empty or '.' part, and only letters, digits and . _ / -; so it needs no
    quoting in a shell and names the same place under HOME and under the login storage."""
    if path.startswith("/") or ".." in path or not LOGIN_PATH.fullmatch(path) or "." in path.split("/"):
        return (f"'{visible(path)}' is no login path: one relative to HOME, without .. or \\, in letters, digits "
                "and . _ / - only")
    return None


def login_paths(ini, runner: str) -> list[str]:
    """[runner.X] login_paths: what a run copies from /bot/login into its own HOME (only if it is there), only when the
    runner signs in with auth = login. Every path is checked first; a bad one stops the run (prepare calls this
    before the run's lock)."""
    paths = words(get(ini, f"runner.{runner}", "login_paths"))
    for path in paths:
        problem = login_path_problem(path)
        if problem:
            die(f"[runner.{runner}] login_paths: {problem}")
    return paths if engine.uses_login(ini, runner) else []


def no_link_above(path: str) -> str:
    """The test that no folder above path is a link: /bot/login itself and every parent of path in it and in
    $HOME, each `[ ! -L ... ]` from the top down, so each later one names a place inside real folders. Built from
    the path alone (a checked login path needs no quoting); a folder that does not exist yet passes."""
    parts = path.split("/")[:-1]
    above = ["/".join(parts[:end]) for end in range(1, len(parts) + 1)]
    checks = [LOGIN_STORE] + [f"{LOGIN_STORE}/{part}" for part in above] + [f'"$HOME/{part}"' for part in above]
    return " && ".join(f"[ ! -L {check} ]" for check in checks)


def unsafe_skip(path: str) -> str:
    """The head of a step: one line on stderr and nothing else when /bot/login/<path> is a link or a folder above
    it, in /bot/login or in $HOME, is one. The step goes on with `elif`."""
    login = f"{LOGIN_STORE}/{path}"
    say = f"echo 'wouf: login path {path}: %s; skipped' >&2"
    above = f"a folder above it in HOME or {LOGIN_STORE} is a link"
    return (f"if [ -L {login} ]; then {say % (login + ' is a link')}; "
            f"elif ! {{ {no_link_above(path)}; }}; then {say % above}; ")


def login_link_steps(paths: list[str]) -> tuple[list[str], list[str]]:
    """The run script's steps for the login paths: (before the CLI, after the CLI); the name and the tuple stay so
    run.py is unchanged. Before: each step first skips a path, with one line on stderr, whose /bot/login/<path> is a
    link or that has a link above it in /bot/login or in $HOME (unsafe_skip). Then, when /bot/login/<path> is a
    regular file, it is copied to $HOME/<path>~new; when it is a folder, its regular files only (no link, no special
    file, no folder in it) are copied into a new folder $HOME/<path>~new. That copy then replaces whatever HOME held
    at the path (an earlier run's copy, or a link: removed, never followed). Nothing is linked, so the CLI writes
    only its own HOME. After: nothing; no run copies a change back (keep_login is not called by a run in this
    version). A run finds /bot/login/<path> only if the bot's own storage (/bot) holds it. A path that is no login
    path is refused (ValueError)."""
    before: list[str] = []
    for path in paths:
        problem = login_path_problem(path)
        if problem:
            raise ValueError(problem)
        home, login, new = f'"$HOME/{path}"', f"{LOGIN_STORE}/{path}", f'"$HOME/{path}~new"'
        parent = posixpath.dirname(path)
        fresh = (f'mkdir -p "$HOME/{parent}" && ' if parent else "") + f"rm -rf {new}"
        files = f"""find {login}/. ! -name . -prune -type f -exec sh -c 'cp "$@" "$0"' {new} {{}} +"""
        swap = f"rm -rf {home} && mv {new} {home}"
        before.append(f"{unsafe_skip(path)}elif [ -f {login} ]; then {fresh} && cp {login} {new} && {swap}; "
                      f"elif [ -d {login} ]; then {fresh} && mkdir {new} && {files} && {swap}; fi")
    return before, []


def open_below(top: int, parts: list[str], make: bool) -> int:
    """The folder parts[0]/parts[1]/... below the open folder top, each one opened with O_DIRECTORY|O_NOFOLLOW
    relative to the one before (dir_fd), so a link at any step fails (ENOTDIR or ELOOP) and is never followed. With
    make, a missing folder is made first (mkdir 0700 with dir_fd), then opened the same way. Returns a new fd, the
    caller closes it; OSError when a step is missing (FileNotFoundError), a link or no folder."""
    fd = os.dup(top)
    try:
        for part in parts:
            if make:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, 0o700, dir_fd=fd)
            fd, above = os.open(part, FOLDER, dir_fd=fd), fd
            os.close(above)
    except BaseException:
        os.close(fd)
        raise
    return fd


def read_regular(name: str, folder: int) -> bytes | None:
    """The bytes of the regular file name in the open folder, opened with O_NOFOLLOW; None when it is not there.
    OSError when it is a link or anything but a regular file."""
    try:
        fd = os.open(name, READ, dir_fd=folder)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "not a regular file", name)
        return handle.read()


def keep_file(source: int, target: int, name: str) -> bool:
    """Write the regular file name of the open folder source into the open folder target when the stored one
    differs or is missing: <name>.new made with O_CREAT|O_EXCL|O_NOFOLLOW (an old <name>.new, a link too, removed
    first; unlink never follows), written through core.sink (registered values masked, as core.put_bytes masks
    every file the pack writes; other bytes kept exactly by surrogateescape) and fsynced, then renamed over <name>
    with src_dir_fd/dst_dir_fd. True when written; OSError when either side is a link or no regular file."""
    data = read_regular(name, source)
    if data is None or read_regular(name, target) == data:
        return False
    new = name + ".new"
    with contextlib.suppress(FileNotFoundError):
        os.unlink(new, dir_fd=target)
    with os.fdopen(os.open(new, NEW, 0o600, dir_fd=target), "w", encoding="utf-8", errors="surrogateescape",
                   newline="") as handle:
        sink(handle, data.decode("utf-8", "surrogateescape"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(new, name, src_dir_fd=target, dst_dir_fd=target)
    os.fsync(target)
    return True


def keep_folder(source: int, target: int, path: str) -> tuple[list[str], list[str]]:
    """keep_file for each entry of the open folder source, a folder login path: regular files only. A link, a
    special file or a folder in it is skipped and named (path/entry), as is a file whose stored one is no regular
    file."""
    kept: list[str] = []
    skipped: list[str] = []
    for name in sorted(os.listdir(source)):
        try:
            if not stat.S_ISREG(os.stat(name, dir_fd=source, follow_symlinks=False).st_mode):
                skipped.append(f"{path}/{name}")
            elif keep_file(source, target, name):
                kept.append(f"{path}/{name}")
        except OSError:
            skipped.append(f"{path}/{name}")
    return kept, skipped


def keep_path(home: int, login: int, path: str) -> tuple[list[str], list[str]]:
    """keep_login for one login path, HOME and the login storage open: (kept, skipped). Nothing at the path in HOME
    keeps nothing; a link at any step on either side, or neither a regular file nor a folder in HOME, skips it."""
    parents, name = path.split("/")[:-1], posixpath.basename(path)
    with contextlib.ExitStack() as fds:
        try:
            above = open_below(home, parents, make=False)
            fds.callback(os.close, above)
            mode = os.stat(name, dir_fd=above, follow_symlinks=False).st_mode
        except FileNotFoundError:
            return [], []
        except OSError:
            return [], [path]
        try:
            if stat.S_ISDIR(mode):
                source = open_below(above, [name], make=False)
                fds.callback(os.close, source)
                target = open_below(login, [*parents, name], make=True)
                fds.callback(os.close, target)
                return keep_folder(source, target, path)
            if not stat.S_ISREG(mode):
                return [], [path]
            target = open_below(login, parents, make=True)
            fds.callback(os.close, target)
            return ([path], []) if keep_file(above, target, name) else ([], [])
        except OSError:
            return [], [path]


def keep_login(home_dir: str, login_dir: str, paths: list[str]) -> tuple[list[str], list[str]]:
    """After a run, on the host: write each login path the run changed in its HOME (home_dir) into the member's
    login storage (login_dir) when the run's copy differs from the stored one or none is stored; nothing is deleted.
    A folder path keeps its regular files only. home_dir and login_dir are opened with O_DIRECTORY|O_NOFOLLOW (the
    folders above them are the host's), and every folder of a path below them the same way, relative to the one
    before (open_below); missing folders in login_dir are made with mkdir and dir_fd; files are read with
    O_NOFOLLOW and written by <name>.new and a rename (keep_file). A link at any step, on either side, is never
    followed and skips that path. An exclusive flock on .<name>.keep.lock beside login_dir (in the host folder that
    holds it, which no sandbox mounts) is held throughout, so two runs of one
    member that end at once keep one after the other. Returns (kept, skipped): the paths written (for a folder, its
    files) and the paths refused. When home_dir, login_dir or the lock cannot be opened so, every path is skipped.
    A path that is no login path is refused (ValueError) before anything is opened."""
    for path in paths:
        problem = login_path_problem(path)
        if problem:
            raise ValueError(problem)
    kept: list[str] = []
    skipped: list[str] = []
    with contextlib.ExitStack() as fds:
        try:
            home = os.open(home_dir, FOLDER)
            fds.callback(os.close, home)
            login = os.open(login_dir, FOLDER)
            fds.callback(os.close, login)
            above = os.open(os.path.dirname(os.path.abspath(login_dir)), FOLDER)
            fds.callback(os.close, above)
            lock = os.open(KEEP_LOCK.format(os.path.basename(os.path.abspath(login_dir))), LOCK, 0o600, dir_fd=above)
            fds.callback(os.close, lock)
            fcntl.flock(lock, fcntl.LOCK_EX)
        except OSError:
            return [], list(paths)
        for path in paths:
            done = keep_path(home, login, path)
            kept += done[0]
            skipped += done[1]
    return kept, skipped
