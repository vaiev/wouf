"""The demo exercises real safeguards and leaves its caller untouched."""
import contextlib
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

BIN = Path(__file__).resolve().parents[1] / '.agents' / 'bin'
sys.path.insert(0, str(BIN))
MARKER = 'MARKER-team-config-of-another-team'
MARKED_CONFIG = f'[team]\nname = {MARKER}\n[environment]\nengine = {MARKER}\n'


def copy_pack(root, config=None):
    """A copy of this pack's bin folder under root/.agents, with the team folder of the pack only if config is given."""
    pack = root / '.agents'
    shutil.copytree(BIN, pack / 'bin', ignore=shutil.ignore_patterns('__pycache__'))
    if config is not None:
        (pack / 'team').mkdir()
        (pack / 'team' / 'config.ini').write_text(config)
    return pack


def run_demo(pack, team_dir=None):
    env = dict(os.environ)
    env.pop('WOUF_TEAM_DIR', None)
    if team_dir is not None:
        env['WOUF_TEAM_DIR'] = str(team_dir)
    return subprocess.run([sys.executable, str(pack / 'bin' / 'team'), 'demo'], cwd=pack.parent,
                          capture_output=True, text=True, timeout=60, env=env)


class DemoTest(unittest.TestCase):
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
                bad, stale, good = report['steps']
                self.assertEqual(bad['model_calls'], 0)
                self.assertIn('outside Paths: outside.txt', bad['results'][0]['output'])
                self.assertEqual(stale['review']['verdict'], 'stale')
                refusal = stale['results'][-1]
                self.assertNotEqual(refusal['code'], 0)
                self.assertIn(stale['head'], refusal['output'])
                self.assertIn(stale['approved_head'][:12], refusal['output'])
                self.assertTrue(all(r['code'] == 0 for r in good['results']))
                self.assertEqual(good['review']['verdict'], 'approve')
                self.assertTrue(all(r['command'] for s in report['steps'] for r in s['results']))
                self.assertEqual(before, sorted((str(p.relative_to(root)), p.read_bytes() if p.is_file() else None)
                                               for p in root.rglob('*')))
            self.assertLess(time.monotonic() - start, 10)
            done = subprocess.run([sys.executable, str(BIN / 'team'), 'demo'], cwd=root,
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(done.stderr, '')
            self.assertNotRegex(done.stdout.lower(), r'is missing|error|traceback|flags|no merge check command exists')
            self.assertTrue(done.stdout.rstrip().endswith('PASS'))
            self.assertIn(report['flags'][0], done.stdout)
            self.assertIn('0 model calls', done.stdout)
            self.assertIn('team gate', done.stdout)
            self.assertIn('team merge propose', done.stdout)

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
