"""The demo exercises real safeguards and leaves its caller untouched."""
import contextlib
import functools
import importlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import TIME_FACTOR, isolate_wouf_env, writable_copy  # noqa: E402

BIN = Path(__file__).resolve().parents[1] / '.agents' / 'bin'
sys.path.insert(0, str(BIN))
MARKER = 'MARKER-team-config-of-another-team'
MARKED_CONFIG = f'[team]\nname = {MARKER}\n[environment]\nengine = {MARKER}\n'
# Built from parts: the pack's leak scan refuses a literal key prefix (tests/test_pack.py).
PRIVATE_KEY_TEXT = ('PRIVATE ' + 'KEY', 'BEGIN ' + 'OPENSSH')
# An ssh-keygen ahead of the real one on PATH: it logs its arguments and what it sees of HOME and the agent.
KEYGEN_SPY = ('import json, os, sys\n'
              'with open(sys.argv[0] + ".log", "a") as log:\n'
              '    log.write(json.dumps({"argv": sys.argv[1:], "home": os.environ.get("HOME"),\n'
              '                          "agent": os.environ.get("SSH_AUTH_SOCK"), "cwd": os.getcwd()}) + "\\n")\n'
              'os.execv(REAL, [REAL, *sys.argv[1:]])\n')
# An ssh-keygen that is there but cannot make a key, in the words of the real one in a container whose uid 0 has no
# passwd entry (the lead's sandboxed gate).
KEYGEN_NO_USER = 'import sys\nprint("No user exists for uid 0", file=sys.stderr)\nsys.exit(255)\n'
# An ssh-keygen that makes the two key files and prints a fingerprint, but fails each call with FAIL among its
# arguments ('-l', the fingerprint; '-Y', signing).
KEYGEN_FAILS_AT = ('import sys\nargs = sys.argv[1:]\n'
                   'if FAIL in args:\n'
                   '    print("fake ssh-keygen: cannot " + FAIL, file=sys.stderr)\n'
                   '    sys.exit(1)\n'
                   'if "-l" in args:\n'
                   '    print("256 SHA256:fake wouf-demo (ED25519)")\n'
                   '    sys.exit(0)\n'
                   'key = args[args.index("-f") + 1]\n'
                   'open(key, "w").write("not a key\\n")\n'
                   'open(key + ".pub", "w").write("ssh-ed25519 AAAAfake wouf-demo\\n")\n')


def setUpModule() -> None:
    isolate_wouf_env()


@functools.lru_cache(maxsize=None)
def no_key_here():
    """Why the demo's receipt step cannot make a key in this test environment ('' if it can): probed once, in a temp
    folder, with the demo's own env (demo.isolated) and its own key step (demo.key_or_reason)."""
    demo = importlib.import_module('teamlib.demo')
    if not shutil.which('ssh-keygen'):
        return demo.NO_SSH_KEYGEN
    with tempfile.TemporaryDirectory(prefix='wouf-demo-probe-') as folder, demo.isolated(Path(folder)):
        return demo.key_or_reason(Path(folder))[1]


def copy_pack(root, config=None):
    """A copy of this pack's bin folder under root/.agents, with the team folder of the pack only if config is given."""
    pack = root / '.agents'
    writable_copy(BIN, pack / 'bin', ignore=shutil.ignore_patterns('__pycache__'))
    if config is not None:
        (pack / 'team').mkdir()
        (pack / 'team' / 'config.ini').write_text(config)
    return pack


def snapshot(root, skip=()):
    """Every path under root with its bytes (None for a folder), but the paths in skip."""
    return sorted((str(p.relative_to(root)), p.read_bytes() if p.is_file() else None)
                  for p in root.rglob('*') if p not in skip)


def run_demo(pack, team_dir=None):
    env = dict(os.environ)
    env.pop('WOUF_TEAM_DIR', None)
    if team_dir is not None:
        env['WOUF_TEAM_DIR'] = str(team_dir)
    return subprocess.run([sys.executable, str(pack / 'bin' / 'team'), 'demo'], cwd=pack.parent,
                          capture_output=True, text=True, timeout=60, env=env)


class DemoTest(unittest.TestCase):
    def need_a_key(self):
        reason = no_key_here()
        if reason:
            self.skipTest('the receipt step needs a key: ' + reason)

    def test_cli_json_repeat_cleanup_and_text(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'keep').write_text('untouched')
            before = sorted((str(p.relative_to(root)), p.read_bytes() if p.is_file() else None)
                            for p in root.rglob('*'))
            start = time.monotonic()
            for _ in range(2):
                done = subprocess.run([sys.executable, str(BIN / 'team'), 'demo', '--json'],
                                      cwd=root, capture_output=True, text=True, timeout=60)
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                report = json.loads(done.stdout)
                self.assertTrue(report['ok'])
                self.assertEqual(set(report), {'ok', 'steps', 'flags'})
                self.assertEqual(report['flags'], [
                    'A fake agent exercised the real gate and the real head check. '
                    'Nothing was merged; no network calls were made.'])
                bad, stale, good, made = report['steps']
                self.assertEqual(bad['model_calls'], 0)
                self.assertIn('outside Paths: outside.txt', bad['results'][0]['output'])
                self.assertEqual(stale['review']['verdict'], 'stale')
                refusal = stale['results'][-1]
                self.assertNotEqual(refusal['code'], 0)
                self.assertIn(stale['head'], refusal['output'])
                self.assertIn(stale['approved_head'][:12], refusal['output'])
                self.assertTrue(all(r['code'] == 0 for r in good['results']))
                self.assertEqual(good['review']['verdict'], 'approve')
                self.assertTrue(made['ok'], made)
                self.assertEqual(made['skipped'], bool(no_key_here()), made)
                self.assertTrue(all(r['command'] for s in report['steps'] for r in s['results']))
                self.assertEqual(before, sorted((str(p.relative_to(root)), p.read_bytes() if p.is_file() else None)
                                               for p in root.rglob('*')))
            self.assertLess(time.monotonic() - start, 10 * TIME_FACTOR)
            done = subprocess.run([sys.executable, str(BIN / 'team'), 'demo'], cwd=root,
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(done.stderr, '')
            before_receipt = done.stdout.split('\n4. ', 1)[0]
            self.assertNotRegex(before_receipt.lower(),
                                r'is missing|error|traceback|flags|no merge check command exists')
            self.assertNotRegex(done.stdout.lower(), r'is missing|error|traceback')
            self.assertTrue(done.stdout.rstrip().endswith('PASS'))
            self.assertIn(report['flags'][0], done.stdout)
            self.assertIn('0 model calls', done.stdout)
            self.assertIn('team gate', done.stdout)
            self.assertIn('team merge propose', done.stdout)
            if not no_key_here():
                self.assertIn('$ team receipt make T-1 ', done.stdout)
                self.assertIn('$ team verify ', done.stdout)

    def test_demo_proposal_accepts_real_gate_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            done = subprocess.run([sys.executable, str(BIN / 'team'), 'demo', '--json'],
                                  cwd=folder, capture_output=True, text=True, timeout=60)
            report = json.loads(done.stdout)
            proposal = report['steps'][2]['results'][-1]
            self.assertEqual(proposal['code'], 0, proposal['output'])
            self.assertIn('review approve', proposal['output'])

    def assert_clean_pass(self, done):
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(done.stderr, '')
        self.assertNotRegex(done.stdout.lower(), r'is missing|unexpected|traceback')
        self.assertTrue(done.stdout.rstrip().endswith('PASS'))
        self.assertNotIn(MARKER, done.stdout + done.stderr)

    def test_demo_without_config_in_the_pack_with_team_dir_outside_it(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pack = copy_pack(root)
            self.assertFalse((pack / 'team').exists())
            other = root / 'other-team'
            other.mkdir()
            (other / 'config.ini').write_text(MARKED_CONFIG)
            self.assert_clean_pass(run_demo(pack, other))

    def test_demo_ignores_config_of_the_pack(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pack = copy_pack(root, MARKED_CONFIG)
            other = root / 'other-team'
            other.mkdir()
            (other / 'config.ini').write_text(MARKED_CONFIG)
            self.assert_clean_pass(run_demo(pack, other))

    def test_disposable_team_gets_only_its_own_minimal_config(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pack = copy_pack(root, MARKED_CONFIG)
            fixture = root / 'fixture'
            code = ('import sys\nfrom pathlib import Path\n'
                    'sys.path.insert(0, sys.argv[1])\n'
                    'from teamlib import demo\n'
                    'root = Path(sys.argv[2])\n'
                    'root.mkdir()\n'
                    'with demo.isolated(root):\n'
                    '    demo.exercise(root)\n')
            done = subprocess.run([sys.executable, '-c', code, str(pack / 'bin'), str(fixture)],
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            for path in fixture.rglob('*'):
                if path.is_file():
                    self.assertNotIn(MARKER.encode(), path.read_bytes(), str(path))
            config = (fixture / 'team' / 'config.ini').read_text()
            self.assertNotIn('[', config)
            for word in ('engine', 'docker', 'runner', 'network', 'image'):
                self.assertNotIn(word, config)

    def test_broken_gate_fails_demo(self):
        demo = importlib.import_module('teamlib.demo')
        with patch('teamlib.gate.cmd_gate', return_value=0), contextlib.redirect_stdout(io.StringIO()) as out:
            code = demo.do_demo(['--json'])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out.getvalue())['ok'])

    def demo_with(self, path, *extra, env=None):
        return subprocess.run([sys.executable, str(BIN / 'team'), 'demo', *extra], capture_output=True, text=True,
                              timeout=60, env={**(env or os.environ), 'PATH': path})

    def test_receipt_step_makes_and_verifies_a_receipt_and_refuses_a_tampered_copy(self):
        self.need_a_key()
        with tempfile.TemporaryDirectory() as folder:
            done = subprocess.run([sys.executable, str(BIN / 'team'), 'demo', '--json'],
                                  cwd=folder, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        step = json.loads(done.stdout)['steps'][3]
        self.assertTrue(step['ok'], step)
        self.assertFalse(step['skipped'])
        commands = [r['command'] for r in step['results']]
        self.assertTrue(any(c.startswith('ssh-keygen -q -t ed25519 ') for c in commands), commands)
        self.assertTrue(any(c.startswith('team receipt make T-1 --reviewed ') for c in commands), commands)
        made = next(r for r in step['results'] if r['command'].startswith('team receipt make'))
        self.assertEqual(made['code'], 0, made['output'])
        self.assertIn('stored team/receipts/', made['output'])
        good, bad = step['verify']['receipt'], step['verify']['tampered']
        self.assertEqual(good['code'], 0, good['output'])
        self.assertTrue(good['command'].startswith('team verify '), good['command'])
        self.assertIn('ok signature: SHA256:', good['output'])
        self.assertIn('ok brief: brief hash', good['output'])
        self.assertTrue(good['output'].splitlines()[-1].startswith('verified '), good['output'])
        self.assertIn('1 approval(s) on H', good['output'])
        self.assertNotEqual(bad['code'], 0, bad['output'])
        self.assertIn('tampered.json', bad['command'])
        self.assertEqual(bad['output'].splitlines()[-1], 'refused: signature: invalid')
        self.assertEqual(sum(line.startswith('refused: ') for line in bad['output'].splitlines()), 1)
        self.assertIn('reviews.rounds[0].bot', step['tampered'])
        for text in PRIVATE_KEY_TEXT:
            self.assertNotIn(text, done.stdout)

    def test_receipt_step_prints_its_commands_and_no_private_key(self):
        self.need_a_key()
        with tempfile.TemporaryDirectory() as folder:
            done = subprocess.run([sys.executable, str(BIN / 'team'), 'demo'], cwd=folder,
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        receipt = done.stdout.split('\n4. ', 1)[1]
        for line in ('$ ssh-keygen -q -t ed25519 ', '$ team receipt make T-1 ', '$ team verify ',
                     '$ team verify tampered.json ', 'refused: signature: invalid', 'exit 1'):
            self.assertIn(line, receipt)
        self.assertTrue(done.stdout.rstrip().endswith('PASS'))
        for text in PRIVATE_KEY_TEXT:
            self.assertNotIn(text, done.stdout + done.stderr)

    def test_receipt_step_is_skipped_without_ssh_keygen(self):
        with tempfile.TemporaryDirectory() as folder:
            tools = Path(folder) / 'bin'
            tools.mkdir()
            for place in os.environ['PATH'].split(os.pathsep):  # this PATH, but ssh-keygen
                for tool in Path(place).glob('*') if Path(place).is_dir() else []:
                    if tool.name != 'ssh-keygen' and not os.path.lexists(tools / tool.name):
                        (tools / tool.name).symlink_to(tool)
            self.assertIsNone(shutil.which('ssh-keygen', path=str(tools)))
            self.assertIsNotNone(shutil.which('git', path=str(tools)))
            done = self.demo_with(str(tools))
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertIn('\nreceipt: skipped, ssh-keygen not found\n', done.stdout)
            self.assertNotIn('$ ssh-keygen', done.stdout)
            self.assertTrue(done.stdout.rstrip().endswith('PASS'))
            done = self.demo_with(str(tools), '--json')
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            report = json.loads(done.stdout)
            self.assertTrue(report['ok'])
            step = report['steps'][3]
            self.assertEqual((step['ok'], step['skipped'], step['results'], step['verify']), (True, True, [], {}))

    def fake_keygen(self, folder, code):
        """PATH with an ssh-keygen running code ahead of everything else."""
        tools = Path(folder) / 'bin'
        tools.mkdir()
        fake = tools / 'ssh-keygen'
        fake.write_text(f'#!{sys.executable}\n' + code)
        fake.chmod(0o755)
        return f'{tools}{os.pathsep}{os.environ["PATH"]}'

    def assert_skipped_at_the_key(self, path, reason):
        done = self.demo_with(path)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn('\nreceipt: skipped, ' + reason + '\n', done.stdout)
        self.assertNotIn('$ git -C repo', done.stdout)
        self.assertNotIn('$ team receipt', done.stdout)
        self.assertTrue(done.stdout.rstrip().endswith('PASS'))
        done = self.demo_with(path, '--json')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        report = json.loads(done.stdout)
        self.assertTrue(report['ok'])
        step = report['steps'][3]
        self.assertEqual((step['ok'], step['skipped'], step['reason'], step['verify']), (True, True, reason, {}))
        self.assertEqual([r['command'].split()[0] for r in step['results']], ['ssh-keygen'], step['results'])
        self.assertNotEqual(step['results'][0]['code'], 0)

    def test_receipt_step_is_skipped_when_ssh_keygen_cannot_make_a_key(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assert_skipped_at_the_key(self.fake_keygen(folder, KEYGEN_NO_USER),
                                           'ssh-keygen cannot make a key here: No user exists for uid 0')

    def test_receipt_step_is_skipped_when_the_fingerprint_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assert_skipped_at_the_key(self.fake_keygen(folder, "FAIL = '-l'\n" + KEYGEN_FAILS_AT),
                                           'ssh-keygen cannot make a key here: fake ssh-keygen: cannot -l')

    def test_receipt_step_fails_the_demo_when_signing_fails_after_the_key_is_made(self):
        with tempfile.TemporaryDirectory() as folder:
            done = self.demo_with(self.fake_keygen(folder, "FAIL = '-Y'\n" + KEYGEN_FAILS_AT), '--json')
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        report = json.loads(done.stdout)
        self.assertFalse(report['ok'])
        step = report['steps'][3]
        self.assertEqual((step['ok'], step['skipped']), (False, False), step)
        made = next(r for r in step['results'] if r['command'].startswith('team receipt make T-1 '))
        self.assertEqual(made['code'], 1, made)
        self.assertIn('ssh-keygen could not sign the receipt', made['output'])

    def test_receipt_step_never_touches_the_callers_keys_agent_or_files(self):
        self.need_a_key()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / 'home'
            (home / '.ssh').mkdir(parents=True)
            for name in ('id_ed25519', 'id_ed25519.pub', 'known_hosts', 'config', 'allowed_signers'):
                (home / '.ssh' / name).write_text(f'the caller\'s {name}\n')
            # bin/team itself, on main too, makes this empty folder at start (teamlib.update): not the demo.
            (home / '.config' / 'wouf' / 'secrets').mkdir(parents=True)
            (root / 'agent.sock').write_text('not a socket: an agent the demo must not use\n')
            tools = root / 'bin'
            tools.mkdir()
            spy = tools / 'ssh-keygen'
            spy.write_text(f'#!{sys.executable}\nREAL = {shutil.which("ssh-keygen")!r}\n' + KEYGEN_SPY)
            spy.chmod(0o755)
            log = Path(f'{spy}.log')
            before = snapshot(root)
            env = {**os.environ, 'HOME': str(home), 'SSH_AUTH_SOCK': str(root / 'agent.sock')}
            done = subprocess.run([sys.executable, str(BIN / 'team'), 'demo', '--json'], cwd=home,
                                  capture_output=True, text=True, timeout=60,
                                  env={**env, 'PATH': f'{tools}{os.pathsep}{os.environ["PATH"]}'})
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertTrue(json.loads(done.stdout)['steps'][3]['ok'])
            self.assertEqual(before, snapshot(root, skip={log}))
            calls = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertGreaterEqual(len(calls), 4, calls)  # make the key, its fingerprint, sign, verify (twice)
        self.assertTrue(any(c['argv'][:2] == ['-Y', 'sign'] for c in calls), calls)
        self.assertTrue(any(c['argv'][:2] == ['-Y', 'verify'] for c in calls), calls)
        temp = {c['home'] for c in calls}
        self.assertEqual(len(temp), 1, temp)
        temp = Path(temp.pop())
        self.assertTrue(temp.name.startswith('wouf-demo-'), temp)
        self.assertFalse(temp.exists(), 'the temp folder, the throwaway key with it, is deleted')
        for call in calls:
            self.assertIsNone(call['agent'], call)
            keys = [arg for at, arg in enumerate(call['argv']) if at and call['argv'][at - 1] == '-f']
            if call['argv'][:2] != ['-Y', 'verify']:  # verify's -f is a copy of the signers file, in its own folder
                self.assertTrue(keys and all(temp in (Path(call['cwd']) / key).parents for key in keys), call)
            self.assertNotIn(str(home), ' '.join(call['argv']), call)

    @unittest.skipUnless(subprocess.run(['unshare', '-rn', 'true'], capture_output=True).returncode == 0
                         if shutil.which('unshare') else False, 'no unprivileged network namespace (unshare -rn)')
    def test_receipt_step_needs_no_network(self):
        self.need_a_key()
        with tempfile.TemporaryDirectory() as folder:
            done = subprocess.run(['unshare', '-rn', sys.executable, str(BIN / 'team'), 'demo', '--json'],
                                  cwd=folder, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        report = json.loads(done.stdout)
        self.assertTrue(report['ok'])
        self.assertTrue(report['steps'][3]['ok'])
        if report['steps'][3]['skipped']:
            # unshare -r maps the caller to uid 0, which may have no passwd entry here (a gate sandbox): the demo
            # skips its key step there by design, so this environment cannot show the offline run.
            self.skipTest('ssh-keygen cannot make a key inside unshare -rn here')
