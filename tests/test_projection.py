"""State cache security, deterministic replay and polling bus."""
import json
import os
from pathlib import Path
import random
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from test_log import helper, LOG


class Projection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'env/state.sqlite'
        # A home of its own: the caller's secret folder and config must not slow or change the helpers.
        (self.root / 'home').mkdir()
        self.env = patch.dict(os.environ, WOUF_EVENTS_DIR=str(self.root / 'events'),
                              WOUF_LOG_DIR=str(self.root / 'logs'), WOUF_STATE_DB=str(self.path), WOUF_MASK='[]',
                              HOME=str(self.root / 'home'), XDG_CONFIG_HOME=str(self.root / 'home' / '.config'))
        self.env.start()
        self.addCleanup(self.env.stop)
        self.log = helper(LOG)

    def append(self, kind='task.created', data=None, task='T-1'):
        return self.log.event_append(kind, 'lead', task, data or {'title': 'Example'})

    def rows(self, rebuild=False):
        db = self.log.state_db(rebuild=rebuild)
        try:
            return [tuple(r) for r in db.execute('SELECT * FROM tasks ORDER BY id')]
        finally:
            db.close()

    def test_rebuild_equals_incremental_500_events(self):
        rng = random.Random(23)
        for i in range(500):
            self.append('task.state', {'state': rng.choice(['ready', 'done', 'blocked'])}, 'T-' + str(rng.randrange(30)))
            if i % 37 == 0:
                self.rows()
        self.assertEqual(self.rows(), self.rows(True))

    def test_deleted_and_corrupt_cache_rebuilt(self):
        self.append()
        expected = self.rows()
        self.path.unlink()
        self.assertEqual(expected, self.rows())
        self.path.write_bytes(b'broken database')
        self.assertEqual(expected, self.rows())

    def test_torn_line_counted_and_check_fails(self):
        self.append()
        path = next((self.root / 'events').glob('*.jsonl'))
        with path.open('ab') as f:
            f.write(b'{"torn":')
        with self.log.state_db() as db:
            self.assertEqual(db.execute("SELECT v FROM meta WHERE k='skipped'").fetchone()[0], '1')
        result = subprocess.run([sys.executable, str(LOG), 'state', 'check'], capture_output=True)
        self.assertEqual(result.returncode, 1)

    def test_claim_uniqueness(self):
        self.append('claim.set', {'id': 'a', 'owner': 'worker'})
        self.append('claim.set', {'id': 'b', 'owner': 'worker2'})
        with self.log.state_db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM claims WHERE status != 'released'").fetchone()[0], 1)
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT INTO claims(id,task,status) VALUES ('c','T-1','open')")

    def test_mask_rotation_and_passed_memory(self):
        self.append(data={'title': 'rotation-example'})
        self.rows()
        with patch.object(self.log, 'REGISTERED', ['rotation-example']), patch.object(self.log, 'STORED', ['rotation-example']):
            self.assertNotIn('rotation-example', str(self.rows()))
            for p in self.path.parent.glob('state.sqlite*'):
                self.assertNotIn(b'rotation-example', p.read_bytes())
        with patch.object(self.log, 'PASSED', ['temporary']):
            self.rows()
            self.assertFalse(self.path.exists())

    def test_wal_permissions_and_incremental_offset(self):
        self.append()
        with self.log.state_db() as db:
            self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
            self.assertEqual(db.execute('PRAGMA busy_timeout').fetchone()[0], 30000)
            self.assertEqual(db.execute('PRAGMA secure_delete').fetchone()[0], 1)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.append('task.state', {'state': 'done'})
        self.assertEqual(self.rows()[0][4], 'done')

    def test_wait_after_other_process_append(self):
        e = self.append()
        proc = subprocess.Popen([sys.executable, str(LOG), 'state', 'wait', '--since', str(e['seq']), '--timeout', '4'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(.3)
        result = subprocess.run([sys.executable, str(LOG), 'event', 'add', 'task.state', '--actor', 'lead', '--task', 'T-1', '--data', '{"state":"done"}'], capture_output=True)
        self.assertEqual(result.returncode, 0)
        out, err = proc.communicate(timeout=6)
        self.assertEqual(proc.returncode, 0, err)
        self.assertEqual(out.strip(), '2')

    def test_projection_is_pure_and_unknown_kind_ignored(self):
        e = self.append()
        before = json.dumps(e)
        self.assertEqual(self.log.project_event(e), self.log.project_event(e))
        self.assertEqual(before, json.dumps(e))
        self.assertEqual(self.log.project_event(dict(e, kind='session.started')), [])

    def test_receipts_incremental_equals_rebuild(self):
        logs = self.root / 'logs'
        logs.mkdir()
        path = logs / 'lead.jsonl'
        for i in range(3):
            with path.open('a') as f:
                f.write(json.dumps(dict(t=str(i), who='lead', task='T-1', outcome='done', signal='ok')) + '\n')
            db = self.log.state_db()
            db.close()
        db = self.log.state_db()
        before = db.execute('SELECT * FROM runs ORDER BY id').fetchall()
        db.close()
        db = self.log.state_db(rebuild=True)
        self.assertEqual(before, db.execute('SELECT * FROM runs ORDER BY id').fetchall())
        db.close()

    def test_source_edit_same_size_and_mtime_rebuilds(self):
        self.append(data={'title': 'First'})
        self.rows()
        path = next((self.root / 'events').glob('*.jsonl'))
        stat = path.stat()
        path.write_bytes(path.read_bytes().replace(b'First', b'Other'))
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertEqual(self.rows()[0][1], 'Other')

    def test_all_projected_kinds_and_claim_release(self):
        events = [
            ('lesson.proposed', {'title': 'Pruning', 'body': 'Trim branches'}),
            ('task.created', {'title': 'Task'}), ('task.state', {'owner': 'worker'}),
            ('question.asked', {'text': 'Why?', 'from': 'worker'}),
            ('question.answered', {'text': 'Yes', 'from': 'worker'}),
            ('decision.recorded', {'number': 'D1', 'title': 'Choice', 'why': 'Reason'}),
            ('run.started', {'bot': 'worker'}), ('run.finished', {'bot': 'worker', 'outcome': 'done'}),
            ('flow.step', {'kind': 'review', 'status': 'waiting'}),
            ('schedule.item', {'id': 's1', 'when': 'tomorrow', 'title': 'Review'}),
            ('claim.set', {'id': 'c1', 'owner': 'worker'}),
            ('claim.released', {'id': 'c1'}), ('claim.set', {'id': 'c2', 'owner': 'worker2'}),
            ('handoff.created', {'id': 'h1', 'from': 'worker', 'to': 'review', 'text': 'Ready'}),
            ('handoff.done', {'id': 'h1', 'outcome': 'done'}),
            ('phase.state', {'phase': 'VERIFY', 'state': 'active'}),
            ('dag.edge', {'from': 'T-1', 'to': 'T-2', 'relation': 'depends_on'}),
        ]
        for kind, data in events:
            self.append(kind, data)
        db = self.log.state_db()
        for table in self.log.STATE_COLUMNS:
            self.assertGreater(db.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0, table)
        self.assertEqual(db.execute('SELECT status FROM questions').fetchone()[0], 'answered')
        self.assertEqual(db.execute("SELECT status FROM claims WHERE id='c1'").fetchone()[0], 'released')
        db.close()

    def test_pack_stage_excludes_cache(self):
        from test_team_cli import Project
        project = Project()
        self.addCleanup(project.close)
        cache = project.root / '.agents/team/env/state.sqlite'
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(b'host-only-cache')
        # A fresh process respects team's refusal to import a different pack's teamlib.
        script = ("import sys; from pathlib import Path; from importlib.machinery import SourceFileLoader; "
                  "from importlib.util import module_from_spec,spec_from_loader; "
                  "loader=SourceFileLoader('stage_test',sys.argv[1]); "
                  "module=module_from_spec(spec_from_loader('stage_test',loader)); "
                  "loader.exec_module(module); module.pack_stage('worker',[],Path(sys.argv[2]))")
        result = subprocess.run([sys.executable, '-c', script,
                                 str(project.root / '.agents/bin/team'), str(self.root / 'stage')],
                                env=project.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any('state.sqlite' in str(p) for p in (self.root / 'stage/pack').rglob('*')))

    def test_cross_origin_replay_order_and_corruption_check(self):
        directory = self.root / 'events'
        directory.mkdir()
        for origin, title in [('aaaaaaaaaaaa', 'First'), ('bbbbbbbbbbbb', 'Last')]:
            event = dict(v=1, id=origin+'-1', seq=1, t='2026-10-28T12:00:00Z',
                         origin=origin, actor='lead', kind='task.created', task='T-1', scope='local',
                         data={'title': title})
            (directory / (origin+'.jsonl')).write_text(json.dumps(event)+'\n')
        self.rows()
        event.update(origin='aaaaaaaaaaaa', id='aaaaaaaaaaaa-2', seq=2, data={'title': 'Later'})
        with (directory / 'aaaaaaaaaaaa.jsonl').open('a') as f:
            f.write(json.dumps(event)+'\n')
        self.assertEqual(self.rows(), self.rows(True))
        self.path.write_bytes(b'corrupt')
        result = subprocess.run([sys.executable, str(LOG), 'state', 'check'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)['corruption'], '1')

    def test_invalid_origin_file_is_skipped(self):
        e = self.append()
        original = next((self.root / 'events').glob('*.jsonl'))
        e.update(origin='invalid', id='invalid-1')
        original.unlink()
        (self.root / 'events/invalid.jsonl').write_text(json.dumps(e)+'\n')
        db = self.log.state_db()
        self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT v FROM meta WHERE k='skipped'").fetchone()[0], '1')
        db.close()

    def test_incremental_applies_only_new_line(self):
        self.append()
        self.rows()
        self.append('task.state', {'state': 'done'})
        with patch.object(self.log, 'project_event', wraps=self.log.project_event) as project:
            self.rows()
            self.assertEqual(project.call_count, 1)
        with patch.object(self.log, 'project_event', wraps=self.log.project_event) as project:
            self.rows()
            self.assertEqual(project.call_count, 0)

    def test_rotated_origin_is_absent_from_metadata(self):
        event = self.append()
        self.rows()
        with patch.object(self.log, 'REGISTERED', [event['origin']]), patch.object(self.log, 'STORED', [event['origin']]):
            self.rows()
            for path in self.path.parent.glob('state.sqlite*'):
                self.assertNotIn(event['origin'].encode(), path.read_bytes())

    def test_wait_detects_rebuild_when_max_sequence_is_unchanged(self):
        first = self.append()
        directory = self.root / 'events'
        origin = 'ffffffffffff' if first['origin'] != 'ffffffffffff' else 'eeeeeeeeeeee'
        other = dict(first, origin=origin, id=origin+'-50', seq=50)
        (directory / (origin+'.jsonl')).write_text(json.dumps(other)+'\n')
        proc = subprocess.Popen([sys.executable, str(LOG), 'state', 'wait', '--since', '50', '--timeout', '5'],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: proc.kill() if proc.poll() is None else None)
        time.sleep(.3)
        self.append('task.state', {'state': 'done'})
        out, err = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 0, err)
        self.assertEqual(out.strip(), '50')

    def write_receipt(self, **fields):
        logs = self.root / 'logs'
        logs.mkdir(exist_ok=True)
        entry = dict(t='2026-10-28T12:00:00Z', who='lead', task='T-1', outcome='done', signal='ok')
        entry.update(fields)
        with (logs / 'lead.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps(entry) + '\n')

    def test_json_escaped_registered_value_never_reaches_cache(self):
        secret = 's3crét"q\\z'
        self.write_receipt(signal=secret, ref=[secret], cost={'a': secret})
        forms = {secret.encode(), json.dumps(secret)[1:-1].encode(),
                 json.dumps(secret, ensure_ascii=False)[1:-1].encode()}
        with patch.object(self.log, 'REGISTERED', [secret]), patch.object(self.log, 'STORED', [secret]):
            db = self.log.state_db()
            self.assertEqual([(signal, json.loads(ref), json.loads(cost))
                              for signal, ref, cost in db.execute('SELECT signal, ref, cost FROM runs')],
                             [('***', ['***'], {'a': '***'})])
            db.close()
            for path in self.path.parent.glob('state.sqlite*'):
                self.assertFalse(any(form in path.read_bytes() for form in forms), path.name)

    def test_nested_registered_value_is_masked_before_serialization(self):
        value = 'synthetic-' + chr(233) + '-marker'
        self.write_receipt(signal={'nested': value})
        with patch.object(self.log, 'REGISTERED', [value]), patch.object(self.log, 'STORED', [value]):
            db = self.log.state_db()
            self.assertEqual(json.loads(db.execute('SELECT signal FROM runs').fetchone()[0]), {'nested': '***'})
            db.close()

    def test_row_still_holding_a_key_shape_is_skipped_and_counted(self):
        self.write_receipt(ref=['Abcdefghijklmnopqrstuvwxyz0123456789ABCDEF'])
        db = self.log.state_db()
        self.assertEqual(db.execute('SELECT count(*) FROM runs').fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT v FROM meta WHERE k='skipped'").fetchone()[0], '1')
        db.close()
        result = subprocess.run([sys.executable, str(LOG), 'state', 'check'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)

    def test_cache_from_previous_format_is_rebuilt(self):
        self.append()
        self.append('task.state', {'state': 'done'})
        with patch.object(self.log, 'STATE_FORMAT', 'state-v1'):
            self.rows()
        with patch.object(self.log, 'project_event', wraps=self.log.project_event) as project:
            self.rows()
            self.assertEqual(project.call_count, 2)

    def test_receipts_present_new_event_applies_one_line(self):
        self.write_receipt()
        self.append()
        self.rows()
        self.append('task.state', {'state': 'done'})
        with patch.object(self.log, 'project_event', wraps=self.log.project_event) as project:
            self.rows()
            self.assertEqual(project.call_count, 1)
        self.assertEqual(self.rows(), self.rows(True))

    def test_new_event_source_sorting_before_applied_source_rebuilds(self):
        directory = self.root / 'events'
        directory.mkdir()

        def write(origin, title):
            event = dict(v=1, id=origin + '-1', seq=1, t='2026-10-28T12:00:00Z', origin=origin, actor='lead',
                         kind='task.created', task='T-1', scope='local', data={'title': title})
            (directory / (origin + '.jsonl')).write_text(json.dumps(event) + '\n')

        write('bbbbbbbbbbbb', 'Last')
        self.rows()
        write('aaaaaaaaaaaa', 'First')
        with patch.object(self.log, 'project_event', wraps=self.log.project_event) as project:
            incremental = self.rows()
            self.assertEqual(project.call_count, 2)
        self.assertEqual(incremental, self.rows(True))


    def test_lesson_replay(self):
        event = self.append('lesson.proposed', {'title': 'Pruning', 'body': 'Trim branches',
                                               'topic': 'fruit.md', 'source': 'worker2'})
        ident = 'L-' + event['id']
        self.append('lesson.accepted', {'id': ident})
        for rebuild in (False, True):
            with self.log.state_db(rebuild=rebuild) as db:
                self.assertEqual(db.execute('SELECT id,title,body,source,status FROM lessons').fetchone(),
                                 (ident, 'Pruning', 'Trim branches', 'worker2', 'accepted'))
