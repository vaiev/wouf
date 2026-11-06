"""Context graphs use source text only and stay deterministic and bounded."""
import importlib
import io
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.agents/bin'))


class GraphTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.module = importlib.import_module('teamlib.graph')
        self.file('src/api.py', 'class Widget:\n    def run(self, n: int = 2) -> str:\n        return str(n)\n\nasync def fetch(url: str):\n    pass\n')
        self.file('src/lib.rs', 'pub struct Engine {}\npub fn start(x: i32) -> i32 { x }\nimpl Engine {}\n')
        self.file('src/web.ts', 'export interface View {}\nexport function render(x: string): void {}\n')
        self.file('caller.py', 'from src.api import Widget\nWidget().run(3)\nfetch("example")\n')
        self.file('tests/test_api.py', 'def test_widget():\n    assert Widget().run()\n')

    def file(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def graph(self, globs=None, limit=400):
        return self.module.graph(self.repo, globs or ['src/**'], limit)

    def test_fixture_and_determinism(self):
        with patch.object(self.module.shutil, 'which', return_value=None):
            result = self.graph()
            self.assertEqual(result, self.graph())
        for expected in ('Widget', 'run(self, n: int=2) -> str', 'fetch(url: str)',
                         'src/api.py:2', 'Engine', 'start', 'render', 'View',
                         'caller.py:2', 'tests/test_api.py:2', 'Python: ast',
                         'Rust: regex', 'TypeScript: regex'):
            self.assertIn(expected, result)

    def test_cap_and_small_limit(self):
        self.file('src/many.py', ''.join('def f%d(): pass\n' % i for i in range(600)))
        for limit in (1, 12, 400, 900):
            self.assertLessEqual(len(self.graph(limit=limit).splitlines()), min(limit, 400))
        self.assertIn('truncated', self.graph())

    def test_extensionless_python_script(self):
        self.file('src/tool', '#!/usr/bin/env python3\ndef tool(arg): pass\n')
        result = self.graph(['src/tool'])
        self.assertIn('Python: ast', result)
        self.assertIn('src/tool:2 def tool(arg)', result)

    def test_no_matches(self):
        self.assertIn('No files matched', self.graph(['missing/**']))

    def test_skips(self):
        self.file('.git/hidden.py', 'def hidden(): pass\n')
        self.file('src/big.py', 'x' * (1024 * 1024))
        (self.repo / 'src/binary.py').write_bytes(b'abc\x00def')
        (self.repo / 'src/invalid.py').write_bytes(b'\xff')
        result = self.graph(['**'])
        self.assertIn('large=1', result)
        self.assertIn('binary=2', result)
        self.assertIn('.git=1', result)
        self.assertNotIn('hidden.py', result)

    def test_forced_fallback_c(self):
        self.file('src/native.c', 'struct Native { int x; };\nint native(int x) { return x; }\n')
        with patch.object(self.module.shutil, 'which', return_value=None):
            result = self.graph()
        self.assertIn('C: regex', result)
        self.assertIn('native(int x)', result)

    def test_ctags(self):
        completed = subprocess.CompletedProcess([], 0, '{"_type":"tag","name":"render","path":"src/web.ts","line":2,"kind":"function","signature":"(x: string)"}\n')
        with patch.object(self.module.shutil, 'which', return_value='/usr/bin/ctags'), patch.object(
                self.module.subprocess, 'run', return_value=completed) as run:
            result = self.graph(['src/web.ts'])
        self.assertIn('TypeScript: ctags', result)
        self.assertIn('src/web.ts:2', result)
        self.assertIn('--options=NONE', run.call_args.args[0])

    def test_mask_and_cli(self):
        # Synthetic key shape, assembled here, never usable as a credential.
        value = 'xox' + 'b-1234567890-abcdefghijklmnop'
        self.file('src/secret.py', 'def sample(value="' + value + '"): pass\n')
        self.assertNotIn(value, self.graph())
        target = self.repo / 'CONTEXT_GRAPH.md'
        done = subprocess.run([sys.executable, str(ROOT / '.agents/bin/team'), 'graph',
                               '--paths', 'src/*.py', '--repo', str(self.repo), '-o', str(target)],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(target.read_text(), self.graph(['src/*.py']))
        self.assertNotIn(value, target.read_text())

    def test_callers_in_other_matched_files_and_caps(self):
        self.file('src/consumer.py', 'fetch("example")\n' * 12)
        with patch.object(self.module.shutil, 'which', return_value=None):
            result = self.graph()
        self.assertIn('caller src/consumer.py:1 fetch', result)
        self.assertNotIn('caller src/consumer.py:6 fetch', result)

    def test_ctags_failure_falls_back(self):
        with patch.object(self.module.shutil, 'which', return_value='/usr/bin/ctags'), patch.object(
                self.module.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ctags', 0.5)):
            result = self.graph()
        self.assertIn('Rust: regex', result)
        self.assertIn('TypeScript: regex', result)

    def test_mask_before_signature_truncation(self):
        value = 'xox' + 'b-1234567890-abcdefghijklmnop'
        self.file('src/long.py', 'def long(value="' + 'a' * 270 + ' ' + value + '"): pass\n')
        self.assertNotIn('xox' + 'b-', self.graph())

    def test_home_paths_library_and_cli(self):
        paths = ['/home/fixture-user/project', '/home/a b/c',
                 r'C:\Users\x', '~/x', '//host/UsErS/x',
                 r'\\host\home\x', '/srv/HOME/x', '/Users/x']
        self.repo = self.repo / 'home' / 'fixture-repo'
        self.repo.mkdir(parents=True)
        own = str(self.repo.resolve() / 'home' / 'kept.py')
        relative = 'src/home/x.py'
        source = '\n'.join('def sample%d(value=%r): pass' % (i, value)
                           for i, value in enumerate(paths + [relative, own]))
        self.file('src/paths.py', source)
        self.file('src/home/x.py', 'def kept(): pass\n')
        self.file('src/long_path.py', 'def long_path(value=' + repr('a' * 265 + ' /home/' + 'x' * 80) + '): pass\n')
        result = self.graph()
        self.assertGreaterEqual(result.count('[path]'), len(paths) + 1)
        for value in ('/home/fixture-user', '/home/a', 'Users', '~/x', '/srv/HOME', '/Users', '/home/' + 'x' * 10):
            self.assertNotIn(value, result)
        self.assertIn("sample1(value='[path]')", result)
        self.assertIn(relative, result)
        self.assertNotIn(str(self.repo.resolve()), result)
        self.assertIn("value='home/kept.py'", result)
        target = self.repo / 'graph.md'
        done = subprocess.run([sys.executable, str(ROOT / '.agents/bin/team'), 'graph',
                               '--paths', 'src/**', '--repo', str(self.repo), '-o', str(target)],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(result, target.read_text())

    def test_repository_root_is_relative_before_home_masking(self):
        # Model an absolute /home/u/... root without writing outside the fixture.
        root = Path('/home/u/fixture-repo')
        with patch.object(Path, 'resolve', return_value=root):
            for value, expected in (
                    ('/home/u/fixture-repo/src/a.py', 'src/a.py'),
                    ('/home/u/fixture-repo/home/a.py', 'home/a.py'),
                    ('/home/u/fixture-repo', '.'),
                    ('/home/u/fixture-repo-other/src/a.py', '[path]'),
                    ('/home/other/a.py', '[path]'),
                    ('src/home/x.py', 'src/home/x.py')):
                with self.subTest(value=value):
                    self.assertEqual(self.module._mask(value, root), expected)

    def test_path_boundaries_and_all_output_lines(self):
        self.file('src/paths.ts', 'function show(value="/home/x") {}\n')
        self.file('src/home/kept.ts', 'function kept() {}\n')
        with patch.object(self.module.shutil, 'which', return_value=None):
            result = self.graph()
        self.assertIn('value="[path]"', result)
        self.assertIn('src/home/kept.ts:1', result)
        self.file('src/=/home/x.py', 'def unusual(): pass\n')
        result = self.graph()
        self.assertIn('## src/=[path]', result)
        self.assertNotIn('src/=/home/x.py', result)
        preserved = ['src/home/x.py', 'https://host/home/x', 'word/home/x',
                     './home/x', 'v1:/home/x', '/opt/code/x']
        for value in preserved:
            self.assertEqual(self.module._mask(value, self.repo), value)
        for value in ('"/home/x"', '(/Users/x)', '=/home/x,', '[~/x]', r'=C:\Users\x'):
            self.assertIn('[path]', self.module._mask(value, self.repo))

    def test_string_literals_with_absolute_roots_are_masked_whole(self):
        source = r'''def literals(a="/Volumes/Shared Disk/Users/fixture/private.txt", b="/data/x", c="C:\\Program Files\\x", d="~/.ssh/id", e="src/a.py", f=f"/data/{name}", g=None, h=3): pass
'''
        self.file('src/literals.py', source)
        self.file('src/lit.ts', 'function show(value="/Volumes/Shared Disk/Users/x") {}\n')
        expected = "a='[path]', b='[path]', c='[path]', d='[path]', e='src/a.py', f=f'[path]', g=None, h=3"
        with patch.object(self.module.shutil, 'which', return_value=None):
            result = self.graph(['src/literals.py', 'src/lit.ts'])
        self.assertIn(expected, result)
        self.assertIn('function show(value="[path]")', result)
        for leaked in ('Volumes', 'Shared', 'private', '/data', 'ssh', 'Program'):
            self.assertNotIn(leaked, result)
        target = self.repo / 'literals.md'
        done = subprocess.run([sys.executable, str(ROOT / '.agents/bin/team'), 'graph',
                               '--paths', 'src/literals.py,src/lit.ts', '--repo', str(self.repo), '-o', str(target)],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(expected, target.read_text())
        self.assertNotIn('Shared', target.read_text())

    def test_literal_rule_on_text(self):
        root = Path('/home/u/fixture-repo')
        with patch.object(Path, 'resolve', return_value=root):
            for value, expected in (
                    ('"/Volumes/Shared Disk/Users/fixture/private.txt"', '"[path]"'),
                    ('"/data/x"', '"[path]"'),
                    (r'"C:\\Program Files\\x"', '"[path]"'),
                    ("'~/.ssh/id'", "'[path]'"),
                    ('f"/data/{name}"', 'f"[path]"'),
                    ('"src/a.py"', '"src/a.py"'),
                    ('"/home/u/fixture-repo/src/a.py"', '"src/a.py"'),
                    ('"/home/u/fixture-repo"', '"."'),
                    ('"see /home/u/fixture-repo/src/a.py"', '"see src/a.py"'),
                    ('"x=/home/u/fixture-repo/a"', '"x=a"'),
                    ('"x=/home/other/a"', '"[path]"'),
                    ('"https://host/home/x"', '"https://host/home/x"'),
                    ('None 3', 'None 3')):
                with self.subTest(value=value):
                    self.assertEqual(self.module._mask(value, root), expected)

    def test_all_quoting_forms_and_final_boundary(self):
        literals = [
            '`/Volumes/Shared Disk/Users/fixture/private.txt`',
            '`/data/${name}`',
            '"""/home/u/x"""',
            "'''/data/x'''",
            'r"/data/x"', 'f"/data/{name}"', 'br"/data/x"', 'rf"/data/{name}"', 'fr"/data/{name}"', 'rb"/data/x"',
            'r#"/home/u/x"#', 'r###"/data/x"###',
            "'/home/u/x'", 'b"/data/x"',
        ]
        for literal in literals:
            with self.subTest(literal=literal):
                result = self.module._mask(literal, self.repo)
                self.assertIn('[path]', result)
                self.assertNotIn('/data', result)
                self.assertNotIn('/home', result)
                opening = literal[:literal.index('/')]
                quote = '"""' if '"""' in opening else "'''" if "'''" in opening else '`' if '`' in opening else '"' if '"' in opening else "'"
                hashes = opening.count('#')
                self.assertEqual(result, opening + '[path]' + quote + '#' * hashes)
        self.file('src/templates.ts', 'function show(value=`/Volumes/Shared Disk/Users/fixture/private.txt`) {}\nfunction template(value=`/data/${name}`) {}\n')
        self.file('src/triple.py', 'def triple(value="""/home/u/x"""): pass\n')
        self.file('src/raw.rs', 'fn raw(value: &str = r#"/home/u/x"#) {}\n')
        self.file('src/mixed.ts', 'function mixed(value=/Volumes/Shared Disk/Users/fixture/private.txt) {}\nfunction clean(value=3) {}\n')
        with patch.object(self.module.shutil, 'which', return_value=None):
            result = self.graph()
        self.assertIn('value=`[path]`', result)
        self.assertIn('function template(value=`[path]`)', result)
        self.assertIn('r#"[path]"#', result)
        self.assertIn('[line with a private path removed]', result)
        self.assertIn('Private path lines removed: 1', result)
        self.assertIn('function clean(value=3)', result)
        self.assertNotIn('Shared', result)
        target = self.repo / 'quoting.md'
        done = subprocess.run([sys.executable, str(ROOT / '.agents/bin/team'), 'graph',
                               '--paths', 'src/**', '--repo', str(self.repo), '-o', str(target)],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn('Shared', target.read_text())
        self.assertIn('Private path lines removed: 1', target.read_text())

    def test_final_check_precedes_truncation(self):
        value = 'function long(value=/Volumes/' + 'x' * 320 + ' Disk/Users/fixture/x) {}'
        self.file('src/long.ts', value)
        with patch.object(self.module.shutil, 'which', return_value=None):
            result = self.graph(['src/long.ts'])
        self.assertIn('[line with a private path removed]', result)
        self.assertIn('Private path lines removed: 1', result)
        self.assertNotIn('/Volumes', result)
        self.assertEqual(self.module._line_check('clean src/home/x.py'), 'clean src/home/x.py')

    def test_uri_paths_and_relative_paths_library_and_cli(self):
        private = ['file:///home/alice/secret.txt', 'file:///Users/alice/x',
                   'file:/home/x', 'see /USERS/bob/x', 'see ~alice/x',
                   r'\home\fixture\x', 'value=prefix~/x']
        for value in private:
            with self.subTest(value=value):
                self.assertEqual(self.module._line_check(value), self.module.PRIVATE_LINE)
        for value in ('src/home/x.py', 'clean src/home/x.py', 'src/home/x.py file:/opt/x', '~0', '~x'):
            self.assertEqual(self.module._line_check(value), value)
            self.assertEqual(self.module._mask(value, self.repo), value)
        self.file('src/uri.ts', ''.join(
            'function uri%d(value=%s) {}\n' % (i, value)
            for i, value in enumerate(private[:4])) +
            'function invert(value=~0) {}\nfunction relative(value="src/home/x.py") {}\n')
        self.file('src/home/x.py', 'def kept(): pass\n')
        self.file('src/uri.py', ''.join(
            'def uri%d(value=%r): pass\n' % (i, value)
            for i, value in enumerate(private[:3])))
        with patch.object(self.module.shutil, 'which', return_value=None):
            result = self.graph()
        self.assertIn('Private path lines removed: 6', result)
        self.assertIn('src/home/x.py:1 def kept()', result)
        self.assertIn('function relative(value="src/home/x.py")', result)
        self.assertIn('function invert(value=~0)', result)
        for value in private[:3]:
            self.assertNotIn(value, result)
        target = self.repo / 'uri.md'
        done = subprocess.run([sys.executable, str(ROOT / '.agents/bin/team'), 'graph',
                               '--paths', 'src/**', '--repo', str(self.repo), '-o', str(target)],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(result, target.read_text())

    def test_annotated_relative_paths_library_and_cli(self):
        python = "def show(value: str = 'src/home/config.py') -> str: pass\n"
        typescript = 'function show(value: string = "src/home/config.py"): string {}\n'
        self.file('src/relative.py', python)
        self.file('src/relative.ts', typescript)
        for value in ("def show(value: str = 'src/home/config.py') -> str",
                      'function show(value: string = "src/home/config.py"): string',
                      "mapping = {'path': 'src/users/config.py'}",
                      'value = flag ? src/home/x.py : src/users/x.py'):
            self.assertEqual(self.module._line_check(value), value)
        for value in ('file:///home/alice/x', 'file:///Users/alice/x', 'file:/home/x',
                      '/USERS/x', r'C:\Users\x', r'\\host\home\x', '~/x', '~name/x'):
            self.assertEqual(self.module._line_check(value), self.module.PRIVATE_LINE)
        with patch.object(self.module.shutil, 'which', return_value=None):
            result = self.graph(['src/relative.*'])
        self.assertIn("def show(value: str='src/home/config.py') -> str", result)
        self.assertIn(typescript.split(' {}')[0], result)
        self.assertIn('Private path lines removed: 0', result)
        target = self.repo / 'relative.md'
        done = subprocess.run([sys.executable, str(ROOT / '.agents/bin/team'), 'graph',
                               '--paths', 'src/relative.*', '--repo', str(self.repo), '-o', str(target)],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(result, target.read_text())

    def test_cli_invalid_arguments(self):
        for args in ([], ['--paths'], ['--unknown'], ['--paths', '**', '--repo', 'missing']):
            done = subprocess.run([sys.executable, str(ROOT / '.agents/bin/team'), 'graph', *args],
                                  cwd=self.repo, capture_output=True, text=True)
            self.assertEqual(done.returncode, 2, done.stderr)
            self.assertIn('team graph --paths', done.stderr)

    def test_symlinks_do_not_leave_repository(self):
        (self.repo / 'src/link.py').symlink_to(ROOT / 'tests/test_graph.py')
        self.assertNotIn('link.py', self.graph())


class GoldenGraphTest(unittest.TestCase):
    """`team graph` of the current tree gives main's bytes on the repository at main."""

    def git(self, *args):
        return subprocess.run(['git', '-C', str(ROOT), *args], capture_output=True)

    def team_graph(self, team, repo, target):
        done = subprocess.run([sys.executable, str(team), 'graph', '--paths', '.agents/bin/**',
                               '--repo', str(repo), '-o', str(target)], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return target.read_bytes()

    def test_agents_bin_graph_matches_main(self):
        # The expected bytes come from main's own code in this run, never from a stored file.
        refs = (os.environ.get('GRAPH_GOLDEN_REF'), 'main', 'origin/main')
        ref = next((ref for ref in refs if ref and not self.git('rev-parse', '--verify', '-q', ref + '^{commit}').returncode), None)
        if ref is None:
            self.skipTest('no main ref in this checkout (set GRAPH_GOLDEN_REF)')
        archive = self.git('archive', '--format=tar', ref)
        self.assertEqual(archive.returncode, 0, archive.stderr)
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp) / 'main'
            with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
                if hasattr(tarfile, 'data_filter'):
                    tar.extractall(tree, filter='data')
                else:
                    tar.extractall(tree)
            expected = self.team_graph(tree / '.agents/bin/team', tree, Path(tmp) / 'expected.md')
            actual = self.team_graph(ROOT / '.agents/bin/team', tree, Path(tmp) / 'actual.md')
        self.assertIn(b'## .agents/bin/federate:1', expected)
        self.assertEqual(actual, expected)


if __name__ == '__main__':
    unittest.main()
