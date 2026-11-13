"""Local git packs exercise update without contacting a remote or a model."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import writable_copy  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / '.agents'
MANIFEST = '.agents/team/.pack-files'
CONFIG = '[team]\nname = test\nrunner = fake\n[environment]\nmode = host\n[runner.fake]\ncommand = git\nauth =\n'


class UpdateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / 'project'
        self.source = self.root / 'source'
        self.scratch = self.root / 'scratch'
        self.scratch.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(('WOUF_', 'GIT_'))}
        self.env.update(TMPDIR=str(self.scratch), PYTHONDONTWRITEBYTECODE='1',
                        WOUF_SECRET_BACKEND='file', WOUF_SECRET_DIR=str(self.root / 'secrets'))
        for directory in (self.project, self.source):
            writable_copy(PACK, directory / '.agents', ignore=shutil.ignore_patterns('__pycache__'))
            self.git(directory, 'init', '-q')
            self.git(directory, 'config', 'user.name', 'Test')
            self.git(directory, 'config', 'user.email', 'test@example.invalid')
        self.write(self.project, '.agents/VERSION', '0.1.0\n')
        self.write(self.project, '.agents/team/config.ini', CONFIG)
        self.write(self.project, '.agents/skills/retired.py', 'old = True\n')
        self.write(self.project, MANIFEST, '\n'.join(self.managed(self.project)) + '\n')
        for name in ('.agents/skills/custom.md', '.agents/roles/nested/own.md',
                     '.agents/team/board.md', '.agents/team/decisions.md',
                     '.agents/team/env/worker/storage', 'team/board.md'):
            self.write(self.project, name, 'user data\n')
        # Even a source collision belongs to the user if the old manifest never owned it.
        self.write(self.source, '.agents/skills/custom.md', 'source collision\n')
        self.write(self.source, '.agents/roles/nested/own.md', 'nested source collision\n')
        self.write(self.source, '.agents/skills/new.md', 'new skill\n')
        self.write(self.source, '.agents/VERSION', '0.1.1\n')
        self.write(self.source, '.agents/upgrade/0.1.1.md',
                   '# 0.1.1\nFrom: 0.1.0\nStep: .agents/upgrade/0.1.1/01-example.py\n')
        self.write(self.source, '.agents/upgrade/0.1.1/01-example.py',
                   'def plan():\n    return ["Normalize the new skill"]\n'
                   'def apply():\n    pack.write_text(".agents/skills/new.md", "normalized skill\\n")\n')
        self.write(self.source, '.agents/rules.md', (PACK / 'rules.md').read_text() + '\nNew rules.\n')
        self.write(self.source, '.agents/team/config.ini', 'never copy this\n')
        self.commit(self.project)
        self.commit(self.source)

    def git(self, directory, *args):
        return subprocess.run(['git', '-C', str(directory), *args], env=self.env,
                              capture_output=True, text=True, check=True).stdout.strip()

    def commit(self, directory):
        self.git(directory, 'add', '-A')
        self.git(directory, 'commit', '-qm', 'fixture')

    def write(self, directory, name, text):
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')

    def managed(self, directory):
        return sorted(p.relative_to(directory).as_posix() for p in (directory / '.agents').rglob('*')
                      if p.is_file() and 'team' != p.relative_to(directory / '.agents').parts[0])

    def snapshot(self, directory):
        return {p.relative_to(directory).as_posix(): (p.read_bytes(), p.stat().st_mode & 0o777)
                for p in directory.rglob('*') if p.is_file() and '.git' not in p.relative_to(directory).parts}

    def run_update(self, *args, source=True, env=None):
        result = subprocess.run([sys.executable, str(self.project / '.agents/bin/team'), 'update',
                                 *(['--from', str(self.source)] if source else []), *args],
                                cwd=self.project, env=env or self.env, capture_output=True, text=True)
        self.assertEqual(list(self.scratch.iterdir()), [], result.stdout + result.stderr)
        return result

    def assert_ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_plan_only_lists_versions_and_changes_without_writing(self):
        before = self.snapshot(self.project)
        # No inherited bytecode setting: the command itself must keep previews read only.
        env = {k: v for k, v in self.env.items() if k != 'PYTHONDONTWRITEBYTECODE'}
        result = self.run_update(env=env)
        self.assert_ok(result)
        for line in ('Pack version now:', 'Pack version after:', 'Added:', 'Changed:', 'Retired:',
                     'Upgrade path: 0.1.0 -> 0.1.1', 'Normalize the new skill', 'Free disk space:',
                     'Kept (user-owned):', '.agents/skills/new.md', '.agents/rules.md',
                     '.agents/skills/retired.py', '.agents/roles/nested/own.md'):
            self.assertIn(line, result.stdout)
        self.assertEqual(before, self.snapshot(self.project))

    def test_apply_updates_manifest_and_runs_installed_doctor(self):
        before = self.snapshot(self.project)
        result = self.run_update('--yes')
        self.assert_ok(result)
        self.assertIn('all good', result.stdout)
        self.assertIn('doctor passed', result.stdout)
        for name in ('.agents/rules.md',):
            self.assertEqual((self.project / name).read_bytes(), (self.source / name).read_bytes())
        owned = (self.project / MANIFEST).read_text().splitlines()
        self.assertIn('.agents/skills/new.md', owned)
        self.assertNotIn('.agents/skills/retired.py', owned)
        self.assertNotIn('.agents/skills/custom.md', owned)
        self.assertNotIn('.agents/roles/nested/own.md', owned)
        after = self.snapshot(self.project)
        for name, data in before.items():
            if name.startswith(('team/', '.agents/team/')) and name != MANIFEST:
                self.assertEqual(after[name], data, name)

    def test_retired_file_and_only_its_bytecode_are_removed(self):
        cache = '.agents/skills/__pycache__/'
        self.write(self.project, cache + 'retired.cpython-39.pyc', 'old bytecode')
        self.write(self.project, cache + 'custom.cpython-39.pyc', 'user bytecode')
        self.assert_ok(self.run_update('--yes'))
        self.assertFalse((self.project / '.agents/skills/retired.py').exists())
        self.assertFalse((self.project / (cache + 'retired.cpython-39.pyc')).exists())
        self.assertEqual((self.project / (cache + 'custom.cpython-39.pyc')).read_text(), 'user bytecode')

    def test_nested_and_colliding_user_files_are_kept(self):
        before = self.snapshot(self.project)
        self.assert_ok(self.run_update('--yes'))
        after = self.snapshot(self.project)
        for name in ('.agents/skills/custom.md', '.agents/roles/nested/own.md'):
            self.assertEqual(after[name], before[name])

    def test_pre_manifest_install_stops_even_with_force(self):
        (self.project / MANIFEST).unlink()
        before = self.snapshot(self.project)
        result = self.run_update('--yes', '--force')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('ownership manifest missing', result.stderr)
        self.assertIn('INSTALL.md', result.stderr)
        self.assertEqual(before, self.snapshot(self.project))

    def test_dirty_managed_file_refused_then_force_applies(self):
        self.write(self.project, '.agents/rules.md', 'local edit\n')
        before = self.snapshot(self.project)
        result = self.run_update('--yes')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('uncommitted', result.stderr)
        self.assertIn('--force', result.stderr)
        self.assertEqual(before, self.snapshot(self.project))
        self.assert_ok(self.run_update('--yes', '--force'))

    def test_staged_managed_file_refused(self):
        self.write(self.project, '.agents/rules.md', 'staged edit\n')
        self.git(self.project, 'add', '.agents/rules.md')
        result = self.run_update('--yes')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('uncommitted', result.stderr)

    def test_dirty_user_state_is_allowed(self):
        self.write(self.project, '.agents/team/board.md', 'current work\n')
        self.write(self.project, '.agents/roles/nested/own.md', 'current user edit\n')
        self.assert_ok(self.run_update('--yes'))
        self.assertEqual((self.project / '.agents/team/board.md').read_text(), 'current work\n')

    def test_tag_and_commit_refs(self):
        first = self.git(self.source, 'rev-parse', 'HEAD')
        self.git(self.source, 'tag', 'release-test')
        self.write(self.source, '.agents/skills/new.md', 'later\n')
        self.commit(self.source)
        for ref in ('release-test', first):
            with self.subTest(ref=ref):
                result = self.run_update('--ref', ref)
                self.assert_ok(result)
                self.assertIn(first, result.stdout)

    def test_default_branch_and_local_clone_worktree_changes(self):
        self.git(self.source, 'checkout', '-qb', 'pack-default')
        self.write(self.source, '.agents/skills/new.md', 'default branch\n')
        self.commit(self.source)
        self.write(self.source, '.agents/skills/new.md', 'uncommitted source edit\n')
        result = self.run_update('--yes')
        self.assert_ok(result)
        self.assertIn(self.git(self.source, 'rev-parse', 'HEAD'), result.stdout)
        self.assertEqual((self.project / '.agents/skills/new.md').read_text(), 'normalized skill\n')

    def test_invalid_ref_failure_cleans_up_without_changes(self):
        before = self.snapshot(self.project)
        result = self.run_update('--yes', '--ref', 'no-such-ref')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(before, self.snapshot(self.project))

    def test_clone_failure_cleans_up(self):
        result = self.run_update('--from', str(self.root / 'missing'), source=False)
        self.assertNotEqual(result.returncode, 0)

    def test_invalid_pack_refused_without_changes(self):
        (self.source / '.agents/START.md').unlink()
        self.commit(self.source)
        before = self.snapshot(self.project)
        result = self.run_update('--yes')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('WOUF pack', result.stderr)
        self.assertEqual(before, self.snapshot(self.project))

    def test_manifest_cannot_claim_team_or_escape_paths(self):
        for name in ('.agents/team/board.md', '../elsewhere', '.agents/../outside', '/absolute',
                     '.agents//rules.md', '.agents', '.agents/team'):
            with self.subTest(name=name):
                self.write(self.project, MANIFEST, name + '\n')
                before = self.snapshot(self.project)
                result = self.run_update('--yes', '--force')
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(before, self.snapshot(self.project))

    def test_managed_symlink_refused_even_with_force(self):
        path = self.project / '.agents/rules.md'
        path.unlink()
        path.symlink_to('../team/board.md')
        before = (self.project / '.agents/team/board.md').read_bytes()
        result = self.run_update('--yes', '--force')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.project / '.agents/team/board.md').read_bytes(), before)

    def test_source_symlink_refused(self):
        path = self.source / '.agents/skills/new.md'
        path.unlink()
        path.symlink_to('../team/config.ini')
        self.commit(self.source)
        result = self.run_update('--yes')
        self.assertNotEqual(result.returncode, 0)

    def test_user_symlink_directory_is_never_followed(self):
        shutil.rmtree(self.project / '.agents/roles/nested')
        (self.project / '.agents/roles/nested').symlink_to(self.project / '.agents/team', target_is_directory=True)
        self.write(self.source, '.agents/roles/nested/board.md', 'would clobber state\n')
        self.commit(self.source)
        self.assert_ok(self.run_update('--yes'))
        self.assertEqual((self.project / '.agents/team/board.md').read_text(), 'user data\n')

    def test_doctor_failure_is_nonzero_and_reports_applied_update(self):
        self.write(self.project, '.agents/team/config.ini', CONFIG.replace('mode = host', 'mode = undeclared'))
        self.commit(self.project)
        result = self.run_update('--yes')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('doctor failed', result.stdout)
        self.assertTrue((self.project / '.agents/skills/new.md').exists())

    def test_update_also_works_without_project_git(self):
        shutil.rmtree(self.project / '.git')
        self.assert_ok(self.run_update('--yes'))

    def test_tmpdir_inside_project_is_not_used(self):
        inside = self.project / 'temporary'
        inside.mkdir()
        env = dict(self.env, TMPDIR=str(inside))
        result = self.run_update(env=env)
        self.assert_ok(result)
        self.assertEqual(list(inside.iterdir()), [])

    def test_secret_store_is_not_created_or_chmodded(self):
        store = self.root / 'secrets'
        self.assert_ok(self.run_update())
        self.assertFalse(store.exists())
        store.mkdir(mode=0o755)
        (store / '.names').write_text('LOCAL_TEST_VALUE\n')
        (store / 'LOCAL_TEST_VALUE').write_text('private-test-value\n')
        before = self.snapshot(store)
        self.assert_ok(self.run_update('--yes'))
        self.assertEqual(store.stat().st_mode & 0o777, 0o755)
        self.assertEqual(self.snapshot(store), before)

    def run_injected(self, code, *args):
        setup = 'import sys; sys.path.insert(0, sys.argv[1]); from teamlib import update\n'
        result = subprocess.run([sys.executable, '-B', '-c', setup + code,
                                 str(self.project / '.agents/bin'), *args], env=self.env,
                                cwd=self.project, text=True, capture_output=True)
        self.assertEqual(list(self.scratch.iterdir()), [], result.stdout + result.stderr)
        return result

    def test_apply_failure_restores_files_manifest_and_directories(self):
        self.write(self.source, '.agents/aaa/new.md', 'new nested file\n')
        self.commit(self.source)
        before = self.snapshot(self.project)
        directories = {p for p in self.project.rglob('*') if p.is_dir()}
        code = """
original = update.write_file
calls = 0
def failing_write(*args):
    global calls
    calls += 1
    if calls == 2:
        raise OSError('injected copy failure')
    return original(*args)
update.write_file = failing_write
sys.exit(update.do_update(sys.argv[2:]))
"""
        result = self.run_injected(code, '--from', str(self.source), '--yes')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('rolled back', result.stderr)
        self.assertEqual(self.without_backups(self.snapshot(self.project)), before)
        self.assertEqual({p for p in self.project.rglob('*') if p.is_dir()
                          and 'backups' not in p.parts}, directories)

    def test_default_source_is_public_repository_without_contacting_network(self):
        code = """
original = update.git
def local_default(at, *args):
    if args[0] == 'clone':
        assert args[-2] == 'https://github.com/vaiev/wouf', args
        args = (*args[:-2], sys.argv[2], args[-1])
    return original(at, *args)
update.git = local_default
sys.exit(update.do_update([]))
"""
        self.assert_ok(self.run_injected(code, str(self.source)))

    def test_managed_file_to_directory_layout_change_stops(self):
        self.write(self.source, '.agents/skills/retired.py/new.md', 'new layout\n')
        self.commit(self.source)
        before = self.snapshot(self.project)
        result = self.run_update('--yes')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('layout changed', result.stderr)
        self.assertEqual(self.snapshot(self.project), before)

    def without_backups(self, snapshot):
        return {name: data for name, data in snapshot.items()
                if not name.startswith('.agents/team/backups/')}

    def test_pack_version_and_doctor(self):
        result = subprocess.run([sys.executable, str(self.project / '.agents/bin/team'), '--version'],
                                env=self.env, capture_output=True, text=True)
        self.assert_ok(result)
        self.assertEqual(result.stdout.strip(), '0.1.0')
        self.assertIn('Pack version: 0.1.1', self.run_update('--yes').stdout)
        self.assertEqual((self.project / '.agents/VERSION').read_text(), '0.1.1\n')

    def test_backups_and_rollback_by_id(self):
        self.write(self.project, '.agents/team/events/test.jsonl', 'event record\n')
        self.write(self.project, '.agents/team/.salt', 'excluded key fixture')
        self.write(self.project, '.agents/team/env/worker/login.json', 'excluded login fixture')
        before = self.snapshot(self.project)
        self.assert_ok(self.run_update('--yes'))
        backups = list((self.project / '.agents/team/backups').glob('*.tar.gz'))
        self.assertEqual(len(backups), 1)
        with tarfile.open(backups[0]) as archive:
            names = archive.getnames()
        for name in ('.agents/rules.md', '.agents/VERSION', MANIFEST,
                     '.agents/team/board.md', '.agents/team/config.ini', '.agents/team/events/test.jsonl'):
            self.assertIn(name, names)
        self.assertFalse(any(name.startswith('.agents/team/env/') or name in
                             ('.agents/team/.salt', '.agents/team/secrets.md') for name in names))
        backup_id = backups[0].name[:-7]
        listed = self.run_update('--list-backups', source=False)
        self.assert_ok(listed)
        self.assertIn(backup_id, listed.stdout)
        self.write(self.project, '.agents/team/board.md', 'later state\n')
        result = self.run_update('--rollback', backup_id, source=False)
        self.assert_ok(result)
        self.assertEqual(self.without_backups(self.snapshot(self.project)), before)

    def test_failing_step_rolls_back(self):
        self.write(self.source, '.agents/upgrade/0.1.1/01-example.py',
                   'def plan():\n    return ["Fail deliberately"]\n'
                   'def apply():\n    pack.write_text(".agents/rules.md", "changed")\n'
                   '    raise RuntimeError("injected failure")\n')
        self.commit(self.source)
        before = self.snapshot(self.project)
        result = self.run_update('--yes')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('rolled back', result.stderr)
        self.assertEqual(self.without_backups(self.snapshot(self.project)), before)

    def test_failing_step_with_blank_manifest_lines_rolls_back(self):
        manifest = (self.project / MANIFEST).read_text()
        self.write(self.project, MANIFEST, '\n' + manifest.replace('\n', '\n\n'))
        self.commit(self.project)
        self.write(self.source, '.agents/upgrade/0.1.1/01-example.py',
                   'def plan():\n    return ["Fail deliberately"]\n'
                   'def apply():\n    raise RuntimeError("injected failure")\n')
        self.commit(self.source)
        before = self.snapshot(self.project)
        result = self.run_update('--yes')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('automatically rolled back', result.stderr)
        self.assertEqual(self.without_backups(self.snapshot(self.project)), before)

    def test_rollback_by_id_restores_manifest_with_blank_lines(self):
        manifest = (self.project / MANIFEST).read_text()
        self.write(self.project, MANIFEST, '\n' + manifest.replace('\n', '\n\n'))
        self.commit(self.project)
        before = self.snapshot(self.project)
        self.assert_ok(self.run_update('--yes'))
        backup_id = next((self.project / '.agents/team/backups').glob('*.tar.gz')).name[:-7]
        self.assert_ok(self.run_update('--rollback', backup_id, source=False))
        self.assertEqual(self.without_backups(self.snapshot(self.project)), before)
        # The restored live manifest must also be accepted on another rollback.
        self.assert_ok(self.run_update('--rollback', backup_id, source=False))
        self.assertEqual(self.without_backups(self.snapshot(self.project)), before)

    def test_downgrade_requires_both_ref_and_force(self):
        self.write(self.project, '.agents/VERSION', '0.2.0\n')
        self.commit(self.project)
        self.git(self.source, 'tag', 'old-release')
        before = self.snapshot(self.project)
        for args in ((), ('--force',), ('--ref', 'old-release')):
            result = self.run_update('--yes', *args)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('downgrade', result.stderr)
            self.assertEqual(self.snapshot(self.project), before)
        self.assert_ok(self.run_update('--yes', '--ref', 'old-release', '--force'))

    def test_missing_upgrade_link_and_missing_version_refused(self):
        self.write(self.source, '.agents/upgrade/0.1.1.md', '# Missing predecessor\n')
        self.commit(self.source)
        self.assertNotEqual(self.run_update('--yes').returncode, 0)
        (self.source / '.agents/VERSION').unlink()
        self.commit(self.source)
        self.assertNotEqual(self.run_update('--yes').returncode, 0)

    def test_upgrade_steps_chain_in_release_order(self):
        self.write(self.source, '.agents/VERSION', '0.1.2\n')
        self.write(self.source, '.agents/upgrade/0.1.2.md',
                   '# 0.1.2\nFrom: 0.1.1\nStep: .agents/upgrade/0.1.2/01-second.py\n')
        self.write(self.source, '.agents/upgrade/0.1.2/01-second.py',
                   'def plan():\n    return ["Append second"]\n'
                   'def apply():\n    text = pack.read_text(".agents/skills/new.md")\n'
                   '    if not text.endswith("second\\n"):\n'
                   '        pack.write_text(".agents/skills/new.md", text + "second\\n")\n')
        self.commit(self.source)
        result = self.run_update('--yes')
        self.assert_ok(result)
        self.assertIn('0.1.0 -> 0.1.1 -> 0.1.2', result.stdout)
        self.assertEqual((self.project / '.agents/skills/new.md').read_text(), 'normalized skill\nsecond\n')

    def test_step_plan_and_apply_cannot_touch_user_data(self):
        for function in ('plan', 'apply'):
            with self.subTest(function=function):
                self.write(self.source, '.agents/upgrade/0.1.1/01-example.py',
                           'def plan():\n    return ["Protected write"]\n'
                           'def apply():\n    pass\n'
                           + 'def ' + function + '():\n'
                           + '    open(' + repr(str(self.project / '.agents/team/board.md'))
                           + ', "w").write("clobber")\n')
                self.commit(self.source)
                before = self.without_backups(self.snapshot(self.project))
                result = self.run_update('--yes')
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.without_backups(self.snapshot(self.project)), before)

    def test_low_disk_space_refused_before_writes(self):
        before = self.snapshot(self.project)
        result = self.run_injected("""
from collections import namedtuple
Disk = namedtuple('Disk', 'total used free')
update.shutil.disk_usage = lambda path: Disk(100, 100, 0)
sys.exit(update.do_update(sys.argv[2:]))
""", '--from', str(self.source), '--yes')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('disk space', result.stderr)
        self.assertEqual(self.snapshot(self.project), before)

    def test_backup_path_symlink_refused_before_apply(self):
        destination = self.root / 'user-folder'
        destination.mkdir()
        (self.project / '.agents/team/backups').symlink_to(destination, target_is_directory=True)
        before = self.snapshot(self.project)
        result = self.run_update('--yes')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(destination.iterdir()), [])
        self.assertEqual(self.snapshot(self.project), before)

    def test_rollback_without_id_uses_latest(self):
        self.assert_ok(self.run_update('--yes'))
        result = self.run_update('--rollback', source=False)
        self.assert_ok(result)
        self.assertEqual((self.project / '.agents/VERSION').read_text(), '0.1.0\n')

    def test_image_recipe_change_prompts_rebuild(self):
        name = '.agents/bin/teamlib/engine.py'
        text = (self.source / name).read_text()
        self.write(self.source, name, text.replace('WORKDIR /bot', 'WORKDIR /wouf'))
        self.commit(self.source)
        result = self.run_update('--yes')
        self.assert_ok(result)
        self.assertIn('run team image', result.stdout)

    def test_rollback_old_backup_retires_later_managed_additions(self):
        self.assert_ok(self.run_update('--yes'))
        backup_id = next((self.project / '.agents/team/backups').glob('*.tar.gz')).name[:-7]
        self.commit(self.project)
        self.write(self.source, '.agents/skills/later.md', 'later release file\n')
        self.commit(self.source)
        self.assert_ok(self.run_update('--yes'))
        self.assert_ok(self.run_update('--rollback', backup_id, source=False))
        self.assertFalse((self.project / '.agents/skills/later.md').exists())

    def test_help_lists_update(self):
        result = subprocess.run([sys.executable, str(self.project / '.agents/bin/team'), '--help'],
                                env=self.env, capture_output=True, text=True)
        self.assert_ok(result)
        self.assertIn('team update [--from URL|PATH] [--ref REF] [--yes] [--force]', result.stdout)


if __name__ == '__main__':
    unittest.main()
