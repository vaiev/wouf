#!/usr/bin/env python3
"""Check a local model server and make a team runner block."""
import argparse
import ipaddress
import json
import os
import re
import shlex
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

ADVICE = 'Start the server, pull a model, and check the URL.'


def base_url(value):
    parts = urlsplit(value)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError('invalid URL')
    path = parts.path.rstrip('/')
    if path.endswith('/v1'):
        path = path[:-3]
    return urlunsplit((parts.scheme, parts.netloc, path, '', ''))


def request(url, key=None, data=None):
    headers = {'Content-Type': 'application/json'}
    if key:
        headers['Authorization'] = 'Bearer ' + key
    body = json.dumps(data).encode() if data is not None else None
    with urlopen(Request(url, data=body, headers=headers), timeout=10) as response:
        return json.load(response)


def check(args):
    key = os.environ.get(args.key_env) if args.key_env else None
    try:
        root = base_url(args.url)
        try:
            result = request(root + '/v1/models', key)
            models = [item.get('id') for item in result.get('data', []) if isinstance(item, dict)]
        except (HTTPError, ValueError, TypeError, AttributeError):
            models = []
        if not models or not any(isinstance(name, str) and name for name in models):
            result = request(root + '/api/tags', key)
            models = [item.get('name') for item in result.get('models', []) if isinstance(item, dict)]
        names = [name for name in models if isinstance(name, str) and name and (not key or key not in name)]
        if not names:
            raise ValueError('no models')
        for name in names:
            print(name.replace('\n', ' ').replace('\r', ' '))
        return 0
    except (ValueError, TypeError, AttributeError, OSError, URLError):
        print(ADVICE)
        return 2


def local_host(host):
    if host.lower() == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def block(args):
    try:
        root = base_url(args.url)
        if not all(re.fullmatch(r'[A-Za-z0-9._:/@-]+', name) for name in (args.model, args.light or args.model)):
            raise ValueError('invalid model')
        parts = urlsplit(root)
        host_root = root
        container_root = root
        local = local_host(parts.hostname)
        if local:
            port = ':' + str(parts.port) if parts.port else ''
            container_root = urlunsplit((parts.scheme, 'host.docker.internal' + port, parts.path, '', ''))
        def command(url, sandbox):
            env = shlex.quote('CODEX_OSS_BASE_URL=' + url + '/v1')
            return 'env ' + env + ' codex exec --oss --local-provider lmstudio ' + sandbox + ' --skip-git-repo-check {prompt}'
        print('[runner.local]')
        print('# Server URL: ' + root)
        if local:
            inside = urlsplit(container_root)
            pair = inside.hostname + ':' + str(inside.port or (443 if inside.scheme == 'https' else 80))
            print('# Containers use host.docker.internal (Docker) or host.containers.internal (Podman);')
            print('# for Podman replace host.docker.internal in command with host.containers.internal.')
            print('# network = egress: set [egress] private = ' + pair + ' (exactly this host and port;')
            print('# the proxy refuses every other private address). With mode = bwrap and network = none,')
            print('# a local server is not reachable.')
        print('install = npm install -g @openai/codex')
        print('command = ' + command(container_root, '--dangerously-bypass-approvals-and-sandbox --ephemeral'))
        print('host_command = ' + command(host_root, '--sandbox workspace-write'))
        print('model_flag = -m')
        print('strong = ' + args.model)
        print('light = ' + (args.light or args.model))
        print('auth =')  # empty: no key; the pack reads any other value as a secret name
        return 0
    except ValueError:
        print('Check the URL and model name.', file=sys.stderr)
        return 2


def smoke(args):
    start = time.monotonic()
    try:
        result = request(base_url(args.url) + '/v1/chat/completions', data={
            'model': args.model, 'messages': [{'role': 'user', 'content': 'Say hello.'}], 'max_tokens': 16})
        answer = result['choices'][0]['message']['content']
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError('empty answer')
        print('Answer length: %d characters; time: %.2f seconds.' % (len(answer), time.monotonic() - start))
        return 0
    except (ValueError, TypeError, KeyError, IndexError, OSError, URLError):
        print(ADVICE)
        return 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    check_parser = sub.add_parser('check')
    check_parser.add_argument('url')
    check_parser.add_argument('--key-env', metavar='NAME')
    block_parser = sub.add_parser('block')
    block_parser.add_argument('url')
    block_parser.add_argument('--model', required=True)
    block_parser.add_argument('--light')
    block_parser.add_argument('--cli', choices=['codex'], default='codex')
    smoke_parser = sub.add_parser('smoke')
    smoke_parser.add_argument('url')
    smoke_parser.add_argument('--model', required=True)
    args = parser.parse_args()
    return {'check': check, 'block': block, 'smoke': smoke}[args.action](args)


if __name__ == '__main__':
    sys.exit(main())
