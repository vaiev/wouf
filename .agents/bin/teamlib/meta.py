"""team meta: chains of steps that repeat across the run records, so the lead can make one command of a chain it keeps
spending model turns on. Read only: the events, the lead's receipts and each run's run.json and result.json (with the
wouf-usage line of its output.log); nothing is written, and every line leaves through say() or dumps()."""
from __future__ import annotations

import importlib.util
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
from importlib.machinery import SourceFileLoader
from pathlib import Path

from teamlib.core import KNOWN, PACK, die, dumps, mask_secrets, say, slug, team_dir, visible

USAGE = '  team meta [--since DAYS] [--min N] [--json]\n' \
        '                                      chains of 2 to 5 steps that repeat in the run records, by tokens'
SCHEMA = 'wouf.meta.v1'
SINCE_DAYS = 14
MIN_COUNT = 3
SHORTEST, LONGEST = 2, 5
# One run is seen up to three times (its run.finished event, the lead's receipt, its result.json): records of the
# same bot on the same task this close together are one run.
SAME_RUN = 600
TOKENS = re.compile(r'([0-9]{1,12})/([0-9]{1,12})/([0-9]{1,12})/([0-9]{1,12})')
USAGE_LINE = re.compile(r'wouf-usage tokens=(' + TOKENS.pattern + r'|unknown) skipped=[0-9]{1,9}')
WORD = re.compile(r'[a-z][a-z0-9-]{0,40}')
# A field as it is shown: printable, at most this long.
FIELD = 60
TASKS_SHOWN = 5


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return result if result.tzinfo else result.replace(tzinfo=timezone.utc)


def tokens_of(value):
    """The sum of 'in/cache_read/cache_write/out', or 0 when unknown."""
    found = TOKENS.fullmatch(value) if isinstance(value, str) else None
    return sum(int(n) for n in found.groups()) if found else 0


def field(value):
    # Masked before it is cut: a registered value cut short is no longer found by the masking of say() and dumps().
    return visible(mask_secrets(value, KNOWN))[:FIELD] if isinstance(value, str) and value else 'unknown'


def step(task, when, bot, role, outcome, tokens, run):
    return {'task': task, 't': when, 'bot': field(bot), 'role': field(role), 'outcome': field(outcome),
            'tokens': tokens, 'run': run}


# ---------------------------------------------------------------- the records

def log_module():
    """bin/log as a module, for its event reader: the same checks every reader of the store makes."""
    loader = SourceFileLoader('wouf_meta_events', str(PACK / 'bin' / 'log'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    log = importlib.util.module_from_spec(spec)
    loader.exec_module(log)
    log.REGISTERED[:] = sorted(set(log.REGISTERED + KNOWN), key=len, reverse=True)
    return log


def events_dir(log):
    """Where the events are, without setting anything: WOUF_EVENTS_DIR, else the team's folder (WOUF_TEAM_DIR)."""
    if os.environ.get('WOUF_EVENTS_DIR'):
        return Path(os.environ['WOUF_EVENTS_DIR'])
    return team_dir() / 'events' if os.environ.get('WOUF_TEAM_DIR') else log.events_dir()


def event_steps():
    """Each run.finished event: a bot's run, its outcome and its tokens."""
    log = log_module()
    folder = events_dir(log)
    steps = []
    for path in sorted(folder.glob('*.jsonl')) if folder.is_dir() else []:
        if path.is_symlink() or not path.is_file():
            continue
        try:
            events = log.event_read(path)[0]
        except OSError:
            continue
        for event in events:
            data = event.get('data')
            if event.get('kind') != 'run.finished' or not isinstance(data, dict):
                continue
            steps.append(step(event.get('task'), timestamp(event.get('t')), data.get('bot'), None,
                              data.get('outcome'), tokens_of(data.get('tokens')), True))
    return steps


def lines_of(path):
    try:
        if path.is_symlink() or not path.is_file():
            return []
        return path.read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return []


def receipt_steps():
    """The lead's receipts: a run's end (its cost names tokens; the bot is the signal's first word) or a command the
    lead ran on a task (gate, flow, merge: the signal's first word). Starts and team-wide lines are not steps."""
    steps = []
    for line in lines_of(team_dir() / 'logs' / 'lead.jsonl'):
        try:
            entry = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(entry, dict) or not all(isinstance(entry.get(k), str) for k in ('task', 'outcome', 'signal')):
            continue
        if entry['outcome'] == 'started' or entry['task'] in ('', '-'):
            continue
        first = (entry['signal'].split() or [''])[0].rstrip(':')
        word = first if WORD.fullmatch(first) else 'lead'
        cost = entry.get('cost') if isinstance(entry.get('cost'), str) else ''
        when = timestamp(entry.get('t'))
        if cost.startswith('tokens='):
            steps.append(step(entry['task'], when, word, None, entry['outcome'], tokens_of(cost[7:]), True))
        else:
            steps.append(step(entry['task'], when, 'lead', word, entry['outcome'], 0, False))
    return steps


def json_file(path):
    """A JSON object file as a dict; anything else (missing, a link, broken) is empty."""
    try:
        data = json.loads('\n'.join(lines_of(path)) or '{}')
    except (ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def usage_line(path):
    """The tokens of the last wouf-usage line of a run's output.log (read from its tail only)."""
    try:
        if path.is_symlink() or not path.is_file():
            return 0
        with path.open('rb') as handle:
            handle.seek(max(0, path.stat().st_size - 65536))
            tail = handle.read().decode('utf-8', errors='replace').splitlines()
    except OSError:
        return 0
    for line in reversed(tail):
        if line.startswith('wouf-usage'):
            found = USAGE_LINE.fullmatch(line)
            return tokens_of(found.group(1)) if found else 0
    return 0


def stage_steps():
    """The last run of each bot on each task, from its folder: run.json (the role) and result.json (the end)."""
    steps = []
    env = team_dir() / 'env'
    for spec_path in sorted(env.glob('*/tasks/*/run.json')) if env.is_dir() else []:
        folder = spec_path.parent
        member, task = folder.parent.parent.name, folder.name
        spec, result = json_file(spec_path), json_file(folder / 'result.json')
        if result.get('state') in (None, 'running'):
            continue
        tokens = tokens_of(result.get('tokens')) or usage_line(folder / 'output.log')
        steps.append(step(task, timestamp(result.get('ended')), member, spec.get('role'), result.get('state'),
                          tokens, True))
    return steps


def same_run(a, b):
    return (a['run'] and b['run'] and a['task'] == b['task'] and a['bot'] == b['bot']
            and abs((a['t'] - b['t']).total_seconds()) <= SAME_RUN)


def timelines(cutoff):
    """Task -> its steps in time order. Events first, then receipts, then run folders: a run already seen in an
    earlier record is one step, which takes the role and tokens the later records add."""
    kept = []
    for source in (event_steps(), receipt_steps(), stage_steps()):
        for new in source:
            if not isinstance(new['task'], str) or new['t'] is None or new['t'] < cutoff:
                continue
            seen = next((old for old in kept if same_run(old, new)), None)
            if seen is None:
                kept.append(new)
                continue
            if seen['role'] == 'unknown':
                seen['role'] = new['role']
            seen['tokens'] = seen['tokens'] or new['tokens']
    tasks = {}
    for item in sorted(kept, key=lambda s: s['t']):
        if item['role'] == 'unknown':
            item['role'] = item['bot']
        tasks.setdefault(item['task'], []).append(item)
    return tasks


# ---------------------------------------------------------------- the chains

def label(key):
    return f'{key[0]}/{key[1]}:{key[2]}'


def command_name(keys):
    """team-<the roles and commands of the chain>, repeats in a row once; masked before it is made a slug, so a
    registered value cannot pass in lower case."""
    words = []
    for key in keys:
        word = slug(mask_secrets(key[1], KNOWN)).strip('-')
        if word and (not words or words[-1] != word):
            words.append(word)
    return ('team-' + '-'.join(words))[:60].rstrip('-') if words else 'team-chain'


def find_chains(tasks, least):
    """Every run of 2 to 5 consecutive steps of a task, counted across tasks. A chain shown is not repeated by its
    shorter parts that occur exactly as often (they are the same occurrences)."""
    found = {}
    for task, steps in sorted(tasks.items()):
        keys = [(s['bot'], s['role'], s['outcome']) for s in steps]
        for size in range(SHORTEST, LONGEST + 1):
            for start in range(len(steps) - size + 1):
                chain = tuple(keys[start:start + size])
                entry = found.setdefault(chain, {'count': 0, 'tasks': set(), 'tokens': 0})
                entry['count'] += 1
                entry['tasks'].add(task)
                entry['tokens'] += sum(s['tokens'] for s in steps[start:start + size])
    shown = {chain: entry for chain, entry in found.items() if entry['count'] >= least}

    def inside(short, long):
        return len(short) < len(long) and any(long[i:i + len(short)] == short for i in range(len(long) - len(short) + 1))

    kept = [chain for chain, entry in shown.items()
            if not any(inside(chain, other) and shown[other]['count'] == entry['count'] for other in shown)]
    rows = [{'chain': ' > '.join(label(k) for k in chain),
             'steps': [{'bot': k[0], 'role': k[1], 'outcome': k[2]} for k in chain],
             'count': shown[chain]['count'], 'tasks': sorted(shown[chain]['tasks']),
             'tokens': shown[chain]['tokens'], 'command': command_name(chain)} for chain in kept]
    # Tokens saved: count × the chain's tokens, which is the chain's total; ties by count, then by name.
    return sorted(rows, key=lambda r: (-r['tokens'], -r['count'], r['chain']))


# ---------------------------------------------------------------- the command

def number(text, name, kind):
    try:
        value = kind(text)
        if not math.isfinite(value) or value < (0 if kind is float else 1):
            raise ValueError
    except (ValueError, OverflowError):
        die(f'meta: {name} requires a ' + ('nonnegative number of days' if kind is float else 'whole number of 1 or more'))
    return value


def cmd_meta(args):
    days, least, as_json = SINCE_DAYS, MIN_COUNT, False
    i = 0
    while i < len(args):
        if args[i] in ('--since', '--min') and i + 1 < len(args):
            if args[i] == '--since':
                days = number(args[i + 1], '--since', float)
            else:
                least = number(args[i + 1], '--min', int)
            i += 2
            continue
        if args[i] != '--json':
            die('usage: ' + USAGE.splitlines()[0].strip())
        as_json = True
        i += 1
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    except OverflowError:
        cutoff = datetime.min.replace(tzinfo=timezone.utc)
    tasks = timelines(cutoff)
    chains = find_chains(tasks, least)
    shown_days = int(days) if float(days).is_integer() else days
    if as_json:
        say(dumps({'schema': SCHEMA, 'since_days': shown_days, 'min': least, 'tasks': len(tasks),
                   'steps': sum(len(s) for s in tasks.values()), 'chains': chains}, sort_keys=True))
        return 0
    say(f'Chains of {SHORTEST} to {LONGEST} steps seen {least} times or more in the last {shown_days} days '
        f'({sum(len(s) for s in tasks.values())} steps in {len(tasks)} tasks): {len(chains)}')
    for row in chains:
        more = len(row['tasks']) - TASKS_SHOWN
        names = ', '.join(row['tasks'][:TASKS_SHOWN]) + (f' +{more}' if more > 0 else '')
        say(f"{row['tokens']} tokens, {row['count']}x: {row['chain']} (tasks {names}) -> {row['command']}")
    return 0


COMMANDS = {'meta': cmd_meta}
