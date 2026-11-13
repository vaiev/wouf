"""Numbers-only lead usage, measured from synthetic Claude Code transcripts."""
from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

BIN = Path(__file__).resolve().parents[1] / '.agents' / 'bin'
sys.path.insert(0, str(BIN))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import isolate_wouf_env  # noqa: E402


def setUpModule() -> None:
    isolate_wouf_env()


class Stats(unittest.TestCase):
    def setUp(self):
        self.stats = importlib.import_module('teamlib.stats')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.transcripts = self.root / 'transcripts'
        self.transcripts.mkdir()
        self.events = self.root / 'events'
        self.events.mkdir()
        # A home and team of its own: the caller's lead log, transcripts and config must not reach the report.
        (self.root / 'home').mkdir()
        env = patch.dict(os.environ, WOUF_EVENTS_DIR=str(self.events), HOME=str(self.root / 'home'),
                         WOUF_TEAM_DIR=str(self.root / 'team'), WOUF_LOG_DIR=str(self.root / 'team' / 'logs'),
                         XDG_CONFIG_HOME=str(self.root / 'home' / '.config'))
        env.start()
        self.addCleanup(env.stop)

    def session(self, name='session', tokens=10, days=0, action='.agents/bin/team run T-1'):
        start = datetime.now(timezone.utc) - timedelta(days=days)
        def row(kind, seconds, message):
            return {'type': kind, 'timestamp': (start + timedelta(seconds=seconds)).isoformat(), 'message': message}
        usage = {'input_tokens': tokens, 'cache_read_input_tokens': 20,
                 'cache_creation_input_tokens': 30, 'output_tokens': 4}
        rows = [row('user', 0, {'role': 'user', 'content': 'synthetic message'}),
                row('assistant', 1, {'id': 'a', 'usage': usage, 'content': [
                    {'type': 'tool_use', 'name': 'Bash', 'input': {'command': '.agents/bin/team context'}}]}),
                row('assistant', 2, {'id': 'b', 'usage': usage, 'content': [
                    {'type': 'tool_use', 'name': 'Bash', 'input': {'command': '.agents/bin/team status'}}]}),
                row('assistant', 3, {'id': 'c', 'usage': usage, 'content': [
                    {'type': 'tool_use', 'name': 'Bash', 'input': {'command': action}}]})]
        path = self.transcripts / (name + '.jsonl')
        path.write_text(''.join(json.dumps(r) + '\n' for r in rows))
        return path

    def test_bot_runs_follow_the_log_folder_rule(self):
        line = {'t': datetime.now(timezone.utc).isoformat(), 'who': 'lead', 'task': 'T-1', 'outcome': 'done',
                'signal': 'worker returned', 'cost': 'tokens=1/2/3/4'}
        for folder in (self.root / 'other-logs', self.root / 'team' / 'logs'):
            folder.mkdir(parents=True)
            (folder / 'lead.jsonl').write_text(json.dumps(line) + '\n')
            with patch.dict(os.environ, WOUF_LOG_DIR=str(folder)):
                self.assertEqual([r[:2] for r in self.stats.bot_runs(None)], [('worker', 'T-1')])
            (folder / 'lead.jsonl').unlink()
        self.assertEqual(self.stats.bot_runs(None), [])  # the override's lines never come from the team folder

    def run_stats(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.stats.cmd_stats(['--transcripts', str(self.transcripts), *args])
        self.assertEqual(code, 0)
        return out.getvalue(), err.getvalue()

    def test_medians_cache_separate_and_last_ten(self):
        for i in range(12):
            self.session(str(i), tokens=i, days=12-i)
        result = json.loads(self.run_stats('--json')[0])
        self.assertEqual(result['sessions'], 12)
        m1 = result['m1_first_turn']
        self.assertEqual(m1['median']['input_tokens'], 5.5)
        self.assertEqual(m1['last_10_median']['input_tokens'], 6.5)
        self.assertEqual(m1['median']['cache_read_input_tokens'], 20)
        self.assertEqual(m1['median']['cache_creation_input_tokens'], 30)
        self.assertEqual(result['m2_first_action']['median']['seconds'], 3)
        self.assertEqual(result['m2_first_action']['median']['input_tokens'], 16.5)

    def test_first_useful_action_and_write(self):
        self.assertFalse(self.stats.useful({'name': 'Bash', 'input': {'command': '.agents/bin/team help'}}))
        self.assertFalse(self.stats.useful({'name': 'Read', 'input': {'file_path': '.agents/team/env/lead/a'}}))
        self.assertTrue(self.stats.useful({'name': 'Write', 'input': {'file_path': '/repo/.agents/team/env/lead/T-1/in/BRIEF.md'}}))
        self.session()
        result = json.loads(self.run_stats('--json')[0])
        self.assertEqual(result['m2_first_action']['median']['input_tokens'], 30)
        self.assertEqual(result['m2_first_action']['median']['seconds'], 3)

    def test_since_and_malformed_unknown(self):
        self.session('old', days=40)
        self.session('new', days=1)
        (self.transcripts / 'other.jsonl').write_text('{broken\n' + json.dumps({'type': 'response_item'}) + '\n')
        result = json.loads(self.run_stats('--since', '30', '--json')[0])
        self.assertEqual(result['sessions'], 1)
        self.assertEqual(result['skipped'], 1)
        (self.transcripts / 'old.jsonl').unlink()
        (self.transcripts / 'new.jsonl').unlink()
        result = json.loads(self.run_stats('--json')[0])
        self.assertEqual(result['m1_first_turn']['median']['input_tokens'], 'unknown')

    def test_privacy_record_and_deduplication(self):
        path = self.session()
        fake = 'sk-' + 'ant-' + 'X'*24
        personal = 'Synthetic Person: fictional private note'
        with path.open('a') as handle:
            handle.write(json.dumps({'type': 'user', 'message': {'content': fake + personal}}) + '\n')
            handle.write(fake + personal + '\n')
        out, err = self.run_stats('--record', '--json')
        self.run_stats('--record', '--json')
        files = list(self.events.glob('*.jsonl'))
        self.assertEqual(len(files), 1)
        records = [json.loads(line) for line in files[0].read_text().splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['kind'], 'usage.session')
        written = ''.join(p.read_text() for p in self.events.iterdir() if p.is_file())
        for forbidden in (fake, personal, path.name):
            self.assertNotIn(forbidden, out + err + written)
        self.assertLessEqual(len(self.run_stats()[0].splitlines()), 15)

    def test_missing_usage_and_no_action_are_unknown(self):
        path = self.session(action='.agents/bin/team help')
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        for row in rows:
            row['message'].pop('usage', None)
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        result = json.loads(self.run_stats('--json')[0])
        self.assertEqual(result['m1_first_turn']['median']['input_tokens'], 'unknown')
        self.assertEqual(result['m2_first_action']['median']['seconds'], 'unknown')

    def test_mentions_are_not_actions_and_shell_write_is(self):
        for command in ('echo .agents/bin/team run T-1', '.agents/bin/team context',
                        "printf '%s' '.agents/bin/team run T-1'"):
            self.assertFalse(self.stats.useful({'name': 'Bash', 'input': {'command': command}}))
        for command in ('cd repo && .agents/bin/team run T-1',
                        'cd repo\n.agents/bin/team run T-1',
                        'python3 .agents/bin/team run T-1',
                        'cat > .agents/team/env/lead/T-1/in/BRIEF.md << EOF'):
            self.assertTrue(self.stats.useful({'name': 'Bash', 'input': {'command': command}}))

    def test_duplicate_assistant_records_are_counted_once(self):
        path = self.session()
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows.insert(2, rows[1])
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        result = json.loads(self.run_stats('--json')[0])
        self.assertEqual(result['m2_first_action']['median']['input_tokens'], 30)
        self.assertEqual(result['m1_first_turn']['session_totals']['input_tokens'], 30)

    def test_cli_registry_and_no_default_transcript_path(self):
        import subprocess
        result = subprocess.run([sys.executable, str(BIN / 'team'), 'stats', '--json'],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--transcripts', result.stderr)
        self.session()
        result = subprocess.run([sys.executable, str(BIN / 'team'), 'stats', '--transcripts',
                                 str(self.transcripts), '--json'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['sessions'], 1)

    def test_m2_excludes_turns_before_first_user(self):
        path = self.session()
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        earlier = dict(rows[1])
        earlier['message'] = dict(rows[1]['message'], id='prior')
        path.write_text(''.join(json.dumps(row) + '\n' for row in [earlier, *rows]))
        result = json.loads(self.run_stats('--json')[0])
        self.assertEqual(result['m2_first_action']['median']['input_tokens'], 30)
