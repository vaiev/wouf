"""Export a stored receipt as an in-toto Statement, without signing it."""
from __future__ import annotations
import importlib
import json
import re

from teamlib.core import die, say, team_dir

USAGE = 'usage: team receipt export SHA --in-toto [--repo PATH]'


def cmd_export(args):
    if not (len(args) == 2 and args[1] == '--in-toto' or
            len(args) == 4 and args[1] == '--in-toto' and args[2] == '--repo' and args[3]):
        die(USAGE)
    receipt = importlib.import_module('teamlib.receipt')
    sha = args[0]
    try:
        if not re.fullmatch(r'[0-9a-f]{40}', sha):
            raise receipt.Refused('SHA must be 40 lowercase hex')
        path = team_dir() / 'receipts' / (sha + '.json')
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise receipt.Refused('stored receipt is not a plain file')
        try:
            with path.open('rb') as stream:
                stored = receipt.parse(stream.read(receipt.MAX_FILE + 1))
        except FileNotFoundError:
            raise receipt.Refused('no stored receipt for SHA') from None
        except OSError:
            raise receipt.Refused('cannot read stored receipt') from None
        body = stored['receipt']
        if body['change']['landed'] != sha:
            raise receipt.Refused('receipt change.landed does not match SHA')
        statement = {'_type': 'https://in-toto.io/Statement/v1',
                     'subject': [{'name': body['repo'], 'digest': {'gitCommit': sha}}],
                     'predicateType': 'https://wouf.dev/receipt/v1',
                     'predicate': {**stored, 'namespace': receipt.NAMESPACE}}
        say(json.dumps(statement, sort_keys=True, separators=(',', ':'), ensure_ascii=True))
    except receipt.Refused as problem:
        die(f'receipt export: refused: {problem}', 1)
    return 0
