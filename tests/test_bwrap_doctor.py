"""Bwrap scope and doctor checks with no host dependencies."""
import configparser
import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '.agents/bin'))
from teamlib import bwrap, status


class BwrapDoctorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.env = patch.dict(os.environ, {'HOME': str(self.root), 'WOUF_TEAM_DIR': str(self.root),
                                           'PATH': str(self.bin) + os.pathsep + os.environ.get('PATH', '')})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.fake('bwrap', 'if [ "$1" = --version ]; then echo "bubblewrap 0.7.0"; exit; fi\nexit 0')
        self.fake('systemd-run', 'exit 0')

    def fake(self, name, body):
        path = self.bin / name
        path.write_text('#!/bin/sh\n' + body + '\n')
        path.chmod(0o755)
        return path

    def test_scope_limits_and_fallback(self):
        argv = bwrap.limit_argv(['bwrap', '--'], '3g', '1.5')
        self.assertEqual(argv[:8], ['systemd-run', '--user', '--scope', '-p', 'MemoryMax=3221225472',
                                    '-p', 'CPUQuota=150%', '-p'])
        self.assertEqual(argv[8], 'TasksMax=1024')
        self.fake('systemd-run', 'exit 1')
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            self.assertEqual(bwrap.limit_argv(['bwrap'], '4g', '2'), ['bwrap'])
        self.assertIn('no CPU, memory or process limit on this computer', out.getvalue())

    def test_bot_and_gate_use_scopes_with_configured_limits(self):
        spec = {'stage': str(self.root), 'box': str(self.root), 'pack': str(self.root), 'network': 'none',
                'script': 'script', 'argv': [], 'memory': '6g', 'cpus': '1.25'}
        with patch.object(bwrap, 'watched', return_value='script'):
            command, env = bwrap.launch(spec)
        self.assertIn('MemoryMax=6442450944', command)
        self.assertIn('CPUQuota=125%', command)
        self.assertIn('TasksMax=1024', command)
        self.assertIn('PATH', env)
        ini = configparser.ConfigParser()
        ini.read_dict({'environment': {'memory': '6g', 'cpus': '1.25'}})
        with patch.object(bwrap, 'bwrap_usable', return_value=True):
            bwrap.gate_check(ini)
        with patch.object(bwrap.gate, 'run_masked', return_value=0) as run:
            bwrap.gate_run(self.root, self.root, 'ref', 'head', 'true', 'base', 'true', 10, None)
        self.assertIn('MemoryMax=6442450944', run.call_args.args[0])
        self.assertIn('CPUQuota=125%', run.call_args.args[0])

    def test_doctor_reports_bwrap_requirements(self):
        ini = configparser.ConfigParser()
        ini.read_dict({'environment': {'mode': 'bwrap', 'network': 'bridge', 'docker_host': 'remote'},
                       'team': {'runner': 'fake'}, 'runner.fake': {'cli': 'fake'},
                       'runner.local': {'command': 'local'}})
        out = io.StringIO()
        with patch.object(status, 'active_bots', return_value=[]), contextlib.redirect_stdout(out):
            status.cmd_doctor(ini)
        text = out.getvalue()
        for phrase in ('0.7.0', '0.8.0', '--disable-userns', 'systemd', 'bridge',
                       'bwrap runs on this computer only', 'a local model server is not reachable'):
            self.assertIn(phrase, text)

    def test_setuid_and_trial_failure_are_reported(self):
        path = self.fake('bwrap', 'if [ "$1" = --version ]; then echo "bubblewrap 0.8.0"; exit; fi\necho "AppArmor denied user namespace" >&2\nexit 1')
        path.chmod(0o4755)
        ini = configparser.ConfigParser()
        ini.read_dict({'environment': {'mode': 'bwrap', 'network': 'none'}, 'team': {'runner': 'fake'},
                       'runner.fake': {'cli': 'fake'}})
        out = io.StringIO()
        with patch.object(status, 'active_bots', return_value=[]), contextlib.redirect_stdout(out):
            status.cmd_doctor(ini)
        self.assertIn('setuid', out.getvalue())
        self.assertIn('AppArmor denied user namespace', out.getvalue())

    def test_wrapped_runner_cli_is_checked_not_the_wrapper(self):
        # The shipped agy command starts with dbus-run-session; the CLI is the program after the wrapper.
        self.fake('bwrap', 'if [ "$1" = --version ]; then echo "bubblewrap 0.8.0"; exit; fi\n'
                           'while [ "$1" != -- ]; do shift; done\nshift\nexec "$@"')
        self.fake('dbus-run-session', 'exit 0')
        path_env = patch.dict(os.environ, {'PATH': str(self.bin) + os.pathsep + '/bin:/usr/bin'})
        path_env.start()
        self.addCleanup(path_env.stop)
        self.assertEqual(os.environ['HOME'], str(self.root))
        self.assertEqual(os.environ['WOUF_TEAM_DIR'], str(self.root))
        ini = configparser.ConfigParser()
        ini.read_dict({'environment': {'mode': 'bwrap', 'network': 'none'}, 'team': {'runner': 'agy'},
                       'runner.agy': {'command': "dbus-run-session -- sh -c 'exec agy \"$@\"' agy -p {prompt}"}})
        out = io.StringIO()
        with patch.object(status, 'active_bots', return_value=[]), contextlib.redirect_stdout(out):
            status.cmd_doctor(ini)
        lines = out.getvalue().splitlines()
        self.assertIn('fix  agy CLI inside the sandbox', lines)
        self.assertNotIn('ok   agy CLI inside the sandbox', lines)
        self.fake('agy', 'exit 0')
        out = io.StringIO()
        with patch.object(status, 'active_bots', return_value=[]), contextlib.redirect_stdout(out):
            status.cmd_doctor(ini)
        self.assertIn('ok   agy CLI inside the sandbox', out.getvalue().splitlines())

    def test_memory_uses_systemd_size_syntax(self):
        # The container syntax (4g, 512m) is not a systemd size: systemd rejects lowercase suffixes.
        for memory, expected in (('4g', 4 << 30), ('512m', 512 << 20), ('1.5g', 3 << 29), ('2048', 2048), ('4G', 4 << 30)):
            argv = bwrap.limit_argv(['bwrap'], memory, '2')
            self.assertIn(f'MemoryMax={expected}', argv, memory)
        for memory in ('lots', '4 gigabytes', ''):
            with self.assertRaises(SystemExit):
                with contextlib.redirect_stderr(io.StringIO()):
                    bwrap.limit_argv(['bwrap'], memory, '2')

    def test_sh_c_runner_names_the_program_in_its_script(self):
        # With no NAME after the script, the program is the script's own first command.
        self.assertEqual(status.runner_cli("sh -c 'exec missing-cli'"), 'missing-cli')
        self.assertEqual(status.runner_cli('sh -c \'exec agy "$@"\' agy -p {prompt}'), 'agy')

    def test_sh_c_runner_cli_is_checked(self):
        self.fake('bwrap', 'if [ "$1" = --version ]; then echo "bubblewrap 0.8.0"; exit; fi\n'
                           'while [ "$1" != -- ]; do shift; done\nshift\nexec "$@"')
        ini = configparser.ConfigParser()
        ini.read_dict({'environment': {'mode': 'bwrap', 'network': 'none'}, 'team': {'runner': 'missing'},
                       'runner.missing': {'command': "sh -c 'exec missing-cli'"}})
        out = io.StringIO()
        with patch.object(status, 'active_bots', return_value=[]), contextlib.redirect_stdout(out):
            status.cmd_doctor(ini)
        self.assertIn('fix  missing CLI inside the sandbox', out.getvalue().splitlines())
        self.fake('missing-cli', 'exit 0')
        out = io.StringIO()
        with patch.object(status, 'active_bots', return_value=[]), contextlib.redirect_stdout(out):
            status.cmd_doctor(ini)
        self.assertIn('ok   missing CLI inside the sandbox', out.getvalue().splitlines())


if __name__ == '__main__':
    unittest.main()
