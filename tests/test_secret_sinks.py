"""Secrets at the sinks: a secret-shaped value in any config or input field reaches no output and no file.

One table. Each row puts a synthetic value into one field (it becomes a key shape in the field, or it is a
key the team passed) and the same commands run for every row. The test fails when the value appears in any
stdout, stderr or file the commands wrote, or in the name of such a file. A new field is a new row, not a new test.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_team_cli import KEY, PEM, Project  # noqa: E402
from test_team_cli_gate import FLOW_REVIEW, FLOW_WORKER  # noqa: E402

# A Slack-token shape built from parts: it fits every model, runner, connector and file name pattern, so only
# the sinks stand between it and a screen. It works nowhere.
VALUE = "xox" "b-1234567890-" + "abcdefghijklmnop"

LOG = Path(__file__).resolve().parents[1] / ".agents" / "bin" / "log"
RUN_FAKE = """#!/bin/sh
echo "seen {v}"
echo "WOUF Worker · T-1 · ready-for-review"
echo "Flags: {v}"
printf '%s\\n' '{{"t":"2026-10-19T09:00:00.000+00:00","who":"worker","task":"T-1","outcome":"done","signal":"{v}"}}' >> "$WOUF_LOG_DIR/worker.jsonl"
"""


def decl(p: Project, **fields: str) -> None:
    head = {"id": "worker", "name": "Worker", "role": "worker", "status": "active", "runner": "auto",
            "tier": "strong", "connectors": "none", **fields}
    p.bot_file("worker").write_text("---\n" + "".join(f"{k}: {v}\n" for k, v in head.items()) + "---\n")


# field -> how the value gets in. Each setup gets the project and the value.
def runner_name(p, v):
    p.set("[runner.fake]", f"[runner.{v}]\ninstall =\ncommand = fake-cli {{prompt}}\nhost_command = sh {{workdir}}/../../fake-cli.sh {{prompt}}\n"
                           "model_flag =\nstrong =\nlight =\nauth = ALPHA_KEY\nmcp_arg =\nmcp_path =\nmcp_format = claude\n\n[runner.fake]")
    decl(p, runner=v)


def auto_runner(p, v):
    # the first configured runner whose program is found: auto-selection picks the one the value names
    p.set("[runner.alpha]", f"[runner.{v}]\ninstall =\ncommand = fake-cli {{prompt}}\nhost_command = sh {{workdir}}/../../fake-cli.sh {{prompt}}\n"
                            "model_flag =\nstrong =\nlight =\nauth = ALPHA_KEY\nmcp_arg =\nmcp_path =\nmcp_format = claude\n\n[runner.alpha]")
    p.set("runner = fake", "runner = auto")
    (p.bin / "fake-cli").write_text("#!/bin/sh\nexit 0\n")
    (p.bin / "fake-cli").chmod(0o755)
    decl(p)


def model(p, v):
    p.set("strong =\nlight =\nauth = ALPHA_KEY\nmcp_arg =\nmcp_path =\nmcp_format = claude\n",
          f"strong = {v}\nlight =\nauth = ALPHA_KEY\nmcp_arg =\nmcp_path =\nmcp_format = claude\n")
    p.set("model_flag =\nstrong =", f"model_flag = --model\nstrong =")


def role(p, v):
    decl(p, role=v)


def review_runners(p, v):
    p.config.write_text(p.config.read_text() + f"\n[class.standard]\nreview_runners = {v}\n")


def connectors(p, v):
    decl(p, connectors=v)


def declaration_id(p, v):
    decl(p, id=v)


def catalog_file(p, v):
    (p.root / ".agents" / "bots" / f"{v}.md").write_text(
        f"---\nid: {v}\nname: X\nrole: specialist\nstatus: available\nrunner: auto\ntier: light\nconnectors: none\n---\n")


def catalog_id(p, v):
    (p.root / ".agents" / "bots" / "ops.md").write_text(
        f"---\nid: {v}\nname: X\nrole: specialist\nstatus: available\nrunner: auto\ntier: light\nconnectors: none\n---\n")


def worker_output(p, v):
    p.store_secret("ALPHA_KEY", KEY)
    (p.root / ".agents" / "team" / "env" / "worker" / "fake-cli.sh").write_text(RUN_FAKE.format(v=v))


def result_file(p, v):
    # a file the bot leaves in out/ with the value in its text: copied back by collect
    (p.root / ".agents" / "team" / "env" / "worker" / "fake-cli.sh").write_text(
        "#!/bin/sh\nmkdir -p out && printf '%s\\n' '" + v + "' > out/result.md\n"
        "echo \"WOUF Worker · T-1 · ready-for-review\"\n")


def result_name(p, v):
    # a file the bot leaves in out/ under a name that holds the value
    (p.root / ".agents" / "team" / "env" / "worker" / "fake-cli.sh").write_text(
        "#!/bin/sh\nmkdir -p out && echo ok > 'out/" + v + ".md'\n"
        "echo \"WOUF Worker · T-1 · ready-for-review\"\n")


def input_name(p, v):
    # an input the run is given under a name that holds the value; run_row passes it with --input
    path = p.root / f"{v}.txt"
    path.write_text("notes\n")
    return [path]


def knowledge_name(p, v):
    # a knowledge file named with the value: export --with-data writes its name into the archive
    folder = p.root / ".agents" / "team" / "knowledge"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{v}.md").write_text("notes\n")


def passed_secret(p, v):
    # not a key shape at all: only the exact value tells it; it is given as --model (see SCOPE)
    p.store_secret("ALPHA_KEY", v)
    (p.root / ".agents" / "team" / "env" / "worker" / "fake-cli.sh").write_text(
        '#!/bin/sh\necho "got $ALPHA_KEY"\necho "WOUF Worker · T-1 · ready-for-review"\necho "Flags: $ALPHA_KEY"\n')


def pem_in_output(p, v):
    (p.root / ".agents" / "team" / "env" / "worker" / "fake-cli.sh").write_text(
        "#!/bin/sh\ncat <<'EOF'\n" + PEM + "\nWOUF Worker · T-1 · ready-for-review\nEOF\n")


def plain_config_section(p, v):
    # a stored plain value that names a config section, which holds a duplicate option: the parse fails
    p.store_secret("ALPHA_KEY", v)
    p.config.write_text(p.config.read_text() + f"\n[runner.{v}]\ncommand = a\ncommand = b\n")


def plain_index(p, v):
    # a bot that writes a plain value into its receipts and searches them: its index stays in its log folder
    p.store_secret("ALPHA_KEY", v)
    (p.root / ".agents" / "team" / "env" / "worker" / "fake-cli.sh").write_text(
        f"#!/bin/sh\n{sys.executable} {LOG} add worker T-1 done \"$ALPHA_KEY\"\n"
        f"{sys.executable} {LOG} find \"$ALPHA_KEY\" >/dev/null\n"
        "echo \"WOUF Worker · T-1 · ready-for-review\"\n")


def plain_export(p, v):
    # a knowledge file whose text holds a stored plain value: the export refuses it
    p.store_secret("ALPHA_KEY", v)
    folder = p.root / ".agents" / "team" / "knowledge"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "notes.md").write_text(f"the value is {v}\n")


def plain_result(p, v):
    # a stored plain value the bot writes into a result file: masked when collected
    p.store_secret("ALPHA_KEY", v)
    (p.root / ".agents" / "team" / "env" / "worker" / "fake-cli.sh").write_text(
        "#!/bin/sh\nmkdir -p out && printf '%s\\n' '" + v + "' > out/result.md\n"
        "echo \"WOUF Worker · T-1 · ready-for-review\"\n")


def knowledge_link(p, v):
    # a knowledge link whose target is named with the value: the archive would carry the target's name
    folder = p.root / ".agents" / "team" / "knowledge"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{v}.md").write_text("notes\n")
    (folder / "link.md").symlink_to(f"{v}.md")


def receipts(p, v):
    # a receipt that never passed log add (a bot's own file, an import) is masked when it is shown
    folder = p.root / ".agents" / "team" / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "worker.jsonl").write_text(json.dumps(
        {"t": "2026-10-19T09:00:00.000+00:00", "who": "worker", "task": "T-1", "outcome": "done", "signal": f"s {v}",
         "ref": v, "cost": v}) + "\n")


def plain_receipt(p, v):
    # a stored plain value in a receipt a bot wrote: log, run by hand without WOUF_MASK, reads the store itself
    p.store_secret("ALPHA_KEY", v)
    folder = p.root / ".agents" / "team" / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "worker.jsonl").write_text(json.dumps(
        {"t": "2026-10-19T09:00:00.000+00:00", "who": "worker", "task": "T-1", "outcome": "done",
         "signal": f"found {v}", "ref": "", "cost": ""}) + "\n")


def receipt_file_name(p, v):
    # a receipt file named with the value: its path used to reach the index stamp
    folder = p.root / ".agents" / "team" / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{v}.jsonl").write_text(json.dumps(
        {"t": "2026-10-19T09:00:00.000+00:00", "who": "worker", "task": "T-1", "outcome": "done",
         "signal": "ok", "ref": "", "cost": ""}) + "\n")


def multiline_output(p, v):
    # a stored plain value over two lines, which the bot prints line by line: no single line holds the whole value
    p.store_secret("ALPHA_KEY", v)
    first, second = v.split("\n")
    (p.root / ".agents" / "team" / "env" / "worker" / "fake-cli.sh").write_text(
        f"#!/bin/sh\nprintf '%s\\n' '{first}'\nprintf '%s\\n' '{second}'\n"
        "echo \"WOUF Worker · T-1 · ready-for-review\"\n")


def binary_value(p, v):
    # a stored plain value in bytes that are not UTF-8: a result the bot leaves, and a knowledge file that export
    # would archive; both are scanned as bytes
    p.store_secret("ALPHA_KEY", v)
    (p.root / ".agents" / "team" / "env" / "worker" / "fake-cli.sh").write_text(
        f"#!/bin/sh\nmkdir -p out && printf '\\377\\376' > out/blob.bin && printf '%s' '{v}' >> out/blob.bin\n"
        "echo \"WOUF Worker · T-1 · ready-for-review\"\n")
    folder = p.root / ".agents" / "team" / "knowledge"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "blob.bin").write_bytes(b"\xff\xfe" + v.encode() + b"\xfd")


def knowledge_dir_link(p, v):
    # a knowledge link to a folder named with the value
    folder = p.root / ".agents" / "team" / "knowledge"
    (folder / v).mkdir(parents=True, exist_ok=True)
    (folder / v / "a.md").write_text("notes\n")
    (folder / "dirlink").symlink_to(v)


# Where the value may appear on command lines: "all" (anywhere), "model" (only as the value of --model: a stored
# plain secret the human passes as a model), "none" (never: the value is a text, not an argument).
SCOPE_ALL, SCOPE_MODEL, SCOPE_NONE = "all", "model", "none"
LOG_COMMANDS = (("add", "worker", "T-1", "done", "{v}"), ("add", "worker", "T-1", "done", "ok", "--ref", "{v}"),
                ("add", "worker", "T-1", "done", "ok", "--cost", "{v}"), ("add", "worker", "{v}", "done", "ok"),
                ("add", "{v}", "T-1", "done", "ok"), ("status",), ("task", "T-1"), ("task", "{v}"), ("find", "{v}"),
                ("find", "T-1"), ("find", "T-1", "--limit", "{v}"), ("find", "{v}", "--limit", "{v}"),
                ("tail",), ("tail", "-n", "{v}"), ("tail", "{v}"))
# (field, setup, value, scope, whether the value is also given to the log helper). A setup may return the
# input files to pass with --input; by default that is notes.txt, which holds the value.
TABLE = (
    ("runner name", runner_name, VALUE, SCOPE_ALL, True),
    ("auto-selected runner", auto_runner, VALUE, SCOPE_ALL, True),
    ("model", model, VALUE, SCOPE_ALL, True),
    ("role", role, VALUE, SCOPE_ALL, True),
    ("review_runners", review_runners, VALUE, SCOPE_ALL, True),
    ("connector", connectors, VALUE, SCOPE_ALL, True),
    ("bot declaration id", declaration_id, VALUE, SCOPE_ALL, True),
    ("catalog file name", catalog_file, VALUE, SCOPE_ALL, True),
    ("catalog declaration id", catalog_id, VALUE, SCOPE_ALL, True),
    ("task signal, ref, cost", receipts, VALUE, SCOPE_ALL, True),
    ("bot output and return block", worker_output, VALUE, SCOPE_ALL, True),
    ("input file content", None, VALUE, SCOPE_ALL, True),
    ("input file name", input_name, VALUE, SCOPE_ALL, True),
    ("result file content", result_file, VALUE, SCOPE_ALL, True),
    ("result file name", result_name, VALUE, SCOPE_ALL, True),
    ("knowledge file name", knowledge_name, VALUE, SCOPE_ALL, True),
    ("key block in bot output", pem_in_output, PEM, SCOPE_NONE, False),
    ("passed secret as --model", passed_secret, "plain-passed-value-8271", SCOPE_MODEL, False),
    ("passed secret with spaces as --model", passed_secret, "plain passed value 8271", SCOPE_MODEL, False),
    ("stored plain value in a duplicated config section", plain_config_section, "plain-config-value-8273", SCOPE_NONE,
     False),
    ("stored plain value in a bot's receipts and index", plain_index, "plain-index-value-8275", SCOPE_ALL, False),
    ("stored plain value in a knowledge file", plain_export, "plain-export-value-8274", SCOPE_ALL, False),
    ("stored plain value in a result file", plain_result, "plain-result-value-8276", SCOPE_ALL, False),
    ("stored plain value over two lines in bot output", multiline_output, "plain-multi-first-8280\n"
     "plain-multi-second-8280", SCOPE_ALL, False),
    ("stored plain value in binary results and a binary knowledge file", binary_value, "plain-binary-value-8281",
     SCOPE_ALL, False),
    ("knowledge link named with the value", knowledge_link, VALUE, SCOPE_ALL, False),
    ("knowledge folder link named with the value", knowledge_dir_link, VALUE, SCOPE_ALL, False),
    ("stored plain value in a receipt, log run by hand", plain_receipt, "plain-receipt-value-8277", SCOPE_ALL, True),
    ("receipt file named with the value", receipt_file_name, VALUE, SCOPE_ALL, True),
)


def on_command_line(args, value: str, scope: str) -> bool:
    """Whether a command may be run: a command with the value is kept only where the scope allows it."""
    if value not in args or scope == SCOPE_ALL:
        return True
    return scope == SCOPE_MODEL and any(args[i] == value and args[i - 1] == "--model" for i in range(1, len(args)))


def snapshot(root: Path) -> dict:
    return {path: path.read_bytes() for path in root.rglob("*") if path.is_file() and ".git" not in path.parts}


def texts(path: Path, data: bytes):
    """The pieces of a written file to search: itself and, for an archive, its members' names and bytes."""
    yield str(path), data
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path) as tar:
            for member in tar.getmembers():
                yield member.name, b""
                yield member.linkname, b""
                if member.isfile():
                    yield member.name, tar.extractfile(member).read()


class SecretSinksTest(unittest.TestCase):
    def run_row(self, field, setup, value, scope, log_too):
        p = Project(mode="host", runner="fake")
        self.addCleanup(p.close)
        env = p.root / ".agents" / "team" / "env"
        for member, script in (("worker", FLOW_WORKER), ("bugfixer", FLOW_WORKER), ("review", FLOW_REVIEW)):
            (env / member).mkdir(parents=True)
            (env / member / "fake-cli.sh").write_text(script)
        (env / "review" / "plan").write_text("approve\n")
        p.config.write_text(p.config.read_text() + "\n[flow]\ngate = true\n")
        p.store_secret("ALPHA_KEY", KEY)
        p.brief.write_text("Class: standard\nPaths: file.txt\nObjective: change file.txt\n")
        notes = p.root / "notes.txt"
        notes.write_text(value + "\n")
        inputs = (setup and setup(p, value)) or [notes]
        member = f"team/knowledge/{value}.md"
        archive = p.root / "incoming.tar.gz"
        if "\n" not in value:
            # an archive from elsewhere: a knowledge file whose name is the value (import writes its masked name)
            with tarfile.open(archive, "w:gz") as tar:
                info = tarfile.TarInfo(member)
                info.size = 6
                tar.addfile(info, io.BytesIO(b"notes\n"))
        before = snapshot(p.root)
        brief = str(p.brief)
        given = [a for path in inputs for a in ("--input", str(path))]
        commands = [("run", "worker", "T-1", "--brief", brief, "--dry-run"),
                    ("run", "worker", "T-1", "--brief", brief, "--dry-run", "--model", value),
                    ("run", "worker", "T-1", "--brief", brief, "--runner", value, "--dry-run"),
                    ("run", "worker", "T-1", "--brief", brief, *given),
                    ("run", "worker", "T-1", "--brief", brief, "--model", value, *given),
                    ("result", "worker", "T-1"), ("result", "worker", value),
                    ("flow", "T-40", "--brief", brief, "--repo", str(p.root)),
                    ("flow", "T-40", "--status"), ("status",),
                    ("flow", "T-40", "--brief", str(p.root / f"{value}.md"), "--repo", str(p.root)),
                    ("flow", "T-40", "--brief", str(p.brief), "--repo", str(p.root / value)),
                    ("bot", "add", value), ("bot", "add", "ops"), ("bot", "add", "writer"),
                    ("export", "-o", str(p.root / "export.tar.gz")),
                    ("export", "--with-data", "-o", str(p.root / "export-data.tar.gz")),
                    ("import", str(archive)), ("import", str(archive), "--apply"),
                    ("import", str(p.root / f"{value}.tar.gz"))]
        commands = [c for c in commands if on_command_line(c, value, scope)]
        seen = []
        for args in commands:
            done = p.team(*args)
            seen += [(f"stdout of {args[:2]}", done.stdout), (f"stderr of {args[:2]}", done.stderr)]
        for args in LOG_COMMANDS if log_too else ():
            argv = [a.replace("{v}", value) for a in args]
            done = subprocess.run([sys.executable, str(LOG), *argv], env={**p.env, "WOUF_LOG_DIR": str(
                p.root / ".agents" / "team" / "logs")}, capture_output=True, text=True, cwd=p.root)
            seen += [(f"stdout of log {args[0]}", done.stdout), (f"stderr of log {args[0]}", done.stderr)]
        after = snapshot(p.root)
        for path, data in after.items():
            if before.get(path) != data or path.name.startswith(".index.sqlite"):
                old = before.get(path)
                if old is not None and data.startswith(old):
                    # a file the setup made and a command appended to: only the appended bytes are new
                    data = data[len(old):]
                    seen += [(f"appended to {path.relative_to(p.root)}", data.decode("utf-8", "replace"))]
                    continue
                seen += [(f"file {path.relative_to(p.root)}", name + "\n" + chunk.decode("utf-8", "replace"))
                         for name, chunk in texts(path, data)]
        if "\n" not in value:
            with self.subTest(field=field, where="import destination"):
                self.assertTrue(any(f.name.startswith("***") for f in
                                    (p.root / ".agents" / "team" / "knowledge").glob("*.md")))
        for where, text in seen:
            with self.subTest(field=field, where=where):
                self.assertNotIn(value, text)
        return p

    def test_no_field_reaches_a_sink(self) -> None:
        for field, setup, value, scope, log_too in TABLE:
            with self.subTest(field=field):
                p = self.run_row(field, setup, value, scope, log_too)
                if field == "catalog declaration id":
                    # a key-shaped id in a catalog declaration is replaced by the file name, never copied
                    copied = (p.root / ".agents" / "team" / "bots" / "ops.md").read_text()
                    self.assertIn("\nid: ops\n", copied)
                    self.assertIn("status: proposed", copied)
                if field == "bot declaration id":
                    # the flow reads the declarations it selects before any state or receipt exists
                    self.assertFalse((p.root / ".agents" / "team" / "flows").exists())
                    self.assertFalse((p.root / ".agents" / "team" / "logs" / "lead.jsonl").exists())


class HandlerAndBinaryTest(unittest.TestCase):
    def project(self) -> Project:
        p = Project(mode="host", runner="fake")
        self.addCleanup(p.close)
        return p

    def test_uncaught_error_is_masked_and_debug_shows_a_masked_traceback(self) -> None:
        p = self.project()
        for helper in ("team", "log"):
            with self.subTest(helper=helper):
                code = ("import runpy, sys\nsys.argv = [sys.argv[0]]\n"
                        f"sys.path.insert(0, {str(p.root / '.agents' / 'bin')!r})\n"
                        f"m = runpy.run_path({str(p.root / '.agents' / 'bin' / helper)!r}, run_name='lib')\n"
                        f"m['sys'].excepthook(ValueError, ValueError({VALUE!r}), None)\n")
                for debug in ("", "1"):
                    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                          env={**p.env, "WOUF_DEBUG": debug})
                    self.assertNotIn(VALUE, done.stdout + done.stderr)
                    self.assertIn("ValueError", done.stderr)
                    if not debug:
                        self.assertNotIn("Traceback", done.stderr)

    def test_flow_with_a_missing_brief_named_like_a_key(self) -> None:
        p = self.project()
        done = p.team("flow", "T-40", "--brief", str(p.root / f"{VALUE}.md"), "--repo", str(p.root))
        self.assertNotEqual(done.returncode, 0)
        self.assertNotIn(VALUE, done.stdout + done.stderr)
        self.assertNotIn("Traceback", done.stderr)

    def test_stored_plain_model_reaches_the_cli_and_no_sink(self) -> None:
        # the CLI gets the real value as its model; run.json and every output show it masked only
        p = self.project()
        plain = "plain-model-value-8277"
        p.store_secret("ALPHA_KEY", plain)
        p.set("model_flag =\nstrong =", "model_flag = --model\nstrong =")
        env = p.root / ".agents" / "team" / "env"
        (env / "worker").mkdir(parents=True)
        (env / "worker" / "fake-cli.sh").write_text(
            f"#!/bin/sh\ncase \"$*\" in *\"--model {plain}\"*) echo model-received;; esac\n"
            "echo \"WOUF Worker · T-1 · ready-for-review\"\n")
        done = p.team("run", "worker", "T-1", "--brief", str(p.brief), "--model", plain)
        stage = env / "worker" / "tasks" / "T-1"
        self.assertIn("model-received", (stage / "output.log").read_text())
        run_json = (stage / "run.json").read_text()
        self.assertNotIn(plain, run_json)
        self.assertNotIn(plain, done.stdout + done.stderr)

    def test_binary_files_are_listed_and_only_their_secrets_are_masked(self) -> None:
        p = self.project()
        env = p.root / ".agents" / "team" / "env"
        (env / "worker").mkdir(parents=True)
        (env / "worker" / "fake-cli.sh").write_text(
            "#!/bin/sh\nmkdir -p out && printf '\\377\\376bin' > out/data.bin && echo text > out/a.txt\n"
            "echo \"WOUF Worker · T-1 · ready-for-review\"\n")
        blob = p.root / "blob.bin"
        blob.write_bytes(b"\xff\xfe" + VALUE.encode())
        done = p.team("run", "worker", "T-1", "--brief", str(p.brief), "--input", str(blob))
        stage = env / "worker" / "tasks" / "T-1"
        result = json.loads((stage / "result.json").read_text())
        self.assertEqual(result["binary"], ["in/blob.bin", "out/data.bin"])
        self.assertEqual((stage / "in" / "blob.bin").read_bytes(), b"\xff\xfe***")
        self.assertEqual((stage / "out" / "data.bin").read_bytes(), b"\xff\xfebin")
        self.assertNotIn(VALUE.encode(), (stage / "in" / "blob.bin").read_bytes())
        self.assertIn("binary", done.stderr)
        self.assertNotIn("a.txt", " ".join(result["binary"]))

    def test_a_registered_value_in_binary_bytes_is_masked_and_refused_by_export(self) -> None:
        p = self.project()
        plain = "plain-binary-bytes-8282"
        p.store_secret("ALPHA_KEY", plain)
        env = p.root / ".agents" / "team" / "env"
        (env / "worker").mkdir(parents=True)
        (env / "worker" / "fake-cli.sh").write_text(
            f"#!/bin/sh\nmkdir -p out && printf '\\377{plain}' > out/data.bin\n"
            "echo \"WOUF Worker · T-1 · ready-for-review\"\n")
        p.team("run", "worker", "T-1", "--brief", str(p.brief))
        stage = env / "worker" / "tasks" / "T-1"
        self.assertEqual((stage / "out" / "data.bin").read_bytes(), b"\xff***")
        knowledge = p.root / ".agents" / "team" / "knowledge"
        (knowledge / "blob.bin").write_bytes(b"\xfe" + plain.encode())
        target = p.root / "e.tar.gz"
        done = p.team("export", "--with-data", "-o", str(target))
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("secrets", done.stderr)
        self.assertNotIn(plain, done.stdout + done.stderr)
        self.assertFalse(target.exists())

    def masked(self, name: str) -> str:
        # what the helpers call a component whose key-shaped name was masked: *** and a short hash of the original
        stem, dot, ext = name.replace(VALUE, "***").partition(".")
        return f"{stem}-{hashlib.sha256(name.encode()).hexdigest()[:8]}{dot}{ext}"

    def archive(self, p, members: dict) -> Path:
        path = p.root / "tree.tar.gz"
        with tarfile.open(path, "w:gz") as tar:
            for name, data in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return path

    def test_masked_names_of_different_originals_stay_apart(self) -> None:
        p = self.project()
        other = VALUE.replace("abcdefghijklmnop", "ponmlkjihgfedcba")
        archive = self.archive(p, {f"team/knowledge/{VALUE}/notes.md": b"one", f"team/knowledge/{other}/notes.md": b"two"})
        done = p.team("import", str(archive), "--apply")
        self.assertEqual(done.returncode, 0, done.stderr)
        found = sorted(f.read_text() for f in (p.root / ".agents" / "team" / "knowledge").rglob("notes.md"))
        self.assertEqual(found, ["one", "two"])
        self.assertNotIn(VALUE, done.stdout + done.stderr)

    def test_import_refuses_a_file_that_is_another_members_folder_before_writing(self) -> None:
        p = self.project()
        knowledge = p.root / ".agents" / "team" / "knowledge"
        for first, second in ((f"knowledge/{VALUE}", f"knowledge/{self.masked(VALUE)}/notes.md"),
                              ("knowledge/x", "knowledge/x/notes.md")):
            with self.subTest(first=first.replace(VALUE, "V")):
                archive = self.archive(p, {"team/decisions.md": b"d", f"team/{first}": b"a", f"team/{second}": b"b"})
                done = p.team("import", str(archive), "--apply")
                self.assertNotEqual(done.returncode, 0)
                self.assertNotIn(VALUE, done.stdout + done.stderr)
                self.assertNotIn("Traceback", done.stderr)
                self.assertEqual([f.name for f in knowledge.glob("*")], ["INDEX.md"])
                self.assertFalse((p.root / ".agents" / "team" / "decisions.md").exists() and
                                 (p.root / ".agents" / "team" / "decisions.md").read_text() == "d")

    def test_import_refuses_a_masked_file_name_that_is_already_a_folder(self) -> None:
        p = self.project()
        knowledge = p.root / ".agents" / "team" / "knowledge"
        (knowledge / self.masked(VALUE) / "inner").mkdir(parents=True)
        archive = self.archive(p, {f"team/knowledge/{VALUE}": b"a", "team/decisions.md": b"d"})
        done = p.team("import", str(archive), "--apply")
        self.assertNotEqual(done.returncode, 0)
        self.assertNotIn("Traceback", done.stderr)
        self.assertFalse((p.root / ".agents" / "team" / "decisions.md").exists() and
                         (p.root / ".agents" / "team" / "decisions.md").read_text() == "d")

    def staged_collision(self, p, one: str, two: str):
        folder = p.root / "inputs"
        for name, text in ((one, "payload one"), (two, "payload two")):
            (folder / name).mkdir(parents=True)
            (folder / name / "notes.md").write_text(text)
        return folder

    def test_staging_keeps_both_directories_whose_names_mask_alike(self) -> None:
        p = self.project()
        env = p.root / ".agents" / "team" / "env"
        (env / "worker").mkdir(parents=True)
        (env / "worker" / "fake-cli.sh").write_text("#!/bin/sh\necho \"WOUF Worker · T-1 · ready-for-review\"\n")
        other = VALUE.replace("abcdefghijklmnop", "ponmlkjihgfedcba")
        folder = self.staged_collision(p, VALUE, other)
        done = p.team("run", "worker", "T-1", "--brief", str(p.brief), "--input", str(folder))
        self.assertEqual(done.returncode, 0, done.stderr)
        staged = sorted(f.read_text() for f in (env / "worker" / "tasks" / "T-1" / "in").rglob("notes.md"))
        self.assertEqual(staged, ["payload one", "payload two"])

    def test_collection_keeps_both_directories_whose_names_mask_alike(self) -> None:
        p = self.project()
        env = p.root / ".agents" / "team" / "env"
        (env / "worker").mkdir(parents=True)
        other = VALUE.replace("abcdefghijklmnop", "ponmlkjihgfedcba")
        (env / "worker" / "fake-cli.sh").write_text(
            f"#!/bin/sh\nmkdir -p out/{VALUE} out/{other}\necho one > out/{VALUE}/notes.md\n"
            f"echo two > out/{other}/notes.md\necho \"WOUF Worker · T-1 · ready-for-review\"\n")
        done = p.team("run", "worker", "T-1", "--brief", str(p.brief))
        self.assertEqual(done.returncode, 0, done.stderr)
        out = env / "worker" / "tasks" / "T-1" / "out"
        self.assertEqual(sorted(f.read_text().strip() for f in out.rglob("notes.md")), ["one", "two"])
        self.assertNotIn(VALUE, done.stdout + done.stderr)

    def test_staging_refuses_a_masked_name_that_collides_before_writing(self) -> None:
        p = self.project()
        env = p.root / ".agents" / "team" / "env"
        (env / "worker").mkdir(parents=True)
        (env / "worker" / "fake-cli.sh").write_text("#!/bin/sh\necho x\n")
        folder = p.root / "inputs"
        (folder / VALUE).mkdir(parents=True)
        (folder / VALUE / "notes.md").write_text("one")
        (folder / self.masked(VALUE)).write_text("two")
        done = p.team("run", "worker", "T-1", "--brief", str(p.brief), "--input", str(folder))
        self.assertNotEqual(done.returncode, 0)
        self.assertNotIn(VALUE, done.stdout + done.stderr)
        self.assertNotIn("Traceback", done.stderr)
        self.assertEqual(list((env / "worker" / "tasks" / "T-1" / "in").rglob("notes.md")), [])

    def test_inputs_that_collide_are_refused_before_the_stage_is_cleaned(self) -> None:
        # the previous run's results and brief stay as they were: the staging tree is checked before any write
        p = self.project()
        env = p.root / ".agents" / "team" / "env"
        (env / "worker").mkdir(parents=True)
        (env / "worker" / "fake-cli.sh").write_text("#!/bin/sh\necho x\n")
        stage = env / "worker" / "tasks" / "T-1"
        (stage / "out").mkdir(parents=True)
        (stage / "out" / "keep.txt").write_text("previous result")
        (stage / "in").mkdir(parents=True)
        (stage / "in" / "BRIEF.md").write_text("previous brief")
        cases = {}
        for name, first, second in (
                ("directories named alike", f"one/{VALUE}", f"two/{VALUE}"),
                ("files named alike", "one/notes.txt", "two/notes.txt"),
                ("an input named as the brief", "one/BRIEF.md", "two/notes.txt")):
            for root in ("one", "two"):
                (p.root / root).mkdir(exist_ok=True)
            for path in (first, second):
                target = p.root / path
                if target.suffix or target.name == "BRIEF.md":
                    target.write_text("payload")
                else:
                    target.mkdir(parents=True, exist_ok=True)
                    (target / "notes.md").write_text("payload")
            cases[name] = [a for path in (first, second) for a in ("--input", str(p.root / path))]
        for name, inputs in cases.items():
            with self.subTest(case=name.replace(VALUE, "V")):
                done = p.team("run", "worker", "T-1", "--brief", str(p.brief), *inputs)
                self.assertNotEqual(done.returncode, 0)
                self.assertNotIn(VALUE, done.stdout + done.stderr)
                self.assertNotIn("Traceback", done.stderr)
                self.assertEqual((stage / "out" / "keep.txt").read_text(), "previous result")
                self.assertEqual((stage / "in" / "BRIEF.md").read_text(), "previous brief")

    def test_import_refuses_two_folders_that_mask_to_one_name_before_writing(self) -> None:
        p = self.project()
        knowledge = p.root / ".agents" / "team" / "knowledge"
        archive = self.archive(p, {"team/decisions.md": b"d", f"team/knowledge/{VALUE}/notes.md": b"one",
                                   f"team/knowledge/{self.masked(VALUE)}/notes.md": b"two"})
        done = p.team("import", str(archive), "--apply")
        self.assertNotEqual(done.returncode, 0)
        self.assertNotIn(VALUE, done.stdout + done.stderr)
        self.assertEqual([f.name for f in knowledge.glob("*")], ["INDEX.md"])
        decisions = p.root / ".agents" / "team" / "decisions.md"
        self.assertFalse(decisions.exists() and decisions.read_text() == "d")

    def test_export_refuses_a_link_to_a_folder(self) -> None:
        p = self.project()
        knowledge = p.root / ".agents" / "team" / "knowledge"
        (knowledge / "real").mkdir(parents=True)
        (knowledge / "real" / "a.md").write_text("a")
        (knowledge / "alias").symlink_to("real")
        done = p.team("export", "--with-data", "-o", str(p.root / "e.tar.gz"))
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("links", done.stderr)
        self.assertFalse((p.root / "e.tar.gz").exists())

    def test_export_to_a_name_with_a_registered_value_leaves_no_name_in_the_gzip_header(self) -> None:
        p = self.project()
        p.store_secret("ALPHA_KEY", "plain-export-name-8278")
        target = p.root / "plain-export-name-8278.tar.gz"
        done = p.team("export", "-o", str(target))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn(b"plain-export-name-8278", target.read_bytes()[:64])
        self.assertNotIn("plain-export-name-8278", done.stdout + done.stderr)

    def test_import_refuses_a_bot_named_like_a_key(self) -> None:
        p = self.project()
        archive = p.root / "bot.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            info = tarfile.TarInfo(f"team/bots/{VALUE}.md")
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
        done = p.team("import", str(archive), "--apply")
        self.assertNotEqual(done.returncode, 0)
        self.assertNotIn(VALUE, done.stdout + done.stderr)


# Functions that are the sinks: every other print or write call in the helpers is a bug.
SINKS = {"team": {"say", "sink", "put_bytes", "archive_add"}, "log": {"say", "put"}, "federate": set(), "project": set()}
# Calls that put text or copies into a file. A tar's add() writes an archive member; other add() calls are sets.
WRITES = {"print", "write", "write_text", "write_bytes", "dump", "writelines", "copy", "copy2", "copyfile",
          "copytree", "copyfileobj"}


class MainPathGapsTest(unittest.TestCase):
    """The three gaps of the T-0005 series on the main paths: container runs, imported receipts, rotated values."""

    def test_a_container_run_registers_its_keys_for_the_helpers_inside(self) -> None:
        plain = "plain-container-key-9101"
        p = Project(mode="container", engine_name="fakeengine", runner="alpha")
        self.addCleanup(p.close)
        p.store_secret("ALPHA_KEY", plain)
        taken = p.bin / "stdin-taken"
        fake = p.bin / "fakeengine"
        # An engine that answers every command and keeps what `start` reads: the keys the container is given.
        fake.write_text(f"#!/bin/sh\ncase \"$1\" in start) cat > {taken} ;; esac\nexit 0\n")
        fake.chmod(0o755)
        p.team("run", "worker", "T-1", "--brief", str(p.brief))
        lines = taken.read_text().split("\n")
        self.assertIn(f"ALPHA_KEY={plain}", lines)
        mask = [l for l in lines if l.startswith("WOUF_MASK=")]
        self.assertEqual(len(mask), 1, lines)
        self.assertIn(plain, json.loads(mask[0].split("=", 1)[1]))
        # What the container script does with those lines, then what the helpers write there.
        logs = p.root / "container-logs"
        logs.mkdir()
        loop = ('while IFS= read -r line; do [ -z "$line" ] && break; k=${line%%=*}; v=${line#*=}; '
                'export "$k=$v"; done')
        env = {k: v for k, v in p.env.items() if k != "WOUF_SECRET_DIR"}
        env.update(WOUF_LOG_DIR=str(logs), WOUF_SECRET_DIR=str(p.root / "no-store"), WOUF_SECRET_BACKEND="file")
        done = subprocess.run(
            ["sh", "-c", f'{loop}\n{sys.executable} {LOG} add worker T-1 done "saw $ALPHA_KEY" --ref "$ALPHA_KEY" '
             f'&& {sys.executable} {LOG} find saw && {sys.executable} {LOG} tail'],
            input=taken.read_text(), env=env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("saw ***", done.stdout)
        self.assertNotIn(plain, done.stdout + done.stderr)
        for path in logs.rglob("*"):
            if path.is_file():
                self.assertNotIn(plain.encode(), path.read_bytes(), path.name)
        # team inside the container masks the same values
        said = subprocess.run([sys.executable, str(p.root / ".agents" / "bin" / "team"), "result", "worker", plain],
                              env={**p.env, "WOUF_SECRET_DIR": str(p.root / "no-store"),
                                   "WOUF_MASK": json.dumps([plain])}, capture_output=True, text=True, cwd=p.root)
        self.assertNotIn(plain, said.stdout + said.stderr)

    def test_a_container_run_gets_only_the_values_it_was_passed(self) -> None:
        passed, unrelated = "plain-passed-key-9105", "plain-unrelated-key-9106"
        p = Project(mode="container", engine_name="fakeengine", runner="alpha")
        self.addCleanup(p.close)
        p.store_secret("ALPHA_KEY", passed)
        p.store_secret("UNRELATED_KEY", unrelated)
        taken = p.bin / "stdin-taken"
        fake = p.bin / "fakeengine"
        fake.write_text(f"#!/bin/sh\ncase \"$1\" in start) cat > {taken} ;; esac\nexit 0\n")
        fake.chmod(0o755)
        p.team("run", "worker", "T-1", "--brief", str(p.brief))
        text = taken.read_text()
        self.assertIn(f"ALPHA_KEY={passed}", text.split("\n"))
        self.assertNotIn(unrelated, text, "a value that was not passed for this run stays out of the container")
        mask = [l for l in text.split("\n") if l.startswith("WOUF_MASK=")]
        self.assertEqual(len(mask), 1, text)
        self.assertEqual(json.loads(mask[0].split("=", 1)[1]), [passed])

    def collection_project(self, plain: str, tar_script: str) -> Project:
        """A container project whose engine answers `cp <container>:/outbox -` with the archive tar_script prints."""
        p = Project(mode="container", engine_name="fakeengine", runner="alpha")
        self.addCleanup(p.close)
        p.store_secret("ALPHA_KEY", plain)
        (p.bin / "emit.py").write_text(tar_script)
        fake = p.bin / "fakeengine"
        fake.write_text(f"#!/bin/sh\ncase \"$1\" in start) cat >/dev/null ;;\n"
                        f"cp) case \"$3\" in -) case \"$2\" in *:/outbox) exec {sys.executable} {p.bin / 'emit.py'} ;; "
                        f"esac ;; esac ;; esac\nexit 0\n")
        fake.chmod(0o755)
        return p

    def host_bytes(self, p: Project) -> bytes:
        """Everything the run left on this computer under its env folder."""
        env = p.root / ".agents" / "team" / "env"
        return b"\n".join(path.name.encode() + b"\n" + (os.readlink(path).encode() if path.is_symlink() else path.read_bytes())
                          for path in env.rglob("*") if path.is_symlink() or path.is_file())

    TAR_HEAD = ("import sys, tarfile, io\nplain = {plain!r}\n"
                "def add(tar, name, data):\n    info = tarfile.TarInfo(name); info.size = len(data)\n"
                "    tar.addfile(info, io.BytesIO(data))\n")

    def test_a_container_collection_masks_every_file_before_it_reaches_the_host(self) -> None:
        plain = "plain-collected-value-9301"
        p = self.collection_project(plain, self.TAR_HEAD.format(plain=plain) +
                                    "with tarfile.open(fileobj=sys.stdout.buffer, mode='w|') as tar:\n"
                                    "    add(tar, 'outbox/out/notes.txt', ('note ' + plain + '\\n').encode())\n"
                                    "    add(tar, f'outbox/out/{plain}.md', b'x')\n"
                                    "    add(tar, 'outbox/out/blob.bin', b'\\xff\\xfe ' + plain.encode())\n")
        p.team("run", "worker", "T-1", "--brief", str(p.brief))
        raw = self.host_bytes(p)
        self.assertNotIn(plain.encode(), raw)
        notes = next((p.root / ".agents" / "team" / "env").rglob("notes.txt"))
        self.assertEqual(notes.read_text(), "note ***\n")
        self.assertTrue(any(f.name.startswith("***") for f in (p.root / ".agents" / "team" / "env").rglob("*.md")))

    def test_a_collection_that_fails_half_way_leaves_no_unmasked_copy(self) -> None:
        plain = "plain-halfway-value-9302"
        p = self.collection_project(plain, self.TAR_HEAD.format(plain=plain) +
                                    "tar = tarfile.open(fileobj=sys.stdout.buffer, mode='w|')\n"
                                    "add(tar, 'outbox/out/first.txt', ('first ' + plain + '\\n').encode())\n"
                                    "info = tarfile.TarInfo('outbox/out/gone.txt'); info.size = 5000\n"
                                    "tar.addfile(info, io.BytesIO(plain.encode() * 3))\n"
                                    "sys.stdout.buffer.flush()\nsys.exit(1)\n")
        done = p.team("run", "worker", "T-1", "--brief", str(p.brief))
        self.assertNotIn(plain, done.stdout + done.stderr)
        self.assertNotIn(plain.encode(), self.host_bytes(p))
        env = p.root / ".agents" / "team" / "env"
        self.assertEqual([x for x in env.rglob("first.txt")] + [x for x in env.rglob("gone.txt")], [],
                         "a failed collection keeps no partial copy")
        self.assertEqual([x for x in env.rglob("outbox")], [])
        result = json.loads(next(env.rglob("result.json")).read_text())
        self.assertIn("could not be collected", result["error"])

    def test_a_hard_link_collects_the_masked_contents_under_its_own_name(self) -> None:
        plain = "plain-linked-value-9304"
        p = self.collection_project(plain, self.TAR_HEAD.format(plain=plain) +
                                    "with tarfile.open(fileobj=sys.stdout.buffer, mode='w|') as tar:\n"
                                    "    add(tar, 'outbox/out/a.txt', ('note ' + plain + '\\n').encode())\n"
                                    "    link = tarfile.TarInfo('outbox/out/b.txt'); link.type = tarfile.LNKTYPE\n"
                                    "    link.linkname = 'outbox/out/a.txt'; tar.addfile(link)\n")
        p.team("run", "worker", "T-1", "--brief", str(p.brief))
        self.assertNotIn(plain.encode(), self.host_bytes(p))
        env = p.root / ".agents" / "team" / "env"
        for name in ("a.txt", "b.txt"):
            with self.subTest(name=name):
                found = list(env.rglob(name))
                self.assertEqual(len(found), 1, f"{name} is kept once")
                self.assertEqual(found[0].read_text(), "note ***\n")

    def test_a_hard_link_to_a_file_not_collected_leaves_no_copy(self) -> None:
        plain = "plain-dangling-value-9305"
        p = self.collection_project(plain, self.TAR_HEAD.format(plain=plain) +
                                    "tar = tarfile.open(fileobj=sys.stdout.buffer, mode='w|')\n"
                                    "add(tar, 'outbox/out/first.txt', ('first ' + plain + '\\n').encode())\n"
                                    "link = tarfile.TarInfo('outbox/out/b.txt'); link.type = tarfile.LNKTYPE\n"
                                    "link.linkname = 'outbox/out/missing.txt'; tar.addfile(link)\n"
                                    "tar.close()\n")
        done = p.team("run", "worker", "T-1", "--brief", str(p.brief))
        self.assertNotIn(plain, done.stdout + done.stderr)
        self.assertNotIn(plain.encode(), self.host_bytes(p))
        env = p.root / ".agents" / "team" / "env"
        self.assertEqual(list(env.rglob("first.txt")) + list(env.rglob("b.txt")), [],
                         "a link that cannot be filled fails the collection, and nothing of it stays")
        result = json.loads(next(env.rglob("result.json")).read_text())
        self.assertIn("could not be collected", result["error"])

    def test_an_archive_of_a_container_storage_keeps_every_hard_link_name(self) -> None:
        plain = "plain-archived-link-9306"
        p = self.collection_project(plain, "")
        fake = p.bin / "fakeengine"
        script = (self.TAR_HEAD.format(plain=plain) + "with tarfile.open(fileobj=sys.stdout.buffer, mode='w|') as tar:\n"
                  "    add(tar, 'bot/home/auth.txt', ('token ' + plain).encode())\n"
                  "    link = tarfile.TarInfo('bot/home/copy.txt'); link.type = tarfile.LNKTYPE\n"
                  "    link.linkname = 'bot/home/auth.txt'; tar.addfile(link)\n")
        (p.bin / "emit.py").write_text(script)
        fake.write_text(f"#!/bin/sh\ncase \"$1\" in cp) exec {sys.executable} {p.bin / 'emit.py'} ;; esac\nexit 0\n")
        done = p.team("bot", "reset", "worker")
        self.assertNotIn(plain, done.stdout + done.stderr)
        self.assertNotIn(plain.encode(), self.host_bytes(p))
        archived = list((p.root / ".agents" / "team" / "env" / "_archive").rglob("*.txt"))
        self.assertEqual(sorted(path.name for path in archived), ["auth.txt", "copy.txt"], done.stdout + done.stderr)
        for path in archived:
            self.assertEqual(path.read_text(), "token ***")

    def test_an_archive_keeps_symbolic_links_as_links_and_reads_nothing_through_them(self) -> None:
        plain = "plain-archived-sym-9307"
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside, True)
        (outside / "secret.txt").write_text("outside " + plain)
        for action in ("reset", "remove"):
            with self.subTest(action=action, mode="container"):
                p = self.collection_project(plain, "")
                script = (self.TAR_HEAD.format(plain=plain) + "with tarfile.open(fileobj=sys.stdout.buffer, mode='w|') as tar:\n"
                          "    add(tar, 'bot/home/auth.txt', ('token ' + plain).encode())\n"
                          "    for name, target in (('current-auth', 'auth.txt'), ('away', " + repr(str(outside)) + "),\n"
                          "                         ('named-' + plain, 'auth.txt'), ('to-' + plain, plain + '/x')):\n"
                          "        link = tarfile.TarInfo('bot/home/' + name); link.type = tarfile.SYMTYPE\n"
                          "        link.linkname = target; tar.addfile(link)\n")
                (p.bin / "emit.py").write_text(script)
                (p.bin / "fakeengine").write_text(
                    f"#!/bin/sh\ncase \"$1\" in cp) exec {sys.executable} {p.bin / 'emit.py'} ;; esac\nexit 0\n")
                done = p.team("bot", action, "worker")
                self.assertNotIn(plain, done.stdout + done.stderr)
                self.assertNotIn(plain.encode(), self.host_bytes(p))
                archived = next((p.root / ".agents" / "team" / "env" / "_archive").iterdir()) / "home"
                self.assertEqual(os.readlink(archived / "current-auth"), "auth.txt")
                self.assertEqual(os.readlink(archived / "away"), str(outside))
                masked = sorted(path.name for path in archived.iterdir() if path.is_symlink())
                self.assertEqual(len(masked), 4, masked)
                self.assertFalse(any(plain in n or plain in os.readlink(archived / n) for n in masked))
                self.assertEqual((archived / "auth.txt").read_text(), "token ***")
        with self.subTest(mode="host"):
            p = Project(mode="host", runner="alpha")
            self.addCleanup(p.close)
            p.store_secret("ALPHA_KEY", plain)
            home = p.root / ".agents" / "team" / "env" / "worker" / "home"
            home.mkdir(parents=True)
            (home / "auth.txt").write_text("token " + plain)
            (home / "current-auth").symlink_to("auth.txt")
            (home / "away").symlink_to(outside / "secret.txt")
            (home / "awaydir").symlink_to(outside, target_is_directory=True)
            done = p.team("bot", "reset", "worker")
            self.assertEqual(done.returncode, 0, done.stderr)
            archived = next((p.root / ".agents" / "team" / "env" / "_archive").iterdir()) / "home"
            self.assertEqual(os.readlink(archived / "current-auth"), "auth.txt")
            self.assertEqual(os.readlink(archived / "away"), str(outside / "secret.txt"))
            self.assertEqual(os.readlink(archived / "awaydir"), str(outside))
            self.assertEqual((archived / "auth.txt").read_text(), "token ***")
            self.assertNotIn(plain.encode(), self.host_bytes(p))

    def test_an_archive_keeps_the_syntax_of_link_targets(self) -> None:
        plain = "plain-archived-syntax-9309"
        links = (("self", "."), ("slash", "auth.txt/"), ("up", "../home/./auth.txt"), ("abs", "//tmp//x/"),
                 ("hidden", plain + "/"))
        kept = {name: target for name, target in links if plain not in target}
        for action in ("reset", "remove"):
            with self.subTest(action=action, mode="container"):
                p = self.collection_project(plain, "")
                script = (self.TAR_HEAD.format(plain=plain) + "with tarfile.open(fileobj=sys.stdout.buffer, mode='w|') as tar:\n"
                          "    add(tar, 'bot/home/auth.txt', ('token ' + plain).encode())\n"
                          f"    for name, target in {links!r}:\n"
                          "        link = tarfile.TarInfo('bot/home/' + name); link.type = tarfile.SYMTYPE\n"
                          "        link.linkname = target; tar.addfile(link)\n")
                (p.bin / "emit.py").write_text(script)
                (p.bin / "fakeengine").write_text(
                    f"#!/bin/sh\ncase \"$1\" in cp) exec {sys.executable} {p.bin / 'emit.py'} ;; esac\nexit 0\n")
                done = p.team("bot", action, "worker")
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                archived = next((p.root / ".agents" / "team" / "env" / "_archive").iterdir()) / "home"
                for name, target in kept.items():
                    self.assertEqual(os.readlink(archived / name), target)
                hidden = os.readlink(archived / "hidden")
                self.assertTrue(hidden.endswith("/") and plain not in hidden, hidden)
        with self.subTest(mode="host"):
            p = Project(mode="host", runner="alpha")
            self.addCleanup(p.close)
            p.store_secret("ALPHA_KEY", plain)
            home = p.root / ".agents" / "team" / "env" / "worker" / "home"
            home.mkdir(parents=True)
            (home / "auth.txt").write_text("token " + plain)
            for name, target in links:
                os.symlink(target, home / name)
            done = p.team("bot", "reset", "worker")
            self.assertEqual(done.returncode, 0, done.stderr)
            archived = next((p.root / ".agents" / "team" / "env" / "_archive").iterdir()) / "home"
            for name, target in kept.items():
                self.assertEqual(os.readlink(archived / name), target)
            hidden = os.readlink(archived / "hidden")
            self.assertTrue(hidden.endswith("/") and plain not in hidden, hidden)
            self.assertNotIn(plain.encode(), self.host_bytes(p))

    def test_a_storage_that_could_not_be_archived_is_not_reset(self) -> None:
        plain = "plain-archived-fail-9308"
        p = self.collection_project(plain, "")
        (p.bin / "emit.py").write_text(
            self.TAR_HEAD.format(plain=plain) + "tar = tarfile.open(fileobj=sys.stdout.buffer, mode='w|')\n"
            "add(tar, 'bot/home/auth.txt', b'x')\nsys.stdout.buffer.flush()\nsys.exit(1)\n")
        log = p.bin / "engine.log"
        (p.bin / "fakeengine").write_text(
            f"#!/bin/sh\necho \"$@\" >> {log}\ncase \"$1\" in cp) exec {sys.executable} {p.bin / 'emit.py'} ;; esac\nexit 0\n")
        done = p.team("bot", "reset", "worker")
        self.assertNotEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertNotIn("volume rm", log.read_text())

    def test_an_archive_of_a_container_storage_is_masked_and_a_failure_leaves_nothing(self) -> None:
        plain = "plain-archived-value-9303"
        p = self.collection_project(plain, "")
        fake = p.bin / "fakeengine"
        good = (self.TAR_HEAD.format(plain=plain) + "with tarfile.open(fileobj=sys.stdout.buffer, mode='w|') as tar:\n"
                "    add(tar, 'bot/home/auth.txt', ('token ' + plain).encode())\n")
        bad = good.replace("with tarfile.open(fileobj=sys.stdout.buffer, mode='w|') as tar:\n    add",
                           "tar = tarfile.open(fileobj=sys.stdout.buffer, mode='w|')\nadd") + \
            "sys.stdout.buffer.flush()\nsys.exit(1)\n"
        for label, script, expect in (("complete", good, True), ("failing", bad, False)):
            with self.subTest(archive=label):
                (p.bin / "emit.py").write_text(script)
                fake.write_text(f"#!/bin/sh\ncase \"$1\" in cp) exec {sys.executable} {p.bin / 'emit.py'} ;; esac\nexit 0\n")
                done = p.team("bot", "reset", "worker")
                self.assertNotIn(plain, done.stdout + done.stderr)
                self.assertNotIn(plain.encode(), self.host_bytes(p))
                found = list((p.root / ".agents" / "team" / "env" / "_archive").rglob("auth.txt"))
                self.assertEqual(bool(found), expect, done.stdout + done.stderr)
                for path in found:
                    self.assertEqual(path.read_text(), "token ***")
                shutil.rmtree(p.root / ".agents" / "team" / "env" / "_archive", ignore_errors=True)

    def test_an_imported_receipt_holding_a_registered_value_with_a_quote_is_masked(self) -> None:
        value = 'plain"quoted\\value-9102'
        p = Project(mode="host", runner="fake")
        self.addCleanup(p.close)
        p.store_secret("ALPHA_KEY", value)
        line = json.dumps({"t": "2026-10-19T09:00:00.000+00:00", "who": "worker", "task": "T-1", "outcome": "done",
                           "signal": f"saw {value}", "ref": value, "cost": ""})
        self.assertNotIn(value, line, "serialized, the value is escaped: text masking alone misses it")
        archive = p.root / "incoming.tar.gz"
        data = (line + "\n").encode()
        with tarfile.open(archive, "w:gz") as tar:
            info = tarfile.TarInfo("team/logs/worker.jsonl")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        done = p.team("import", str(archive), "--apply")
        self.assertEqual(done.returncode, 0, done.stderr)
        written = (p.root / ".agents" / "team" / "import" / "incoming" / "logs" / "worker.jsonl").read_text()
        self.assertNotIn(json.dumps(value)[1:-1], written)
        entry = json.loads(written)
        self.assertEqual((entry["signal"], entry["ref"]), ("saw ***", "***"))

    def test_a_rotated_value_changes_the_index_stamp(self) -> None:
        old, new = "plain-rotated-old-9103", "plain-rotated-new-9104"
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        logs, store = Path(tmp.name) / "logs", Path(tmp.name) / "secrets"
        logs.mkdir()
        store.mkdir(mode=0o700)
        (logs / "worker.jsonl").write_text(json.dumps(
            {"t": "2026-10-19T09:00:00.000+00:00", "who": "worker", "task": "T-1", "outcome": "done",
             "signal": f"saw {new}", "ref": "", "cost": ""}) + "\n")
        env = {**os.environ, "WOUF_LOG_DIR": str(logs), "WOUF_SECRET_DIR": str(store), "WOUF_SECRET_BACKEND": "file"}
        env.pop("WOUF_MASK", None)
        entry = store / "ALPHA_KEY"
        entry.write_text(old + "\n")
        (store / ".names").write_text("ALPHA_KEY\n")

        def find():
            return subprocess.run([sys.executable, str(LOG), "find", "saw"], env=env, capture_output=True, text=True)

        def stamp() -> str:
            db = sqlite3.connect(logs / ".index.sqlite")
            try:
                return db.execute("SELECT v FROM meta WHERE k = 'stamp'").fetchone()[0]
            finally:
                db.close()

        self.assertIn(new, find().stdout)
        before, kept = stamp(), entry.stat()
        # rotated in place with the same size and time, as a backend outside the folder may
        entry.write_text(new + "\n")
        os.utime(entry, ns=(kept.st_atime_ns, kept.st_mtime_ns))
        # a row of the old index that holds neither value stays valid for the old stamp only
        done = find()
        self.assertNotIn(new, done.stdout + done.stderr)
        self.assertNotEqual(stamp(), before)
        self.assertFalse(any(new.encode() in f.read_bytes() for f in logs.glob(".index.sqlite*")))


class NoDirectSinksTest(unittest.TestCase):
    def test_no_print_or_write_call_outside_the_sinks(self) -> None:
        import ast
        # Every helper in SINKS (federate and project write through team's sinks), and every module of bin/team's teamlib/.
        helpers = [(helper, Path(LOG).parent / helper) for helper in SINKS]
        helpers += [("team", path) for path in sorted((Path(LOG).parent / "teamlib").glob("*.py"))]
        self.assertIn("core.py", [path.name for _, path in helpers])
        for helper, path in helpers:
            tree = ast.parse(path.read_text())
            for func in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.Lambda))]:
                name = getattr(func, "name", "<lambda>")
                if name in SINKS[helper]:
                    continue
                for call in [n for n in ast.walk(func) if isinstance(n, ast.Call)]:
                    fn = call.func
                    called = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
                    # The one allowed direct write: the keys a caller passes to a run go down a process's stdin pipe
                    # (no file, no screen), and they have to arrive unmasked.
                    to_stdin = isinstance(fn, ast.Attribute) and getattr(fn.value, "attr", "") == "stdin"
                    writes_member = called == "add" and getattr(fn.value, "id", "") == "tar"
                    if (called in WRITES or writes_member) and not to_stdin:
                        self.fail(f"{path.name}:{call.lineno} calls {called}() in {name}; use say/sink/put")
            if helper != "team":
                continue
            for node in tree.body:  # module level code outside functions
                for call in [n for n in ast.walk(node) if isinstance(n, ast.Call)] if not isinstance(
                        node, (ast.FunctionDef, ast.ClassDef)) else []:
                    called = getattr(call.func, "id", getattr(call.func, "attr", ""))
                    self.assertNotIn(called, WRITES, f"{path.name}:{call.lineno}")


if __name__ == "__main__":
    unittest.main()
