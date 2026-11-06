"""Close a lead session, persisting only numerical transcript measurements."""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

from teamlib.core import die, dumps, say

USAGE = '  team session end [--claude] [--transcript PATH]'


def cmd_session(args):
    # Resolve the shared reader at execution, keeping command discovery independent.
    stats = importlib.import_module('teamlib.stats')
    TOKENS, UNKNOWN = stats.TOKENS, stats.UNKNOWN
    if not args or args[0] != 'end':
        die(USAGE.strip())
    claude = False
    path = None
    i = 1
    while i < len(args):
        if args[i] == '--claude' and not claude:
            claude = True
        elif args[i] == '--transcript' and i + 1 < len(args) and path is None:
            i += 1
            path = args[i]
        else:
            die(USAGE.strip())
        i += 1
    if claude:
        if path is not None:
            die(USAGE.strip())
        try:
            payload = json.load(sys.stdin)
        except (ValueError, RecursionError):
            die('session: invalid hook payload')
        path = payload.get('transcript_path') if isinstance(payload, dict) else None
        if not isinstance(path, str) or not path:
            die('session: hook requires a transcript path')
    session = stats.read_session(Path(path))[0] if path else None
    clean = lambda values: {k: None if v == UNKNOWN else v for k, v in values.items()}
    empty = dict.fromkeys(TOKENS)
    numbers = {
        'first_turn_tokens': clean(session['m1']) if session else empty,
        'time_to_first_action': (None if session['m2']['seconds'] == UNKNOWN else session['m2']['seconds']) if session else None,
        'tokens_to_first_action': clean({k: session['m2'][k] for k in TOKENS}) if session else empty,
        'total_tokens': clean(session['total']) if session else empty,
    }
    log = stats.log_module()
    log.event_append('session.ended', 'lead', '', {'cli': 'claude' if claude else 'manual', 'reason': 'ended'})
    log.event_append('usage.session', 'lead', '', numbers)
    if session:
        say(dumps(numbers, sort_keys=True))
    return 0


COMMANDS = {'session': cmd_session}
