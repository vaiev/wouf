"""Run output lessons use the existing proposal and acceptance store."""
import json
import subprocess
import sys
import unittest
from test_team_cli import Project, SOURCE


class LessonCollect(unittest.TestCase):
    def setUp(self):
        self.p = Project(mode='host', runner='fake')
        self.addCleanup(self.p.close)
        self.team = self.p.root / '.agents/team'
        self.worker = self.team / 'env/worker'
        self.worker.mkdir(parents=True)

    def run_script(self, script):
        (self.worker / 'fake-cli.sh').write_text('#!/bin/sh\nmkdir -p out/knowledge\n' + script)
        result = self.p.team('run', 'worker', 'T-1', '--brief', str(self.p.brief))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.stage = self.worker / 'tasks/T-1'
        return result

    def proposals(self):
        return [json.loads(line) for p in (self.team / 'events').glob('*.jsonl')
                for line in p.read_text().splitlines() if json.loads(line)['kind'] == 'lesson.proposed']

    def test_two_files_source_title_body_and_acceptance(self):
        self.run_script("printf 'preface\\n## Orchard\\npruning guidance\\n' > out/knowledge/a.md\n"
                        "printf 'pear guidance' > out/knowledge/b.md\n")
        events = self.proposals()
        self.assertEqual(len(events), 2)
        self.assertEqual([e['data']['title'] for e in events], ['Orchard', 'b.md'])
        self.assertEqual(events[0]['data']['body'], 'preface\n## Orchard\npruning guidance\n')
        self.assertTrue(all((e['actor'], e['task'], e['data']['source']) == ('worker', 'T-1', 'worker')
                            for e in events))
        ident = 'L-' + events[0]['id']
        self.assertEqual(self.p.team('lesson', 'accept', ident).returncode, 0)
        hits = json.loads(self.p.team('ask', 'pruning').stdout)['hits']
        self.assertEqual(hits[0]['citation'], ident)

    def test_invalid_entries_and_file_count_are_logged_without_failing_run(self):
        self.run_script("echo text > out/knowledge/a.txt\nmkdir out/knowledge/b.md\n"
                        "ln -s a.txt out/knowledge/c.md\nmkfifo out/knowledge/d.md\n"
                        "head -c 65537 /dev/zero > out/knowledge/e.md\n"
                        "for n in $(seq 1 22); do echo advice > out/knowledge/g$n.md; done\n")
        self.assertEqual(len(self.proposals()), 20)
        lines = (self.stage / 'run.log').read_text().splitlines()
        skipped = [line for line in lines if 'lesson skipped' in line]
        self.assertEqual(len(skipped), 7, skipped)
        for name in ['a.txt', 'b.md', 'c.md', 'd.md', 'e.md']:
            self.assertEqual(sum(name in line for line in skipped), 1)

    def test_registered_value_masked_and_body_over_500_is_preserved(self):
        value = 'fixture-' + 'registered-value'
        self.p.store_secret('ALPHA_KEY', value)
        self.run_script("printf '# Safety\\n%s\\n' \"$ALPHA_KEY\" > out/knowledge/a.md\n"
                        "head -c 1000 /dev/zero | tr '\\000' x >> out/knowledge/a.md\n")
        [event] = self.proposals()
        self.assertNotIn(value, json.dumps(event))
        self.assertIn('***', event['data']['body'])
        self.assertGreater(len(event['data']['body']), 1000)

    def test_container_skipped_knowledge_paths_are_logged(self):
        stage = self.worker / 'tasks/T-1'
        stage.mkdir(parents=True)
        spec = {'stage': str(stage), 'member': 'worker', 'task': 'T-1'}
        skipped = ['out/knowledge', 'out/knowledge/c.md', 'out/other.md',
                   'out/knowledge-other/a.md']
        script = ('import sys,json; sys.path.insert(0,sys.argv[1]); '
                  'from teamlib import run; '
                  'run.collect_lessons(json.loads(sys.argv[2]), (), json.loads(sys.argv[3]))')
        result = subprocess.run([sys.executable, '-c', script, str(SOURCE / 'bin'),
                                 json.dumps(spec), json.dumps(skipped)], cwd=self.p.root,
                                env=self.p.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((stage / 'run.log').read_text().splitlines(), [
            'team: lesson skipped out/knowledge: link or special file',
            'team: lesson skipped out/knowledge/c.md: link or special file',
        ])
        self.assertEqual(self.proposals(), [])

    def test_existing_event_body_limit_is_logged_without_failing_run(self):
        self.run_script("head -c 2001 /dev/zero | tr '\\000' x > out/knowledge/a.md\n")
        [event] = self.proposals()
        suffix = '\n… (cut at 2,000 characters; the whole file is out/knowledge/a.md of run T-1)'
        self.assertEqual(event['data']['body'], 'x' * (2000 - len(suffix)) + suffix)
        self.assertEqual(len(event['data']['body']), 2000)
        self.assertEqual((self.stage / 'out/knowledge/a.md').read_text(), 'x' * 2001)
        lines = (self.stage / 'run.log').read_text().splitlines()
        self.assertEqual(sum('lesson truncated a.md' in line for line in lines), 1)
        self.assertFalse(any('lesson skipped' in line for line in lines))
        self.assertEqual(self.p.team('lesson', 'accept', 'L-' + event['id']).returncode, 0)

    def test_body_at_limit_is_preserved(self):
        self.run_script("head -c 2000 /dev/zero | tr '\\000' x > out/knowledge/a.md\n")
        [event] = self.proposals()
        self.assertEqual(event['data']['body'], 'x' * 2000)
        self.assertNotIn('lesson truncated', (self.stage / 'run.log').read_text())

    def test_recollect_does_not_duplicate_proposals(self):
        self.run_script("echo '# Orchard' > out/knowledge/a.md\n")
        script = ('import sys,json; sys.path.insert(0,sys.argv[1]); '
                  'from teamlib import run,state; '
                  'spec=json.load(open(sys.argv[2])); run.collect(spec,0,False)')
        result = subprocess.run([sys.executable, '-c', script, str(SOURCE / 'bin'),
                                 str(self.stage / 'run.json')], cwd=self.p.root,
                                env={**self.p.env, 'WOUF_TEAM_DIR': str(self.team)},
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.proposals()), 1)
