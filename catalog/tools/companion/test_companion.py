import base64, hashlib, http.client, json, os, re, shutil, sqlite3, subprocess, sys, tempfile, threading, unittest
import unittest.mock
import urllib.error, urllib.request
from pathlib import Path
D = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, D)
import companion
sys.path.insert(0, str(Path(D).parents[2] / "tests"))
from support import writable_copy  # the one helper for every copy of the checkout (tests/support.py)

BOARD = """# Board

| ID | Task | Owner | Status | Next gate | Artifacts | Blocked reason | Updated |
|---|---|---|---|---|---|---|---|
| T-1 | Add the status endpoint | worker | working | review | | | 2026-10-28 |
| T-2 | Rotate a key <script>alert(1)</script> | review | review | | | | 2026-10-28 |

## Questions for the human

- Which staging URL? (T-1)
"""
SECRET = "gh" "p_" + "A1b2" * 9  # a full GitHub key shape, as the pack's SECRET_PATTERNS know it



class T(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, self.tmp)
        t = self.team = self.tmp / "team"
        for d in ("bots", "logs", "flows/T-1", "flows/T-2", "import/federation/peer-b", "env/federation/outbox"):
            (t / d).mkdir(parents=True)
        (t / "board.md").write_text(BOARD)
        for bot, status in (("worker", "active"), ("review", "active"), ("scout", "proposed")):
            (t / "bots" / f"{bot}.md").write_text(f"---\nid: {bot}\nname: {bot.title()}\nrole: {bot}\nstatus: {status}\n---\n")
        logs = [{"t": "2026-10-28T08:00:00.000+00:00", "who": "worker", "task": "T-1", "outcome": "limited",
                 "signal": "usage limit"},
                {"t": "2026-10-28T09:00:00.000+00:00", "who": "review", "task": "T-2", "outcome": "approve",
                 "signal": "token " + SECRET + " from ops@example.org"},
                {"t": "2026-10-28T07:00:00.000+00:00", "who": "lead", "task": "T-1", "outcome": "started",
                 "signal": "flow standard on wouf/T-1"}]
        for e in logs:
            with (t / "logs" / f"{e['who']}.jsonl").open("a") as h: h.write(json.dumps(e) + "\n")
        with (t / "logs" / "lead.jsonl").open("a") as h: h.write("not json\n")
        (t / "flows/T-1/brief.md").write_text("Task: T-1\nClass: standard\nPaths: src/**\nObjective: x\nDepends: none\n")
        (t / "flows/T-1/state.json").write_text(json.dumps({
            "task": "T-1", "class": "standard", "status": "limited", "branch": "wouf/T-1", "repo": "/home/someone/private/repo",
            "steps": [{"kind": "worker", "bot": "worker", "outcome": "limited", "until": "2099-01-01T09:50:00+00:00",
                       "ended": "2026-10-28T08:00:00+00:00"}]}))
        (t / "flows/T-2/brief.md").write_text("Task: T-2\nClass: security\nPaths: src/**\nObjective: y\nDepends: T-1\n")
        (t / "flows/T-2/state.json").write_text(json.dumps({
            "task": "T-2", "class": "security", "status": "ready", "branch": "wouf/T-2",
            "steps": [{"kind": "worker", "outcome": "done", "head": "a" * 40},
                      {"kind": "gate", "outcome": "green", "head": "a" * 40},
                      {"kind": "review", "outcome": "done", "verdict": "approve"}]}))
        (t / "import/federation/peer-b/" ).joinpath("0" * 32 + ".json").write_text(json.dumps({
            "id": "0" * 32, "type": "spec_sync", "from": "peer-b", "to": "me", "at": "2026-10-28T09:00:00Z",
            "body": {"name": "api.md", "text": "body is never shown"}}))
        (t / "env/federation/outbox/peer-b.jsonl").write_text('{"id":"1"}\n{"id":"2"}\n')

    def test_state_reads_board_bots_flows_logs_and_federation(self):
        s = companion.collect(self.team)
        self.assertEqual([r["ID"] for r in s["board"]], ["T-1", "T-2"])
        self.assertEqual(s["questions"], ["Which staging URL? (T-1)"])
        bots = {b["id"]: b for b in s["bots"]}
        self.assertEqual(bots["worker"]["state"], "limited")
        self.assertEqual(bots["worker"]["until"], "2099-01-01T09:50:00+00:00")
        self.assertEqual(bots["review"]["state"], "approve")
        self.assertEqual(bots["scout"]["status"], "proposed")
        flows = {f["task"]: f for f in s["flows"]}
        self.assertEqual(flows["T-2"]["phase"], "done")
        self.assertEqual(flows["T-1"]["phase"], "REALIZE")
        self.assertEqual(flows["T-2"]["depends"], ["T-1"])
        self.assertEqual(s["waves"], [["T-1"], ["T-2"]])
        self.assertEqual([e["who"] for e in s["log"]], ["lead", "worker", "review"])
        self.assertEqual(s["federation"], [{"peer": "peer-b", "sent": 2, "held": 1,
                                            "last": {"type": "spec_sync", "at": "2026-10-28T09:00:00Z"}}])

    def test_secrets_and_private_paths_never_leave(self):
        text = json.dumps(companion.collect(self.team))
        self.assertNotIn(SECRET, text)
        self.assertIn("[redacted]", text)
        self.assertNotIn("/home/someone", text)
        self.assertNotIn("body is never shown", text)
        self.assertNotIn("ops@example.org", text)

    def test_redact_uses_the_packs_patterns(self):
        log = companion.pack_log(self.team)
        self.assertTrue(hasattr(log, "SECRET_PATTERNS"))
        self.assertIsNone(log.secret_in(companion.redact("key " + SECRET)))
        self.assertNotIn("SENSITIVE_RE", Path(companion.__file__).read_text())

    def test_hub_counts_are_read_only(self):
        db = self.tmp / "hub.db"
        c = sqlite3.connect(db); c.execute("CREATE TABLE work(id INTEGER PRIMARY KEY, status TEXT)")
        c.executemany("INSERT INTO work(status) VALUES (?)", [("active",), ("done",), ("active",)]); c.commit(); c.close()
        s = companion.collect(self.team, hub=db)
        self.assertEqual(s["hub"], {"work": {"active": 2, "done": 1}})
        self.assertEqual(companion.collect(self.team, hub=self.tmp / "missing.db")["hub"], {})
        self.assertFalse((self.tmp / "missing.db").exists())

    def test_showcase_is_one_self_contained_file(self):
        out = self.tmp / "show.html"
        companion.export_showcase(self.team, out, title="Building a demo project")
        html = out.read_text()
        self.assertIn("Building a demo project", html)
        self.assertNotRegex(html, r"<script[^>]+src=|<link[^>]+href=\"http|fetch\(|EventSource")
        self.assertIn("connect-src 'none'", html)
        self.assertNotIn(SECRET, html)
        self.assertNotIn("/home/someone", html)
        # Data cannot close the script element it sits in.
        self.assertEqual(html.count("</script>"), 2)  # the data element and the app; the board's markup is escaped
        data = json.loads(re.search(r'<script type="application/json" id="data">(.*?)</script>', html, re.S)
                          .group(1).replace("<\\/", "</"))
        self.assertEqual(data["mode"], "showcase")
        self.assertIn("T-2", [f["task"] for f in data["flows"]])

    def test_showcase_refuses_to_write_what_a_residual_check_still_finds(self):
        out = self.tmp / "show.html"
        (self.team / "logs" / "review.jsonl").unlink()  # its signal has two leaks; each case below has one
        for leak, kind in (("/home/someone/x", "private path"), ("/etc/company/internal.conf", "private path"),
                           ("ops@example.org", "e-mail address"), (SECRET, "GitHub classic"),
                           ("Ab3dEf6hIj9kLm2nOp5qRs8tUv1wXy4z", "token-like word")):
            with self.subTest(kind=kind):
                with unittest.mock.patch.object(companion, "redact", lambda value: value):
                    (self.team / "board.md").write_text(BOARD.replace("Add the status endpoint", leak))
                    with self.assertRaises(SystemExit) as stop:
                        companion.export_showcase(self.team, out, title="Demo")
                self.assertIn(kind, str(stop.exception))
                self.assertNotIn(leak, str(stop.exception))
                self.assertFalse(out.exists())

    def test_the_page_renders_text_never_markup(self):
        page = companion.page({"mode": "live"})
        self.assertNotIn("innerHTML", page)
        self.assertIn("textContent", page)

    def test_server_binds_loopback_and_serves_state(self):
        server = companion.make_server(self.team, port=0)
        self.addCleanup(server.server_close)
        self.assertEqual(server.server_address[0], "127.0.0.1")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        state = json.loads(urllib.request.urlopen(base + "/api/state").read())
        self.assertEqual(state["board"][0]["ID"], "T-1")
        page = urllib.request.urlopen(base + "/").read().decode()
        self.assertIn("/api/state", page)
        self.assertIn("EventSource('/events')", page)
        for path in ("/../board.md", "/index.html", "/api/state?x"):
            with self.subTest(path=path):
                with self.assertRaises(urllib.error.HTTPError) as err:
                    urllib.request.urlopen(base + path)
                self.assertEqual(err.exception.code, 404)

    def test_events_stream_the_state_and_its_changes(self):
        server = companion.make_server(self.team, port=0)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        stream = urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/events", timeout=10)
        self.addCleanup(stream.close)
        self.assertEqual(stream.headers["Content-Type"], "text/event-stream")

        def next_state():
            event, data = None, None
            while True:
                line = stream.readline().decode().rstrip("\n")
                if line.startswith("event: "): event = line[7:]
                elif line.startswith("data: "): data = line[6:]
                elif not line and event == "state": return json.loads(data)

        self.assertEqual(next_state()["board"][0]["ID"], "T-1")
        (self.team / "board.md").write_text(BOARD.replace("| T-1 |", "| T-9 |"))
        self.assertEqual(next_state()["board"][0]["ID"], "T-9")

    def test_events_refuse_another_host(self):
        server = companion.make_server(self.team, port=0)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        request = urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}/events",
                                         headers={"Host": "evil.example"})
        with self.assertRaises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(err.exception.code, 403)

    def serve(self):
        server = companion.make_server(self.team, port=0)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        return server.server_address

    def test_every_response_carries_the_same_headers(self):
        address = self.serve()
        cases = [("GET", "/", "127.0.0.1", 200), ("GET", "/api/state", "localhost", 200),
                 ("GET", "/nothing", "localhost", 404), ("GET", "/", "evil.example", 403),
                 ("POST", "/", "localhost", 501), ("PUT", "/api/state", "localhost", 501),
                 ("BREW", "/", "localhost", 501)]
        for method, path, host, code in cases:
            with self.subTest(method=method, path=path, host=host):
                con = http.client.HTTPConnection(*address, timeout=5)
                self.addCleanup(con.close)
                con.request(method, path, headers={"Host": host if host == "evil.example" else f"{host}:{address[1]}"})
                answer = con.getresponse()
                answer.read()
                self.assertEqual(answer.status, code)
                policy = answer.getheader("Content-Security-Policy") or ""
                self.assertIn("default-src 'none'", policy)
                self.assertIn("frame-ancestors 'none'", policy)
                self.assertNotIn("unsafe-inline", policy)
                self.assertEqual(answer.getheader("X-Content-Type-Options"), "nosniff")

    def test_scripts_and_styles_run_by_hash_only(self):
        def digest(text):
            return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'"
        live = companion.page({"mode": "live"})
        script = re.findall(r"<script>(.*?)</script>", live, re.S)[0]
        self.assertIn(f"script-src {digest(script)};", companion.CSP_LIVE)
        # the live page has no inline style: its styles are the vendored files, from this server only
        self.assertNotIn("<style", live)
        self.assertNotRegex(live, r"\sstyle=")
        self.assertIn("style-src 'self';", companion.CSP_LIVE)
        out = self.tmp / "show.html"
        companion.export_showcase(self.team, out)
        show = out.read_text()
        meta = re.search(r'<meta http-equiv="Content-Security-Policy" content="([^"]*)">', show).group(1)
        self.assertIn(digest(re.findall(r"<script>(.*?)</script>", show, re.S)[0]), meta)
        self.assertIn(digest(re.findall(r"<style>(.*?)</style>", show, re.S)[0]), meta)
        self.assertIn("connect-src 'none'", meta)
        self.assertNotIn("'self'", meta)
        self.assertNotIn("unsafe-inline", meta)

    def test_keys_are_redacted_and_board_columns_are_fixed(self):
        token = "Ab3dEf6hIj9kLm2nOp5qRs8tUv1wXy4z"
        (self.team / "board.md").write_text(BOARD.replace(
            "| ID | Task |", f"| ID /home/alice/x reviewer@example.org {SECRET} | {token} |"))
        state = companion.collect(self.team)
        text = json.dumps(state)
        for leak in ("/home/alice", "reviewer@example.org", SECRET, token):
            self.assertNotIn(leak, text)
        self.assertEqual(list(state["board"][0]), list(companion.BOARD_COLUMNS))
        self.assertEqual(state["board"][0]["Task"], "Add the status endpoint")
        self.assertEqual(companion.redact({"ops@example.org": {"/etc/x/y": 1}}), {"[email]": {"[path]": 1}})
        db = self.tmp / "hub.db"
        c = sqlite3.connect(db); c.execute("CREATE TABLE work(id INTEGER PRIMARY KEY, status TEXT)")
        c.execute("INSERT INTO work(status) VALUES (?)", ("ops@example.org",)); c.commit(); c.close()
        self.assertNotIn("ops@example.org", json.dumps(companion.collect(self.team, hub=db)))
        out = self.tmp / "show.html"
        companion.export_showcase(self.team, out)
        self.assertNotIn(token, out.read_text())

    def test_system_and_glued_paths_are_redacted(self):
        # the text, and the part of it that may not stay (a path glued to a word loses the path)
        for text, leak in (("/etc/company/internal.conf", "/etc/company"), ("~/notes/plan.md", "~/notes"),
                           ("/usr/local/share/x", "/usr/local"), ("/bot/tasks/T-1/out", "/bot/tasks"),
                           ("/workspace/repo/a", "/workspace/repo"), ("x/home/alice/y", "/home/alice"),
                           ("build/Users/bob/z", "/Users/bob"), ("C:\\Users\\a\\b", "Users\\a"),
                           ("\\\\server\\share\\x", "server\\share")):
            with self.subTest(text=text):
                self.assertNotIn(leak, companion.redact(f"see {text} now"))
                self.assertEqual(companion.residual(f"see {text} now"), "a private path")
        for keep in ("src/api/**", "colead/federate", "1/2", "a/b/c"):
            with self.subTest(keep=keep):
                self.assertEqual(companion.redact(keep), keep)

    def test_links_in_the_team_folder_are_never_followed(self):
        outside = self.tmp / "outside"
        (outside / "flows/T-7").mkdir(parents=True)
        (outside / "board.md").write_text(BOARD.replace("Add the status endpoint", "OUTSIDE_BOARD"))
        (outside / "log.jsonl").write_text(json.dumps({"t": "2026-10-28T10:00:00Z", "who": "x", "task": "T-1",
                                                       "outcome": "done", "signal": "OUTSIDE_LOG"}) + "\n")
        (outside / "bot.md").write_text("---\nid: outsider\nname: OUTSIDE_BOT\n---\n")
        (outside / "flows/T-7/state.json").write_text(json.dumps({"task": "T-7", "status": "ready",
                                                                 "branch": "OUTSIDE_FLOW"}))
        (outside / "peer.json").write_text(json.dumps({"type": "OUTSIDE_PEER", "at": "x"}))
        t = self.team
        (t / "board.md").unlink(); (t / "board.md").symlink_to(outside / "board.md")
        (t / "logs" / "evil.jsonl").symlink_to(outside / "log.jsonl")
        (t / "bots" / "outsider.md").symlink_to(outside / "bot.md")
        (t / "flows" / "T-7").symlink_to(outside / "flows" / "T-7")
        (t / "import/federation/peer-b" / ("1" * 32 + ".json")).symlink_to(outside / "peer.json")
        (t / "import/federation/peer-c").symlink_to(outside)
        text = json.dumps(companion.collect(t))
        for leak in ("OUTSIDE_BOARD", "OUTSIDE_LOG", "OUTSIDE_BOT", "OUTSIDE_FLOW", "OUTSIDE_PEER", "peer-c"):
            self.assertNotIn(leak, text)
        # a folder on the way that is a link counts the same
        shutil.rmtree(t / "logs"); (t / "logs").symlink_to(outside)
        (outside / "a.jsonl").write_text((outside / "log.jsonl").read_text())
        self.assertNotIn("OUTSIDE_LOG", json.dumps(companion.collect(t)))

    def test_a_fifo_or_a_huge_file_never_holds_a_request(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("no FIFOs here")
        os.mkfifo(self.team / "logs" / "pipe.jsonl")
        os.mkfifo(self.team / "flows" / "T-1" / "brief.md.tmp")
        (self.team / "flows/T-1/brief.md").unlink(); os.mkfifo(self.team / "flows/T-1/brief.md")
        done = []
        worker = threading.Thread(target=lambda: done.append(companion.collect(self.team)), daemon=True)
        worker.start(); worker.join(10)
        self.assertTrue(done, "collect hung on a FIFO")
        with unittest.mock.patch.object(companion, "MAX_READ", 2000):
            (self.team / "board.md").write_text(BOARD + "x" * 5000)
            self.assertEqual(companion.collect(self.team)["board"], [])
            with (self.team / "logs" / "lead.jsonl").open("a") as h:
                h.write("y" * 5000 + "\n" + json.dumps({"t": "2026-10-29T00:00:00Z", "who": "lead", "task": "T-1",
                                                       "outcome": "done", "signal": "newest"}) + "\n")
            self.assertEqual(companion.collect(self.team)["log"][-1]["signal"], "newest")

    def test_without_a_no_follow_open_nothing_is_read(self):
        # Fail closed: where the OS cannot open relative to a folder without following a link, no path-based
        # fallback runs (a check, then an open, would race; a FIFO would block).
        os.mkfifo(self.team / "logs" / "pipe.jsonl")
        opened = []
        real_open = os.open
        def watch(path, *args, **kwargs):
            opened.append(path)
            return real_open(path, *args, **kwargs)
        with unittest.mock.patch.object(companion, "FD_WALK", False), \
                unittest.mock.patch.object(companion.os, "open", watch):
            done = []
            worker = threading.Thread(target=lambda: done.append(companion.collect(self.team)), daemon=True)
            worker.start(); worker.join(10)
            self.assertTrue(done, "collect blocked")
            self.assertEqual((done[0]["board"], done[0]["log"], done[0]["flows"]), ([], [], []))
            self.assertEqual(companion.read_text(self.team, "board.md"), "")
            self.assertIn("link", companion.unsafe_platform())
        self.assertEqual(opened, [])

    def test_reading_the_hub_changes_no_file(self):
        folder = self.tmp / "hubdir"; folder.mkdir()
        db = folder / "hub.db"
        # A WAL database whose writer stopped without a checkpoint: the rows are in hub.db-wal, the -shm file is gone.
        writer = ("import os, sqlite3, sys\ncon = sqlite3.connect(sys.argv[1]); con.execute('PRAGMA journal_mode=WAL')\n"
                  "con.execute('CREATE TABLE work(status TEXT)')\n"
                  "con.executemany('INSERT INTO work VALUES (?)', [('active',), ('active',), ('done',)])\n"
                  "con.commit(); os._exit(0)\n")
        subprocess.run([sys.executable, "-c", writer, str(db)], check=True)
        (folder / "hub.db-shm").unlink()
        self.assertGreater((folder / "hub.db-wal").stat().st_size, 0)
        before = {f.name: f.read_bytes() for f in folder.iterdir()}
        server = companion.make_server(self.team, port=0, hub=db)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        for _ in range(2):
            state = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/api/state").read())
            self.assertEqual(state["hub"], {"work": {"active": 2, "done": 1}})  # the WAL's committed rows count
        self.assertEqual({f.name: f.read_bytes() for f in folder.iterdir()}, before)

    def test_odd_values_in_the_folder_cost_no_page(self):
        with (self.team / "logs" / "worker.jsonl").open("a") as h:
            h.write('{"t": "2026-10-29T00:00:00Z", "who": "worker", "signal": "\\ud800 x"}\n')
        (self.team / "flows/T-1/state.json").write_text(json.dumps({
            "task": "T-1", "status": "running", "steps": [{"kind": "worker", "head": 12345}, {"kind": ["x"]}, "x"]}))
        (self.team / "flows/T-2/state.json").write_text(json.dumps({"task": "T-2", "status": "ready", "steps": "abc"}))
        state = companion.collect(self.team)
        companion.page(state).encode("utf-8")
        self.assertEqual({f["task"]: f["steps"][0]["head"] for f in state["flows"] if f["steps"]}, {"T-1": "12345"})
        out = self.tmp / "show.html"
        companion.export_showcase(self.team, out)
        self.assertTrue(out.exists())

    def test_paths_with_spaces_and_other_homes(self):
        for text, leak in (("/Users/John Smith/Documents/x", "Smith"), ("/home/Ann Lee/x", "Lee"),
                           ("~bob/notes", "bob"), ("C:\\Users\\John Smith\\x", "Smith"),
                           ("file:///etc/passwd", "/etc/passwd"), ("C:/Users/alice/y", "alice")):
            with self.subTest(text=text):
                self.assertNotIn(leak, companion.redact(f"see {text} now"))
                self.assertEqual(companion.residual(f"see {text} now"), "a private path")
        self.assertEqual(companion.redact("x~y/z"), "x~y/z")

    def test_windows_paths_with_either_slash(self):
        windows = (("C:/Windows/System32/config/SAM", "Windows/System32"), ("c:/x\\y", "x\\y"),
                   ("D:\\data/secret.txt", "data/secret"), ("C:/Users/John Smith/x", "Smith"),
                   ("//server/share/x", "server/share"), ("\\\\server/share", "server/share"),
                   ("//server\\share", "server\\share"), ("file:///C:/Windows/win.ini", "Windows/win"),
                   ("(C:/Windows/x)", "Windows/x"))
        for text, leak in windows:
            with self.subTest(text=text):
                self.assertNotIn(leak, companion.redact(f"see {text} now"))
                self.assertEqual(companion.residual(f"see {text} now"), "a private path")
        for keep in ("a//b", "x:y"):
            with self.subTest(keep=keep):
                self.assertEqual(companion.redact(keep), keep)
        self.assert_on_no_route("C:/Windows/System32/config/SAM", "System32")

    def assert_on_no_route(self, task, leak, kind="private path"):
        """A board task holding a path (or another kind): the serialized text of every route and of the export never
        holds the leak, and with redaction skipped the export's last check refuses it."""
        (self.team / "board.md").write_text(BOARD.replace("Add the status endpoint", task))
        host, port = self.serve()
        base = f"http://{host}:{port}"
        for path in ("/api/state", "/"):
            with self.subTest(route=path, task=task):
                self.assertNotIn(leak, urllib.request.urlopen(base + path).read().decode())
        stream = urllib.request.urlopen(base + "/events", timeout=10)
        self.addCleanup(stream.close)
        while True:
            line = stream.readline().decode()
            if line.startswith("data: "): break
        self.assertIn("T-1", line)
        self.assertNotIn(leak, line)
        out = self.tmp / "show.html"
        companion.export_showcase(self.team, out, title="Demo")
        self.assertNotIn(leak, out.read_text())
        out.unlink()
        with unittest.mock.patch.object(companion, "redact", lambda value: value):
            with self.assertRaises(SystemExit) as stop:
                companion.export_showcase(self.team, out, title="Demo")
        self.assertIn(kind, str(stop.exception))
        self.assertFalse(out.exists())

    def test_quoted_and_literal_e_mail_addresses(self):
        # T-0042 (Codex T-0022r3.r1): a quoted local part passed the redaction and the last check.
        for text, leak in (('"jane.doe"@example.org', "jane.doe"), ('"Jane Doe"@example.org', "Jane Doe"),
                           ('"a\\"b"@example.org', 'a\\"b'), ("ops@[192.0.2.1]", "ops@"),
                           ("ops@[IPv6:2001:db8::1]", "ops@"), ("ops@[IPv6:2001:db8::192.0.2.1]", "ops@"),
                           ('"jane"@[IPv6:::1]', "jane"),
                           ("jane.doe@example.org", "jane.doe")):
            with self.subTest(text=text):
                for line in (text, f"mail {text} now", f"<{text}>"):
                    self.assertNotIn(leak, companion.redact(line))
                    self.assertIn("[email]", companion.redact(line))
                    self.assertEqual(companion.residual(line), "an e-mail address")
        for keep in ('say "hi" @ noon', "a@b", '"quoted" text'):
            with self.subTest(keep=keep):
                self.assertEqual(companion.redact(keep), keep)
        for task, leak in (('"jane.doe"@example.org', "jane.doe"), ("ops@[IPv6:2001:db8::1]", "2001:db8")):
            self.assert_on_no_route(task, leak, kind="e-mail address")

    def test_a_home_runs_to_the_line_end_and_system_roots_stand_alone(self):
        for text, leak in (("/home/John Smith", "Smith"), ("/Users/Ann Lee", "Lee"),
                           ("C:\\Users\\John Smith", "Smith"), ("C:/Users/John Smith", "Smith"),
                           ("/home/alice", "alice"), ("/root", "/root"), ("/etc", "/etc"), ("(/tmp)", "/tmp"),
                           ("/var.", "/var"), ("x /usr", "/usr")):
            with self.subTest(text=text):
                for line in (text, f"see {text}", f"see {text} now", f"{text}\nnext line"):
                    self.assertNotIn(leak, companion.redact(line))
                    self.assertEqual(companion.residual(line), "a private path")
                self.assertIn("next line", companion.redact(f"{text}\nnext line"))
        for keep in ("feature/dev", "colead/run", "a/etc", "and/or", "src/api/**"):
            with self.subTest(keep=keep):
                self.assertEqual(companion.redact(keep), keep)
        for task, leak in (("/home/John Smith", "Smith"), ("/root", "/root"), ("/etc", "/etc"),
                           ("C:\\Users\\John Smith", "Smith")):
            self.assert_on_no_route(task, leak)

    def test_the_leads_rule_ends_the_path_cases(self):
        for text, leak in (("/home/John Smith.", "Smith"), ("(see /etc)", "/etc"), ("/sys", "/sys"),
                           ("/boot/grub", "/boot"), ("~/notes plan.md", "notes"), ("~bob/John Smith", "Smith"),
                           ("Home directory: /root; system configuration directory: /etc", "/root"),
                           ("D:Users\\bob", "bob"), ("key:/data/alice/x", "alice")):
            with self.subTest(text=text):
                self.assertNotIn(leak, companion.redact(text))
                self.assertEqual(companion.residual(text), "a private path")
        self.assertNotIn("/etc", companion.redact("Home directory: /root; system configuration directory: /etc"))
        for task, leak in (("/home/John Smith.", "Smith"), ("(see /etc)", "/etc"), ("~bob/John Smith", "Smith")):
            self.assert_on_no_route(task, leak)

    def test_every_absolute_path_by_one_rule(self):
        # The lead's rule (2026-10-28): any absolute path, not a list of known roots; a home or users segment in any
        # case and anywhere runs to the line end. Every example of the T-0022r2 reviews (Codex r1, Claude r2).
        for text, leak in (("/bin", "/bin"), ("/sbin/x", "/sbin"), ("/lib", "/lib"), ("/lib64", "/lib64"),
                           ("/Applications/Tool.app", "Tool"), ("/Library/Prefs", "Library"), ("/System", "/System"),
                           ("/mnt/c/Users/John Smith/notes", "Smith"), ("c:\\users\\John Smith\\x", "Smith"),
                           ("C:\\USERS\\John Smith", "Smith"), ("/var/home/John Smith/x", "Smith"),
                           ("D:\\Documents and Settings\\John Smith", "Smith"), ("/HOME/Ann Lee", "Lee"),
                           ("a=/data/x", "/data"), ("~", "~"), ("/", "/")):
            with self.subTest(text=text):
                for line in (text, f"see {text} now", f"({text})"):
                    self.assertNotIn(leak, companion.redact(line))
                    self.assertEqual(companion.residual(line), "a private path")
                self.assertEqual(companion.redact(f"{text}\nnext line").split("\n")[1], "next line")
        # Over-redaction in prose is accepted: a URL's "//" after ":" and a "/" standing alone are roots too.
        for text in ("https://example.org/docs/a", "ratio 3:/4", "v1 / v2", "see /status"):
            with self.subTest(over=text):
                self.assertIn("[path]", companion.redact(text))
        # Relative paths, branch names and a "/" inside a word stay.
        for keep in ("src/api/**", "colead/federate", "and/or", "a/b/c", "x~y/z", "T-1"):
            with self.subTest(keep=keep):
                self.assertEqual(companion.redact(keep), keep)
        for task, leak in (("/bin", "/bin"), ("/mnt/c/Users/John Smith/notes", "Smith"),
                           ("c:\\users\\John Smith\\x", "Smith"), ("/var/home/John Smith/x", "Smith")):
            self.assert_on_no_route(task, leak)

    def test_a_blob_status_in_the_hub_costs_no_page(self):
        db = self.tmp / "hub.db"
        c = sqlite3.connect(db); c.execute("CREATE TABLE work(id INTEGER PRIMARY KEY, status)")
        c.executemany("INSERT INTO work(status) VALUES (?)", [(b"active",), ("done",), (None,)]); c.commit(); c.close()
        state = companion.collect(self.team, hub=db)
        self.assertEqual(state["hub"], {"work": {"active": 1, "done": 1, "None": 1}})
        json.dumps(state)

    def test_the_host_must_name_this_port(self):
        host, port = self.serve()
        for value, code in ((f"127.0.0.1:{port}", 200), (f"localhost:{port}", 200), ("localhost:1", 403),
                            (f"127.0.0.1:{port + 1}", 403), ("127.0.0.1", 403)):
            with self.subTest(host=value):
                c = http.client.HTTPConnection(host, port, timeout=5)
                c.putrequest("GET", "/api/state", skip_host=True); c.putheader("Host", value); c.endheaders()
                response = c.getresponse(); response.read(); c.close()
                self.assertEqual(response.status, code)

STATIC = Path(D) / "static"
OTHER_BRANDS = re.compile(rb"r[o]enu|w[u]lew|f[o]ug|v[a]ikor", re.I)
SHEETS = ("v8/css/tokens.css", "v8/css/fonts.css", "v8/css/base.css", "v8/css/materials.css",
          "v8/css/components.css", "v8/css/patterns.css", "cockpit.css")


def raw_get(address, path, host=None):
    """A GET with the path sent exactly as written (urllib would normalise ../ and %2e)."""
    c = http.client.HTTPConnection(*address, timeout=5)
    try:
        c.putrequest("GET", path, skip_host=True); c.putheader("Host", host or f"127.0.0.1:{address[1]}"); c.endheaders()
        response = c.getresponse()
        return response.status, response.getheader("Content-Type"), response.read()
    finally:
        c.close()


class V8(unittest.TestCase):
    """The cockpit in VAIEV UI v8 (wouf): vendored files from this server only, four views with data behind them."""
    setUp, serve = T.setUp, T.serve

    def test_the_live_page_loads_only_vendored_files_from_this_server(self):
        live = companion.page(companion.collect(self.team))
        refs = re.findall(r'(?:href|src)="([^"]*)"', live)
        self.assertEqual([r for r in refs if r.endswith(".css")], [f"/static/{s}" for s in SHEETS])
        for ref in refs:
            with self.subTest(ref=ref):
                self.assertTrue(ref.startswith("/static/"), ref)
                self.assertTrue((STATIC / ref.split("#")[0][len("/static/"):]).is_file(), ref)
        self.assertNotRegex(live, r"(?i)https?://(?!www\.w3\.org/2000/svg)|(?:href|src)=\"//|@import")
        self.assertIn('<html lang="en" data-brand="wouf">', live)
        self.assertNotIn("data-theme", live)  # the system setting chooses light or dark

    def test_vendored_styles_reach_nothing_outside_static(self):
        for sheet in SHEETS:
            text = (STATIC / sheet).read_text(encoding="utf-8")
            with self.subTest(sheet=sheet):
                self.assertNotIn("@import", text)
                for ref in re.findall(r"url\(\s*[\"']?([^\"')]+)", text):
                    self.assertNotRegex(ref, r"^(?:[a-z]+:|/)", ref)
                    target = ((STATIC / sheet).parent / ref).resolve()
                    self.assertTrue(target.is_file(), ref)
                    self.assertTrue(target.is_relative_to(STATIC.resolve()), ref)
        tokens = (STATIC / "v8/css/tokens.css").read_text(encoding="utf-8")
        self.assertIn('[data-brand="wouf"]', tokens)
        self.assertIn("@media (prefers-color-scheme: dark)", tokens)

    def test_the_vendored_folder_is_single_brand(self):
        for path in STATIC.rglob("*"):
            if path.is_file():
                with self.subTest(path=str(path.relative_to(STATIC))):
                    self.assertIsNone(OTHER_BRANDS.search(path.name.encode() + path.read_bytes()))
        for sheet in SHEETS:
            brands = set(re.findall(r'data-brand="([^"]*)"', (STATIC / sheet).read_text(encoding="utf-8")))
            self.assertLessEqual(brands, {"wouf"}, sheet)

    def test_static_files_are_served_and_nothing_outside_static(self):
        address = self.serve()
        for path, kind in (("/static/v8/css/tokens.css", "text/css"), ("/static/cockpit.css", "text/css"),
                           ("/static/v8/fonts/ibm-plex-sans-latin-400-normal.woff2", "font/woff2"),
                           ("/static/v8/icons.svg", "image/svg+xml"), ("/static/v8/brand/wouf-icon.svg", "image/svg+xml")):
            with self.subTest(path=path):
                code, got, body = raw_get(address, path)
                self.assertEqual((code, got.split(";")[0]), (200, kind))
                self.assertEqual(body, (STATIC / path[len("/static/"):]).read_bytes())
        for path in ("/static/../companion.py", "/static/v8/../../companion.py", "/static/%2e%2e/companion.py",
                     "/static/v8/css/..%2f..%2f..%2fcompanion.py", "/static/v8\\..\\..\\companion.py",
                     "/static//v8/css/tokens.css", "/static/./v8/css/tokens.css", "/static/v8/css/tokens.css?x",
                     "/static/v8/css/tokens.css/", "/static/", "/static/v8", "/static/showcase.html", "/companion.py",
                     "/README.md", "/static/v8/css/TOKENS.CSS", "/../board.md"):
            with self.subTest(path=path):
                self.assertEqual(raw_get(address, path)[0], 404)
        self.assertEqual(raw_get(address, "/static/v8/css/tokens.css", host="evil.example")[0], 403)

    def test_a_link_inside_static_is_never_served(self):
        folder = self.tmp / "static"
        writable_copy(STATIC, folder)
        secret = self.tmp / "secret.css"; secret.write_text("body{}")
        (folder / "linked.css").symlink_to(secret)
        (folder / "v8" / "more").symlink_to(self.tmp, target_is_directory=True)
        with unittest.mock.patch.object(companion, "STATIC", folder):
            address = self.serve()
            self.assertEqual(raw_get(address, "/static/v8/css/tokens.css")[0], 200)
            for path in ("/static/linked.css", "/static/v8/more/secret.css"):
                with self.subTest(path=path):
                    self.assertEqual(raw_get(address, path)[0], 404)

    def test_the_views_are_the_ones_with_data_and_in_english(self):
        self.assertEqual([v[1] for v in companion.VIEWS], ["Today", "Approvals", "Board", "Team"])
        live = companion.page(companion.collect(self.team))
        script = re.findall(r"<script>(.*?)</script>", live, re.S)[0]
        for word in ("Chat", "Map", "Goal", "Plan", "Heute", "Freigaben", "Braucht dich", "Läuft", "Karte", "Ziel"):
            with self.subTest(word=word):
                self.assertNotRegex(live.replace(script, "") + script, rf"\b{word}\b")
        for view in ("today", "approvals", "board", "team"):
            self.assertIn(f'data-view="{view}"', live)

    def test_every_script_writes_text_never_markup(self):
        for name in ("SCRIPT", "LIVE", "ACTIONS"):
            code = getattr(companion, name)
            with self.subTest(script=name):
                for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "Function(",
                             "srcdoc", "setAttribute('on", "createContextualFragment", "DOMParser"):
                    self.assertNotIn(sink, code)
        self.assertIn("textContent", companion.SCRIPT)

    def test_the_live_policy_adds_self_only_for_the_vendored_files(self):
        script = companion.digest(companion.SCRIPT + companion.LIVE)
        self.assertEqual(companion.CSP_LIVE, f"default-src 'none'; style-src 'self'; font-src 'self'; img-src 'self'; "
                                             f"script-src {script}; connect-src 'self'; base-uri 'none'; "
                                             f"form-action 'none'; frame-ancestors 'none'")
        actions = companion.digest(companion.SCRIPT + companion.LIVE + companion.ACTIONS)
        self.assertEqual(companion.csp_for(True), companion.CSP_LIVE.replace(script, actions))

    def test_the_showcase_template_inlines_the_v8_wouf_tokens(self):
        page = (STATIC / "showcase.html").read_text(encoding="utf-8")
        style = re.search(r"<style>(.*?)</style>", page, re.S).group(1)
        self.assertEqual(page.count("<style"), 1)
        self.assertEqual(len(re.findall(r"<script>", page)), 1)
        for slot in ("__POLICY__", "__TITLE__", '<script type="application/json" id="showcase-data">__DATA__</script>'):
            self.assertIn(slot, page)
        self.assertNotRegex(page, r"(?i)(?:src|href)\s*=|https?://|@import|url\(")
        self.assertIn("prefers-color-scheme:dark", style.replace(" ", ""))
        tokens = (STATIC / "v8/css/tokens.css").read_text(encoding="utf-8")
        light = re.search(r'\[data-brand="wouf"\] \{(.*?)\}', tokens, re.S).group(1)
        dark = re.search(r'\[data-brand="wouf"\]\[data-theme="dark"\] \{(.*?)\}', tokens, re.S).group(1)
        values = lambda text: dict(re.findall(r"(--[\w-]+):\s*([^;}]+)[;}]", text))
        light_in, dark_in = style.split("@media (prefers-color-scheme: dark)")
        for want, block in ((values(light), values(light_in)), (values(dark), values(dark_in))):
            used = {k: v for k, v in block.items() if k in want}
            self.assertGreaterEqual(len(used), 12)
            for key, value in used.items():
                self.assertEqual(value.strip(), want[key].strip(), key)
        for material in (".plane", ".plate", ".field"):
            self.assertIn(material, style)


if __name__ == "__main__":
    unittest.main()
