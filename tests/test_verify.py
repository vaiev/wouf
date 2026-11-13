"""Offline receipt verification against real Git objects."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import isolate_wouf_env  # noqa: E402
from tests.test_receipt import sample, keypair, public, receipt  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TEAM = ROOT / '.agents/bin/team'


def setUpModule():
    isolate_wouf_env(secret_store=True)


@unittest.skipUnless(shutil.which('ssh-keygen') and shutil.which('git'), 'needs git and ssh-keygen')
class VerifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.repo = self.dir / 'repo'
        self.repo.mkdir()
        (self.dir / 'team').mkdir()
        (self.dir / 'team' / 'config.ini').write_text('[team]\nname = test\n')
        self.git('init', '-q')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.com')
        (self.repo / 'a').write_text('base')
        self.git('add', 'a')
        self.git('commit', '-qm', 'base')
        self.base = self.git('rev-parse', 'HEAD')
        (self.repo / 'a').write_text('change')
        self.git('commit', '-qam', 'change')
        self.head = self.git('rev-parse', 'HEAD')
        self.tree = self.git('rev-parse', 'HEAD^{tree}')
        self.key = keypair(self.dir / 'key')
        self.signers = self.dir / 'signers'
        self.signers.write_text('werkstatt ' + public(self.key) + '\n')
        self.body = sample()
        c = self.body['change']
        c.update(base=self.base, landed_parent=self.base, reviewed=self.head, landed=self.head, tree=self.tree, files=1, merge='fast-forward')
        self.body['gate'].update(head=self.head, base=self.base)
        self.body['reviews']['rounds'][0].update(head=self.head, approve_sha=self.head[:12])
        self.file = self.dir / 'receipt.json'
        self.write()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True).strip()

    def write(self):
        sig = receipt.sign(self.body, str(self.key))
        self.file.write_bytes(receipt.stored(self.body, sig))

    def verify(self, *args):
        return subprocess.run([sys.executable, str(TEAM), 'verify', *map(str, args), '--repo', str(self.repo), '--signers', str(self.signers)], capture_output=True, text=True, env={**os.environ, 'WOUF_TEAM_DIR': str(self.dir / 'team')})

    def test_complete_and_partial(self):
        done = self.verify(self.file, '--commit', self.head)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn('verified ', done.stdout)
        self.assertIn('SHA256:', done.stdout.splitlines()[-1])
        self.assertEqual(self.verify(self.file).returncode, 3)

    def test_bad_signature_and_wrong_subject(self):
        data = self.file.read_bytes().replace(b'"repo":"wouf"', b'"repo":"wouh"')
        self.file.write_bytes(data)
        self.assert_failure('signature')
        self.write()
        self.assert_failure('subject', self.base)

    def raw_write(self):
        sig = subprocess.run(['ssh-keygen', '-Y', 'sign', '-q', '-f', str(self.key), '-n', 'wouf-receipt-v1'], input=receipt.canonical(self.body), capture_output=True, check=True).stdout.decode()
        self.file.write_bytes(receipt.canonical({'receipt': self.body, 'sig': sig}) + b'\n')

    def assert_failure(self, check, commit=None, *args):
        done = self.verify(self.file, '--commit', commit or self.head, *args)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        details = {'signature': 'untrusted or invalid signature', 'subject': 'commit binding',
                   'landed': 'tree and parent', 'reviewed': 'tree, base and files',
                   'form': 'invalid receipt', 'reviews': 'approval consistency',
                   'flags': 'flag consistency', 'brief': 'brief hash'}
        self.assertIn('FAIL ' + check + ': ' + details[check], done.stdout.splitlines())

    def test_inconsistent_records_fail(self):
        import copy
        original = copy.deepcopy(self.body)
        cases = [
            ('landed tree', 'landed', lambda b: b['change'].update(tree='a' * 40)),
            ('landed parent and gate base against real parent', 'landed', lambda b: (b['change'].update(merge='squash', base=self.head, landed_parent=self.head), b['gate'].update(base=self.head))),
            ('reviewed tree', 'reviewed', lambda b: (b['change'].update(merge='squash', reviewed=self.base), b['gate'].update(head=self.base), b['reviews']['rounds'][0].update(head=self.base, approve_sha=self.base[:12]))),
            ('red gate', 'form', lambda b: b['gate'].update(state='red')),
            ('gate base differs from change base', 'form', lambda b: b['gate'].update(base=self.head)),
            ('approval on another real head', 'form', lambda b: b['reviews']['rounds'][0].update(head=self.base, approve_sha=self.base[:12])),
            ('forged counts', 'form', lambda b: b['reviews']['rounds'][0].update(approve_sha='a' * 7)),
            ('too few approvals', 'reviews', lambda b: b['reviews'].update(required=2)),
            ('sovereign public host', 'flags', lambda b: (b['flags'].update(sovereign=True), [r['egress'].update(mode='proxy', log='ab' * 32) for r in b['runs']])),
            ('sovereign bridge gate', 'flags', lambda b: (b['flags'].update(sovereign=True), b['gate'].update(network='bridge'), [r['egress'].update(mode='proxy', log='ab' * 32, hosts=[]) for r in b['runs']])),
        ]
        for name, check, mutate in cases:
            with self.subTest(case=name):
                self.body = copy.deepcopy(original)
                mutate(self.body)
                self.raw_write()
                self.assert_failure(check)

    def test_secret_refused_without_disclosure(self):
        secret = 'AKIA' + 'IOSFODNN7EXAMPL0'
        self.body['repo'] = secret
        self.raw_write()
        done = self.verify(self.file, '--commit', self.head)
        self.assertEqual(done.returncode, 1)
        self.assertIn('FAIL secrets: invalid receipt', done.stdout.splitlines())
        self.assertNotIn(secret, done.stdout + done.stderr)

    def test_missing_objects_and_brief(self):
        self.body['change']['reviewed'] = 'a' * 40
        self.body['gate']['head'] = 'a' * 40
        self.body['reviews']['rounds'][0].update(head='a' * 40, approve_sha='a' * 7)
        self.body['change']['merge'] = 'squash'
        self.write()
        self.assertEqual(self.verify(self.file, '--commit', self.head).returncode, 3)
        self.body['change']['landed'] = 'b' * 40
        self.write()
        self.assertEqual(self.verify(self.file).returncode, 3)
        self.assertEqual(self.verify(self.file, '--commit', 'b' * 40).returncode, 3)
        self.body['change'].update(landed=self.head, reviewed=self.head, merge='fast-forward')
        self.body['gate']['head'] = self.head
        self.body['reviews']['rounds'][0].update(head=self.head, approve_sha=self.head[:12])
        self.write()
        brief = self.dir / 'brief'
        brief.write_text('different')
        self.assert_failure('brief', None, '--brief', brief)

    def test_note_pr_and_no_receipt(self):
        store = self.dir / 'team' / 'receipts'
        store.mkdir()
        (store / (self.head + '.json')).write_bytes(self.file.read_bytes())
        self.assertEqual(self.verify('pr:142').returncode, 0)
        self.assertEqual(self.verify(self.head[:8]).returncode, 0)
        self.assertEqual(self.verify(self.base).returncode, 1)
        self.git('notes', '--ref=wouf-receipts', 'add', '-F', str(self.file), self.base)
        copied = self.verify(self.base)
        self.assertEqual(copied.returncode, 1)
        self.assertIn('FAIL subject: commit binding', copied.stdout.splitlines())

    def test_untrusted_revoked_and_other_namespace(self):
        other = keypair(self.dir / 'other')
        self.signers.write_text('werkstatt ' + public(other) + '\n')
        self.assert_failure('signature')
        self.signers.write_text('werkstatt ' + public(self.key) + '\n')
        revoked = self.dir / 'revoked'
        revoked.write_text(Path(str(self.key) + '.pub').read_text())
        self.assert_failure('signature', None, '--revoked', revoked)
        sig = subprocess.run(['ssh-keygen', '-Y', 'sign', '-q', '-f', str(self.key), '-n', 'wouf-a2a'], input=receipt.canonical(self.body), capture_output=True, check=True).stdout.decode()
        self.file.write_bytes(receipt.canonical({'receipt': self.body, 'sig': sig}) + b'\n')
        self.assert_failure('signature')

    def test_offline_tool_path_and_imports(self):
        import ast
        source = (ROOT / '.agents/bin/teamlib/verify.py').read_text()
        imports = [n for n in ast.walk(ast.parse(source)) if isinstance(n, (ast.Import, ast.ImportFrom))]
        names = [alias.name for node in imports for alias in node.names] + [node.module or '' for node in imports if isinstance(node, ast.ImportFrom)]
        self.assertFalse(any(n.split('.')[0] in ('socket', 'urllib', 'http') for n in names))
        tools = self.dir / 'tools'
        tools.mkdir()
        for name in ('ssh-keygen', 'python3'):
            (tools / name).symlink_to(shutil.which(name))
        calls = self.dir / 'git-calls'
        wrapper = tools / 'git'
        wrapper.write_text('#!' + str(tools / 'python3') + '\n' +
                           'import os, sys\n' +
                           'with open(' + repr(str(calls)) + ', \"a\") as f: f.write(sys.argv[3] + \"\\n\")\n' +
                           'os.execv(' + repr(shutil.which('git')) + ', [' + repr(shutil.which('git')) + '] + sys.argv[1:])\n')
        wrapper.chmod(0o755)
        done = subprocess.run([str(tools / 'python3'), str(TEAM), 'verify', str(self.file), '--repo', str(self.repo), '--commit', self.head, '--signers', str(self.signers)], capture_output=True, text=True, env={**os.environ, 'PATH': str(tools), 'WOUF_TEAM_DIR': str(self.dir / 'team')})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(calls.read_text().splitlines())
        self.assertEqual(set(calls.read_text().splitlines()) - {'rev-parse', 'cat-file', 'merge-base', 'notes', 'ls-tree', 'diff-tree'}, set())

    def test_setup_errors(self):
        self.assertEqual(self.verify(self.file, '--commit', 'not-a-sha').returncode, 2)
        self.signers.unlink()
        self.assertEqual(self.verify(self.file).returncode, 2)

    def test_missing_ssh_keygen(self):
        tools = self.dir / 'no_ssh'
        tools.mkdir()
        for name in ('git', 'python3'):
            (tools / name).symlink_to(shutil.which(name))
        done = subprocess.run([str(tools / 'python3'), str(TEAM), 'verify', str(self.file), '--repo', str(self.repo), '--signers', str(self.signers)], capture_output=True, text=True, env={**os.environ, 'PATH': str(tools), 'WOUF_TEAM_DIR': str(self.dir / 'team')})
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
