"""Tests for federate send and pull: envelopes carried on a git mailbox, held in quarantine, never run."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_federate import Team, helper_module  # noqa: E402
from support import isolate_wouf_env  # noqa: E402


def git(*args: str, cwd: Path | None = None, data: bytes | None = None) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, input=data, capture_output=True, check=True,
                          env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    return done.stdout.decode().strip()


def disable_maintenance(repo: Path) -> None:
    """Keep Git from starting detached maintenance jobs against repositories cleaned up by the test."""
    git("--git-dir", str(repo), "config", "gc.auto", "0")
    git("--git-dir", str(repo), "config", "maintenance.auto", "false")


def setUpModule() -> None:
    isolate_wouf_env()


@unittest.skipUnless(shutil.which("ssh-keygen") and shutil.which("git"), "needs ssh-keygen and git")
class TransportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.mailbox = self.tmp / "wouf-a2a.git"
        git("init", "-q", "--bare", str(self.mailbox))
        disable_maintenance(self.mailbox)
        # The mailbox is reached as over SSH: this stand-in runs the remote side's git as a host would, in its own
        # process with none of the caller's git settings.
        fake_ssh = self.tmp / "fake-ssh"
        fake_ssh.write_text('#!/bin/sh\nfor last; do :; done\n'
                            'exec env -u GIT_CONFIG_PARAMETERS -u GIT_CONFIG_COUNT sh -c "git ${last#git-}"\n')
        fake_ssh.chmod(0o755)
        self.url = f"ssh://mailbox.test{self.mailbox}"
        self.a = Team(self.tmp, "team-a", "team-b")
        self.b = Team(self.tmp, "team-b", "team-a")
        self.a.trust("team-b", self.b)
        self.b.trust("team-a", self.a)
        for t in (self.a, self.b):
            with (t.dir / "config.ini").open("a", encoding="utf-8") as handle:
                handle.write(f"mailbox = {self.url}\n")
            t.env.update(GIT_SSH_COMMAND=str(fake_ssh), GIT_SSH_VARIANT="simple")
            t.env.pop("WOUF_EVENTS_DIR", None)  # the events follow WOUF_TEAM_DIR, as bin/team's state commands do

    def outbox(self) -> Path:
        return self.a.dir / "env" / "federation" / "outbox" / "team-b.jsonl"

    def pack(self, name: str = "api.md", text: str = "# API\n") -> dict:
        spec = self.tmp / name
        spec.write_text(text, encoding="utf-8")
        done = self.a.run("pack", "spec_sync", "--to", "team-b", "--file", str(spec))
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def send(self, *args: str) -> subprocess.CompletedProcess:
        return self.a.run("send", str(self.outbox()), *args)

    def pull(self) -> subprocess.CompletedProcess:
        return self.b.run("pull")

    def held(self) -> list[str]:
        folder = self.b.dir / "import" / "federation" / "team-a"
        return sorted(p.stem for p in folder.glob("*.json")) if folder.is_dir() else []

    def events(self, t: Team) -> list[dict]:
        folder = t.dir / "events"
        return [json.loads(line) for f in sorted(folder.glob("*.jsonl")) for line in f.read_text().splitlines()]

    def ref(self, name: str = "team-a") -> str:
        return git("--git-dir", str(self.mailbox), "for-each-ref", "--format=%(objectname)", f"refs/wouf-a2a/{name}")

    def push_raw(self, files: dict[str, bytes], parent: str | None = None, ref: str = "team-a",
                 force: bool = False) -> str:
        """A commit as a broken or hostile mailbox writer could push it, without federate."""
        work = self.tmp / "raw.git"
        if not work.is_dir():
            git("init", "-q", "--bare", str(work))
            disable_maintenance(work)
        entries = "".join(f"100644 blob {git('--git-dir', str(work), 'hash-object', '-w', '--stdin', data=body)}\t{n}\n"
                          for n, body in files.items())
        tree = git("--git-dir", str(work), "mktree", data=entries.encode())
        if parent:
            git("--git-dir", str(work), "fetch", "-q", str(self.mailbox), f"refs/wouf-a2a/{ref}")
        commit = git("--git-dir", str(work), "-c", "user.name=x", "-c", "user.email=x@example.org", "commit-tree",
                     tree, *(["-p", parent] if parent else []), "-m", "raw")
        git("--git-dir", str(work), "push", "-q", *(["--force"] if force else []), str(self.mailbox),
            f"{commit}:refs/wouf-a2a/{ref}")
        return commit

    # ------------------------------------------------------------ send

    def test_events_land_in_the_team_folder_not_in_the_pack(self) -> None:
        pack_events = Path(__file__).resolve().parents[1] / ".agents" / "team" / "events"
        before = sorted(pack_events.rglob("*")) if pack_events.exists() else []
        self.pack()
        self.assertEqual(self.send("--yes").returncode, 0)
        self.assertEqual([e["kind"] for e in self.events(self.a)], ["a2a.sent"])
        self.assertEqual(sorted(pack_events.rglob("*")) if pack_events.exists() else [], before)

    def test_send_without_yes_only_says_what_it_would_send(self) -> None:
        env = self.pack()
        done = self.send()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(f"would send {env['id']} spec_sync to team-b seq 1", done.stdout)
        self.assertIn("--yes", done.stdout)
        self.assertEqual(self.ref(), "")
        self.assertEqual(self.events(self.a), [])

    def test_send_puts_one_envelope_per_commit_and_only_moves_forward(self) -> None:
        first = self.pack()
        done = self.send("--yes")
        self.assertEqual(done.returncode, 0, done.stderr)
        tip = self.ref()
        tree = git("--git-dir", str(self.mailbox), "ls-tree", tip)
        self.assertEqual(len(tree.splitlines()), 1)
        self.assertTrue(tree.endswith("\tenvelope.json"))
        blob = git("--git-dir", str(self.mailbox), "show", f"{tip}:envelope.json")
        self.assertEqual(json.loads(blob)["id"], first["id"])
        # the same file again sends nothing new; a new envelope is a child of the tip
        again = self.send("--yes")
        self.assertIn("nothing new", again.stdout)
        self.assertEqual(self.ref(), tip)
        self.pack("b.md")
        self.assertEqual(self.send("--yes").returncode, 0)
        self.assertEqual(git("--git-dir", str(self.mailbox), "rev-parse", f"{self.ref()}^"), tip)
        sent = [e["data"] for e in self.events(self.a) if e["kind"] == "a2a.sent"]
        self.assertEqual([(s["seq"], s["to"], s["kind"]) for s in sent], [(1, "team-b", "spec_sync"),
                                                                           (2, "team-b", "spec_sync")])

    def test_a_failed_push_keeps_the_seq_and_writes_the_event_once_it_is_in(self) -> None:
        self.pack()
        hook = self.mailbox / "hooks" / "pre-receive"  # the mailbox host refuses this push
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        done = self.send("--yes")
        self.assertEqual(done.returncode, 1)
        self.assertIn("push to the mailbox failed", done.stderr)
        self.assertEqual(self.ref(), "")
        self.assertEqual([e for e in self.events(self.a) if e["kind"] == "a2a.sent"], [])
        # sent again once the host takes it: the same envelope and seq, never a new one, and one event
        hook.unlink()
        self.assertEqual(self.send("--yes").returncode, 0)
        self.assertEqual([e["data"]["seq"] for e in self.events(self.a) if e["kind"] == "a2a.sent"], [1])
        on_box = git("--git-dir", str(self.mailbox), "show", f"{self.ref()}:envelope.json")
        self.assertEqual(json.loads(on_box)["seq"], 1)
        self.assertEqual(self.pack("c.md")["seq"], 2)

    def test_send_refuses_what_is_not_its_own_envelope(self) -> None:
        foreign = self.tmp / "foreign.jsonl"
        (self.tmp / "x.md").write_text("# X\n")
        env = json.loads(self.b.run("pack", "spec_sync", "--to", "team-a", "--file", str(self.tmp / "x.md")).stdout)
        foreign.write_text(json.dumps(env) + "\n{not json\n")
        done = self.a.run("send", str(foreign), "--yes")
        self.assertEqual(done.returncode, 1)
        self.assertIn("refused line 1", done.stdout)
        self.assertIn("refused line 2", done.stdout)
        self.assertEqual(self.ref(), "")

    def test_send_pushes_nothing_with_a_secret_or_a_line_changed_after_pack(self) -> None:
        token = "gh" + "p_" + "A1b2C3d4" * 5  # built here, so this file holds no key shape
        for change, says in ((lambda env: env["body"].update(text=f"# API\n{token}\n"), "secret"),
                             (lambda env: env["body"].update(text="# API\nsee /home/owner/notes/plan.md\n"),
                              "bad signature"),
                             (lambda env: env.update(seq=7), "bad signature")):
            with self.subTest(says=says):
                env = self.pack()
                change(env)
                self.outbox().write_text(json.dumps(env) + "\n")
                done = self.send("--yes")
                self.assertEqual(done.returncode, 1)
                self.assertIn(says, done.stdout)
                self.assertNotIn(token, done.stdout + done.stderr)
                self.assertEqual(self.ref(), "")
                self.assertEqual(self.events(self.a), [])
        # the line as pack wrote it still goes
        self.outbox().unlink()
        env = self.pack()
        self.assertEqual(self.send("--yes").returncode, 0)
        self.assertEqual(json.loads(git("--git-dir", str(self.mailbox), "show", f"{self.ref()}:envelope.json"))["id"],
                         env["id"])

    def test_pack_into_the_outbox_itself_writes_the_line_once(self) -> None:
        spec = self.tmp / "api.md"
        spec.write_text("# API\n", encoding="utf-8")
        self.outbox().parent.mkdir(parents=True, exist_ok=True)
        for target in (self.outbox(), self.outbox().parent / "." / self.outbox().name):
            done = self.a.run("pack", "spec_sync", "--to", "team-b", "--file", str(spec), "-o", str(target))
            self.assertEqual(done.returncode, 0, done.stderr)
        ids = [json.loads(line)["id"] for line in self.outbox().read_text().splitlines()]
        self.assertEqual(len(ids), 2)
        self.assertEqual(len(set(ids)), 2)
        other = self.tmp / "copy.jsonl"
        self.a.run("pack", "spec_sync", "--to", "team-b", "--file", str(spec), "-o", str(other))
        self.assertEqual(len(other.read_text().splitlines()), 1)
        self.assertEqual(len(self.outbox().read_text().splitlines()), 3)

    def test_an_envelope_twice_in_the_outbox_is_sent_once(self) -> None:
        env = self.pack()
        line = json.dumps(env, ensure_ascii=False) + "\n"
        with self.outbox().open("a", encoding="utf-8") as handle:  # as pack -o into the outbox itself writes it
            handle.write(line)
        self.assertEqual(self.send("--yes").returncode, 0)
        self.assertEqual(git("--git-dir", str(self.mailbox), "rev-list", "--count", self.ref()), "1")
        changed = dict(env, seq=9)
        with self.outbox().open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(changed) + "\n")
        done = self.send("--yes")
        self.assertEqual(done.returncode, 1)
        self.assertIn("refused line 3", done.stdout)

    # ------------------------------------------------------------ pull

    def test_round_trip_holds_and_applies_only_by_hand(self) -> None:
        env = self.pack("api.md", "# API\n\nv1\n")
        self.send("--yes")
        done = self.pull()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.held(), [env["id"]])
        self.assertIn("1 held", done.stdout)
        self.assertFalse((self.b.dir / "knowledge" / "federated").exists())
        inbox = self.b.dir / "env" / "federation" / "inbox" / "team-a" / f"{env['id']}.jsonl"
        self.assertIn(f"federate receive {inbox} --apply", done.stdout.replace("team/", f"{self.b.dir}/"))
        applied = self.b.run("receive", str(inbox), "--apply")
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        self.assertIn("v1", (self.b.dir / "knowledge/federated/team-a/api.md").read_text())
        received = [e["data"] for e in self.events(self.b) if e["kind"] == "a2a.received"]
        self.assertEqual([(r["id"], r["origin"], r["seq"], r["status"]) for r in received],
                         [(env["id"], "team-a", 1, "held")])
        # a second pull finds nothing new
        self.assertIn("nothing new", self.pull().stdout)

    def test_a_replayed_envelope_is_refused(self) -> None:
        self.pack()
        self.send("--yes")
        self.pull()
        line = git("--git-dir", str(self.mailbox), "show", f"{self.ref()}:envelope.json") + "\n"
        self.push_raw({"envelope.json": line.encode()}, parent=self.ref())
        done = self.pull()
        self.assertEqual(done.returncode, 1)
        self.assertIn("held before", done.stdout)
        self.assertEqual(len(self.held()), 1)
        refused = [e["data"] for e in self.events(self.b) if e["kind"] == "a2a.refused"]
        self.assertEqual(refused, [{"origin": "team-a", "code": "invalid", "count": 1}])

    def test_an_oversized_blob_is_refused_before_it_is_read(self) -> None:
        self.push_raw({"envelope.json": b"x" * (64 * 1024 + 1)})
        done = self.pull()
        self.assertEqual(done.returncode, 1)
        self.assertIn("too_large", done.stdout)
        self.assertEqual(self.held(), [])
        self.assertEqual([e["data"]["code"] for e in self.events(self.b) if e["kind"] == "a2a.refused"], ["too_large"])

    def test_a_tree_with_other_files_or_a_merge_is_refused(self) -> None:
        self.pack()
        self.send("--yes")
        good = git("--git-dir", str(self.mailbox), "show", f"{self.ref()}:envelope.json").encode() + b"\n"
        self.push_raw({"envelope.json": good, "hooks.sh": b"#!/bin/sh\n"}, parent=self.ref())
        done = self.pull()
        self.assertIn("tree", done.stdout)
        self.assertEqual(len(self.held()), 1)  # the first, good commit
        work = self.tmp / "raw.git"
        tree = git("--git-dir", str(work), "mktree", data=b"")
        tip = self.ref()
        git("--git-dir", str(work), "fetch", "-q", str(self.mailbox), "refs/wouf-a2a/team-a")
        merge = git("--git-dir", str(work), "-c", "user.name=x", "-c", "user.email=x@example.org", "commit-tree",
                    tree, "-p", tip, "-p", f"{tip}^", "-m", "merge")
        git("--git-dir", str(work), "push", "-q", str(self.mailbox), f"{merge}:refs/wouf-a2a/team-a")
        self.assertIn("not_linear", self.pull().stdout)

    def test_a_forged_signature_is_refused_and_nothing_is_held(self) -> None:
        env = self.pack()
        other = Team(self.tmp, "mallory", "team-b", signer="team-a")
        env["body"]["text"] = "# changed\n"
        env["sig"] = helper_module().sign(env, str(other.key))
        self.push_raw({"envelope.json": (json.dumps(env) + "\n").encode()})
        done = self.pull()
        self.assertEqual(done.returncode, 1)
        self.assertEqual(self.held(), [])
        self.assertNotIn("changed", done.stdout + done.stderr)

    def test_a_rewound_ref_is_refused_and_the_old_tip_stays_seen(self) -> None:
        self.pack()
        self.send("--yes")
        self.pull()
        mirror = self.b.dir / "env" / "federation" / "mailbox.git"
        self.assertEqual(git("--git-dir", str(mirror), "config", "--get", "gc.auto"), "0")
        self.assertEqual(git("--git-dir", str(mirror), "config", "--get", "maintenance.auto"), "false")
        self.push_raw({"envelope.json": b"{}\n"}, force=True)
        done = self.pull()
        self.assertEqual(done.returncode, 1)
        self.assertIn("rewound", done.stdout)
        self.assertEqual(len(self.held()), 1)
        self.assertEqual([e["data"]["code"] for e in self.events(self.b) if e["kind"] == "a2a.refused"], ["rewound"])

    def test_a_deleted_ref_is_refused_as_rewound(self) -> None:
        self.pack()
        self.send("--yes")
        self.pull()
        git("--git-dir", str(self.mailbox), "update-ref", "-d", "refs/wouf-a2a/team-a")
        for _ in range(2):  # until a person looks, every pull refuses again
            done = self.pull()
            self.assertEqual(done.returncode, 1)
            self.assertIn("rewound: the ref is gone", done.stdout)
            self.assertNotIn("nothing new", done.stdout)
        self.assertEqual([e["data"] for e in self.events(self.b) if e["kind"] == "a2a.refused"],
                         [{"origin": "team-a", "code": "rewound", "count": 1}] * 2)

    def test_seq_must_rise_across_the_commits_of_one_pull(self) -> None:
        first, second = self.pack("a.md"), self.pack("b.md")
        tip = self.push_raw({"envelope.json": (json.dumps(second) + "\n").encode()})
        self.push_raw({"envelope.json": (json.dumps(first) + "\n").encode()}, parent=tip)
        done = self.pull()
        self.assertEqual(done.returncode, 1)
        self.assertIn("seq 1 is not after 2", done.stdout)
        self.assertEqual(self.held(), [second["id"]])

    def test_a_merge_anywhere_in_the_range_refuses_the_whole_range(self) -> None:
        self.pack()
        self.send("--yes")
        self.pull()
        base = self.ref()
        good = self.pack("b.md")
        self.push_raw({"envelope.json": (json.dumps(good) + "\n").encode()}, parent=base)
        work = self.tmp / "raw.git"
        tree = git("--git-dir", str(work), "mktree", data=b"")
        git("--git-dir", str(work), "fetch", "-q", str(self.mailbox), "refs/wouf-a2a/team-a")
        merge = git("--git-dir", str(work), "-c", "user.name=x", "-c", "user.email=x@example.org", "commit-tree",
                    tree, "-p", self.ref(), "-p", base, "-m", "merge")
        git("--git-dir", str(work), "push", "-q", str(self.mailbox), f"{merge}:refs/wouf-a2a/team-a")
        done = self.pull()
        self.assertEqual(done.returncode, 1)
        self.assertIn("not_linear", done.stdout)
        self.assertNotIn(good["id"], self.held())  # validly signed, but in a range with a merge
        self.assertEqual([e["data"] for e in self.events(self.b) if e["kind"] == "a2a.refused"],
                         [{"origin": "team-a", "code": "not_linear", "count": 2}])

    def test_a_pull_stopped_halfway_finishes_on_the_next_run(self) -> None:
        env = self.pack()
        self.send("--yes")
        mirror = self.b.dir / "env" / "federation" / "mailbox.git"
        quarantined = self.b.dir / "import" / "federation" / "team-a" / f"{env['id']}.json"
        self.assertEqual(self.pull().returncode, 0)
        # stopped before refs/seen moved, once after the envelope was stored and once before
        for stored in (True, False):
            with self.subTest(stored=stored):
                git("--git-dir", str(mirror), "update-ref", "-d", "refs/seen/team-a")
                if not stored:
                    quarantined.unlink()
                done = self.pull()
                self.assertEqual(done.returncode, 0, done.stdout)
                self.assertNotIn("held before", done.stdout)
                self.assertTrue(quarantined.is_file())
        self.assertEqual([e for e in self.events(self.b) if e["kind"] == "a2a.refused"], [])

    def test_a_blank_envelope_is_refused(self) -> None:
        tip = self.push_raw({"envelope.json": b"\n"})
        self.push_raw({"envelope.json": b"  \t \n"}, parent=tip)
        done = self.pull()
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout.count("invalid: blank"), 2)
        self.assertEqual([e["data"] for e in self.events(self.b) if e["kind"] == "a2a.refused"],
                         [{"origin": "team-a", "code": "invalid", "count": 2}])

    def test_a_ref_that_is_no_commit_is_refused_and_seen_stays(self) -> None:
        blob = git("--git-dir", str(self.mailbox), "hash-object", "-w", "--stdin", data=b"{}\n")
        git("--git-dir", str(self.mailbox), "update-ref", "refs/wouf-a2a/team-a", blob)
        mirror = self.b.dir / "env" / "federation" / "mailbox.git"
        for _ in range(2):
            done = self.pull()
            self.assertEqual(done.returncode, 1)
            self.assertIn("does not point to a commit", done.stdout)
            self.assertNotIn("rewound", done.stdout)
        self.assertEqual(subprocess.run(["git", "--git-dir", str(mirror), "rev-parse", "--verify", "-q",
                                         "refs/seen/team-a"], capture_output=True).stdout, b"")

    def test_a_long_range_is_read_in_parts(self) -> None:
        work = self.tmp / "raw.git"
        git("init", "-q", "--bare", str(work))
        blob = git("--git-dir", str(work), "hash-object", "-w", "--stdin", data=b"{}\n")
        tree = git("--git-dir", str(work), "mktree", data=f"100644 blob {blob}\tenvelope.json\n".encode())
        head = ""
        for n in range(101):
            head = git("--git-dir", str(work), "-c", "user.name=x", "-c", "user.email=x@example.org", "commit-tree",
                       tree, *(["-p", head] if head else []), "-m", f"raw {n}")
        git("--git-dir", str(work), "push", "-q", str(self.mailbox), f"{head}:refs/wouf-a2a/team-a")
        first = self.pull()
        self.assertIn("0 held, 100 refused (more next pull)", first.stdout)
        second = self.pull()
        self.assertIn("0 held, 1 refused", second.stdout)
        self.assertNotIn("more next pull", second.stdout)
        self.assertIn("nothing new", self.pull().stdout)

    def test_a_path_or_file_mailbox_is_refused_and_its_hooks_never_run(self) -> None:
        marker = self.tmp / "hook-ran"
        hook = self.mailbox / "hooks" / "pre-receive"
        hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
        hook.chmod(0o755)
        self.pack()
        for value in (str(self.mailbox), f"file://{self.mailbox}", "./wouf-a2a.git"):
            with self.subTest(value=value):
                for t, command in ((self.a, ("send", str(self.outbox()), "--yes")), (self.b, ("pull",))):
                    config = t.dir / "config.ini"
                    config.write_text(config.read_text().rsplit("mailbox = ", 1)[0] + f"mailbox = {value}\n")
                    done = t.run(*command)
                    self.assertEqual(done.returncode, 2)
                    self.assertIn("never a path or file://", done.stderr)
        self.assertFalse(marker.exists())
        self.assertEqual(self.ref(), "")

    def test_only_ssh_and_https_mailboxes_are_taken(self) -> None:
        rule = helper_module().NETWORK_URL
        for url in ("git@github.com:owner/mailbox.git", "ssh://git@mailbox.example/srv/a2a.git",
                    "ssh://git@[::1]:2222/srv/a2a.git", "https://mailbox.example/a2a.git"):
            self.assertTrue(rule.match(url), url)
        for url in ("/srv/a2a.git", "file:///srv/a2a.git", "./a2a.git", "C:\\a2a.git", "c:/a2a.git",
                    "git://mailbox.example/a2a.git", "http://mailbox.example/a2a.git", "ext::sh -c x",
                    "ssh://-oProxyCommand=x/a", "git@host::x", "fd::3", "--upload-pack=x"):
            self.assertFalse(rule.match(url), url)

    def test_pull_runs_no_git_config_or_hook_of_this_machine_or_the_mailbox(self) -> None:
        marker = self.tmp / "ran"
        hook = self.tmp / "hook.sh"
        hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
        hook.chmod(0o755)
        home = self.tmp / "home"
        home.mkdir()
        (home / ".gitconfig").write_text(f"[core]\n\tfsmonitor = {hook}\n\thooksPath = {self.tmp}\n")
        (self.tmp / "post-checkout").write_text(hook.read_text())
        (self.mailbox / "hooks" / "post-update").write_text("#!/bin/sh\nexit 0\n")
        self.pack()
        self.send("--yes")
        self.b.env.update(HOME=str(home), GIT_CONFIG_GLOBAL=str(home / ".gitconfig"), GIT_DIR=str(self.tmp))
        done = self.pull()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(len(self.held()), 1)
        self.assertFalse(marker.exists())
        mirror = self.b.dir / "env" / "federation" / "mailbox.git"
        self.assertFalse((mirror / "hooks").exists())
        self.assertFalse((self.b.dir / "env" / "federation" / "mailbox.git" / "HEAD").is_symlink())

    def test_without_a_mailbox_or_with_a_command_url_nothing_runs(self) -> None:
        for value, says in (("", "set [federation] mailbox"), ("ext::sh -c touch% /tmp/x", "must be an ssh://"),
                            ("--upload-pack=touch /tmp/x", "must be an ssh://")):
            with self.subTest(value=value):
                config = self.b.dir / "config.ini"
                text = config.read_text().rsplit("mailbox = ", 1)[0]
                config.write_text(text + f"mailbox = {value}\n")
                done = self.pull()
                self.assertEqual(done.returncode, 2)
                self.assertIn(says, done.stderr)


if __name__ == "__main__":
    unittest.main()
