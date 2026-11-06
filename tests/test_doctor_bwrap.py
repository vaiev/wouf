"""First-bot doctor regressions; no bubblewrap or real credentials needed."""
import configparser
import contextlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '.agents/bin'))
from teamlib import bwrap, engine, status


class FirstBotDoctorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        program = self.root / 'program'
        program.write_text('#!/bin/sh\nexit 0\n')
        program.chmod(0o755)
        self.ini = configparser.ConfigParser(interpolation=None)
        self.ini.read_dict({'environment': {'mode': 'bwrap', 'network': 'none'},
                            'team': {'runner': 'fake'},
                            'runner.fake': {'command': 'fake-cli {prompt}', 'auth': ''}})
        for target, name, value in ((status, 'active_bots', []),
                                    (status.shutil, 'which', str(program)),
                                    (bwrap, 'bwrap_version', (0, 8, 0)),
                                    (bwrap, 'scope_usable', True),
                                    (bwrap, 'prefix_dir', self.root / 'prefix'),
                                    (status, 'read_json', {})):
            mock = patch.object(target, name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        run_patch = patch.object(status.subprocess, 'run', side_effect=self.trial)
        self.run = run_patch.start()
        self.addCleanup(run_patch.stop)
        self.missing = set()
        self.usable = True

    def trial(self, argv, **kwargs):
        code = 0
        if argv[0] == 'bwrap':
            code = 0 if self.usable else 1
            if 'command -v' in ' '.join(argv):
                code = int(any(name in argv for name in self.missing))
        return subprocess.CompletedProcess(argv, code, stdout='', stderr='namespace denied' if code else '')

    def doctor(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = status.cmd_doctor(self.ini)
        return code, out.getvalue()

    def test_healthy_offline_runner_passes(self):
        self.assertEqual(self.doctor()[0], 0)

    def test_missing_bwrap_and_unusable_namespaces_fail(self):
        with patch.object(status.shutil, 'which', side_effect=lambda name: None if name == 'bwrap' else '/bin/git'):
            code, text = self.doctor()
        self.assertEqual(code, 1)
        self.assertIn('fix  bwrap found', text)
        self.usable = False
        code, text = self.doctor()
        self.assertEqual(code, 1)
        self.assertIn('namespace denied', text)

    def test_only_supported_networks_pass(self):
        for network in ('bridge', 'host', 'typo'):
            with self.subTest(network=network):
                self.ini['environment']['network'] = network
                self.assertEqual(self.doctor()[0], 1)

    def test_egress_requires_a_proxy_file(self):
        self.ini['environment']['network'] = 'egress'
        self.ini['egress'] = {'proxy': str(self.root / 'missing.py')}
        for proxy in ('', str(self.root), str(self.root / 'missing.py')):
            self.ini['egress']['proxy'] = proxy
            code, text = self.doctor()
            self.assertEqual(code, 1, text)
            self.assertIn('egress proxy', text)
        proxy = self.root / 'proxy.py'
        proxy.write_text('# synthetic proxy stand-in\n')
        self.ini['egress']['proxy'] = str(proxy)
        self.assertEqual(self.doctor()[0], 0)

    def test_bwrap_checks_each_bots_login(self):
        self.ini['runner.fake'].update({'auth': 'login', 'login_check': 'test -s "$HOME/.fake/login"'})
        bots = [{'id': 'worker', 'runner': 'fake'}, {'id': 'review', 'runner': 'fake'}]
        with patch.object(status, 'active_bots', return_value=bots), \
                patch.object(status, 'login_check', side_effect=[True, False]) as login:
            code, text = self.doctor()
        self.assertEqual(code, 1, text)
        self.assertIn('ok   worker: login for fake', text)
        self.assertIn('fix  review: login for fake', text)
        self.assertEqual(login.call_count, 2)

    def test_missing_login_check_cannot_confirm_bwrap_login(self):
        self.ini['runner.fake']['auth'] = 'login'
        with patch.object(status, 'active_bots', return_value=[{'id': 'worker', 'runner': 'fake'}]), \
                patch.object(status, 'login_check', return_value=None):
            code, text = self.doctor()
        self.assertEqual(code, 1, text)
        self.assertIn('no login_check', text)

    def test_default_runner_key_checked_before_first_bot_exists(self):
        self.ini['runner.fake']['auth'] = 'SYNTHETIC_API_KEY'
        with patch.object(status, 'stored', return_value={}):
            code, text = self.doctor()
        self.assertEqual(code, 1, text)
        self.assertIn('key for fake', text)

    def test_sandbox_needs_git_and_python(self):
        for program in ('git', 'python3'):
            self.missing = {program}
            code, text = self.doctor()
            self.assertEqual(code, 1, text)
            self.assertIn(program, text)

    def test_bwrap_requires_both_wrapper_and_real_cli(self):
        self.ini['runner.fake']['command'] = 'dbus-run-session -- fake-cli {prompt}'
        for missing in ('dbus-run-session', 'fake-cli'):
            with self.subTest(missing=missing):
                self.missing = {missing}
                code, text = self.doctor()
                self.assertEqual(code, 1, text)
                self.assertIn('fix  fake CLI inside the sandbox', text)

    def test_prefix_skipped_install_is_reported(self):
        with patch.object(status, 'read_json', return_value={'skipped': ['synthetic-install']}):
            code, text = self.doctor()
        self.assertEqual(code, 1)
        self.assertIn("prefix skipped 'synthetic-install'", text)

    def test_confirmed_bwrap_login_passes_and_unusable_sandbox_does_not_probe_login(self):
        self.ini['runner.fake']['auth'] = 'login'
        with patch.object(status, 'active_bots', return_value=[{'id': 'worker', 'runner': 'fake'}]), \
                patch.object(status, 'login_check', return_value=True) as login:
            self.assertEqual(self.doctor()[0], 0)
            self.usable = False
            login.reset_mock()
            self.assertEqual(self.doctor()[0], 1)
            login.assert_not_called()

    def test_prefix_probe_clears_sandbox_environment_and_sets_its_path(self):
        self.doctor()
        probes = [call for call in self.run.call_args_list if 'command -v' in ' '.join(call.args[0])]
        self.assertTrue(probes)
        for call in probes:
            argv = call.args[0]
            self.assertIn('--clearenv', argv)
            at = argv.index('--setenv')
            self.assertEqual(argv[at:at + 3], ['--setenv', 'PATH', bwrap.INSIDE_PATH])


class WrappedCliTest(unittest.TestCase):
    def ini(self, command, host=''):
        ini = configparser.ConfigParser(interpolation=None)
        ini.read_dict({'runner.agy': {'command': command, 'host_command': host}})
        return ini

    def test_cli_of_unwraps_host_and_container_commands(self):
        commands = ('dbus-run-session -- agy -p {prompt}',
                    'env SYNTHETIC_MODE=test dbus-run-session -- agy -p {prompt}',
                    "dbus-run-session -- sh -c 'exec agy \"$@\"' agy -p {prompt}")
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(engine.cli_of(self.ini(command), 'agy'), 'agy')
                self.assertEqual(engine.cli_of(self.ini('other-cli', command), 'agy'), 'agy')

    def test_image_checks_real_cli_and_wrapper(self):
        tools = engine.image_tools(self.ini('dbus-run-session -- agy -p {prompt}', 'host-cli'), {'agy'})
        self.assertIn('agy', tools)
        self.assertIn('dbus-run-session', tools)
        self.assertNotIn('host-cli', tools)

    def test_container_doctor_rejects_missing_cli_with_wrapper_installed(self):
        ini = self.ini('dbus-run-session -- agy -p {prompt}')
        ini.read_dict({'environment': {'mode': 'container'}, 'team': {'runner': 'agy'}})

        def image_probe(ini, argv, **kwargs):
            missing = 'agy' if 'command -v' in ' '.join(argv) and 'agy' in argv else ''
            return subprocess.CompletedProcess(argv, 0, stdout=missing, stderr='')

        with patch.object(status, 'active_bots', return_value=[]), \
                patch.object(status, 'image_runners', return_value=({'agy'}, [])), \
                patch.object(status.shutil, 'which', return_value='/bin/sh'), \
                patch.object(status, 'sh', side_effect=image_probe), \
                patch.object(status.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = status.cmd_doctor(ini)
        self.assertEqual(code, 1, out.getvalue())
        self.assertIn('missing in the team image: agy', out.getvalue())

    def test_host_doctor_does_not_accept_an_installed_wrapper_as_the_cli(self):
        ini = self.ini('dbus-run-session -- agy -p {prompt}')
        ini.read_dict({'environment': {'mode': 'host'}, 'team': {'runner': 'agy'}})
        with patch.object(status, 'active_bots', return_value=[]), \
                patch.object(status.shutil, 'which', side_effect=lambda name: None if name == 'agy' else '/bin/sh'), \
                patch.object(status.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = status.cmd_doctor(ini)
        self.assertEqual(code, 1, out.getvalue())
        self.assertIn('fix  agy CLI on this computer', out.getvalue())


if __name__ == '__main__':
    unittest.main()
