"""Bot refresh uses a recording engine, never containers or the secret store."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from test_team_cli import Project
import test_team_update as pack_update


ENGINE = r'''#!{python}
import json, sys
from pathlib import Path
state = Path({state!r})
args = sys.argv[1:]
with (state / 'calls').open('a') as handle:
    handle.write(json.dumps(args) + '\n')
images = json.loads((state / 'images').read_text())
if args[:2] == ['image', 'inspect']:
    if args[-1] not in images:
        sys.exit(1)
    labels = images[args[-1]]
    print(json.dumps(labels) if 'json' in args[args.index('-f') + 1] else labels.get('wouf.recipe', ''))
elif args[0] == 'build':
    sys.stdin.read()
    if (state / 'fail-build').exists():
        print('private-build-output', file=sys.stderr)
        sys.exit(1)
    labels = dict(arg.split('=', 1) for i, arg in enumerate(args) if args[i-1] == '--label')
    images[args[args.index('-t') + 1]] = labels
    if (state / 'start-task-on-build').exists():
        (state / 'running').write_text('box|worker|T-1\n')
elif args[:2] == ['image', 'tag']:
    if (state / 'fail-tag').exists():
        sys.exit(1)
    images[args[-1]] = images[args[-2]]
elif args[:2] == ['image', 'rm']:
    images.pop(args[-1], None)
elif args[0] == 'ps':
    if (state / 'fail-ps').exists():
        sys.exit(1)
    running = state / 'running'
    if running.exists():
        print(running.read_text(), end='')
        if (state / 'finish-task').exists():
            running.unlink()
elif args[:2] == ['volume', 'inspect']:
    sys.exit(0 if (state / 'storages' / args[-1]).exists() else 1)
elif args[0] == 'run':
    sys.exit(1 if (state / 'fail-login').exists() else 0)
else:
    raise AssertionError(args)
(state / 'images').write_text(json.dumps(images))
'''


class UpdateBotsTest(unittest.TestCase):
    def setUp(self):
        self.p = Project(mode='container', engine_name='fakeengine', runner='alpha')
        self.addCleanup(self.p.close)
        bots = self.p.root / '.agents/team/bots'
        for path in bots.glob('*.md'):
            path.unlink()
        for member, runner, status in [('worker', 'alpha', 'active'), ('review', 'beta', 'trial'),
                                        ('retired', 'alpha', 'retired')]:
            (bots / (member + '.md')).write_text(
                f'---\nid: {member}\nrunner: {runner}\nstatus: {status}\n---\n')
        self.p.set('install =\ncommand = alpha-cli', 'install = npm install -g alpha\ncommand = alpha-cli')
        self.p.set('auth = ALPHA_KEY', 'auth = login\nlogin_check = test -f /bot/home/login')
        self.p.set('command = beta-cli exec', 'login_check = test -f /bot/home/login\ncommand = beta-cli exec')
        self.state = Path(self.p.tmp.name) / 'engine'
        self.state.mkdir()
        (self.state / 'images').write_text('{}')
        (self.state / 'storages').mkdir()
        self.tag = 'wouf-' + self.p.root.name + '-image'
        for member in ('worker', 'review', 'retired'):
            (self.state / 'storages' / self.tag.replace('-image', '-' + member)).write_text('login and memory')
        fake = self.p.bin / 'fakeengine'
        fake.write_text(ENGINE.format(python=sys.executable, state=str(self.state)))
        fake.chmod(0o755)

    def refresh(self, *args):
        return self.p.team('update', '--bots', *args)

    def assert_ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def calls(self):
        path = self.state / 'calls'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def clear_calls(self):
        (self.state / 'calls').unlink(missing_ok=True)

    def images(self):
        return json.loads((self.state / 'images').read_text())

    def warm(self):
        self.assert_ok(self.refresh('--yes'))
        self.clear_calls()

    def test_unchanged_recipe_does_nothing_and_is_safe_to_repeat(self):
        self.warm()
        before = self.images()
        result = self.refresh('--yes')
        self.assert_ok(result)
        self.assertIn('nothing to do', result.stdout)
        self.assertEqual(self.images(), before)
        self.assertFalse(any(c[0] in ('build', 'run') for c in self.calls()))

    def test_changed_recipe_plans_runner_install_tools_and_affected_bots(self):
        self.warm()
        self.p.set('npm install -g alpha', 'npm install -g alpha@next')
        self.p.set('setup = ', 'setup = echo new-tool && ')
        before = self.images()
        files_before = {p.relative_to(self.p.root): p.read_bytes() for p in self.p.root.rglob('*')
                        if p.is_file() and '.git' not in p.relative_to(self.p.root).parts}
        result = self.refresh()
        self.assert_ok(result)
        for text in ('Rebuild planned', 'runner.alpha install: changed', 'environment setup: changed',
                     'worker', 'review', 'storages, logins and keys stay', '--yes'):
            self.assertIn(text, result.stdout)
        self.assertNotIn('retired', result.stdout)
        self.assertEqual(before, self.images())
        self.assertEqual(files_before, {p.relative_to(self.p.root): p.read_bytes() for p in self.p.root.rglob('*')
                                        if p.is_file() and '.git' not in p.relative_to(self.p.root).parts})
        self.assertFalse(any(c[0] in ('build', 'run') for c in self.calls()))

    def test_success_checks_each_active_login_without_resetting_storage(self):
        before = {p.name: p.read_bytes() for p in (self.state / 'storages').iterdir()}
        result = self.refresh('--yes')
        self.assert_ok(result)
        runs = [c for c in self.calls() if c[0] == 'run']
        self.assertEqual(len(runs), 2)
        self.assertTrue(all(':ro' in ' '.join(c) for c in runs))
        self.assertIn('worker: login OK', result.stdout)
        self.assertIn('review: login OK', result.stdout)
        self.assertEqual(before, {p.name: p.read_bytes() for p in (self.state / 'storages').iterdir()})
        self.assertFalse(any(c[0] in ('stop', 'rm', 'logout') or
                             c[:2] in (['volume', 'rm'], ['volume', 'create']) for c in self.calls()))

    def test_running_task_refused_even_with_yes(self):
        (self.state / 'running').write_text('box|worker|T-1\n')
        result = self.refresh('--yes')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('running', result.stderr)
        self.assertIn('--wait', result.stderr)
        self.assertFalse(any(c[0] in ('build', 'stop', 'run') for c in self.calls()))

    def test_local_task_lock_refused_before_container_starts(self):
        lock = self.p.root / '.agents/team/env/worker/tasks/T-1/run.lock'
        lock.parent.mkdir(parents=True)
        lock.write_text(json.dumps({'pid': os.getpid()}))
        result = self.refresh('--yes')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('running', result.stderr)
        self.assertFalse(any(c[0] == 'build' for c in self.calls()))
        lock.unlink()

    def test_wait_waits_until_running_tasks_finish_without_stopping_them(self):
        (self.state / 'running').write_text('box|worker|T-1\n')
        (self.state / 'finish-task').touch()
        result = self.refresh('--wait', '--yes')
        self.assert_ok(result)
        self.assertIn('Waiting', result.stdout)
        self.assertTrue(any(c[0] == 'build' for c in self.calls()))
        self.assertFalse(any(c[0] in ('stop', 'kill') for c in self.calls()))

    def test_failed_rebuild_keeps_old_image_and_does_not_check_logins(self):
        self.warm()
        before = self.images()
        self.p.set('npm install -g alpha', 'npm install -g alpha@next')
        (self.state / 'fail-build').touch()
        result = self.refresh('--yes')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('previous image stays in use', result.stdout + result.stderr)
        self.assertNotIn('private-build-output', result.stdout + result.stderr)
        self.assertEqual(self.images(), before)
        self.assertFalse(any(c[0] == 'run' for c in self.calls()))
        (self.state / 'fail-build').unlink()
        self.assert_ok(self.refresh('--yes'))

    def test_failed_promotion_keeps_old_image(self):
        self.warm()
        before = self.images()
        self.p.set('npm install -g alpha', 'npm install -g alpha@next')
        (self.state / 'fail-tag').touch()
        result = self.refresh('--yes')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.images(), before)
        self.assertIn('previous image stays in use', result.stdout + result.stderr)

    def test_task_starting_during_build_prevents_promotion(self):
        self.warm()
        before = self.images()
        self.p.set('npm install -g alpha', 'npm install -g alpha@next')
        (self.state / 'start-task-on-build').touch()
        result = self.refresh('--yes')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(before, self.images())
        self.assertIn('previous image stays in use', result.stdout + result.stderr)
        self.assertFalse(any(c[0] in ('run', 'stop') for c in self.calls()))

    def test_stamp_write_failure_after_promotion_does_not_claim_old_image_is_used(self):
        stamp = self.p.root / '.agents/team/env/engine-stamp.json'
        stamp.mkdir(parents=True)
        result = self.refresh('--yes')
        self.assert_ok(result)
        self.assertIn('Team image refreshed', result.stdout)
        self.assertIn(self.tag, self.images())
        self.assertEqual(len([c for c in self.calls() if c[0] == 'run']), 2)

    def test_legacy_image_without_parts_reports_unknown_inputs_honestly(self):
        (self.state / 'images').write_text(json.dumps({self.tag: {'wouf.recipe': 'old'}}))
        result = self.refresh()
        self.assert_ok(result)
        self.assertIn('Previous install/tool details unavailable', result.stdout)
        self.assertIn('runner.alpha install: needs checking', result.stdout)
        self.assertFalse(any(c[0] == 'build' for c in self.calls()))

    def test_install_contents_and_engine_diagnostics_are_never_printed(self):
        private = 'private-install-value-4821'
        self.p.set('npm install -g alpha', 'echo ' + private)
        for args in ((), ('--yes',)):
            result = self.refresh(*args)
            self.assert_ok(result)
            self.assertNotIn(private, result.stdout + result.stderr)
            self.assertNotIn(private, json.dumps(self.images()))

    def test_failed_running_check_refuses_build(self):
        (self.state / 'fail-ps').touch()
        result = self.refresh('--yes')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(c[0] == 'build' for c in self.calls()))

    def test_missing_login_reported_without_logging_out_or_removing_storage(self):
        (self.state / 'fail-login').touch()
        result = self.refresh('--yes')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('login needs attention', result.stdout)
        self.assertFalse(any(c[:2] in (['volume', 'rm'], ['volume', 'create']) for c in self.calls()))
        self.assertIn(self.tag, self.images())

    def test_missing_storage_is_not_implicitly_created_by_login_check(self):
        (self.state / 'storages' / self.tag.replace('-image', '-worker')).unlink()
        result = self.refresh('--yes')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('worker: storage missing', result.stdout)
        self.assertEqual(len([c for c in self.calls() if c[0] == 'run']), 1)

    def test_secret_store_never_opened_or_queried(self):
        code = """
import sys
sys.path.insert(0, sys.argv[1])
from teamlib import core
core.secrets_module = lambda: (_ for _ in ()).throw(AssertionError('secret store queried'))
def guard(event, args):
    if event in ('open', 'os.listdir', 'os.scandir') and str(sys.argv[2]) in str(args[0]):
        raise AssertionError('secret store read')
sys.addaudithook(guard)
import runpy
sys.argv = [sys.argv[1] + '/team', 'update', '--bots', '--yes']
runpy.run_path(sys.argv[0], run_name='__main__')
"""
        result = subprocess.run([sys.executable, '-B', '-c', code, str(self.p.root / '.agents/bin'),
                                 str(self.p.store)], env=self.p.env, cwd=self.p.root,
                                capture_output=True, text=True)
        self.assert_ok(result)

    def test_host_mode_does_not_call_container_engine(self):
        self.p.set('mode = container', 'mode = host')
        self.assert_ok(self.refresh('--yes'))
        self.assertEqual(self.calls(), [])

    def test_pack_source_options_cannot_be_silently_ignored(self):
        result = self.refresh('--from', '/unused', '--yes')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_pack_recipe_change_offers_bot_refresh_after_doctor(self):
        fixture = pack_update.UpdateTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        name = '.agents/bin/teamlib/engine.py'
        text = (fixture.source / name).read_text()
        fixture.write(fixture.source, name, text.replace('WORKDIR /bot', 'WORKDIR /wouf'))
        fixture.commit(fixture.source)
        result = fixture.run_update('--yes')
        fixture.assert_ok(result)
        self.assertIn('team update --bots', result.stdout.splitlines()[-1])
        self.assertLess(result.stdout.index('doctor passed'), result.stdout.rindex('team update --bots'))


if __name__ == '__main__':
    unittest.main()
