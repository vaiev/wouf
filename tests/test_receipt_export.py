"""Export stored receipts without changing their signed contents."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import isolate_wouf_env  # noqa: E402
from tests import test_verify  # noqa: E402
from tests.test_receipt import receipt  # noqa: E402


def setUpModule():
    isolate_wouf_env(secret_store=True)


class ExportTest(unittest.TestCase):
    git = test_verify.VerifyTest.git
    write = test_verify.VerifyTest.write
    verify = test_verify.VerifyTest.verify

    def setUp(self):
        test_verify.VerifyTest.setUp(self)
        self.store = self.dir / 'team' / 'receipts'
        self.store.mkdir()
        self.path = self.store / (self.head + '.json')
        self.path.write_bytes(self.file.read_bytes())

    def export(self, *args):
        return subprocess.run([sys.executable, str(test_verify.TEAM), 'receipt', 'export', *map(str, args)],
                              capture_output=True, text=True, env={**os.environ, 'WOUF_TEAM_DIR': str(self.dir / 'team')})

    def test_export_round_trip(self):
        result = self.export(self.head, '--in-toto', '--repo', self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        statement = json.loads(result.stdout)
        self.assertEqual(result.stdout, json.dumps(statement, sort_keys=True, separators=(',', ':'), ensure_ascii=True) + '\n')
        self.assertEqual(result.stdout, self.export(self.head, '--in-toto').stdout)
        stored = receipt.parse(self.file.read_bytes())
        self.assertEqual(statement, {'_type': 'https://in-toto.io/Statement/v1',
                         'subject': [{'name': self.body['repo'], 'digest': {'gitCommit': self.head}}],
                         'predicateType': 'https://wouf.dev/receipt/v1',
                         'predicate': {**stored, 'namespace': 'wouf-receipt-v1'}})
        predicate = statement['predicate']
        self.file.write_bytes(receipt.stored(predicate['receipt'], predicate['sig']))
        self.assertEqual(self.verify(self.file, '--commit', self.head).returncode, 0)

    def test_export_refusals(self):
        for sha, reason in [('A' * 40, '40'), ('a' * 40, 'no stored receipt')]:
            with self.subTest(reason=reason):
                done = self.export(sha, '--in-toto')
                self.assertEqual(done.returncode, 1)
                self.assertEqual(done.stdout, '')
                self.assertEqual(len(done.stderr.splitlines()), 1)
                self.assertIn(reason, done.stderr)
        self.path.write_bytes(b'bad')
        done = self.export(self.head, '--in-toto')
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout, '')
        self.assertIn('JSON', done.stderr)
        self.path.write_bytes(receipt.stored({**self.body, 'change': {**self.body['change'], 'landed': 'a' * 40}}, receipt.parse(self.file.read_bytes())['sig']))
        done = self.export(self.head, '--in-toto')
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout, '')
        self.assertIn('landed', done.stderr)
        self.assertEqual(len(done.stderr.splitlines()), 1)

    def test_requires_format(self):
        done = self.export(self.head)
        self.assertEqual(done.returncode, 2)
        self.assertEqual(done.stdout, '')
        self.assertIn('usage: team receipt export', done.stderr)
