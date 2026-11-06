#!/usr/bin/env python3
"""Compare AST-only function complexity in two git trees (Python 3.9+).

Depth counts decision blocks, including conditional expressions and comprehension
filters; boolean operands add complexity but no nesting. Elif is a sibling
branch. Nested functions/classes have independent metrics. Invalid base source
is reported and head functions use the new-function ceilings; invalid head
source fails closed. Renames are treated as removal plus addition.
"""
import ast
import re
import subprocess
import sys
from pathlib import PurePosixPath


class Decisions(ast.NodeVisitor):
    def __init__(self):
        self.complexity = 1
        self.depth = 0
        self.maximum = 0

    def visit_FunctionDef(self, node):
        pass  # A nested definition is measured separately.

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef

    def block(self, nodes):
        self.complexity += 1
        self.depth += 1
        self.maximum = max(self.maximum, self.depth)
        for node in nodes:
            self.visit(node)
        self.depth -= 1

    def visit_If(self, node):
        branches = node.orelse
        if (len(branches) == 1 and isinstance(branches[0], ast.If)
                and branches[0].col_offset == node.col_offset):
            self.block([node.test, *node.body])
            self.visit(branches[0])  # elif has the same indentation as if.
        else:
            self.block([node.test, *node.body, *branches])

    def visit_For(self, node):
        self.block([node.target, node.iter, *node.body, *node.orelse])

    visit_AsyncFor = visit_For

    def visit_While(self, node):
        self.block([node.test, *node.body, *node.orelse])

    def visit_ExceptHandler(self, node):
        self.block(list(ast.iter_child_nodes(node)))

    visit_IfExp = visit_ExceptHandler
    visit_match_case = visit_ExceptHandler

    def visit_BoolOp(self, node):
        self.complexity += len(node.values) - 1
        self.generic_visit(node)

    def comprehension(self, generators, values):
        if not generators:
            for value in values:
                self.visit(value)
            return
        generator, *rest = generators
        self.visit(generator.iter)
        self.complexity += 1
        self.depth += 1
        self.maximum = max(self.maximum, self.depth)
        for condition in generator.ifs:
            self.complexity += 1
            self.depth += 1
            self.maximum = max(self.maximum, self.depth)
            self.visit(condition)
        self.comprehension(rest, values)
        self.depth -= 1 + len(generator.ifs)

    def visit_ListComp(self, node):
        self.comprehension(node.generators, [node.elt])

    visit_SetComp = visit_ListComp
    visit_GeneratorExp = visit_ListComp

    def visit_DictComp(self, node):
        self.comprehension(node.generators, [node.key, node.value])


def functions(source):
    """Return qualified name -> (line, cyclomatic complexity, maximum depth)."""
    result = {}

    def collect(node, prefix=''):
        for child in ast.iter_child_nodes(node):
            name = prefix
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = prefix + child.name
                if not isinstance(child, ast.ClassDef):
                    visitor = Decisions()
                    for statement in child.body:
                        visitor.visit(statement)
                    result[name] = (child.lineno, visitor.complexity, visitor.maximum)
                name += '.'
            collect(child, name)

    collect(ast.parse(source))
    return result


def git(*args):
    return subprocess.check_output(['git', *args], stderr=subprocess.PIPE)


def python_path(path):
    return path.endswith('.py') or (
        path.startswith('.agents/bin/') and not PurePosixPath(path).suffix)


def read_functions(revision, path, exists):
    if not exists:
        return {}, False
    try:
        return functions(git('show', revision + ':' + path)), False
    except (SyntaxError, ValueError) as error:
        print('%s:%s: syntax error at %s: %s' %
              (path, getattr(error, 'lineno', 1), revision, error))
        return {}, True


def violations(old, new):
    _, complexity, depth = new
    if old is None:
        return complexity > 15 or depth > 5
    return complexity > old[1] + 2 or depth > old[2] + 1


def describe(old, new):
    before = 'new' if old is None else str(old[1])
    depth = 'new' if old is None else str(old[2])
    return 'complexity %s -> %s, depth %s -> %s' % (before, new[1], depth, new[2])


def compare(base, head):
    message = git('show', '-s', '--format=%B', head).decode('utf-8', 'replace')
    escapes = dict(re.findall(r'^Complexity: (\S+) ([^\s\n][^\n]*)$', message, re.M))
    changes = git('diff', '--no-renames', '--name-status', '-z', base, head).split(b'\0')
    files = count = failures = 0
    for status, raw_path in zip(changes[0::2], changes[1::2]):
        path = raw_path.decode('utf-8', 'surrogateescape')
        if not python_path(path):
            continue
        files += 1
        if status == b'D':
            continue
        old, _ = read_functions(base, path, status != b'A')
        new, invalid = read_functions(head, path, True)
        failures += int(invalid)
        count += len(new)
        for name, metric in new.items():
            previous = old.get(name)
            if name in escapes:
                print('ACCEPTED %s:%s %s: %s (%s)' %
                      (path, metric[0], name, describe(previous, metric), escapes[name]))
            elif violations(previous, metric):
                failures += 1
                print('FAIL %s:%s %s: %s' %
                      (path, metric[0], name, describe(previous, metric)))
    print('%s files, %s functions, %s failures' % (files, count, failures))
    return int(bool(failures))


def main():
    if len(sys.argv) != 3:
        print('Usage: complexity.py BASE HEAD', file=sys.stderr)
        return 1
    try:
        return compare(*sys.argv[1:])
    except subprocess.CalledProcessError as error:
        print('complexity: git failed: ' + error.stderr.decode('utf-8', 'replace').strip(),
              file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
