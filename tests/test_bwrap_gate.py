"""Gate isolation in a real bubblewrap sandbox."""
import json
import sys
import tempfile
from unittest.mock import patch
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_team_cli import KEY, Project
from test_team_cli_gate import PLANT, VICTIM, branch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '.agents/bin'))
from teamlib import bwrap


class GateSetup(unittest.TestCase):
    def setUp(self):
        isolated = tempfile.TemporaryDirectory()
        self.addCleanup(isolated.cleanup)
        home = Path(isolated.name) / 'home'
        home.mkdir()
        with patch.dict('os.environ', {'HOME': str(home), 'WOUF_TEAM_DIR': isolated.name}):
            self.p = Project(mode='bwrap', runner='fake')
        self.addCleanup(self.p.close)
        self.p.env['HOME'] = str(home)
        self.p.env['WOUF_TEAM_DIR'] = str(self.p.root / '.agents/team')
        self.p.git('branch', '-M', 'main')
        self.p.config.write_text(self.p.config.read_text() + '\n[flow]\ngate = true\n')

    def gate(self, command, task='T-1', ref='main', *options):
        text = self.p.config.read_text().split('\n[flow]')[0]
        self.p.config.write_text(text + '\n[flow]\ngate = ' + command + '\n')
        return self.p.team('gate', task, '--repo', ref, '--base', 'main', *options)

    def test_bridge_is_refused(self):
        self.p.config.write_text(self.p.config.read_text() + 'gate_network = bridge\n')
        result = self.p.team('gate', 'T-1', '--repo', 'main', '--base', 'main')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('none', result.stderr + result.stdout)


@unittest.skipUnless(bwrap.bwrap_usable(), 'bwrap cannot start a sandbox here')
class BwrapGateTest(GateSetup):
    def ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_an_earlier_gate_cannot_change_a_later_checkout_or_test(self):
        base = self.p.git('rev-parse', 'HEAD').strip()
        branch(self.p, 'attack', {'gate.sh': PLANT})
        branch(self.p, 'victim', {'gate.sh': VICTIM})
        self.p.config.write_text(self.p.config.read_text().split('\n[flow]')[0] + '\n[flow]\ngate = sh gate.sh\n')
        for task, ref in [('T-81', 'attack'), ('T-82', 'victim')]:
            result = self.p.team('gate', task, '--repo', ref, '--base', base)
            self.assertEqual(result.returncode, 0 if task == 'T-81' else 1, result.stdout + result.stderr)
        state = json.loads((self.p.root / '.agents/team/env/gate/T-82/gate.json').read_text())
        self.assertEqual((state['code'], state['head']), (7, self.p.git('rev-parse', 'victim').strip()))

    def test_no_key_host_environment_or_network(self):
        self.p.store_secret('ALPHA_KEY', KEY)
        self.p.env['WOUF_CANARY'] = 'host-env-canary'
        self.p.env['ALPHA_KEY'] = KEY
        host_key = Path(self.p.env['HOME']) / 'host-key'
        host_key.write_text(KEY)
        command = ('test -z "${ALPHA_KEY-}${WOUF_CANARY-}" && test ! -e "$HOME/.ssh" && '
                   f'test ! -e {host_key} && '
                   'test "$(tail -n +3 /proc/net/dev | cut -d: -f1 | tr -d " ")" = lo && test ! -s "$HOME/.gitconfig" && '
                   'test ! -w /bot/hooks && test ! -w "$BUNDLE"')
        self.ok(self.gate(command))

    def test_cache_warm_fresh_and_branch_rule(self):
        self.ok(self.gate('echo trusted > "$WOUF_CACHE/value"', 'T-W', 'main', '--warm-cache'))
        self.ok(self.gate('test "$(cat "$WOUF_CACHE/value")" = trusted && echo bad > "$WOUF_CACHE/value"'))
        self.ok(self.gate('test "$(cat "$WOUF_CACHE/value")" = trusted'))
        self.ok(self.p.team('gate', '--fresh-cache'))
        self.ok(self.gate('test ! -e "$WOUF_CACHE/value"'))
        self.p.git('checkout', '-qb', 'task')
        (self.p.root / 'file.txt').write_text('branch\n')
        self.p.git('commit', '-qam', 'branch')
        result = self.gate('true', 'T-W', 'task', '--warm-cache')
        self.assertIn('reachable from main', result.stdout + result.stderr)
        self.assertNotEqual(result.returncode, 0)
