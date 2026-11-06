"""Update only the files whose ownership the installation manifest records."""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

from teamlib import core, engine
from teamlib.core import PROJECT, die, say, visible

USAGE = """
  team update [--from URL|PATH] [--ref REF] [--yes] [--force]
                                      preview a pack update; --yes applies it and runs doctor;
                                      --force permits uncommitted changes to managed files
  team update --rollback [ID]          restore a backup (latest when ID is omitted)
  team update --list-backups           list dated managed-file and team-state backups
  team update --bots [--yes] [--wait]  preview a bot image refresh; --yes rebuilds and checks logins;
                                      --wait waits for running bot tasks without stopping them
"""
DEFAULT_SOURCE = 'https://github.com/vaiev/wouf'
MANIFEST = '.agents/team/.pack-files'
VERSION = '.agents/VERSION'
BACKUPS = '.agents/team/backups'
OWNERSHIP = 'ownership manifest missing: stop; follow INSTALL.md Update ownership guidance'


@contextmanager
def read_only_store():
    """Keep masking secrets without the store helper's normal mkdir/chmod housekeeping."""
    original = core.secrets_module

    def readonly():
        store = original()
        base = os.environ.get('WOUF_SECRET_DIR') or os.path.join(
            os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config'), 'wouf', 'secrets')
        store.store_dir = lambda: Path(base).expanduser().resolve()
        return store

    core.secrets_module = readonly
    try:
        yield
    finally:
        core.secrets_module = original


def register_secrets():
    if sys.argv[1:2] == ['update'] and '--bots' in sys.argv[2:]:
        # This path emits names/status only, never build output or install commands.
        # It must work without opening, unlocking or querying the secret store.
        return
    with read_only_store():
        core.register_stored()


# Bootstrap the new pack's actual `team doctor` in a fresh interpreter. It may predate this updater;
# patch only store housekeeping, leaving its own startup, registry and doctor checks in control.
DOCTOR = """import os, runpy, sys
from pathlib import Path
team = Path(sys.argv[1])
sys.path.insert(0, str(team.parent))
from teamlib import core
original = core.secrets_module
def readonly():
    store = original()
    base = os.environ.get('WOUF_SECRET_DIR') or os.path.join(
        os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config'), 'wouf', 'secrets')
    store.store_dir = lambda: Path(base).expanduser().resolve()
    return store
core.secrets_module = readonly
sys.argv = [str(team), 'doctor']
runpy.run_path(str(team), run_name='__main__')
"""


class UpdateParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default error echoes arbitrary arguments, which may contain credentials.
        die('usage: team update [--from URL|PATH] [--ref REF] [--yes] [--force] '
            'or team update --bots [--yes] [--wait]')


def managed_name(name):
    parts = PurePosixPath(name).parts
    if (len(parts) < 2 or parts[0] != '.agents' or parts[1] == 'team'
            or any(p in ('.', '..') for p in name.split('/'))
            or '/'.join(parts) != name or not name.isprintable() or '\\' in name):
        die('invalid managed path: ownership must stay inside .agents/ and outside .agents/team/')
    if protected(name):
        die('unsafe managed path: secret stores, keys and bot data cannot be managed')
    return name


def protected(name):
    parts = PurePosixPath(name).parts
    if any(p.lower() in ('secrets', 'keys', 'storage', 'storages', 'logins') for p in parts):
        return True
    leaf = parts[-1].lower()
    if leaf.startswith('.env') or leaf in ('.salt', 'auth.json', 'login.json') or leaf.endswith(('.key', '.pem')):
        return True
    store = os.environ.get('WOUF_SECRET_DIR') or os.path.join(
        os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config'), 'wouf', 'secrets')
    target = (PROJECT / name).absolute()
    secret = Path(store).expanduser().resolve()
    return target == secret or secret in target.parents


def parents_safe(root, name):
    """No managed operation may follow a link or turn a user's file into a directory."""
    for parent in (root / name).parents:
        if parent == root:
            break
        if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
            return False
    return True


def read_file(root, name):
    if not parents_safe(root, name):
        die('unsafe managed path: a parent is a symlink or is not a directory')
    path = root / name
    if path.is_symlink():
        die('unsafe managed path: symlinks cannot be replaced, even with --force')
    if not path.exists():
        return None
    mode = path.stat().st_mode
    if not stat.S_ISREG(mode):
        die('unsafe managed path: expected a regular file')
    return path.read_bytes(), stat.S_IMODE(mode)


def manifest_names(data):
    """Use the same ownership rules for installed and archived manifests."""
    return {managed_name(name) for name in data.decode('utf-8').splitlines() if name}


def manifest():
    saved = read_file(PROJECT, MANIFEST)
    if saved is None:
        die(OWNERSHIP)
    try:
        owned = manifest_names(saved[0])
    except UnicodeDecodeError:
        die('invalid ownership manifest: expected UTF-8 paths; follow INSTALL.md Update ownership guidance')
    if VERSION not in owned:
        die('pack VERSION is not managed: follow INSTALL.md Update ownership guidance')
    return saved, {name: read_file(PROJECT, name) for name in sorted(owned)}


def git(at, *args):
    # Git errors can echo a URL containing credentials. Report the failed step, never git's raw diagnostics.
    result = subprocess.run(['git', '-C', str(at), *args], stdin=subprocess.DEVNULL,
                            capture_output=True)
    if result.returncode:
        die('update: git operation failed; check the source, ref and repository access')
    return result.stdout


def source_files(repo):
    entries = git(repo, 'ls-tree', '-rz', 'HEAD', '--', '.agents').split(b'\0')
    files = {}
    for entry in filter(None, entries):
        header, raw_name = entry.split(b'\t', 1)
        name = raw_name.decode('utf-8')
        if name == '.agents/team' or name.startswith('.agents/team/'):
            continue
        mode, kind, _ = header.split()
        managed_name(name)
        if kind != b'blob' or mode not in (b'100644', b'100755'):
            die('source is not a safe WOUF pack: only regular pack files are supported')
        files[name] = read_file(repo, name)
    required = ('.agents/START.md', '.agents/rules.md', '.agents/roles/lead.md',
                '.agents/bin/team', '.agents/bin/teamlib/core.py', VERSION)
    if not all(name in files for name in required) or read_file(repo, '.agents/team/config.ini') is None:
        die('source is not a WOUF pack: required pack files are missing')
    return files


def dirty_check(owned, force):
    probe = subprocess.run(['git', '-C', str(PROJECT), 'rev-parse', '--is-inside-work-tree'],
                           capture_output=True)
    if probe.returncode:
        # A plain-folder installation has no git baseline against which to detect edits.
        if (PROJECT / '.git').exists():
            die('update: cannot check the project git repository for uncommitted changes')
        say('Managed-file git check: unavailable (project is not a git working tree).')
        return
    paths = sorted({*owned, MANIFEST})
    changed = git(PROJECT, '--no-optional-locks', '--literal-pathspecs', 'status',
                  '--porcelain=v1', '-z', '--untracked-files=all', '--', *paths)
    entries = changed.split(b'\0')
    say(f'Uncommitted managed changes: {sum(bool(entry) for entry in entries)}')
    for entry in filter(None, entries):
        say('  ' + visible(entry.decode('utf-8', errors='replace')))
    if changed and not force:
        die('uncommitted changes to managed files or the manifest: commit or restore them, or use --force')


def user_files(owned):
    kept = set()
    for folder, directories, files in os.walk(PROJECT / '.agents', followlinks=False):
        parent = Path(folder)
        directories[:] = sorted(name for name in directories if parent / name != PROJECT / '.agents/team')
        for name in [*directories, *files]:
            path = parent / name
            relative = path.relative_to(PROJECT).as_posix()
            if (path.is_symlink() or not path.is_dir()) and relative not in owned:
                kept.add(relative)
    return kept


def version(files):
    saved = files.get(VERSION)
    if saved is None:
        die('pack VERSION missing; follow INSTALL.md Update ownership guidance')
    value = saved[0].decode('ascii').strip()
    version_key(value)
    return value


def version_key(value):
    if not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', value):
        die('invalid pack VERSION: expected MAJOR.MINOR.PATCH')
    return tuple(int(p) for p in value.split('.'))


def plan_update(owned, incoming):
    added, changed, desired = [], [], {}
    kept = user_files(owned)
    for name, saved in sorted(incoming.items()):
        if name not in owned and not parents_safe(PROJECT, name):
            if any(str(parent) in owned for parent in PurePosixPath(name).parents):
                die('update stopped: pack file/directory layout changed; use the INSTALL.md manual fallback')
            kept.add(name)
            continue
        if name not in owned and os.path.lexists(PROJECT / name):
            kept.add(name)
            continue
        desired[name] = saved
        if owned.get(name) is None:
            added.append(name)
        elif owned[name] != saved:
            changed.append(name)
    retired = sorted(owned.keys() - incoming.keys())
    caches = bytecode_files([*changed, *retired])
    return dict(added=added, changed=changed, retired=retired, caches=caches,
                kept=sorted(kept - caches.keys()), desired=desired)


def show_plan(owned, plan, revision):
    say(f'Pack version now:   {version(owned)}')
    say(f'Pack version after: {version(plan["desired"])}')
    say(f'Source commit: {revision}')
    say('Upgrade path: ' + ' -> '.join(plan['path']))
    for name, descriptions in plan['steps_plan']:
        say('Step: ' + name)
        for description in descriptions:
            say('  ' + visible(description))
    say(f'Free disk space: {plan["free"]} bytes; required: {plan["required"]} bytes')
    for key, title in (('added', 'Added'), ('changed', 'Changed'), ('retired', 'Retired'),
                       ('kept', 'Kept (user-owned)'), ('caches', 'Managed bytecode caches removed')):
        say(f'{title}: {len(plan[key])}')
        for name in plan[key]:
            say('  ' + visible(name))
    say('Team state, board, decisions, secrets and bot storages are preserved.')


def bytecode_files(names):
    """Match only caches of changed/retired managed modules, without following a cache-directory link."""
    caches = {}
    for name in names:
        if not name.endswith('.py'):
            continue
        module = PurePosixPath(name)
        folder = module.parent / '__pycache__'
        path = PROJECT / folder
        if not parents_safe(PROJECT, str(folder / 'cache')) or not path.is_dir():
            continue
        for cache in path.iterdir():
            if (cache.name.startswith(module.stem + '.') and cache.name.endswith('.pyc')
                    and not cache.is_symlink() and cache.is_file()):
                relative = cache.relative_to(PROJECT).as_posix()
                caches[relative] = read_file(PROJECT, relative)
    return caches


def make_parents(path, created):
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        directory.mkdir()
        created.append(directory)


def write_file(name, saved, created):
    if not parents_safe(PROJECT, name):
        die('update stopped: an unsafe parent appeared during apply')
    path = PROJECT / name
    if saved is None and not os.path.lexists(path):
        return
    make_parents(path, created)
    # Replacing the inode also avoids changing user data linked to a managed file by a hard link.
    if os.path.lexists(path):
        path.unlink()
    if saved is not None:
        data, mode = saved
        if core.mask_bytes(data) != data:
            die('update refused: managed content contains secret material')
        core.put_bytes(path, data, mode)



def validate_plan(saved_manifest, owned, plan):
    if read_file(PROJECT, MANIFEST) != saved_manifest:
        die('update stopped: ownership manifest changed after planning; run again')
    for name, saved in owned.items():
        if read_file(PROJECT, name) != saved:
            die('update stopped: a managed file changed after planning; run again')
    for name in plan['added']:
        if name not in owned and (not parents_safe(PROJECT, name) or os.path.lexists(PROJECT / name)):
            die('update stopped: a new file appeared after planning; run again')
    for name, saved in plan['caches'].items():
        if read_file(PROJECT, name) != saved:
            die('update stopped: a managed bytecode cache changed after planning; run again')


def apply_plan(saved_manifest, owned, plan):
    validate_plan(saved_manifest, owned, plan)
    backup_id = make_backup(saved_manifest, owned, plan)
    created = []
    try:
        for name in [*plan['added'], *plan['changed']]:
            if name != VERSION:
                write_file(name, plan['desired'][name], created)
        for name in [*plan['retired'], *plan['caches']]:
            write_file(name, None, created)
        migrated = run_steps(plan['steps'], plan['desired'], apply=True)[1]
        for name, saved in migrated.items():
            if saved != plan['desired'][name]:
                write_file(name, saved, created)
        write_file(VERSION, plan['desired'][VERSION], created)
        manifest_data = ''.join(name + '\n' for name in sorted(plan['desired'])).encode('utf-8')
        write_file(MANIFEST, (manifest_data, saved_manifest[1]), created)
    except BaseException:
        try:
            restore_backup(backup_id, live_owned=set(plan['desired']))
            for directory in reversed(created):
                if directory.exists():
                    directory.rmdir()
        except BaseException:
            die('update failed and rollback was incomplete; use team update --rollback ' + backup_id, 1)
        die('update failed; automatically rolled back from backup ' + backup_id, 1)
    say('Backup: ' + backup_id)


def doctor(plan):
    say('Running team doctor:')
    try:
        result = subprocess.run([sys.executable, '-B', '-c', DOCTOR, str(PROJECT / '.agents/bin/team')],
                                cwd=PROJECT, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, errors='replace')
    except OSError:
        result = subprocess.CompletedProcess([], 1, '', 'team doctor could not be started')
    if result.stdout:
        say(result.stdout.rstrip())
    if result.stderr:
        say(result.stderr.rstrip(), err=True)
    counts = ', '.join(f'{len(plan[key])} {key}' for key in ('added', 'changed', 'retired'))
    status = 'doctor passed' if result.returncode == 0 else f'doctor failed (exit {result.returncode})'
    say(f'Update applied: {counts}; {len(plan["kept"])} user files kept; {status}.')
    return 0 if result.returncode == 0 else 1


def image_recipe():
    code = DOCTOR[:DOCTOR.index("sys.argv = [str(team), 'doctor']")] + """
import contextlib, hashlib, io
from teamlib.engine import recipe
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    text = recipe(core.config())
print(hashlib.sha256(text.encode('utf-8')).hexdigest())
"""
    try:
        result = subprocess.run([sys.executable, '-B', '-c', code, str(PROJECT / '.agents/bin/team')],
                                cwd=PROJECT, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=30)
        if result.returncode == 0 and re.fullmatch(r'[0-9a-f]{64}\n', result.stdout):
            return result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def temporary_root():
    root = Path(tempfile.gettempdir()).resolve()
    # TMPDIR may point inside the project. The clone must stay outside it, even for previews.
    if root == PROJECT or PROJECT in root.parents:
        root = PROJECT.parent
    return root


# Steps exchange only managed bytes with a fresh interpreter, never project paths or environment secrets.
# The audit guard catches accidental direct IO; release scripts still must be trusted and reviewed.
STEP_RUNNER = r'''
import base64, contextlib, io, json, os, sys
request = json.load(sys.stdin)
stdlib = os.path.realpath(os.path.dirname(os.__file__)) + os.sep

def audit(event, args):
    if event == 'open':
        path, mode, flags = args
        if not isinstance(path, (str, bytes)):
            raise PermissionError('direct IO is unavailable to upgrade steps')
        path = os.path.realpath(os.fsdecode(path))
        if (not path.startswith(stdlib) or 'site-packages' in path.split(os.sep)
                or 'dist-packages' in path.split(os.sep)
                or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)):
            raise PermissionError('use the managed pack API')
    elif event.startswith(('subprocess.', 'socket.', 'ctypes.', 'shutil.', 'winreg.')):
        raise PermissionError('external operations are unavailable to upgrade steps')
    elif event.startswith('os.') and event not in ('os.listdir', 'os.scandir'):
        raise PermissionError('filesystem operations are unavailable to upgrade steps')

sys.addaudithook(audit)
files = {name: [base64.b64decode(saved[0]), saved[1]] for name, saved in request['files'].items()}

class Pack:
    def read_text(self, name):
        return files[name][0].decode('utf-8')

    def write_text(self, name, text):
        if not request['apply']:
            raise PermissionError('plan must be read only')
        if name not in files or name == '.agents/VERSION':
            raise PermissionError('steps can only modify target managed files other than VERSION')
        files[name][0] = text.encode('utf-8')

plans = []
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    for name, script in request['steps']:
        namespace = {'pack': Pack(), '__name__': 'wouf_upgrade_step'}
        exec(compile(script, name, 'exec'), namespace)
        if not callable(namespace.get('plan')) or not callable(namespace.get('apply')):
            raise ValueError('steps require plan() and apply()')
        description = namespace['plan']()
        if not isinstance(description, list) or not all(isinstance(s, str) for s in description):
            raise ValueError('plan() must return a list of descriptions')
        plans.append([name, description])
        if request['apply']:
            namespace['apply']()
json.dump({'plans': plans, 'files': {name: [base64.b64encode(saved[0]).decode('ascii'), saved[1]]
                                  for name, saved in files.items()}}, sys.stdout)
'''


def upgrade_path(current, target, incoming, opts):
    if version_key(target) < version_key(current):
        if not (opts.ref and opts.force):
            die('downgrade refused: select the older release with --ref and --force')
        say('Explicit downgrade: no reverse steps; use a backup to restore older team state.')
        return [current, target], []
    versions = {}
    for name in incoming:
        match = re.fullmatch(r'\.agents/upgrade/([0-9]+\.[0-9]+\.[0-9]+)\.md', name)
        if match:
            value = match[1]
            if version_key(current) < version_key(value) <= version_key(target):
                versions[value] = name
    if target != current and target not in versions:
        die('upgrade path incomplete: target release entry missing')
    path, steps = [current], []
    for value in sorted(versions, key=version_key):
        text = incoming[versions[value]][0].decode('utf-8')
        previous = re.findall(r'^From: (\S+)\s*$', text, re.MULTILINE)
        if previous != [path[-1]]:
            die('upgrade path incomplete: each entry must name its immediate predecessor with From:')
        declared = re.findall(r'^Step: (\S+)\s*$', text, re.MULTILINE)
        actual = sorted(name for name in incoming if name.startswith(f'.agents/upgrade/{value}/')
                        and name.endswith('.py'))
        if len(set(declared)) != len(declared) or set(declared) != set(actual):
            die('upgrade path invalid: Step: lines must name each release script exactly once')
        for name in declared:
            steps.append((name, incoming[name][0].decode('utf-8')))
        path.append(value)
    return path, steps


def run_steps(steps, files, apply=False):
    if not steps:
        return [], files
    payload = dict(steps=steps, apply=apply,
                   files={name: [base64.b64encode(saved[0]).decode('ascii'), saved[1]]
                          for name, saved in files.items() if saved is not None})
    try:
        result = subprocess.run([sys.executable, '-I', '-B', '-c', STEP_RUNNER],
                                input=json.dumps(payload), capture_output=True, text=True,
                                cwd=temporary_root(), env={}, timeout=30)
        if result.returncode:
            die('upgrade step failed during ' + ('apply' if apply else 'dry-run'), 1)
        output = json.loads(result.stdout)
        migrated = {name: (base64.b64decode(saved[0], validate=True), saved[1])
                    for name, saved in output['files'].items()}
        if migrated.keys() != files.keys() or any(migrated[n][1] != files[n][1] for n in files):
            die('upgrade step changed ownership or modes', 1)
        if migrated[VERSION] != files[VERSION] or (not apply and migrated != files):
            die('upgrade step changed VERSION or wrote during dry-run', 1)
        return output['plans'], migrated
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        die('upgrade step failed: invalid result or exceeded 30 seconds', 1)


# Only known declarative/event state is archived. env/ contains logins, storage and caches;
# secrets.md is merely a list of names but is excluded along with all unknown private material.
STATE_FILES = {'board.md', 'decisions.md', 'config.ini', 'schedule.md', 'tools.md', 'provision.sh'}
STATE_DIRS = {'events', 'bots', 'skills', 'knowledge', 'logs'}


def state_name(name):
    parts = PurePosixPath(name).parts
    if len(parts) < 3 or parts[:2] != ('.agents', 'team') or protected(name):
        return False
    if any(p.startswith('.') or p in ('auth', 'login', 'logins') for p in parts[2:]):
        return False
    return ((len(parts) == 3 and parts[2] in STATE_FILES)
            or (parts[2] in STATE_DIRS and parts[-1].endswith(('.md', '.jsonl')))
            or name == '.agents/team/events/ORIGIN')


def state_files():
    result = {}
    team = PROJECT / '.agents/team'
    for folder, directories, files in os.walk(team, followlinks=False):
        parent = Path(folder)
        directories[:] = [name for name in directories
                          if (parent / name).relative_to(team).parts[0] in STATE_DIRS
                          and not (parent / name).is_symlink()]
        for leaf in files:
            path = parent / leaf
            name = path.relative_to(PROJECT).as_posix()
            if state_name(name) and not path.is_symlink():
                result[name] = read_file(PROJECT, name)
    return result


def backup_safe(files):
    for name, saved in files.items():
        data = name.encode('utf-8')
        if (core.mask_bytes(data) != data
                or (saved is not None and core.mask_bytes(saved[0]) != saved[0])):
            die('update refused: a managed or team-state file contains secret material; remove it first')


def backup_directory(create=False):
    if not parents_safe(PROJECT, BACKUPS + '/check'):
        die('unsafe backup directory: symlinks or non-directory parents refused')
    path = PROJECT / BACKUPS
    if protected(BACKUPS + '/archive'):
        die('backup path overlaps the secret store; choose a separate secret store before updating')
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    return path


def make_backup(saved_manifest, owned, plan):
    files = {**owned, **plan['caches'], **state_files(), MANIFEST: saved_manifest}
    backup_safe(files)
    metadata = dict(format=1, current=version(owned), target=version(plan['desired']),
                    owned=sorted(owned), added=plan['added'],
                    missing=sorted(name for name, saved in files.items() if saved is None))
    backup_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ-') + uuid.uuid4().hex[:8]
    folder = backup_directory(create=True)
    path = folder / (backup_id + '.tar.gz')
    try:
        with path.open('xb') as handle:
            os.fchmod(handle.fileno(), 0o600)
            with tarfile.open(fileobj=handle, mode='w:gz') as archive:
                for name, saved in {**files, 'wouf-backup.json':
                                    (json.dumps(metadata).encode('utf-8'), 0o600)}.items():
                    if saved is None:
                        continue
                    data, mode = saved
                    info = tarfile.TarInfo(name)
                    info.size, info.mode = len(data), mode
                    archive.addfile(info, io.BytesIO(data))
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return backup_id


def backup_ids():
    folder = backup_directory()
    return sorted(p.name[:-7] for p in folder.glob('*.tar.gz')
                  if re.fullmatch(r'[0-9]{8}T[0-9]{6}\.[0-9]{6}Z-[0-9a-f]{8}\.tar\.gz', p.name)
                  and p.is_file() and not p.is_symlink())


def restore_backup(backup_id, live_owned=None):
    if backup_id not in backup_ids():
        die('backup not found; use team update --list-backups')
    path = backup_directory() / (backup_id + '.tar.gz')
    try:
        with tarfile.open(path, 'r:gz') as archive:
            members = archive.getmembers()
            if any(not m.isfile() for m in members) or len({m.name for m in members}) != len(members):
                raise ValueError('invalid archive')
            metadata = json.load(archive.extractfile('wouf-backup.json'))
            if metadata['format'] != 1:
                raise ValueError('unknown format')
            owned = {managed_name(name) for name in metadata['owned']}
            added = {managed_name(name) for name in metadata['added']}
            files = {}
            for member in members:
                name = member.name
                if name == 'wouf-backup.json':
                    continue
                cache = ('/__pycache__/' in name and name.endswith('.pyc')
                         and managed_name(name))
                if name not in owned and name != MANIFEST and not state_name(name) and not cache:
                    raise ValueError('unsafe member')
                files[name] = archive.extractfile(member).read(), member.mode & 0o777
            for name in metadata['missing']:
                if name not in owned:
                    raise ValueError('unsafe missing path')
                files[name] = None
            if MANIFEST not in files or VERSION not in files:
                raise ValueError('missing manifest or version')
            recorded = manifest_names(files[MANIFEST][0])
            if recorded != owned or not owned <= files.keys():
                raise ValueError('inconsistent manifest')
    except (OSError, tarfile.TarError, ValueError, KeyError, TypeError, UnicodeError):
        die('backup invalid; nothing restored', 1)
    if live_owned is None:
        current_manifest = read_file(PROJECT, MANIFEST)
        live_owned = (manifest_names(current_manifest[0])
                      if current_manifest else set())
    retired = (live_owned | added) - owned
    # Read and validate everything before the first restoration write. Never use extractall.
    for name in files.keys() | retired:
        read_file(PROJECT, name)
    created = []
    for name in sorted(retired):
        write_file(name, None, created)
    for name, saved in files.items():
        write_file(name, saved, created)
    say('Restored backup: ' + backup_id)


def disk_check(owned, plan):
    backup_directory()
    snapshot = {**owned, **plan['caches'], **state_files()}
    backup_safe(snapshot)
    # An uncompressed backup estimate, replacement bytes and headroom for archive/filesystem metadata.
    required = (sum(len(v[0]) for v in snapshot.values() if v is not None)
                + sum(len(v[0]) for v in plan['desired'].values()) + 1024 * 1024)
    free = shutil.disk_usage(PROJECT).free
    if free < required:
        die('update refused: insufficient free disk space for backup and apply')
    return free, required


def recipe_parts(ini) -> dict:
    """Names and hashes only: identify changed install lines without saving or displaying their contents."""
    def digest(text):
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    runners, _ = engine.image_runners(ini)
    installs = {name: digest(core.get(ini, f"runner.{name}", "install")) for name in sorted(runners)}
    tools = {"base image": digest(engine.image(ini)), "image user": digest(engine.user(ini)),
             "environment setup": digest(core.get(ini, "environment", "setup"))}
    for name in engine.image_tools(ini, runners):
        tools["command " + name] = digest(name)
    for name in sorted({c for decl in core.active_bots() for c in core.words(decl.get("connectors", ""))}):
        try:
            tools["connector " + name] = digest(engine.connector(name).get("install", ""))
        except SystemExit:
            continue  # Match recipe(): only the bot listing a broken connector is stopped.
    provision = core.team_dir() / "provision.sh"
    lines = provision.read_text(encoding="utf-8").splitlines() if provision.is_file() else []
    for index, line in enumerate(line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")):
        tools[f"provision line {index + 1}"] = digest(line)
    return {"runners": installs, "tools": tools}


def image_state(ini) -> dict:
    """Read the installed image's recipe labels; an absent image needs an initial build."""
    result = engine.sh(ini, ["image", "inspect", "-f", "{{json .Config.Labels}}", engine.image_tag(ini)], check=False)
    if result.returncode:
        return {}
    try:
        data = json.loads(result.stdout)
        parts = json.loads(data.get("wouf.recipe-parts", "{}"))
        if not isinstance(parts, dict) or any(not isinstance(parts.get(k, {}), dict) for k in ("runners", "tools")):
            parts = {}
        return {"digest": data.get("wouf.recipe"), "parts": parts}
    except (ValueError, AttributeError, TypeError):
        die("cannot check the installed team image; refresh refused", 1)


def refresh_tasks(ini) -> bool:
    """Check engine tasks and local supervisors, including runs still staging their container."""
    result = engine.sh(ini, ["ps", "--filter", f"label=wouf.team={core.base(ini)}", "--filter", "label=wouf.task",
                             "--format", "{{.ID}}"], check=False)
    if result.returncode:
        die("cannot check running bot tasks; refresh refused", 1)
    if result.stdout.strip():
        return True
    for path in core.env_dir().glob("*/tasks/*/run.lock"):
        if core.alive(core.read_json(path).get("pid")):
            return True
    for path in core.env_dir().glob("*/tasks/*/result.json"):
        result = core.read_json(path)
        if result.get("state") == "running" and core.alive(result.get("pid")):
            return True
    return False


def refresh_image(ini, text: str, parts: dict, idle) -> bool:
    """Build under a temporary tag, then publish it only after a successful build and idle check."""
    at, tag = engine.where(ini), engine.image_tag(ini)
    candidate = tag + "-refresh-" + uuid.uuid4().hex[:12]
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    try:
        result = engine.sh(at, ["build", "-q", "-t", candidate, "--label", f"wouf.recipe={digest}",
                               "--label", "wouf.recipe-parts=" + json.dumps(parts), *engine.labels(ini), "-"],
                           check=False, input_text=text)
        if result.returncode:
            return False
        idle()  # A task may have started while the build was running; never stop it.
        if engine.sh(at, ["image", "tag", candidate, tag], check=False).returncode:
            return False
        try:
            engine.stamp_note(at, image=tag, digest=digest)
        except OSError:
            # The stamp is a cache; promotion already succeeded and must be reported as such.
            say("team: image refreshed, but its engine stamp could not be saved", err=True)
        return True
    finally:
        try:
            engine.sh(at, ["image", "rm", candidate], check=False)
        except OSError:
            say("team: temporary image tag could not be cleaned up", err=True)


def wait_bot_tasks(ini, wait):
    announced = False
    while refresh_tasks(ini):
        if not wait:
            die('bot task running; refresh refused. Use team update --bots --wait to wait for it.', 1)
        if not announced:
            say('Waiting for running bot tasks to finish; no task will be stopped.')
            sys.stdout.flush()
            announced = True
        time.sleep(1)


def show_bot_plan(current, parts, bots):
    say('Rebuild planned for the team image.')
    previous = current.get('parts', {})
    for section in ('runners', 'tools'):
        old, new = previous.get(section, {}), parts[section]
        if not previous:
            say('  Previous install/tool details unavailable; checking all current inputs.'
                if section == 'runners' else '  Tool inputs:')
        for name in sorted(old.keys() | new.keys()):
            if previous and old.get(name) == new.get(name):
                continue
            change = ('changed' if name in old and name in new else
                      'removed' if name not in new else 'added' if previous else 'needs checking')
            label = 'runner.' + name + ' install' if section == 'runners' else name
            say('  ' + visible(label) + ': ' + change)
    say('  Remaining image recipe/dependencies: changed or not recorded.'
        if previous == parts else '  Image recipe/dependencies will be rebuilt together.')
    say('Affected bots: ' + (', '.join(decl['id'] for decl in bots) or 'none'))


def check_bot_logins(ini, bots, wait):
    failed = False
    for decl in bots:
        wait_bot_tasks(ini, wait)
        member = decl['id']
        runner = decl.get('runner') or 'auto'
        if runner == 'auto':
            runner = core.get(ini, 'team', 'runner', 'auto')
        if runner not in engine.runner_names(ini):
            say(member + ': runner unknown; login not checked')
            failed = True
            continue
        # A -v mount would silently create an absent volume. Never let a check do that.
        if engine.sh(ini, ['volume', 'inspect', engine.volume(ini, member)], check=False).returncode:
            say(member + ': storage missing; login not checked and storage not created')
            failed = True
            continue
        status = engine.login_check(ini, member, runner, read_only=True)
        say(member + (': login OK' if status is True else ': login needs attention; no logout performed'
                      if status is False else ': login_check not configured'))
        failed = failed or status is False
    return 1 if failed else 0


def refresh_bots(opts):
    ini = core.config()
    if engine.mode(ini) == 'host':
        say('Host mode: no team image to refresh; team doctor reports missing host tools.')
        return 0
    if engine.mode(ini) != 'container':
        die('bot image refresh requires container mode; use team image for the bwrap prefix')
    idle = lambda: wait_bot_tasks(ini, opts.wait)
    idle()
    text, parts = engine.recipe(ini), recipe_parts(ini)
    current = image_state(ini)
    digest = hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]
    say('Bot storages, logins and keys stay; the secret store is not read.')
    if current.get('digest') == digest:
        say('Team image recipe unchanged; nothing to do.')
        return 0
    bots = core.active_bots()
    show_bot_plan(current, parts, bots)
    if not opts.yes:
        say('Plan only; nothing written. Run team update --bots --yes to rebuild and check each active login.')
        return 0
    idle()
    say('Building the new team image; the previous image stays in use until the build succeeds.')
    try:
        rebuilt = refresh_image(ini, text, parts, idle)
    except (OSError, SystemExit, KeyboardInterrupt):
        say('Refresh did not complete; the previous image stays in use.', err=True)
        raise
    if not rebuilt:
        say('Rebuild failed; the previous image stays in use. Storages, logins and keys stay.', err=True)
        return 1
    say('Team image refreshed. Checking active bot logins (read-only; no logout).')
    return check_bot_logins(ini, bots, opts.wait)


def bot_update_option(opts):
    if opts.bots:
        if opts.source != DEFAULT_SOURCE or opts.ref or opts.force or opts.rollback is not None or opts.list_backups:
            die('--bots cannot be combined with pack source, force or backup options')
        return refresh_bots(opts)
    if opts.wait:
        die('--wait requires --bots')
    return None


def do_update(args):
    parser = UpdateParser(prog='team update', description='Preview or apply a managed pack update.')
    parser.add_argument('--from', dest='source', default=DEFAULT_SOURCE, metavar='URL|PATH')
    parser.add_argument('--ref', metavar='REF')
    parser.add_argument('--yes', action='store_true')
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--bots', action='store_true')
    parser.add_argument('--wait', action='store_true')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--rollback', nargs='?', const='', metavar='ID')
    group.add_argument('--list-backups', action='store_true')
    opts = parser.parse_args(args)
    bots_result = bot_update_option(opts)
    if bots_result is not None:
        return bots_result
    if opts.rollback is not None or opts.list_backups:
        if opts.source != DEFAULT_SOURCE or opts.ref or opts.yes or opts.force:
            die('backup commands cannot be combined with update options')
        ids = backup_ids()
        if opts.list_backups:
            say('Backups (oldest first):')
            for backup_id in ids:
                say('  ' + backup_id)
            if not ids:
                say('  none')
            return 0
        if not ids:
            die('no update backups available')
        restore_backup(opts.rollback or ids[-1])
        return 0
    saved_manifest, owned = manifest()
    dirty_check(owned, opts.force)
    try:
        with tempfile.TemporaryDirectory(prefix='wouf-update-', dir=temporary_root()) as tmp:
            repo = Path(tmp) / 'repo'
            git(PROJECT, 'clone', '--quiet', '--no-hardlinks', '--', opts.source, str(repo))
            if opts.ref:
                revision = git(repo, 'rev-parse', '--verify', '--end-of-options', opts.ref + '^{commit}').strip()
                git(repo, 'checkout', '--quiet', '--detach', revision.decode('ascii'), '--')
            revision = git(repo, 'rev-parse', 'HEAD').decode('ascii').strip()
            incoming = source_files(repo)
            backup_safe(incoming)
            current, target = version(owned), version(incoming)
            path, steps = upgrade_path(current, target, incoming, opts)
            plan = plan_update(owned, incoming)
            if VERSION not in plan['desired']:
                die('target VERSION collides with a user file; ownership must be resolved first')
            if any(name not in plan['desired'] for name, _ in steps):
                die('upgrade script collides with a user-owned file; resolve ownership first')
            plan['path'], plan['steps'] = path, steps
            plan['steps_plan'] = run_steps(steps, plan['desired'])[0]
            plan['free'], plan['required'] = disk_check(owned, plan)
            plan['image_changed'] = any(name.startswith(('.agents/connectors/', '.agents/bots/'))
                                        or name == '.agents/bin/teamlib/engine.py'
                                        for name in [*plan['added'], *plan['changed'], *plan['retired']])
            show_plan(owned, plan, revision)
            if not opts.yes:
                say('Plan only; nothing written. Run with --yes to apply.')
                return 0
            recipe_before = image_recipe()
            apply_plan(saved_manifest, owned, plan)
            recipe_after = image_recipe()
            result = doctor(plan)
            if ((recipe_before is not None and recipe_after is not None and recipe_before != recipe_after)
                    or ((recipe_before is None or recipe_after is None) and plan['image_changed'])):
                say('Team image recipe changed or needs checking; run team update --bots to preview a safe '
                    'bot refresh, or run team image.')
            return result
    except (OSError, UnicodeError):
        die('update failed: could not read, copy or check the pack; temporary clone cleaned up', 1)


def version_command(command, commands):
    value = (Path(__file__).resolve().parents[2] / 'VERSION').read_text(encoding='utf-8').strip()
    say(value if command == '--version' else 'Pack version: ' + value)
    return 0 if command == '--version' else commands['doctor']([])


COMMANDS = {'update': do_update}
