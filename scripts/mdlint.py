#!/usr/bin/env python3
"""Check Markdown files for the markdownlint rules bots break most (Python 3.9+, stdlib only).

Usage: scripts/mdlint.py [FILE...]; without FILE every tracked *.md file. Prints one line per finding,
`PATH:LINE MDxxx reason`, and exits 1 if there is one, else 0 without output (2 on a usage error).
Outside a git work tree (or without git) FILE defaults to every *.md file under the current folder, dot
folders included, .git and node_modules skipped, like markdownlint-cli2's `**/*.md`.

The rules follow markdownlint's defaults: MD009 trailing spaces (exactly two allowed, br_spaces 2), MD010
hard tabs, MD012 more than one consecutive blank line, MD022 headings, MD031 fenced code blocks and MD032
lists without blank lines around, MD047 a file not ending in exactly one newline. A rule set to false in the
`config:` map of `.markdownlint-cli2.yaml` (in the current folder) is skipped, as is every rule not set
to true when `default: false`. YAML front matter is skipped like markdownlint does. The block parsing is
a small subset of CommonMark: fenced, indented and HTML blocks, ATX and setext headings and top-level
lists with lazy continuation lines, also inside block quotes; those inside HTML blocks are not checked.
Lines of a block quote inside a list item get no finding from any rule: they are left to docs-ci's markdownlint.
"""
import os
import re
import subprocess
import sys

CONFIG = ".markdownlint-cli2.yaml"
SKIP_DIRS = {".git", "node_modules"}
NAMES = {
    "MD009": "no-trailing-spaces", "MD010": "no-hard-tabs", "MD012": "no-multiple-blanks",
    "MD022": "blanks-around-headings", "MD031": "blanks-around-fences", "MD032": "blanks-around-lists",
    "MD047": "single-trailing-newline",
}
FENCE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")
ATX = re.compile(r"^ {0,3}#{1,6}(?:[ \t]|$)")
SETEXT = re.compile(r"^ {0,3}(?:=+|-+)[ \t]*$")
BREAK = re.compile(r"^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$")
ITEM = re.compile(r"^( {0,3})([-*+]|\d{1,9}[.)])(?:( +)(.*)|[ \t]*)$")
FRONT_OPEN = re.compile(r"^---[ \t]*$")
FRONT_CLOSE = re.compile(r"^---\s*$")
HTML_ENDS = (
    (re.compile(r"^ {0,3}<(?:script|pre|style|textarea)(?:\s|>|$)", re.I), "</"),
    (re.compile(r"^ {0,3}<!--"), "-->"),
    (re.compile(r"^ {0,3}<\?"), "?>"),
    (re.compile(r"^ {0,3}<![A-Za-z]"), ">"),
    (re.compile(r"^ {0,3}<!\[CDATA\["), "]]>"),
    (re.compile(r"^ {0,3}</?(?:address|article|aside|base|basefont|blockquote|body|caption|center|col|colgroup|dd"
                r"|details|dialog|dir|div|dl|dt|fieldset|figcaption|figure|footer|form|frame|frameset|h[1-6]|head"
                r"|header|hr|html|iframe|legend|li|link|main|menu|menuitem|nav|noframes|ol|optgroup|option|p"
                r"|param|search|section|summary|table|tbody|td|tfoot|th|thead|title|tr|track|ul)(?:\s|/?>|$)",
                re.I), ""),
)
QUOTE = re.compile(r"^ {0,3}> ?")
QUOTED_ITEM = re.compile(r"(?:(?:[-*+]|\d{1,9}[.)]) +)*>")
HTML_TAG = re.compile(r"^ {0,3}(?:<[A-Za-z][A-Za-z0-9-]*(?:\s[^>]*)?/?>|</[A-Za-z][A-Za-z0-9-]*\s*>)\s*$")


def enabled_rules(path=CONFIG):
    """The rules on in the flat `MDxxx: true|false` lines of the config file's `config:` map."""
    values, inside = {}, False
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        text = ""
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if not raw[0].isspace():
            inside = line == "config:"
            continue
        match = re.match(r"\s+([\w-]+):\s*(true|false)$", line, re.I) if inside else None
        if match:
            values[match.group(1).lower()] = match.group(2).lower() == "true"
    default = values.get("default", True)
    return {rule for rule, name in NAMES.items() if values.get(rule.lower(), values.get(name, default))}


def is_blank(line):
    """markdownlint's isBlankLine: missing, whitespace, or only HTML comments and `>` markers."""
    if line is None or not line.strip():
        return True
    while True:
        start, end = line.find("<!--"), line.find("-->")
        if end != -1 and (start == -1 or end < start):
            line = line[end + 3:]
        elif start != -1 and end != -1:
            line = line[:start] + line[end + 3:]
        elif start != -1:
            line = line[:start]
        else:
            return not line.replace(">", "").strip()


def lead(line):
    return len(line) - len(line.lstrip(" "))


def item_indent(match):
    """The content column of a list item."""
    width = len(match.group(1)) + len(match.group(2))
    spaces = match.group(3) or ""
    return width + (1 if not match.group(4) or len(spaces) > 4 else len(spaces))


def item_kind(match):
    return match.group(2)[-1]


def can_interrupt(match):
    """A list item may interrupt a paragraph only when it has content and, if ordered, starts at 1."""
    marker = match.group(2)
    return bool(match.group(4)) and (marker in "-*+" or int(marker[:-1]) == 1)


def html_end(line, paragraph):
    """The end marker of the HTML block `line` starts ('' ends at a blank line), or None."""
    for pattern, end in HTML_ENDS:
        if pattern.match(line):
            return end
    return "" if paragraph is None and HTML_TAG.match(line) else None


def block_start(line):
    """Whether `line` starts a block that ends a paragraph (so it is no lazy continuation line)."""
    if ATX.match(line) or FENCE.match(line) or BREAK.match(line) or line.lstrip().startswith(">"):
        return True
    match = ITEM.match(line)
    return bool(match and can_interrupt(match)) or html_end(line, None) not in (None, "")


def open_fence(match):
    """(char, length) of a fence opening, or None when the backtick info string holds a backtick."""
    fence = match.group(2)
    if fence[0] == "`" and "`" in match.group(3):
        return None
    return fence[0], len(fence)


def quote_prefix(line):
    """The start of `line` up to its last block quote marker: as many as QUOTE.sub strips one after the other.

    The space after a marker goes with it, so up to 4 spaces may come before the next `>` (3 before the first)."""
    head = " " + line[:len(line) - len(line.lstrip(" >"))]
    cut = head.find("     >")
    head = head if cut == -1 else head[:cut]
    return head[1:head.rfind(">") + 1]


class Markers:
    """The `>` markers that start each line: the text inside a depth of them, and where a quote run ends."""

    def __init__(self, lines):
        self.lines = lines
        self.prefix = [quote_prefix(line) for line in lines]
        self.count = [prefix.count(">") for prefix in self.prefix]
        self.mins = None

    def text(self, i, depth):
        """Line `i` without its first `depth` markers, as `depth` times QUOTE.sub would leave it."""
        if not depth:
            return self.lines[i]
        if depth == self.count[i]:
            at = len(self.prefix[i])
        else:
            at = sum(map(len, self.prefix[i].split(">", depth)[:depth])) + depth
        return self.lines[i][at + 1 if self.lines[i][at:at + 1] == " " else at:]

    def end(self, i, depth, last):
        """The last line from `i` to `last` up to which every line has more than `depth` markers."""
        if self.mins is None:
            self.mins, size = [self.count], 1  # mins[k][j]: the fewest markers on lines j to j + 2**k - 1
            while size * 2 <= len(self.count):
                below = self.mins[-1]
                self.mins.append([min(below[j], below[j + size]) for j in range(len(below) - size)])
                size *= 2
        j = i
        for k in range(len(self.mins) - 1, -1, -1):
            if j + (1 << k) - 1 <= last and self.mins[k][j] > depth:
                j += 1 << k
        return j - 1


class Blocks:
    """Code lines, fenced blocks, headings and top-level lists (0-based), also in block quotes.

    One pass per block quote depth, from a work list rather than recursion, so deep nesting cannot exhaust
    the stack; a run of quoted lines is skipped in one jump and parsed again one depth deeper."""

    def __init__(self, lines):
        self.code, self.fences, self.headings, self.lists = set(), [], [], []
        self.unknown = set()  # lines of a block quote inside a list item: not parsed, so no finding
        self.markers = Markers(lines)
        todo = [(0, 0, len(lines) - 1)]
        while todo:
            self.depth, first, last = todo.pop()
            self.quotes = []
            self.scan(first, last)
            todo += [(self.depth + 1, start, end) for start, end in self.quotes]

    def scan(self, first, last):
        """The lines `first` to `last`, each seen inside `self.depth` block quote markers."""
        self.fence = self.html = self.indented = self.item = self.para = None
        self.last, i = last, first
        while i <= last:
            self.jump = i
            if self.markers.count[i] > self.depth and self.idle():
                self.quote(i)  # what step would do, without cutting the markers off the line
            else:
                self.step(i, self.markers.text(i, self.depth))
            i = self.jump + 1
        if self.fence:
            self.fences.append((self.fence["start"], last if self.depth else None))
        if self.indented:
            self.end_indented()
        if self.item:
            self.close_list()

    def idle(self):
        """Whether no fence, HTML block, indented code block or list is open."""
        return not (self.fence or self.indented or self.item) and self.html is None

    def quote(self, i):
        """A block quote starts on line `i`: its run of quoted lines is parsed one depth deeper."""
        self.para, self.jump = None, self.markers.end(i, self.depth, self.last)
        self.quotes.append((i, self.jump))

    def step(self, i, line):
        if self.fence and self.in_fence(i, line):
            return
        if self.html is not None and self.in_html(line):
            return
        if self.indented and self.in_indented(i, line):
            return
        if not line.strip():
            self.para = None
            if self.item:
                self.item.update(blank=True, lazy=False, quoted=False)
            return
        if self.item and self.in_list(i, line):
            return
        self.top(i, line)

    def in_fence(self, i, line):
        fence, base = self.fence, self.fence["base"]
        if base and line.strip() and lead(line) < base:
            self.fences.append((fence["start"], i - 1))  # the list item ends, and its fence with it
            self.fence = None
            return False
        close = re.match(r" {0,3}(%s{%d,})[ \t]*$" % (re.escape(fence["char"]), fence["length"]), line[base:])
        if self.item and base and line.strip():
            self.item.update(end=i, blank=False, lazy=False)
        if close and lead(line) >= base:
            self.fences.append((fence["start"], i))
            self.fence = None
        else:
            self.code.add(i)
        return True

    def in_html(self, line):
        if self.html == "":
            if not line.strip():
                self.html = None
                return False
        elif self.html in line:
            self.html = None
        return True

    def in_indented(self, i, line):
        if line.strip() and lead(line) < 4:
            self.end_indented()
            return False
        if line.strip():
            self.indented[1] = i
        return True

    def end_indented(self):
        self.code.update(range(self.indented[0], self.indented[1] + 1))
        self.indented = None

    def in_list(self, i, line):
        item = self.item
        if lead(line) >= item["indent"]:
            self.item_line(i, line[item["indent"]:], item["indent"])
            return True
        match = None if BREAK.match(line) else ITEM.match(line)
        if match and item_kind(match) == item["kind"]:
            item["indent"] = item_indent(match)
            self.item_line(i, match.group(4) or "", item["indent"])
            return True
        if not item["blank"] and item["lazy"] and not block_start(line):
            item["end"] = i
            if item.get("quoted"):
                self.unknown.add(i)
            return True
        self.close_list()
        return False

    def item_line(self, i, content, base):
        """A line inside the open list; `content` is the part from column `base` on."""
        lazy = self.item.get("quoted") and content.strip() and not block_start(content)
        self.item.update(end=i, blank=False, lazy=False, quoted=False)
        if lazy or ">" in content and QUOTED_ITEM.match(content.lstrip(" ")):
            self.unknown.add(i)  # a block quote in the item, or its lazy line: left to markdownlint
            self.item.update(lazy=True, quoted=True)
            return
        fence = FENCE.match(content)
        opened = open_fence(fence) if fence else None
        if opened:
            self.fence = {"char": opened[0], "length": opened[1], "base": base + len(fence.group(1)), "start": i}
        elif ATX.match(content):
            self.headings.append((i, i))
        else:
            match = None if BREAK.match(content) else ITEM.match(content)
            text = (match.group(4) or "") if match else content
            self.item["lazy"] = bool(text.strip()) and not block_start(text)

    def close_list(self):
        self.lists.append((self.item["start"], self.item["end"]))
        self.item = None

    def top(self, i, line):
        """A line outside every list, fence, HTML and indented code block."""
        if lead(line) >= 4 and self.para is None:
            self.indented = [i, i]
            return
        para, self.para = self.para, None
        fence = FENCE.match(line)
        opened = open_fence(fence) if fence else None
        if opened:
            self.fence = {"char": opened[0], "length": opened[1], "base": 0, "start": i}
            return
        end = html_end(line, para)
        if end is not None:
            self.html = None if end and end in line[lead(line) + 2:] else end
        elif ATX.match(line) or (para is not None and SETEXT.match(line)):
            self.headings.append((i if para is None or ATX.match(line) else para, i))
        elif not BREAK.match(line):
            self.top_text(i, line, para)

    def top_text(self, i, line, para):
        match = ITEM.match(line)
        if match and (para is None or can_interrupt(match)):
            self.item = {"start": i, "end": i, "indent": item_indent(match), "kind": item_kind(match)}
            self.item_line(i, match.group(4) or "", self.item["indent"])
        elif QUOTE.match(line):
            self.quote(i)
        elif not line.lstrip().startswith((">", "|")):
            self.para = i if para is None else para


def md009(lines, blocks):
    for i, line in enumerate(lines):
        spaces = len(line) - len(line.rstrip())
        if spaces and spaces != 2 and i not in blocks.code:
            yield i, "Trailing spaces [Expected: 0 or 2; Actual: %d]" % spaces


def md010(lines, blocks):
    for i, line in enumerate(lines):
        if "\t" in line:
            yield i, "Hard tabs"


def md012(lines, blocks):
    count = 0
    if lines and lines[-1] == "":
        lines = lines[:-1]  # the empty end after the final newline is no line
    for i, line in enumerate(lines):
        count = 0 if i in blocks.code or line.strip() else count + 1
        if count > 1:
            yield i, "Multiple consecutive blank lines [Expected: 1; Actual: %d]" % count


def around(lines, first, last):
    """Whether the line above `first` and the line below `last` are blank (None: no such check)."""
    above = is_blank(lines[first - 1]) if first > 0 else True
    below = True if last is None or last + 1 >= len(lines) else is_blank(lines[last + 1])
    return above, below


def md022(lines, blocks):
    for first, last in blocks.headings:
        above, below = around(lines, first, last)
        if not above:
            yield first, "Headings should be surrounded by blank lines [Expected: 1; Actual: 0; Above]"
        if not below:
            yield first, "Headings should be surrounded by blank lines [Expected: 1; Actual: 0; Below]"


def md031(lines, blocks):
    for first, last in blocks.fences:
        above, below = around(lines, first, last)
        if not above:
            yield first, "Fenced code blocks should be surrounded by blank lines"
        if not below:
            yield last, "Fenced code blocks should be surrounded by blank lines"


def md032(lines, blocks):
    for first, last in blocks.lists:
        above, below = around(lines, first, last)
        if not above:
            yield first, "Lists should be surrounded by blank lines"
        if not below:
            yield last, "Lists should be surrounded by blank lines"


def md047(lines, blocks):
    """A file ends in exactly one newline: the end after it is empty, the line before it is no empty line."""
    if lines[-1] != "":
        yield len(lines) - 1, "Files should end with a single newline character"
    elif len(lines) > 2 and lines[-2] == "":
        yield len(lines) - 2, "Files should end with a single newline character"


RULES = {"MD009": md009, "MD010": md010, "MD012": md012, "MD022": md022, "MD031": md031, "MD032": md032,
         "MD047": md047}


def split(text):
    """The lines, the number of them YAML front matter takes (markdownlint skips them), as markdownlint splits."""
    lines = re.split(r"\r\n|\r|\n", text[1:] if text.startswith("﻿") else text)
    if len(lines) < 2 or not FRONT_OPEN.match(lines[0]):
        return lines, 0
    close = next((i for i in range(1, len(lines)) if FRONT_CLOSE.match(lines[i])), None)
    if close is None:
        return lines, 0
    skip = close + 1
    while skip < len(lines) - 1 and not lines[skip].strip():
        skip += 1  # markdownlint's front matter pattern takes the blank lines after it too, not the end
    return lines, skip


def lint_text(text, rules):
    """Sorted (line number, rule, reason) findings of a document."""
    whole, offset = split(text)
    lines = whole[offset:]
    blocks = Blocks(lines)
    found = [(i + 1 + offset, rule, reason) for rule in sorted(rules - {"MD047"})
             for i, reason in RULES[rule](lines, blocks) if i not in blocks.unknown]
    if "MD047" in rules:  # the file's real end: the blank lines after front matter count here
        found += [(i + 1, "MD047", reason) for i, reason in md047(whole, blocks) if i - offset not in blocks.unknown]
    return sorted(found)


def in_work_tree():
    """Whether the current folder is inside a git work tree (False too when git is missing)."""
    try:
        result = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
    except OSError:
        return False
    return result.returncode == 0 and result.stdout.strip() == b"true"


def walked(root="."):
    """Every *.md file under `root` like markdownlint-cli2's `**/*.md` (dot folders too), sorted and relative.

    .git and node_modules folders are skipped, and symbolic links to folders are not followed."""
    found = []
    for folder, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if name not in SKIP_DIRS]
        rel = os.path.relpath(folder, root)
        found += [name if rel == "." else os.path.join(rel, name) for name in files if name.endswith(".md")]
    return sorted(found)


def tracked():
    """The tracked *.md files in a git work tree, else every *.md file under the current folder."""
    if not in_work_tree():
        return walked()
    out = subprocess.run(["git", "ls-files", "-z", "--", "*.md"], stdout=subprocess.PIPE, check=True).stdout
    return [path for path in out.decode("utf-8", "surrogateescape").split("\0") if os.path.isfile(path)]


def main(argv):
    if any(arg.startswith("-") for arg in argv):
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    try:
        paths = argv or tracked()
    except (OSError, subprocess.CalledProcessError) as error:
        print("mdlint: cannot list the tracked files: %s" % error, file=sys.stderr)
        return 2
    rules, found = enabled_rules(), False
    for path in paths:
        try:
            with open(path, "rb") as fh:
                text = fh.read().decode("utf-8", "replace")
        except OSError as error:
            print("mdlint: %s" % error, file=sys.stderr)
            return 2
        for line, rule, reason in lint_text(text, rules):
            print("%s:%d %s %s" % (path, line, rule, reason))
            found = True
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
