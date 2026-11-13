import os
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

    def test_a_suite_never_reads_the_callers_stdin(self):
        """A suite that reads stdin to the end gets end of file at once, even while the caller's stdin stays open."""
        with tempfile.TemporaryDirectory() as tmp:
            tests = pathlib.Path(tmp) / 'tests'
            tests.mkdir()
            (tests / 'test_stdin.py').write_text('import sys, unittest\nclass T(unittest.TestCase):\n'
                                                 ' def test_eof(self): self.assertEqual(sys.stdin.read(), "")\n')
            # the runner's stdin is a pipe this test keeps open: a suite that inherited it would wait forever
            with subprocess.Popen([sys.executable, str(RUNNER), '-j', '1'], cwd=tmp, text=True, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as proc:
                try:
                    code = proc.wait(timeout=60)
                finally:
                    proc.kill()
                out = proc.stdout.read()
            self.assertEqual(code, 0, out)
            self.assertIn('Ran 1 test', out)

    def test_a_suite_starts_without_the_callers_wouf_variables(self):
        """A second line of defence: no suite sees the caller's WOUF_* (a sandbox's WOUF_LOG_DIR), the time factor included."""
        with tempfile.TemporaryDirectory() as tmp:
            tests = pathlib.Path(tmp) / 'tests'
            tests.mkdir()
            (tests / 'test_env.py').write_text('import os, unittest\nclass T(unittest.TestCase):\n'
                                               ' def test_none(self):\n'
                                               '  wouf = sorted(k for k in os.environ if k.startswith("WOUF_"))\n'
                                               '  self.assertEqual(wouf, ["WOUF_TEST_TIME_FACTOR"])\n'
                                               '  self.assertEqual(os.environ["WOUF_TEST_TIME_FACTOR"], "3")\n'
                                               '  self.assertEqual(os.environ.get("KEPT"), "1")\n')
            env = dict(os.environ, WOUF_LOG_DIR='/caller/logs', WOUF_EVENTS_DIR='/caller/events', WOUF_STATE_DB='x',
                       WOUF_SECRET_BACKEND='pass', WOUF_PASS_PREFIX='p', WOUF_TEST_TIME_FACTOR='3', KEPT='1')
            result = subprocess.run([sys.executable, str(RUNNER), '-j', '1'], cwd=tmp, text=True, capture_output=True,
                                    env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('Ran 1 test', result.stdout)


if __name__ == '__main__':
    unittest.main()
