"""Owner actions of the companion: each one is a visible `team` command behind a token, a same-origin check and a
confirmation step; nothing else can run."""
import http.client, json, os, shutil, stat, subprocess, sys, tempfile, threading, unittest
from pathlib import Path
D = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, D)
import companion
from test_companion import BOARD, SECRET, writable_copy

ROOT = Path(D).parents[2]
FAKE_TEAM = """#!{python}
import json, sys
with open({out!r}, "a") as h: h.write(json.dumps(sys.argv[1:]) + "\\n")
print("done: " + " ".join(sys.argv[1:]) + {extra!r})
"""


def event(origin, seq, kind, task, data):
    return json.dumps({"v": 1, "id": f"{origin}-{seq}", "seq": seq, "t": "2026-10-29T08:00:00Z", "origin": origin,
                       "actor": "lead", "kind": kind, "task": task, "scope": "local", "data": data}) + "\n"


class Actions(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, self.tmp)
        t = self.team = self.tmp / "team"
        for d in ("bots", "logs", "flows", "events"):
            (t / d).mkdir(parents=True)
        (t / "board.md").write_text(BOARD)
        origin = "a1b2c3d4e5f6"
        (t / "events" / f"{origin}.jsonl").write_text("".join([
            event(origin, 1, "question.asked", "T-1", {"id": "Q-1", "text": "Which staging URL?", "from": "worker"}),
            event(origin, 2, "question.asked", "T-2", {"id": "Q-2", "text": "Rotate now?", "from": "review"}),
            event(origin, 3, "question.answered", "T-2", {"id": "Q-2", "text": "yes", "from": "review"}),
            event(origin, 4, "decision.recorded", "T-1", {"number": 1, "title": "Use the new API", "options": ["a", "b"],
                                                          "why": "w", "risk": "r", "authority": "owner", "next": "n",
                                                          "status": "pending"}),
            event(origin, 5, "decision.recorded", "", {"number": 2, "title": "Done already", "options": ["a"],
                                                       "why": "w", "risk": "r", "authority": "owner", "next": "n",
                                                       "status": "approved", "decided_by": "human"}),
            event(origin, 6, "merge.proposed", "T-2", {"task": "T-2", "head": "ab" * 20, "branch": "wouf/T-2",
                                                       "verdict": "approve"})]))
        self.calls = self.tmp / "calls.jsonl"
        self.fake = self.tmp / "team-bin"
        self.fake.write_text(FAKE_TEAM.format(python=sys.executable, out=str(self.calls), extra=""))
        self.fake.chmod(0o755)
        self.token = "t" * 43

    def serve(self, actions=True):
        server = companion.make_server(self.team, port=0, actions=actions, token=self.token, team_bin=self.fake)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]
        return server

    def post(self, path, body, **headers):
        own = f"127.0.0.1:{self.port}"
        sent = {"Host": own, "Content-Type": "application/json", "X-WOUF-Token": self.token,
                "Sec-Fetch-Site": "same-origin", "Origin": f"http://{own}"}
        for key, value in headers.items():
            key = key.replace("_", "-")
            if value is None:
                sent.pop(key, None)
            else:
                sent[key] = value
        con = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(con.close)
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        con.putrequest("POST", path, skip_host=True, skip_accept_encoding=True)
        for key, value in sent.items():
            con.putheader(key, value)
        con.putheader("Content-Length", str(len(raw)))
        con.endheaders(raw)
        answer = con.getresponse()
        text = answer.read().decode()
        return answer.status, (json.loads(text) if answer.getheader("Content-Type") == "application/json" else text)

    def ran(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []

    # ------------------------------------------------------------ what the page shows

    def test_the_state_lists_what_waits_for_the_owner(self):
        owner = companion.collect(self.team)["owner"]
        self.assertEqual([q["id"] for q in owner["questions"]], ["Q-1"])
        self.assertEqual(owner["questions"][0]["text"], "Which staging URL?")
        self.assertEqual([d["id"] for d in owner["decisions"]], ["D-1"])
        self.assertEqual(owner["merges"], [{"task": "T-2", "head": "ab" * 6, "branch": "wouf/T-2",
                                            "verdict": "approve", "marked": False}])
        self.assertNotIn("owner", companion.collect(self.team, mode="showcase"))

    # ------------------------------------------------------------ the three actions

    def test_each_action_runs_one_team_command_after_a_preview(self):
        self.serve()
        cases = (("/action/answer", {"answers": [{"id": "Q-1", "text": "staging-2, the new one"}]},
                  [["question", "answer", "Q-1", "staging-2, the new one"]]),
                 ("/action/decision", {"id": "D-1", "verdict": "approve"}, [["decision", "approve", "D-1"]]),
                 ("/action/decision", {"id": "D-1", "verdict": "reject", "note": "too early"},
                  [["decision", "reject", "D-1", "--note", "too early"]]),
                 ("/action/merge-proposal", {"task": "T-2", "head": "ab" * 6},
                  [["merge", "mark", "T-2", "--head", "ab" * 6]]))
        for path, body, argv in cases:
            with self.subTest(path=path, body=body):
                if self.calls.exists():
                    self.calls.unlink()
                code, preview = self.post(path, {**body, "confirm": False})
                self.assertEqual(code, 200, preview)
                self.assertEqual([c["command"] for c in preview["commands"]],
                                 [companion.shown(["team", *a]) for a in argv])
                self.assertEqual(self.ran(), [])
                code, done = self.post(path, {**body, "confirm": True})
                self.assertEqual(code, 200, done)
                self.assertEqual(self.ran(), argv)
                self.assertEqual([r["code"] for r in done["results"]], [0] * len(argv))

    def test_several_answers_are_one_command_each(self):
        self.serve()
        body = {"answers": [{"id": "Q-1", "text": "one"}, {"id": "Q-7", "text": "two\nlines"}], "confirm": True}
        self.assertEqual(self.post("/action/answer", body)[0], 200)
        self.assertEqual(self.ran(), [["question", "answer", "Q-1", "one"], ["question", "answer", "Q-7", "two\nlines"]])

    def test_the_output_shown_is_masked(self):
        self.fake.write_text(FAKE_TEAM.format(python=sys.executable, out=str(self.calls), extra=" " + SECRET))
        self.serve()
        code, done = self.post("/action/decision", {"id": "D-1", "verdict": "approve", "confirm": True})
        self.assertEqual(code, 200)
        self.assertNotIn(SECRET, json.dumps(done))
        self.assertIn("[redacted]", done["results"][0]["output"])

    # ------------------------------------------------------------ what never runs

    def test_a_forged_or_cross_site_request_is_refused(self):
        self.serve()
        body = {"id": "D-1", "verdict": "approve", "confirm": True}
        forged = {"no token": {"X_WOUF_Token": None}, "wrong token": {"X_WOUF_Token": "x" * 43},
                  "cross site": {"Sec_Fetch_Site": "cross-site"}, "same site": {"Sec_Fetch_Site": "same-site"},
                  "other origin": {"Origin": "http://evil.example"},
                  "no fetch header, no origin": {"Sec_Fetch_Site": None, "Origin": None},
                  "dns rebinding": {"Host": f"evil.example:{self.port}"},
                  "other port": {"Host": f"127.0.0.1:{self.port + 1}"},
                  "form post": {"Content_Type": "application/x-www-form-urlencoded"},
                  "text post": {"Content_Type": "text/plain"}}
        for name, headers in forged.items():
            with self.subTest(case=name):
                code, _ = self.post("/action/decision", body, **headers)
                self.assertEqual(code, 403)
        # without Sec-Fetch-Site (an older browser) the page's own Origin is enough
        self.assertEqual(self.post("/action/decision", body, Sec_Fetch_Site=None)[0], 200)
        self.assertEqual(len(self.ran()), 1)

    def test_bad_arguments_run_nothing(self):
        self.serve()
        bad = (("/action/answer", {"answers": [{"id": "Q-1; rm -rf /", "text": "x"}]}),
               ("/action/answer", {"answers": [{"id": "Q-1", "text": "x" * 501}]}),
               ("/action/answer", {"answers": [{"id": "Q-1", "text": "key " + SECRET}]}),
               ("/action/answer", {"answers": [{"id": "Q-1", "text": "--help"}]}),
               ("/action/answer", {"answers": [{"id": "Q-1", "text": ""}]}),
               ("/action/answer", {"answers": [{"id": f"Q-{n}", "text": "x"} for n in range(21)]}),
               ("/action/answer", {"answers": [{"id": "Q-1", "text": "x", "extra": 1}]}),
               ("/action/decision", {"id": "D-1", "verdict": "merge"}),
               ("/action/decision", {"id": "1", "verdict": "approve"}),
               ("/action/merge-proposal", {"task": "T-2", "head": "--force"}),
               ("/action/merge-proposal", {"task": "../T-2", "head": "abcdef1"}),
               ("/action/merge-proposal", {"task": "T-2", "head": "abcdef1", "push": True}))
        for path, body in bad:
            with self.subTest(body=body):
                code, _ = self.post(path, {**body, "confirm": True})
                self.assertEqual(code, 400)
        for raw in (b"{not json", b'{"id": "D-1", "id": "D-2", "verdict": "approve", "confirm": true}', b"x" * 70000):
            with self.subTest(raw=raw[:20]):
                self.assertIn(self.post("/action/decision", raw)[0], (400, 413))
        self.assertEqual(self.ran(), [])

    def test_get_never_acts_and_other_paths_do_not_exist(self):
        self.serve()
        con = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(con.close)
        con.request("GET", "/action/decision?id=D-1&verdict=approve", headers={"Host": f"127.0.0.1:{self.port}"})
        self.assertEqual(con.getresponse().status, 404)
        self.assertEqual(self.post("/action/shell", {"cmd": "id", "confirm": True})[0], 404)
        self.assertEqual(self.post("/", {"confirm": True})[0], 404)
        self.assertEqual(self.ran(), [])

    def test_without_actions_the_cockpit_stays_read_only(self):
        self.serve(actions=False)
        code, _ = self.post("/action/decision", {"id": "D-1", "verdict": "approve", "confirm": True})
        self.assertEqual(code, 501)
        self.assertEqual(self.ran(), [])
        self.assertNotIn("/action/", companion.page(companion.collect(self.team)))

    def test_the_page_confirms_before_it_posts_and_keeps_the_token_out_of_the_url(self):
        script = companion.ACTIONS
        self.assertIn("location.hash", script)
        self.assertIn("history.replaceState", script)
        self.assertIn("X-WOUF-Token", script)
        self.assertIn("confirm: false", script)  # the sheet shows the server's commands first
        self.assertIn("confirm: true", script)
        self.assertNotIn("innerHTML", script)
        page = companion.page(companion.collect(self.team), actions=True)
        self.assertIn(companion.digest(companion.SCRIPT + companion.LIVE + companion.ACTIONS), companion.csp_for(True))

    def test_the_launch_url_carries_the_token_in_the_fragment_only(self):
        url = companion.launch_url(8765, "abc")
        self.assertEqual(url, "http://127.0.0.1:8765/#token=abc")
        self.assertGreaterEqual(len(companion.new_token()), 43)

    # ------------------------------------------------------------ with the real team command

    def test_an_answer_reaches_the_real_store(self):
        team_bin = ROOT / ".agents" / "bin" / "team"
        real = self.tmp / "real"
        writable_copy(ROOT / ".agents" / "team", real)
        env = {**os.environ, "WOUF_TEAM_DIR": str(real)}
        for k in ("WOUF_EVENTS_DIR", "WOUF_LOG_DIR", "WOUF_STATE_DB"):
            env.pop(k, None)
        made = subprocess.run([sys.executable, str(team_bin), "question", "add", "T-1", "Which port?"], env=env,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(made.returncode, 0, made.stderr)
        server = companion.make_server(real, port=0, actions=True, token=self.token, team_bin=team_bin)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]
        self.assertEqual([q["id"] for q in companion.collect(real)["owner"]["questions"]], ["Q-1"])
        code, done = self.post("/action/answer", {"answers": [{"id": "Q-1", "text": "8800"}], "confirm": True})
        self.assertEqual(code, 200, done)
        self.assertEqual(done["results"][0]["code"], 0, done)
        self.assertEqual(companion.collect(real)["owner"]["questions"], [])
        self.assertNotIn("Which port?", (real / "board.md").read_text())


@unittest.skipUnless(os.environ.get("WOUF_BROWSER_TESTS") and shutil.which("playwright"),
                     "browser tests: set WOUF_BROWSER_TESTS=1 with Playwright installed")
class Browser(unittest.TestCase):
    def test_placeholder_for_the_optional_browser_run(self):
        self.skipTest("run by hand: the confirmation sheet in a real browser")


if __name__ == "__main__":
    unittest.main()
