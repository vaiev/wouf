import configparser
import json
import os
import socket
import shlex
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT = Path(__file__).with_name('local_runner.py')


class Handler(BaseHTTPRequestHandler):
    mode = 'normal'
    authorization = None

    def log_message(self, *args):
        pass

    def do_GET(self):
        type(self).authorization = self.headers.get('Authorization')
        self.send_response(200)
        self.end_headers()
        if self.mode == 'bad':
            self.wfile.write(b'not json')
        else:
            self.wfile.write(json.dumps({'data': [] if self.mode == 'empty' else [{'id': 'test-model'}]}).encode())

    def do_POST(self):
        self.rfile.read(int(self.headers['Content-Length']))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({'choices': [{'message': {'content': 'hello'}}]}).encode())


class LocalRunnerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = 'http://127.0.0.1:%d' % cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        Handler.mode = 'normal'
        Handler.authorization = None

    def run_tool(self, *args, env=None):
        return subprocess.run([sys.executable, str(SCRIPT), *args], text=True, capture_output=True, env=env)

    def test_check(self):
        result = self.run_tool('check', self.url)
        self.assertEqual(result.returncode, 0)
        self.assertIn('test-model', result.stdout)
        self.assertIsNone(Handler.authorization)

    def test_check_errors(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        for mode, url in [('normal', 'http://127.0.0.1:%d' % port), ('bad', self.url), ('empty', self.url)]:
            with self.subTest(mode=mode):
                Handler.mode = mode
                result = self.run_tool('check', url)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(len((result.stdout + result.stderr).strip().splitlines()), 1)
                self.assertIn('server', (result.stdout + result.stderr).lower())

    def test_key(self):
        env = dict(os.environ, TEST_RUNNER_KEY='test-secret-value')
        result = self.run_tool('check', self.url, '--key-env', 'TEST_RUNNER_KEY', env=env)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(Handler.authorization, 'Bearer test-secret-value')
        self.assertNotIn('test-secret-value', result.stdout + result.stderr)

    def test_block(self):
        result = self.run_tool('block', self.url, '--model', 'test-model')
        self.assertEqual(result.returncode, 0)
        config = configparser.ConfigParser(interpolation=None)
        config.read_string(result.stdout)
        section = config['runner.local']
        self.assertEqual(section['model_flag'], '-m')
        for command in ('command', 'host_command'):
            argv = [part.replace('{prompt}', 'say hi') for part in shlex.split(section[command])]
            argv += [section['model_flag'], section['strong']]
            self.assertEqual(argv.count('-m'), 1)
            self.assertEqual(argv[argv.index('-m') + 1], 'test-model')
        self.assertEqual(section['strong'], 'test-model')
        self.assertEqual(section['light'], 'test-model')
        self.assertEqual(section['auth'], '', 'empty: no key; the pack reads any other value as a secret name')
        self.assertIn('[egress] private = host.docker.internal:%d' % self.server.server_port, result.stdout)
        self.assertNotIn('auth = none', result.stdout)
        self.assertNotIn('test-secret-value', result.stdout)

    def test_smoke(self):
        result = self.run_tool('smoke', self.url, '--model', 'test-model')
        self.assertEqual(result.returncode, 0)
        self.assertIn('5', result.stdout)


if __name__ == '__main__':
    unittest.main()
