"""Live team migration keeps authoritative state and goal/practice metadata."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import isolate_wouf_env  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TEAM = ROOT / '.agents/bin/team'
LOG = ROOT / '.agents/bin/log'


def setUpModule() -> None:
    isolate_wouf_env()


class MigrationTest(unittest.TestCase):
    def test_unprefixed_members_in_migration_archive_are_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            team, archive = root / 'team', root / 'evil.tar.gz'
            team.mkdir()
            (team / 'config.ini').write_text('[team]\nname = Test\n')
            members = {
                'team/migration/v1/FORMAT': b'wouf-team-migration-v1\n',
                'team/events/ORIGIN': b'not an origin\n',
                'team/events/aaaaaaaaaaaa.jsonl': b'not an event at all\n',
                'team/practice/codex.md': b'Practice\n',
            }
            with tarfile.open(archive, 'w:gz') as tar:
                for name, data in members.items():
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
            env = dict(os.environ, WOUF_TEAM_DIR=str(team), WOUF_EVENTS_DIR=str(team / 'events'))
            result = subprocess.run([sys.executable, str(TEAM), 'import', str(archive), '--apply'],
                                    env=env, text=True, capture_output=True, check=True)
            self.assertIn('kept for review, not applied', result.stdout)
            self.assertFalse((team / 'events/ORIGIN').exists())
            self.assertFalse((team / 'events/aaaaaaaaaaaa.jsonl').exists())
            self.assertFalse((team / 'practice/codex.md').exists())
            for rel, data in members.items():
                if rel.endswith('/FORMAT'):
                    continue
                held = team / 'import/evil' / rel.removeprefix('team/')
                self.assertEqual(held.read_bytes(), data)

    def test_round_trip_live_team(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, dest, archive = root / 'source', root / 'dest', root / 'team.tar.gz'
            source_events, dest_events = root / 'source-events', root / 'dest-events'
            source.mkdir(); dest.mkdir()
            (source / 'config.ini').write_text('[team]\nname = Test\n')
            env = dict(os.environ, WOUF_TEAM_DIR=str(source), WOUF_EVENTS_DIR=str(source_events))
            def run(cmd, env=env):
                return subprocess.run(cmd, env=env, text=True, capture_output=True, check=True)
            run([sys.executable, str(TEAM), 'task', 'new', 'Migration task', '--class', 'standard'])
            for rel, data in {'env/goals/T-8.json': '{"task":"T-8","repo":"/example","plan":null}',
                              'env/lead/T-8.md': 'Goal brief\n', 'practice/codex.md': 'Practice\n',
                              'flows/T-8/state.json': '{"status":"blocked"}'}.items():
                path = source / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(data)
            run([sys.executable, str(LOG), 'event', 'add', 'phase.state', '--actor', 'lead',
                 '--task', 'T-8', '--data', '{"phase":"INIT","state":"active","reason":"goal"}'])
            run([sys.executable, str(TEAM), 'export', '--with-data', '-o', str(archive)])
            dest_env = dict(os.environ, WOUF_TEAM_DIR=str(dest), WOUF_EVENTS_DIR=str(dest_events))
            run([sys.executable, str(TEAM), 'import', str(archive), '--apply'], dest_env)
            for rel in ('env/goals/T-8.json', 'env/lead/T-8.md', 'practice/codex.md', 'flows/T-8/state.json'):
                self.assertEqual((dest / rel).read_bytes(), (source / rel).read_bytes())
            events = run([sys.executable, str(LOG), 'event', 'list'], dest_env).stdout
            self.assertIn('phase.state', events)
            self.assertIn('task.created', events)
            self.assertTrue(list(dest_events.glob('*.jsonl')))
            self.assertFalse((dest / 'events').exists())
            self.assertEqual(json.loads((dest / 'env/goals/T-8.json').read_text())['repo'], '/example')
            (dest / 'config.ini').write_text('[team]\nname = Test\n')
            status = run([sys.executable, str(TEAM), 'goal', 'status', 'T-8'], dest_env).stdout
            self.assertIn('INIT', status)
            before = (dest / 'env/goals/T-8.json').read_bytes()
            repeat = subprocess.run([sys.executable, str(TEAM), 'import', str(archive), '--apply', '--force'],
                                    env=dest_env, text=True, capture_output=True)
            self.assertNotEqual(repeat.returncode, 0)
            self.assertIn('existing events', repeat.stderr)
            self.assertEqual((dest / 'env/goals/T-8.json').read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
