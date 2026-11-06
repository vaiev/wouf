"""Chunked, masked knowledge search and sandbox-only shared snapshots."""
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from test_log import helper, LOG


class Knowledge(unittest.TestCase):
    def setUp(self):
        try:
            sqlite3.connect(':memory:').execute('CREATE VIRTUAL TABLE probe USING fts5(body)')
        except sqlite3.OperationalError:
            self.skipTest('SQLite FTS5 is unavailable')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.team = self.root / '.agents/team'
        (self.team / 'knowledge').mkdir(parents=True)
        self.env = patch.dict(os.environ, WOUF_LOG_DIR=str(self.team / 'logs'),
                              WOUF_EVENTS_DIR=str(self.team / 'events'),
                              WOUF_KNOWLEDGE_DB='', WOUF_MASK='[]')
        self.env.start()
        self.addCleanup(self.env.stop)
        self.log = helper(LOG)
        self.log.PASSED = []
        self.log.REGISTERED = []

    def doc(self, name, body):
        p = self.team / 'knowledge' / name
        p.write_text(body)
        return p

    def ask(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.log.cmd_ask(list(args))
        return out.getvalue()

    def test_chunk_headings_fences_and_long_paragraphs(self):
        chunks = self.log.chunk_text('# Parent\nintro\n## Child\n```md\n# fake\n```\n~~~\n# also fake\n~~~\n\n' + 'x' * 3900)
        self.assertEqual(chunks[0], ('Parent', 'intro'))
        self.assertTrue(all(h == 'Parent > Child' for h, _ in chunks[1:]))
        self.assertIn('# fake', chunks[1][1])
        self.assertTrue(all(len(b) <= 1800 for _, b in chunks))

    def test_dedupe_change_counter_and_removed_source(self):
        a = self.doc('a.md', '# Heading\nunique zebra')
        b = self.doc('b.md', '# Heading\nunique zebra')
        with patch.object(self.log, 'chunk_text', wraps=self.log.chunk_text) as counter:
            db = self.log.knowledge_db(); db.close()
            self.assertEqual(counter.call_count, 2)
            os.utime(a, None)
            db = self.log.knowledge_db()
            self.assertEqual(db.execute('SELECT count(*) FROM chunks').fetchone()[0], 1)
            db.close()
            self.assertEqual(counter.call_count, 2)
            a.write_text('# Heading\nchanged giraffe')
            db = self.log.knowledge_db(); db.close()
            self.assertEqual(counter.call_count, 3)
        b.unlink()
        self.assertNotIn('zebra', self.ask('zebra'))
        self.assertIn('giraffe', self.ask('giraffe'))

    def test_citations_caps_syntax_and_permissions(self):
        self.doc('a.md', '\n'.join('# Section%d\nneedle %s' % (i, str(i) + 'a' * 600) for i in range(25)))
        result = self.ask('needle', '-k', '99', '--json')
        data = json.loads(result)
        self.assertIn('quoted data', data['notice'])
        self.assertEqual(len(data['hits']), 20)
        self.assertTrue(all(len(h['text']) <= 400 for h in data['hits']))
        self.assertIn('knowledge/a.md § Section', data['hits'][0]['citation'])
        for query in ('"', '*', 'NEAR', 'needle " * NEAR'):
            self.ask(query)
        p = self.team / 'env/knowledge.sqlite'
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        db = self.log.knowledge_db()
        self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
        self.assertEqual(db.execute('PRAGMA secure_delete').fetchone()[0], 1)
        db.close()

    def test_masking_events_and_shared_isolation(self):
        key = 'gh' + 'p_' + 'A' * 36
        self.doc('safe.md', '# Safety\nsharedmarker ' + key)
        self.log.event_append('decision.recorded', 'lead', '', {'number': 7, 'title': 'decisionmarker'})
        self.log.event_append('lesson.accepted', 'lead', '', {'title': 'acceptedmarker', 'body': 'lesson text'})
        self.log.event_append('lesson.proposed', 'lead', '', {'title': 'proposedmarker'})
        self.assertIn('D-7', self.ask('decisionmarker'))
        self.assertIn('L-', self.ask('acceptedmarker'))
        self.assertNotIn('proposedmarker', self.ask('proposedmarker'))
        self.assertNotIn(key, self.ask('sharedmarker'))
        self.assertNotIn(key.encode(), (self.team / 'env/knowledge.sqlite').read_bytes())
        shared = self.root / 'shared.sqlite'
        self.ask('--build-shared', str(shared))
        self.assertNotIn(b'decisionmarker', shared.read_bytes())
        self.assertNotIn(b'acceptedmarker', shared.read_bytes())
        with patch.dict(os.environ, WOUF_KNOWLEDGE_DB=str(shared)):
            self.assertIn('sharedmarker', self.ask('sharedmarker'))
            self.assertNotIn('decisionmarker', self.ask('decisionmarker'))

    def test_project_sources_and_passed_memory(self):
        (self.root / 'docs').mkdir()
        (self.root / 'docs/a.md').write_text('# Project\nprojectmarker')
        (self.team / 'config.ini').write_text('[knowledge]\nsources = docs\n')
        self.assertIn('projectmarker', self.ask('projectmarker'))
        shared = self.root / 'shared.sqlite'
        self.ask('--build-shared', str(shared))
        self.assertNotIn(b'projectmarker', shared.read_bytes())
        (self.root / 'docs/link.md').symlink_to('/etc/passwd')
        self.log.PASSED = ['temporary']
        db = self.log.knowledge_db()
        self.assertEqual(db.execute('PRAGMA database_list').fetchone()[2], '')
        db.close()

    def test_shared_with_passed_values_stays_in_memory(self):
        self.doc('shared.md', '# Shared\npublic temporary')
        shared = self.root / 'snapshot.sqlite'
        self.ask('--build-shared', str(shared))
        self.log.PASSED = ['temporary']
        self.log.REGISTERED = ['temporary']
        with patch.dict(os.environ, WOUF_KNOWLEDGE_DB=str(shared)):
            result = self.ask('public')
            self.assertIn('public ***', result)
            self.assertNotIn('temporary', result)

    def test_sources_cannot_escape_project(self):
        outside = self.root / 'outside.md'
        outside.write_text('escapemarker')
        nested_team = self.root / 'nested/.agents/team'
        (nested_team / 'knowledge').mkdir(parents=True)
        (nested_team / 'knowledge/link.md').symlink_to(outside)
        (nested_team / 'config.ini').write_text('[knowledge]\nsources = ../outside.md, https://invalid.test/doc\n')
        with patch.dict(os.environ, WOUF_LOG_DIR=str(nested_team / 'logs')), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.log.knowledge_sources(), {})

    def test_shared_rotation_masks_output_and_preserves_snapshot(self):
        self.doc('a.md', '# Title\nsharedmarker rotationvalue')
        shared = self.root / 'rotated.sqlite'
        self.ask('--build-shared', str(shared))
        before = shared.read_bytes()
        self.log.REGISTERED = ['rotationvalue']
        with patch.dict(os.environ, WOUF_KNOWLEDGE_DB=str(shared)):
            self.assertIn('sharedmarker ***', self.ask('sharedmarker'))
            self.assertIn('sharedmarker ***', self.ask('sharedmarker'))
            db = self.log.knowledge_db()
            try:
                with self.assertRaisesRegex(sqlite3.OperationalError, 'readonly'):
                    db.execute('DELETE FROM chunks')
            finally:
                db.close()
        self.assertEqual(shared.read_bytes(), before)
        self.assertFalse(shared.with_name(shared.name + '.lock').exists())

    def test_rrf_fuses_tables_and_default_k(self):
        self.doc('a.md', 'fusionmarker')
        self.log.event_append('decision.recorded', 'lead', '', {'title': 'fusionmarker'})
        data = json.loads(self.ask('fusionmarker', '--json'))
        self.assertEqual(len(data['hits']), 1)
        self.assertAlmostEqual(data['hits'][0]['score'], 2 / 61)
        self.doc('many.md', '\n'.join('# H%d\nfusionmarker unique%d' % (i, i) for i in range(10)))
        self.assertEqual(len(json.loads(self.ask('fusionmarker', '--json'))['hits']), 5)

    def test_token_like_chunk_is_refused_at_ingest_and_counted(self):
        token = 'Ab3d' * 10
        self.doc('a.md', '# Safe\nsafemarker ok\n\n# Leak\nleakmarker ' + token)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            result = self.ask('leakmarker')
        self.assertNotIn('leakmarker', result)
        self.assertNotIn(token.encode(), (self.team / 'env/knowledge.sqlite').read_bytes())
        self.assertIn('1 chunk(s) refused', err.getvalue())
        self.assertIn('safemarker', self.ask('safemarker'))

    def test_printed_snippet_is_checked_before_output(self):
        self.doc('a.md', '# Safe\nneedle ok')
        self.ask('needle')
        con = sqlite3.connect(self.team / 'env/knowledge.sqlite')
        con.execute('UPDATE chunks SET body=?', ('needle ' + 'Ab3d' * 10,))
        con.commit()
        con.close()
        result = self.ask('needle')
        self.assertNotIn('Ab3d', result)
        self.assertNotIn('needle', result)

    def test_configured_text_file_is_ingested_by_paragraph(self):
        (self.root / 'reference.txt').write_text('first configuredmarker\n\nsecond paragraph\n')
        (self.team / 'config.ini').write_text('[knowledge]\nsources = reference.txt\n')
        data = json.loads(self.ask('configuredmarker', '--json'))
        self.assertEqual(data['hits'][0]['citation'], 'reference.txt § (top)')

    def test_plain_chunks_are_paragraph_bounded(self):
        chunks = self.log.chunk_plain('a\n\n' + 'b' * 1000 + '\n\n' + 'c' * 1000)
        self.assertEqual([len(body) for _, body in chunks], [1003, 1000])
        self.assertTrue(all(len(body) <= 1800 for _, body in self.log.chunk_plain('d' * 4000)))

    def test_skipped_listed_files_are_reported_with_reason(self):
        (self.root / 'binary.txt').write_bytes(b'configuredmarker\x00')
        (self.root / 'latin.txt').write_bytes('configuredmarker é'.encode('latin-1'))
        (self.root / 'big.txt').write_text('configuredmarker ' + 'x' * self.log.KNOWLEDGE_MAX_BYTES)
        (self.team / 'config.ini').write_text(
            '[knowledge]\nsources = binary.txt, latin.txt, big.txt, missing.txt, https://invalid.test/doc\n')
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            result = self.ask('configuredmarker')
        self.assertNotIn('configuredmarker', result)
        report = err.getvalue()
        for name, reason in [('binary.txt', 'NUL bytes'), ('latin.txt', 'not UTF-8'), ('big.txt', 'larger than'),
                             ('missing.txt', 'not a regular file'), ('https://invalid.test/doc', 'URLs')]:
            self.assertIn(name, report)
            self.assertIn(reason, report)

    def test_rebuild_shared_over_host_cache_erases_event_text(self):
        self.doc('a.md', '# Shared\npublicmarker')
        self.log.event_append('decision.recorded', 'lead', '', {'title': 'privateeventmarker'})
        self.ask('privateeventmarker')
        path = self.team / 'env/knowledge.sqlite'
        self.ask('--build-shared', str(path))
        self.assertNotIn(b'privateeventmarker', path.read_bytes())
        self.assertNotIn('privateeventmarker', self.ask('privateeventmarker'))

    def run_ask(self, log, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            log.cmd_ask(list(args))
        return out.getvalue()

    def test_run_keys_read_staged_snapshot_twice_and_never_change_it(self):
        self.doc('a.md', '# Shared\nstagedmarker runvalue public')
        snapshot = self.root / 'staged.sqlite'
        self.ask('--build-shared', str(snapshot))
        before = snapshot.read_bytes()
        with patch.dict(os.environ, WOUF_KNOWLEDGE_DB=str(snapshot), WOUF_MASK=json.dumps(['runvalue'])):
            log = helper(LOG)
            first = self.run_ask(log, 'stagedmarker')
            second = self.run_ask(log, 'stagedmarker')
        self.assertIn('stagedmarker', first)
        self.assertIn('stagedmarker', second)
        self.assertNotIn('runvalue', first + second)
        self.assertEqual(snapshot.read_bytes(), before)

    def test_build_shared_with_run_keys_writes_masked_snapshot(self):
        self.doc('a.md', '# Shared\nbuildmarker runvalue')
        out = self.root / 'built.sqlite'
        with patch.dict(os.environ, WOUF_MASK=json.dumps(['runvalue'])):
            self.run_ask(helper(LOG), '--build-shared', str(out))
        self.assertTrue(out.exists())
        self.assertEqual(out.stat().st_mode & 0o777, 0o644)
        self.assertNotIn(b'runvalue', out.read_bytes())
        self.assertFalse(out.with_name('built.sqlite.lock').exists())
        self.assertEqual(list(self.root.glob('.built.sqlite.*')), [])
        with patch.dict(os.environ, WOUF_KNOWLEDGE_DB=str(out), WOUF_MASK=json.dumps(['runvalue'])):
            result = self.run_ask(helper(LOG), 'buildmarker')
        self.assertIn('buildmarker', result)
        self.assertNotIn('runvalue', result)

    def test_lesson_citation_comes_from_event_id(self):
        event = self.log.event_append('lesson.accepted', 'lead', '', {'title': 'lessonmarker', 'body': 'x'})
        self.assertIn('L-' + event['id'], self.ask('lessonmarker'))
