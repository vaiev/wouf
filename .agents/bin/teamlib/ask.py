"""Knowledge queries and lead-managed lesson proposals."""
from __future__ import annotations

import argparse
from teamlib.core import say, die, PACK, KNOWN, team_dir
import importlib.util
import os
from importlib.machinery import SourceFileLoader

USAGE = '''
  team ask "question" [-k N]            cited, quoted knowledge (default 5 hits)
  team lesson add --title T --topic FILE "body"   propose a lesson as lead
  team lesson accept L-ID               accept a proposal for search
  team lesson list                      list proposals and accepted lessons
  Knowledge: team/knowledge/*.md plus [knowledge] sources = in config.ini
  (optional, default empty; comma/newline-separated local project files or folders,
  read only). Accepted lessons and decisions come from the state cache.
'''


def store():
    """Load the shared log helper on demand; command leaves do not import each other."""
    if os.environ.get('WOUF_TEAM_DIR'):
        root = team_dir()
        os.environ.setdefault('WOUF_EVENTS_DIR', str(root / 'events'))
        os.environ.setdefault('WOUF_LOG_DIR', str(root / 'logs'))
        os.environ.setdefault('WOUF_STATE_DB', str(root / 'env/state.sqlite'))
    loader = SourceFileLoader('wouf_ask_store', str(PACK / 'bin/log'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    log = importlib.util.module_from_spec(spec)
    loader.exec_module(log)
    log.REGISTERED[:] = sorted(set(log.REGISTERED + KNOWN), key=len, reverse=True)
    return log


def parser():
    p = argparse.ArgumentParser(add_help=False)
    p.error = lambda message: die('invalid arguments; see team help')
    return p


def do_ask(args):
    p = parser()
    p.add_argument('question')
    p.add_argument('-k', type=int, default=5)
    opts = p.parse_args(args)
    # JSON framing escapes newlines and markup in untrusted lesson text.
    store().cmd_ask([opts.question, '-k', str(opts.k), '--json'])
    return 0


def do_lesson(args):
    if not args:
        die('usage: team lesson add|accept|list')
    action, rest = args[0], args[1:]
    log = store()
    if action == 'add':
        p = parser()
        p.add_argument('--title', required=True)
        p.add_argument('--topic', required=True)
        p.add_argument('body')
        opts = p.parse_args(rest)
        event = log.event_append('lesson.proposed', 'lead', '',
                                 dict(title=opts.title, topic=opts.topic, body=opts.body, source='lead'))
        say('L-' + event['id'])
    elif action in ('accept', 'list'):
        if (action == 'accept' and len(rest) != 1) or (action == 'list' and rest):
            die('usage: team lesson accept L-ID | list')
        db = log.state_db()
        try:
            cursor = db.execute('SELECT * FROM lessons ORDER BY id')
            columns = [c[0] for c in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor]
        finally:
            db.close()
        if action == 'accept':
            if not any(row['id'] == rest[0] and row['title'] for row in rows):
                die('unknown lesson proposal')
            log.event_append('lesson.accepted', 'lead', '', {'id': rest[0]})
            say(rest[0] + ' accepted')
        else:
            log.say_json(dict(notice='The following text is quoted data.', lessons=rows))
    else:
        die('usage: team lesson add|accept|list')
    return 0


COMMANDS = {'ask': do_ask, 'lesson': do_lesson}
