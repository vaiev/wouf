"""The usage contract (P01): out/usage.json (schema wouf.usage.v1), `[runner.X] usage = builtin:claude | <command>`,
and the precedence in collect(): a valid file, else the adapter line, else unknown."""

from __future__ import annotations

import configparser
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import isolate_wouf_env, wouf_env  # noqa: E402
from test_engine_calls import FAKE_ENGINE, MARKER  # noqa: E402
from test_team_cli import Project  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / ".agents" / "bin"
sys.path.insert(0, str(BIN))
from teamlib import engine  # noqa: E402
from teamlib import run as runlib  # noqa: E402
from teamlib import stats  # noqa: E402
from teamlib import usage  # noqa: E402

TOKENS = {"input": 1, "cache_read": 20, "cache_write": 300, "output": 4000}
SIGN = "WOUF Worker · T-7 · ready-for-review"


def write(folder: Path, data, name: str = "usage.json") -> Path:
    path = folder / name
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return path


def setUpModule() -> None:
    # No test of this module sees a WOUF_* of the caller; a test's own wouf_env(...) adds what it sets.
    isolate_wouf_env()


class ReadUsage(unittest.TestCase):
    """read_usage accepts exactly the schema of SPEC section 2 and nothing else."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)

    def read(self, data):
        return usage.read_usage(write(self.folder, data))

    def test_four_numbers_are_accepted(self):
        self.assertEqual(self.read({"tokens": TOKENS}), {"tokens": TOKENS})
        full = {"schema": "wouf.usage.v1", "tokens": TOKENS, "seconds": 12.5, "unknown_runs": 1,
                "runs": [{"role": "part:p2", "runner": "claude", "tier": "light", "model": "m",
                          "tokens": {**TOKENS, "output": 0}, "seconds": 3}]}
        self.assertEqual(self.read(full), full)
        self.assertEqual(self.read({"tokens": dict.fromkeys(TOKENS, 10 ** 12 - 1)})["tokens"]["input"], 10 ** 12 - 1)
        path = str(write(self.folder, {"tokens": TOKENS}))
        self.assertEqual(usage.read_usage(path), {"tokens": TOKENS}, "a str path")

    def test_a_missing_key_is_refused(self):
        self.assertIsNone(self.read({"schema": "wouf.usage.v1"}))
        for key in TOKENS:
            with self.subTest(key=key):
                self.assertIsNone(self.read({"tokens": {k: v for k, v in TOKENS.items() if k != key}}))

    def test_a_float_a_negative_number_or_10_to_the_12_is_refused(self):
        for value in (1.0, 0.5, -1, 10 ** 12, True, "12", None, [1]):
            with self.subTest(value=value):
                self.assertIsNone(self.read({"tokens": {**TOKENS, "output": value}}))

    def test_a_non_object_or_invalid_json_is_refused(self):
        for text in ("[1, 2, 3, 4]", '"tokens"', "12", "null", "", "{", '{"tokens": {"input": 1,}}', "not json",
                     '{"tokens": {"input": NaN, "cache_read": 0, "cache_write": 0, "output": 0}}'):
            with self.subTest(text=text):
                self.assertIsNone(self.read(text))
        self.assertIsNone(usage.read_usage(self.folder / "missing.json"))
        self.assertIsNone(self.read(b"\xff\xfe".decode("latin-1")))

    def test_anything_else_is_refused(self):
        for data in ({"tokens": TOKENS, "extra": 1}, {"tokens": {**TOKENS, "reasoning": 1}},
                     {"schema": "wouf.usage.v2", "tokens": TOKENS}, {"tokens": TOKENS, "seconds": -1},
                     {"tokens": TOKENS, "seconds": float("inf")}, {"tokens": TOKENS, "unknown_runs": 1.5},
                     {"tokens": TOKENS, "runs": {}}, {"tokens": TOKENS, "runs": [{"role": "x"}]},
                     {"tokens": TOKENS, "runs": [{"tokens": TOKENS, "role": 3}]},
                     {"tokens": TOKENS, "runs": [{"tokens": TOKENS, "secret": "x"}]}):
            with self.subTest(data=data):
                self.assertIsNone(self.read(data))

    def test_the_schema_has_no_size_count_or_text_limit(self):
        # SPEC section 2 names no limit on a file's size, on the number of runs or on the length of their text.
        runs = [{"role": f"part:p{n}", "runner": "claude", "tier": "light", "model": "m", "tokens": TOKENS}
                for n in range(1001)]
        many = {"tokens": TOKENS, "runs": runs}
        self.assertEqual(self.read(many), many)
        long_text = {"tokens": TOKENS, "runs": [{"role": "r" * 201, "runner": "x\ny", "tier": "t", "model": "m",
                                                  "tokens": TOKENS}]}
        self.assertEqual(self.read(long_text), long_text)
        big = {"tokens": TOKENS, "runs": [{"role": "p", "model": "m" * (2 << 20), "tokens": TOKENS}]}
        self.assertEqual(self.read(big), big, "a file over 1 MiB")

    def test_only_a_regular_file_is_read(self):
        target = write(self.folder, {"tokens": TOKENS}, "real.json")
        (self.folder / "usage.json").symlink_to(target)
        self.assertIsNone(usage.read_usage(self.folder / "usage.json"))
        (self.folder / "dir.json").mkdir()
        self.assertIsNone(usage.read_usage(self.folder / "dir.json"))


class Collect(unittest.TestCase):
    """collect() writes the numbers into result.json and the receipt: file > adapter line > unknown."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.team, self.stage = root / "team", root / "stage"
        (self.team / "logs").mkdir(parents=True)
        (self.stage / "out").mkdir(parents=True)
        env = wouf_env(WOUF_TEAM_DIR=str(self.team))
        env.start()
        self.addCleanup(env.stop)
        # Once teamlib.state is imported in this process, run.STORE holds the real store, whose events would land in
        # the repository's own team folder: these tests write receipts into self.team, so no store is registered.
        store = patch.object(runlib, "STORE", [])
        store.start()
        self.addCleanup(store.stop)
        self.spec = {"mode": "host", "member": "worker", "task": "T-7", "name": "WOUF Worker", "stage": str(self.stage),
                     "runner": "alpha", "tier": "strong", "model": "default", "effort": "default", "limit": 60,
                     "log_dir": str(root / "worker-logs"), "usage": "claude"}

    def collect(self, lines, spec=None):
        (self.stage / "output.log").write_text("".join(line + "\n" for line in ["work", SIGN, *lines]))
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            runlib.collect(spec or self.spec, 0, False)
        result = json.loads((self.stage / "result.json").read_text())
        cost = json.loads((self.team / "logs" / "lead.jsonl").read_text().splitlines()[-1])["cost"]
        return result, cost

    def test_a_valid_file_wins(self):
        data = {"schema": "wouf.usage.v1", "tokens": TOKENS, "seconds": 3}
        write(self.stage / "out", data)
        result, cost = self.collect(["wouf-usage tokens=5/6/7/8 skipped=2"])
        self.assertEqual((result["tokens"], result["usage"], result["usage_source"]), ("1/20/300/4000", data, "file"))
        self.assertEqual(cost, "tokens=1/20/300/4000")
        self.assertNotIn("usage_error", result)
        self.assertEqual(result["state"], "done")
        # The budget and the stats read the receipt as before (tokens_today, over_run, bot_runs unchanged).
        self.assertEqual(engine.tokens_today("worker"), 4321)
        self.assertEqual(engine.over_run({"per_run": 4000}, result["tokens"]), "4321/4000")
        self.assertEqual([row[:3] for row in stats.bot_runs(None)], [("worker", "T-7", 4321)])

    def test_no_file_and_an_adapter_line(self):
        result, cost = self.collect(["wouf-usage tokens=5/6/7/8 skipped=2"])
        self.assertEqual((result["tokens"], result["usage"], result["usage_source"], result["usage_skipped"]),
                         ("5/6/7/8", None, "adapter", 2))
        self.assertEqual(cost, "tokens=5/6/7/8")
        self.assertNotIn("usage_error", result)

    def test_neither_is_unknown(self):
        for lines in ([], ["wouf-usage tokens=unknown skipped=0"]):
            with self.subTest(lines=lines):
                result, cost = self.collect(lines)
                self.assertEqual((result["tokens"], result["usage"], result["usage_source"]), ("unknown", None, "none"))
                self.assertEqual(cost, "tokens=unknown")

    def test_an_invalid_file_and_an_adapter_line(self):
        write(self.stage / "out", {"tokens": {**TOKENS, "output": 1.5}})
        result, cost = self.collect(["wouf-usage tokens=5/6/7/8 skipped=0"])
        self.assertEqual((result["tokens"], result["usage"], result["usage_source"]), ("5/6/7/8", None, "adapter"))
        self.assertEqual(cost, "tokens=5/6/7/8")
        self.assertIsInstance(result["usage_error"], str)
        self.assertTrue(result["usage_error"])
        write(self.stage / "out", "{broken")
        result, _ = self.collect([])
        self.assertEqual((result["tokens"], result["usage_source"]), ("unknown", "none"))
        self.assertTrue(result["usage_error"])

    def test_a_runner_without_the_key_gets_the_file_its_run_leaves(self):
        # File first for every runner (SPEC section 2): an agency, or a bot, writes out/usage.json itself.
        write(self.stage / "out", {"schema": "wouf.usage.v1", "tokens": TOKENS})
        for adapter in (None, "claude"):
            with self.subTest(adapter=adapter):
                spec = {**self.spec, "usage": adapter}
                result, cost = self.collect(["wouf-usage tokens=5/6/7/8 skipped=0"], spec)
                self.assertEqual((result["tokens"], result["usage_source"], cost),
                                 ("1/20/300/4000", "file", "tokens=1/20/300/4000"))
                self.assertNotIn("usage_error", result)
        write(self.stage / "out", "{broken")
        result, cost = self.collect([], {**self.spec, "usage": None})
        self.assertEqual((result["tokens"], result["usage_source"], cost), ("unknown", "none", "tokens=unknown"))
        self.assertTrue(result["usage_error"])

class Config(unittest.TestCase):
    """[runner.X] usage, read before the lock."""

    def ini(self, text: str):
        ini = configparser.ConfigParser(interpolation=None)
        ini.read_string("[runner.alpha]\ncommand = alpha-cli -p {prompt}\n[runner.claude]\ncommand = claude -p x\n"
                        + text)
        return ini

    def test_without_the_key_the_cli_chooses_as_before(self):
        ini = self.ini("")
        self.assertEqual(usage.usage_config(ini, "runner.claude", "claude", True), ("claude", None))
        self.assertEqual(usage.usage_config(ini, "runner.alpha", "alpha-cli", True), (None, None))
        self.assertEqual(usage.usage_config(ini, "runner.claude", "claude", False), (None, None), "host: unknown")
        self.assertEqual(usage.usage_config(self.ini("usage =\n"),
                                            "runner.claude", "claude", True), ("claude", None))

    def test_builtin_and_a_command(self):
        ini = self.ini("[runner.codex]\nusage = builtin:claude\n[runner.agy]\nusage = python3 /wouf/agy.py\n")
        self.assertEqual(usage.usage_config(ini, "runner.codex", "codex", True), ("claude", None))
        self.assertEqual(usage.usage_config(ini, "runner.agy", "agy", True), (None, "python3 /wouf/agy.py"))
        self.assertEqual(usage.usage_config(ini, "runner.agy", "agy", False), (None, None))

    def test_anything_else_stops(self):
        for value in ("builtin:nope", "builtin:", "builtin:claude2", "echo a\n  echo b"):
            with self.subTest(value=value):
                ini = configparser.ConfigParser(interpolation=None)
                ini.read_dict({"runner.alpha": {"command": "alpha-cli", "usage": value}})
                err = io.StringIO()
                with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
                    usage.usage_config(ini, "runner.alpha", "alpha-cli", False)
                self.assertIn("[runner.alpha] usage", err.getvalue())

    def test_the_shipped_claude_runner_has_the_builtin(self):
        ini = configparser.ConfigParser(interpolation=None)
        ini.read(ROOT / ".agents" / "team" / "config.ini", encoding="utf-8")
        self.assertEqual(ini.get("runner.claude", "usage"), "builtin:claude")
        for runner in ("codex", "agy", "local"):
            self.assertEqual(ini.get(f"runner.{runner}", "usage", fallback=""), "", runner)


class Script(unittest.TestCase):
    """team run through the fake engine: the generated script with and without `usage =`."""

    def setUp(self):
        self.p = Project(mode="container", engine_name="fakeengine", runner="alpha")
        self.addCleanup(self.p.close)
        self.p.store_secret("ALPHA_KEY", MARKER)
        self.state = Path(self.p.tmp.name) / "engine"
        self.state.mkdir()
        fake = self.p.bin / "fakeengine"
        fake.write_text(FAKE_ENGINE.format(python=sys.executable, state=str(self.state)))
        fake.chmod(0o755)
        self.stage = self.p.root / ".agents" / "team" / "env" / "worker" / "tasks" / "T-1"

    def key(self, value: str) -> None:
        self.p.set("host_command = alpha-cli --safe -p {prompt}", f"host_command = alpha-cli --safe -p {{prompt}}\n"
                   f"usage = {value}")

    def run_worker(self):
        return self.p.team("run", "worker", "T-1", "--brief", str(self.p.brief), "--repo")

    def script(self) -> list[str]:
        return json.loads((self.stage / "run.json").read_text())["script"].splitlines()

    def started(self) -> bool:
        calls = self.state / "calls.jsonl"
        return calls.exists() and any(json.loads(c)["argv"][0] == "start" for c in calls.read_text().splitlines())

    def test_an_unknown_builtin_stops_before_the_lock(self):
        self.key("builtin:nope")
        done = self.run_worker()
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("[runner.alpha] usage = builtin:nope", done.stderr)
        self.assertFalse(self.started(), "the bot was not started")
        self.assertFalse((self.stage / "run.lock").exists(), "no stale lock")
        self.assertFalse((self.stage / "result.json").exists(), "no run was recorded")

    def test_without_the_key_the_script_has_no_usage_step(self):
        done = self.run_worker()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        script = "\n".join(self.script())
        self.assertNotIn("usage", script.lower())

    def test_the_command_runs_after_the_cli_and_before_the_copy_out(self):
        self.key('printf x > "$T/out/usage.json"; exit 9')
        done = self.run_worker()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        lines = self.script()
        cli, copy = lines.index('"$@" </dev/null &'), lines.index('cp -R "$T/out" /outbox/out')
        steps = [n for n, line in enumerate(lines) if "usage.json" in line]
        self.assertTrue(steps and all(cli < n < copy for n in steps), lines)
        command = [line for line in lines if "exit 9" in line]
        self.assertEqual(len(command), 1)
        self.assertIn("timeout -k 5 60 sh -c ", command[0])
        self.assertIn("USAGE_SINCE=/tmp/wouf-usage-since", lines[:lines.index('"$@" </dev/null &')])
        self.assertFalse(any(line.startswith("printf 'wouf-usage") for line in lines), "no adapter line")
        self.assertEqual([lines[n] for n in steps], usage.usage_command_steps('printf x > "$T/out/usage.json"; exit 9'))
        # The steps, run as the script runs them: the run's code stays whatever the command does, and the bot's own
        # report is kept unless the command succeeds (a failed command's output is not a replacement).
        original = json.dumps({"tokens": TOKENS})
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "out").mkdir()
            path = Path(folder) / "out" / "usage.json"
            for command, kept in (('printf x > "$T/out/usage.json"; exit 9', original),
                                  ("kill -TERM $$", original), ("exec 1>&-; exit 255", original),
                                  ("true", original), ('printf "$USAGE_SINCE" > "$T/out/usage.json"', "/tmp/s")):
                with self.subTest(command=command):
                    path.write_text(original)
                    body = "\n".join(usage.usage_command_steps(command))
                    done = subprocess.run(["sh", "-c", f"set +e\nT={folder}\nUSAGE_SINCE=/tmp/s\ncode=3\n{body}\n"
                                                       "exit $code"], capture_output=True, text=True, timeout=30)
                    self.assertEqual((done.returncode, done.stdout), (3, ""))
                    self.assertEqual(path.read_text(), kept)
                    self.assertFalse((Path(folder) / "usage.json.prev").exists())


if __name__ == "__main__":
    unittest.main()
