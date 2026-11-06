"""A disposable, offline demonstration of the real gate and merge proposal checks."""
from __future__ import annotations

import configparser
import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

from teamlib import flow, gate
from teamlib.core import put, say

# The disposable team's own config: `team merge propose` needs no key from it. No engine, runner or network; nothing
# of a real team's config is read or carried in.
TEAM_CONFIG = '# Disposable demo team: no key is needed by `team merge propose`.\n'

USAGE = "  team demo [--json]                   offline gate and exact-head approval demo; Python 3.9+ and git"


@contextlib.contextmanager
def isolated(root):
    """Keep configuration, git hooks, receipts and state out of the caller's project."""
    previous = dict(os.environ)
    os.environ.clear()
    os.environ.update(PATH=previous.get('PATH', os.defpath), HOME=str(root),
                      WOUF_TEAM_DIR=str(root / 'team'), WOUF_EVENTS_DIR=str(root / 'team' / 'events'),
                      WOUF_LOG_DIR=str(root / 'team' / 'logs'),
                      WOUF_STATE_DB=str(root / 'team' / 'env' / 'state.sqlite'), GIT_CONFIG_NOSYSTEM='1',
                      GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT='0')
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(previous)


def capture(command, handler, args):
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        try:
            code = handler(args)
        except SystemExit as error:
            code = error.code
    return {'command': command, 'code': code, 'output': output.getvalue().strip()}


def exercise(root):
    (root / 'team').mkdir()
    put(root / 'team' / 'config.ini', TEAM_CONFIG)
    repo = root / 'repo'
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(['git', *args], cwd=repo, text=True,
                                       stderr=subprocess.STDOUT, timeout=10).strip()

    def commit(path, text):
        put(repo / path, text)
        git('add', path)
        git('commit', '-qm', 'demo change')
        return git('rev-parse', 'HEAD')

    git('init', '-q', '--template=')
    git('config', 'user.name', 'Demo')
    git('config', 'user.email', 'demo@example.invalid')
    git('config', 'core.hooksPath', str(root / 'no-hooks'))
    base = commit('allowed.txt', 'base\n')
    brief = root / 'brief.md'
    put(brief, 'Task: T-1\nPaths: allowed.txt\n')
    ini = configparser.ConfigParser(interpolation=None)
    ini.read_dict({'environment': {'mode': 'host'},
                   'flow': {'gate': shlex.quote(sys.executable) + ' -c "assert open(\'allowed.txt\').read()"'}})
    calls = root / 'fake-calls'
    fake = root / 'fake-agent.py'
    put(fake, "import pathlib, sys\np = pathlib.Path(sys.argv[1])\n"
                    "with p.open('a') as f: f.write('review\\n')\n"
                    "print('Demo Review · T-1 · APPROVE ' + sys.argv[2])\n")

    def gate_result():
        args = ['T-1', '--repo', str(repo) + '@HEAD', '--base', base, '--brief', str(brief)]
        return capture('team gate T-1 --repo repo@HEAD --base ' + base + ' --brief brief.md',
                       lambda rest: gate.cmd_gate(ini, rest), args)

    def review(head, current):
        done = subprocess.run([sys.executable, str(fake), str(calls), head],
                              capture_output=True, text=True, check=True, timeout=10)
        verdict = flow.review_verdict(done.stdout.splitlines(), 'Demo Review', 'T-1', current)
        return done.stdout.strip(), verdict

    def ready(head, verdict):
        # Fixture of the ready flow consumed by the real merge proposal command.
        target = root / 'team' / 'flows' / 'T-1' / 'state.json'
        target.parent.mkdir(parents=True, exist_ok=True)
        put(target, json.dumps({'status': 'ready', 'head': head, 'branch': 'demo',
                                     'steps': [{'kind': 'gate', 'outcome': 'green', 'head': head},
                                               {'kind': 'review', **verdict, 'head': head}]}))

    def merge(head):
        args = ['propose', 'T-1', '--head', head]
        done = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / 'team'),
                               'merge', *args], capture_output=True, text=True, timeout=10)
        return {'command': 'team merge ' + ' '.join(args), 'code': done.returncode,
                'output': (done.stdout + done.stderr).strip()}

    commit('outside.txt', 'outside brief\n')
    bad_gate = gate_result()
    bad = {'name': '1. Gate stops work outside Paths before review', 'results': [bad_gate],
           'model_calls': 0, 'fake_agent_calls': len(calls.read_text().splitlines()) if calls.exists() else 0}
    bad['ok'] = bad_gate['code'] == 1 and 'outside Paths: outside.txt' in bad_gate['output']

    git('reset', '--hard', base)
    head_a = commit('allowed.txt', 'within brief\n')
    approval, verdict = review(head_a, head_a)
    ready(head_a, verdict)
    head_b = commit('allowed.txt', 'one more commit\n')
    stale_verdict = flow.review_verdict(approval.splitlines(), 'Demo Review', 'T-1', head_b)
    refused = merge(head_b)
    stale = {'name': '2. Approval on A does not cover B', 'approved_head': head_a, 'head': head_b,
             'approval': approval, 'approval_command': 'python3 fake-agent.py fake-calls ' + head_a,
             'review': stale_verdict, 'results': [refused],
             'ok': refused['code'] != 0 and head_b in refused['output'] and
                   head_a[:12] in refused['output'] and stale_verdict['verdict'] == 'stale'}

    green = gate_result()
    approval, verdict = review(head_b, head_b)
    ready(head_b, verdict)
    passed = merge(head_b)
    good = {'name': '3. Within Paths and approved exact head', 'head': head_b,
            'approval': approval, 'approval_command': 'python3 fake-agent.py fake-calls ' + head_b,
            'review': verdict, 'results': [green, passed],
            'ok': green['code'] == 0 and verdict['verdict'] == 'approve' and passed['code'] == 0}
    return [bad, stale, good]


def do_demo(args):
    if args not in ([], ['--json']):
        say('usage: team demo [--json]', err=True)
        return 2
    with tempfile.TemporaryDirectory(prefix='wouf-demo-') as folder:
        root = Path(folder)
        with isolated(root):
            steps = exercise(root)
    report = {'ok': all(step['ok'] for step in steps), 'steps': steps,
              'flags': ['A fake agent exercised the real gate and the real head check. '
                        'Nothing was merged; no network calls were made.']}
    if args:
        say(json.dumps(report))
    else:
        say('Offline demo: disposable git repository, fake agent CLI, no model or network calls.')
        for step in steps:
            say('\n' + step['name'])
            for result in step['results']:
                if result['command'].startswith('team merge'):
                    say('$ ' + step['approval_command'] + '\n' + step['approval'])
                    say('Review parser: ' + step['review']['verdict'])
                say('$ ' + result['command'])
                say(result['output'])
                say('exit ' + str(result['code']))
            if 'model_calls' in step:
                say(str(step['model_calls']) + ' model calls; ' + str(step['fake_agent_calls']) + ' fake agent calls')
            say('PASS' if step['ok'] else 'FAIL')
        say('\n' + report['flags'][0])
        say('PASS' if report['ok'] else 'FAIL')
    return 0 if report['ok'] else 1


COMMANDS = {'demo': do_demo}
