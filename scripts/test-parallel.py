#!/usr/bin/env python3
"""Run unittest discovery's modules in isolated processes."""
import argparse
import concurrent.futures
import fnmatch
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

# Modules requiring exclusive access belong here, with the reason beside each entry.
SERIAL_MODULES = {}


def modules_in(directory, prefix=''):
    # Match unittest discovery: recurse only into packages and ignore invalid module names.
    for path in sorted(directory.iterdir()):
        if path.is_file() and fnmatch.fnmatch(path.name, 'test*.py') and path.stem.isidentifier():
            yield prefix + path.stem
        elif path.is_dir() and (path / '__init__.py').is_file():
            yield from modules_in(path, prefix + path.name + '.')


def run_module(name):
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='test-parallel-') as tmp:
        env = os.environ.copy()
        env['TMPDIR'] = tmp
        env['PYTHONPATH'] = os.path.abspath('tests') + os.pathsep + env.get('PYTHONPATH', '')
        try:
            result = subprocess.run([sys.executable, '-m', 'unittest', name], stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, errors='replace', env=env)
            output, code = result.stdout, result.returncode
        except OSError as exc:
            output, code = str(exc), 1
    elapsed = time.monotonic() - start
    count = re.search(r'^Ran (\d+) tests? in ', output, re.MULTILINE)
    status = re.search(r'^(?:FAILED|OK) \(([^)]*)\)', output, re.MULTILINE)
    counts = dict(re.findall(r'(failures|errors|skipped)=(\d+)', status.group(1))) if status else {}
    failures, errors, skipped = (int(counts.get(key, 0)) for key in ('failures', 'errors', 'skipped'))
    if code and not (failures or errors):
        errors = 1
    return name, int(count.group(1)) if count else 0, failures, errors, skipped, elapsed, output, code


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('must be at least 1')
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-j', type=positive, default=min(os.cpu_count() or 1, 8))
    parser.add_argument('--slowest', type=int, default=0, metavar='K')
    args = parser.parse_args()
    if args.slowest < 0:
        parser.error('--slowest must be non-negative')
    names = list(modules_in(Path('tests')))
    results = []

    def record(result):
        results.append(result)
        name, count, failures, errors, skipped, seconds, _, code = result
        state = 'ERROR' if errors else 'FAIL' if failures or code else 'ok'
        print('{}: {} (tests={}, skipped={}, {:.2f}s)'.format(name, state, count, skipped, seconds), flush=True)

    start = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.j) as pool:
        futures = [pool.submit(run_module, name) for name in names if name not in SERIAL_MODULES]
        for future in concurrent.futures.as_completed(futures):
            record(future.result())
    for name in names:
        if name in SERIAL_MODULES:
            record(run_module(name))
    count = sum(r[1] for r in results)
    failures = sum(r[2] for r in results)
    errors = sum(r[3] for r in results)
    skipped = sum(r[4] for r in results)
    print('\nRan {} tests in {:.3f}s'.format(count, time.monotonic() - start))
    if failures or errors:
        print('FAILED (failures={}, errors={}, skipped={})'.format(failures, errors, skipped))
    else:
        print('OK' + (' (skipped={})'.format(skipped) if skipped else ''))
    if args.slowest:
        print('\nSlowest modules:')
        for result in sorted(results, key=lambda r: r[5], reverse=True)[:args.slowest]:
            print('{}: {:.2f}s'.format(result[0], result[5]))
    for result in results:
        if result[7]:
            print('\n--- {} ---\n{}'.format(result[0], result[6]), end='\n')
    return int(bool(failures or errors))


if __name__ == '__main__':
    sys.exit(main())
