#!/usr/bin/env python3
"""Build the three static docs pages with Python 3.9+ and no dependencies.

Run from any directory; --check compares bytes without writing. --root selects a
fixture checkout for tests. The supported Markdown subset is ATX headings,
paragraphs, backtick code spans, * / ** emphasis (also _ / __), inline links and
HTTP(S) autolinks, nested unordered / ordered lists, backtick fenced code and
blockquotes. Backslash escapes punctuation. Images, raw HTML, tables, reference
links, task lists, indented code, setext headings and other extensions fail with
source locations. Add syntax deliberately, with a failing fixture first.

The page shell comes from index.html, so header/footer and security metadata
follow the site. Guide inputs are read-only. examples.html belongs to T-0115.
"""
import argparse
from collections import Counter
import hashlib
from html import escape
from pathlib import Path
import re
import string
import sys
import unicodedata
from urllib.parse import quote, unquote, urlsplit

PARTS = ('overview.md', 'method.md', 'safety.md', 'getting-started.md', 'limits.md', 'SOURCES.md')
GUIDES = set(PARTS) | {'examples.md', 'contribute.md'}
DESTINATIONS = {**{'docs/guide/' + n: 'docs.html' for n in PARTS},
                'docs/guide/contribute.md': 'contribute.html', 'CHANGELOG.md': 'changelog.html'}
REPOSITORY = 'https://github.com/vaiev/wouf/blob/main/'
HEADING = re.compile(r'^(#{1,6}) (.+?)(?: +#+)?$')
LIST = re.compile(r'^( *)([-+*]|\d+\.) (.+)$')
ALIASES = {'overview-what-wouf-is': ('what',), 'method': ('how',),
           'safety-sandbox-modes': ('where',), 'getting-started': ('quickstart',),
           'getting-started-set-it-up': ('setup',), 'getting-started-first-steps': ('use',),
           'getting-started-tiers-which-model': ('yours',), 'overview-a-plain-folder': ('maintain',),
           'sources-license': ('license',)}
TECHNICAL = {'getting-started-agentsmd-and-adapters', 'getting-started-runners-which-cli-runs-the-bots',
             'getting-started-tiers-which-model', 'safety-network-and-egress'}


def error(file, line, message):
    raise ValueError(f'{file}:{line}: {message}')


def slug(text):
    plain = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z0-9]+', '-', re.sub(r'[`*_\[\].]', '', plain)).strip('-') or 'section'


def safe_url(url, file, line):
    decoded = unquote(url)
    if (not url or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in decoded)
            or '\\' in decoded or decoded.startswith('//')):
        error(file, line, 'unsafe or unsupported URL')
    try:
        parts = urlsplit(decoded)
    except ValueError:
        error(file, line, 'invalid URL')
    if parts.scheme and parts.scheme.lower() not in ('http', 'https', 'mailto'):
        error(file, line, 'unsafe URL scheme: ' + parts.scheme)
    if parts.scheme in ('http', 'https') and not parts.netloc:
        error(file, line, 'HTTP URL needs a host')
    return url


def link_end(text, start, file, line):
    depth, at = 1, start
    while at < len(text):
        if text[at] == '\\':
            at += 2
            continue
        depth += (text[at] == '(') - (text[at] == ')')
        if depth == 0:
            return at
        at += 1
    error(file, line, 'unclosed link destination')


def inline_code(text, at, file, line):
    fence = re.match(r'`+', text[at:])[0]
    end = text.find(fence, at + len(fence))
    if end == -1:
        error(file, line, 'unclosed inline code')
    return '<code>' + escape(text[at + len(fence):end]) + '</code>', end + len(fence)


def inline_link(text, at, file, line, resolve, in_link):
    end = text.find(']', at + 1)
    if end == -1 or text[end + 1:end + 2] not in ('(', '['):
        return '[', at + 1
    if in_link or text[end + 1] == '[':
        error(file, line, 'nested or reference links are unsupported')
    stop = link_end(text, end + 2, file, line)
    url = safe_url(text[end + 2:stop], file, line)
    if resolve:
        url = resolve(url, line)
    label = inline(text[at + 1:end], file, line, resolve, True)
    return f'<a href="{escape(url, quote=True)}">{label}</a>', stop + 1


def inline_angle(text, at, file, line, in_link):
    if re.match(r'<https?://', text[at:]):
        end = text.find('>', at)
        if end == -1 or in_link:
            error(file, line, 'invalid autolink')
        url = safe_url(text[at + 1:end], file, line)
        return f'<a href="{escape(url, quote=True)}">{escape(url)}</a>', end + 1
    if re.match(r'<(?:[A-Za-z/!?])', text[at:]):
        error(file, line, 'raw HTML is unsupported')
    return '&lt;', at + 1


def inline_emphasis(text, at, file, line, resolve, in_link):
    char = text[at]
    marker = char * (2 if text.startswith(char * 2, at) else 1)
    start = at + len(marker)
    if start == len(text) or text[start].isspace():
        return char, at + 1
    end = text.find(marker, start)
    if end == -1:
        error(file, line, 'unclosed emphasis')
    tag = 'strong' if len(marker) == 2 else 'em'
    content = inline(text[start:end], file, line, resolve, in_link)
    return f'<{tag}>' + content + f'</{tag}>', end + len(marker)


def inline_token(text, at, file, line, resolve, in_link):
    char = text[at]
    if char == '\\' and at + 1 < len(text) and text[at + 1] in string.punctuation:
        return escape(text[at + 1]), at + 2
    if char == '`':
        return inline_code(text, at, file, line)
    if text.startswith(('![', '~~'), at):
        error(file, line, 'unsupported image or strikethrough syntax')
    if char == '[':
        return inline_link(text, at, file, line, resolve, in_link)
    if char == '<':
        return inline_angle(text, at, file, line, in_link)
    if char in '*_' and not (char == '_' and at and text[at - 1].isalnum()):
        return inline_emphasis(text, at, file, line, resolve, in_link)
    return escape(char), at + 1


def inline(text, file, line, resolve=None, in_link=False):
    out, at = [], 0
    while at < len(text):
        fragment, at = inline_token(text, at, file, line, resolve, in_link)
        out.append(fragment)
    return ''.join(out)


class Block:
    def __init__(self, kind, line, text='', level=0, children=None):
        self.kind, self.line, self.text, self.level = kind, line, text, level
        self.children = children or []
        self.id = ''


def special(text):
    return bool(HEADING.match(text) or LIST.match(text) or text.startswith(('>', '```')))


def unsupported(text, file, line):
    if ('\t' in text or re.match(r' {4}\S', text) or text.startswith(('|', '~~~'))
            or re.fullmatch(r'\s*(?:[-*_]\s*){3,}|\s*=+\s*', text)
            or re.match(r'\s*\[[^]]+\]:', text) or re.match(r'[-+*] \[[ xX]\]', text)):
        error(file, line, 'unsupported Markdown block syntax')


def parse_fence(lines, at, file):
    number, text = lines[at]
    opening = re.fullmatch(r'(`{3,})([\w+-]*)\s*', text)
    if not opening:
        error(file, number, 'unsupported code fence')
    code, at = [], at + 1
    while at < len(lines) and lines[at][1] != opening[1]:
        code.append(lines[at][1])
        at += 1
    if at == len(lines):
        error(file, number, 'unclosed code fence')
    return Block('code', number, '\n'.join(code) + ('\n' if code else '')), at + 1


def parse_quote(lines, at, file):
    number, quoted = lines[at][0], []
    while at < len(lines) and lines[at][1].startswith('>'):
        n, value = lines[at]
        quoted.append((n, value[1:].removeprefix(' ')))
        at += 1
    return Block('quote', number, children=parse_blocks(quoted, file)), at


def paragraph_line(lines, at, file):
    number, value = lines[at]
    if at + 1 < len(lines):
        following = lines[at + 1][1]
        if re.fullmatch(r'(?:=+|-+)\s*', following):
            error(file, number, 'setext headings are unsupported; use # headings')
        if re.fullmatch(r'\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)+\|?', following):
            error(file, number, 'tables are unsupported')
    unsupported(value, file, number)
    if value.endswith(('  ', '\\')):
        error(file, number, 'hard line breaks are unsupported')
    # Validate each source line separately to keep precise diagnostics.
    inline(value, file, number)
    return value


def parse_paragraph(lines, at, file):
    number, paragraph = lines[at][0], []
    while at < len(lines) and lines[at][1].strip() and not special(lines[at][1]):
        paragraph.append(paragraph_line(lines, at, file))
        at += 1
    return Block('paragraph', number, '\n'.join(paragraph)), at


def parse_blocks(lines, file):
    blocks, at = [], 0
    while at < len(lines):
        number, text = lines[at]
        if not text.strip():
            at += 1
            continue
        unsupported(text, file, number)
        heading = HEADING.match(text)
        if heading:
            block = Block('heading', number, heading[2], len(heading[1]))
            at += 1
        elif text.startswith('```'):
            block, at = parse_fence(lines, at, file)
        elif text.startswith('>'):
            block, at = parse_quote(lines, at, file)
        elif LIST.match(text):
            block, at = parse_list(lines, at, file)
        else:
            block, at = parse_paragraph(lines, at, file)
        blocks.append(block)
    return blocks


def parse_list(lines, at, file):
    first = LIST.match(lines[at][1])
    indent, ordered = len(first[1]), first[2].endswith('.')
    block = Block('ol' if ordered else 'ul', lines[at][0], first[2][:-1] if ordered else '')
    while at < len(lines):
        match = LIST.match(lines[at][1])
        if not match or len(match[1]) != indent or match[2].endswith('.') != ordered:
            break
        n, _ = lines[at]
        if re.match(r'\[[ xX]\] ', match[3]):
            error(file, n, 'task lists are unsupported')
        item = [(n, match[3])]
        width, at = len(match[1]) + len(match[2]) + 1, at + 1
        while at < len(lines):
            number, value = lines[at]
            if not value.strip():
                if at + 1 < len(lines) and lines[at + 1][1].startswith(' ' * width):
                    item.append((number, ''))
                    at += 1
                    continue
                break
            if not value.startswith(' ' * width):
                break
            item.append((number, value[width:]))
            at += 1
        block.children.append(Block('li', n, children=parse_blocks(item, file)))
    return block, at


def walk(blocks):
    for block in blocks:
        yield block
        yield from walk(block.children)


def render_blocks(blocks, file, resolve=None, demote=0):
    out = []
    for block in blocks:
        kind, text = block.kind, block.text
        if kind == 'heading':
            level = block.level + demote
            if level > 6:
                error(file, block.line, 'heading is too deep for the combined guide')
            out.append(f'<h{level} id="{block.id}">' + inline(text, file, block.line, resolve) + f'</h{level}>')
        elif kind == 'paragraph':
            content = '\n'.join(inline(t, file, block.line + i, resolve) for i, t in enumerate(text.split('\n')))
            out.append('<p>' + content + '</p>')
        elif kind == 'code':
            out.append('<pre><code>' + escape(text) + '</code></pre>')
        else:
            tag = 'blockquote' if kind == 'quote' else kind
            attr = f' start="{int(text)}"' if kind == 'ol' and text != '1' else ''
            out.append(f'<{tag}{attr}>' + render_blocks(block.children, file, resolve, demote) + f'</{tag}>')
    return '\n'.join(out)


def unique_id(base, counts, used):
    while True:
        counts[base] += 1
        candidate = base + (f'-{counts[base]}' if counts[base] > 1 else '')
        if candidate not in used:
            used.add(candidate)
            return candidate


def assign_ids(blocks, prefix=''):
    counts, html_counts, anchors = Counter(), Counter(), {}
    local_ids, html_ids = set(), {'doc', 'top', 'end', 'setup-line', 'copy-status'}
    for block in walk(blocks):
        if block.kind != 'heading':
            continue
        base = slug(block.text)
        local = unique_id(base, counts, local_ids)
        desired = prefix if prefix and block.level == 1 else (prefix + '-' if prefix else '') + local
        block.id = unique_id(desired, html_counts, html_ids)
        anchors[local] = block.id
    return anchors


def render_markdown(text, filename='fixture.md'):
    blocks = parse_blocks(list(enumerate(text.splitlines(), 1)), filename)
    assign_ids(blocks)
    return render_blocks(blocks, filename)


class Document:
    def __init__(self, root, name):
        self.name = name
        self.raw = (root / name).read_text(encoding='utf-8')
        self.blocks = parse_blocks(list(enumerate(self.raw.splitlines(), 1)), name)
        headings = [b for b in walk(self.blocks) if b.kind == 'heading']
        if not self.blocks or self.blocks[0].kind != 'heading' or self.blocks[0].level != 1:
            error(name, 1, 'source must start with one level-one heading')
        if sum(h.level == 1 for h in headings) != 1:
            error(name, next(h.line for h in headings[1:] if h.level == 1), 'source must have exactly one level-one heading')
        self.title = self.blocks[0].text
        self.combined = DESTINATIONS[name] == 'docs.html'
        self.anchors = assign_ids(self.blocks, slug(Path(name).stem) if self.combined else '')


def source_target(root, source, parts, line):
    if not parts.path:
        path = root / source.name
    elif parts.path.startswith('/'):
        path = root / unquote(parts.path).lstrip('/')
    else:
        path = root / Path(source.name).parent / unquote(parts.path)
    try:
        return path, path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        error(source.name, line, 'relative link escapes repository')


def resolve_link(root, documents, source, url, line):
    safe_url(url, source.name, line)
    parts = urlsplit(url)
    if parts.scheme:
        return url
    path, name = source_target(root, source, parts, line)
    if name in documents:
        target = documents[name]
        fragment = unquote(parts.fragment)
        if fragment and fragment not in target.anchors:
            error(source.name, line, 'unknown source fragment: ' + url)
        return DESTINATIONS[name] + ('?' + parts.query if parts.query else '') + ('#' + target.anchors[fragment] if fragment else '')
    if name == 'docs/guide/examples.md':
        if parts.fragment:
            error(source.name, line, 'examples fragments are owned by the examples page; link its stable URL')
        return 'examples.html'
    if not path.is_file():
        error(source.name, line, 'relative source target is missing: ' + url)
    site = name.removeprefix('site/')
    dest = site if name.startswith('site/') else REPOSITORY + quote(name, safe='/')
    return dest + ('?' + parts.query if parts.query else '') + ('#' + parts.fragment if parts.fragment else '')


def page_shell(root, name, title, description, content):
    home = (root / 'site/index.html').read_text(encoding='utf-8')
    prefix = home.split('<main ', 1)[0]
    prefix = re.sub(r'<title>.*?</title>', '<title>' + escape(title) + '</title>', prefix)
    prefix = re.sub(r'(<meta (?:name="(?:description|twitter:description)"|property="og:description") content=")[^"]*',
                    lambda m: m[1] + escape(description, quote=True), prefix)
    prefix = re.sub(r'(<meta (?:name="twitter:title"|property="og:title") content=")[^"]*',
                    lambda m: m[1] + escape(title, quote=True), prefix)
    prefix = prefix.replace('https://wouf.dev/"', 'https://wouf.dev/' + name + '"')
    prefix = prefix.replace('<body data-page="home">', '<body>').replace('<div class="sky" aria-hidden="true"></div>\n', '')
    prefix = prefix.replace('href="#top">Skip', 'href="#doc">Skip')
    prefix = prefix.replace(' aria-current="page"', '')
    if name == 'docs.html':
        prefix = prefix.replace('href="docs.html">Docs', 'href="docs.html" aria-current="page">Docs')
    prefix = prefix.replace('<link rel="expect"', '<link rel="stylesheet" href="css/docs.css">\n<link rel="expect"')
    prefix = prefix.replace('</head>', '<script src="js/docs.js" defer></script>\n</head>')
    footer = '<footer class="site-foot">' + home.split('<footer class="site-foot">', 1)[1]
    if name == 'docs.html':
        footer = footer.replace('<a href="docs.html">Docs</a>', '<a href="index.html">Home</a>')
    else:
        footer = footer.replace('<a href="docs.html">Docs</a>', '<a href="index.html">Home</a><a href="docs.html">Docs</a>')
    return prefix + '<main class="page site wide" id="top">\n' + content + '\n  ' + footer


def fork():
    return ('<div class="docs-fork" aria-label="Choose your path">'
            '<a class="btn btn-filled press" href="#quickstart">Just get started</a>'
            '<a class="btn btn-tonal press" href="#how">Understand and adapt</a></div>')


def glossary():
    return ('<p class="docs-note">A <dfn>sandbox</dfn> is a separate workspace that limits what a bot can access. '
            'Host mode has no sandbox. <dfn>Headless</dfn> means the bot runs the assistant without an interactive chat window.</p>')


def privacy():
    # F19: bounded explanatory copy, matching the qualified data-flow claims in in/CLAIMS.md.
    return ('<section class="docs-privacy" aria-labelledby="privacy-data">'
            '<h3 id="privacy-data">Privacy and where your data goes</h3>'
            '<p>You choose where the team and model run. WOUF does not require its own cloud or server.</p>'
            '<ul><li><strong>Cloud model:</strong> the agent CLI sends the content it uses to its model provider. '
            'An allowed network destination can receive data.</li>'
            '<li><strong>Compatible local model:</strong> the CLI sends content to the model server you configure. '
            'Verify model support, authentication, tool use and network reachability in your chosen environment. '
            'Other configured services, including Git hosting and external engines, may still receive data.</li></ul>'
            '<p>Docker, Podman and bubblewrap stage inputs and a repository copy into a separate workspace. '
            'Host mode runs under your user permissions without a sandbox. Choose what inputs, keys and connectors to provide.</p>'
            '<p><a href="#safety-network-and-egress">Read the network settings and their limits</a>; '
            '<a href="#limits">read the limits before using real data</a>.</p></section>')


def compatibility_anchors(doc):
    """Keep aliases in their guide part if a source heading is renamed."""
    ids = {b.id for b in walk(doc.blocks) if b.kind == 'heading'}
    root = doc.blocks[0].id
    anchors = {}
    for target, aliases in ALIASES.items():
        if target == root or target.startswith(root + '-'):
            closest = target if target in ids else root
            anchors.setdefault(closest, []).extend(aliases)
    return anchors


def decorate(doc, root, documents):
    resolve = lambda url, line: resolve_link(root, documents, doc, url, line)
    anchors = compatibility_anchors(doc)
    out, details, setup_quote = [], False, False
    for block in doc.blocks:
        if block.kind == 'heading':
            if details:
                out.append('</details>')
                details = False
            heading = render_blocks([block], doc.name, resolve, 1)
            aliases = ''.join(f'<span class="docs-anchor" id="{alias}"></span>' for alias in anchors.get(block.id, ()))
            heading = heading.replace('>', '>' + aliases, 1)
            if block.id == 'overview-a-plain-folder':
                # The fork follows all paragraphs in "What WOUF is".
                out.append(fork())
            out.append(heading)
            if block.id in TECHNICAL:
                out.append('<details class="docs-details"><summary>Technical details</summary>')
                details = True
            if block.id == 'getting-started-set-it-up':
                setup_quote = True
                # Share the redesigned site's existing terminal help, without another copy.
                home = (root / 'site/index.html').read_text(encoding='utf-8')
                help_block = re.search(r'<details class="terminal-help">.*?</details>', home, re.S)
                if help_block:
                    out.append(help_block[0])
        elif setup_quote and block.kind == 'quote':
            content = render_blocks(block.children, doc.name, resolve)
            out.append('<div class="docs-setup"><div class="copyline lens"><div id="setup-line">' + content + '</div>'
                       '<button class="btn btn-filled press" type="button" data-copy="#setup-line">Copy the setup sentence</button>'
                       '</div><p>The assistant sets up the team in this folder, asks for your choices, then runs its checks.</p>'
                       '<p id="copy-status" role="status" aria-live="polite"></p></div>')
            setup_quote = False
        else:
            out.append(render_blocks([block], doc.name, resolve, 1))
        if block.kind == 'heading' and block.id == 'overview':
            out.append(glossary())
    if details:
        out.append('</details>')
    html = '\n'.join(out)
    if doc.name.endswith('/safety.md'):
        html += '\n' + privacy()
    return html


def navigation_group(doc, filename):
    links = [b for b in doc.blocks if b.kind == 'heading' and b.level <= 2]
    if filename == 'SOURCES.md':
        links = links[:1]
    anchors = ''.join(f'<a href="#{b.id}">' + inline(b.text, doc.name, b.line) + '</a>' for b in links)
    if filename == 'safety.md':
        anchors += '<a href="#privacy-data">Privacy and where your data goes</a>'
    return '<div class="docs-nav-group"><p class="docs-group">' + escape(doc.title) + '</p>' + anchors + '</div>'


def guide(root, documents):
    nav, content = [], ['<h1>WOUF guide</h1>']
    for filename in PARTS:
        doc = documents['docs/guide/' + filename]
        nav.append(navigation_group(doc, filename))
        content.append(decorate(doc, root, documents))
    return ('<div class="docs docs-generated"><nav class="docs-nav" aria-label="Guide sections">' + '\n'.join(nav) +
            '</nav><article class="prose" id="doc" tabindex="-1">\n' + '\n'.join(content) + '\n</article></div>')


def outputs(root):
    directory = root / 'docs/guide'
    unexpected = sorted(p.name for p in directory.iterdir() if p.is_file() and p.name not in GUIDES)
    if unexpected:
        error('docs/guide', 1, 'unexpected guide filename: ' + ', '.join(unexpected))
    for filename in sorted(GUIDES):
        if not (directory / filename).is_file():
            error('docs/guide/' + filename, 1, 'required guide source is missing')
    documents = {name: Document(root, name) for name in DESTINATIONS}
    contents = {'docs.html': guide(root, documents)}
    for name in ('docs/guide/contribute.md', 'CHANGELOG.md'):
        doc = documents[name]
        resolve = lambda url, line: resolve_link(root, documents, doc, url, line)
        contents[DESTINATIONS[name]] = '<div class="page-body"><article class="prose" id="doc" tabindex="-1">\n' + render_blocks(doc.blocks, name, resolve) + '\n</article></div>'
    result = {}
    for name, content in contents.items():
        sources = [d for d in documents.values() if DESTINATIONS[d.name] == name]
        digest = hashlib.sha256(''.join(d.name + '\0' + d.raw for d in sources).encode()).hexdigest()
        title = {'docs.html': 'WOUF guide', 'changelog.html': 'Changelog', 'contribute.html': 'Contribute'}[name]
        html = page_shell(root, name, title + ' — WOUF', title + ': the guide, evidence and next steps for WOUF.',
                          f'<!-- Generated by scripts/build_site_docs.py; source SHA-256 {digest}. -->\n' + content)
        result[name] = html.encode('utf-8')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='compare all outputs without writing')
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1], help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        generated = outputs(args.root)
        stale = []
        for name, data in generated.items():
            path = args.root / 'site' / name
            if args.check:
                if not path.is_file() or path.read_bytes() != data:
                    stale.append('site/' + name)
            else:
                path.write_bytes(data)
        if stale:
            print('Stale generated docs: ' + ', '.join(stale) + '. Run python3 scripts/build_site_docs.py', file=sys.stderr)
            return 1
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
