"""guardlib_lead: the lead mode of .agents/bin/guard, loaded by guard only when [guard] lead_mode is on.

Lead mode is an allowlist for the lead's own tool calls. The session may write only the paths of [guard] lead_write
(globs relative to the project, or absolute or ~-rooted for paths outside it) and the write_roots; a refusal names the
path or the command and says "brief a bot".

- file tools: the path of Write, Edit, MultiEdit, NotebookEdit, of every file in an apply_patch patch, and of any tool
  not known here as a reading one (READ_TOOLS) that has a file_path, path or notebook_path, a relative one resolved
  against the tool's cwd, the hook payload's (tool_path_reason); a relative path in a payload without cwd, or a ~
  that does not expand, is "cannot tell where this writes", and guard check-path resolves against the project; a
  reading tool keeps guard's base path rules, as with lead_mode off;
- shell commands: the line is split into simple commands (through ;, &&, ||, |, newlines, ( ) and { }), and every one
  must match a shape of Boundary.shapes with only the options listed there. A construct this module does not parse
  refuses the whole line with "cannot tell where this writes": eval, exec, xargs, bash -c, sh -c and any shell of
  SHELLS (by name or path) given an option or no script, also in a hook's argv (["bash", "-lc", LINE]), a $(...), `...`
  or <(...) other than NAME=$(pass show ENTRY), an arithmetic or ${...} expansion other than ${NAME}, for, while,
  until, if, case, select, a function, coproc, &, a heredoc to a command not listed, a heredoc delimiter with a $,
  a ` or a part the shell computes (bash ends the body at the word as written), an unquoted heredoc body line that
  ends in \\ (bash joins it with the next one) and a $'...' with a \\. A quoted or escaped {, } or NAME=VALUE is the
  program, as for bash. Any other command or option
  refuses it with "not on the lead's list: brief a bot". A word the shell expands (a glob, a brace expansion, a ~
  other than ~ and ~/PATH, a ~ after the = of NAME=, $"...") cannot tell where the guard relies on it as written: in
  uniq and sed, which have an operand that writes, and as a path it checks (a write target, cd, mkdir, cp, mv,
  git -C, git add, git commit, bash, . and source, a pack tool); an operand of the readers, find, git's reads and gh
  may expand after a fixed first character (literals). The shapes: cat, head, tail, wc, ls, cut, tr, diff, grep,
  sort and uniq with only the options of READER_OPTIONS (uniq without an output file), and basename, dirname,
  realpath, pwd, echo, printf, true and date (+FORMAT only) with none; sed -n SCRIPT FILE... (-E too; a script of
  addresses and p or = only); find PATH... with -name, -iname, -path, -type, -maxdepth, -mindepth, -newer, -mtime,
  -size and -print; cd ./DIR, ../DIR or an absolute or ~ DIR inside the project or a write_root (bash looks another
  relative DIR up in CDPATH: cannot tell), followed for the commands after it where bash runs it for sure (Position);
  mkdir [-p] DIR,
  cp SRC DST and mv SRC DST into lead_write (SRC a regular file; mv's SRC in lead_write too); git [-C DIR] status,
  log, show, diff, rev-parse, merge-base, rev-list, ls-files, for-each-ref, cat-file -p OBJECT and branch --list,
  -v or -a (no name) with only GIT_OPTIONS, worktree list and remote -v; git add [--] FILE... of existing regular
  lead_write files, no option; git commit [-q] -m MSG when every staged path is lead_write and none is a gitlink
  (listed past the config that hides or renames one; refused when git cannot list them or a name is not UTF-8);
  gh pr view/list/checks and gh run list/view with only the options of GH_OPTIONS (not --web); the
  pack's .agents/bin/team, log and federate by path; bash PATH ..., and . PATH and source PATH with no other word,
  for a PATH of [guard] lead_run that lead_write and the write_roots do not cover (as written or where it resolves);
  NAME=VALUE before a command and export NAME=VALUE for a NAME of [guard] lead_env, the VALUE $(pass show ENTRY)
  or a literal of env_value_ok. A redirection writes only to /dev/null or a lead_write or write_roots path, with
  > or >> (also 1> and 1>>); 2>&1 and 2>/dev/null are the only other descriptor forms (2>FILE, 3>FILE, >&N, >|,
  <>, &>, the here-string <<< and the like are refused); < reads any file.

Paths are resolved (.., symlinks) before they are matched, and a file with another hard link is refused. The secret
store, .env files, code files (CODE_SUFFIXES) and the files that switch the guard (the team's config.ini and
guard-deny.txt, .agents/bin/guard*, the CLIs' settings files) stay refused even where lead_write covers them; so do a
shell write into a .git folder, git in a repository whose git folder, config or hooks folder lead_write covers, a
name of ENV_RUNS in lead_env (HOME, PATH, every GIT_*, LD_* and PYTHON*, a pager or editor, ...; ignored, with a
stderr line), a lead_run script that lead_write or a write_root covers, and a pack tool that lead_write covers. Any
other lead_env name is trusted as the owner listed it; only its value is bounded.

What this is not: a sandbox. It is a tripwire on the lead's tool calls. The trusted scripts of lead_run and the
pack's tools run unseen: the guard does not look inside them. The host's restricted account or an OS sandbox is the
stronger boundary.
"""
from __future__ import annotations

import fnmatch
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

BRIEF = "brief a bot for this change"
CANNOT_TELL = "cannot tell where this writes"
NOT_LISTED = "not on the lead's list: brief a bot"
CODE_SUFFIXES = frozenset(".py .pyw .sh .bash .zsh .fish .ps1 .bat .cmd .js .mjs .cjs .ts .tsx .jsx .rb .pl .pm .php "
                          ".lua .go .rs .c .h .cc .cpp .hpp .java .kt .swift .cs .ipynb".split())
READ_TOOLS = frozenset("Read Grep Glob LS NotebookRead WebFetch WebSearch read_file read_many_files list_directory "
                       "glob search_file_content view_image".split())
PATH_KEYS = ("file_path", "path", "notebook_path")
PATCH_TARGET = re.compile(r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$", re.M)
ENV_FILE = re.compile(r"\.env(\.[\w-]+)?")
# The CLIs' settings files, below the project and below home: they wire the guard's hook.
SETTINGS = ((".claude", ("settings*.json",)), (".codex", ("config.toml", "hooks.json")),
            (".gemini", ("settings.json",)))

OPERATORS = ("&&", "||", ";;", "|&", ";", "&", "|", "(", ")", "\n")
UNSPLIT = frozenset((";;", "|&", "&"))
REDIRECT = re.compile(r"\d*(>>|>\||>&|<<<|<<-|<<|<>|<&|>|<)|(&>>|&>)")
# A here-string (<<<) is no heredoc: it is outside the redirect rule and refuses the line (review T-0188.r8).
HEREDOCS = frozenset(("<<", "<<-"))
# The redirections whose descriptor is neither none nor 1 that the lead may use: (descriptor, operator, target).
DESCRIPTOR_FORMS = frozenset((("2", ">&", "1"), ("2", ">", "/dev/null")))
# bash's {NAME}> form: it opens a new descriptor and stores its number in NAME.
NAMED_FD = re.compile(r"\{[A-Za-z_]\w*\}")
VARIABLE = re.compile(r"\$(?:[A-Za-z_]\w*|[0-9@*#?$!-])")
PLAIN_BRACE = re.compile(r"#?(?:[A-Za-z_]\w*|[0-9]+)|[@*#?$!-]")
BODY_EXPANSION = re.compile(r"\$\[|\$\{(?!(?:#?(?:[A-Za-z_]\w*|[0-9]+)|[@*#?$!-])\})")
ASSIGNMENT = re.compile(r"([A-Za-z_]\w*)\+?=")
PASS_VALUE = re.compile(r"[A-Za-z_]\w*\+?=(\"?)\$\(pass show [\w.@+][\w.@+-]*(?:/[\w.@+-]+)*\)\1")
# Names that redirect where a program reads its configuration or what it runs, or how it reads its words, where cd
# goes or where it puts files no operand names: lead_env never allows them (review T-0188.r11: HOME=docs/notes git
# status read a .gitconfig the lead wrote there and ran its core.fsmonitor). Also EXECIGNORE (the programs bash skips
# in PATH) and HIST* (bash truncates HISTFILE when HISTFILESIZE is assigned, also without a terminal). env_names
# drops them with a stderr line.
ENV_RUNS = re.compile(r"ENV|IFS|SHELL|PS\d|PROMPT_COMMAND|ZDOTDIR|VISUAL|POSIXLY_CORRECT|PWD|OLDPWD|TMPDIR|TMP|TEMP|"
                      r"EXECIGNORE|OPENSSL_CONF\w*|(?:LD_|DYLD_|GIT_|PYTHON|XDG_|BASH|PERL|RUBY|NODE_|LESS|TERMINFO|"
                      r"PASSWORD_STORE_|HIST)\w*|"
                      r"\w*(?:HOME|PATH|CONFIG\w*|RC|PAGER|EDITOR|BROWSER|ASKPASS|OPTS?|OPTIONS)")
# A lead_env value is $(pass show ENTRY) or a literal of these characters (brief T-0188-fix11: the guard bounds the
# value, not the name); env_value_ok also refuses . and .., a leading ~ or -, and the name of an entry of the folder
# the command runs in.
ENV_LITERAL = re.compile(r"[A-Za-z0-9_.:@+,=-]*")
# The ~ forms the guard expands as the shell does: ~ alone and ~/PATH. bash leaves ~"/x", ~\/x and ~''/x as they are,
# and ~+, ~- and ~N name folders the guard does not know.
HOME_TILDE = re.compile(r"~(?:/|$)")
CONTROL = frozenset("if then else elif fi for select while until do done case esac function coproc [[ ]] eval exec "
                    "xargs".split())
# The shells whose -c (or any option, or no script) cannot tell; another one is not on the list.
SHELLS = frozenset("sh bash dash zsh ksh mksh ash rbash yash posh csh tcsh fish pwsh nu".split())
READERS = frozenset("cat head tail wc ls cut tr diff basename dirname realpath pwd date echo printf true grep sort "
                    "uniq".split())
NUMBER = re.compile(r"[+-]?\d+")
COUNT = re.compile(r"-\d+")
FIND_TESTS = frozenset("-name -iname -path -type -maxdepth -mindepth -newer -mtime -size".split())
SED_NUMBER = re.compile(r"\d+(?:~\d+)?|\$|[+~]\d+")
GIT_EXACT = (("worktree", ("list",)), ("remote", ("-v",)))
GIT_HOOKS = ("pre-commit", "prepare-commit-msg", "commit-msg", "post-commit", "post-index-change",
             "reference-transaction", "pre-auto-gc", "fsmonitor-watchman")
GH_READS = frozenset((("pr", "view"), ("pr", "list"), ("pr", "checks"), ("run", "list"), ("run", "view")))
PACK_TOOLS = ("team", "log", "federate")


@dataclass(frozen=True)
class Listed:
    """The options of one command on the lead's list. flags take no value; numbers take a number and values a word
    that does not start with -- (see plain_value): a short one (-k) the rest of its word or the next word, a long one
    (--json) the next word or what follows =, a long one listed with = (--format=) only what follows =. letters may
    share one word (ls -la), count allows -N (head -3), ends lets -- end the options. Any other word that starts with
    - is not on the list, and after that -- any other word that starts with --."""
    flags: frozenset = frozenset()
    numbers: frozenset = frozenset()
    values: frozenset = frozenset()
    letters: str = ""
    count: bool = False
    ends: bool = False


def listed(flags: str = "", numbers: str = "", values: str = "", letters: str = "", count: bool = False,
           ends: bool = False) -> Listed:
    return Listed(*(frozenset(text.split()) for text in (flags, numbers, values)), letters, count, ends)


# The options of brief T-0188-fix4's table, per command; echo, printf, true, pwd, date, basename, dirname and realpath
# take none.
NO_OPTIONS = listed()
READER_OPTIONS = {
    "cat": listed("-n -A -v"),
    "head": listed(numbers="-n -c", count=True),
    "tail": listed(numbers="-n -c", count=True),
    "wc": listed("-l -w -c -m"),
    "ls": listed(letters="laAdhtr1R"),
    "cut": listed(values="-d -f -c"),
    "tr": listed("-d -s"),
    "diff": listed("-u -r -q --brief -N", numbers="-U"),
    "grep": listed(numbers="-A -B -C -m", values="-e --include= --exclude=", letters="nclLivEFrRhHowx"),
    "sort": listed("-n -r -u", values="-k -t"),
    "uniq": listed("-c -d -u"),
}
SED_OPTIONS = listed(letters="nE", ends=True)
GIT_REVISIONS = listed("--verify -q --short --abbrev-ref --is-ancestor --count")
GIT_OPTIONS = {
    "status": listed("-s --short -b --porcelain"),
    "log": listed("--oneline --stat --name-only --graph --all -p", numbers="-n", values="--format= --pretty=",
                  count=True, ends=True),
    "show": listed("--stat --name-only -s", values="--format="),
    "diff": listed("--stat --name-only --name-status --cached", numbers="-U", ends=True),
    "rev-parse": GIT_REVISIONS, "merge-base": GIT_REVISIONS, "rev-list": GIT_REVISIONS,
    "ls-files": NO_OPTIONS, "for-each-ref": NO_OPTIONS, "cat-file": listed("-p"), "branch": listed("--list -v -a"),
}
GH_OPTIONS = listed("--log-failed", numbers="-L --limit", values="-R --json --jq --state --commit")
GIT_COMMIT_OPTIONS = listed("-q", values="-m")
# The listing of what git commit records, so that no config hides or renames an entry (review T-0188.r10):
# submodule-ignore settings (diff.ignoreSubmodules, submodule.NAME.ignore) hide a gitlink, diff.renames keeps only a
# rename's new name, diff.relative cuts paths outside the folder, a replace ref (core.useReplaceRefs) swaps HEAD's
# tree, and -z gives each name as it is. Without HEAD, git diff --cached compares with the empty tree.
STAGED_CONFIG = ("--no-replace-objects", "-c", "diff.ignoreSubmodules=none", "-c", "core.quotePath=false")
STAGED = ("--no-renames", "--ignore-submodules=none", "-z", "--no-relative")
GITLINK = b"160000"


class CannotTell(Exception):
    """A construct this module does not parse; the line is refused."""


class NotListed(Exception):
    """A command or option that matches no shape of the lead's list; the line is refused."""


@dataclass
class Word:
    """One shell word: its text after quote removal, its source, and whether the shell computes part of it: dyn a
    variable, substitution or $"..."; glob an unquoted *, ? or [, a brace expansion, or a ~ after the = of a NAME=
    word or a : after it (bash: x=~/m.md); tilde a leading ~. brace: 1 after an unquoted {, 2 once an unquoted , or
    . follows it."""
    text: str = ""
    dyn: bool = False
    glob: bool = False
    tilde: bool = False
    quoted: bool = False
    subs: list = field(default_factory=list)
    raw: str = ""
    brace: int = 0

    def unquoted(self, ch: str) -> None:
        """An unquoted character. A brace expansion needs an unquoted {, then , or .., then an unquoted }: a quoted
        or escaped } does not close it ({"a}",b} is a} b, review T-0188.r12)."""
        if ch == "{":
            self.brace = self.brace or 1
        elif ch in ",." and self.brace:
            self.brace = 2
        tilde = ch == "~" and self.text[-1:] in ("=", ":") and bool(ASSIGNMENT.match(self.text))
        self.glob = self.glob or ch in "*?[" or (ch == "}" and self.brace == 2) or tilde
        self.text += ch


@dataclass
class Simple:
    """A simple command: its words, its redirections (operator, target word, descriptor digits) and the operator
    after it."""
    words: list = field(default_factory=list)
    redirects: list = field(default_factory=list)
    after: str = ""


# ---------------------------------------------------------------------------------------------------- shell words
def quote_end(s: str, i: int) -> int:
    """The index of the quote that closes the one at i, or len(s)."""
    if s[i] == "'":
        end = s.find("'", i + 1)
        return len(s) if end < 0 else end
    i += 1
    while i < len(s) and s[i] != '"':
        i += 2 if s[i] == "\\" else 1
    return min(i, len(s))


def closing(s: str, i: int, opening: str = "(", close: str = ")") -> int:
    """The index of the bracket that closes an opened one, scanning from i, or len(s)."""
    depth = 1
    while i < len(s):
        ch = s[i]
        if ch == "\\":
            i += 1
        elif ch in "'\"":
            i = quote_end(s, i)
        elif ch == opening:
            depth += 1
        elif ch == close:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return len(s)


def expansions(text: str) -> list:
    """The command substitutions ($(...) and `...`) in a text the shell expands, such as an unquoted heredoc body."""
    found, i = [], 0
    while i < len(text):
        if text.startswith("$(", i):
            end = closing(text, i + 2)
            found.append(text[i + 2:end])
            i = end
        elif text[i] == "`":
            end = text.find("`", i + 1)
            end = len(text) if end < 0 else end
            found.append(text[i + 1:end])
            i = end
        elif text[i] == "\\":
            i += 1
        i += 1
    return found


def delimiter(target: Word) -> Word:
    """A heredoc's delimiter word: bash ends the body at the word as written, so one with a $, a ` or a part the
    shell computes cannot tell."""
    if target.dyn or "$" in target.raw or "`" in target.raw:
        raise CannotTell(f"the heredoc delimiter {target.raw} has a $, a ` or a part the shell computes; bash ends "
                         "the body at the word as written")
    return target


class Lexer:
    """Splits a command line into words, operators and redirections, as far as a POSIX shell or bash would."""

    def __init__(self, text: str) -> None:
        self.s, self.i, self.tokens, self.heredocs = text, 0, [], []

    def run(self) -> list:
        while self.i < len(self.s):
            self.step()
        self.bodies()
        return self.tokens

    def step(self) -> None:
        s, i = self.s, self.i
        match = REDIRECT.match(s, i)
        if s[i] in " \t":
            self.i += 1
        elif s.startswith("\\\n", i):
            self.i += 2
        elif s[i] == "#":
            end = s.find("\n", i)
            self.i = len(s) if end < 0 else end
        elif s[i] == "\n":
            self.tokens.append(("op", "\n"))
            self.i += 1
            self.bodies()
        elif match and not s.startswith(("<(", ">("), i):
            self.redirect(match.group(1) or match.group(2), match.end())
        else:
            op = next((o for o in OPERATORS if s.startswith(o, i)), None)
            self.tokens.append(("op", op) if op else ("word", self.word()))
            self.i += len(op) if op else 0

    def redirect(self, op: str, end: int) -> None:
        fd = self.s[self.i:end - len(op)] or self.named_fd()
        self.i = end
        while self.i < len(self.s) and self.s[self.i] in " \t":
            self.i += 1
        target = self.word()
        if not (target.text or target.dyn):
            raise CannotTell(f"a {op} without a target")
        self.tokens.append(("redirect", op, target, fd))
        if op in HEREDOCS:
            self.heredocs.append((delimiter(target), op == "<<-"))

    def named_fd(self) -> str:
        """bash's {NAME}> form: a {NAME} word right before the operator names a new descriptor; it is no argument."""
        last = self.tokens[-1] if self.tokens else ("op", "")
        if last[0] != "word" or self.s[self.i - 1:self.i] != "}" or not NAMED_FD.fullmatch(last[1].raw):
            return ""
        self.tokens.pop()
        return last[1].raw

    def bodies(self) -> None:
        """Skip the heredoc bodies that follow a line; an unquoted body still expands $(...), `...`, $[...] and
        ${...}, so those count as the target's substitutions or refuse, and bash joins its line that ends in \\ with
        the next one, the end line too, so such a line refuses."""
        for target, strip in self.heredocs:
            while self.i < len(self.s):
                end = self.s.find("\n", self.i)
                end = len(self.s) if end < 0 else end
                line, self.i = self.s[self.i:end], end + 1
                if (line.lstrip("\t") if strip else line) == target.text:
                    break
                if not target.quoted and BODY_EXPANSION.search(line):
                    raise CannotTell("a heredoc with a $[...] or ${...} expansion")
                if not target.quoted and line.endswith("\\"):
                    raise CannotTell("a heredoc body line that ends in \\, which bash joins with the next one")
                if not target.quoted:
                    target.subs.extend(expansions(line))
        self.heredocs = []

    def word(self) -> Word:
        start, w = self.i, Word(tilde=self.s.startswith("~", self.i))
        while self.i < len(self.s):
            ch = self.s[self.i]
            if ch in "<>" and self.s.startswith("(", self.i + 1):
                self.substitution(w, self.i + 2)
            elif ch in " \t\n;&|()<>":
                break
            elif ch == "\\":
                w.text += self.s[self.i + 1:self.i + 2].replace("\n", "")
                self.i += 2
            elif ch in "'\"":
                self.quoted(w)
            elif ch in "$`":
                self.dollar(w, False)
            else:
                self.plain(w, ch)
        w.raw = self.s[start:self.i]
        return w

    def plain(self, w: Word, ch: str) -> None:
        w.unquoted(ch)
        self.i += 1

    def quoted(self, w: Word) -> None:
        w.quoted = True
        if self.s[self.i] == "'":
            end = quote_end(self.s, self.i)
            if end >= len(self.s):
                raise CannotTell("an unclosed quote")
            w.text += self.s[self.i + 1:end]
            self.i = end + 1
            return
        self.i += 1
        while self.i < len(self.s) and self.s[self.i] != '"':
            ch = self.s[self.i]
            if ch == "\\" and self.s[self.i + 1:self.i + 2] in ('$', '`', '"', "\\", "\n"):
                w.text += self.s[self.i + 1].replace("\n", "")
                self.i += 2
            elif ch in "$`":
                self.dollar(w, True)
            else:
                w.text += ch
                self.i += 1
        if self.i >= len(self.s):
            raise CannotTell("an unclosed quote")
        self.i += 1

    def substitution(self, w: Word, start: int) -> None:
        end = closing(self.s, start)
        if end >= len(self.s):
            raise CannotTell("an unclosed substitution")
        body = self.s[start:end]
        if self.s.startswith("$((", start - 2) and body.startswith("(") and body.endswith(")"):
            raise CannotTell("an arithmetic expansion, which can assign a variable")
        w.subs.append(body)
        w.dyn = True
        self.i = end + 1

    def dollar(self, w: Word, in_quotes: bool) -> None:
        s, i = self.s, self.i
        if s[i] == "`":
            end = s.find("`", i + 1)
            if end < 0:
                raise CannotTell("an unclosed `")
            w.subs.append(s[i + 1:end])
            w.dyn, self.i = True, end + 1
        elif s.startswith("$(", i):
            self.substitution(w, i + 2)
        elif s.startswith("${", i):
            end = closing(s, i + 2, "{", "}")
            if not PLAIN_BRACE.fullmatch(s[i + 2:end]):
                raise CannotTell("a ${...} expansion other than ${NAME}")
            w.dyn, self.i = True, end + 1
        elif s.startswith("$[", i):
            raise CannotTell("an arithmetic expansion, which can assign a variable")
        elif s.startswith("$'", i) and not in_quotes:
            end = quote_end(s, i + 1)
            if end >= len(s):
                raise CannotTell("an unclosed quote")
            if "\\" in s[i + 2:end]:
                raise CannotTell("a $'...' with a \\: in bash a \\' does not end it")
            w.dyn, self.i = True, end + 1
        elif s.startswith('$"', i) and not in_quotes:
            w.dyn, self.i = True, i + 1  # bash translates it and drops the $, sh keeps the $: the quote follows
        elif VARIABLE.match(s, i):
            w.dyn, self.i = True, VARIABLE.match(s, i).end()
        else:
            w.text += "$"
            self.i += 1


def simple_commands(tokens: list) -> list:
    """The simple commands of a token list, with "(" and ")" kept as items: a subshell's cd stays inside it."""
    items, current = [], Simple()
    for token in tokens:
        if token[0] == "word":
            current.words.append(token[1])
        elif token[0] == "redirect":
            current.redirects.append((token[1], token[2], token[3]))
        elif token[1] in UNSPLIT:
            raise CannotTell(f"{token[1]}")
        elif token[1] == "(" and current.words:
            raise CannotTell("a function or an array")
        elif token[1] in ("(", ")"):
            items += bracketed(current, token[1])
            current = Simple()
        else:
            current.after = token[1]
            items.append(current)
            current = Simple()
    items.append(current)
    return items


def bracketed(current: Simple, bracket: str) -> list:
    """The items at a ( or ): the simple command before it (when it has words or redirections), then the bracket."""
    return [current, bracket] if current.words or current.redirects else [bracket]


def cd_folder(word: Word) -> None:
    """A cd folder bash does not look up in CDPATH: absolute (also after ~), ./X or ../X (review T-0188.r20)."""
    text = os.path.expanduser(word.text) if word.tilde else word.text
    if not (os.path.isabs(text) or text.startswith(("./", "../"))):
        raise CannotTell(f"cd {word.text}: bash looks a folder that starts with neither /, ./ nor ../ up in CDPATH")


def entered(folders: list, after: str) -> bool:
    """Whether a cd that bash runs for sure, with ; or a newline (or nothing) after it, has entered its folders: they
    exist and may be entered (a cd that fails leaves the next command in the folder before it)."""
    return after in (";", "\n", "") and all(os.path.isdir(p) and os.access(p, os.X_OK) for p in folders)


class Position:
    """Where a simple command stands in its line, for a cd (brief T-0188-fix13, review T-0188.r21): bash skips a cd
    after || in its and-or list and runs one in a pipeline or in ( ) in a subshell, so the commands after it run in the
    folder before it. A cd after && is skipped when the command before it fails: only the commands that && joins to it
    run after it for sure, not those after ; or a newline. A group ( ) or { } may be skipped as a whole: its and-or
    state is kept apart."""

    def __init__(self) -> None:
        self.saved, self.ored, self.piped, self.anded = [], False, False, False

    def open(self) -> None:
        self.saved.append((self.ored, self.piped, self.anded))

    def close(self) -> None:
        if self.saved:
            self.ored, self.piped, self.anded = self.saved.pop()

    def sure(self, item: Simple) -> bool:
        """Takes the item's leading { and } words; then whether it is a plain cd (its first word) that bash runs in
        this shell for sure before the commands after it: at the start of the line or after ; or && with no || before
        it, not in a pipeline (no | before or after it) and not inside ( ) or { }; after && only when && follows it."""
        for word in item.words:
            if word.raw not in ("{", "}"):
                break
            if word.raw == "{":
                self.open()
            else:
                self.close()
        plain = bool(item.words) and item.words[0].text == "cd"
        joined = item.after == "&&" or not self.anded
        return plain and joined and not (self.saved or self.ored or self.piped) and item.after != "|"

    def follow(self, item: Simple) -> None:
        """The operator after item: ; and a newline start a new and-or list, || marks it, && marks the next command
        (bash may skip it), | starts a pipeline. A newline after an empty item (after &&, || or |, or an empty line)
        changes nothing."""
        if item.after == "\n" and not (item.words or item.redirects):
            return
        self.ored = item.after == "||" or (self.ored and item.after in ("&&", "|"))
        self.piped = item.after == "|"
        self.anded = item.after == "&&"


def command_words(words: list) -> tuple:
    """The leading NAME=VALUE words, and the words from the program on ({ and } of a group skipped). bash takes a
    word as { or } or as NAME=VALUE only as written: a quoted or escaped one (\\{, "X=1") is the program."""
    i = 0
    while i < len(words) and words[i].raw in ("{", "}"):
        i += 1
    k = i
    while k < len(words) and ASSIGNMENT.match(words[k].raw):
        k += 1
    return words[i:k], words[k:]


def command_parts(item: Simple) -> tuple:
    """The NAME=VALUE words (export's too) and the words from the program on; a control word, or a $(...), `...` or
    <(...) outside a NAME=VALUE word, cannot tell."""
    assigns, words = command_words(item.words)
    if words and words[0].text in CONTROL:
        raise CannotTell(words[0].text)
    if words and words[0].text == "export":
        if len(words) < 2:
            raise NotListed("export without NAME=VALUE")
        assigns, words = assigns + words[1:], []
    for word in words + [target for _, target, _ in item.redirects]:
        if word.subs:
            raise CannotTell(f"{word.raw}: a $(...), `...` or <(...) runs a command")
    return assigns, words


def literals(words: list, what: str) -> None:
    """Words whose expansion cannot add an option: no variable or substitution, and a glob only after a fixed first
    character (a * or ? up front could match a file named like an option)."""
    for word in words:
        if word.dyn or (word.glob and word.text[:1] in "*?[{-"):
            raise CannotTell(f"{what} {word.raw}: the shell computes it")


def shell_code(words: list) -> None:
    """bash -c, sh -c and any shell of SHELLS, by its name or its path, given an option (-c, -lc, -e -c, ...) or no
    script: it runs code from a string or from stdin that this module does not parse, so the line cannot tell. A
    hook's argv is checked as a line, so ["bash", "-lc", LINE] cannot tell too."""
    if os.path.basename(words[0].text) in SHELLS and (len(words) < 2 or words[1].text.startswith(("-", "+"))):
        shown = " ".join(word.raw for word in words[:3])
        raise CannotTell(f"{shown}: a shell given its code with -c or on stdin runs code the guard does not see")


def exact(word: Word, what: str) -> None:
    """A word that must name one path as written: no variable, substitution, glob or brace expansion, and a ~ only
    as HOME_TILDE (~ or ~/PATH, which the guard expands as the shell does)."""
    if word.dyn or word.glob or (word.tilde and not HOME_TILDE.match(word.raw)):
        raise CannotTell(f"{what} {word.raw}: the shell computes it")


def fixed(words: list, what: str) -> None:
    """The words of a command with an operand that writes (uniq's OUTPUT, sed's SCRIPT): each one exact, so the
    operands the guard counts and reads are the ones the command gets (review T-0188.r12: uniq src/* overwrote
    src/b.txt)."""
    for word in words:
        exact(word, what)


def listed_options(words: list, spec: Listed, name: str) -> tuple:
    """getopt's reading of words against the command's list: the options seen and the operands (an option may follow
    an operand). Every word that starts with - (but - alone) is an option, and one not on the list raises NotListed,
    -- too unless the list lets it end the options. After that -- a word with one - is an operand, but every word that
    starts with -- is still checked against the list (git log -- --output=x is refused)."""
    seen, operands, i, dash = set(), [], 0, "-"
    while i < len(words):
        text = words[i].text
        if text == "--" and spec.ends and dash == "-":
            dash, i = "--", i + 1
        elif text.startswith(dash) and text != "-":
            i = option_end(words, i, spec, name, seen)
        else:
            operands.append(words[i])
            i += 1
    return seen, operands


def option_end(words: list, i: int, spec: Listed, name: str, seen: set) -> int:
    """The index after the option at words[i]: a flag, -N, a word of letters, or an option with its value."""
    text = words[i].text
    if text in spec.flags or (spec.count and COUNT.fullmatch(text)):
        seen.add(text)
        return i + 1
    if not text.startswith("--") and set(text[1:]) <= set(spec.letters):
        seen.update("-" + letter for letter in text[1:])
        return i + 1
    return value_end(words, i, spec, name, seen)


def value_end(words: list, i: int, spec: Listed, name: str, seen: set) -> int:
    """An option with a value: -kVALUE or -k VALUE, --json=VALUE or --json VALUE, --format=VALUE; a number where the
    list says so. Anything else raises NotListed (a long option is named without its value)."""
    text = words[i].text
    option, eq, value = text.partition("=") if text.startswith("--") else (text[:2], "", text[2:])
    keys = (option + eq, option) if eq else (option,)
    key = next((k for k in keys if k in spec.numbers or k in spec.values), None)
    if key is None:
        raise NotListed(f"{name} {option if eq else text}")
    own_word = not (eq or value)
    if own_word:
        if i + 1 >= len(words):
            raise NotListed(f"{name} {option} without its value")
        i, value = i + 1, words[i + 1].text
    if not (NUMBER.fullmatch(value) if key in spec.numbers else plain_value(value)):
        raise NotListed(f"{name} {option} {value}")
    seen.add(option)
    return i + 1


def plain_value(value: str) -> bool:
    """A value never starts with --: such a word the list does not name is refused even where the option before it
    takes it as its value (gh pr view --json --web). One - is allowed (grep -e -needle): getopt and gh's flag parser
    take the word after an option that needs a value as that value, whatever it starts with."""
    return not value.startswith("--")


def find_tests(tests: list) -> None:
    """find's words after its paths: only the tests of FIND_TESTS, each with its value (see plain_value), and
    -print."""
    k = 0
    while k < len(tests):
        test = tests[k].text
        if test != "-print" and (test not in FIND_TESTS or k + 1 >= len(tests) or not plain_value(tests[k + 1].text)):
            raise NotListed(f"find {test}")
        k += 1 if test == "-print" else 2


def blanks(s: str, i: int) -> int:
    return len(s) - len(s[i:].lstrip(" \t"))


def sed_address(s: str, i: int) -> int:
    """The index after the address at i (N, first~step, $, +N, ~N, /regex/ or \\cregexc, with I and M), or i."""
    number = SED_NUMBER.match(s, i)
    if number:
        return number.end()
    if s.startswith("/", i):
        delim, i = "/", i + 1
    elif s.startswith("\\", i) and i + 1 < len(s) and s[i + 1] not in "\n\\":
        delim, i = s[i + 1], i + 2
    else:
        return i
    while i < len(s) and s[i] != delim:
        if s[i] == "\n":
            raise NotListed("sed: a regex across lines")
        i += 2 if s[i] == "\\" else 1
    if i >= len(s):
        raise NotListed("sed: an unclosed regex")
    i += 1
    while i < len(s) and s[i] in "IM":
        i += 1
    return i


def sed_script_ok(script: str) -> bool:
    """Whether a sed script holds only addresses and the commands p and = (no w, W, e, r, R, s or any other)."""
    i = 0
    while i < len(script):
        if script[i] in " \t\n;":
            i += 1
            continue
        i = blanks(script, sed_address(script, i))
        if script.startswith(",", i):
            i = blanks(script, sed_address(script, blanks(script, i + 1)))
        if script.startswith("!", i):
            i = blanks(script, i + 1)
        if i >= len(script) or script[i] not in "p=":
            return False
        i = blanks(script, i + 1)
        if i < len(script) and script[i] not in ";\n":
            return False
    return True


# ---------------------------------------------------------------------------------------------------- globs
def glob_char(pattern: str, i: int) -> tuple:
    ch = pattern[i]
    end = pattern.find("]", i + 2)
    if ch == "*":
        return "[^/]*", i + 1
    if ch == "?":
        return "[^/]", i + 1
    if ch == "[" and end > 0:
        body = pattern[i + 1:end]
        body = "^" + body[1:] if body.startswith("!") else body
        return "[" + body.replace("\\", "\\\\") + "]", end + 1
    return re.escape(ch), i + 1


def translate(pattern: str) -> str:
    """A glob as a regular expression: ** any depth (a trailing /** also the folder itself), * and ? within a name."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("/**", i) and i + 3 == len(pattern):
            out.append("(?:/.*)?")
            i += 3
        elif pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        else:
            piece, i = glob_char(pattern, i)
            out.append(piece)
    return "".join(out)


def glob_pattern(glob: str, project: Path):
    """One lead_write glob, made absolute and with its literal folders resolved; None without a home for ~."""
    text = os.path.expanduser(glob)
    if text.startswith("~"):
        return None
    parts = (Path(text) if os.path.isabs(text) else project / text).parts
    cut = next((k for k, part in enumerate(parts) if any(c in part for c in "*?[")), len(parts))
    head = Path(*parts[:cut]).resolve().as_posix().rstrip("/")
    rest = "/".join(parts[cut:])
    return re.compile(translate(head + "/" + rest if rest else head))


def hook_paths(tool_input: dict) -> list:
    """The paths a tool call names: file_path, path, notebook_path, and every file of a patch (input or patch)."""
    paths = [tool_input[key] for key in PATH_KEYS if isinstance(tool_input.get(key), str)]
    for key in ("input", "patch"):
        if isinstance(tool_input.get(key), str):
            paths += [target.strip() for target in PATCH_TARGET.findall(tool_input[key])]
    return paths


def hard_linked(path: Path) -> bool:
    """A file with more than one name: a write through a name in lead_write changes the others too."""
    try:
        return path.is_file() and path.stat().st_nlink > 1
    except OSError:
        return False


def inside(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents


def env_names(text: str) -> set:
    """The names of lead_env (split at commas and blanks). One that ENV_RUNS matches is ignored, with one stderr line
    that names it."""
    names = dict.fromkeys(name for name in re.split(r"[,\s]+", text) if name)
    for name in (name for name in names if ENV_RUNS.fullmatch(name)):
        print(f"guard: lead_env: {name} is ignored: it redirects where a program reads its configuration or what it "
              "runs (docs/DESIGN.md, section 10)", file=sys.stderr)
    return {name for name in names if not ENV_RUNS.fullmatch(name)}


def env_value_ok(value: str, cwds: list) -> bool:
    """A lead_env literal that cannot name a path: only ENV_LITERAL's characters (no /), not . or .., no leading ~ or
    -, and no file, folder or link of that name in a folder the command may run in (lexists: a link is not
    followed). The empty value names nothing."""
    if not ENV_LITERAL.fullmatch(value) or value in (".", "..") or value.startswith(("~", "-")):
        return False
    return not value or not any(os.path.lexists(os.path.join(cwd, value)) for cwd in cwds)


def env_value(word: Word, start: int, cwds: list) -> bool:
    """The VALUE of a NAME=VALUE word (from start on): exactly $(pass show ENTRY), or a literal of env_value_ok after
    quote removal. Any other substitution, and any other value (a variable), is False: not listed."""
    if word.subs:
        return bool(PASS_VALUE.fullmatch(word.raw))
    return not word.dyn and env_value_ok(word.text[start:], cwds)


# ---------------------------------------------------------------------------------------------------- the boundary
@dataclass
class Boundary:
    """The lead's write boundary: guard passes its folders, settings, and its base rules for commands and for the
    paths of reading tools."""
    project: Path
    pack: Path
    team: Path
    roots: list
    secrets: list
    lead_write: str
    base_command: object
    base_path: object
    lead_run: str = ""
    lead_env: str = ""

    def __post_init__(self) -> None:
        self.patterns = self.globs(self.lead_write)
        self.run_patterns = self.globs(self.lead_run)
        self.env_names = env_names(self.lead_env)
        home = os.path.expanduser("~")
        bases = [self.project] + ([] if home.startswith("~") else [Path(home)])
        self.git_configs = [] if home.startswith("~") else [Path(home) / ".gitconfig",
                                                             Path(home) / ".config" / "git" / "config"]
        teams = {self.team, self.pack / "team", self.project / ".agents" / "team"}
        self.guarded = [(t.resolve(), ("config.ini", "guard-deny.txt")) for t in teams]
        self.guarded += [(b.resolve(), ("guard*",)) for b in (self.pack / "bin", self.project / ".agents" / "bin")]
        self.guarded += [((base / folder).resolve(), names) for base in bases for folder, names in SETTINGS]
        self.tools = {(b / name).resolve() for b in (self.pack / "bin", self.project / ".agents" / "bin")
                      for name in PACK_TOOLS}

    def globs(self, text: str) -> list:
        return [p for p in (glob_pattern(g, self.project) for g in re.split(r"[,\s]+", text) if g) if p]

    def check(self, kind: str, value) -> str | None:
        """kind: path (guard check-path), command (guard check-command) or hook (a PreToolUse payload)."""
        try:
            if kind == "path":
                return self.check_path(value)
            if kind == "command":
                return self.check_command(value, Path.cwd())
            return self.check_hook(value)
        except CannotTell as why:
            return f"lead mode: {CANNOT_TELL} ({why}); {BRIEF}"
        except NotListed as why:
            return f"lead mode: {why} is {NOT_LISTED}"

    # ------------------------------------------------------------------------------------------------ paths
    def display(self, path: Path) -> str:
        return path.relative_to(self.project).as_posix() if self.project in path.parents else str(path)

    def writable(self, path: Path) -> bool:
        text = path.as_posix()
        return any(p.fullmatch(text) for p in self.patterns) or any(path == r or r in path.parents for r in self.roots)

    def protected(self, path: Path) -> bool:
        return any(path.parent == folder and any(fnmatch.fnmatchcase(path.name, name) for name in names)
                   for folder, names in self.guarded)

    def why_refused(self, path: Path) -> str | None:
        if any(path == store or store in path.parents for store in self.secrets):
            return "is in the secret store"
        if ENV_FILE.fullmatch(path.name):
            return "is an .env file"
        if self.protected(path):
            return "switches the guard or the CLI, which only the human changes"
        if path.suffix.lower() in CODE_SUFFIXES:
            return "is code"
        if not self.writable(path):
            return "is outside lead_write"
        return "has another hard link, which may be code" if hard_linked(path) else None

    def place_reason(self, path: Path, shown: str, follow: bool = True) -> str | None:
        """Why a write to path (absolute, not yet resolved) is refused; follow=False: the name itself (mv replaces a
        symlink, it does not write through it)."""
        resolved = path.resolve() if follow else path.parent.resolve() / path.name
        where = self.display(resolved)
        note = "" if where == os.path.normpath(shown) else f" (resolves to {where})"
        why = self.why_refused(resolved)
        return None if why is None else f"lead mode: {shown}{note} {why}; {BRIEF}"

    def check_path(self, raw: str) -> str | None:
        path = Path(os.path.expanduser(raw))
        return self.place_reason(path if path.is_absolute() else self.project / path, raw)

    def places(self, word: Word, cwds: list) -> list:
        """Where a word names a file, once per possible working folder; None where that folder is unknown."""
        text = os.path.expanduser(word.text) if word.tilde else word.text
        if word.tilde and (text.startswith("~") or not HOME_TILDE.match(word.raw)):
            return [None]
        if os.path.isabs(text):
            return [Path(text)]
        return [None if cwd is None else cwd / text for cwd in cwds]

    def known_places(self, word: Word, cwds: list) -> list:
        places = self.places(word, cwds)
        if None in places:
            raise CannotTell(f"{word.text}: ~ does not expand or a cd it cannot follow")
        return places

    def target_reason(self, word: Word, cwds: list, follow: bool = True) -> str | None:
        """A shell write target: /dev/null, or a lead_write or write_roots path outside any .git folder."""
        exact(word, "the target")
        if word.text == "/dev/null":
            return None
        for place in self.known_places(word, cwds):
            resolved = place.resolve() if follow else place.parent.resolve() / place.name
            if ".git" in resolved.parts:
                return f"lead mode: {word.text} is in a .git folder, git's own data; {BRIEF}"
            why = self.place_reason(place, word.text, follow)
            if why:
                return why
        return None

    # ------------------------------------------------------------------------------------------------ hook
    def check_hook(self, payload) -> str | None:
        if not isinstance(payload, dict):
            return "unreadable hook payload (fail closed)"
        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return None
        cwd = payload.get("cwd")
        cwd = Path(cwd) if isinstance(cwd, str) and os.path.isabs(cwd) else None
        why = self.hook_command(tool_input.get("command"), cwd or Path.cwd())
        if why:
            return why
        paths = hook_paths(tool_input)
        if payload.get("tool_name") in READ_TOOLS:
            return next((why for why in map(self.base_path, paths) if why), None)
        return next((why for why in (self.tool_path_reason(path, cwd) for path in paths) if why), None)

    def tool_path_reason(self, raw: str, cwd) -> str | None:
        """A file tool's path, a relative one taken from the tool's cwd (the hook payload's); none: cannot tell."""
        path = Path(os.path.expanduser(raw))
        if path.is_absolute():
            return self.place_reason(path, raw)
        if raw.startswith("~"):
            raise CannotTell(f"{raw}: ~ does not expand here")
        if cwd is None:
            raise CannotTell(f"{raw} is relative and the hook payload has no cwd")
        return self.place_reason(cwd / path, raw)

    def hook_command(self, command, cwd: Path) -> str | None:
        """A shell tool's command: a string is a command line; an argv (Codex CLI's form) is the line shlex.join writes
        for it, never unwrapped, so ["bash", "-lc", LINE] cannot tell like bash -lc LINE (review T-0188.r5)."""
        if isinstance(command, str):
            return self.check_command(command, cwd)
        if not isinstance(command, list):
            return None
        argv = [str(part) for part in command]
        return self.base_command(" ".join(argv)) or self.check_line(shlex.join(argv), [cwd])

    # ------------------------------------------------------------------------------------------------ commands
    def check_command(self, command: str, cwd: Path) -> str | None:
        return self.base_command(command) or self.check_line(command, [cwd])

    def check_line(self, text: str, cwds: list) -> str | None:
        """Every simple command of a command line, in order, following cd (a subshell's cd stays inside it)."""
        stack, fallback, where = [], [], Position()
        for item in simple_commands(Lexer(text).run()):
            if item == "(":
                stack.append((cwds, fallback))
                where.open()
            elif item == ")":
                cwds, fallback = stack.pop() if stack else (cwds, fallback)
                where.close()
            else:
                why = self.check_simple(item, cwds)
                if why:
                    return why
                cwds, fallback = self.next_cwds(item, cwds, fallback, where.sure(item))
                where.follow(item)
        return None

    def next_cwds(self, item: Simple, cwds: list, fallback: list, sure: bool) -> tuple:
        """The folders the next command may run in, which every reader of the list checks (brief T-0188-fix13). A
        cd that bash runs for sure (Position.sure) narrows them to its folder when && follows it, or ; or a newline
        and its folder exists (a cd that fails or is skipped leaves the next command in the folder before it); any
        other cd adds its folder to the old ones. After ;, || or a newline the folders before a cd X && ... come back
        too."""
        _, words = command_words(item.words)
        if words and words[0].text == "cd":
            new = list(dict.fromkeys(p.resolve() for p in self.known_places(words[1], cwds)))
            if sure and item.after == "&&":
                return new, fallback + cwds
            cwds = new if sure and entered(new, item.after) else cwds + new
        if item.after in ("&&", "|"):
            return list(dict.fromkeys(cwds)), fallback
        return list(dict.fromkeys(cwds + fallback)), []

    def check_simple(self, item: Simple, cwds: list) -> str | None:
        """A simple command: its form, its redirections, its NAME=VALUE words, then its shape."""
        assigns, words = command_parts(item)
        why = self.redirects_reason(item, cwds)
        if why or not (words or assigns):
            return why
        for word in assigns:
            self.assignment(word, cwds)
        if not words:
            return None
        try:
            return self.shape_reason(words, cwds)
        except NotListed as why:
            if any(op in HEREDOCS for op, _, _ in item.redirects):
                raise CannotTell(f"a heredoc to {why}") from None
            raise

    def redirects_reason(self, item: Simple, cwds: list) -> str | None:
        """The listed redirections only: > and >> (no descriptor, or 1) to a write target, 2>&1 (stderr to stdout),
        2>/dev/null, and < and heredocs (no descriptor) from any file. Any other operator or descriptor (2>FILE,
        3>FILE, 9>/dev/null, >&N, 1>&2, >|, <>, &>, &>>, 3<FILE, {NAME}>FILE) and the here-string <<< are
        refused."""
        for op, target, fd in item.redirects:
            if op in (">", ">>") and fd in ("", "1"):
                why = self.target_reason(target, cwds)
            elif (fd, op, target.text) in DESCRIPTOR_FORMS and not target.dyn:
                why = None
            elif fd == "" and (op == "<" or op in HEREDOCS):
                why = None
            else:
                why = f"lead mode: {fd}{op} {target.raw} is not on the lead's list: {BRIEF}"
            if why:
                return why
        return None

    def assignment(self, word: Word, cwds: list) -> None:
        """NAME=VALUE: NAME in lead_env (env_names has none of ENV_RUNS), VALUE of env_value. NAME+= adds to a value
        the guard does not see: not listed, as is any other value, and the refusal does not repeat it."""
        match = ASSIGNMENT.match(word.text)
        if not match:
            raise NotListed(f"export {word.text}")
        name = match.group(1)
        if name not in self.env_names:
            raise NotListed(f"the variable {name} (lead_env)")
        if match.group(0).endswith("+=") or not env_value(word, match.end(), cwds):
            raise NotListed(f"the value of {name} (lead_env: $(pass show ENTRY) or a short literal that cannot "
                            "name a path)")

    # ------------------------------------------------------------------------------------------------ shapes
    def shapes(self) -> dict:
        table = {"sed": self.sed_reason, "find": self.find_reason, "cd": self.cd_reason, "mkdir": self.mkdir_reason,
                 "cp": self.copy_reason, "mv": self.copy_reason, "git": self.git_reason, "gh": self.gh_reason,
                 "uniq": self.uniq_reason}
        return {**{name: self.reader_reason for name in READERS}, **table}

    def shape_reason(self, words: list, cwds: list) -> str | None:
        head = words[0]
        if head.dyn or head.glob:
            raise CannotTell("the program is computed by the shell")
        shell_code(words)
        if "/" in head.text:
            return self.tool_reason(head, cwds)
        if head.text in SHELLS or head.text in (".", "source"):
            return self.script_reason(head.text, words[1:], cwds)
        handler = self.shapes().get(head.text)
        if handler is None:
            raise NotListed(head.text)
        return handler(words, cwds)

    def reader_reason(self, words: list, cwds: list) -> None:
        """The readers write only to their output, which the redirect rule checks, and take only the options of
        READER_OPTIONS (the others none, so no printf -v): date only +FORMAT, so it cannot set the clock. Their
        operands may expand (literals); uniq has its own shape (uniq_reason)."""
        name = words[0].text
        literals(words[1:], name)
        _, operands = listed_options(words[1:], READER_OPTIONS.get(name, NO_OPTIONS), name)
        if name == "date" and (len(operands) > 1 or any(not w.text.startswith("+") for w in operands)):
            raise NotListed("date other than date +FORMAT")
        return None

    def uniq_reason(self, words: list, cwds: list) -> None:
        """uniq [-c -d -u] [INPUT]: no output file, no word the shell expands (fixed), and its options before INPUT:
        with POSIXLY_CORRECT in the session's environment, GNU uniq reads uniq x.md -c as INPUT x.md, OUTPUT -c."""
        fixed(words[1:], "uniq")
        _, operands = listed_options(words[1:], READER_OPTIONS["uniq"], "uniq")
        if len(operands) > 1 or (operands and operands[0] is not words[-1]):
            raise NotListed("uniq other than uniq [-c -d -u] [INPUT] (no output file, the options first)")
        return None

    def sed_reason(self, words: list, cwds: list) -> None:
        """sed -n SCRIPT FILE...: no word the shell expands (the SCRIPT could write), a script of sed_script_ok."""
        fixed(words[1:], "sed")
        seen, operands = listed_options(words[1:], SED_OPTIONS, "sed")
        if "-n" not in seen or len(operands) < 2:
            raise NotListed("sed without -n SCRIPT FILE")
        if not sed_script_ok(operands[0].text):
            raise NotListed(f"sed script {operands[0].text!r} (addresses and p or = only)")
        return None

    def find_reason(self, words: list, cwds: list) -> None:
        """find PATH... [tests]: the paths, then only the tests of FIND_TESTS (each with its value) and -print."""
        args = words[1:]
        literals(args, "find")
        k = next((i for i, w in enumerate(args) if w.text.startswith(("-", "(", ")", "!", ","))), len(args))
        if k == 0:
            raise NotListed("find without a PATH")
        find_tests(args[k:])
        return None

    def cd_reason(self, words: list, cwds: list) -> str | None:
        """cd DIR inside the project or a write_root: DIR absolute, ~ or ~/PATH, or ./X or ../X. bash looks any other
        relative DIR up in CDPATH first, which cannot tell (review T-0188.r20)."""
        if len(words) != 2 or words[1].text.startswith("-"):
            raise NotListed("cd without one folder")
        exact(words[1], "cd")
        places = self.known_places(words[1], cwds)
        cd_folder(words[1])
        for place in places:
            target = place.resolve()
            if not (inside(target, self.project) or any(inside(target, root) for root in self.roots)):
                return f"lead mode: cd {words[1].text} leaves the project and the write_roots; {BRIEF}"
        return None

    def mkdir_reason(self, words: list, cwds: list) -> str | None:
        parents = len(words) > 1 and words[1].text == "-p"
        folders = words[2:] if parents else words[1:]
        if not folders or any(w.text.startswith("-") for w in folders):
            raise NotListed("mkdir with an option other than -p")
        for word in folders:
            why = self.target_reason(word, cwds) or (self.parents_reason(word, cwds) if parents else None)
            if why:
                return why
        return None

    def parents_reason(self, word: Word, cwds: list) -> str | None:
        """mkdir -p also makes the missing folders above DIR: each must be a lead_write path too."""
        for place in self.known_places(word, cwds):
            parent = place.resolve().parent
            while not os.path.lexists(parent):
                why = self.place_reason(parent, str(parent))
                if why:
                    return why
                parent = parent.parent
        return None

    def copy_reason(self, words: list, cwds: list) -> str | None:
        """cp SRC DST and mv SRC DST: no option, SRC a regular file, DST a file path in lead_write (mv: the name
        itself too, and SRC)."""
        name, args = words[0].text, words[1:]
        if len(args) != 2 or any(w.text.startswith("-") for w in args):
            raise NotListed(f"{name} other than {name} SRC DST")
        src, dst = args
        exact(src, name)
        exact(dst, name)
        for place in self.known_places(src, cwds):
            if os.path.islink(place) or not os.path.isfile(place):
                return f"lead mode: {name} {src.text} is not a regular file; {BRIEF}"
        if dst.text.endswith("/") or any(os.path.isdir(p) for p in self.known_places(dst, cwds)):
            return f"lead mode: {name} into the folder {dst.text}, not to a file path; {BRIEF}"
        why = self.target_reason(dst, cwds)
        if name == "mv":
            why = why or self.target_reason(dst, cwds, follow=False) or self.target_reason(src, cwds)
        return why

    def gh_reason(self, words: list, cwds: list) -> None:
        """gh pr view/list/checks and gh run list/view, with only the options of GH_OPTIONS (no --web)."""
        literals(words[1:], "gh")
        command = " ".join(w.text for w in words[:3])
        if tuple(w.text for w in words[1:3]) not in GH_READS:
            raise NotListed(command)
        listed_options(words[3:], GH_OPTIONS, command)
        return None

    def tool_reason(self, head: Word, cwds: list) -> None:
        """A program by its path: only the pack's own team, log and federate, and not one lead_write covers."""
        exact(head, "the program")
        for place in self.known_places(head, cwds):
            real = place.resolve()
            if real not in self.tools or self.writable(real):
                raise NotListed(head.text)
        return None

    def script_reason(self, name: str, args: list, cwds: list) -> None:
        """bash PATH ... (with any arguments), . PATH and source PATH (no other word, review T-0188.r6): PATH (with a
        /) a lead_run script that lead_write does not cover."""
        if name not in ("bash", ".", "source") or not args:
            raise NotListed(name)
        if name != "bash" and len(args) != 1:
            raise NotListed(f"{name} {' '.join(word.raw for word in args)} ({name} PATH, no other word)")
        self.run_script(name, args[0], cwds)
        return None

    def run_script(self, name: str, word: Word, cwds: list) -> None:
        """The PATH of bash, . or source: as written, with a /, a lead_run script where it resolves, and one the lead
        cannot write through its own boundary: neither that file nor PATH as written (its folders resolved or not,
        so a link the lead could replace) is a path lead_write or a write_root covers."""
        exact(word, name)
        if "/" not in word.text:
            raise NotListed(f"{name} {word.text} (a lead_run script by its path)")
        for place in self.known_places(word, cwds):
            real = place.resolve()
            if not any(p.fullmatch(real.as_posix()) for p in self.run_patterns):
                raise NotListed(f"{name} {word.text} (not a lead_run script)")
            names = (real, place.parent.resolve() / place.name, Path(os.path.normpath(place)))
            if any(self.writable(path) for path in names):
                raise NotListed(f"{name} {word.text} (a lead_run script that lead_write or a write_root covers)")

    # ------------------------------------------------------------------------------------------------ git
    def git_reason(self, words: list, cwds: list) -> str | None:
        args, dirs = words[1:], []
        literals(args, "git")
        while len(args) >= 2 and args[0].text == "-C" and plain_value(args[1].text):
            exact(args[1], "git -C")
            dirs, args = dirs + [args[1]], args[2:]
        if not args or args[0].text.startswith("-"):
            raise NotListed("git with an option other than -C DIR before the subcommand")
        sub, rest = args[0].text, args[1:]
        if sub not in ("add", "commit"):
            self.git_read(sub, rest)
        for cwd in self.git_places(cwds, dirs):
            why = self.git_trust_reason(cwd) or self.git_write_reason(sub, rest, cwd)
            if why:
                return why
        return None

    def git_read(self, sub: str, rest: list) -> None:
        """A git read with only the options of GIT_OPTIONS: cat-file only -p OBJECT, branch only with --list, -v
        or -a and without a name, and worktree list and remote -v as written."""
        if (sub, tuple(w.text for w in rest)) in GIT_EXACT:
            return
        if sub not in GIT_OPTIONS:
            raise NotListed(f"git {sub}")
        seen, operands = listed_options(rest, GIT_OPTIONS[sub], f"git {sub}")
        if sub == "cat-file" and (seen != {"-p"} or len(operands) != 1):
            raise NotListed("git cat-file other than git cat-file -p OBJECT")
        if sub == "branch" and (operands or not seen):
            raise NotListed("git branch other than git branch --list, -v or -a (no name)")

    def git_places(self, cwds: list, dirs: list) -> list:
        places = list(cwds)
        for word in dirs:
            places = [None if p is None else self.places(word, [p])[0] for p in places]
        if None in places:
            raise CannotTell("git after a cd it cannot follow")
        return places

    def git_run(self, cwd: Path, *args: str, options: tuple = (), raw: bool = False):
        """git's stdout (bytes with raw), options before -C; a failure refuses (cannot tell)."""
        try:
            done = subprocess.run(["git", "--no-optional-locks", "-c", "core.fsmonitor=false", *options, "-C", str(cwd),
                                   *args], capture_output=True, text=not raw, errors=None if raw else "surrogateescape",
                                  timeout=20, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as error:
            raise CannotTell(f"git failed: {type(error).__name__}") from None
        if done.returncode:
            raise CannotTell(f"git {args[0]} failed in {self.display(cwd)}")
        return done.stdout

    def git_trust_reason(self, cwd: Path) -> str | None:
        """git reads its repository's config and runs its hooks: a repository whose git folder, config or hooks (or
        the global config) the lead may write could run the lead's code, so git is refused there."""
        lines = self.git_run(cwd, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir",
                             "--git-path", "hooks").splitlines()
        if len(lines) != 3:
            raise CannotTell("git rev-parse gave no git folder")
        git_dir, common, hooks = (Path(line).resolve() for line in lines)
        risky = [git_dir, common, git_dir / "config", common / "config", git_dir / "config.worktree", hooks]
        risky += [hooks / name for name in GIT_HOOKS] + self.git_configs
        if any(self.writable(path) for path in risky):
            where = self.display(git_dir)
            return f"lead mode: git in {where}, whose git folder, config or hooks lead_write covers; {BRIEF}"
        return None

    def git_write_reason(self, sub: str, rest: list, cwd: Path) -> str | None:
        if sub == "add":
            return self.git_add_reason(rest, cwd)
        if sub == "commit":
            return self.git_commit_reason(rest, cwd)
        return None

    def git_add_reason(self, rest: list, cwd: Path) -> str | None:
        """git add [--] FILE...: no option, every FILE an existing regular file in lead_write, no glob or :magic."""
        files = rest[1:] if rest[:1] and rest[0].text == "--" else rest
        if not files or any(w.text.startswith("-") for w in files):
            raise NotListed("git add with an option or without a file")
        for word in files:
            why = self.git_file_reason(word, cwd)
            if why:
                return why
        return None

    def git_file_reason(self, word: Word, cwd: Path) -> str | None:
        """One FILE of git add: as written (no glob or :magic), an existing regular file, a lead_write path."""
        exact(word, "git add")
        if word.text.startswith(":") or any(c in word.text for c in "*?[\\"):
            raise CannotTell(f"git pathspec {word.text} is a glob or magic")
        for place in self.known_places(word, [cwd]):
            if os.path.islink(place) or not os.path.isfile(place):
                return f"lead mode: git add {word.text} is not an existing regular file; {BRIEF}"
        return self.target_reason(word, [cwd])

    def git_commit_reason(self, rest: list, cwd: Path) -> str | None:
        """git commit [-q] -m MSG: only -q and -m (GIT_COMMIT_OPTIONS), no gitlink staged, and every path staged in
        that repository in lead_write (refused when git cannot list them or a name is not UTF-8)."""
        for word in rest:
            exact(word, "git commit")
        seen, operands = listed_options(rest, GIT_COMMIT_OPTIONS, "git commit")
        if "-m" not in seen or operands:
            raise NotListed("git commit other than git commit [-q] -m MSG")
        top = Path(self.git_run(cwd, "rev-parse", "--show-toplevel").strip()).resolve()
        staged = self.staged_paths(top)
        refused = (self.name_reason(top, p) or self.place_reason(top / p, p) for p in staged)
        return self.gitlink_reason(top) or next((why for why in refused if why), None)

    def name_reason(self, top: Path, name: str) -> str | None:
        """Why the name git records is refused, read lexically: a working-tree symlink on its way (src -> docs/notes)
        must not make src/outside.md look like docs/notes/outside.md (review T-0188.r3)."""
        why = self.why_refused(top / name)
        return None if why is None else f"lead mode: {name} {why}; {BRIEF}"

    def staged_paths(self, top: Path) -> list:
        """Every path git commit records in top: git diff --cached --name-only with STAGED_CONFIG and STAGED, each
        name decoded as UTF-8 (cannot tell otherwise)."""
        names = self.git_run(top, "diff", "--cached", "--name-only", *STAGED, options=STAGED_CONFIG, raw=True)
        try:
            return [name.decode("utf-8") for name in names.split(b"\0") if name]
        except UnicodeDecodeError:
            raise CannotTell("git lists a staged name that is not UTF-8") from None

    def gitlink_reason(self, top: Path) -> str | None:
        """A staged change with a gitlink (mode 160000) on either side: what it records is another repository's
        commit, so git commit is refused, inside lead_write too (the same listing with --raw: ":OLD NEW ...", then
        the path)."""
        fields = self.git_run(top, "diff", "--cached", "--raw", *STAGED, options=STAGED_CONFIG, raw=True).split(b"\0")
        for meta, name in zip(fields[0::2], fields[1::2]):
            if GITLINK in meta.lstrip(b":").split()[:2]:
                shown = name.decode("utf-8", "backslashreplace")
                return f"lead mode: git commit of {shown}, a gitlink (a submodule's commit); {BRIEF}"
        return None
