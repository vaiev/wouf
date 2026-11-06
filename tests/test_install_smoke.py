"""INSTALL.md followed by scripts/install-smoke.sh: in fresh containers when Docker or Podman is reachable,
and on this computer for the checks that must fail when INSTALL.md names something the pack does not have."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "install-smoke.sh"
IMAGES = ("python:3.9-slim", "ubuntu:24.04")
# The same commands the CI job runs; the slim image has no git, the Ubuntu image no python3.
IN_CONTAINER = (
    'if ! command -v git >/dev/null || ! command -v python3 >/dev/null; then '
    'apt-get update -qq && apt-get install -y -qq --no-install-recommends git python3 ca-certificates; fi; '
    'git config --global --add safe.directory "*"; bash /src/scripts/install-smoke.sh /src'
)


def caller_env() -> dict:
    """The caller's environment without what could steer the pack's tools (WOUF_*) or the commit identity."""
    return {k: v for k, v in os.environ.items() if not k.startswith(("WOUF_", "GIT_AUTHOR_", "GIT_COMMITTER_"))}


def engine():
    for name in ("docker", "podman"):
        if shutil.which(name) and subprocess.run([name, "info"], capture_output=True).returncode == 0:
            return name
    return None


def commit_of_tree(into: Path, change=None) -> Path:
    """A bare clone of the working tree as it is now (committed in a scratch repository).
    change(work), if given, edits the copy before it is committed."""
    work = into / "tree"
    shutil.copytree(ROOT, work, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    for path in (work, *work.rglob("*")):
        if not path.is_symlink():
            path.chmod(path.stat().st_mode | 0o200)
    if change:
        change(work)
    env = dict(caller_env(), GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
    for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "tree"]):
        subprocess.run(["git", *cmd], cwd=work, env=env, check=True, capture_output=True)
    bare = into / "pack.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True, capture_output=True)
    return bare


def fenced_block(text: str, heading: str, lang: str = "bash", want: int = 1) -> str:
    """Extract the want-th fenced block of lang inside section '## <heading>' of INSTALL.md."""
    insec, inblock, seen, lines = False, False, 0, []
    for raw in text.splitlines():
        if raw.startswith("## "):
            insec = raw.startswith("## " + heading)
            inblock = False
        elif insec and raw.strip() == "```" + lang and not inblock:
            inblock = True
            seen += 1
        elif insec and raw.strip() == "```" and inblock:
            inblock = False
        elif insec and inblock and seen == want:
            lines.append(raw[2:] if raw.startswith("  ") else raw)
    return "\n".join(lines)


class InstallSmokeContainerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = engine()
        if not cls.engine:
            raise unittest.SkipTest("no reachable Docker or Podman")
        if not shutil.which("git"):
            raise unittest.SkipTest("needs git")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.bare = commit_of_tree(Path(cls.tmp.name))
        os.chmod(cls.tmp.name, 0o755)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_install_in_fresh_containers(self):
        for image in IMAGES:
            with self.subTest(image=image):
                done = subprocess.run(
                    [self.engine, "run", "--rm", "-v", f"{ROOT}:/src:ro", "-v", f"{self.bare}:/pack.git:ro",
                     "-e", "WOUF_INSTALL_SRC=file:///pack.git", image, "sh", "-c", IN_CONTAINER],
                    capture_output=True, text=True, timeout=900)
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertIn("all install steps ok", done.stdout)


class InstallSmokeHostTest(unittest.TestCase):
    """The script itself, on this computer, against a copy of INSTALL.md that is wrong."""

    @classmethod
    def setUpClass(cls):
        if not (shutil.which("git") and shutil.which("bash")):
            raise unittest.SkipTest("needs git and bash")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.bare = commit_of_tree(Path(cls.tmp.name))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_with(self, old: str, new: str):
        src = Path(self.tmp.name) / "src"
        shutil.rmtree(src, ignore_errors=True)
        src.mkdir()
        text = (ROOT / "INSTALL.md").read_text(encoding="utf-8")
        self.assertIn(old, text)
        (src / "INSTALL.md").write_text(text.replace(old, new), encoding="utf-8")
        env = dict(caller_env(), WOUF_INSTALL_SRC=f"file://{self.bare}")
        return subprocess.run(["bash", str(SCRIPT), str(src)], capture_output=True, text=True, env=env, timeout=600)

    def test_wrong_session_hook_fails_the_script(self):
        done = self.run_with('"matcher": "startup|resume|compact"', '"matcher": "startup"')
        self.assert_fails_at(done, "2 adapters (existing files): session hook merge and guard preservation")

    def test_renamed_skill_breaks_the_script(self):
        done = self.run_with(".agents/skills/setup.md", ".agents/skills/onboarding.md")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("names in INSTALL.md exist in the pack", done.stderr)
        self.assertIn("onboarding.md", done.stderr)

    def test_unknown_command_breaks_the_script(self):
        done = self.run_with("`.agents/bin/team doctor`", "`.agents/bin/team diagnose`")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("team diagnose", done.stderr)

    def test_allow_list_entry_that_is_not_a_command_fails_the_script(self):
        done = self.run_with("Bash(.agents/bin/secret list)", "Bash(.agents/bin/secret nonexistent)")
        self.assertNotEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("FAIL  names in INSTALL.md exist in the pack: allow list: 'secret nonexistent'", done.stderr)
        self.assertNotIn("all install steps ok", done.stdout)

    def run_with_stub(self, name: str, body: str):
        """The script against a pack whose .agents/bin/NAME is a test double: BODY, with the real helper kept as
        NAME.real (run it with `exec python3 "$(dirname "$0")/NAME.real" "$@"`)."""
        def stub(work):
            helper = work / ".agents" / "bin" / name
            helper.rename(work / ".agents" / "bin" / (name + ".real"))
            helper.write_text("#!/bin/sh\n" + body, encoding="utf-8")
            helper.chmod(0o755)

        into = Path(self.tmp.name) / ("broken-" + name)
        shutil.rmtree(into, ignore_errors=True)
        into.mkdir()
        bare = commit_of_tree(into, change=stub)
        env = dict(caller_env(), WOUF_INSTALL_SRC=f"file://{bare}")
        return subprocess.run(["bash", str(SCRIPT), str(ROOT)], capture_output=True, text=True, env=env, timeout=600)

    def assert_fails_at(self, done, step: str):
        self.assertNotEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("FAIL  " + step, done.stderr)
        self.assertNotIn("all install steps ok", done.stdout)

    def test_secret_list_failure_with_no_output_fails_the_script(self):
        done = self.run_with_stub("secret", 'if [ "$1" = list ]; then exit 1; fi\n'
                                  'exec python3 "$(dirname "$0")/secret.real" "$@"\n')
        self.assert_fails_at(done, "4 secret list (file backend)")
        self.assertIn("exit status: 1", done.stderr)
        self.assertIn("secret list", done.stderr)

    def test_log_status_failure_after_its_receipt_fails_the_script(self):
        done = self.run_with_stub("log", 'python3 "$(dirname "$0")/log.real" "$@"; rc=$?\n'
                                  '[ "$1" = status ] && exit 1\nexit $rc\n')
        self.assert_fails_at(done, "4 log add, log status")
        self.assertIn("exit status: 1", done.stderr)
        self.assertIn("lead T-1 started | smoke", done.stderr)  # the receipt is in the report, not a pass

    def test_allow_list_validation_failure_with_usage_fails_the_script(self):
        # A wrapper that prints the real usage, then FAIL, and exits 1: never a pass.
        done = self.run_with_stub("secret", 'python3 "$(dirname "$0")/secret.real" "$@"; rc=$?\n'
                                  '[ $# -eq 0 ] && { echo "FAIL usage"; exit 1; }\nexit $rc\n')
        self.assert_fails_at(done, "names in INSTALL.md exist in the pack: usage of")
        self.assertIn("exit status: 1", done.stderr)
        self.assertIn("FAIL usage", done.stderr)

    def test_team_doctor_that_succeeds_without_an_engine_fails_the_script(self):
        done = self.run_with_stub("team", 'if [ "$1" = doctor ]; then echo "container engine: wouf-smoke-no-such-engine (not installed)"; '
                                  'echo "→ start Docker or Podman"; exit 0; fi\n'
                                  'exec python3 "$(dirname "$0")/team.real" "$@"\n')
        self.assert_fails_at(done, "4 team doctor (no container engine)")
        self.assertIn("expected 1", done.stderr)

    def test_team_doctor_with_the_wrong_failure_status_fails_the_script(self):
        done = self.run_with_stub("team", 'if [ "$1" = doctor ]; then echo "container engine: wouf-smoke-no-such-engine (not installed)"; '
                                  'echo "→ start Docker or Podman"; exit 2; fi\n'
                                  'exec python3 "$(dirname "$0")/team.real" "$@"\n')
        self.assert_fails_at(done, "4 team doctor (no container engine)")
        self.assertIn("exit status: 2", done.stderr)
        self.assertIn("expected 1", done.stderr)

    def test_unchanged_guide_passes(self):
        env = dict(caller_env(), WOUF_INSTALL_SRC=f"file://{self.bare}", WOUF_LOG_DIR="/nonexistent/ignored")
        done = subprocess.run(["bash", str(SCRIPT), str(ROOT)], capture_output=True, text=True, env=env, timeout=600)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("existing: 4 setup, git init, bot create, doctor all good and first signed stand-in run", done.stdout)
        self.assertIn("existing: Remove (existing setup)", done.stdout)
        self.assertIn("session hooks merged once; existing hooks and guard preserved", done.stdout)
        self.assertIn("already existed (tell the human): .agents/rules.md", done.stdout)

    def test_d03_setup_configures_or_reports_flow_gate(self):
        setup = (ROOT / ".agents" / "skills" / "setup.md").read_text(encoding="utf-8")
        self.assertIn("[flow] gate", setup)
        self.assertGreaterEqual(setup.count("Paths only; no tests configured"), 2)

    def test_d06_secret_store_and_approved_install_exceptions_disclosed(self):
        install = (ROOT / "INSTALL.md").read_text(encoding="utf-8")
        setup = (ROOT / ".agents" / "skills" / "setup.md").read_text(encoding="utf-8")
        self.assertIn("~/.config/wouf/secrets", install)
        self.assertIn("software the human asks you to install", install)
        self.assertIn("~/.config/wouf/secrets", setup)

    def test_d07_windows_wsl2_prerequisite_stated_first(self):
        install = (ROOT / "INSTALL.md").read_text(encoding="utf-8")
        setup = (ROOT / ".agents" / "skills" / "setup.md").read_text(encoding="utf-8")
        step1_install = install.split("## 1. Get the pack", 1)[1].split("## 2.", 1)[0]
        step1_setup = setup.split("## Sequence of work", 1)[1].split("2. Notice", 1)[0]
        self.assertIn("WSL 2", step1_install)
        self.assertIn("WSL 2", step1_setup)

    def test_d08_step1_clone_uses_mktemp_trap_and_chains_commands(self):
        install = (ROOT / "INSTALL.md").read_text(encoding="utf-8")
        step1 = fenced_block(install, "1.", "bash")
        self.assertIn("mktemp -d", step1)
        self.assertIn("trap 'rm -rf \"$pack\"' EXIT", step1)
        self.assertNotIn("/tmp/wouf-pack", install)
        self.assertIn(".agents/team/.pack-files", step1)
        work = Path(self.tmp.name) / "d08"
        tmp_root = Path(self.tmp.name) / "d08-tmp"
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(tmp_root, ignore_errors=True)
        (work / ".agents" / "skills").mkdir(parents=True)
        (work / ".agents" / "rules.md").write_text("custom rules\n", encoding="utf-8")
        (work / ".agents" / "skills" / "custom.md").write_text("custom skill\n", encoding="utf-8")
        (tmp_root / "wouf-pack" / ".agents").mkdir(parents=True)
        (tmp_root / "wouf-pack" / ".agents" / "START.md").write_text("stale\n", encoding="utf-8")
        env = dict(caller_env(), TMPDIR=str(tmp_root))
        bad = step1.replace("https://github.com/vaiev/wouf.git", f"file://{self.tmp.name}/no-such.git")
        failed = subprocess.run(["bash", "-eu", "-c", bad], cwd=work, env=env, capture_output=True, text=True)
        self.assertNotEqual(failed.returncode, 0)
        self.assertFalse((work / ".agents" / "START.md").exists())
        good = step1.replace("https://github.com/vaiev/wouf.git", f"file://{self.bare}")
        ok = subprocess.run(["bash", "-eu", "-c", good], cwd=work, env=env, capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self.assertTrue((work / ".agents" / "START.md").is_file())
        self.assertEqual((work / ".agents" / "rules.md").read_text(encoding="utf-8"), "custom rules\n")
        self.assertEqual((work / ".agents" / "skills" / "custom.md").read_text(encoding="utf-8"), "custom skill\n")
        managed = (work / ".agents" / "team" / ".pack-files").read_text(encoding="utf-8").splitlines()
        self.assertIn(".agents/START.md", managed)
        self.assertNotIn(".agents/rules.md", managed)
        self.assertNotIn(".agents/skills/custom.md", managed)
        self.assertEqual([p.name for p in tmp_root.iterdir()], ["wouf-pack"])

    def test_d09_remove_preserves_preexisting_nested_and_top_level_files(self):
        install = (ROOT / "INSTALL.md").read_text(encoding="utf-8")
        self.assertIn("_archive/", install)
        self.assertIn("<engine> rmi", install)
        self.assertIn("_prefix", install)
        step1 = fenced_block(install, "1.", "bash").replace("https://github.com/vaiev/wouf.git", f"file://{self.bare}")
        remove = fenced_block(install, "Remove", "bash")
        self.assertTrue(remove, "Remove section in INSTALL.md must have a bash block")
        work = Path(self.tmp.name) / "d09"
        shutil.rmtree(work, ignore_errors=True)
        (work / ".agents" / "skills").mkdir(parents=True)
        (work / ".agents" / "mytool.sh").write_text("my tool\n", encoding="utf-8")
        (work / ".agents" / "skills" / "custom.md").write_text("nested skill\n", encoding="utf-8")
        (work / ".agents" / "rules.md").write_text("our rules\n", encoding="utf-8")
        subprocess.run(["bash", "-eu", "-c", step1], cwd=work, env=caller_env(), check=True, capture_output=True)
        done = subprocess.run(["bash", "-eu", "-c", remove], cwd=work, env=caller_env(), capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual((work / ".agents" / "mytool.sh").read_text(encoding="utf-8"), "my tool\n")
        self.assertEqual((work / ".agents" / "skills" / "custom.md").read_text(encoding="utf-8"), "nested skill\n")
        self.assertEqual((work / ".agents" / "rules.md").read_text(encoding="utf-8"), "our rules\n")
        self.assertFalse((work / ".agents" / "START.md").exists())
        self.assertFalse((work / ".agents" / "skills" / "setup.md").exists())
        self.assertFalse((work / ".agents" / "team").exists())
        self.assertFalse((work / ".agents" / "bin").exists())
        broken = self.run_with(
            "rm -rf .agents/team &&",
            "rm -rf .agents/team .agents/skills &&",
        )
        self.assert_fails_at(broken, "Remove (existing setup): pre-existing .agents/skills/custom.md was removed")

    def test_d10_update_removes_retired_files_and_preserves_nested_content(self):
        install = (ROOT / "INSTALL.md").read_text(encoding="utf-8")
        self.assertIn("so retired pack files do not remain", install)
        step1 = fenced_block(install, "1.", "bash").replace("https://github.com/vaiev/wouf.git", f"file://{self.bare}")
        update = fenced_block(install, "Update", "bash")
        self.assertTrue(update, "Update section in INSTALL.md must have a bash block")
        work = Path(self.tmp.name) / "d10"
        newer = Path(self.tmp.name) / "d10-newer"
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(newer, ignore_errors=True)
        (work / ".agents" / "skills").mkdir(parents=True)
        (work / ".agents" / "mytool.sh").write_text("my tool\n", encoding="utf-8")
        (work / ".agents" / "skills" / "custom.md").write_text("nested skill\n", encoding="utf-8")
        (work / ".agents" / "rules.md").write_text("our rules\n", encoding="utf-8")
        subprocess.run(["bash", "-eu", "-c", step1], cwd=work, env=caller_env(), check=True, capture_output=True)
        (work / ".agents" / "skills" / "retired-smoke.md").write_text("retired\n", encoding="utf-8")
        (work / ".agents" / "bin" / "teamlib" / "retired_smoke.py").write_text("X = 1\n", encoding="utf-8")
        with (work / ".agents" / "team" / ".pack-files").open("a", encoding="utf-8") as fh:
            fh.write(".agents/skills/retired-smoke.md\n.agents/bin/teamlib/retired_smoke.py\n")
        (work / ".agents" / "team" / "config.ini").write_text("custom-config = 1\n", encoding="utf-8")
        subprocess.run(["git", "clone", "-q", str(self.bare), str(newer)], check=True, capture_output=True)
        with (newer / ".agents" / "START.md").open("a", encoding="utf-8") as fh:
            fh.write("# newer start\n")
        (newer / ".agents" / "skills" / "smoke-new.md").write_text("new skill\n", encoding="utf-8")
        git_env = dict(caller_env(), GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
                       GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
        subprocess.run(["git", "-C", str(newer), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(newer), "commit", "-q", "-m", "newer"], env=git_env, check=True, capture_output=True)
        cmd = update.replace("https://github.com/vaiev/wouf.git", f"file://{newer}")
        done = subprocess.run(["bash", "-eu", "-c", cmd], cwd=work, env=caller_env(), capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertFalse((work / ".agents" / "skills" / "retired-smoke.md").exists())
        self.assertFalse((work / ".agents" / "bin" / "teamlib" / "retired_smoke.py").exists())
        self.assertTrue((work / ".agents" / "skills" / "smoke-new.md").is_file())
        self.assertTrue((work / ".agents" / "START.md").read_text(encoding="utf-8").endswith("# newer start\n"))
        self.assertEqual((work / ".agents" / "team" / "config.ini").read_text(encoding="utf-8"), "custom-config = 1\n")
        self.assertEqual((work / ".agents" / "mytool.sh").read_text(encoding="utf-8"), "my tool\n")
        self.assertEqual((work / ".agents" / "skills" / "custom.md").read_text(encoding="utf-8"), "nested skill\n")
        self.assertEqual((work / ".agents" / "rules.md").read_text(encoding="utf-8"), "our rules\n")
        broken = self.run_with(
            'rm -rf "$pack/repo/.agents/team" &&',
            'rm -rf "$pack/repo/.agents/team" .agents/skills &&',
        )
        self.assert_fails_at(broken, "Update (existing setup): the update removed or changed pre-existing .agents/skills/custom.md")

    def test_legacy_update_and_remove_stop_before_changing_files(self):
        install = (ROOT / "INSTALL.md").read_text(encoding="utf-8")
        for heading in ("Update", "Remove"):
            with self.subTest(operation=heading), tempfile.TemporaryDirectory(dir=self.tmp.name) as tmp:
                work = Path(tmp)
                files = {".agents/START.md": "old start", ".agents/skills/retired.md": "retired",
                         ".agents/team/config.ini": "custom config"}
                for name, value in files.items():
                    path = work / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(value, encoding="utf-8")
                command = fenced_block(install, heading).replace(
                    "https://github.com/vaiev/wouf.git", f"file://{self.bare}")
                done = subprocess.run(["bash", "-c", command], cwd=work, env=caller_env(),
                                      capture_output=True, text=True)
                self.assertNotEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertIn("ownership manifest missing", done.stderr)
                self.assertFalse((work / ".agents/team/.pack-files").exists())
                self.assertEqual({name: (work / name).read_text(encoding="utf-8")
                                  for name in files}, files)

    def test_update_and_remove_preserve_unmanaged_caches(self):
        install = (ROOT / "INSTALL.md").read_text(encoding="utf-8")
        for heading in ("Update", "Remove"):
            with self.subTest(operation=heading), tempfile.TemporaryDirectory(dir=self.tmp.name) as tmp:
                work = Path(tmp)
                (work / ".agents/team").mkdir(parents=True)
                (work / ".agents/team/.pack-files").write_text(
                    ".agents/bin/retired.py\n", encoding="utf-8")
                files = {".agents/custom/__pycache__/unrelated": "custom cache",
                         ".agents/bin/__pycache__/custom.cpython-39.pyc": "nested cache"}
                for name, value in dict(files, **{
                        ".agents/bin/retired.py": "pass",
                        ".agents/bin/__pycache__/retired.cpython-39.pyc": "managed cache"}).items():
                    path = work / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(value, encoding="utf-8")
                command = fenced_block(install, heading).replace(
                    "https://github.com/vaiev/wouf.git", f"file://{self.bare}")
                done = subprocess.run(["bash", "-c", command], cwd=work, env=caller_env(),
                                      capture_output=True, text=True)
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                for name, value in files.items():
                    self.assertTrue((work / name).is_file(), name)
                    self.assertEqual((work / name).read_text(encoding="utf-8"), value)
                self.assertFalse((work / ".agents/bin/retired.py").exists())
                self.assertFalse((work / ".agents/bin/__pycache__/retired.cpython-39.pyc").exists())

    def test_first_user_setup_and_standin_signed_run(self):
        install = (ROOT / "INSTALL.md").read_text(encoding="utf-8")
        setup = (ROOT / ".agents" / "skills" / "setup.md").read_text(encoding="utf-8")
        smoke = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("not raw event logs, active flows, goals or practice files", install)
        self.assertIn("mcp_arg", setup)
        self.assertIn("codex login --device-auth", setup)
        self.assertIn("T-smoke", setup)
        self.assertIn("exit `3`", setup)
        self.assertIn("exit `4`", setup)
        self.assertIn("WOUF Worker · T-smoke · ready-for-review", smoke)

    def test_usage_with_a_failing_status_fails_the_script(self):
        done = self.run_with_stub("log", 'python3 "$(dirname "$0")/log.real" "$@"; rc=$?\n'
                                  '[ $# -eq 0 ] && exit 2\nexit $rc\n')
        self.assert_fails_at(done, "names in INSTALL.md exist in the pack: usage of")
        self.assertIn("exit status: 2", done.stderr)

    def test_grep_error_fails_the_script(self):
        # grep exits 2 (not 1) whenever check looks for a pattern in output that is not empty.
        shims = Path(self.tmp.name) / "shims"
        shims.mkdir(exist_ok=True)
        real = shutil.which("grep")
        shim = shims / "grep"
        shim.write_text(f'#!/bin/sh\ncase "$1" in -Eq) echo "grep: simulated error" >&2; exit 2;; esac\nexec {real} "$@"\n',
                        encoding="utf-8")
        shim.chmod(0o755)
        env = dict(caller_env(), WOUF_INSTALL_SRC=f"file://{self.bare}", PATH=f"{shims}:{os.environ['PATH']}")
        done = subprocess.run(["bash", str(SCRIPT), str(ROOT)], capture_output=True, text=True, env=env, timeout=600)
        self.assert_fails_at(done, "")
        self.assertIn("exit status: 2 (grep failed", done.stderr)
        self.assertIn("simulated error", done.stderr)


if __name__ == "__main__":
    unittest.main()
