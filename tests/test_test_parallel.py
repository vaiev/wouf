import pathlib
import subprocess
import sys
import tempfile
import unittest

RUNNER = pathlib.Path(__file__).resolve().parents[1] / 'scripts' / 'test-parallel.py'


class ParallelRunnerTests(unittest.TestCase):
    def test_totals_failure_output_and_parallel_equivalence(self):
        with tempfile.TemporaryDirectory() as tmp:
            tests = pathlib.Path(tmp) / 'tests'
            tests.mkdir()
            (tests / 'test_pass.py').write_text('import unittest\nclass T(unittest.TestCase):\n def test_ok(self): pass\n @unittest.skip("later")\n def test_skip(self): pass\n')
            (tests / 'test_fail.py').write_text('import unittest\nclass T(unittest.TestCase):\n def test_bad(self): self.fail("distinct failure")\n')
            outputs = []
            for jobs in (1, 4):
                result = subprocess.run([sys.executable, str(RUNNER), '-j', str(jobs), '--slowest', '1'], cwd=tmp, text=True, capture_output=True)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn('distinct failure', result.stdout)
                self.assertIn('Ran 3 tests', result.stdout)
                self.assertIn('FAILED (failures=1, errors=0, skipped=1)', result.stdout)
                self.assertIn('test_pass', result.stdout)
                self.assertIn('test_fail', result.stdout)
                self.assertIn('Slowest', result.stdout)
                outputs.append(result.stdout)


if __name__ == '__main__':
    unittest.main()
