"""Synthetic session hook measurements; no real transcripts."""
import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '.agents/bin'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import isolate_wouf_env  # noqa: E402


def setUpModule() -> None:
    isolate_wouf_env()


class SessionTest(unittest.TestCase):
    def test_fixture_numbers_and_privacy(self):
        session = importlib.import_module('teamlib.session')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'fixture.jsonl'
            rows = [{'type': 'user', 'timestamp': '2026-01-01T00:00:00Z',
                     'message': {'content': 'PRIVATE FIXTURE TEXT'}}]
            for i, command in enumerate(('.agents/bin/team context', '.agents/bin/team run T-1'), 1):
                rows.append({'type': 'assistant', 'timestamp': f'2026-01-01T00:00:0{i}Z', 'message': {
                    'id': str(i), 'usage': dict(input_tokens=10, cache_read_input_tokens=20,
                                              cache_creation_input_tokens=30, output_tokens=4),
                    'content': [{'type': 'tool_use', 'name': 'Bash', 'input': {'command': command}}]}})
            path.write_text(''.join(json.dumps(r) + '\n' for r in rows))
            out = io.StringIO()
            with patch.dict(os.environ, WOUF_EVENTS_DIR=str(root / 'events')), patch('sys.stdin', io.StringIO(
                    json.dumps({'transcript_path': str(path), 'reason': 'PRIVATE FIXTURE TEXT'}))), contextlib.redirect_stdout(out):
                self.assertEqual(session.cmd_session(['end', '--claude']), 0)
            events = [json.loads(line) for f in (root / 'events').glob('*.jsonl') for line in f.read_text().splitlines()]
            self.assertEqual([e['kind'] for e in events], ['session.ended', 'usage.session'])
            data = events[1]['data']
            self.assertEqual(data['first_turn_tokens'], dict(input_tokens=10, cache_read_input_tokens=20,
                                                            cache_creation_input_tokens=30, output_tokens=4))
            self.assertEqual(data['time_to_first_action'], 2)
            totals = dict(input_tokens=20, cache_read_input_tokens=40,
                          cache_creation_input_tokens=60, output_tokens=8)
            self.assertEqual(data['tokens_to_first_action'], totals)
            self.assertEqual(data['total_tokens'], totals)
            self.assertNotIn('PRIVATE', json.dumps(events) + out.getvalue())
            self.assertNotIn(str(path), json.dumps(events) + out.getvalue())

    def test_without_hooks(self):
        session = importlib.import_module('teamlib.session')
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, WOUF_EVENTS_DIR=tmp):
            self.assertEqual(session.cmd_session(['end']), 0)
            events = [json.loads(line) for f in Path(tmp).glob('*.jsonl') for line in f.read_text().splitlines()]
            self.assertEqual([e['kind'] for e in events], ['session.ended', 'usage.session'])
            self.assertIsNone(events[1]['data']['time_to_first_action'])

    def test_bad_hook_does_not_record_payload(self):
        session = importlib.import_module('teamlib.session')
        for payload in ('{bad', '{}', '[]', '{"transcript_path": 42}'):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as tmp:
                with patch.dict(os.environ, WOUF_EVENTS_DIR=tmp), patch('sys.stdin', io.StringIO(payload)), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        session.cmd_session(['end', '--claude'])
                self.assertEqual(list(Path(tmp).glob('*.jsonl')), [])

    def test_human_supplied_transcript_and_unknowns(self):
        session = importlib.import_module('teamlib.session')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'fixture.jsonl'
            path.write_text(json.dumps({'type': 'assistant', 'message': {'content': []}}) + '\n')
            with patch.dict(os.environ, WOUF_EVENTS_DIR=str(Path(tmp) / 'events')), contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(session.cmd_session(['end', '--transcript', str(path)]), 0)
            data = json.loads(out.getvalue())
            self.assertIsNone(data['first_turn_tokens']['input_tokens'])
            self.assertIsNone(data['time_to_first_action'])
