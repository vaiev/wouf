"""A disposable, offline demonstration of the real gate and merge proposal checks, and of a change receipt made,
signed with a throwaway key and verified offline."""
from __future__ import annotations

import configparser
import contextlib
import copy
import importlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile

from teamlib import flow, gate, run
from teamlib.core import env_dir, put, put_bytes, read_json, say, team_dir

# The disposable team's own config: `team merge propose` needs no key from it. No engine, runner or network; nothing
# of a real team's config is read or carried in.
TEAM_CONFIG = '# Disposable demo team: no key is needed by `team merge propose`.\n'

# The receipt step: the disposable team's federation name, its review run, and its throwaway key in the temp folder.
DEMO_TEAM = 'demo-team'
REVIEW_RUN = 'T-1.r1'
KEY = 'keys/demo_ed25519'
# Why the step is skipped, printed after 'receipt: skipped, ' and kept as the step's reason.
NO_SSH_KEYGEN = 'ssh-keygen not found'
NO_KEY = 'ssh-keygen cannot make a key here: '

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
                    "print('Demo Review · ' + (sys.argv[3:] or ['T-1'])[0] + ' · APPROVE ' + sys.argv[2])\n")

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
    return [bad, stale, good, receipt_step(root, repo, base, head_b)]


def shown(root, command, *argvs):
    """Commands of the receipt step, run in the temp folder one after the other while they pass, as one line of the
    step (command, joined with &&)."""
    output, code = [], 0
    for argv in argvs:
        done = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        output.append((done.stdout + done.stderr).strip())
        code = done.returncode
        if code:
            break
    return {'command': command, 'code': code, 'output': '\n'.join(line for line in output if line)}


def throwaway_key(root):
    """An Ed25519 key without passphrase that ssh-keygen makes in the temp folder and its fingerprint; it is deleted
    with the folder. Only its public half is ever read here or printed."""
    (root / KEY).parent.mkdir(mode=0o700)
    return shown(root, 'ssh-keygen -q -t ed25519 -N "" -C wouf-demo -f ' + KEY + ' && ssh-keygen -l -f ' + KEY + '.pub',
                 ['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'wouf-demo', '-f', KEY],
                 ['ssh-keygen', '-l', '-f', KEY + '.pub'])


def key_or_reason(root):
    """throwaway_key's result, and why the step stops there: '' when the key and its fingerprint are made, else
    NO_KEY and the last line ssh-keygen printed (its exit code if it printed nothing)."""
    made = throwaway_key(root)
    if made['code'] == 0:
        return made, ''
    return made, NO_KEY + (made['output'].splitlines() or ['exit ' + str(made['code'])])[-1]


def land(root, base, head):
    """The approved head lands as a squash onto main of the disposable repository: the reviewed tree on base."""
    made = shown(root, 'git -C repo commit-tree ' + head[:12] + '^{tree} -p ' + base[:12] + ' -m "T-1 (squash)"',
                 ['git', '-C', 'repo', 'commit-tree', head + '^{tree}', '-p', base, '-m', 'T-1 (squash)'])
    if made['code']:
        return '', [made]
    landed = made['output']
    return landed, [made, shown(root, 'git -C repo update-ref refs/heads/main ' + landed + ' && git -C repo log '
                                '--oneline -1 main', ['git', '-C', 'repo', 'update-ref', 'refs/heads/main', landed],
                                ['git', '-C', 'repo', 'log', '--oneline', '-1', 'main'])]


def review_run(root, ini, head):
    """The fake agent reviews the approved head as run T-1.r1, and the lead keeps the records team run and team flow
    keep of a review: run.json, result.json and output.log in its stage folder, the run.finished event, and the
    flow's gate and review steps with their evidence (flow.flow_step's shape; gate.json is the real gate's)."""
    approval = shown(root, 'python3 fake-agent.py fake-calls ' + head + ' ' + REVIEW_RUN,
                     [sys.executable, 'fake-agent.py', 'fake-calls', head, REVIEW_RUN])
    spec = {'task': REVIEW_RUN, 'member': 'review', 'name': 'Demo Review', 'runner': 'fake', 'tier': 'strong',
            'model': 'fake', 'effort': None, 'mode': 'host', 'commit': head}
    result = {'state': 'done' if approval['code'] == 0 else 'failed'}
    stage = run.stage_of(spec['member'], REVIEW_RUN)
    stage.mkdir(parents=True)
    put(stage / 'run.json', json.dumps(spec))
    put(stage / 'result.json', json.dumps(result))
    put(stage / 'output.log', approval['output'] + '\n')
    run.record([run.run_event(spec, 'run.finished', result['state'], head, 'approve')])
    state = read_json(env_dir() / 'gate' / 'T-1' / 'gate.json')
    steps = [{'kind': 'gate', 'outcome': state.get('state', 'red'), 'head': state.get('head'),
              'why': state.get('why', 'the gate did not run'), **gate.evidence('gate_step', state)},
             {'kind': 'review', 'bot': spec['member'], 'task': REVIEW_RUN, 'outcome': result['state'],
              'runner': spec['runner'], 'tier': spec['tier'],
              **gate.evidence('run_record', ini, stage, spec, result, spec['runner']),
              **flow.review_verdict(approval['output'].splitlines(), spec['name'], REVIEW_RUN, head), 'head': head}]
    put(flow.flow_dir('T-1') / 'state.json', json.dumps({
        'task': 'T-1', 'repo': str(root / 'repo'), 'base': state.get('base'), 'branch': 'demo', 'class': 'standard',
        'status': 'ready', 'head': head, 'steps': steps}))
    return approval


def tamper(root, landed):
    """A copy of the stored receipt that names another reviewer, its signature kept: written in the one stored form
    (receipt.stored), so only the signature can refuse it."""
    receipt = importlib.import_module('teamlib.receipt')
    kept = receipt.parse((team_dir() / 'receipts' / (landed + '.json')).read_bytes())
    body = copy.deepcopy(kept['receipt'])
    body['reviews']['rounds'][0]['bot'] = 'someone-else'
    put_bytes(root / 'tampered.json', receipt.stored(body, kept['sig']))
    return ('tampered.json: a copy of that receipt with reviews.rounds[0].bot changed from '
            + kept['receipt']['reviews']['rounds'][0]['bot'] + ' to someone-else, its signature kept')


def receipt_step(root, repo, base, head):
    """4: the approved head lands; team receipt make builds its receipt from the team's records and signs it with a
    throwaway key; team verify checks it offline against an allowed_signers file of that key alone, then refuses a
    copy with one field changed. Skipped, before any other command, without ssh-keygen or where it cannot make the
    key."""
    step = {'name': '4. Every landed change comes with a receipt', 'skipped': False, 'reason': '', 'results': [],
            'verify': {}}
    if not shutil.which('ssh-keygen'):
        return {**step, 'ok': True, 'skipped': True, 'reason': NO_SSH_KEYGEN}
    with isolated(root):  # again, whoever calls: HOME in the temp folder, no SSH_AUTH_SOCK, for every ssh-keygen
        key, reason = key_or_reason(root)
        step['results'].append(key)
        if reason:
            return {**step, 'ok': True, 'skipped': True, 'reason': reason}
        return signed_and_verified(root, repo, base, head, step)


def signed_and_verified(root, repo, base, head, step):
    """receipt_step's commands; the step with its results, verify outputs and ok."""
    attest = importlib.import_module('teamlib.attest')
    for leaf in ('evidence', 'state'):  # as bin/team loads them: the gate's evidence, and the store of run.record
        importlib.import_module('teamlib.' + leaf)
    ini = configparser.ConfigParser(interpolation=None)
    ini.read_dict({'environment': {'mode': 'host'},
                   'federation': {'name': DEMO_TEAM, 'signing_key': str(root / KEY)}})
    results = step['results']
    landed, landing = land(root, base, head)
    results += landing
    results.append(review_run(root, ini, head))
    args = ['make', 'T-1', '--reviewed', head, '--landed', landed, '--repo', str(repo),
            '--brief', str(root / 'brief.md')]
    results.append(capture('team receipt make T-1 --reviewed ' + head + ' --landed ' + landed
                           + ' --repo repo --brief brief.md', lambda rest: attest.cmd_receipt(ini, rest), args))
    if any(result['code'] != 0 for result in results):
        return {**step, 'ok': False}
    public = (root / (KEY + '.pub')).read_text(encoding='utf-8').split()[:2]
    namespace = importlib.import_module('teamlib.receipt').NAMESPACE
    put(root / 'allowed_signers', DEMO_TEAM + ' namespaces="' + namespace + '" ' + ' '.join(public) + '\n')
    team = [sys.executable, str(Path(__file__).resolve().parents[1] / 'team'), 'verify']
    good = shown(root, 'team verify ' + landed + ' --repo repo --signers allowed_signers --brief brief.md',
                 [*team, landed, '--repo', 'repo', '--signers', 'allowed_signers', '--brief', 'brief.md'])
    step['tampered'] = tamper(root, landed)
    bad = shown(root, 'team verify tampered.json --commit ' + landed + ' --repo repo --signers allowed_signers',
                [*team, 'tampered.json', '--commit', landed, '--repo', 'repo', '--signers', 'allowed_signers'])
    bad['note'] = step['tampered']
    results += [good, bad]
    step['verify'] = {'receipt': good, 'tampered': bad}
    last = [(result['output'].splitlines() or [''])[-1] for result in (good, bad)]
    step['ok'] = good['code'] == 0 and last[0].startswith('verified ') and bad['code'] != 0 and \
        last[1] == 'refused: signature: invalid'
    return step


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
                if result.get('note'):
                    say(result['note'])
                if result['command'].startswith('team merge'):
                    say('$ ' + step['approval_command'] + '\n' + step['approval'])
                    say('Review parser: ' + step['review']['verdict'])
                say('$ ' + result['command'])
                say(result['output'])
                say('exit ' + str(result['code']))
            if 'model_calls' in step:
                say(str(step['model_calls']) + ' model calls; ' + str(step['fake_agent_calls']) + ' fake agent calls')
            say('receipt: skipped, ' + step['reason'] if step.get('skipped') else 'PASS' if step['ok'] else 'FAIL')
        say('\n' + report['flags'][0])
        say('PASS' if report['ok'] else 'FAIL')
    return 0 if report['ok'] else 1


COMMANDS = {'demo': do_demo}
