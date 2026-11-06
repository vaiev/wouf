"""Exercise the ratchet against real, isolated git commits."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/complexity.py'


def function(points, name='f'):
    return 'def %s(x):\n' % name + '    if x: pass\n' * (points - 1)


class RatchetTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.git('init', '-q')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Test')

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.repo, text=True).strip()

    def commit(self, source, message='fixture', path='code.py'):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source)
        self.git('add', '.')
        self.git('commit', '-qm', message)
        return self.git('rev-parse', 'HEAD')

    def check(self, base, head):
        return subprocess.run([sys.executable, str(SCRIPT), base, head],
                              cwd=self.repo, text=True, capture_output=True)

    def test_thresholds(self):
        for old, new, expected in [(3, 7, 1), (3, 5, 0), (3, 16, 1), (3, 10, 0)]:
            with self.subTest(new=new):
                base = self.commit(function(old), 'base %s' % new)
                head = self.commit(function(new, 'new' if new in (16, 10) else 'f'))
                result = self.check(base, head)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                self.assertIn('files,', result.stdout)
                if expected:
                    self.assertIn('code.py:1', result.stdout)
                    self.assertIn('->', result.stdout)

    def test_depth(self):
        base = self.commit('def f(x):\n    if x:\n        if x: pass\n')
        head = self.commit('def f(x):\n    if x:\n        if x:\n            if x:\n                if x: pass\n')
        result = self.check(base, head)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('depth 2 -> 4', result.stdout)

    def test_escape(self):
        base = self.commit(function(3))
        head = self.commit(function(7), 'Deliberate\n\nComplexity: f required decision table')
        result = self.check(base, head)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('ACCEPTED', result.stdout)
        self.assertIn('required decision table', result.stdout)

    def test_non_python_and_invalid_base(self):
        base = self.commit('invalid python !')
        self.commit('not python at all', path='notes.txt')
        head = self.commit(function(3))
        result = self.check(base, head)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('syntax', result.stdout.lower())
        self.assertIn('1 files, 1 functions, 0 failures', result.stdout)
        self.assertNotIn('Traceback', result.stderr)

    def test_extensionless_and_invalid_head(self):
        base = self.commit(function(3), path='.agents/bin/helper')
        head = self.commit(function(7), path='.agents/bin/helper')
        self.assertEqual(self.check(base, head).returncode, 1)
        broken = self.commit('def broken(', path='.agents/bin/helper')
        result = self.check(head, broken)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('Traceback', result.stderr)

    def test_removed_and_unchanged_functions(self):
        base = self.commit(function(20, 'legacy') + function(3))
        head = self.commit(function(20, 'legacy'))
        result = self.check(base, head)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('1 functions, 0 failures', result.stdout)

    def test_escape_is_exact_and_head_only(self):
        base = self.commit(function(3), 'Complexity: f previous excuse')
        head = self.commit(function(7), 'Complexity: foo unrelated')
        self.assertEqual(self.check(base, head).returncode, 1)

    @unittest.skipIf(sys.version_info < (3, 10), 'match requires Python 3.10')
    def test_cases(self):
        base = self.commit('def f(x): pass\n')
        head = self.commit('def f(x):\n    match x:\n' +
                           ''.join('        case %s: pass\n' % i for i in range(4)))
        result = self.check(base, head)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('complexity 1 -> 5, depth 0 -> 1', result.stdout)

    def test_metrics_and_scopes(self):
        spec = importlib.util.spec_from_file_location('complexity', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        metrics = module.functions('''class C:
    async def f(self, xs):
        if xs and xs or xs:
            for x in xs:
                while x:
                    pass
        try:
            pass
        except ValueError:
            pass
        return [x if x else 0 for x in xs if x]
        def nested():
            if xs: pass
''')
        self.assertEqual(metrics['C.f'], (2, 10, 3))
        self.assertEqual(metrics['C.f.nested'], (12, 2, 1))
        branches = module.functions('def f(x):\n    if x: pass\n    elif x: pass\n'
                                    '    else:\n        if x: pass\n')
        self.assertEqual(branches['f'], (1, 4, 2))


if __name__ == '__main__':
    unittest.main()
