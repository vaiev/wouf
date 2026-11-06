"""Tests for .agents/bin/team: the gate, task and member ids, and the flow.

Split from test_team_cli.py, which keeps the shared fixtures (Project, the keys, the stand-in CLI).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

from test_team_cli import (  # noqa: E402
    AWS, FAKE_CLI, GITHUB_UNDERSCORE, KEY,
    LEAKED, PEM_LINES, SLACK_WORKFLOW, Project, team_module)


# A gate that leaves what one task could leave for the next: a post-checkout hook in the shared cache, a global git
# config naming it (in its HOME and in the cache), and a checkout and HOME where the next task's gate will work.
PLANT = """mkdir -p "$WOUF_CACHE/hooks"
printf '#!/bin/sh\\necho "exit 0" > gate.sh\\necho ran > "%s/hook-ran"\\n' "$WOUF_CACHE" > "$WOUF_CACHE/hooks/post-checkout"
chmod +x "$WOUF_CACHE/hooks/post-checkout"
printf '[core]\\n\\thooksPath = %s/hooks\\n' "$WOUF_CACHE" > "$WOUF_CACHE/gitconfig"
cp "$WOUF_CACHE/gitconfig" "$HOME/.gitconfig"
mkdir -p "$HOME/.config/git" && cp "$WOUF_CACHE/gitconfig" "$HOME/.config/git/config"
git config --global core.hooksPath "$WOUF_CACHE/hooks" 2>/dev/null || true
for d in ../../T-2 ../../../T-2; do mkdir -p "$d/home" && cp "$WOUF_CACHE/gitconfig" "$d/home/.gitconfig"; done
echo planted > "$WOUF_CACHE/mark"
exit 0
"""
# The next task's own test: red (7) on its own checkout, with an empty HOME (but for the empty global git config), a disposable cache not on PATH.
VICTIM = """test -z "$(ls -A "$HOME" | grep -v '^.gitconfig$')" && test ! -s "$HOME/.gitconfig" || exit 8
case ":$PATH:" in *"$WOUF_CACHE"*) exit 9 ;; esac
test ! -e "$WOUF_CACHE/mark" || exit 10
exit 7
"""


def branch(p: Project, name: str, files: dict) -> None:
    """A branch off the current commit with these files; the working tree is left as it was."""
    p.git("checkout", "-q", "-b", name)
    for path, text in files.items():
        (p.root / path).write_text(text)
        p.git("add", path)
    p.git("commit", "-q", "-m", name)
    p.git("checkout", "-q", "-")


class GateTest(unittest.TestCase):
    """The pre-gate: paths against the brief, then the team's gate command on the branch head, without keys."""

    def setUp(self) -> None:
        self.p = Project(mode="host", runner="fake")
        fake = self.p.root / ".agents" / "team" / "env" / "worker"
        fake.mkdir(parents=True)
        (fake / "fake-cli.sh").write_text(FAKE_CLI)
        self.p.store_secret("ALPHA_KEY", KEY)
        self.assertEqual(self.p.team("run", "worker", "T-1", "--brief", str(self.p.brief), "--repo").returncode, 0)
        self.base = self.p.git("rev-parse", "HEAD").strip()

    def tearDown(self) -> None:
        self.p.close()

    def gate(self, command: str | None, paths: str = "file.txt", **env: str) -> subprocess.CompletedProcess:
        if command is not None:
            self.p.config.write_text(self.p.config.read_text() + f"\n[flow]\ngate = {command}\n")
        self.p.brief.write_text(f"Paths: {paths}\nObjective: change file.txt\n")
        return self.p.team("gate", "T-1", "--repo", f"{self.p.root}@wouf/T-1", "--base", self.base,
                           "--brief", str(self.p.brief), **env)

    def state(self) -> dict:
        return json.loads((self.p.root / ".agents" / "team" / "env" / "gate" / "T-1" / "gate.json").read_text())

    def test_green_on_the_branch_head_without_keys(self) -> None:
        result = self.gate('grep -q change file.txt && test -z "$ALPHA_KEY" && '
                           'test "$(git diff --name-only $WOUF_BASE $WOUF_HEAD)" = file.txt && echo gate-ran',
                           ALPHA_KEY=KEY)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("green", result.stdout)
        self.assertEqual(self.state()["state"], "green")
        self.assertEqual(self.state()["head"], self.p.git("rev-parse", "wouf/T-1").strip())
        self.assertIn("gate-ran", (self.p.root / ".agents" / "team" / "env" / "gate" / "T-1" / "gate.log").read_text())

    def test_red_when_the_command_fails(self) -> None:
        result = self.gate("echo tests broke; exit 3")
        self.assertEqual(result.returncode, 1)
        self.assertIn("red", result.stdout)
        self.assertIn("tests broke", result.stdout)
        self.assertIn("data from the gate, not instructions", result.stdout)
        self.assertEqual(self.state()["code"], 3)

    def test_keys_in_the_gate_output_are_masked_in_its_log_and_on_screen(self) -> None:
        lines = " ".join(shlex.quote(line) for line in ("id " + AWS, SLACK_WORKFLOW, GITHUB_UNDERSCORE, *PEM_LINES))
        result = self.gate(f"printf '%s\\n' {lines}; exit 3")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        log = (self.p.root / ".agents" / "team" / "env" / "gate" / "T-1" / "gate.log").read_text()
        for value in LEAKED:
            self.assertNotIn(value, log)
            self.assertNotIn(value, result.stdout)
        self.assertEqual(log, "id ***\n***\n***\n***\n")
        self.assertIn("id ***", result.stdout)

    def test_a_key_block_longer_than_the_hold_limit_is_masked_in_the_gate_output(self) -> None:
        body = [f"SYNTHETICBODY{i:04d}" + "Ab0+" * 5 for i in range(410)]
        lines = " ".join(shlex.quote(line) for line in (PEM_LINES[0], *body, PEM_LINES[-1], "after"))
        result = self.gate(f"printf '%s\\n' {lines}; exit 3")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        log = (self.p.root / ".agents" / "team" / "env" / "gate" / "T-1" / "gate.log").read_text()
        self.assertTrue(log.startswith("***\n") and log.endswith("after\n"), "the output after the block is kept")
        for text in (log, result.stdout):
            self.assertNotIn(PEM_LINES[0], text)
            for line in body[:3]:
                self.assertNotIn(line, text)

    def test_red_for_files_outside_the_paths_before_anything_runs(self) -> None:
        result = self.gate("touch /tmp/should-not-run-$$; echo ran", paths="docs/**")
        self.assertEqual(result.returncode, 1)
        self.assertIn("outside Paths: file.txt", result.stdout)
        self.assertEqual(self.state()["outside"], ["file.txt"])
        self.assertNotIn("ran", result.stdout)

    def test_no_gate_command_checks_the_paths_only(self) -> None:
        result = self.gate(None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no gate command", result.stdout)

    def test_the_gate_discards_cache_writes_between_runs(self) -> None:
        write = ('test -d "$WOUF_CACHE" && case "$WOUF_CACHE" in "$PWD"*) exit 9 ;; esac && '
                 'echo kept > "$WOUF_CACHE/mark" && echo > in-checkout')
        first = self.gate(write)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        cache = self.p.root / ".agents" / "team" / "env" / "gate" / "T-1" / "cache"
        self.assertEqual((cache / "mark").read_text(), "kept\n")
        self.p.set(write, 'test ! -e "$WOUF_CACHE/mark" && test ! -e in-checkout')
        second = self.gate(None)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(self.state()["state"], "green")

    def test_a_container_gate_sees_the_cache_on_the_gate_storage(self) -> None:
        calls = self.p.bin / "engine-calls"
        fake = self.p.bin / "podman"
        fake.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" >> {calls}\necho END >> {calls}\nexit 0\n")
        fake.chmod(0o755)
        self.p.set("mode = host", "mode = container")
        self.p.set("engine = docker", "engine = podman")
        result = self.gate("make test")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        create = next(c for c in calls.read_text().split("END\n") if c.startswith("run\n"))
        self.assertIn("\nWOUF_CACHE=/bot/cache\n", create)
        self.assertIn("\n-v\nwouf-" + self.p.root.name + "-gate-cache:/bot/cache:O\n", create)
        self.assertEqual(create.count("\n-v\n"), 1, "only the cache lasts; HOME and the work folder do not")
        for value in ("GIT_CONFIG_GLOBAL=/bot/home/.gitconfig", "GIT_CONFIG_NOSYSTEM=1", "HOME=/bot/home"):
            self.assertIn(f"\n{value}\n", create)
        self.assertIn('mkdir "$HOME" /bot/hooks /bot/work /bot/work/T-1 && : > "$GIT_CONFIG_GLOBAL" && chmod 555 /bot/hooks && '
                      'cd /bot/work/T-1 && set -e\n', create)
        self.assertIn("git init -q --template= repo", create)
        self.assertIn("git config core.hooksPath /bot/hooks\n", create)

    def test_a_gate_command_may_set_global_git_config_and_the_next_run_starts_empty(self) -> None:
        write = ('git config --global user.name x && test "$(git config --global user.name)" = x && '
                 'test "$(git config --global --list | wc -l)" = 1')
        first = self.gate(write)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.p.set(write, 'test -z "$(git config --global --list)" && test -e "$GIT_CONFIG_GLOBAL" && '
                          'test "$GIT_CONFIG_GLOBAL" != /dev/null && test "$GIT_CONFIG_NOSYSTEM" = 1')
        second = self.gate(None)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)

    # ---- one task's gate cannot change what a later task's gate checks out or runs

    def two_tasks(self) -> None:
        branch(self.p, "attack", {"gate.sh": PLANT})
        branch(self.p, "victim", {"gate.sh": VICTIM})
        self.p.config.write_text(self.p.config.read_text() + "\n[flow]\ngate = sh gate.sh\n")
        self.p.brief.write_text("Paths: gate.sh\nObjective: a gate\n")

    def gate_of(self, task: str, ref: str, *extra: str) -> subprocess.CompletedProcess:
        return self.p.team("gate", task, "--repo", f"{self.p.root}@{ref}", "--base", self.base,
                           "--brief", str(self.p.brief), *extra)

    def gate_dir(self) -> Path:
        return self.p.root / ".agents" / "team" / "env" / "gate"

    def test_an_earlier_host_gate_cannot_change_a_later_checkout_or_test(self) -> None:
        self.two_tasks()
        first = self.gate_of("T-1", "attack")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        cache = self.gate_dir() / "T-1" / "cache"
        self.assertTrue((cache / "hooks" / "post-checkout").is_file(), "the plant ran")
        second = self.gate_of("T-2", "victim")
        self.assertEqual(second.returncode, 1, second.stdout + second.stderr)
        state = json.loads((self.gate_dir() / "T-2" / "gate.json").read_text())
        self.assertEqual((state["code"], state["head"]), (7, self.p.git("rev-parse", "victim").strip()),
                         second.stdout)
        self.assertFalse((cache / "hook-ran").exists(), "the planted hook ran in the later checkout")

    def fake_engine(self) -> Path:
        """An engine that does nothing and keeps every call, one JSON list per line; it knows no volume."""
        calls = self.p.bin / "engine-calls.jsonl"
        fake = self.p.bin / "podman"
        fake.write_text(f"#!{sys.executable}\nimport json, sys\n"
                        f"open({str(calls)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
                        "sys.exit(sys.argv[1:3] == ['volume', 'inspect'])\n")
        fake.chmod(0o755)
        self.p.set("mode = host", "mode = container")
        self.p.set("engine = docker", "engine = podman")
        return calls

    def as_a_container(self, calls: Path, task: str, run: Path) -> subprocess.CompletedProcess:
        """Run the last container the gate created as the engine would, without an engine: every -v volume is a
        lower folder, with a private copy for :O; the rest of /bot is this container's own (run)."""
        args = [json.loads(line) for line in calls.read_text().splitlines()]
        create = [a for a in args if a[0] == "run"][-1]
        self.assertEqual(create[-3:-1], ["sh", "-c"])
        env, paths, cwd = {}, {"/tmp/repo.bundle": str(self.gate_dir() / task / "repo.bundle")}, "/"
        for flag, value in zip(create, create[1:]):
            if flag == "-v":
                name, target, *options = value.split(":")
                lower = Path(self.p.tmp.name) / "volumes" / name
                lower.mkdir(parents=True, exist_ok=True)
                if "O" in options:
                    upper = run.parent / (run.name + "-overlay-" + task)
                    shutil.copytree(lower, upper, dirs_exist_ok=True)
                    paths[target] = str(upper)
                else:
                    paths[target] = str(lower)
            elif flag == "-e":
                key, _, val = value.partition("=")
                env[key] = val
            elif flag == "-w":
                cwd = value
        paths.setdefault("/bot", str(run))
        run.mkdir(parents=True, exist_ok=True)
        pattern = re.compile("|".join(re.escape(p) for p in sorted(paths, key=len, reverse=True)) + r"(?![\w.-])")

        def local(text: str) -> str:
            return pattern.sub(lambda m: paths[m.group(0)], text)

        env = {key: local(val) for key, val in env.items()}
        self.cache = Path(env["WOUF_CACHE"])
        # The engine forwards stdin to the container; keep the actual bundle pipeline in the exercised shell.
        # Use a separate source because the container writes its own BUNDLE destination.
        bundle_data = (self.gate_dir() / task / "repo.bundle").read_bytes()
        result = subprocess.run(["sh", "-c", local(create[-1])], cwd=local(cwd), capture_output=True,
                                input=bundle_data, env={"PATH": os.environ["PATH"], "LANG": "C.UTF-8", **env},
                                timeout=120)
        result.stdout, result.stderr = result.stdout.decode(), result.stderr.decode()
        return result

    def test_an_earlier_container_gate_cannot_change_a_later_checkout_or_test(self) -> None:
        # Without an engine: the exact shell the gate hands the container, run with its mounts and environment.
        self.two_tasks()
        calls = self.fake_engine()
        runs = Path(self.p.tmp.name) / "containers"
        self.assertEqual(self.gate_of("T-1", "attack").returncode, 0)
        first = self.as_a_container(calls, "T-1", runs / "1")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertTrue((self.cache / "hooks" / "post-checkout").is_file(), "the plant ran on the disposable cache")
        self.assertEqual(self.gate_of("T-2", "victim").returncode, 0)
        second = self.as_a_container(calls, "T-2", runs / "2")
        self.assertEqual(second.returncode, 7, second.stdout + second.stderr)
        self.assertFalse((self.cache / "hook-ran").exists(), "the planted hook ran in the later checkout")

    def test_the_container_checkout_ignores_a_poisoned_home(self) -> None:
        # Even if HOME and the work folder were to last (the old shared /bot), the checkout reads no inherited
        # git config and runs no hook: shown with one folder for the whole of /bot, kept between both runs.
        self.two_tasks()
        calls = self.fake_engine()
        run = Path(self.p.tmp.name) / "shared-bot"
        self.assertEqual(self.gate_of("T-1", "attack").returncode, 0)
        self.assertEqual(self.as_a_container(calls, "T-1", run).returncode, 0)
        (run / "home").mkdir(exist_ok=True)
        (run / "home" / ".gitconfig").write_text((self.cache / "gitconfig").read_text())
        (run / "home" / "left-behind").write_text("an earlier run\n")
        self.assertEqual(self.gate_of("T-2", "victim").returncode, 0)
        create = [json.loads(line) for line in calls.read_text().splitlines() if line.startswith('["run"')][-1]
        create[-1] = create[-1].replace('mkdir "$HOME" /bot/hooks /bot/work /bot/work/T-2', 'mkdir -p /bot/hooks /bot/work/T-2')
        calls.write_text(json.dumps(create) + "\n")
        second = self.as_a_container(calls, "T-2", run)
        self.assertEqual(second.returncode, 8, "a red test, and the HOME it found was not empty: "
                         + second.stdout + second.stderr)
        self.assertFalse((self.cache / "hook-ran").exists(), "the planted hook ran in the later checkout")

    def test_links_and_locked_folders_an_earlier_host_gate_left_are_not_followed(self) -> None:
        outside = Path(self.p.tmp.name) / "outside"
        outside.mkdir()
        (outside / "keep").write_text("keep\n")
        self.gate_dir().mkdir(parents=True)
        (self.gate_dir() / ".cache").symlink_to(outside, target_is_directory=True)
        (self.gate_dir() / "T-1").symlink_to(outside, target_is_directory=True)
        plant = (f'test ! -L "$WOUF_CACHE" && echo new > "$WOUF_CACHE/new" && ln -s {outside} "$WOUF_CACHE/out" && '
                 f'ln -s {outside}/written ../../gate.json && mkdir -p "$HOME/locked/in" "$WOUF_CACHE/locked/in" && '
                 f'chmod 500 "$HOME/locked" "$HOME" "$WOUF_CACHE/locked" && chmod 500 .')
        first = self.gate(plant)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ["keep"])
        for folder in (self.gate_dir() / "T-1" / "cache", self.gate_dir() / "T-1"):
            self.assertFalse(folder.is_symlink())
            self.assertTrue(folder.is_dir())
        self.assertFalse((self.gate_dir() / "T-1" / "gate.json").is_symlink())
        self.assertEqual(self.state()["state"], "green")
        self.p.set(plant, 'test -z "$(ls -A "$HOME" | grep -v "^.gitconfig$")" && test ! -s "$HOME/.gitconfig" && test ! -e "$WOUF_CACHE/out"')
        second = self.gate(None)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        cleared = self.p.team("gate", "--fresh-cache")
        self.assertEqual(cleared.returncode, 0, cleared.stdout + cleared.stderr)
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ["keep"])
        self.assertEqual((outside / "keep").read_text(), "keep\n")
        self.assertFalse((self.gate_dir() / ".cache").exists())

    def test_the_gate_state_replaces_a_link_left_in_its_folder(self) -> None:
        team = team_module()
        folder, outside = Path(self.p.tmp.name) / "stage", Path(self.p.tmp.name) / "outside-file"
        folder.mkdir()
        for name in (f".gate.json.{os.getpid()}.tmp", "gate.json"):
            (folder / name).symlink_to(outside)
        team.write_json(folder / "gate.json", {"state": "green"})
        self.assertFalse(outside.exists())
        self.assertFalse((folder / "gate.json").is_symlink())
        self.assertEqual(json.loads((folder / "gate.json").read_text()), {"state": "green"})

    def test_fresh_cache_empties_the_cache_alone_or_before_a_gate(self) -> None:
        snapshot = self.gate_dir() / ".cache"
        snapshot.mkdir(parents=True)
        (snapshot / "lower").write_text("trusted\n")
        write = 'echo kept > "$WOUF_CACHE/mark"'
        self.assertEqual(self.gate(write).returncode, 0)
        cache = self.gate_dir() / "T-1" / "cache"
        self.assertTrue((cache / "mark").is_file())
        self.p.set(write, f'test ! -e "$WOUF_CACHE/mark" && test ! -e "$WOUF_CACHE/lower" && {write}')
        again = self.gate_of("T-1", "wouf/T-1", "--fresh-cache")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertTrue((cache / "mark").is_file())
        cleared = self.p.team("gate", "--fresh-cache")
        self.assertEqual(cleared.returncode, 0, cleared.stdout + cleared.stderr)
        self.assertFalse((self.gate_dir() / ".cache").exists())
        self.assertIn("cache", cleared.stdout)

    def test_fresh_cache_removes_the_container_cache_volume(self) -> None:
        calls = self.fake_engine()
        cleared = self.p.team("gate", "--fresh-cache")
        self.assertEqual(cleared.returncode, 0, cleared.stdout + cleared.stderr)
        args = [json.loads(line) for line in calls.read_text().splitlines()]
        self.assertIn(["volume", "rm", "-f", f"wouf-{self.p.root.name}-gate-cache"], args)
        self.assertIn(["volume", "rm", "-f", f"wouf-{self.p.root.name}-gate"], args)

class TaskIdTest(unittest.TestCase):
    """Task ids name folders and branches: reserved names and case twins of an existing task are refused."""

    def setUp(self) -> None:
        self.p = Project(mode="host", runner="alpha")
        self.env = self.p.root / ".agents" / "team" / "env"

    def tearDown(self) -> None:
        self.p.close()

    def test_reserved_names_are_refused_everywhere(self) -> None:
        for task in (".", "..", ".hidden", "-x", "T-1\n", "x" * 41):
            for args in (("run", "worker", task, "--brief", str(self.p.brief), "--dry-run"),
                         ("result", "worker", task), ("gate", task, "--repo", "HEAD", "--base", "HEAD"),
                         ("flow", task, "--status")):
                with self.subTest(task=task, command=args[0]):
                    result = self.p.team(*args)
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn(f"task {task!r}", result.stderr)
        self.assertEqual(self.p.plan("worker", "_T.1", "--brief", str(self.p.brief))["mode"], "host")

    def test_ids_that_are_no_git_branch_or_a_windows_device_are_refused_before_any_state(self) -> None:
        self.p.config.write_text(self.p.config.read_text() + "\n[flow]\ngate = touch /tmp/never-$$; true\n")
        team = self.p.root / ".agents" / "team"
        for task in ("T-1.", "T..1", "T.lock", "T.LOCK", "CON", "con", "PRN", "AUX", "NUL", "COM1", "LPT1",
                     "aux.txt", "com1.r1", AWS):
            for args in (("run", "worker", task, "--brief", str(self.p.brief), "--dry-run"),
                         ("run", "worker", task, "--brief", str(self.p.brief), "--repo", "--dry-run"),
                         ("run", "worker", task, "--brief", str(self.p.brief), "--repo"),
                         ("result", "worker", task), ("gate", task, "--repo", "HEAD", "--base", "HEAD"),
                         ("flow", task, "--brief", str(self.p.brief), "--repo", str(self.p.root)),
                         ("flow", task, "--status")):
                with self.subTest(task=task, command=" ".join(args[:1] + args[3:4] + args[-1:])):
                    result = self.p.team(*args)
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    if task == AWS:
                        self.assertIn("task id looks like it contains a secret (AWS access key)", result.stderr)
                        self.assertNotIn(AWS, result.stdout + result.stderr)
                    else:
                        self.assertIn(f"task {task!r}", result.stderr)
        self.assertFalse((team / "env" / "gate").exists())
        self.assertFalse((team / "env" / "worker" / "tasks").exists())
        self.assertFalse((team / "flows").exists())
        self.assertFalse((team / "logs").exists() and any((team / "logs").glob("*.jsonl")))
        self.assertEqual(self.p.git("for-each-ref", "--format=%(refname)"), "refs/heads/" +
                         self.p.git("branch", "--show-current"))

    def test_a_trailing_dot_alias_of_an_existing_task_is_refused(self) -> None:
        # T-1. is the folder T-1 on Windows, and no git branch name anywhere.
        (self.env / "gate" / "T-1").mkdir(parents=True)
        for args in (("run", "worker", "T-1.", "--brief", str(self.p.brief), "--dry-run"), ("result", "worker", "T-1."),
                     ("gate", "T-1.", "--repo", "HEAD", "--base", "HEAD"), ("flow", "T-1.", "--status")):
            with self.subTest(command=args[0]):
                result = self.p.team(*args)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("cannot be a git branch name", result.stderr)
        self.assertEqual([p.name for p in (self.env / "gate").iterdir()], ["T-1"])

    def assert_twin_refused(self, existing: Path) -> None:
        existing.mkdir(parents=True)
        for args in (("run", "review", "t-1", "--brief", str(self.p.brief), "--dry-run"),
                     ("result", "worker", "t-1"), ("gate", "t-1", "--repo", "HEAD", "--base", "HEAD"),
                     ("flow", "t-1", "--status")):
            with self.subTest(command=args[0]):
                result = self.p.team(*args)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("task 't-1' differs only in case from the existing task 'T-1'", result.stderr)
        # An exact repeat of the existing id stays allowed.
        self.assertEqual(self.p.plan("review", "T-1", "--brief", str(self.p.brief))["mode"], "host")

    def test_a_case_twin_of_a_bot_stage_is_refused(self) -> None:
        self.assert_twin_refused(self.env / "worker" / "tasks" / "T-1")

    def test_a_case_twin_of_a_gate_folder_is_refused(self) -> None:
        self.assert_twin_refused(self.env / "gate" / "T-1")

    def test_a_case_twin_of_a_flow_folder_is_refused(self) -> None:
        self.assert_twin_refused(self.p.root / ".agents" / "team" / "flows" / "T-1")

    def test_an_exact_repeat_of_an_existing_gate_id_is_allowed(self) -> None:
        (self.env / "worker" / "tasks" / "T-1").mkdir(parents=True)
        self.assertEqual(self.p.team("gate", "T-1", "--repo", "HEAD", "--base", "HEAD").returncode, 0)

    def test_a_case_twin_recorded_only_in_a_log_is_refused_before_anything_runs(self) -> None:
        # The receipt of a run is the only trace of a task that never got a folder: log refuses its case twins,
        # so team must too, and must not create state for the refused task.
        logs = self.p.root / ".agents" / "team" / "logs"
        log_env = {**self.p.env, "WOUF_LOG_DIR": str(logs)}
        log = str(self.p.root / ".agents" / "bin" / "log")
        subprocess.run([sys.executable, log, "add", "lead", "T-1", "started", "dispatch"], check=True,
                       capture_output=True, env=log_env)
        bot_log = self.env / "worker" / "logs" / "worker.jsonl"
        bot_log.parent.mkdir(parents=True)
        bot_log.write_text(json.dumps({"t": "2026-10-19T09:00:00.000+00:00", "who": "worker", "task": "T-2",
                                       "outcome": "started", "signal": "brief"}) + "\n")
        for task in ("t-1", "t-2"):
            with self.subTest(task=task):
                result = self.p.team("gate", task, "--repo", "HEAD", "--base", "HEAD")
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(f"task {task!r} differs only in case from the existing task", result.stderr)
                self.assertFalse((self.env / "gate" / task).exists())
        self.assertFalse((self.env / "gate").exists())
        self.assertEqual(self.p.team("gate", "T-1", "--repo", "HEAD", "--base", "HEAD").returncode, 0)


SLACK_MEMBER = "xox" "b-12345-" + "abcdefghijklmn"


class MemberIdTest(unittest.TestCase):
    """Bot ids are checked like task ids: no key shape, and safe as a folder and a branch part."""

    def setUp(self) -> None:
        self.p = Project(mode="host", runner="alpha")
        self.team_dir = self.p.root / ".agents" / "team"

    def tearDown(self) -> None:
        self.p.close()

    def commands(self, member: str) -> list:
        brief = str(self.p.brief)
        return [("run", member, "T-1", "--brief", brief), ("run", member, "T-1", "--brief", brief, "--dry-run"),
                ("result", member, "T-1"), ("export", member), ("bot", "add", member), ("bot", "create", member),
                ("bot", "stop", member), ("bot", "reset", member), ("bot", "remove", member),
                ("bot", "inspect", member), ("bot", "login", member)]

    def assert_nothing_created(self) -> None:
        env = self.team_dir / "env"
        self.assertEqual(list(env.glob("*/tasks")) + list(env.glob("gate")) + list(env.glob("_archive")), [])
        self.assertFalse((self.team_dir / "flows").exists())
        self.assertFalse((self.team_dir / "logs").exists() and any((self.team_dir / "logs").glob("*.jsonl")))

    def test_a_key_shaped_member_id_with_an_active_declaration_is_refused_before_any_state(self) -> None:
        self.p.bot_file(SLACK_MEMBER).write_text(f"---\nid: {SLACK_MEMBER}\nname: X\nrole: worker\nstatus: active\n---\n")
        for args in self.commands(SLACK_MEMBER):
            with self.subTest(command=" ".join(args[:2])):
                result = self.p.team(*args)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("member id looks like it contains a secret (Slack token)", result.stderr)
                self.assertNotIn(SLACK_MEMBER, result.stdout + result.stderr)
        self.assert_nothing_created()

    def test_a_key_shaped_declaration_file_or_id_field_is_refused_without_the_value(self) -> None:
        self.p.bot_file(SLACK_MEMBER).write_text("---\nname: X\nrole: worker\nstatus: active\n---\n")
        for args in (("status",), ("doctor",), ("export",)):
            with self.subTest(command=args[0]):
                result = self.p.team(*args)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertNotIn(SLACK_MEMBER, result.stdout + result.stderr)
        self.p.bot_file(SLACK_MEMBER).unlink()
        self.p.bot_file("worker").write_text(f"---\nid: {SLACK_MEMBER}\nname: X\nrole: worker\nstatus: active\n---\n")
        for args in (("run", "worker", "T-1", "--brief", str(self.p.brief)), ("status",)):
            with self.subTest(command=args[0], field="id"):
                result = self.p.team(*args)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertNotIn(SLACK_MEMBER, result.stdout + result.stderr)
        self.assert_nothing_created()

    def test_a_flow_with_a_key_shaped_bot_in_its_class_is_refused_before_a_flow_exists(self) -> None:
        self.p.config.write_text(self.p.config.read_text() + f"\n[class.standard]\nbot = {SLACK_MEMBER}\n")
        result = self.p.team("flow", "T-1", "--brief", str(self.p.brief), "--repo", str(self.p.root))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertNotIn(SLACK_MEMBER, result.stdout + result.stderr)
        self.assert_nothing_created()

    def test_a_flow_whose_bot_declaration_states_a_key_shaped_id_is_refused_before_any_state(self) -> None:
        self.p.bot_file("worker").write_text(f"---\nid: {SLACK_MEMBER}\nname: X\nrole: worker\nstatus: active\n---\n")
        result = self.p.team("flow", "T-1", "--brief", str(self.p.brief), "--repo", str(self.p.root))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("member id looks like it contains a secret (Slack token)", result.stderr)
        self.assertNotIn(SLACK_MEMBER, result.stdout + result.stderr)
        self.assert_nothing_created()

    def test_a_key_shaped_class_of_a_brief_is_refused_without_the_value(self) -> None:
        value = "xox" "b-1234567890-" + "abcdefghijklmnop"
        self.p.brief.write_text(f"Task: T-1\nClass: {value}\nObjective: change file.txt\n")
        result = self.p.team("flow", "T-1", "--brief", str(self.p.brief), "--repo", str(self.p.root))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("Class: '***' is not one of", result.stderr)
        self.assertNotIn(value, result.stdout + result.stderr)
        self.assert_nothing_created()

    def test_ids_that_are_no_safe_folder_or_branch_part_are_refused(self) -> None:
        for member in ("aux", "con", "com1", "lpt9", "nul", "a--b", "worker-", "Worker", "w", "x" * 32, ".hidden",
                       "-x", "a.b", "../w", "worker\n", "lead"):
            for args in (("run", member, "T-1", "--brief", str(self.p.brief), "--dry-run"), ("result", member, "T-1"),
                         ("bot", "add", member), ("bot", "create", member)):
                with self.subTest(member=member, command=" ".join(args[:2])):
                    result = self.p.team(*args)
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assert_nothing_created()
        self.assertEqual(self.p.plan("worker", "T-1", "--brief", str(self.p.brief))["mode"], "host")


FLOW_WORKER = r"""#!/bin/sh
task=$(printf '%s\n' "$*" | sed -n 's/.*Worker · \([A-Za-z0-9._-]*\) ·.*/\1/p' | head -1)
name=$(printf '%s\n' "$*" | sed -n 's/.*`\(WOUF [A-Za-z]*\) · .*/\1/p' | head -1)
grep -q "Answer from the lead" in/BRIEF.md || { if grep -q ASK in/BRIEF.md; then
  echo "$name · $task · blocked"; echo "Questions: which file?"; exit 0; fi; }
ls in > out/inputs.txt
cd repo && echo "work by $name" >> file.txt && git add file.txt && git commit -q -m "work by $name" && cd ..
echo "$name · $task · ready-for-review"
echo "Questions: none"
"""

FLOW_SCOUT = r"""#!/bin/sh
task=$(printf '%s\n' "$*" | sed -n 's/.*Scout · \([A-Za-z0-9._-]*\) ·.*/\1/p' | head -1)
name=$(printf '%s\n' "$*" | sed -n 's/.*`\(WOUF [A-Za-z]*\) · .*/\1/p' | head -1)
grep -q SCOUTFAIL in/BRIEF.md && exit 1
echo "file.txt:1 the only file" > out/CONTEXT.md
echo "$name · $task · ready-for-review"
echo "Questions: none"
"""

# Writes a plan and two draft briefs (none with NOBRIEFS, no plan with NOPLAN), and tries to commit in its
# repository: the repository is read-only for a planner, so the commit is refused and nothing comes back.
FLOW_PLANNER = r"""#!/bin/sh
task=$(printf '%s\n' "$*" | sed -n 's/.*Planner · \([A-Za-z0-9._-]*\) ·.*/\1/p' | head -1)
name=$(printf '%s\n' "$*" | sed -n 's/.*`\(WOUF [A-Za-z]*\) · .*/\1/p' | head -1)
ls in > out/inputs.txt
if (cd repo && echo "plan by $name" >> file.txt && git add file.txt && git commit -q -m "plan by $name") 2>/dev/null; then echo "committed: plan by $name"; else echo "refused: plan by $name"; fi
if ! grep -q NOPLAN in/BRIEF.md; then
  echo "# Plan" > out/PLAN.md; mkdir -p out/briefs
  if ! grep -q NOBRIEFS in/BRIEF.md; then
    printf 'Task: T-41\nClass: standard\nPaths: file.txt\n' > out/briefs/T-41.md
    printf 'Task: T-42\nClass: standard\nPaths: file.txt\n' > out/briefs/T-42.md
  fi
fi
echo "$name · $task · ready-for-review"
echo "Questions: none"
"""

# Answers with the next verdict from ../../plan (one word per review: approve, changes, bug, limit, prose).
FLOW_REVIEW = r"""#!/bin/sh
task=$(printf '%s\n' "$*" | sed -n 's/.*Review · \([A-Za-z0-9._-]*\) ·.*/\1/p' | head -1)
n=$(( $(cat ../../count 2>/dev/null || echo 0) + 1 )); echo $n > ../../count
verdict=$(cut -d' ' -f$n ../../plan)
head=$(cd repo && git rev-parse HEAD)
case "$verdict" in
  approve) echo "WOUF Review · $task · APPROVE $head"; echo "Blocking: none" ;;
  changes) echo "WOUF Review · $task · CHANGES"; echo "Blocking:"; echo "- [change] say more" ;;
  mixed) echo "WOUF Review · $task · CHANGES"; echo "Blocking:"; echo "- [bug] it breaks"; echo "- [change] say more" ;;
  bug) echo "WOUF Review · $task · CHANGES"; echo "Blocking:"; echo "- [bug] it breaks" ;;
  limit) echo "You've hit your usage limit. Try again in 2 hours."; exit 1 ;;
  *) echo "Looks fine to me." ;;
esac
"""


class FlowTest(unittest.TestCase):
    """team flow end to end in host mode, with stand-in worker, bugfixer and review CLIs."""

    def setUp(self) -> None:
        self.p = Project(mode="host", runner="fake")
        env = self.p.root / ".agents" / "team" / "env"
        for member, script in (("worker", FLOW_WORKER), ("bugfixer", FLOW_WORKER), ("scout", FLOW_SCOUT),
                               ("review", FLOW_REVIEW), ("planner", FLOW_PLANNER)):
            (env / member).mkdir(parents=True)
            (env / member / "fake-cli.sh").write_text(script)
        self.p.config.write_text(self.p.config.read_text() + "\n[flow]\ngate = grep -q work file.txt\n")
        self.p.store_secret("ALPHA_KEY", KEY)
        self.p.brief.write_text("Class: standard\nPaths: file.txt\nObjective: change file.txt\n")

    def tearDown(self) -> None:
        self.p.close()

    def plan(self, *verdicts: str) -> None:
        (self.p.root / ".agents" / "team" / "env" / "review" / "plan").write_text(" ".join(verdicts) + "\n")

    def flow(self, *args: str) -> subprocess.CompletedProcess:
        return self.p.team("flow", "T-40", *args)

    def start(self) -> subprocess.CompletedProcess:
        return self.flow("--brief", str(self.p.brief), "--repo", str(self.p.root))

    def kinds(self) -> list:
        state = json.loads((self.p.root / ".agents" / "team" / "flows" / "T-40" / "state.json").read_text())
        return [step["kind"] for step in state["steps"]]

    def test_ready_after_one_approve(self) -> None:
        self.plan("approve")
        result = self.start()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        head = self.p.git("rev-parse", "wouf/T-40").strip()
        self.assertEqual(result.stdout.strip().splitlines()[-1], f"ready wouf/T-40 {head}")
        self.assertEqual(self.kinds(), ["worker", "gate", "review"])
        self.assertEqual(self.flow().returncode, 0, "a finished flow just reports again")
        status = self.flow("--status")
        self.assertIn("review", status.stdout)
        self.assertIn("approve", status.stdout)

    def test_changes_then_approve(self) -> None:
        self.plan("changes", "approve")
        result = self.start()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.kinds(), ["worker", "gate", "review", "worker", "gate", "review"])
        brief = (self.p.root / ".agents" / "team" / "flows" / "T-40" / "brief-04-worker.md").read_text()
        self.assertIn("[change] say more", brief)
        self.assertEqual(self.p.git("log", "--format=%s", "wouf/T-40").count("work by WOUF Worker"), 2)

    def test_a_bug_goes_to_the_bugfixer(self) -> None:
        self.plan("bug", "approve")
        result = self.start()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.kinds(), ["worker", "gate", "review", "bugfixer", "gate", "review"])
        self.assertIn("work by WOUF Bugfixer", self.p.git("log", "--format=%s", "wouf/T-40"))

    def worker_inputs(self) -> str:
        return (self.p.root / ".agents" / "team" / "env" / "worker" / "tasks" / "T-40" / "out" / "inputs.txt").read_text()

    def test_the_scouts_context_reaches_the_worker(self) -> None:
        self.plan("approve")
        self.p.brief.write_text("Class: standard\nScout: yes\nPaths: file.txt\nObjective: change file.txt\n")
        result = self.start()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.kinds(), ["scout", "worker", "gate", "review"])
        self.assertIn("CONTEXT.md", self.worker_inputs())

    def test_a_failed_scout_still_lets_the_worker_run(self) -> None:
        self.plan("approve")
        self.p.brief.write_text("Class: standard\nScout: yes\nPaths: file.txt\nObjective: SCOUTFAIL\n")
        result = self.start()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.kinds(), ["scout", "worker", "gate", "review"])
        self.assertNotIn("CONTEXT.md", self.worker_inputs())

    def test_mixed_findings_go_to_the_worker(self) -> None:
        self.plan("mixed", "approve")
        result = self.start()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.kinds(), ["worker", "gate", "review", "worker", "gate", "review"])

    def test_two_rounds_of_changes_block(self) -> None:
        self.plan("changes", "changes")
        result = self.start()
        self.assertEqual(result.returncode, 1)
        self.assertIn("blocked 2 review rounds without APPROVE", result.stdout)

    def test_a_ref_in_the_way_of_the_result_branch_stops_the_flow_before_it_starts(self) -> None:
        for clash in ("wouf", "wouf/T-40/x"):
            self.p.git("branch", clash)
            result = self.start()
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn(clash, result.stderr)
            self.assertIn("rename or delete", result.stderr)
            self.assertFalse((self.p.root / ".agents" / "team" / "flows" / "T-40").exists())
            self.assertFalse((self.p.root / ".agents" / "team" / "env" / "worker" / "tasks" / "T-40").exists())
            self.p.git("branch", "-D", clash)

    def test_the_lead_grants_one_more_review_round_with_an_answer(self) -> None:
        self.plan("changes", "changes", "changes", "approve")
        self.assertEqual(self.start().returncode, 1)
        self.assertEqual(self.kinds(), ["worker", "gate", "review", "worker", "gate", "review"])
        result = self.flow("--answer", "ROUNDNOTE keep the file small")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("blocked 3 review rounds without APPROVE", result.stdout)
        self.assertEqual(self.kinds(), ["worker", "gate", "review", "worker", "gate", "review", "worker", "gate",
                                        "review"])
        briefs = sorted((self.p.root / ".agents" / "team" / "flows" / "T-40").glob("brief-*-worker.md"))
        self.assertIn("ROUNDNOTE", briefs[-1].read_text())
        again = self.flow("--answer", "one more, please")
        self.assertEqual(again.returncode, 1, "a second block stays final")
        self.assertEqual(len(self.kinds()), 9)

    def test_the_granted_round_can_end_in_approve(self) -> None:
        self.plan("changes", "changes", "approve")
        self.assertEqual(self.start().returncode, 1)
        result = self.flow("--answer", "go on")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.kinds()[-3:], ["worker", "gate", "review"])

    def test_an_answer_does_not_reopen_other_blocks(self) -> None:
        self.plan("prose", "prose")
        self.assertEqual(self.start().returncode, 1)
        before = self.kinds()
        self.assertEqual(self.flow("--answer", "note").returncode, 1)
        self.assertEqual(self.kinds(), before)

    def test_a_review_without_a_verdict_is_asked_again_then_blocked(self) -> None:
        self.plan("prose", "prose")
        result = self.start()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.kinds(), ["worker", "gate", "review", "review"])

    def test_a_limit_waits_and_continues_with_retry(self) -> None:
        self.plan("limit", "approve")
        result = self.start()
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertTrue(result.stdout.strip().splitlines()[-1].startswith("limited 20"))
        self.assertEqual(self.flow().returncode, 4, "still before the reset time")
        done = self.flow("--retry")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.kinds(), ["worker", "gate", "review", "review"])

    def test_a_question_waits_for_the_answer(self) -> None:
        self.p.brief.write_text("Class: standard\nPaths: file.txt\nObjective: change file.txt ASK\n")
        self.plan("approve")
        result = self.start()
        self.assertEqual(result.returncode, 5, result.stdout + result.stderr)
        self.assertIn("question which file?", result.stdout)
        started = self.flow("--answer", "file.txt", "--detach")
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            status = self.flow("--status")
            if status.returncode != 3 and "answered" not in status.stdout.splitlines()[0]:
                break
            time.sleep(0.5)
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        self.assertEqual(self.kinds(), ["worker", "worker", "gate", "review"])

    def test_a_red_gate_goes_back_to_the_worker(self) -> None:
        self.p.config.write_text(self.p.config.read_text().replace("gate = grep -q work file.txt",
                                                                   "gate = test $(grep -c work file.txt) -ge 2"))
        self.plan("approve")
        result = self.start()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.kinds(), ["worker", "gate", "worker", "gate", "review"])

    def test_retry_runs_the_last_step_of_a_blocked_flow_again(self) -> None:
        self.p.config.write_text(self.p.config.read_text().replace("gate = grep -q work file.txt", "gate = false"))
        self.plan("approve")
        self.assertEqual(self.start().returncode, 1)
        self.assertEqual(self.kinds(), ["worker", "gate", "worker", "gate"])
        self.p.config.write_text(self.p.config.read_text().replace("gate = false", "gate = true"))
        done = self.flow("--retry")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.kinds(), ["worker", "gate", "worker", "gate", "review"])

    def test_files_outside_the_paths_never_reach_review(self) -> None:
        self.p.brief.write_text("Class: standard\nPaths: docs/**\nObjective: change file.txt\n")
        self.plan("approve")
        result = self.start()
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("review", self.kinds())

    def test_a_flow_starts_once(self) -> None:
        self.plan("approve")
        self.assertEqual(self.start().returncode, 0)
        again = self.start()
        self.assertEqual(again.returncode, 2)
        self.assertIn("exists", again.stderr)

    def test_detach_and_status(self) -> None:
        self.plan("approve")
        started = self.flow("--brief", str(self.p.brief), "--repo", str(self.p.root), "--detach")
        self.assertEqual(started.returncode, 0, started.stderr)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            status = self.flow("--status")
            if status.returncode != 3:
                break
            time.sleep(0.5)
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        self.assertTrue(status.stdout.startswith("ready wouf/T-40"))
        # The final state is written last, but the process may still be exiting: wait for it, so tearDown never races it.
        pid = json.loads((self.p.root / ".agents" / "team" / "flows" / "T-40" / "state.json").read_text())["pid"]
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            self.fail(f"the flow process {pid} is still running after its final state")

    def steps(self) -> list:
        state = json.loads((self.p.root / ".agents" / "team" / "flows" / "T-40" / "state.json").read_text())
        return [(step["kind"], step["bot"]) for step in state["steps"]]

    def test_a_plan_runs_scout_then_planner_and_ends_ready_with_the_plan(self) -> None:
        self.p.brief.write_text("Class: plan\nObjective: cut the goal into packages\n")
        result = self.start()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.steps(), [("scout", "scout"), ("worker", "planner")])
        out = ".agents/team/env/planner/tasks/T-40/out"
        last = result.stdout.strip().splitlines()[-1]
        self.assertTrue(last.startswith("ready T-40 "), last)
        for path in ("PLAN.md", "briefs/T-41.md", "briefs/T-42.md"):
            self.assertIn(f"{out}/{path}", last)
        planner = self.p.root / ".agents" / "team" / "env" / "planner" / "tasks" / "T-40"
        self.assertIn("CONTEXT.md", (planner / "out" / "inputs.txt").read_text())
        self.assertIn("refused: plan by WOUF Planner", (planner / "output.log").read_text())
        self.assertEqual(self.p.git("branch", "--list", "wouf/*"), "", "a plan flow brings back no commits")
        self.assertNotIn("plan by", self.p.git("log", "--all", "--format=%s"))
        brief = (self.p.root / ".agents" / "team" / "flows" / "T-40" / "brief-02-worker.md").read_text()
        self.assertIn("out/PLAN.md", brief)
        self.assertIn("out/briefs/", brief)
        env = self.p.root / ".agents" / "team" / "env"
        self.assertEqual(sorted(p.name for p in (env / "worker").glob("tasks/*")), [], "nothing is dispatched")
        self.assertFalse((self.p.root / ".agents" / "team" / "flows" / "T-41").exists())
        self.assertEqual(self.flow().stdout.strip().splitlines()[-1], last, "a finished plan reports again")
        self.assertTrue(self.flow("--status").stdout.startswith(last))

    def test_a_plan_without_a_plan_file_is_blocked(self) -> None:
        self.p.brief.write_text("Class: plan\nObjective: NOPLAN\n")
        result = self.start()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("PLAN.md", result.stdout.strip().splitlines()[-1])

    def test_a_plan_without_draft_briefs_is_blocked(self) -> None:
        self.p.brief.write_text("Class: plan\nObjective: NOBRIEFS cut the goal into packages\n")
        result = self.start()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("draft brief", result.stdout.strip().splitlines()[-1])
        state = json.loads((self.p.root / ".agents" / "team" / "flows" / "T-40" / "state.json").read_text())
        self.assertEqual(state["status"], "blocked")

    def test_a_plan_flow_refuses_a_bot_that_commits(self) -> None:
        self.p.config.write_text(self.p.config.read_text() + "\n[class.plan]\nbot = worker\n")
        self.p.brief.write_text("Class: plan\nObjective: cut the goal into packages\n")
        result = self.start()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("commits: no", result.stderr)
        self.assertFalse((self.p.root / ".agents" / "team" / "flows" / "T-40").exists())

    def test_a_threat_model_reaches_the_review_as_binding(self) -> None:
        self.plan("approve", "approve")
        self.p.brief.write_text("Class: security\nScout: no\nPaths: file.txt\nObjective: change file.txt\n"
                                "Threat model: THREATS a bot that reads the file; out of scope: the host\n")
        result = self.start()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        review = (self.p.root / ".agents" / "team" / "flows" / "T-40" / "brief-03-review.md").read_text()
        self.assertIn("THREATS a bot that reads the file", review)
        self.assertIn("only findings inside the threat model", review)

    def test_a_review_without_a_threat_model_gets_no_such_rule(self) -> None:
        self.plan("approve")
        self.assertEqual(self.start().returncode, 0)
        review = (self.p.root / ".agents" / "team" / "flows" / "T-40" / "brief-03-review.md").read_text()
        self.assertNotIn("threat model", review)


if __name__ == "__main__":
    unittest.main()
