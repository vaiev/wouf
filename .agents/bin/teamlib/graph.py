"""Deterministic source context, without importing or executing analysed code.

Callers are lexical name-followed-by-parenthesis matches, not resolved call edges;
tests are lexical name references. Both are capped at five locations per symbol.
Only regular UTF-8 files below 1 MiB are read; symlinks are never followed.
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from teamlib.core import KNOWN, mask_secrets, put, say

USAGE = '  team graph --paths "<globs>" [--repo PATH] [-o FILE]  write CONTEXT_GRAPH.md (400 lines max)'
LANGUAGES = {'.py': 'Python', '.rs': 'Rust', '.ts': 'TypeScript', '.tsx': 'TypeScript',
             '.c': 'C', '.h': 'C'}
FALLBACK = re.compile(r'\b(fn|struct|enum|impl|trait|function|class|interface)\s+([A-Za-z_]\w*)')
C_FUNCTION = re.compile(r'^\s*(?:[\w*]+\s+)+([A-Za-z_]\w*)\s*(\([^;{}]*\))\s*(?:\{|$)')
WORD = re.compile(r'\b[A-Za-z_]\w*\b')


def _matches(path: str, pattern: str) -> bool:
    """Shell-style path globs, including zero-directory **/ and hidden files."""
    pattern = pattern.removeprefix('./')
    if pattern.endswith('/'):
        pattern += '**'
    parts = pattern.split('/')
    # fnmatch handles character classes; individual components keep * within a directory.
    names = path.split('/')

    def match(i, j):
        if j == len(parts):
            return i == len(names)
        if parts[j] == '**':
            return match(i, j + 1) or (i < len(names) and match(i + 1, j))
        return i < len(names) and fnmatch.fnmatchcase(names[i], parts[j]) and match(i + 1, j + 1)

    return match(0, 0)


def _sources(repo: Path):
    texts, skipped = {}, dict(large=0, binary=0, **{'.git': 0}, symlink=0, unreadable=0)
    for folder, dirs, files in os.walk(repo, followlinks=False):
        for name in list(dirs):
            path = Path(folder) / name
            if name == '.git' or path.is_symlink():
                skipped['.git' if name == '.git' else 'symlink'] += 1
                dirs.remove(name)
        for name in files:
            path = Path(folder) / name
            if name == '.git':
                skipped['.git'] += 1
                continue
            if path.is_symlink():
                skipped['symlink'] += 1
                continue
            try:
                if not path.is_file():
                    continue
                if path.stat().st_size >= 1024 * 1024:
                    skipped['large'] += 1
                    continue
                data = path.read_bytes()
                if b'\0' in data:
                    skipped['binary'] += 1
                    continue
                text = data.decode('utf-8')
            except UnicodeDecodeError:
                skipped['binary'] += 1
                continue
            except OSError:
                skipped['unreadable'] += 1
                continue
            texts[path.relative_to(repo).as_posix()] = text
    return dict(sorted(texts.items())), skipped


def _python(text: str):
    """Return name, line, signature, sorted in source order, including nested definitions."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return [], ' (parse error)'
    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            signature = ('async ' if isinstance(node, ast.AsyncFunctionDef) else '') + 'def ' + node.name
            signature += '(' + ast.unparse(node.args) + ')'
            if node.returns:
                signature += ' -> ' + ast.unparse(node.returns)
        elif isinstance(node, ast.ClassDef):
            bases = [ast.unparse(base) for base in node.bases]
            bases += [ast.unparse(kw) for kw in node.keywords]
            signature = 'class ' + node.name + ('(' + ', '.join(bases) + ')' if bases else '')
        else:
            continue
        found.append((node.name, node.lineno, signature))
    return sorted(found, key=lambda item: (item[1], item[0])), ''


def _declaration(line: str) -> str:
    # Body braces end declarations; interpolation braces inside literals do not.
    spans = [match.span() for match in STRING_LITERAL.finditer(line)]
    for index, char in enumerate(line):
        if char == '{' and not any(start <= index < end for start, end in spans):
            return line[:index].strip()
    return line.strip()


def _regex(text: str, language: str):
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        match = FALLBACK.search(line)
        if match:
            found.append((match[2], number, _declaration(line)))
        elif language == 'C':
            match = C_FUNCTION.match(line)
            if match:
                found.append((match[1], number, _declaration(line)))
    return found


def _tags(repo: Path, paths: list[str]):
    """One bounded universal-ctags invocation; ignore all project/user option files."""
    executable = shutil.which('ctags')
    if not executable or not paths:
        return None
    try:
        done = subprocess.run([executable, '--options=NONE', '--output-format=json', '--fields=+nKS',
                               '--sort=yes', '-f', '-', '--', *paths], cwd=repo,
                              capture_output=True, text=True, timeout=0.5)
        if done.returncode:
            return None
        tags = {path: [] for path in paths}
        for line in done.stdout.splitlines():
            tag = json.loads(line)
            if tag.get('_type') != 'tag':
                continue
            path = tag.get('path', '').removeprefix('./')
            number, name = tag.get('line'), tag.get('name')
            if path in tags and isinstance(number, int) and number > 0 and isinstance(name, str):
                signature = str(tag.get('kind', 'symbol')) + ' ' + name + str(tag.get('signature', ''))
                tags[path].append((name, number, signature))
        return {path: sorted(set(items), key=lambda item: (item[1], item[0], item[2]))
                for path, items in tags.items()}
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None


# Roots cannot follow a letter, digit, colon, slash or dot. Tilde denotes
# a home directory even when the expanded path has no explicit home segment.
ABSOLUTE_PATH = re.compile(r"(?<![^\W_])(?<![:/.])(?:[A-Za-z]:[/\\]|\\\\|/|~[\w.-]*/)[^\s\"')\],]*")


# Longest delimiters first; preserve prefixes and raw-string hash delimiters.
STRING_LITERAL = re.compile(
    r'(?P<rust>br|r)(?P<hashes>\#+)"(?P<rawbody>.*?)"(?P=hashes)'
    r'|(?P<prefix>(?i:rb|br|fr|rf|r|f|b|u)?)(?P<quote>"""|\x27\x27\x27|`|"|\x27)'
    r'(?P<body>(?:\\.|(?!(?P=quote)).)*)(?P=quote)', re.DOTALL)
# A literal holds an absolute root anywhere; the same boundary as ABSOLUTE_PATH applies.
LITERAL_ROOT = re.compile(r'(?<![\w:/.])(?:/|~[\w.-]*/|\\\\|[A-Za-z]:[/\\])')


def _relative(text: str, root: str) -> str:
    """Repository root -> '.', and repository children lose the root prefix."""
    if not root:
        return text
    return re.sub(r'(?<![\w:/.])' + re.escape(root) + r'(/|(?=\s|$))',
                  lambda match: '' if match[1] == '/' else '.', text)


def _literal(match, root: str) -> str:
    # Whole literal: quotes stay, contents go when they name an absolute root.
    if match['rust'] is not None:
        opening = match['rust'] + match['hashes'] + '"'
        closing = '"' + match['hashes']
        body = match['rawbody']
    else:
        opening = match['prefix'] + match['quote']
        closing = match['quote']
        body = match['body']
    body = _relative(body, root)
    return opening + ('[path]' if LITERAL_ROOT.search(body) else body) + closing


PRIVATE_LINE = '[line with a private path removed]'
PRIVATE_SEGMENT = re.compile(r'(?i)[/\\](?:home|users)[/\\]|~[\w.-]*/')


# Bind private segments to their own rooted path, rather than punctuation
# elsewhere in the signature. Spaces remain allowed for unquoted volume names.
ROOTED_PATH = re.compile(
    r'~[\w.-]*/[^\n\"\'`()\[\],;]*'
    r'|(?<![\w./\\])(?:[A-Za-z][A-Za-z0-9+.-]*:[/\\]|[/\\])'
    r'[^\n\"\'`()\[\],;]*')


def _line_check(line: str) -> str:
    """Final boundary: only rooted paths can contain private home segments."""
    for match in ROOTED_PATH.finditer(line):
        if PRIVATE_SEGMENT.search(match[0]):
            return PRIVATE_LINE
    return line


def _mask(text: str, repo: Path) -> str:
    root = str(repo.resolve())
    prefix = root.rstrip('/') + '/'

    def replace(match):
        value = match[0]
        # Relativize first: repository roots commonly live beneath home/users.
        if value == root:
            return '.'
        if value.startswith(prefix):
            return value[len(prefix):]
        segments = re.split(r'[/\\]+', value)
        if value.startswith('~') or any(part.lower() in ('home', 'users') for part in segments):
            return '[path]'
        return value

    text = STRING_LITERAL.sub(lambda match: _literal(match, root.rstrip('/')), mask_secrets(text, KNOWN))
    return ABSOLUTE_PATH.sub(replace, text)


def _compact(text: str, repo: Path) -> str:
    return ' '.join(_line_check(_mask(text, repo)).split())[:300]


def _language(path: str, text: str):
    language = LANGUAGES.get(Path(path).suffix)
    if not language and re.match(r'#![^\n]*\bpython[0-9.]*\b', text):
        return 'Python'
    return language


def _definitions(texts: dict, selected: list[str], tags):
    """Definitions, parse notes and the method used per language, for each selected file."""
    definitions, notes, methods = {}, {}, {}
    for path in selected:
        language = _language(path, texts[path])
        notes[path] = ''
        if language == 'Python':
            definitions[path], notes[path] = _python(texts[path])
            methods[language] = 'ast'
        elif language:
            definitions[path] = tags[path] if tags is not None else _regex(texts[path], language)
            methods[language] = 'ctags' if tags is not None else 'regex'
        else:
            definitions[path] = []
    return definitions, notes, methods


def _owners(definitions: dict) -> dict:
    owners = {}
    for path, items in definitions.items():
        for name, _, _ in items:
            owners.setdefault(name, set()).add(path)
    return owners


def _is_test(path: str) -> bool:
    return any(part in ('test', 'tests', '__tests__') for part in Path(path).parts) or bool(
        re.search(r'(^test_|_test\.|\.test\.|\.spec\.)', Path(path).name))


def _calls(name: str, line: str) -> bool:
    return bool(re.search(r'\b' + re.escape(name) + r'\s*\(', line)
                and not re.search(r'\b(def|class|fn|function)\s+' + re.escape(name) + r'\b', line))


def _references(texts: dict, definitions: dict):
    """One repository pass, keeping at most five callers and five test references
    per name. Definitions in the selected file itself are not external callers."""
    owners = _owners(definitions)
    references = {name: [] for name in owners}
    calls = {name: [] for name in owners}
    for path, text in texts.items():
        is_test = _is_test(path)
        for number, line in enumerate(text.splitlines(), 1):
            for name in sorted(set(WORD.findall(line)) & owners.keys()):
                if is_test and len(references[name]) < 5:
                    references[name].append((path, number))
                if path not in owners[name] and len(calls[name]) < 5 and _calls(name, line):
                    calls[name].append((path, number))
    return references, calls


def _header(methods: dict, skipped: dict) -> list[str]:
    return ['# CONTEXT_GRAPH', '', 'Methods: ' + ('; '.join(f'{lang}: {method}' for lang, method in sorted(methods.items())) or 'none'),
            'Skipped: ' + ', '.join(f'{key}={value}' for key, value in sorted(skipped.items())),
            'References: lexical; at most 5 callers and 5 test locations per name.', '']


def _body(selected, definitions, notes, calls, references, repo) -> list[str]:
    lines = [] if selected else ['No files matched.']
    for path in selected:
        lines.append(f'## {path}:1{notes[path]}')
        for name, number, signature in definitions[path]:
            lines.append(f'- {path}:{number} {_compact(signature, repo)}')
            for label, locations in (('caller', calls[name]), ('test', references[name])):
                lines.extend(f'  - {label} {target}:{lineno} {name}' for target, lineno in locations if target != path)
    return lines


def _finish(lines: list[str], repo, limit: int) -> str:
    # Check after masking but before the line cap; count all removed source lines.
    lines = [_line_check(line) for line in _mask('\n'.join(lines), repo).splitlines()]
    removed = sum(PRIVATE_LINE in line for line in lines)
    footer = f'Private path lines removed: {removed}'
    if len(lines) + 1 > limit:
        lines = lines[:max(0, limit - 2)] + (['... truncated (line limit)'] if limit > 1 else [])
    lines.append(footer)
    return '\n'.join(_line_check(line) for line in lines) + '\n'


def graph(repo: Path, globs: list[str], limit: int = 400) -> str:
    """Build a masked Markdown graph, at most min(limit, 400) lines.

    Paths and locations are repository-relative. Skip counters cover the entire
    repository; .git counts pruned directories (or worktree pointer files).
    Output metadata contains neither wall-clock times nor absolute root paths.
    """
    limit = max(0, min(limit, 400))
    if not limit:
        return ''
    texts, skipped = _sources(Path(repo))
    selected = [path for path in texts if any(_matches(path, glob) for glob in globs)]
    foreign = [path for path in selected if LANGUAGES.get(Path(path).suffix) in ('Rust', 'TypeScript', 'C')]
    definitions, notes, methods = _definitions(texts, selected, _tags(Path(repo), foreign))
    references, calls = _references(texts, definitions)
    lines = _header(methods, skipped) + _body(selected, definitions, notes, calls, references, repo)
    return _finish(lines, repo, limit)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def do_graph(rest: list[str]) -> int:
    parser = _Parser(usage=USAGE.strip(), add_help=False)
    parser.add_argument('--paths', required=True)
    parser.add_argument('--repo', type=Path, default=Path.cwd())
    parser.add_argument('-o', type=Path, default=Path('CONTEXT_GRAPH.md'))
    # argparse normally writes directly; route every diagnostic through the sinks.
    try:
        args, extra = parser.parse_known_args(rest)
        if extra or not args.paths or not args.repo.is_dir():
            raise ValueError('invalid arguments or repository')
        patterns = shlex.split(args.paths.replace(',', ' '))
        put(args.o, graph(args.repo, patterns, 400))
    except (OSError, ValueError, AttributeError, TypeError) as error:
        say(f'{USAGE.strip()}\n{error}', err=True)
        return 2
    return 0


COMMANDS = {'graph': do_graph}
