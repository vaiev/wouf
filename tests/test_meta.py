"""team meta: chains of steps that repeat across the run records of a fixture team folder (events, receipts,
run.json and result.json), read only and masked."""
from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

BIN = Path(__file__).resolve().parents[1] / '.agents' / 'bin'
sys.path.insert(0, str(BIN))
ORIGIN = '0123456789ab'
# A registered value of the fixture's secret store, fictional; it is never a real key.
SECRET = 'fixture-registered-value-7Qx2'


def iso(days, seconds=0):
    return (datetime.now(timezone.utc) - timedelta(days=days) + timedelta(seconds=seconds)).isoformat()


class Meta(unittest.TestCase):
    def setUp(self):
        self.meta = importlib.import_module('teamlib.meta')
        self.core = importlib.import_module('teamlib.core')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.team = Path(self.temp.name) / 'team'
        for folder in ('events', 'logs', 'env'):
            (self.team / folder).mkdir(parents=True)
        caller = {k: v for k, v in os.environ.items() if not k.startswith('WOUF_')}
        env = patch.dict(os.environ, {**caller, 'WOUF_TEAM_DIR': str(self.team)}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        known = list(self.core.KNOWN)
        self.addCleanup(lambda: self.core.KNOWN.__setitem__(slice(None), known))
        self.seq = 0

    # ---- the fixture's records, in the shapes teamlib.run and bin/log write them

    def event(self, task, bot, outcome, when, tokens='10/20/30/40'):
        self.seq += 1
        t = datetime.fromisoformat(when).strftime('%Y-%m-%dT%H:%M:%SZ')
        data = {'bot': bot, 'runner': 'claude', 'tier': 'strong', 'model': 'default', 'effort': 'default',
                'outcome': outcome, 'tokens': tokens}
        line = {'v': 1, 'id': f'{ORIGIN}-{self.seq}', 'seq': self.seq, 't': t, 'origin': ORIGIN, 'actor': 'lead',
                'kind': 'run.finished', 'task': task, 'scope': 'local', 'data': data}
        with (self.team / 'events' / f'{ORIGIN}.jsonl').open('a') as handle:
            handle.write(json.dumps(line, separators=(',', ':')) + '\n')

    def receipt(self, task, outcome, signal, when, cost=''):
        entry = {'t': when, 'who': 'lead', 'task': task, 'outcome': outcome, 'signal': signal, 'ref': '', 'cost': cost}
        with (self.team / 'logs' / 'lead.jsonl').open('a') as handle:
            handle.write(json.dumps(entry) + '\n')

    def stage(self, member, task, role, state, ended, tokens='1/1/1/1'):
        folder = self.team / 'env' / member / 'tasks' / task
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'run.json').write_text(json.dumps({'member': member, 'task': task, 'role': role}))
        (folder / 'result.json').write_text(json.dumps({'state': state, 'ended': ended, 'tokens': tokens}))

    def flow(self, task, days, review='done'):
        """scout, worker, gate, review: the runs as events, the gate as the lead's receipt."""
        self.event(task, 'scout', 'done', iso(days, 0), '1/2/3/4')
        self.event(task, 'worker', 'done', iso(days, 100), '100/200/300/400')
        self.receipt(task, 'done', 'gate green: all checks passed', iso(days, 200))
        self.event(task, 'review', review, iso(days, 300), '5/5/5/5')

    def run_meta(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.meta.cmd_meta(list(args))
        self.assertEqual(code, 0, err.getvalue())
        return out.getvalue()

    def chains(self, *args):
        return {item['chain']: item for item in json.loads(self.run_meta('--json', *args))['chains']}

    # ---- the acceptance criteria

    def test_repeated_chain_found_with_count_and_tokens(self):
        for i in range(3):
            self.flow(f'T-{i+1}', days=i+1)
        chains = self.chains()
        chain = 'scout/scout:done > worker/worker:done > lead/gate:done > review/review:done'
        self.assertIn(chain, chains)
        found = chains[chain]
        self.assertEqual(found['count'], 3)
        self.assertEqual(found['tasks'], ['T-1', 'T-2', 'T-3'])
        self.assertEqual(found['tokens'], 3 * (10 + 1000 + 20))
        self.assertEqual(found['command'], 'team-scout-worker-gate-review')
        self.assertEqual(len(found['steps']), 4)
        self.assertEqual(found['steps'][2], {'bot': 'lead', 'role': 'gate', 'outcome': 'done'})
        text = self.run_meta()
        self.assertIn(chain, text)
        self.assertIn('team-scout-worker-gate-review', text)

    def test_chain_under_min_is_not_shown(self):
        for i in range(3):
            self.flow(f'T-{i+1}', days=1, review='done' if i < 2 else 'failed')
        chains = self.chains()
        self.assertNotIn('scout/scout:done > worker/worker:done > lead/gate:done > review/review:done', chains)
        self.assertEqual(chains['scout/scout:done > worker/worker:done > lead/gate:done']['count'], 3)
        self.assertIn('scout/scout:done > worker/worker:done > lead/gate:done > review/review:done',
                      self.chains('--min', '2'))
        self.assertNotIn('worker/worker:done > lead/gate:done', self.chains('--min', '4'))

    def test_since_cuts_old_runs(self):
        self.flow('T-1', days=1)
        self.flow('T-2', days=2)
        self.flow('T-3', days=20)
        self.assertEqual(self.chains(), {})
        self.assertEqual(self.chains('--since', '30')[
            'scout/scout:done > worker/worker:done > lead/gate:done > review/review:done']['tasks'],
            ['T-1', 'T-2', 'T-3'])
        self.assertEqual(self.chains('--since', '30', '--min', '2'), self.chains('--since', '30'))
        self.assertEqual(len(self.chains('--since', '3', '--min', '2')), 1)

    def test_json_has_the_schema_and_ranking(self):
        for i in range(4):
            task = f'T-{i+1}'
            self.receipt(task, 'done', 'gate green', iso(1, 10*i))
            self.receipt(task, 'failed', 'gate red: tests', iso(1, 10*i + 1))
            self.receipt(task, 'done', 'merge marked', iso(1, 10*i + 2))
        for i in range(3):
            self.flow(f'T-{i+10}', days=1)
        result = json.loads(self.run_meta('--json'))
        self.assertEqual(result['schema'], 'wouf.meta.v1')
        self.assertEqual((result['since_days'], result['min']), (14, 3))
        self.assertEqual(set(result['chains'][0]), {'chain', 'steps', 'count', 'tasks', 'tokens', 'command'})
        tokens = [(c['tokens'], c['count']) for c in result['chains']]
        self.assertEqual(tokens, sorted(tokens, reverse=True))
        self.assertEqual(result['chains'][0]['chain'],
                         'scout/scout:done > worker/worker:done > lead/gate:done > review/review:done')
        # The receipts' chain carries no tokens: last, the longer count first.
        self.assertEqual(result['chains'][-1]['tokens'], 0)
        self.assertEqual(result['chains'][-1]['count'], 4)

    def test_registered_secret_in_a_record_is_masked(self):
        self.core.register([SECRET])
        for i in range(3):
            task = f'T-{i+1}'
            self.stage('worker', task, f'worker-{SECRET}', 'done', iso(1, 10*i))
            self.receipt(task, 'done', f'gate {SECRET} green', iso(1, 10*i + 5))
        for out in (self.run_meta(), self.run_meta('--json')):
            self.assertNotIn(SECRET, out)
            self.assertNotIn(SECRET.lower(), out)
            self.assertIn('***', out)

    def test_registered_secret_past_the_field_length_is_masked_before_the_cut(self):
        self.core.register([SECRET])
        role = 'r' * (self.meta.FIELD - 20) + SECRET
        for i in range(3):
            self.stage('worker', f'T-{i+1}', role, 'done', iso(1, 10*i))
            self.receipt(f'T-{i+1}', 'done', 'gate green', iso(1, 10*i + 5))
        for out in (self.run_meta(), self.run_meta('--json')):
            self.assertNotIn(SECRET[:20], out)
            self.assertIn('***', out)

    # ---- how the records are read

    def test_one_run_in_three_records_counts_once(self):
        for i in range(3):
            task, when = f'T-{i+1}', iso(1, 100*i)
            self.event(task, 'worker', 'done', when, '1/1/1/1')
            self.receipt(task, 'done', 'worker returned, branch wouf/' + task, when, 'tokens=1/1/1/1')
            self.stage('worker', task, 'worker', 'done', when, '1/1/1/1')
            self.receipt(task, 'started', 'flow standard on main', iso(1, 100*i + 1))
            self.receipt(task, 'done', 'gate green', iso(1, 100*i + 2))
        chains = self.chains()
        self.assertEqual(list(chains), ['worker/worker:done > lead/gate:done'])
        self.assertEqual(chains['worker/worker:done > lead/gate:done']['tokens'], 12)

    def test_receipt_and_stage_alone_give_runs_and_usage_line(self):
        for i in range(3):
            task = f'T-{i+1}'
            self.receipt(task, 'done', 'planner returned', iso(1, 10*i), 'tokens=2/2/2/2')
            self.stage('worker', task, 'worker', 'done', iso(1, 10*i + 5), tokens='unknown')
            (self.team / 'env' / 'worker' / 'tasks' / task / 'output.log').write_text(
                'work\nwouf-usage tokens=5/5/5/5 skipped=0\n')
        chains = self.chains()
        self.assertEqual(chains['planner/planner:done > worker/worker:done']['tokens'], 3 * (8 + 20))

    def test_writes_nothing(self):
        for i in range(3):
            self.flow(f'T-{i+1}', days=1)
        before = sorted((str(p), p.stat().st_mtime_ns) for p in self.team.rglob('*'))
        self.run_meta()
        self.run_meta('--json')
        self.assertEqual(sorted((str(p), p.stat().st_mtime_ns) for p in self.team.rglob('*')), before)

    def test_bad_arguments(self):
        for args in (['--since', '-1'], ['--min', '0'], ['--min', 'x'], ['--what']):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    self.meta.cmd_meta(args)

    def test_registered_as_a_team_command(self):
        for i in range(3):
            self.flow(f'T-{i+1}', days=1)
        done = subprocess.run([sys.executable, str(BIN / 'team'), 'meta', '--json'], capture_output=True, text=True,
                              env=os.environ.copy())
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)['schema'], 'wouf.meta.v1')
        done = subprocess.run([sys.executable, str(BIN / 'team'), 'help'], capture_output=True, text=True,
                              env=os.environ.copy())
        self.assertIn('team meta [--since DAYS] [--min N] [--json]', done.stdout)


if __name__ == '__main__':
    unittest.main()
