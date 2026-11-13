"""The real upgrade from 0.1.0 to the pack's own release: the shipped .agents/upgrade/0.2.0.md stays in the pack."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import isolate_wouf_env, wouf_env, writable_copy  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / '.agents'
MANIFEST = '.agents/team/.pack-files'
CONFIG = '.agents/team/config.ini'
BACKUPS = '.agents/team/backups/'
STORAGES = {
    '.agents/team/env/worker/home/.cache/note': b'bot home\n',
    '.agents/team/env/worker/tasks/T-1/out/result.txt': b'a result\n',
    '.agents/team/env/worker/storage/data.bin': bytes(range(256)),
    '.agents/team/env/worker/login/.local/share/keyrings/login.keyring': b'sign-in, not a secret value\n',
    '.agents/team/env/reviewer/login/.codex/auth.json': b'{}\n',
}


def setUpModule() -> None:
    isolate_wouf_env()


class UpgradeTo020Test(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / 'project'
        self.source = self.root / 'source'
        self.scratch = self.root / 'scratch'
        self.scratch.mkdir()
        with wouf_env(HOME=str(self.root), TMPDIR=str(self.scratch), PYTHONDONTWRITEBYTECODE='1'):
            self.env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        self.assertEqual([k for k in self.env if k.startswith('WOUF_')], [])
        for directory in (self.project, self.source):
            writable_copy(PACK, directory / '.agents', ignore=shutil.ignore_patterns('__pycache__', 'backups'))
            self.git(directory, 'init', '-q')
            self.git(directory, 'config', 'user.name', 'Test')
            self.git(directory, 'config', 'user.email', 'test@example.invalid')
        self.assertTrue((self.project / '.agents/upgrade/0.2.0.md').is_file())
        (self.project / '.agents/VERSION').write_text('0.1.0\n', encoding='utf-8')
        # Hand-edited values (a team that runs its bots on the host with a stand-in runner) and a hand-written
        # note with a non-ASCII character and a CR, which must survive byte for byte.
        self.config = (self.project / CONFIG).read_text(encoding='utf-8')
        for old, new in (('\nautonomy = ask\n', '\nautonomy = auto\n'), ('\nrunner = auto\n', '\nrunner = fake\n'),
                         ('\nmode = undeclared\n', '\nmode = host\n')):
            self.assertIn(old, self.config)
            self.config = self.config.replace(old, new, 1)
        self.config += '\n[runner.fake]\ncommand = git\nauth =\n# hand note — kept\r\n'
        (self.project / CONFIG).write_bytes(self.config.encode('utf-8'))
        for name, data in STORAGES.items():
            path = self.project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (self.project / MANIFEST).write_text('\n'.join(self.managed(self.project)) + '\n', encoding='utf-8')
        self.commit(self.project)
        self.commit(self.source)

    def git(self, directory, *args) -> str:
        return subprocess.run(['git', '-C', str(directory), *args], env=self.env,
                              capture_output=True, text=True, check=True).stdout.strip()

    def commit(self, directory) -> None:
        self.git(directory, 'add', '-A')
        self.git(directory, 'commit', '-qm', 'fixture')

    def managed(self, directory) -> list[str]:
        return sorted(p.relative_to(directory).as_posix() for p in (directory / '.agents').rglob('*')
                      if p.is_file() and p.relative_to(directory / '.agents').parts[0] != 'team')

    def snapshot(self, top: Path) -> dict:
        return {p.relative_to(self.project).as_posix(): (p.read_bytes(), p.stat().st_mode & 0o777)
                for p in top.rglob('*') if p.is_file() and '.git' not in p.relative_to(self.project).parts}

    def backups(self, files: dict, inside: bool = True) -> dict:
        return {k: v for k, v in files.items() if k.startswith(BACKUPS) == inside}

    def update(self) -> subprocess.CompletedProcess:
        result = subprocess.run([sys.executable, str(self.project / '.agents/bin/team'), 'update',
                                 '--from', str(self.source), '--yes'],
                                cwd=self.project, env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(list(self.scratch.iterdir()), [], result.stdout + result.stderr)
        return result

    def test_the_real_path_from_0_1_0_to_0_2_0(self) -> None:
        self.assertEqual((self.source / '.agents/VERSION').read_text().strip(), '0.2.0')
        env_before = self.snapshot(self.project / '.agents/team/env')
        self.assertEqual({k: env_before[k][0] for k in STORAGES}, STORAGES)
        result = self.update()
        self.assertIn('0.1.0 -> 0.2.0', result.stdout)
        self.assertEqual((self.project / '.agents/VERSION').read_text().strip(), '0.2.0')
        self.assertTrue((self.project / '.agents/upgrade/0.2.0.md').is_file())
        self.assertEqual((self.project / CONFIG).read_bytes(), self.config.encode('utf-8'))
        self.assertEqual(self.snapshot(self.project / '.agents/team/env'), env_before)

    def test_a_second_update_changes_nothing(self) -> None:
        self.update()
        self.commit(self.project)  # a user commits the updated pack; update refuses uncommitted managed files
        after_first = self.snapshot(self.project)
        result = self.update()
        self.assertIn('Added: 0\nChanged: 0\nRetired: 0\n', result.stdout)
        after_second = self.snapshot(self.project)
        # update.py:344-346 always backs up, even for an empty plan; making that a no-op is follow-up T-0191.
        # Until then the second run adds exactly one backup, and nothing else changes.
        old_backups, new_backups = self.backups(after_first), self.backups(after_second)
        self.assertEqual(len(new_backups), len(old_backups) + 1, sorted(new_backups))
        self.assertEqual({k: new_backups.get(k) for k in old_backups}, old_backups)
        self.assertEqual(self.backups(after_second, inside=False), self.backups(after_first, inside=False))
        self.assertEqual((self.project / '.agents/VERSION').read_text().strip(), '0.2.0')


if __name__ == '__main__':
    unittest.main()
