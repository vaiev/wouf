"""Append-only event validation, durability, isolation and process concurrency."""
from __future__ import annotations

import contextlib
import io
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_log import helper, LOG, HITS


def append_batch(directory, count):
    os.environ['WOUF_EVENTS_DIR'] = directory
    log = helper(LOG)
    for _ in range(count):
        log.event_append('session.started', 'lead', '', {'reason': 'startup'})


class Events(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.env = patch.dict(os.environ, WOUF_EVENTS_DIR=self.temp.name, WOUF_MASK='[]')
        self.env.start()
        self.addCleanup(self.env.stop)
        self.log = helper(LOG)

    def append(self, data=None, **kw):
        return self.log.event_append('session.started', kw.pop('actor', 'lead'), kw.pop('task', ''),
                                     {} if data is None else data, **kw)

    def cli(self, *args):
        return subprocess.run([sys.executable, str(LOG), 'event', *args], capture_output=True, text=True)

    def test_origin_creation_validation_and_sequence(self):
        self.assertFalse((self.directory / 'ORIGIN').exists())
        first, second = self.append(), self.append()
        origin = (self.directory / 'ORIGIN').read_text().strip()
        self.assertRegex(origin, r'^[0-9a-f]{12}$')
        self.assertEqual((self.directory / 'ORIGIN').stat().st_mode & 0o777, 0o644)
        self.assertEqual(first['id'], origin + '-1')
        self.assertEqual(second['seq'], 2)
        self.assertTrue(first['t'].endswith('Z'))
        for invalid in ['INVALID-CONTENT', '../escape', 'a'*13, 'a'*12+'\nextra']:
            (self.directory / 'ORIGIN').write_text(invalid)
            result = self.cli('add', 'session.started', '--actor', 'lead', '--data', '{}')
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(invalid, result.stderr)

    def test_concurrent_processes(self):
        ctx = multiprocessing.get_context('spawn')
        processes = [ctx.Process(target=append_batch, args=(self.temp.name, 200)) for _ in range(2)]
        for p in processes:
            p.start()
        for p in processes:
            p.join(60)
            if p.is_alive():
                p.terminate()
                p.join()
            self.assertEqual(p.exitcode, 0)
        lines = next(self.directory.glob('*.jsonl')).read_bytes().splitlines(keepends=True)
        events = [json.loads(line) for line in lines]
        self.assertEqual([e['seq'] for e in events], list(range(1, 401)))
        self.assertEqual(len({e['id'] for e in events}), 400)
        self.assertTrue(all(line.endswith(b'\n') for line in lines))
        self.assertEqual(self.cli('check').returncode, 0)

    def test_kind_data_allowlist_and_validation(self):
        samples = {'id': 'Q-1', 'head': 'abc1234', 'body': 'Lesson text', 'source': 'lead'}
        for kind, keys in self.log.EVENT_DATA_KEYS.items():
            data = {k: samples.get(k, 1) for k in keys}
            if kind == 'lesson.accepted':
                data['id'] = 'L-aaaaaaaaaaaa-1'
            self.log.event_append(kind, 'team', 'T-1', data)
            with self.assertRaises(SystemExit):
                self.log.event_append(kind, 'lead', '', {'unknown_field': 'x'})
        for args in [('unknown', 'lead', '', {}), ('task.state', '../bad', '', {}),
                     ('task.state', 'lead', '../bad', {}), ('task.state', 'lead', '', [])]:
            with self.assertRaises(SystemExit):
                self.log.event_append(*args)
        with self.assertRaises(SystemExit):
            self.append(scope='remote')
        with self.assertRaises(SystemExit):
            self.append({'reason': 'x'*501})

    def test_question_merge_and_extended_fields_are_validated(self):
        self.log.event_append('question.asked', 'worker', 'T-1', {'id': 'Q-12', 'text': 'Why?', 'from': 'worker'})
        self.log.event_append('decision.recorded', 'lead', '', {'number': 'D-3', 'status': 'accepted',
                                                                'decided_by': 'decide', 'note': 'kept'})
        self.log.event_append('schedule.item', 'lead', '', {'id': 's1', 'kind': 'cron', 'ending': 'never',
                                                            'last_run': '2026-10-27'})
        self.log.event_append('merge.proposed', 'review', 'T-1', {'head': 'abc1234', 'branch': 'task/T-1',
                                                                  'verdict': 'APPROVE', 'task': 'T-1'})
        self.log.event_append('merge.marked', 'lead', 'T-1', {'head': 'a' * 40, 'verdict': 'changes'})
        for kind, data in [
            ('question.asked', {'id': 'Q1', 'text': 'x', 'from': 'worker'}),
            ('question.answered', {'id': 1, 'text': 'x', 'from': 'worker'}),
            ('merge.proposed', {'head': 'ABC1234', 'verdict': 'APPROVE'}),
            ('merge.marked', {'head': 'abc123'}),
            ('merge.marked', {'head': 'a' * 41}),
            ('merge.marked', {'verdict': 'changes'}),
            ('decision.recorded', {'note': 'gh' + 'p_' + 'A' * 36}),
        ]:
            with self.subTest(kind=kind, data=data), self.assertRaises(SystemExit):
                self.log.event_append(kind, 'lead', 'T-1', data)
        self.assertEqual(len(list(self.directory.glob('*.jsonl'))), 1)

    def test_extended_fields_reach_the_projection(self):
        state = self.directory / 'state.sqlite'
        with patch.dict(os.environ, WOUF_STATE_DB=str(state), WOUF_LOG_DIR=str(self.directory / 'logs')):
            self.log.event_append('question.asked', 'worker', 'T-1', {'id': 'Q-12', 'text': 'Why?', 'from': 'worker'})
            self.log.event_append('decision.recorded', 'lead', '', {'number': 'D-3', 'status': 'accepted',
                                                                    'decided_by': 'decide', 'note': 'kept'})
            self.log.event_append('schedule.item', 'lead', '', {'id': 's1', 'kind': 'cron', 'ending': 'never',
                                                                'last_run': '2026-10-27'})
            merge = self.log.event_append('merge.proposed', 'review', 'T-1',
                                          {'head': 'abc1234', 'branch': 'task/T-1', 'verdict': 'APPROVE'})
            db = self.log.state_db()
            try:
                self.assertEqual(db.execute("SELECT qid FROM questions WHERE id='T-1:worker'").fetchone(), ('Q-12',))
                self.assertEqual(db.execute("SELECT status,decided_by,note FROM decisions WHERE id='D-3'").fetchone(),
                                 ('accepted', 'decide', 'kept'))
                self.assertEqual(db.execute("SELECT kind,ending,last_run FROM schedule WHERE id='s1'").fetchone(),
                                 ('cron', 'never', '2026-10-27'))
                self.assertEqual(db.execute('SELECT bot,outcome,verdict,head,branch FROM runs WHERE id=?',
                                            (merge['id'],)).fetchone(),
                                 ('review', 'proposed', 'APPROVE', 'abc1234', 'task/T-1'))
            finally:
                db.close()

    def test_secret_refusal_and_registered_mask(self):
        for examples in HITS.values():
            for value in examples:
                with self.assertRaises(SystemExit):
                    self.append({'reason': value})
        with self.assertRaises(SystemExit):
            self.append({'reason': 'Ab0Z'*10})
        self.assertFalse(list(self.directory.glob('*.jsonl')))
        with patch.object(self.log, 'REGISTERED', ['synthetic-value']):
            e = self.append({'reason': 'synthetic-value\t\x00\ntext'})
        self.assertEqual(e['data']['reason'], '***\ntext')
        self.assertNotIn('synthetic-value', next(self.directory.glob('*.jsonl')).read_text())

    def test_masking_cannot_invalidate_own_record(self):
        # Masked schema keys would make the reader reject our own event and reuse seq.
        with patch.object(self.log, 'REGISTERED', ['reason']):
            with self.assertRaises(SystemExit):
                self.append({'reason': 'startup'})
        self.assertFalse(list(self.directory.glob('*.jsonl')))
        # A short registered value can expand beyond the string bound when masked.
        with patch.object(self.log, 'REGISTERED', ['xy']):
            with self.assertRaises(SystemExit):
                self.append({'reason': 'xy.'*166})
        self.assertFalse(list(self.directory.glob('*.jsonl')))
        # A registered envelope key would be masked on the line itself: refused, so no line ever breaks the reader.
        with patch.object(self.log, 'REGISTERED', ['seq']):
            with self.assertRaises(SystemExit):
                self.append()
        self.assertFalse(list(self.directory.glob('*.jsonl')))

    def test_mask_rotation_cannot_reuse_sequence(self):
        self.append({'reason': 'xy.'*166})
        with patch.object(self.log, 'REGISTERED', ['xy']):
            self.assertEqual(self.append()['seq'], 2)
            self.assertEqual(self.log.event_read(next(self.directory.glob('*.jsonl')))[1], 0)
        self.append({'reason': {'alpha': 'a', 'beta': 'b'}})
        with patch.object(self.log, 'REGISTERED', ['alpha', 'beta']):
            self.assertEqual(self.append()['seq'], 4)
            self.assertEqual(self.log.event_read(next(self.directory.glob('*.jsonl')))[1], 0)

    def test_byte_limit_and_single_write_fsync(self):
        with patch.object(self.log.os, 'write', wraps=os.write) as write, \
                patch.object(self.log.os, 'fsync', wraps=os.fsync) as sync:
            self.append({'reason': 'ok'})
        # One event write (origin creation may also use os.write).
        event_writes = [c for c in write.call_args_list if c.args[1].startswith(b'{')]
        self.assertEqual(len(event_writes), 1)
        self.assertGreaterEqual(sync.call_count, 1)
        path = next(self.directory.glob('*.jsonl'))
        before = path.read_bytes()
        with self.assertRaises(SystemExit):
            self.append({'reason': ['é'*500]*5})
        self.assertEqual(path.read_bytes(), before)
        self.append({'reason': 'é'*500})
        self.assertTrue(all(len(line) <= 4096 for line in path.read_bytes().splitlines(keepends=True)))

    def test_torn_malformed_and_origin_mismatch(self):
        e = self.append()
        path = next(self.directory.glob('*.jsonl'))
        with path.open('ab') as f:
            f.write(b'broken\n')
            f.write((json.dumps(dict(e, origin='f'*12))+'\n').encode())
            f.write(json.dumps(e).encode())  # valid JSON without newline is torn too
        result = self.cli('list')
        self.assertEqual(len(result.stdout.splitlines()), 1)
        check = self.cli('check')
        self.assertEqual(check.returncode, 1)
        self.assertIn(path.name + ': 3', check.stdout)
        # Never join a fresh event onto a torn tail, or reuse a possibly committed seq.
        with self.assertRaises(SystemExit):
            self.append()

    def test_foreign_read_only_and_filters(self):
        own = self.append(task='T-1')
        origin = 'f'*12 if own['origin'] != 'f'*12 else 'e'*12
        foreign = self.directory / (origin + '.jsonl')
        foreign.write_text(json.dumps(dict(own, origin=origin, id=origin+'-1', scope='link'))+'\n')
        before = foreign.read_bytes()
        self.append()
        self.assertEqual(foreign.read_bytes(), before)
        result = self.cli('list', '--origin', origin, '--kind', 'session.started', '--task', 'T-1')
        self.assertEqual(json.loads(result.stdout)['origin'], origin)
        self.assertEqual(self.cli('list', '--since', '1', '--origin', origin).stdout, '')
        self.assertEqual(len(self.cli('list', '--since', '1').stdout.splitlines()), 1)

    def test_cli_add_and_masked_read(self):
        result = self.cli('add', 'task.state', '--actor', 'worker2', '--task', 'T-1',
                          '--scope', 'link', '--data', '{"state":"working"}')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['scope'], 'link')
        self.assertNotEqual(self.cli('add', 'task.state', '--actor', 'lead', '--data', '[]').returncode, 0)
        e = json.loads(result.stdout)
        path = next(self.directory.glob('*.jsonl'))
        value = HITS['GitHub classic'][0]
        path.write_text(json.dumps(dict(e, data={'state': value}))+'\n')
        result = self.cli('list')
        self.assertNotIn(value, result.stdout)
        self.assertEqual(json.loads(result.stdout)['data']['state'], '***')

    def test_exact_4096_byte_boundary(self):
        self.append()
        path = next(self.directory.glob('*.jsonl'))
        # Five 500-character strings with JSON newline escaping straddle 4 KiB.
        # seq stays one digit in both the successful and refused attempts.
        for size in (4096, 4097):
            base = self.log.event_append('session.started', 'lead', '',
                                         {'reason': ['x'*500]*5})
            baseline = len((json.dumps(base, ensure_ascii=False, separators=(',', ':'))+'\n').encode())
            extra = size - baseline
            values = ['x'*500 for _ in range(5)]
            for i in range(5):
                n = min(extra, 500)
                values[i] = '\n'*n + 'x'*(500-n)
                extra -= n
            self.assertEqual(extra, 0)
            before = path.read_bytes()
            if size == 4096:
                self.append({'reason': values})
                self.assertEqual(len(path.read_bytes().splitlines(keepends=True)[-1]), size)
            else:
                with self.assertRaises(SystemExit):
                    self.append({'reason': values})
                self.assertEqual(path.read_bytes(), before)

    def test_bad_record_does_not_hide_later_records(self):
        e = self.append()
        path = next(self.directory.glob('*.jsonl'))
        with path.open('ab') as handle:
            handle.write(b'not JSON\n')
        e = self.append()
        self.assertEqual(e['seq'], 2)
        result = self.cli('list')
        self.assertEqual([json.loads(x)['seq'] for x in result.stdout.splitlines()], [1, 2])
        self.assertEqual(self.cli('check').returncode, 1)

    def test_newline_controls_and_nested_secrets(self):
        e = self.append({'reason': ['first\nsecond\r\x1b\x85', {'text': 'ok'}]})
        self.assertEqual(e['data']['reason'][0], 'first\nsecond')
        self.assertEqual(len(next(self.directory.glob('*.jsonl')).read_bytes().splitlines()), 1)
        for data in ({'reason': {'note': HITS['GitHub classic'][0]}},
                     {'reason': float('nan')}, {'reason': float('inf')}):
            with self.assertRaises(SystemExit):
                self.append(data)

    def test_registered_number_is_refused_not_written(self):
        # A registered value sent as a JSON number would reach git unmasked and break the masked output.
        with patch.object(self.log, 'REGISTERED', ['4242424242424242']):
            with self.assertRaises(SystemExit):
                self.append({'total_tokens': 4242424242424242})
        self.assertFalse(list(self.directory.glob('*.jsonl')))
        with patch.object(self.log, 'REGISTERED', ['4242424242424242']):
            e = self.log.event_append('usage.session', 'lead', '', {'total_tokens': 12345})
        self.assertEqual(e['data'], {'total_tokens': 12345})
        env = dict(os.environ, WOUF_MASK='["4242424242424242"]')
        result = subprocess.run([sys.executable, str(LOG), 'event', 'add', 'usage.session', '--actor', 'lead',
                                 '--data', '{"total_tokens":4242424242424242}'],
                                capture_output=True, text=True, env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('4242424242424242', result.stdout + result.stderr)
        self.assertEqual(len(next(self.directory.glob('*.jsonl')).read_text().splitlines()), 1)

    def test_registered_number_on_disk_is_skipped_on_read(self):
        e = self.append()
        path = next(self.directory.glob('*.jsonl'))
        numbered = dict(e, seq=2, id=e['origin'] + '-2', kind='usage.session', data={'total_tokens': 4242424242424242})
        with path.open('ab') as handle:
            handle.write((json.dumps(numbered, separators=(',', ':')) + '\n').encode())
        with patch.object(self.log, 'REGISTERED', ['4242424242424242']):
            events, bad = self.log.event_read(path)
        self.assertEqual(bad, 1)
        self.assertEqual([x['seq'] for x in events], [1])

    def test_registered_value_only_visible_after_json_escaping_is_refused(self):
        # The per-field checks see the raw strings; the serialized line shows the escaped form.
        cases = [('Zq\\nWx-9', {'reason': 'Zq\nWx-9'}),      # real newline, JSON text backslash-n
                 ('pa\\"ss', {'reason': 'pa"ss'}),           # quote, JSON text backslash-quote
                 ('C:\\\\tmp', {'reason': 'C:\\tmp'})]      # backslash, JSON text doubled
        self.append()
        path = next(self.directory.glob('*.jsonl'))
        for registered, data in cases:
            before = path.read_bytes()
            with patch.object(self.log, 'REGISTERED', [registered]):
                with self.assertRaises(SystemExit):
                    self.append(data)
            self.assertEqual(path.read_bytes(), before, registered)

    def test_registered_json_literal_is_refused_not_written(self):
        # true and null are JSON text the per-field checks never look at.
        self.append()
        path = next(self.directory.glob('*.jsonl'))
        for registered, data in (('true', {'reason': True}), ('null', {'reason': None})):
            before = path.read_bytes()
            with patch.object(self.log, 'REGISTERED', [registered]):
                with self.assertRaises(SystemExit):
                    self.append(data)
            self.assertEqual(path.read_bytes(), before, registered)

    def test_registered_number_text_in_final_line_is_refused(self):
        self.append()
        path = next(self.directory.glob('*.jsonl'))
        before = path.read_bytes()
        with patch.object(self.log, 'REGISTERED', ['7']):
            with self.assertRaises(SystemExit):
                self.append({'total_tokens': 17})
        self.assertEqual(path.read_bytes(), before)

    def test_harmless_values_still_write_with_final_line_check(self):
        self.append()
        with patch.object(self.log, 'REGISTERED', ['Zq\\nWx-9', 'true', 'null']):
            e = self.append({'reason': 'plain words'})
        self.assertEqual(e['seq'], 2)
        self.assertEqual(len(next(self.directory.glob('*.jsonl')).read_bytes().splitlines()), 2)

    def test_list_output_stays_valid_json_when_a_registered_literal_is_on_disk(self):
        self.append({'reason': True})
        self.append({'reason': 'startup'})
        env = dict(os.environ, WOUF_MASK='["true"]')
        result = subprocess.run([sys.executable, str(LOG), 'event', 'list'], capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])['data'], {'reason': 'startup'})

    def test_origin_path_that_cannot_be_read_is_a_clean_error(self):
        (self.directory / 'ORIGIN').mkdir()
        result = self.cli('add', 'session.started', '--actor', 'lead', '--data', '{}')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('invalid events ORIGIN file', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertNotIn(str(self.directory), result.stderr)

    def test_no_fcntl_clear_error(self):
        with patch.dict(sys.modules, fcntl=None):
            with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
                self.append()
        self.assertIn('fcntl', err.getvalue())

    def run_cli_with_mask(self, values, *args):
        env = dict(os.environ, WOUF_MASK=json.dumps(values))
        return subprocess.run([sys.executable, str(LOG), 'event', *args], capture_output=True, text=True, env=env)

    def test_spaced_reproduction_is_stored_and_printed_as_the_same_valid_json_line(self):
        # The registered text only occurs in the spaced JSON that add and list used to print.
        result = self.run_cli_with_mask([': true'], 'add', 'session.started', '--actor', 'lead', '--data',
                                        '{"reason":true}')
        self.assertEqual(result.returncode, 0, result.stderr)
        path = next(self.directory.glob('*.jsonl'))
        stored = path.read_text(encoding='utf-8').splitlines()
        self.assertEqual(len(stored), 1)
        self.assertEqual(result.stdout, stored[0] + '\n')
        listed = self.run_cli_with_mask([': true'], 'list')
        self.assertEqual(listed.stdout, stored[0] + '\n')
        self.assertEqual(json.loads(listed.stdout)['data'], {'reason': True})
        check = self.run_cli_with_mask([': true'], 'check')
        self.assertEqual(check.returncode, 0)
        self.assertIn(path.name + ': 0', check.stdout)

    def test_registered_compact_literal_is_refused_by_add_and_leaves_file_unchanged(self):
        self.append()
        path = next(self.directory.glob('*.jsonl'))
        before = path.read_bytes()
        result = self.run_cli_with_mask(['"reason":true'], 'add', 'session.started', '--actor', 'lead', '--data',
                                        '{"reason":true}')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('"reason":true', result.stdout + result.stderr)
        self.assertEqual(path.read_bytes(), before)

    def test_escape_created_token_is_refused_at_the_final_line(self):
        # The raw value has 31 key characters; JSON writes its newline as backslash-n, so the line gets 32.
        self.append()
        path = next(self.directory.glob('*.jsonl'))
        before = path.read_bytes()
        with self.assertRaises(SystemExit):
            self.append({'reason': '\n' + 'Ab0' * 10 + 'C'})
        self.assertEqual(path.read_bytes(), before)

    def test_u2028_in_a_stored_record_is_listed_as_one_valid_line(self):
        e = self.append({'reason': 'a b'})
        path = next(self.directory.glob('*.jsonl'))
        self.assertNotIn(' '.encode('utf-8'), path.read_bytes())
        # A record written by an older writer, with the character raw in the file.
        legacy = dict(e, seq=2, id=e['origin'] + '-2', data={'reason': 'c d'})
        with path.open('ab') as handle:
            handle.write((json.dumps(legacy, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8'))
        result = self.cli('list')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count('\n'), 2)
        self.assertNotIn(' ', result.stdout)
        self.assertEqual([json.loads(x)['data'] for x in result.stdout.splitlines()],
                         [{'reason': 'a b'}, {'reason': 'c d'}])
        self.assertIn(path.name + ': 0', self.cli('check').stdout)

    def test_list_and_check_agree_on_malformed_records(self):
        e = self.append()
        path = next(self.directory.glob('*.jsonl'))
        # Valid JSON, but its line holds a registered value once serialized: malformed for list and check alike.
        held = dict(e, seq=2, id=e['origin'] + '-2', data={'reason': True})
        with path.open('ab') as handle:
            handle.write((json.dumps(held, separators=(',', ':')) + '\n').encode('utf-8'))
            handle.write(b'torn')
        listed = self.run_cli_with_mask(['"reason":true'], 'list')
        check = self.run_cli_with_mask(['"reason":true'], 'check')
        self.assertEqual(check.returncode, 1)
        self.assertIn(path.name + ': 2', check.stdout)
        self.assertEqual(len(listed.stdout.splitlines()), 1)


    def test_lesson_body_limit_and_shape(self):
        self.log.event_append('lesson.proposed', 'lead', '',
                              {'title': 'Pruning', 'body': 'word ' * 400, 'topic': 'fruit.md', 'source': 'lead'})
        for body in ('x' * 2001, ['words']):
            with self.assertRaises(SystemExit):
                self.log.event_append('lesson.proposed', 'lead', '', {'body': body})
    def test_lesson_final_line_secret_rule_and_mask(self):
        with patch.object(self.log, 'REGISTERED', ['plain-marker']):
            event = self.log.event_append('lesson.proposed', 'lead', '', {'body': 'plain-marker ' + 'word ' * 200})
            self.assertTrue(event['data']['body'].startswith('*** '))
        before = next(self.directory.glob('*.jsonl')).read_bytes()
        with patch.object(self.log, 'REGISTERED', ['Zq\\nWx-9']):
            with self.assertRaises(SystemExit):
                self.log.event_append('lesson.proposed', 'lead', '', {'body': 'Zq\nWx-9'})
        self.assertEqual(next(self.directory.glob('*.jsonl')).read_bytes(), before)
