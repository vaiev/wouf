"""site/: wouf.dev as static files. Links and assets resolve inside the folder, nothing loads from another
host, every page carries a CSP, one h1, a lang and a title, and the folder names no other brand."""
from __future__ import annotations

import re
import shutil
import subprocess
import hashlib
import unittest
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

SITE = Path(__file__).resolve().parents[1] / 'site'
HOSTS = {'github.com', 'wouf.dev'}
OTHER_BRANDS = re.compile(rb'r[o]enu|w[u]lew|f[o]ug|v[a]ikor', re.I)
ABSOLUTE = re.compile(r'\b(?:https?:)?//([a-z0-9.-]+)', re.I)
FOOTER_HOST = 'vaiev.com'  # only as the href of an <a> in the site-foot footer ("Made by VAIEV", "Privacy")
SITE_PAGES = ('index.html', 'docs.html', '404.html', 'examples.html', 'changelog.html', 'contribute.html')  # the pages people read; og-card.html is only rendered to og.png
VENDOR = {'css/' + n + '.css' for n in ('materials', 'components', 'patterns')} | {'js/vt.js', 'js/vaiev.js'}
GITHUB = 'https://github.com/vaiev/wouf'
GITHUB_LABEL = 'WOUF on GitHub'
OG_IMAGE = 'https://wouf.dev/og.png'
# rendered by the lead from og-card.html after landing; until then they are not in the folder
RENDERED = {'apple-touch-icon.png'}
LEGAL = '\u00a9 2026 VAIEV \u00b7 WOUF is open source under the Apache 2.0 license'
OTHER_PAGE = {'index.html': {'Docs': 'docs.html'}, 'docs.html': {'Home': 'index.html'},
              '404.html': {'Home': '/', 'Docs': '/docs.html'}, **{n: {'Home': 'index.html', 'Docs': 'docs.html'} for n in ('examples.html', 'changelog.html', 'contribute.html')}}


class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []  # (tag, attrs)
        self.ids = set()
        self.title = ''
        self.h1 = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append((tag, attrs))
        if attrs.get('id'):
            self.ids.add(attrs['id'])
        self.h1 += tag == 'h1'
        self._in_title = tag == 'title'

    handle_startendtag = handle_starttag

    def handle_endtag(self, tag):
        if tag == 'title':
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


class FooterLinks(HTMLParser):
    """Counts the <a href> to FOOTER_HOST inside <footer class="site-foot">."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.count = 0
        self._footer = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'footer' and (self._footer or 'site-foot' in (attrs.get('class') or '').split()):
            self._footer += 1
        elif tag == 'a' and self._footer and (urlsplit(attrs.get('href') or '').hostname or '') == FOOTER_HOST:
            self.count += 1

    def handle_endtag(self, tag):
        if tag == 'footer' and self._footer:
            self._footer -= 1


class Node:
    def __init__(self, tag, attrs, parent=None):
        self.tag, self.attrs, self.parent, self.children = tag, attrs, parent, []

    def text(self):
        return ' '.join(''.join(c if isinstance(c, str) else ' ' + c.text() + ' ' for c in self.children).split())

    def all(self, tag=None, cls=None):
        for child in self.children:
            if isinstance(child, Node):
                if (tag is None or child.tag == tag) and (cls is None or cls in (child.attrs.get('class') or '').split()):
                    yield child
                yield from child.all(tag, cls)


class Tree(HTMLParser):
    """A small element tree: enough to look inside the header and the footer."""
    VOID = {'meta', 'link', 'img', 'br', 'input', 'use', 'source', 'hr', 'path', 'circle'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = self._at = Node('#root', {})

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs), self._at)
        self._at.children.append(node)
        if tag not in self.VOID:
            self._at = node

    def handle_startendtag(self, tag, attrs):
        self._at.children.append(Node(tag, dict(attrs), self._at))

    def handle_endtag(self, tag):
        at = self._at
        while at.parent and at.tag != tag:
            at = at.parent
        if at.parent:
            self._at = at.parent

    def handle_data(self, data):
        self._at.children.append(data)


def tree(name):
    parser = Tree()
    parser.feed((SITE / name).read_text(encoding='utf-8'))
    return parser.root


def metas(name):
    found = {}
    for tag, attrs in parse(SITE / name).tags:
        if tag == 'meta' and (attrs.get('property') or attrs.get('name')):
            found[attrs.get('property') or attrs.get('name')] = attrs.get('content')
    return found


def stray_hosts(text):
    """The absolute hosts in a page or stylesheet that are not allowed there."""
    stray = [host.lower() for host in ABSOLUTE.findall(text) if host.lower() not in HOSTS]
    footer = FooterLinks()
    footer.feed(text)
    for _ in range(footer.count):
        if FOOTER_HOST in stray:
            stray.remove(FOOTER_HOST)
    return stray


def parse(path):
    page = Page()
    page.feed(path.read_text(encoding='utf-8'))
    return page


def pages():
    return sorted(SITE.glob('*.html'))


def stylesheets():
    return sorted((SITE / 'css').glob('*.css'))


def resolve(page, path):
    # 404.html is served at any depth, so it links from the site root ("/docs.html"); the others are relative
    if not path:
        return page
    target = SITE / path.lstrip('/') if path.startswith('/') else page.parent / path
    return target / 'index.html' if path.endswith('/') else target


def ids_of(target):
    if target.suffix == '.html':
        return parse(target).ids
    return set(re.findall(r'\bid="([^"]+)"', target.read_text(encoding='utf-8')))


class Site(unittest.TestCase):
    def test_the_folder_holds_what_wouf_dev_serves(self):
        for name in ('index.html', 'docs.html', '404.html', 'icons.svg', 'robots.txt', 'healthz', 'README.md',
                     'fonts/OFL.txt'):
            self.assertTrue((SITE / name).is_file(), name)
        for folder in ('css', 'js', 'fonts', 'brand'):
            self.assertTrue(any((SITE / folder).iterdir()), folder)
        self.assertEqual((SITE / 'healthz').read_text(encoding='utf-8').strip(), 'ok')

    def test_every_relative_href_and_src_exists(self):
        for page in pages():
            for tag, attrs in parse(page).tags:
                for attr in ('href', 'src'):
                    ref = attrs.get(attr)
                    if not ref or urlsplit(ref).scheme or ref.startswith('//'):
                        continue
                    if ref in RENDERED and (SITE / 'og-card.html').is_file():
                        continue
                    with self.subTest(page=page.name, ref=ref):
                        parts = urlsplit(ref)
                        target = resolve(page, unquote(parts.path))
                        self.assertTrue(target.is_file(), f'{page.name}: {ref} is missing')
                        if parts.fragment and target.suffix in ('.html', '.svg'):
                            self.assertIn(parts.fragment, ids_of(target), f'{page.name}: no #{parts.fragment}')

    def test_every_url_in_the_css_exists(self):
        for sheet in stylesheets():
            for ref in re.findall(r'url\(\s*["\']?([^"\')]+)', sheet.read_text(encoding='utf-8')):
                if ref.startswith('data:') or ref.startswith('#'):
                    continue
                with self.subTest(sheet=sheet.name, ref=ref):
                    self.assertFalse(urlsplit(ref).scheme or ref.startswith('//'), f'{sheet.name}: {ref} is absolute')
                    self.assertTrue((sheet.parent / urlsplit(ref).path).is_file(), f'{sheet.name}: {ref} is missing')
            self.assertNotIn('@import', sheet.read_text(encoding='utf-8'), sheet.name)

    def test_the_only_absolute_hosts_are_github_wouf_dev_and_the_vaiev_footer_link(self):
        for path in pages() + stylesheets():
            with self.subTest(file=path.name):
                self.assertEqual(stray_hosts(path.read_text(encoding='utf-8')), [])

    def test_vaiev_com_is_allowed_only_as_a_footer_link(self):
        link = ('<footer class="site-foot"><p>Made by <a class="vaiev" href="https://vaiev.com">VAIEV</a></p>'
                '<p><a href="https://vaiev.com/privacy/">Privacy</a></p></footer>')
        self.assertEqual(stray_hosts(link), [])
        self.assertEqual(stray_hosts('<a href="https://github.com/vaiev/wouf">x</a> <a href="https://wouf.dev/">y</a>'), [])
        for case in ('<a href="https://vaiev.com">VAIEV</a>',
                     '<footer><a href="https://vaiev.com">VAIEV</a></footer>',
                     '<script src="https://vaiev.com/a.js"></script>',
                     '<link rel="stylesheet" href="https://vaiev.com/a.css">',
                     '<img src="https://vaiev.com/a.png" alt="">',
                     '<footer class="site-foot"><img src="https://vaiev.com/a.png" alt=""></footer>',
                     '<footer class="site-foot"><svg><use href="https://vaiev.com/icons.svg#v"/></svg></footer>',
                     '@font-face { src: url(https://vaiev.com/a.woff2); }',
                     link + '<p>see https://vaiev.com</p>',
                     link + '<script src="https://vaiev.com/a.js"></script>'):
            with self.subTest(case=case):
                self.assertEqual(stray_hosts(case), ['vaiev.com'])

    def test_every_page_has_the_two_line_footer(self):
        for name in SITE_PAGES:
            with self.subTest(page=name):
                footers = list(tree(name).all('footer', 'site-foot'))
                self.assertEqual(len(footers), 1)
                lines = [child for child in footers[0].children if not isinstance(child, str)]
                self.assertEqual(len(lines), 2, 'two lines')
                made, legal = lines
                # first line: "Made by", then the VAIEV symbol and word as the only link
                self.assertEqual(made.text(), 'VAIEV')
                links = list(made.all('a'))
                self.assertEqual(len(links), 1)
                self.assertEqual(links[0].attrs.get('href'), 'https://vaiev.com')
                self.assertEqual(links[0].text(), 'VAIEV')
                self.assertEqual([u.attrs.get('href', '').rsplit('#', 1)[-1] for u in links[0].all('use')], ['vaiev-symbol'])
                # second line: the licence line, the GitHub mark, the other page(s) and Privacy
                self.assertIn(LEGAL, legal.text())
                self.assertFalse(any(n.tag == 'span' and n.attrs.get('aria-hidden') == 'true' for n in legal.children if isinstance(n, Node)))
                hrefs = {a.text(): a.attrs.get('href') for a in legal.all('a') if a.text()}
                self.assertEqual(hrefs, {**OTHER_PAGE[name], 'Privacy': 'https://vaiev.com/privacy/', 'Changelog': ('/' if name == '404.html' else '') + 'changelog.html', 'Contribute': ('/' if name == '404.html' else '') + 'contribute.html'})
                self.assertEqual(len([a for a in legal.all('a') if a.attrs.get('href') == GITHUB]), 1)
                self.assertNotIn('Initialized', footers[0].text())

    def test_phone_bar_sits_at_the_bottom_and_never_covers_the_end(self):
        css = (SITE / 'css/site.css').read_text(encoding='utf-8')
        # F4: v8 puts the bar at the bottom on phones; the site no longer moves it back to the top
        self.assertNotRegex(css, r'\.site-bar\s*\{[^}]*\btop:')
        phone = ''.join(re.findall(r'@media \(max-width: 859px\) \{(.*?)\n\}', css, re.S))
        self.assertRegex(phone, r'\.site-bar \.lockup, \.site-bar \.gh\s*\{\s*display:\s*none')
        # always visible and never folding: the same width whatever v8's scroll module says
        self.assertRegex(phone, r'html \.site-bar\[data-collapsible\], html\[data-scroll\] \.site-bar\[data-collapsible\]\s*\{\s*width:')
        # the page ends above the bar, safe area included
        self.assertRegex(phone, r'\.site\s*\{[^}]*padding-bottom:\s*calc\(60px \+ 2 \* var\(--size-float-gap\) \+ env\(safe-area-inset-bottom, 0px\)\)')
        self.assertRegex(phone, r'scroll-padding-bottom:\s*calc\(60px \+ 2 \* var\(--size-float-gap\) \+ env\(safe-area-inset-bottom, 0px\)\)')
        # from 860px the bar is at the top and 100px stay free above the content
        self.assertRegex(css, r'\.site\s*\{[^}]*padding-top:\s*100px')
        self.assertRegex(css, r'html\[data-site-state="top"\] \.site-bar\s*\{')
        self.assertNotIn('menu-toggle', css)
        self.assertRegex(css, r'\.site-foot \.vaiev \.logo\s*\{[^}]*width:\s*34px;[^}]*height:\s*23px')
        self.assertRegex(css, r'\.foot-legal a\s*\{[^}]*text-decoration:\s*none')

    def test_phone_page_end_clearance_wins_the_cascade(self):
        # F4 (review T-0111.r4): the clearance must be the declaration that applies to the page's <main>, not one
        # that a more specific rule overrides. Resolve padding-bottom for <main class="page site wide"> at 390px
        # over every stylesheet the pages load: matching rules, specificity, then source order.
        def rules(css):
            css = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
            out, i = [], 0
            for m in re.finditer(r'@media ([^{]*)\{((?:[^{}]*\{[^{}]*\})*)\s*\}|([^{}@]+)\{([^{}]*)\}', css):
                if m.group(1) is not None:
                    q = m.group(1)
                    width = re.search(r'\((min|max)-width:\s*(\d+)px\)', q)
                    applies = width is None or (390 <= int(width.group(2)) if width.group(1) == 'max' else 390 >= int(width.group(2)))
                    if 'prefers-reduced-motion' in q or 'hover' in q or 'print' in q or not applies:
                        continue
                    out += [(s, b, i) for s, b in re.findall(r'([^{}]+)\{([^{}]*)\}', m.group(2))]
                else:
                    out.append((m.group(3), m.group(4), i))
                i += 1
            return out
        classes = {'page', 'site', 'wide'}
        winner = None
        for order, sheet in enumerate(['tokens.css', 'base.css', 'materials.css', 'components.css', 'patterns.css', 'site.css']):
            for n, (sels, body, _) in enumerate(rules((SITE / 'css' / sheet).read_text(encoding='utf-8'))):
                decl = re.findall(r'(?:^|;)\s*padding(?:-bottom|-block(?:-end)?)?\s*:\s*([^;]+)', body)
                if not decl:
                    continue
                for sel in sels.split(','):
                    sel = sel.strip()
                    parts = re.fullmatch(r'(main)?((?:\.[\w-]+)+)', sel)
                    if not parts or not set(parts.group(2)[1:].split('.')) <= classes:
                        continue
                    spec = (len(parts.group(2).split('.')) - 1, 1 if parts.group(1) else 0)
                    key = (spec, order, n)
                    if winner is None or key > winner[0]:
                        winner = (key, sel, decl[-1].strip())
        self.assertIsNotNone(winner)
        self.assertIn('env(safe-area-inset-bottom, 0px)', winner[2], winner)
        self.assertIn('60px + 2 * var(--size-float-gap)', winner[2], winner)

    # A small DOM for js/site.js's bar: Home, Examples and Docs in the nav, the GitHub mark, and one focusable
    # element outside the bar. `focus(el)` moves focus the way a browser does (focusout with the element that
    # receives focus as relatedTarget, then focusin).
    BAR_DOM = r"""
const assert = require('assert');
const listeners = () => ({handlers: {}, addEventListener(n, f) { this.handlers[n] = f; }});
const root = {dataset: {scroll: 'down'}, scrollHeight: 3000, hasAttribute: () => false};
const links = ['Home', 'Examples', 'Docs'].map(textContent => ({textContent, hidden: false}));
const nav = Object.assign(listeners(), {querySelectorAll: () => links, contains: el => links.includes(el)});
const gh = {};
const outside = {};
const bar = Object.assign(listeners(), {querySelector: () => nav, contains: el => el === gh || nav.contains(el)});
const doc = listeners();
global.document = Object.assign(doc, {documentElement: root, querySelector: s => s === '.site-bar' ? bar : null,
  querySelectorAll: () => [], getElementById: () => null, activeElement: null});
let mobile = true;
global.matchMedia = () => ({get matches() { return mobile; }});
global.scrollY = 500; global.innerHeight = 600;
const win = {};
global.addEventListener = (n, f) => { win[n] = f; };
global.requestAnimationFrame = f => f();
function focus(el) {
  const from = document.activeElement;
  document.activeElement = el;
  if (from && bar.contains(from) && bar.handlers.focusout) bar.handlers.focusout({target: from, relatedTarget: el});
  if (el && bar.contains(el) && bar.handlers.focusin) bar.handlers.focusin({target: el, relatedTarget: from});
}
const scrollDown = () => { root.dataset.scroll = 'down'; global.scrollY += 10; win.scroll(); };
const hidden = () => links.map(l => l.hidden);
require(process.argv[1]);
"""

    def run_bar(self, script):
        done = subprocess.run([shutil.which('node'), '-e', self.BAR_DOM + script, str(SITE / 'js/site.js')],
                              capture_output=True, text=True)
        if done.returncode:
            self.fail(' | '.join(l.strip() for l in done.stderr.splitlines()
                                 if re.search(r'Error|message:|actual:|expected:', l)) or done.stderr[-600:])

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for the navigation behavior test')
    def test_the_phone_bar_never_folds(self):
        self.run_bar(r"""
mobile = true;
focus(null);
for (let i = 0; i < 5; i++) { scrollDown(); assert.deepStrictEqual(hidden(), [false, false, false], 'reading down on a phone'); }
root.dataset.scroll = 'up'; win.scroll();
assert.deepStrictEqual(hidden(), [false, false, false]);
""")

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for the navigation behavior test')
    def test_the_whole_navigation_stays_while_focus_is_in_the_bar(self):
        self.run_bar(r"""
mobile = false;
focus(null);
scrollDown();
assert.deepStrictEqual(hidden(), [true, true, false], 'folded while reading down');
// focus on Examples, then keep scrolling down: every link stays, not only the focused one
focus(links[1]);
assert.deepStrictEqual(hidden(), [false, false, false], 'focus came in');
assert.notStrictEqual(root.dataset.siteState, 'down');
scrollDown(); scrollDown();
assert.deepStrictEqual(hidden(), [false, false, false], 'scrolling down with focus on Examples');
focus(links[2]); scrollDown();
assert.deepStrictEqual(hidden(), [false, false, false], 'focus on Docs');
focus(gh); scrollDown();
assert.deepStrictEqual(hidden(), [false, false, false], 'focus on the GitHub mark');
// focus leaves the bar: reading down folds it again
focus(outside); scrollDown();
assert.deepStrictEqual(hidden(), [true, true, false], 'focus left');
assert.strictEqual(root.dataset.siteState, 'down');
""")
        # v8 narrows html[data-scroll="down"] bars to 560px (the owner keeps that); the site's "up" state (focus
        # in the bar) wins with the same specificity, later in the cascade, and is as wide as the footer (F3)
        css = (SITE / 'css/site.css').read_text(encoding='utf-8')
        self.assertRegex(css, r'html\[data-site-state="up"\] \.site-bar\[data-collapsible\][^{]*\{\s*width:\s*var\(--bar-wide\)')
        self.assertIn('--bar-wide: min(100% - 2 * var(--space-3), 1200px - 2 * var(--space-6) + 2 * var(--space-3));', css)
        self.assertRegex(css, r'\.site-bar\s*\{[^}]*padding-inline:\s*var\(--space-3\)')

    def test_every_header_has_home_examples_docs_and_a_github_link(self):
        for name in SITE_PAGES:
            with self.subTest(page=name):
                headers = list(tree(name).all('header', 'site-bar'))
                self.assertEqual(len(headers), 1)
                self.assertEqual([a.text() for nav in headers[0].all('nav') for a in nav.all('a')],
                                 ['Home', 'Examples', 'Docs'])
                self.assertEqual(len([a for a in headers[0].all('a') if a.attrs.get('href') == GITHUB]), 1)
                # F1: the wordmark alone, no icon; F4: no menu button
                lockup = next(headers[0].all('a', 'lockup'))
                self.assertEqual(list(lockup.all('img')), [])
                self.assertEqual([u.attrs.get('href', '').rsplit('#', 1)[-1] for u in lockup.all('use')], ['wouf-word'])
                self.assertEqual(list(headers[0].all('button')), [])
                self.assertNotIn('How it works</a>', (SITE / name).read_text(encoding='utf-8').split('</header>')[0])

    def test_github_links_in_header_and_footer_are_the_local_mark(self):
        icons = (SITE / 'icons.svg').read_text(encoding='utf-8')
        self.assertRegex(icons, r'<symbol id="github"[^>]*>')
        self.assertEqual(stray_hosts(icons.replace('http://www.w3.org/2000/svg', '')), [])  # the namespace is no request
        for name in SITE_PAGES:
            root = tree(name)
            for part in list(root.all('header', 'site-bar')) + list(root.all('footer', 'site-foot')):
                for a in part.all('a'):
                    if a.attrs.get('href') != GITHUB:
                        continue
                    with self.subTest(page=name, part=part.tag):
                        self.assertEqual(a.attrs.get('aria-label'), GITHUB_LABEL)
                        self.assertNotIn('btn-filled', (a.attrs.get('class') or '').split())
                        self.assertEqual(a.text(), '')
                        uses = [u.attrs.get('href', '') for u in a.all('use')]
                        self.assertEqual(len(uses), 1)
                        self.assertFalse(urlsplit(uses[0]).scheme or uses[0].startswith('//'), uses[0])
                        self.assertEqual(uses[0].rsplit('#', 1), [('/' if name == '404.html' else '') + 'icons.svg', 'github'])

    def test_vendored_v8_digests(self):
        digests = {
            'css/materials.css': '5dd2bfd8f6892e19d6e89188803f9191281196a68830cfdbe828abad508f2036',
            'css/components.css': '24893798ef0a773677b8845dc51746a3c4f97bfbd5b02428c15a7898c66e5b6b',
            'css/patterns.css': 'fbe0825041943fb0a0f4e4dc08fd7c811ebea23d0a28b99475ded584b1b110cc',
            'js/vt.js': '3ad3127156b707be71c381b96a0c696e140a0029938969aa76bfdf490d13f468',
            'js/vaiev.js': 'b8ccd10c69a25cd03a9e810f6ebd759df8948e80d9a0ab953db2844423fe3dc9',
        }
        for name, digest in digests.items():
            self.assertEqual(hashlib.sha256((SITE / name).read_bytes()).hexdigest(), digest, name)
            snapshot = SITE.parent.parent / 'in' / 'ui-v8' / name
            if snapshot.is_file():
                self.assertEqual((SITE / name).read_bytes(), snapshot.read_bytes(), name)
        for trimmed in ('css/base.css', 'css/tokens.css'):
            raw = (SITE / trimmed).read_bytes()
            self.assertIsNone(OTHER_BRANDS.search(raw), trimmed)
            for ref in re.findall(r'url\(\s*["\']?([^"\')]+)', raw.decode('utf-8')):
                if not (ref.startswith('data:') or ref.startswith('#')):
                    self.assertTrue(((SITE / trimmed).parent / urlsplit(ref).path).is_file(), f'{trimmed}: {ref}')

    # F7: the three hero dialogs exactly as approved in CLAIMS.md (illustrative scenarios, not transcripts)
    DIALOGS = {
        'project': ['Help me plan our community event.', "I'll ask the worker to draft a plan and the review bot to check it.",
                    'What date should the plan use?', 'June 12.',
                    'The reviewed draft is ready for you: a plan and decision notes in the task files.'],
        'team': ['Organize our volunteer tasks.', "I'll have the worker prepare a task list and review check it.",
                 'Who should own the welcome desk?', 'Sam.', 'The reviewed task list is ready in the task files, with Sam as owner.'],
        'code': ['Add a check for missing titles.', "I'll brief worker and ask review to check the change.",
                 'Should an empty title be rejected?', 'Yes.',
                 'The reviewed change and its test are ready on the task branch for your decision.'],
    }

    def test_home_hero_and_setup(self):
        root = tree('index.html')
        hero = next(root.all('section', 'hero'))
        # F10: the title and the demo only; no lead, buttons or status pill in the hero
        self.assertEqual([(n.tag, n.attrs.get('class')) for n in hero.children if isinstance(n, Node)],
                         [('div', 'hero-copy'), ('figure', 'demo')])
        self.assertEqual([n for n in hero.all() if set((n.attrs.get('class') or '').split()) & {'lead', 'btn', 'status-pill'}], [])
        self.assertIn('An agent team in a folder.', hero.text())
        # final round: no setup link in the hero; a cue leads on to how it works
        self.assertNotIn('own folder', hero.text())
        self.assertEqual([a.attrs.get('href') for a in hero.all('a') if a.attrs.get('href', '').startswith('#')], ['#how'])
        demo = next(hero.all('figure', 'demo'))
        self.assertIn('data-demo', demo.attrs)
        dialogs = {d.attrs.get('data-scenario'): [next(m.all('p')).text() for m in d.all('li', 'msg')] for d in demo.all('ol', 'demo-dialog')}
        self.assertEqual(dialogs, self.DIALOGS)
        self.assertEqual([c.attrs.get('data-scenario') for c in demo.all('button', 'chip')], ['project', 'team', 'code'])
        self.assertEqual([d.attrs.get('data-scenario') for d in demo.all('ol') if 'data-active' in d.attrs], ['project'])
        self.assertIn('not a transcript', demo.text())
        setup_nodes = [n for n in root.all() if n.attrs.get('id') == 'setup-line']
        self.assertEqual(len(setup_nodes), 1)
        expected_sentence = ('Set up the agent team from https://github.com/vaiev/wouf/blob/main/INSTALL.md in this folder. '
                             'I am not technical: explain in plain words and do the steps for me.')
        self.assertEqual(setup_nodes[0].text(), expected_sentence)
        self.assertEqual(len([n for n in root.all('button') if n.attrs.get('data-copy') == '#setup-line']), 2)
        start = next(n for n in root.all('section') if n.attrs.get('id') == 'start')
        self.assertEqual(len([li for li in next(start.all('ol', 'path')).children if isinstance(li, Node)]), 3)
        # F20: a calm way into the terminal for Mac and Windows, with no untested time promise
        help_text = next(start.all('details', 'terminal-help')).text()
        self.assertIn('On a Mac', help_text)
        self.assertIn('On Windows', help_text)
        self.assertNotRegex(start.text(), r'\b\d+\s*(?:min|minute|second)')
        # F20 also applies to the docs quickstart: the same Mac and Windows way in, no time promise
        docs = tree('docs.html')
        article = next(docs.all('article'))
        quick_nodes = []
        for node in article.children:
            if isinstance(node, str):
                continue
            if node.attrs.get('id') == 'getting-started-set-it-up':
                quick_nodes.append(node)
            elif quick_nodes:
                if re.fullmatch(r'h[1-3]', node.tag):
                    break
                quick_nodes.append(node)
        self.assertTrue(quick_nodes, 'generated setup section must exist')
        quick = ' '.join(node.text() for node in quick_nodes)
        self.assertIn('On a Mac', quick)
        self.assertIn('On Windows', quick)
        self.assertIn('Open in Terminal', quick)
        self.assertNotRegex(quick, r'\b\d+\s*(?:min|minute|second)')
        self.assertEqual(next(docs.all('details', 'terminal-help')).text(), help_text)
        self.assertEqual(next(n for n in docs.all() if n.attrs.get('id') == 'setup-line').text(), expected_sentence)
        self.assertEqual([n.tag for n in next(tree('404.html').all('section', 'hero')).children if isinstance(n, Node)], ['div'])
        # examples.html is no placeholder any more: tests/test_site_examples.py pins it (F18)

    def test_changelog_and_contribute_have_generated_content(self):
        for name, title in (('changelog.html', 'Changelog'), ('contribute.html', 'Contribute')):
            with self.subTest(page=name):
                article = next(tree(name).all('article'))
                self.assertEqual([n.text() for n in article.all('h1')], [title])
                self.assertTrue(list(article.all('h2')), 'source sections must be present')
                self.assertTrue(list(article.all('p')), 'source prose must be present')
                self.assertNotIn('Content coming soon', article.text())

    def test_index_and_docs_carry_their_social_previews(self):
        seen = {}
        for name in ('index.html', 'docs.html'):
            with self.subTest(page=name):
                meta = metas(name)
                canonical = [a.get('href') for t, a in parse(SITE / name).tags if t == 'link' and a.get('rel') == 'canonical']
                self.assertEqual(meta.get('og:url'), canonical[0])
                for key in ('og:title', 'og:description', 'og:image:alt'):
                    self.assertTrue((meta.get(key) or '').strip(), key)
                self.assertEqual(meta.get('og:type'), 'website')
                self.assertEqual(meta.get('og:site_name'), 'WOUF')
                self.assertEqual(meta.get('og:locale'), 'en_US')
                self.assertEqual(meta.get('og:image'), OG_IMAGE)
                self.assertEqual(meta.get('og:image:width'), '1200')
                self.assertEqual(meta.get('og:image:height'), '630')
                self.assertEqual(meta.get('twitter:card'), 'summary_large_image')
                self.assertEqual(meta.get('twitter:title'), meta.get('og:title'))
                self.assertEqual(meta.get('twitter:description'), meta.get('og:description'))
                self.assertEqual(meta.get('twitter:image'), OG_IMAGE)
                # the dark --surface token, oklch(0.155 0.01 270), in sRGB
                self.assertEqual(meta.get('theme-color'), '#0a0c11')
                touch = [a.get('href') for t, a in parse(SITE / name).tags if t == 'link' and a.get('rel') == 'apple-touch-icon']
                self.assertEqual(touch, ['apple-touch-icon.png'])
                # og:url names the page itself; og:image (and twitter:image) is the only wouf.dev resource
                for key, value in meta.items():
                    if (urlsplit(value or '').hostname or '') == 'wouf.dev':
                        self.assertIn(value, (OG_IMAGE, meta['og:url']) if key == 'og:url' else (OG_IMAGE,), key)
                seen[name] = (meta['og:title'], meta['og:description'], meta['og:url'])
        for a, b in zip(*seen.values()):
            self.assertNotEqual(a, b)
        self.assertEqual(metas('404.html').get('robots'), 'noindex')

    def test_the_og_card_is_a_page_of_its_own(self):
        card = SITE / 'og-card.html'
        self.assertTrue(card.is_file())
        self.assertNotIn('script', [tag for tag, _ in parse(card).tags])
        for page in pages():
            for tag, attrs in parse(page).tags:
                self.assertNotIn('og-card', attrs.get('href') or '', page.name)

    def test_scripts_stylesheets_and_fonts_come_from_the_site(self):
        for page in pages():
            for tag, attrs in parse(page).tags:
                ref = None
                if tag == 'script':
                    ref = attrs.get('src')
                    self.assertTrue(ref, f'{page.name}: inline script, the CSP would block it')
                elif tag == 'link' and set((attrs.get('rel') or '').split()) & {'stylesheet', 'preload', 'icon'}:
                    ref = attrs.get('href')
                if ref:
                    with self.subTest(page=page.name, ref=ref):
                        self.assertFalse(urlsplit(ref).scheme or ref.startswith('//'), f'{page.name}: {ref}')
        fonts = (SITE / 'css' / 'site-fonts.css').read_text(encoding='utf-8')
        for ref in re.findall(r'url\(\s*["\']?([^"\')]+)', fonts):
            self.assertTrue(ref.startswith('../fonts/'), ref)

    def test_each_page_has_a_csp_one_h1_a_lang_a_title_and_no_form(self):
        for page in pages():
            with self.subTest(page=page.name):
                parsed = parse(page)
                html = [attrs for tag, attrs in parsed.tags if tag == 'html']
                self.assertEqual(len(html), 1)
                self.assertTrue(html[0].get('lang'))
                self.assertEqual(html[0].get('data-brand'), 'wouf')
                csp = [attrs.get('content', '') for tag, attrs in parsed.tags
                       if tag == 'meta' and (attrs.get('http-equiv') or '').lower() == 'content-security-policy']
                self.assertEqual(len(csp), 1)
                self.assertIn("default-src 'self'", csp[0])
                self.assertEqual(parsed.h1, 1)
                self.assertTrue(parsed.title.strip())
                self.assertNotIn('form', [tag for tag, _ in parsed.tags])

    def test_the_folder_names_no_other_brand_and_not_the_companion(self):
        for path in SITE.rglob('*'):
            if path.is_file() and path.relative_to(SITE).as_posix() not in VENDOR:
                with self.subTest(file=str(path.relative_to(SITE))):
                    self.assertIsNone(OTHER_BRANDS.search(path.name.encode()))
                    self.assertIsNone(OTHER_BRANDS.search(path.read_bytes()))
        for page in pages():
            self.assertNotRegex(page.read_text(encoding='utf-8').lower(), r'companion|wouf-app')

    def test_wouf_keeps_every_token_the_css_uses(self):
        tokens = (SITE / 'css' / 'tokens.css').read_text(encoding='utf-8')
        self.assertEqual(set(re.findall(r'\[data-brand="([^"]+)"\]', tokens)), {'wouf'})
        self.assertIn('[data-brand="wouf"][data-theme="dark"]', tokens)
        self.assertIn('[data-brand="wouf"]:not([data-theme="light"])', tokens)
        css = ''.join(sheet.read_text(encoding='utf-8') for sheet in stylesheets())
        defined = set(re.findall(r'(--[\w-]+)\s*:', css))
        defined |= set(re.findall(r"setProperty\('(--[\w-]+)", (SITE / 'js' / 'vaiev.js').read_text(encoding='utf-8')))
        used = set(re.findall(r'var\((--[\w-]+)\s*\)', css))
        # post tones are opt-in (materials.css: "any brand with --post-* tokens"); wouf never had them
        self.assertEqual({name for name in used - defined if not name.startswith('--post-')}, set())


if __name__ == '__main__':
    unittest.main()
