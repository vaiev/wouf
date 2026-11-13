"""Claude Code usage measurements and the bots' runs. Transcript contents never leave the parser."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import re
import shlex
from datetime import datetime, timedelta, timezone
from importlib.machinery import SourceFileLoader
from pathlib import Path
from statistics import median

from teamlib.core import PACK, KNOWN, config, die, dumps, get, log_dir, say, team_dir

USAGE = '  team stats --transcripts DIR [--since DAYS] [--json] [--record]'
TOKENS = ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens', 'output_tokens')
UNKNOWN = 'unknown'
# A run's numbers in its receipt's cost (teamlib.run): digits only, each below 10^12.
RUN_COST = re.compile(r'tokens=(?:([0-9]{1,12})/([0-9]{1,12})/([0-9]{1,12})/([0-9]{1,12})|unknown)')


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def lead_file(path):
    if not isinstance(path, str):
        return False
    parts = Path(path).parts
    return any(parts[i:i+4] == ('.agents', 'team', 'env', 'lead')
               and i+4 < len(parts) and '..' not in parts[i+4:] for i in range(len(parts)))


def useful(block):
    """Examine tool arguments only in memory; return a boolean, never their text."""
    args = block.get('input')
    if not isinstance(args, dict):
        return False
    name = block.get('name')
    if name in ('Write', 'Edit', 'MultiEdit'):
        return lead_file(args.get('file_path'))
    if name != 'Bash' or not isinstance(args.get('command'), str):
        return False
    try:
        lexer = shlex.shlex(args['command'], posix=True, punctuation_chars=';&|()<>\n')
        lexer.whitespace = lexer.whitespace.replace('\n', '')
        lexer.whitespace_split = True
        words = list(lexer)
    except ValueError:
        return False
    if any(word in ('>', '>>') and i+1 < len(words) and lead_file(words[i+1])
           for i, word in enumerate(words)):
        return True
    # Only command positions, so echo/quoted mentions do not count as executions.
    command_position = True
    for i, word in enumerate(words):
        if word in (';', '&&', '||', '|', '(') or word.isspace():
            command_position = True
            continue
        if command_position:
            if word in ('python', 'python3', 'env'):
                continue
            if '=' in word and re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', word):
                continue
            if (word == '.agents/bin/team' or word.endswith('/.agents/bin/team')) and i+1 < len(words):
                command = words[i+1]
                if re.fullmatch(r'[a-z][a-z0-9-]*', command) and command not in ('context', 'status', 'help'):
                    return True
            command_position = False
    return False


def usage_numbers(message):
    usage = message.get('usage')
    if not isinstance(usage, dict):
        return dict.fromkeys(TOKENS, UNKNOWN)
    return {key: value if type(value := usage.get(key)) in (int, float)
            and math.isfinite(value) and value >= 0 else UNKNOWN for key in TOKENS}


def summed(turns):
    return {key: sum(t[key] for t in turns) if turns and all(t[key] != UNKNOWN for t in turns)
            else UNKNOWN for key in TOKENS}


def read_session(path):
    """Return only numbers, timestamps and a filename digest. No content is retained."""
    start = None
    action = None
    turns = {}
    action_turns = None
    eligible_turns = set()
    first_key = None
    skipped = 0
    recognized = False
    try:
        with path.open('r', encoding='utf-8', errors='replace') as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except (ValueError, RecursionError):
                    skipped += 1
                    continue
                if not isinstance(row, dict):
                    skipped += 1
                    continue
                kind, message = row.get('type'), row.get('message')
                if kind not in ('user', 'assistant') or not isinstance(message, dict):
                    continue
                recognized = True
                time = timestamp(row.get('timestamp'))
                content = message.get('content', [])
                # Tool results are user records but do not mark the human start.
                tool_result = isinstance(content, list) and any(isinstance(b, dict) and b.get('type') == 'tool_result' for b in content)
                if kind == 'user' and start is None and not tool_result:
                    start = time
                if kind != 'assistant':
                    continue
                numbers = usage_numbers(message)
                # Claude may emit several content records for one assistant message.
                ident = message.get('id')
                key = ident if isinstance(ident, str) else ('row', len(turns))
                turns[key] = numbers
                if first_key is None:
                    first_key = key
                if start is not None:
                    eligible_turns.add(key)
                if action is None and start is not None and time is not None and time >= start:
                    if isinstance(content, list) and any(isinstance(b, dict) and b.get('type') == 'tool_use' and useful(b) for b in content):
                        action = (time-start).total_seconds()
                        action_turns = set(eligible_turns)
    except OSError:
        die('stats: cannot read a supplied transcript')
    if not recognized:
        return None, skipped
    m2 = summed([turns[k] for k in action_turns]) if action_turns else dict.fromkeys(TOKENS, UNKNOWN)
    m2['seconds'] = action if action is not None else UNKNOWN
    return {'start': start, 'hash': hashlib.sha256(path.name.encode('utf-8', 'surrogateescape')).hexdigest(),
            'm1': turns[first_key] if first_key is not None else dict.fromkeys(TOKENS, UNKNOWN), 'm2': m2, 'total': summed(list(turns.values()))}, skipped


def aggregate(sessions, field, keys):
    def med(rows):
        return {key: median(values) if (values := [r[field][key] for r in rows if r[field][key] != UNKNOWN])
                else UNKNOWN for key in keys}
    return {'median': med(sessions), 'last_10_median': med(sessions[-10:]),
            'totals': {key: sum(r[field][key] for r in sessions) if sessions and all(r[field][key] != UNKNOWN for r in sessions)
                       else UNKNOWN for key in keys}}


def log_module():
    loader = SourceFileLoader('wouf_stats_events', str(PACK / 'bin' / 'log'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    log = importlib.util.module_from_spec(spec)
    loader.exec_module(log)
    log.REGISTERED[:] = sorted(set(log.REGISTERED + KNOWN), key=len, reverse=True)
    return log


def task_classes():
    """Task id -> class, from the store's task.created events; unreadable records are left out."""
    log = log_module()
    directory = log.events_dir()
    classes = {}
    if directory.is_dir():
        for path in sorted(directory.glob('*.jsonl')):
            for event in log.event_read(path)[0]:
                klass = event.get('data', {}).get('class')
                if event.get('kind') == 'task.created' and isinstance(klass, str) and re.fullmatch(r'[a-z][a-z0-9-]{0,30}', klass):
                    classes[event.get('task')] = klass
    return classes


def bot_runs(cutoff):
    """The runs in the lead's receipts since cutoff: those whose cost names tokens (teamlib.run writes it at the end
    of every collected run). Returns (bot, task, total tokens or UNKNOWN); a value that is not digits is unknown."""
    path = log_dir() / 'lead.jsonl'
    runs = []
    try:
        lines = path.read_text(encoding='utf-8', errors='replace').splitlines() if path.is_file() else []
    except OSError:
        lines = []
    for line in lines:
        try:
            entry = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(entry, dict) or not all(isinstance(entry.get(k), str) for k in ('cost', 'signal', 'task')):
            continue
        if not entry['cost'].startswith('tokens='):
            continue
        when = timestamp(entry.get('t'))
        if cutoff is not None and (when is None or when < cutoff):
            continue
        bot = (entry['signal'].split() or [''])[0]
        bot = bot if re.fullmatch(r'[a-z][a-z0-9-]{0,40}', bot) else UNKNOWN
        found = RUN_COST.fullmatch(entry['cost'])
        tokens = sum(int(n) for n in found.groups()) if found and found.group(1) else UNKNOWN
        runs.append((bot, entry['task'], tokens))
    return runs


def run_numbers(sessions, cutoff):
    """M3, the lead's share of all tokens, tokens per run per class and bot, and the coverage of known numbers."""
    runs, classes = bot_runs(cutoff), task_classes()
    known = [r for r in runs if r[2] != UNKNOWN]
    lead = sum(sum(s['total'].values()) for s in sessions) if sessions and all(
        UNKNOWN not in s['total'].values() for s in sessions) else UNKNOWN
    bots = sum(r[2] for r in known)
    groups = {}
    for bot, task, tokens in runs:
        groups.setdefault(f"{classes.get(task, UNKNOWN)}/{bot}", []).append(tokens)
    per = {}
    for key, values in sorted(groups.items()):
        numbers = [v for v in values if v != UNKNOWN]
        per[key] = {'runs': len(values), 'known': len(numbers),
                    'tokens_per_run': round(sum(numbers) / len(numbers)) if numbers else UNKNOWN}
    share = round(lead / (lead + bots), 4) if lead != UNKNOWN and runs and lead + bots else UNKNOWN
    return {'lead_tokens': lead, 'bot_tokens': bots, 'lead_share': share, 'runs': len(runs), 'known': len(known),
            'coverage': round(len(known) / len(runs), 4) if runs else UNKNOWN, 'per_class_bot': per}


def budget_today():
    """Today's tokens (UTC day, the lead's receipts) against the caps of [budget] in config.ini, when one is set;
    teamlib.engine keeps them before every run. Caps that are not digits are left out (team run refuses them)."""
    if not (team_dir() / 'config.ini').is_file() or not config().has_section('budget'):
        return {}
    ini = config()
    caps = {key: int(get(ini, 'budget', key)) for key in ini.options('budget')
            if re.fullmatch(r'per_(?:run|day(?:\.[a-z][a-z0-9-]{0,40})?)', key)
            and re.fullmatch(r'[0-9]{1,15}', get(ini, 'budget', key))}
    if not caps:
        return {}
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    runs = [r for r in bot_runs(start) if r[2] != UNKNOWN]
    days = {key: {'used': sum(r[2] for r in runs if key == 'per_day' or r[0] == key.split('.', 1)[1]), 'cap': cap}
            for key, cap in sorted(caps.items()) if key != 'per_run'}
    return {'day': start.date().isoformat(), **days, **({'per_run': caps['per_run']} if 'per_run' in caps else {})}


def record_sessions(sessions):
    """Use the event writer's validation and durability; serialize stats deduplication."""
    log = log_module()
    directory = log.events_dir()
    if not directory.is_dir():
        return
    import fcntl
    with (directory / '.stats.lock').open('a+b') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        seen = set()
        for path in directory.glob('*.jsonl'):
            events, bad = log.event_read(path)
            if bad:
                die('stats: event log contains invalid records')
            for event in events:
                data = event.get('data', {}).get('first_turn_tokens', {})
                if event.get('kind') == 'usage.session' and isinstance(data, dict):
                    seen.add(data.get('session_hash'))
        for session in sessions:
            if session['hash'] in seen:
                continue
            # Null means unavailable in persisted numerical measurements.
            clean = lambda d: {k: None if v == UNKNOWN else v for k, v in d.items()}
            log.event_append('usage.session', 'lead', '', {
                'first_turn_tokens': {**clean(session['m1']), 'session_hash': session['hash']},
                'time_to_first_action': None if session['m2']['seconds'] == UNKNOWN else session['m2']['seconds'],
                'tokens_to_first_action': clean({k: session['m2'][k] for k in TOKENS}),
                'total_tokens': clean(session['total'])})
            seen.add(session['hash'])


def cmd_stats(args):
    directory = None
    days = None
    as_json = record = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ('--transcripts', '--since') and i+1 < len(args):
            i += 1
            if arg == '--transcripts':
                directory = Path(args[i])
            else:
                try:
                    days = float(args[i])
                    if not math.isfinite(days) or days < 0:
                        raise ValueError
                except ValueError:
                    die('stats: --since requires a nonnegative number of days')
        elif arg == '--json':
            as_json = True
        elif arg == '--record':
            record = True
        else:
            die(USAGE.strip())
        i += 1
    if directory is None:
        die(USAGE.strip())
    if not directory.is_dir():
        die('stats: supplied transcript directory is unavailable')
    sessions, skipped = [], 0
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days) if days is not None else None
    except OverflowError:
        die('stats: --since is outside the supported date range')
    try:
        paths = sorted(directory.glob('*.jsonl'))
    except OSError:
        die('stats: cannot list supplied transcript directory')
    for path in paths:
        # Never follow a transcript symlink outside the explicitly supplied directory.
        if path.is_symlink() or not path.is_file():
            continue
        session, bad = read_session(path)
        skipped += bad
        if session is not None and (cutoff is None or session['start'] is not None and session['start'] >= cutoff):
            sessions.append(session)
    sessions.sort(key=lambda s: s['start'] or datetime.min.replace(tzinfo=timezone.utc))
    result = {'sessions': len(sessions), 'm1_first_turn': aggregate(sessions, 'm1', TOKENS),
              'm2_first_action': aggregate(sessions, 'm2', (*TOKENS, 'seconds')), 'skipped': skipped}
    result['m1_first_turn']['session_totals'] = aggregate(sessions, 'total', TOKENS)['totals']
    result['runs'] = run_numbers(sessions, cutoff)
    budget = budget_today()
    if budget:
        result['budget'] = budget
    if record:
        record_sessions(sessions)
    if as_json:
        say(dumps(result, sort_keys=True))
    else:
        say(f"Sessions: {len(sessions)}; skipped lines: {skipped}")
        for label, key in (('M1 first turn', 'm1_first_turn'), ('M2 first useful action', 'm2_first_action')):
            for period in ('median', 'last_10_median', 'totals'):
                say(f'{label} {period}: {dumps(result[key][period], sort_keys=True)}')
        say('Session token totals: ' + dumps(result['m1_first_turn']['session_totals'], sort_keys=True))
        runs = result['runs']
        share = runs['lead_share']
        say(f"Lead share: {runs['lead_tokens']} / ({runs['lead_tokens']} + {runs['bot_tokens']}) = "
            + (f"{share * 100:.1f}%" if share != UNKNOWN else UNKNOWN))
        coverage = runs['coverage']
        say(f"Run coverage: {runs['known']} of {runs['runs']} runs with known numbers ("
            + (f"{coverage * 100:.0f}%" if coverage != UNKNOWN else UNKNOWN) + ')')
        for key, row in runs['per_class_bot'].items():
            say(f"Tokens per run, {key}: {row['tokens_per_run']} ({row['known']} of {row['runs']} runs known)")
        for key, row in budget.items():
            if isinstance(row, dict):
                say(f"Budget today ({budget['day']} UTC), {key}: {row['used']} of {row['cap']} tokens")
        if 'per_run' in budget:
            say(f"Budget per run: {budget['per_run']} tokens")
    return 0


COMMANDS = {'stats': cmd_stats}
