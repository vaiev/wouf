"""Verify change receipts offline against trusted keys and local Git objects."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from teamlib.core import put, say, team_dir, visible

def leaf(name):
    return importlib.import_module(f"teamlib.{name}")

receipt = leaf("receipt")

USAGE = """  team verify TARGET [--repo PATH] [--commit SHA] [--signers FILE] [--revoked FILE] [--brief FILE]
                                      verify a receipt offline; compare the printed key fingerprint out of band"""
LIMITS = "A receipt records what the team signed; it does not prove correctness, honest review, repository membership, or an uncompromised signer. Compare the key fingerprint out of band. Token, gate and egress records depend on the team and sandbox; private hosts may relay. Signer clocks and CLI vendor mappings are not independently verified."

def git(repo, *args):
    env = {**os.environ, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
           'GIT_TERMINAL_PROMPT': '0', 'GIT_NO_REPLACE_OBJECTS': '1', 'GIT_CONFIG_COUNT': '0'}
    for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES'):
        env.pop(key, None)
    try:
        return subprocess.run(['git', '-C', str(repo), *args], env=env, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None

def resolve(repo, name):
    if not re.fullmatch(r'[0-9a-fA-F]{7,40}', name):
        return None
    result = git(repo, 'rev-parse', '--verify', name + '^{commit}')
    value = result.stdout.decode('ascii', 'replace').strip() if result and result.returncode == 0 else ''
    return value if receipt.SHA.fullmatch(value) else None

def signature(body, sig, signers, revoked):
    try:
        lines = receipt.trusted_lines(Path(signers).read_text(encoding='utf-8'), body['team'])
        if not lines:
            return None
        with tempfile.TemporaryDirectory() as tmp:
            allowed, signature_file = Path(tmp) / 'signers', Path(tmp) / 'signature'
            put(allowed, '\n'.join(lines) + '\n')
            put(signature_file, sig)
            cmd = [shutil.which('ssh-keygen'), '-Y', 'verify', '-f', str(allowed), '-I', body['team'],
                   '-n', receipt.NAMESPACE, '-s', str(signature_file), '-O',
                   'verify-time=' + receipt.verify_time(receipt.signature_time(body))]
            if revoked and (Path(revoked).exists() or Path(revoked).is_symlink()):
                cmd += ['-r', str(revoked)]
            done = subprocess.run(cmd, input=receipt.canonical(body), capture_output=True, timeout=60)
            match = re.search(rb'SHA256:[A-Za-z0-9+/]{43}', done.stdout)
            return match.group().decode('ascii') if done.returncode == 0 and match else None
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        return None

def read_receipt(path):
    with open(path, 'rb') as stream:
        return stream.read(receipt.MAX_FILE + 1)

def setup(state):
    parser = argparse.ArgumentParser(prog='team verify', description=LIMITS)
    parser.add_argument('target')
    for name in ('repo', 'commit', 'signers', 'revoked', 'brief'):
        parser.add_argument('--' + name)
    try:
        opt = parser.parse_args(state['args'])
    except SystemExit as exc:
        return int(exc.code)
    repo = Path(opt.repo or os.getcwd())
    signers = Path(opt.signers) if opt.signers else team_dir() / 'receipts' / 'allowed_signers'
    if not opt.signers and not signers.is_file():
        signers = repo / 'receipts' / 'allowed_signers'
    if not shutil.which('ssh-keygen') or not shutil.which('git') or not signers.is_file():
        say('refused: setup: git, ssh-keygen and a signers file are required')
        return 2
    requested = (resolve(repo, opt.commit) or (opt.commit if receipt.SHA.fullmatch(opt.commit) else None)) if opt.commit else None
    if opt.commit and not requested:
        say('refused: setup: invalid commit')
        return 2
    state['opt'] = opt
    state['repo'] = repo
    state['signers'] = signers
    state['requested'] = requested

def find_subject(state):
    opt = state['opt']
    repo = state['repo']
    requested = state['requested']
    subject = resolve(repo, opt.target)
    data = None
    if subject:
        note = git(repo, 'notes', '--ref=wouf-receipts', 'show', subject)
        if note and note.returncode == 0:
            data = note.stdout
        else:
            path = team_dir() / 'receipts' / (subject + '.json')
            if path.is_file():
                try:
                    data = read_receipt(path)
                except OSError:
                    pass
        if data is None:
            say('refused: subject: no receipt')
            return 1
    elif re.fullmatch(r'pr:[1-9][0-9]*', opt.target):
        for path in sorted((team_dir() / 'receipts').glob('*.json')):
            try:
                candidate = read_receipt(path)
                parsed = receipt.parse(candidate)
                if parsed['receipt']['change']['pr'] == int(opt.target[3:]):
                    data = candidate
                    subject = parsed['receipt']['change']['landed']
                    break
            except (OSError, receipt.Refused):
                continue
        if data is None:
            say('refused: subject: no receipt')
            return 1
    else:
        try:
            data = read_receipt(opt.target)
        except OSError:
            say('refused: setup: unreadable target')
            return 2
        subject = requested
    state['subject'] = subject
    state['data'] = data

def form(state):
    data = state['data']
    try:
        parsed = receipt.parse(data)
    except receipt.Refused as exc:
        why = str(exc)
        name = 'secrets' if 'looks like it contains a secret' in why else 'form'
        say('FAIL ' + name + ': invalid receipt')
        say('refused: ' + name + ': invalid receipt')
        return 1
    body, sig = parsed['receipt'], parsed['sig']
    say('ok form: canonical receipt')
    say('ok secrets: none found')
    state['body'] = body
    state['sig'] = sig

def verify_signature(state):
    body = state['body']
    sig = state['sig']
    signers = state['signers']
    opt = state['opt']
    fingerprint = signature(body, sig, signers, opt.revoked)
    if not fingerprint:
        say('FAIL signature: untrusted or invalid signature\nrefused: signature: invalid')
        return 1
    say('ok signature: ' + fingerprint)
    state['fingerprint'] = fingerprint

def check(state, name, result, detail):
    if result is None:
        state['skipped'].append(name)
        say('skip ' + name + ': ' + detail)
    elif result:
        say('ok ' + name + ': ' + detail)
    else:
        say('FAIL ' + name + ': ' + detail)
        say('refused: ' + name + ': ' + detail)
    return result is not False

def obj(repo, sha):
    kind = git(repo, 'cat-file', '-t', sha)
    if not kind or kind.returncode != 0:
        return None
    if kind.stdout.strip() != b'commit':
        return ''
    result = git(repo, 'cat-file', '-p', sha)
    return result.stdout.decode('utf-8', 'replace') if result and result.returncode == 0 else None

def headers(text, name):
    return [line[len(name)+1:] for line in text.split('\n\n', 1)[0].splitlines() if line.startswith(name + ' ')]

def subject_binding(state):
    subject = state['subject']
    body = state['body']
    change = body['change']
    if not check(state, 'subject', subject == change['landed'] if subject else None, 'commit binding'):
        return 1
    state['landed'] = obj(state['repo'], change['landed'])
    state['reviewed'] = obj(state['repo'], change['reviewed'])

def landed_check(state):
    repo = state['repo']
    body = state['body']
    change = body['change']
    landed = state['landed']
    if landed is None:
        good = None
    else:
        parents = headers(landed, 'parent')
        good = headers(landed, 'tree') == [change['tree']] and len(parents) == 1 and (change['merge'] == 'fast-forward' and change['landed'] == change['reviewed'] or change['merge'] == 'squash' and parents[0] == change['landed_parent'] == change['base'])
    if not check(state, 'landed', good, 'tree and parent'):
        return 1

def reviewed_check(state):
    repo = state['repo']
    body = state['body']
    change = body['change']
    reviewed = state['reviewed']
    if reviewed is None:
        good = None
    else:
        ancestor = git(repo, 'merge-base', '--is-ancestor', change['base'], change['reviewed'])
        diff = git(repo, 'diff-tree', '-r', '--no-renames', '--ignore-submodules=none', '--name-only', '-z', change['base'], change['reviewed'])
        good = headers(reviewed, 'tree') == [change['tree']] and ancestor is not None and ancestor.returncode == 0 and diff is not None and diff.returncode == 0 and len([x for x in diff.stdout.split(b'\0') if x]) == change['files']
    if not check(state, 'reviewed', good, 'tree, base and files'):
        return 1

def gate_check(state):
    body = state['body']
    change = body['change']
    gate, reviews = body['gate'], body['reviews']
    if not check(state, 'gate', gate['state'] == 'green' and gate['head'] == change['reviewed'] and gate['base'] == change['base'], 'gate consistency'):
        return 1

def reviews_check(state):
    body = state['body']
    change = body['change']
    reviews = body['reviews']
    count = sum(x['counts'] for x in reviews['rounds'])
    if not check(state, 'reviews', count >= reviews['required'] and count == reviews['approved'] and all(not x['counts'] or receipt.counts(x, change['reviewed']) for x in reviews['rounds']), 'approval consistency'):
        return 1
    state['count'] = count

def flags_check(state):
    body = state['body']
    gate = body['gate']
    sovereign = not body['flags'] or (gate['network'] == 'none' and all(r['egress']['mode'] == 'proxy' and isinstance(r['egress']['log'], str) and receipt.SHA256.fullmatch(r['egress']['log']) and not r['egress']['hosts'] for r in body['runs']))
    if not check(state, 'flags', bool(sovereign), 'flag consistency'):
        return 1

def brief_check(state):
    body = state['body']
    opt = state['opt']
    if opt.brief:
        try:
            match = hashlib.sha256(Path(opt.brief).read_bytes()).hexdigest() == body['brief']['sha256']
        except OSError:
            say('refused: setup: unreadable brief')
            return 2
        if not check(state, 'brief', match, 'brief hash'):
            return 1
    else:
        say('skip brief: no brief supplied')

def finish(state):
    body = state['body']
    fingerprint = state['fingerprint']
    change, gate, reviews = body['change'], body['gate'], body['reviews']
    count = state['count']
    skipped = state['skipped']
    status = 'partial' if skipped else 'verified'
    if skipped:
        detail = '; record ok; not checked here: ' + ', '.join(skipped)
    else:
        vendors = ', '.join(sorted({visible(r['vendor']) for r in reviews['rounds'] if r['counts']}))
        detail = (f"; tree of L = tree of H {visible(change['reviewed'][:12])}; "
                  f"gate green (network {visible(gate['network'])}); {count} approval(s) on H ({vendors})")
    say(f"{status} {visible(change['landed'][:12])} {visible(body['task'])}: signed by {visible(body['team'])} key {fingerprint}" + detail)
    return 3 if skipped else 0

def do_verify(args):
    state = {'args': args, 'skipped': []}
    for step in (setup, find_subject, form, verify_signature, subject_binding,
                 landed_check, reviewed_check, gate_check, reviews_check,
                 flags_check, brief_check, finish):
        result = step(state)
        if result is not None:
            return result

COMMANDS = {'verify': do_verify}
