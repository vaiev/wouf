"""Tests for showcase.py: `companion.py showcase <task-or-epic> -o DIR` exports one finished task or epic as
showcase.json (wouf.showcase.v1) and showcase.html, from `team snapshot` and `team events` only. The fixture team holds
planted secrets, home and workspace paths, e-mail addresses, ids, issue refs and private addresses; none may reach
either file."""
import json, os, re, shutil, subprocess, sys, tempfile, unittest
import unittest.mock
from pathlib import Path
D = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, D)
import companion, showcase

ROOT = Path(D).parents[2]
PACK = ROOT / ".agents"
ORIGIN = "a1b2c3d4e5f6"
# Synthetic values built from parts, so this file holds no contiguous key; none of them works anywhere.
SLACK = "xox" "b-1234567890-" + "abcdefghijklmnop"
GITHUB = "gh" "p_" + "A1b2" * 9
USER, ALIAS, HOST = "zorbatest", "buildbox-alias", "lab-host-17"
PRIVATE_IP = "10." + "1.2.3"  # in parts, so the repository's own leak scan finds no address here
PRIVATE_IP_2 = "10." + "0.0.1"
REGISTERED_VALUE, GETPASS = "s3cr3tvalue-xyz", "quillmoth"
ISSUE_REFS = ("github.com/acme/widget#12", "gitlab.example.com/grp/sub/proj#7", "acme.github.io/widget/x#45")
CONFIG = f"""[team]
name = Werk
autonomy = ask
runner = auto

[environment]
mode = host
docker_host = ssh://{USER}@{ALIAS}:2222
"""
BOTS = (("worker", "worker", "strong"), ("review", "review", "strong"))
# Planted values, each in a field of its own (the snapshot caps a field at 200 characters).
PLANTED = ((f"uid=1000({USER}) gid=1000 via ssh://deploy@{ALIAS}/srv", f"key {SLACK} and {GITHUB}; fixes #123"),
           (f"notes in /home/{USER}/private notes/today.md", "ran in /tmp/wouf-x1/src/a.py and /bot/tasks/T-7-1/b.py"),
           (f"on {PRIVATE_IP} and fd12:3456:789a::1", "Signed-off-by: Jane Doe <jane.doe@corp.example>"))
LEAKS = (SLACK, GITHUB, "Jane", "jane.doe", "corp.example", "#123", PRIVATE_IP, "fd12:3456", "uid=1000", "gid=1000",
         USER, ALIAS, HOST, "/tmp/", "/bot/tasks", "/home/", "private notes", "wouf-x1", "deploy@", "never shown")


def run_data(bot, outcome, **more):
    return {"bot": bot, "runner": "claude", "tier": "strong", "model": "default", "effort": "high", "outcome": outcome,
            **more}


# Epic T-7: two tasks whose worker runs overlap (10:00-10:30 and 10:05-10:35), a gate and a review each, then done.
# T-8 is another task: nothing of it may reach the export.
FIXTURE = (
    ("09:59", "task.created", "lead", "T-7-1", {"title": "Parser for ops@example.org", "class": "complex",
                                                "owner": "worker"}),
    ("09:59", "task.created", "lead", "T-7-2", {"title": "Docs", "class": "standard", "owner": "worker"}),
    ("09:59", "task.created", "lead", "T-8", {"title": "never shown", "class": "standard", "owner": "worker"}),
    ("09:59", "claim.set", "lead", "T-7-1", {"id": "C-1", "owner": "worker", "paths": ["src/parser.py"]}),
    ("10:00", "run.started", "lead", "T-7-1", run_data("worker", "started", ref="0123456")),
    ("10:05", "run.started", "lead", "T-7-2", run_data("worker", "started")),
    ("10:10", "run.started", "lead", "T-8", run_data("worker", "started")),
    ("10:30", "run.finished", "lead", "T-7-1", run_data("worker", "done", ref="task/T-7-1",
                                                        tokens="100/2000/0/300")),
    ("10:31", "flow.step", "lead", "T-7-1", {"kind": "gate", "status": "green"}),
    ("10:35", "run.finished", "lead", "T-7-2", run_data("worker", "done", ref="task/T-7-2")),
    ("10:36", "flow.step", "lead", "T-7-2", {"kind": "gate", "status": "red"}),
    ("10:37", "flow.step", "lead", "T-7-2", {"kind": "gate", "status": "green"}),
    ("10:40", "run.started", "lead", "T-7-1", run_data("review", "started")),
    ("10:41", "run.started", "lead", "T-7-2", run_data("review", "started")),
    *(("10:45", "decision.recorded", "lead", "T-7-1", {"number": n, "title": title, "options": [], "why": why,
                                                       "risk": "low", "authority": "lead", "next": "-",
                                                       "status": "decided"})
      for n, (title, why) in enumerate(PLANTED, 1)),
    ("10:50", "run.finished", "lead", "T-7-1", run_data("review", "done", verdict="approve")),
    ("10:51", "run.finished", "lead", "T-7-2", run_data("review", "done", verdict="approve")),
    ("10:52", "question.asked", "worker", "T-8", {"id": "Q-1", "text": "never shown", "from": "worker"}),
    ("10:55", "task.state", "lead", "T-7-1", {"state": "done", "owner": "worker"}),
    ("10:56", "task.state", "lead", "T-7-2", {"state": "done", "owner": "worker"}),
)


class Team:
    """A team folder of its own (config, bots, events, receipts) and a home folder; events written as bin/log does."""

    def __init__(self, config=CONFIG):
        self.tmp = Path(tempfile.mkdtemp())
        self.dir, self.home, self.out = self.tmp / "team", self.tmp / "home", self.tmp / "out"
        for folder in (self.dir / "bots", self.dir / "events", self.dir / "logs", self.home):
            folder.mkdir(parents=True)
        (self.dir / "config.ini").write_text(config)
        (self.dir / "board.md").write_text("| T-99 | only on the board | worker | todo | - | - | - | - |\n")
        for member, role, tier in BOTS:
            (self.dir / "bots" / f"{member}.md").write_text(
                f"---\nid: {member}\nname: {member.title()}\nrole: {role}\nstatus: active\nrunner: auto\n"
                f"tier: {tier}\nconnectors: none\n---\n")
        caller = {k: v for k, v in os.environ.items() if not k.startswith(("WOUF_", "GIT_"))}
        self.env = {**caller, "WOUF_TEAM_DIR": str(self.dir), "HOME": str(self.home), "USER": USER, "LOGNAME": USER}
        self.seq = 0

    def event(self, at, kind, actor, task, data):
        self.seq += 1
        line = {"v": 1, "id": f"{ORIGIN}-{self.seq}", "seq": self.seq, "t": f"2026-10-29T{at}:00Z", "origin": ORIGIN,
                "actor": actor, "kind": kind, "task": task, "scope": "local", "data": data}
        with (self.dir / "events" / f"{ORIGIN}.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=True, separators=(",", ":")) + "\n")

    def receipt(self, task, cost):
        with (self.dir / "logs" / "worker.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"t": "2026-10-29T10:30:00.000+00:00", "who": "worker", "task": task,
                                     "outcome": "done", "signal": "ok", "cost": cost}) + "\n")

    def fill(self, rows=FIXTURE):
        for row in rows: self.event(*row)
        self.receipt("T-7-1", "$0.25")
        self.receipt("T-7-2", "0.15")

    def cli(self, *args, env=None):
        """companion.py showcase as the owner runs it, from the home folder."""
        return subprocess.run([sys.executable, str(Path(D) / "companion.py"), "showcase", *args, "--team",
                               str(self.dir), "--pack", str(PACK)], env=env or self.env, cwd=self.home,
                              capture_output=True, text=True, timeout=120)


class ShowcaseTest(unittest.TestCase):
    def setUp(self):
        self.t = Team()
        self.addCleanup(shutil.rmtree, self.t.tmp)
        self.t.fill()

    def export(self, target="T-7", *more):
        done = self.t.cli(target, "-o", str(self.t.out), *more)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return json.loads((self.t.out / "showcase.json").read_text(encoding="utf-8")), done

    def test_writes_the_two_files_and_nothing_else(self):
        self.export()
        self.assertEqual(sorted(p.name for p in self.t.out.iterdir()), ["showcase.html", "showcase.json"])

    def test_the_manifest_of_an_epic(self):
        data, _ = self.export()
        m = data["manifest"]
        self.assertEqual(m["v"], "wouf.showcase.v1")
        self.assertEqual(m["task_id"], "T-7")
        self.assertEqual(m["started_at"], "2026-10-29T09:59:00Z")
        self.assertEqual(m["completed_at"], "2026-10-29T10:56:00Z")
        self.assertEqual(m["wall_duration_s"], 57 * 60)
        self.assertEqual(m["total_cost"], "0.40")
        self.assertEqual(m["final_verdict"], "approve")
        self.assertEqual(data["tasks"], [{"id": "T-7-1", "title": "Parser for [email]", "class": "complex",
                                          "status": "done"},
                                         {"id": "T-7-2", "title": "Docs", "class": "standard", "status": "done"}])

    def test_time_saved_is_the_serial_step_time_minus_the_wall_time(self):
        data, _ = self.export()
        serial = (30 + 30 + 10 + 10) * 60  # two worker runs and two reviews, of which the worker runs overlapped
        self.assertEqual(sum(s["duration_s"] or 0 for s in data["steps"]), serial)
        self.assertEqual(data["time_saved_s"], serial - 57 * 60)

    def test_time_saved_is_never_below_zero(self):
        self.assertEqual(showcase.time_saved([{"duration_s": 60}], 3600), 0)

    def test_steps_gates_and_reviews_come_from_the_events_of_the_selection(self):
        data, _ = self.export()
        self.assertEqual({s["task"] for s in data["steps"]}, {"T-7-1", "T-7-2"})
        self.assertEqual([s["seq"] for s in data["steps"]], sorted(s["seq"] for s in data["steps"]))
        self.assertEqual([(g["task"], g["status"]) for g in data["gates"]],
                         [("T-7-1", "green"), ("T-7-2", "red"), ("T-7-2", "green")])
        self.assertEqual([(r["task"], r["bot"], r["verdict"], r["round"]) for r in data["reviews"]],
                         [("T-7-1", "review", "approve", 1), ("T-7-2", "review", "approve", 1)])
        worker = next(s for s in data["steps"] if s["kind"] == "run.finished" and s["task"] == "T-7-1")
        self.assertEqual((worker["bot"], worker["outcome"], worker["duration_s"]), ("worker", "done", 1800))
        self.assertEqual(data["artifacts"], {"tasks": 2, "paths": ["src/parser.py"],
                                             "branches": ["task/T-7-1", "task/T-7-2"], "tokens": "100/2000/0/300"})

    def test_one_task_alone(self):
        data, _ = self.export("T-7-2")
        self.assertEqual(data["manifest"]["task_id"], "T-7-2")
        self.assertEqual(data["manifest"]["title"], "Docs")
        self.assertEqual(data["manifest"]["class"], "standard")
        self.assertEqual({s["task"] for s in data["steps"]}, {"T-7-2"})

    def test_objective_and_criteria_come_from_a_brief_the_owner_names(self):
        brief = self.t.tmp / "brief.md"
        brief.write_text("Task: T-7\nObjective: Parse the config, see /home/zorbatest/x and ops@example.org\n"
                         "Acceptance criteria:\n- parses every key\n- reports bad lines\nScope: the parser\n")
        data, _ = self.export("T-7", "--brief", str(brief), "--title", "A parser in an hour")
        m = data["manifest"]
        self.assertEqual(m["title"], "A parser in an hour")
        self.assertEqual(m["objective"], "Parse the config, see [path]")
        self.assertEqual(m["acceptance_criteria"], ["parses every key", "reports bad lines"])

    def test_the_brief_gets_the_snapshots_masking_and_more_in_both_files(self):
        # Review T-0019-22 r1 (Claude r3, Codex r4): each shape that came through --brief, in both output files.
        brief = self.t.tmp / "brief.md"
        brief.write_text(f"Objective: run in /bot/tasks/T-1/home/janedoe/x on {ALIAS}, pw {REGISTERED_VALUE}\n"
                         "Acceptance criteria:\n"
                         "- private path /tmp/Users/Alice Private/work.txt\n"
                         f"- docker_host={ALIAS} and git@{ALIAS}:infra/repo\n"
                         "- fixes acme/widget#12 and widget#45, not &#38;\n"
                         f"- reach admin@{PRIVATE_IP_2} as uid 1000, --user 1000:1000\n"
                         f"- whoami says {GETPASS}\n")
        env = {**self.t.env, "WOUF_MASK": json.dumps([REGISTERED_VALUE]), "LOGNAME": GETPASS}
        done = self.t.cli("T-7", "-o", str(self.t.out), "--brief", str(brief), env=env)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        for name in ("showcase.json", "showcase.html"):
            text = (self.t.out / name).read_text(encoding="utf-8")
            for leak in ("janedoe", ALIAS, REGISTERED_VALUE, "Alice", "Private", "work.txt", "infra/repo",
                         "widget#", "#12", "#45", PRIVATE_IP_2, "uid 1000", "1000:1000", GETPASS):
                self.assertNotIn(leak, text, f"{name} holds {leak!r}")
        m = json.loads((self.t.out / "showcase.json").read_text(encoding="utf-8"))["manifest"]
        self.assertEqual(m["objective"], "run in [path]")
        self.assertEqual(m["acceptance_criteria"][0], "private path [path]")
        self.assertIn("[ref] and [ref], not &#38;", m["acceptance_criteria"][2])
        self.assertEqual(m["acceptance_criteria"][3], "reach [email] as [id], [id]")

    def test_an_issue_ref_after_any_prefix_is_dropped_from_the_brief_and_the_snapshot(self):
        # Review T-0019-22 r2 (Claude r7): a host or nested-group prefix; any token that ends in #<digits>.
        for n, ref in enumerate(ISSUE_REFS, 10):
            self.t.event("10:46", "decision.recorded", "lead", "T-7-2", {"number": n, "title": f"fixes {ref}",
                         "options": [], "why": f"see ({ref})", "risk": "low", "authority": "lead", "next": "-",
                         "status": "decided"})
        brief = self.t.tmp / "brief.md"
        brief.write_text("Objective: close " + " and ".join(ISSUE_REFS) + ", not &#38;\n")
        done = self.t.cli("T-7", "-o", str(self.t.out), "--brief", str(brief))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        for name in ("showcase.json", "showcase.html"):
            text = (self.t.out / name).read_text(encoding="utf-8")
            for leak in ("github.com", "gitlab.example.com", "grp/sub", "github.io", "#12", "#7", "#45"):
                self.assertNotIn(leak, text, f"{name} holds {leak!r}")
        m = json.loads((self.t.out / "showcase.json").read_text(encoding="utf-8"))["manifest"]
        self.assertEqual(m["objective"], "close [ref] and [ref] and [ref], not &#38;")
        for ref in ISSUE_REFS:
            self.assertEqual(showcase.scrub(f"({ref})", {}), "([ref])")
            self.assertIn("an issue ref", showcase.residual(ref, {}))

    def test_a_short_configured_alias_is_masked_in_both_files(self):
        # Review T-0019-22 r2 (Codex r8): a configured alias of any length, generic word or not.
        for alias in ("db", "node"):  # short, and a generic word
            t = Team(CONFIG.replace(f"{USER}@{ALIAS}", f"{USER}@{alias}"))
            self.addCleanup(shutil.rmtree, t.tmp)
            t.fill()
            brief = t.tmp / "brief.md"
            brief.write_text(f"Objective: deploy with docker_host={alias} on {alias}_backup\n")
            done = t.cli("T-7", "-o", str(t.out), "--brief", str(brief))
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            for name in ("showcase.json", "showcase.html"):
                self.assertNotRegex((t.out / name).read_text(encoding="utf-8"),
                                    rf"(?<![A-Za-z0-9]){alias}(?![A-Za-z0-9])", f"{name} keeps {alias!r}")
            m = json.loads((t.out / "showcase.json").read_text(encoding="utf-8"))["manifest"]
            self.assertEqual(m["objective"], "deploy with docker_host=<host> on <host>_backup")
            self.assertIn("a local name", showcase.residual(f"on {alias}", {}, {"configured": [alias]}))
        # Names only guessed from the environment keep the filter: a generic word stays a word.
        self.assertEqual(showcase.scrub("the lead bot", {"USER": "lead"}), "the lead bot")

    def test_the_pack_is_asked_without_the_current_folder_on_its_path(self):
        planted = self.t.tmp / "cwd"
        planted.mkdir()
        (planted / "json.py").write_text("raise SystemExit(7)\n")
        here = os.getcwd()
        os.chdir(planted)
        self.addCleanup(os.chdir, here)
        companion.pack_log(self.t.dir, PACK)
        known = showcase.pack_names(companion._BIN[0] / "team", {**self.t.env, "WOUF_TEAM_DIR": str(self.t.dir)})
        self.assertEqual(known["configured"], [USER, ALIAS])

    def test_more_shapes_from_the_review_notes(self):
        for text, clean in (("uid:1000 ok", "[id] ok"), ("mail jane@localhost now", "mail [host] now"),
                            ("Co-authored-by: Jane Doe <j@x.example>", "[signed-off]"),
                            ("on fe80::1%eth0 now", "on [ip] now")):
            self.assertEqual(showcase.scrub(text, {}), clean)
            self.assertEqual(showcase.residual(clean, {}), "")
            self.assertNotEqual(showcase.residual(text, {}), "", text)

    def test_the_residual_check_knows_registered_values_aliases_and_home_paths_behind_a_mask(self):
        names = {"users": [GETPASS], "hosts": [ALIAS], "configured": []}
        with unittest.mock.patch.object(showcase, "registered", lambda env: [REGISTERED_VALUE]):
            for text, kind in ((f"pw {REGISTERED_VALUE}", "a registered value"), (f"on {ALIAS}", "a local name"),
                               (f"by {GETPASS}", "a local name"), ("<workspace>/home/janedoe/x", "a private path"),
                               ("<repo>/x/Users/Alice", "a private path"), ("see widget#45", "an issue ref")):
                self.assertIn(kind, showcase.residual(text, {}, names), text)
            self.assertEqual(showcase.residual("<workspace>/src/a.py and &#38;", {}, names), "")

    def test_the_residual_check_covers_the_final_html_before_either_file_is_written(self):
        real = showcase.page

        def leaky(data, title):
            return real(data, title + " ops@example.org").replace("worker on claude", "worker on 10." + "9.9.9")
        with unittest.mock.patch.object(showcase, "page", leaky):
            with self.assertRaises(SystemExit) as stop:
                showcase.export("T-7", self.t.dir, self.t.out, env=self.t.env, pack=PACK)
        self.assertEqual(stop.exception.code, 1)
        self.assertFalse(self.t.out.exists())
        message = showcase.LAST_REFUSAL[0]
        self.assertIn("showcase.html <title> (an e-mail address)", message)
        self.assertRegex(message, r"showcase.html data steps\[\d+\]\.what \(an IP address\)")
        self.assertNotIn("ops@", message)
        self.assertNotIn("9.9.9", message)

    def test_an_address_at_an_ip_is_one_e_mail_not_a_false_refusal(self):
        self.assertEqual(showcase.scrub("mail admin@" + PRIVATE_IP_2, {}), "mail [email]")

    def test_a_failed_second_write_leaves_no_file(self):
        real = showcase.write

        def second_fails(folder, name, text):
            if name == "showcase.html": showcase.refuse("cannot write showcase.html (test)")
            real(folder, name, text)
        with unittest.mock.patch.object(showcase, "write", second_fails):
            with self.assertRaises(SystemExit):
                showcase.export("T-7", self.t.dir, self.t.out, env=self.t.env, pack=PACK)
        self.assertEqual(list(self.t.out.iterdir()), [])

    def test_without_a_brief_objective_is_null(self):
        data, _ = self.export()
        self.assertIsNone(data["manifest"]["objective"])
        self.assertEqual(data["manifest"]["acceptance_criteria"], [])

    def test_no_planted_value_reaches_either_file(self):
        data, _ = self.export()
        for name in ("showcase.json", "showcase.html"):
            text = (self.t.out / name).read_text(encoding="utf-8")
            for leak in LEAKS:
                self.assertNotIn(leak, text, f"{name} holds {leak!r}")
        self.assertEqual(len([s for s in data["steps"] if s["kind"] == "decision.recorded"]), len(PLANTED))

    def test_each_planted_kind_is_dropped_even_when_the_snapshot_let_it_pass(self):
        # The snapshot masks keys, paths and this computer's names; the rest is this exporter's own drop list.
        for planted in [text for pair in PLANTED for text in pair]:
            clean = showcase.scrub(planted, self.t.env)
            for leak in LEAKS:
                self.assertNotIn(leak, clean, f"{planted!r} keeps {leak!r}")
            self.assertEqual(showcase.residual(clean, self.t.env), "", clean)
        self.assertEqual(showcase.scrub("ran in /tmp/wouf-x1/src/a.py", {}), "ran in <workspace>/src/a.py")
        self.assertEqual(showcase.scrub("at /bot/tasks/T-7-1/repo/b.py", {}), "at <workspace>/repo/b.py")
        self.assertEqual(showcase.scrub("ok. Signed-off-by: Jane <j@x.example>", {}), "ok. [signed-off]")

    def test_a_local_host_name_is_hidden(self):
        with unittest.mock.patch("socket.gethostname", return_value=HOST):
            self.assertEqual(showcase.scrub(f"built on {HOST}", self.t.env), "built on <host>")

    def test_scrub_keeps_the_repo_marker_and_its_relative_path(self):
        self.assertEqual(showcase.scrub("see <repo>/src/a.py and /etc/passwd", {}), "see <repo>/src/a.py and [path]")
        self.assertNotIn("bob", showcase.scrub("<repo>/../home/bob/x", {}))
        self.assertEqual(showcase.scrub("<team>/flows and <workspace>/src", {}), "<team>/flows and <workspace>/src")
        self.assertEqual(showcase.scrub("a\u202eb\x07c\ue000d", {}), "a b cd")  # bidi and C0 controls go, the sentinel is never input

    def test_an_unfinished_task_is_refused(self):
        self.t.event("11:00", "task.state", "lead", "T-7-2", {"state": "working", "owner": "worker"})
        done = self.t.cli("T-7", "-o", str(self.t.out))
        self.assertEqual(done.returncode, 1)
        self.assertIn("T-7-2", done.stderr)
        self.assertFalse(self.t.out.exists())

    def test_an_unknown_task_is_refused(self):
        done = self.t.cli("T-9", "-o", str(self.t.out))
        self.assertEqual(done.returncode, 1)
        self.assertIn("no task", done.stderr)

    def test_a_bad_task_id_is_refused_before_anything_runs(self):
        done = self.t.cli("--x", "-o", str(self.t.out))
        self.assertNotEqual(done.returncode, 0)
        done = self.t.cli("T-7/../x", "-o", str(self.t.out))
        self.assertEqual(done.returncode, 1)
        self.assertFalse(self.t.out.exists())

    def test_the_residual_check_writes_nothing_and_names_the_field_not_the_value(self):
        with unittest.mock.patch.object(showcase, "scrub", lambda text, env, known=None: text):
            with self.assertRaises(SystemExit) as stop:
                showcase.export("T-7", self.t.dir, self.t.out, env=self.t.env, pack=PACK)
        self.assertEqual(stop.exception.code, 1)
        self.assertFalse(self.t.out.exists())
        message = showcase.LAST_REFUSAL[0]
        self.assertRegex(message, r"steps\[\d+\]\.what")
        for leak in LEAKS:
            self.assertNotIn(leak, message)

    def test_the_residual_check_runs_on_the_final_bytes(self):
        found = showcase.residual_fields(json.dumps({"steps": [{"what": "mail ops@example.org"}], "v": "ok"}), {})
        self.assertEqual(found, ["steps[0].what (an e-mail address)"])
        self.assertEqual(showcase.residual_fields(json.dumps({"what": "fixes #12"}), {}), ["what (an issue ref)"])
        self.assertEqual(showcase.residual_fields(json.dumps({"what": "<repo>/src/a.py"}), {}), [])
        # A key is the schema's word: one outside it is refused and named by where it sits, never by itself.
        self.assertEqual(showcase.residual_fields(json.dumps({"steps": [{"ops@example.org": 1}]}), {}),
                         ["steps[0]: a key outside the schema"])

    def test_a_link_in_the_output_folder_is_not_followed(self):
        self.t.out.mkdir()
        target = self.t.tmp / "elsewhere.json"
        target.write_text("keep")
        (self.t.out / "showcase.json").symlink_to(target)
        done = self.t.cli("T-7", "-o", str(self.t.out))
        self.assertEqual(done.returncode, 1)
        self.assertEqual(target.read_text(), "keep")

    def test_the_page_is_self_contained_under_a_strict_policy(self):
        data, _ = self.export()
        page = (self.t.out / "showcase.html").read_text(encoding="utf-8")
        policy = re.search(r'<meta http-equiv="Content-Security-Policy" content="([^"]+)">', page).group(1)
        self.assertIn("default-src 'none'", policy)
        self.assertIn("connect-src 'none'", policy)
        self.assertNotIn("unsafe-inline", policy)
        style = re.search(r"<style>(.*?)</style>", page, re.S).group(1)
        script = re.search(r"<script>(.*?)</script>", page, re.S).group(1)
        self.assertIn(companion.digest(style), policy)
        self.assertIn(companion.digest(script), policy)
        self.assertNotRegex(page, r"(?i)(?:src|href)\s*=|https?://|@import|url\(")
        self.assertNotIn("innerHTML", script)
        blob = re.search(r'<script type="application/json" id="showcase-data">(.*?)</script>', page, re.S).group(1)
        self.assertNotIn("<", blob)
        self.assertEqual(json.loads(blob), data)

    def test_markup_in_the_data_stays_text(self):
        self.t.event("11:00", "task.created", "lead", "T-7-3", {"title": "</script><img src=x onerror=alert(1)>",
                                                                "class": "standard", "owner": "worker"})
        self.t.event("11:01", "task.state", "lead", "T-7-3", {"state": "done", "owner": "worker"})
        self.export()
        page = (self.t.out / "showcase.html").read_text(encoding="utf-8")
        self.assertEqual(page.count("</script>"), 2)
        self.assertNotIn("<img", page)

    def test_the_exporter_reads_no_file_of_the_team_folder(self):
        opened = []
        real = open

        def spy(path, *args, **kwargs):
            opened.append(str(path))
            return real(path, *args, **kwargs)
        with unittest.mock.patch("builtins.open", spy), unittest.mock.patch("os.open", wraps=os.open) as raw:
            showcase.export("T-7", self.t.dir, self.t.out, env=self.t.env, pack=PACK)
        touched = opened + [str(c.args[0]) for c in raw.call_args_list]
        self.assertFalse([p for p in touched if p.startswith(str(self.t.dir))], touched)


# T-0052: an epic whose runs carry their own ids, as a real store holds them: a review round T.r<n>, a fix round
# T-fix<n>, a scout run T.scout. Each belongs to its package T. The docker_host user is the product's own name.
EPIC_CONFIG = CONFIG.replace(f"{USER}@{ALIAS}", f"wouf@{ALIAS}")
EPIC = (
    ("09:00", "task.created", "lead", "T-9-1", {"title": "Parser", "class": "complex", "owner": "worker"}),
    ("09:00", "task.created", "lead", "T-9-2", {"title": "Docs", "class": "standard", "owner": "worker"}),
    ("09:01", "run.started", "lead", "T-9-1.scout", run_data("scout", "started")),
    ("09:05", "run.finished", "lead", "T-9-1.scout", run_data("scout", "done")),
    ("09:10", "run.started", "lead", "T-9-1", run_data("worker", "started")),
    ("09:12", "run.started", "lead", "T-9-2", run_data("worker", "started")),
    ("09:40", "run.finished", "lead", "T-9-1", run_data("worker", "done", ref="task/T-9-1")),
    ("09:41", "flow.step", "lead", "T-9-1", {"kind": "gate", "status": "green"}),
    ("09:42", "run.finished", "lead", "T-9-2", run_data("worker", "done", ref="task/T-9-2")),
    ("09:43", "flow.step", "lead", "T-9-2", {"kind": "gate", "status": "green"}),
    ("09:45", "run.started", "lead", "T-9-1.r1", run_data("review", "started")),
    ("09:46", "run.started", "lead", "T-9-2.r1", run_data("review", "started")),
    ("09:55", "run.finished", "lead", "T-9-1.r1", run_data("review", "done", verdict="changes")),
    ("09:56", "run.finished", "lead", "T-9-2.r1", run_data("review", "done", verdict="approve")),
    ("10:00", "run.started", "lead", "T-9-1-fix1", run_data("worker", "started")),
    ("10:20", "run.finished", "lead", "T-9-1-fix1", run_data("worker", "done", ref="task/T-9-1")),
    ("10:21", "flow.step", "lead", "T-9-1-fix1", {"kind": "gate", "status": "green"}),
    ("10:22", "run.started", "lead", "T-9-1.r2", run_data("review", "started")),
    ("10:30", "run.finished", "lead", "T-9-1.r2", run_data("review", "done", verdict="approve")),
    ("10:31", "task.state", "lead", "T-9-1", {"state": "done", "owner": "worker"}),
    ("10:32", "task.state", "lead", "T-9-2", {"state": "done", "owner": "worker"}),
)


class EpicRunsTest(unittest.TestCase):
    def setUp(self):
        self.t = Team(EPIC_CONFIG)
        self.addCleanup(shutil.rmtree, self.t.tmp)
        self.t.fill(EPIC)

    def export(self, *more):
        done = self.t.cli("T-9", "-o", str(self.t.out), *more)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return json.loads((self.t.out / "showcase.json").read_text(encoding="utf-8")), done

    def test_review_fix_and_scout_runs_belong_to_their_package(self):
        data, _ = self.export()
        self.assertEqual([t["id"] for t in data["tasks"]], ["T-9-1", "T-9-2"])
        self.assertEqual([(r["task"], r["verdict"], r["round"]) for r in data["reviews"]],
                         [("T-9-1", "changes", 1), ("T-9-2", "approve", 1), ("T-9-1", "approve", 2)])
        self.assertEqual([(g["task"], g["status"]) for g in data["gates"]],
                         [("T-9-1", "green"), ("T-9-2", "green"), ("T-9-1", "green")])
        for run in ("T-9-1.scout", "T-9-1.r1", "T-9-1.r2", "T-9-1-fix1", "T-9-2.r1"):
            self.assertIn(run, {s["task"] for s in data["steps"] if s["kind"] == "run.finished"}, run)
        self.assertEqual(data["manifest"]["final_verdict"], "approve")

    def test_time_saved_counts_every_run_of_the_epic(self):
        data, _ = self.export()
        serial = (4 + 30 + 30 + 10 + 10 + 20 + 8) * 60  # scout, two workers, two reviews, the fix, the second review
        self.assertEqual(sum(s["duration_s"] or 0 for s in data["steps"]), serial)
        self.assertEqual(data["manifest"]["wall_duration_s"], 92 * 60)
        self.assertEqual(data["time_saved_s"], serial - 92 * 60)

    def test_the_schema_id_never_passes_the_masking(self):
        data, done = self.export("--title", "WOUF next")
        self.assertEqual(data["manifest"]["v"], "wouf.showcase.v1")
        page = (self.t.out / "showcase.html").read_text(encoding="utf-8")
        self.assertIn('"v": "wouf.showcase.v1"', page)
        # A name the caller gave stays masked (fail closed), and the output says so.
        self.assertEqual(data["manifest"]["title"], "<user> next")
        self.assertIn("title", done.stdout)
        self.assertIn("masked", done.stdout)
        # Everything else keeps its masking: the configured user is still hidden in a value.
        self.assertNotRegex(json.dumps(data["steps"]) + json.dumps(data["tasks"]), r"(?i)(?<![a-z0-9])wouf(?![a-z0-9])")

    def test_a_package_without_a_review_is_named_in_the_output(self):
        t = Team(EPIC_CONFIG)
        self.addCleanup(shutil.rmtree, t.tmp)
        t.fill([row for row in EPIC if not row[3].startswith("T-9-2.")])
        done = t.cli("T-9", "-o", str(t.out))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        data = json.loads((t.out / "showcase.json").read_text(encoding="utf-8"))
        self.assertIsNone(data["manifest"]["final_verdict"])
        self.assertRegex(done.stdout, r"no review verdict.*T-9-2")

    def test_an_epic_takes_class_objective_and_criteria_from_its_brief(self):
        brief = self.t.tmp / "brief.md"
        brief.write_text("Task: T-9\nClass: plan\nObjective: Parse and document\n"
                         "Acceptance criteria:\n- parses\n- documented\n")
        data, _ = self.export("--brief", str(brief))
        m = data["manifest"]
        self.assertEqual((m["class"], m["objective"], m["acceptance_criteria"]),
                         ("plan", "Parse and document", ["parses", "documented"]))

    def test_a_run_id_in_the_store_is_no_package_of_its_own(self):
        # Since T-0048 a board row such as T-9-1-fix1 can be a task in the store; open or not, it is a run of T-9-1.
        self.t.event("10:33", "task.created", "lead", "T-9-1-fix1", {"title": "fix", "class": "standard",
                                                                     "owner": "worker"})
        data, _ = self.export()
        self.assertEqual([t["id"] for t in data["tasks"]], ["T-9-1", "T-9-2"])

    def test_package_of(self):
        for run, package in (("T-9-1.r1", "T-9-1"), ("T-9-1-fix1", "T-9-1"), ("T-9-1.scout", "T-9-1"),
                             ("T-9-1.r2-fix1", "T-9-1"), ("T-9-1", "T-9-1"), ("T-1.2", "T-1.2"), ("T-0019", "T-0019")):
            self.assertEqual(showcase.package_of(run), package, run)


if __name__ == "__main__":
    unittest.main()
