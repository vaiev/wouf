"""Gate cache promotion and disposable task writes."""
import stat
import json
import sys
import unittest
from test_team_cli import Project


class GateCacheTest(unittest.TestCase):
    def setUp(self):
        self.p = Project(mode="host", runner="fake")
        self.addCleanup(self.p.close)
        self.p.git("branch", "-M", "main")
        self.p.config.write_text(self.p.config.read_text() + '\n[flow]\ngate = true\n')

    def gate(self, command, task="T-A", ref="main", *options):
        text = self.p.config.read_text()
        text = text[:text.index('\n[flow]')] + '\n[flow]\ngate = ' + command + '\n'
        self.p.config.write_text(text)
        return self.p.team("gate", task, "--repo", ref, "--base", "main", *options)

    def ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_task_writes_discarded_and_main_warming_visible(self):
        self.ok(self.gate('echo poison > "$WOUF_CACHE/poison"'))
        self.ok(self.gate('test ! -e "$WOUF_CACHE/poison"', "T-B"))
        self.ok(self.gate('echo trusted > "$WOUF_CACHE/trusted"', "T-W", "main", "--warm-cache"))
        self.ok(self.gate('test "$(cat "$WOUF_CACHE/trusted")" = trusted && echo poison > "$WOUF_CACHE/trusted"'))
        self.ok(self.gate('test "$(cat "$WOUF_CACHE/trusted")" = trusted', "T-B"))
        snapshot = self.p.root / '.agents/team/env/gate/.cache'
        self.assertFalse(snapshot.stat().st_mode & stat.S_IWUSR)
        self.ok(self.p.team('gate', '--fresh-cache'))
        self.ok(self.gate('test ! -e "$WOUF_CACHE/trusted"'))

    def test_branch_cannot_warm_even_with_main_as_base(self):
        self.p.git('checkout', '-qb', 'task')
        (self.p.root / 'file.txt').write_text('branch\n')
        self.p.git('commit', '-qam', 'branch')
        result = self.gate('true', 'T-A', 'task', '--warm-cache')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('reachable from main', result.stderr + result.stdout)

    def test_tag_named_main_cannot_authorize_branch_warming(self):
        self.p.git('checkout', '-qb', 'task')
        (self.p.root / 'file.txt').write_text('branch\n')
        self.p.git('commit', '-qam', 'branch')
        self.p.git('tag', 'main')
        result = self.gate('true', 'T-A', 'task', '--warm-cache')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('reachable from main', result.stderr + result.stdout)

    def test_container_timeout_is_enforced_inside_engine(self):
        calls = self.p.bin / 'calls'
        fake = self.p.bin / 'podman'
        fake.write_text(f'#!{sys.executable}\nimport json, subprocess, sys\n'
                        f'with open({str(calls)!r}, "a") as f: f.write(json.dumps(sys.argv[1:]) + "\\n")\n'
                        'if sys.argv[1] == "run":\n'
                        '    sys.stdin.buffer.read()\n'
                        '    command = sys.argv[sys.argv.index("timeout"): ]\n'
                        '    assert command[:4] == ["timeout", "-k", "1", "1"]\n'
                        '    assert command[4:6] == ["sh", "-c"]\n'
                        '    sys.exit(subprocess.call(command[:4] + ["sh", "-c", "sleep 30"]))\n')
        fake.chmod(0o755)
        self.p.set('mode = host', 'mode = container')
        self.p.set('engine = docker', 'engine = podman')
        self.p.set('timeout_minutes = 30', 'timeout_minutes = 0.01')
        result = self.gate('sleep 30')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('gate command exit 124', result.stdout)
        lines = [json.loads(line) for line in calls.read_text().splitlines()]
        self.assertLessEqual(len(lines), 3, lines)
        run = next(line for line in lines if line[0] == 'run')
        self.assertIn('--rm', run)

    def test_links_in_warm_cache_are_not_followed(self):
        outside = self.p.root / 'outside'
        outside.mkdir()
        (outside / 'keep').write_text('safe')
        self.ok(self.gate(f'ln -s {outside} "$WOUF_CACHE/link"', 'T-W', 'main', '--warm-cache'))
        self.ok(self.gate('test ! -e "$WOUF_CACHE/link"'))
        self.assertEqual((outside / 'keep').read_text(), 'safe')

    def test_large_snapshot_uses_empty_cache_and_reports_it(self):
        snapshot = self.p.root / '.agents/team/env/gate/.cache'
        snapshot.mkdir(parents=True)
        with (snapshot / 'large').open('wb') as stream:
            stream.truncate(256 * 1024 * 1024 + 1)
        result = self.gate('test -z "$(ls -A "$WOUF_CACHE")"')
        self.ok(result)
        self.assertIn('exceeds 256 MiB', result.stdout)

    def test_failed_host_warm_does_not_replace_snapshot(self):
        self.ok(self.gate('echo trusted > "$WOUF_CACHE/value"', 'T-W', 'main', '--warm-cache'))
        result = self.gate('echo bad > "$WOUF_CACHE/value"; exit 1', 'T-W', 'main', '--warm-cache')
        self.assertNotEqual(result.returncode, 0)
        self.ok(self.gate('test "$(cat "$WOUF_CACHE/value")" = trusted'))

    def test_engine_budget_and_mount_policy(self):
        for engine in ('podman', 'docker'):
            with self.subTest(engine=engine):
                calls = self.p.bin / 'calls'
                calls.write_text('')
                fake = self.p.bin / engine
                fake.write_text(f'#!{sys.executable}\nimport json, sys\n'
                                f'with open({str(calls)!r}, "a") as f: f.write(json.dumps(sys.argv[1:]) + "\\n")\n'
                                'if sys.argv[1] == "run":\n'
                                '    sys.stdin.buffer.read()\n')
                fake.chmod(0o755)
                self.p.set('mode = host', 'mode = container')
                self.p.set('engine = docker', f'engine = {engine}')
                result = self.gate('true')
                self.ok(result)
                lines = [json.loads(line) for line in calls.read_text().splitlines()]
                self.assertLessEqual(len(lines), 3, lines)
                run = next(line for line in lines if line[0] == 'run')
                self.assertIn('--rm', run)
                self.assertIn('WOUF_CACHE=/bot/cache', run)
                if engine == 'podman':
                    self.assertTrue(any(value.endswith(':/bot/cache:O') for value in run))
                else:
                    self.assertFalse(any(':/bot/cache' in value for value in run))
                    self.assertIn('empty cache', result.stdout)
                calls.write_text('')
                self.ok(self.gate('true', 'T-W', 'main', '--warm-cache'))
                warm_calls = [json.loads(line) for line in calls.read_text().splitlines()]
                self.assertLessEqual(len(warm_calls), 3, warm_calls)
                warm_run = next(line for line in warm_calls if line[0] == 'run')
                if engine == 'podman':
                    self.assertTrue(any(value.endswith(':/bot/cache') for value in warm_run))
                    self.assertFalse(any(value.endswith(':/bot/cache:O') for value in warm_run))
                else:
                    self.assertFalse(any(':/bot/cache' in value for value in warm_run))
                self.p.set('mode = container', 'mode = host')
                self.p.set(f'engine = {engine}', 'engine = docker')
