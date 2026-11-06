"""wouf.dev on VAIEV UI v8 (T-0111, round 2): a full first screen with the demo, flat glass bands with a real lock
that keeps every band reachable, a grouped docs nav that marks the current section, and the examples placeholder."""
import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path

SITE = Path(__file__).resolve().parents[1] / 'site'
DOC_SECTIONS = ['overview', 'method', 'safety', 'getting-started', 'limits', 'sources']


class LandingParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.bands = []
        self.ids = set()
        self.text = []
        self.card_icons = []
        self.copy_targets = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get('class', '').split()
        self.ids.add(attrs.get('id'))
        if tag == 'section' and 'band' in classes:
            self.bands.append({'labels': [], 'headings': [], 'first': None})
        elif self.bands and self.bands[-1]['first'] is None and self.stack and self.stack[-1][0] == 'section':
            self.bands[-1]['first'] = (tag, classes, attrs.get('aria-hidden'))
        if tag == 'button' and 'data-copy' in attrs:
            self.copy_targets.append(attrs['data-copy'])
        # every card, glass or flat, and every step or limit tile counts: none of them carries an icon
        card = 'card' in classes or (tag == 'li' and bool({'plate', 'plane'} & set(classes)))
        if (tag in ('svg', 'use', 'img') or 'icon' in classes) and any(x[2] for x in self.stack):
            self.card_icons.append(tag)
        if tag not in ('meta', 'link', 'img', 'br', 'input', 'use'):
            self.stack.append((tag, classes, card))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.text.append(data)
        if self.bands and self.stack and any(t == 'section' and 'band' in c for t, c, _ in self.stack):
            tag, classes, _ = self.stack[-1]
            if 'band-index' in classes:
                self.bands[-1]['labels'].append(data.strip())
            if tag == 'h2':
                self.bands[-1]['headings'].append(data.strip())


def css():
    return (SITE / 'css/site.css').read_text(encoding='utf-8')


def blocks(text, opener):
    """The bodies of every block whose prelude starts with `opener` (brace-matched)."""
    found, at = [], 0
    while (start := text.find(opener, at)) != -1:
        depth, i = 0, text.index('{', start)
        for j in range(i, len(text)):
            depth += {'{': 1, '}': -1}.get(text[j], 0)
            if depth == 0:
                found.append(text[i + 1:j])
                break
        at = j + 1
    return found


LOCK = '@media (prefers-reduced-motion: no-preference)'


class SiteRedesignTest(unittest.TestCase):
    def test_landing_index_and_content(self):
        parser = LandingParser()
        html = (SITE / 'index.html').read_text(encoding='utf-8')
        parser.feed(html)
        # F16: demo, how it works, five strengths, get started, limits, it builds itself (with the sentence)
        # final round: five bands, none numbered (no section index, no step counter)
        self.assertEqual(len(parser.bands), 5)
        self.assertEqual([b['labels'] for b in parser.bands], [[] for _ in range(5)])
        self.assertNotIn('band-index', html)
        for kicker in re.findall(r'<p class="band-kicker">(.*?)</p>', html):
            self.assertNotRegex(re.sub(r'<[^>]+>', '', kicker), r'\d')
        self.assertNotRegex(css(), r'counter-(?:reset|increment)\s*:\s*step|counter\(step')
        self.assertTrue(all(len(b['headings']) == 1 for b in parser.bands))
        self.assertTrue({'how', 'strengths', 'start', 'limits', 'built', 'setup-line'} <= parser.ids)
        self.assertEqual(parser.copy_targets, ['#setup-line', '#setup-line'])
        text = ' '.join(parser.text)
        for phrase in ('It builds itself.', 'Have an AI assistant for the terminal',
                       'Make a folder and open it in the terminal', 'Paste one sentence'):
            self.assertIn(phrase, text)
        self.assertEqual(parser.card_icons, [])
        # F17: the five strengths word for word as approved in CLAIMS.md
        for claim in ("Choose a sandboxed setup to give each bot its own workspace, separate from your project and home folders.",
                      "The gate command you configure, your tests for example, runs before review without an AI call.",
                      "For a batch, WOUF checks that the tasks' declared file areas do not overlap before starting them together.",
                      "Teams can exchange signed handoffs through a shared Git repository, without a dedicated WOUF server.",
                      "Team events and readable summaries can live in your Git repository, so you can follow the work over time."):
            self.assertIn(claim, text)
        # nothing from CLAIMS.md's "must not appear" list, and no legal promise
        for page in ('index.html', 'docs.html', 'examples.html'):
            low = (SITE / page).read_text(encoding='utf-8').lower()
            for banned in ('never mounted', 'nothing of your computer', 'all modes are sandboxed', 'no ai cost',
                           'guarantees quality', 'cannot conflict', 'permanently recorded', 'almost no other',
                           'masterstroke', 'compliant', 'ensures compliance', 'cloud act prohibits'):
                self.assertNotIn(banned, low, page)
        # F13: no cards in the bands; F12: no v8 plate (gradient, light edge) and no lip
        self.assertNotRegex(html, r'class="[^"]*\b(?:card|plate|band-plate|band-lip|node)\b')
        self.assertEqual(len(re.findall(r'</section>\s*<section class="band', html)), 4)
        # "It builds itself" without hashes or counts of the development history: the public export has a fresh one
        for private in ('e838ca4', 'f968367', '75e55ad', '73 of the 134', 'git log'):
            self.assertNotIn(private, html)
        self.assertIn('In its development repository, more than half of the commits name the task they came from.', text)

    def test_only_the_home_hero_takes_the_screen(self):
        sheet = re.sub(r'/\*.*?\*/', '', css(), flags=re.S)
        # F6: the home hero fills the first screen; every other section takes the height of its content
        for rule in re.findall(r'([^{}]+)\{[^{}]*\d+d?s?l?vh[^{}]*\}', sheet):
            self.assertIn(rule.strip(), ('.home-hero', '.sky'))
        self.assertRegex(re.search(r'\.hero\s*\{([^}]*)\}', sheet).group(1), r'min-height:\s*0')
        self.assertNotRegex(sheet, r'scroll-snap|position:\s*fixed')

    def test_bands_are_flat_glass_with_clear_air(self):
        sheet = css()
        band = re.search(r'\n\.band\s*\{([^}]*)\}', sheet).group(1)
        self.assertNotRegex(band, r'gradient|box-shadow')
        self.assertRegex(band, r'background:\s*color-mix')
        self.assertNotIn('band-lip', sheet)
        gap = re.search(r'\.deck\s*\{[^}]*gap:\s*var\(--space-(\d+)\)', sheet).group(1)
        self.assertGreaterEqual(int(gap), 12)
        self.assertNotRegex(sheet, r'\.band \+ \.band\s*\{[^}]*margin-top:\s*calc')

    def test_the_lock_sticks_whole_bands_only_where_it_is_safe(self):
        sheet = css()
        stuck = re.findall(r'([^{}]+)\{[^{}]*position:\s*sticky[^{}]*\}', sheet)
        self.assertEqual([s.strip() for s in stuck], ['.deck[data-lock] > .band:not(:last-child)'])
        motion = ''.join(blocks(sheet, LOCK))
        supports = blocks(motion, '@supports (animation-timeline: view())')
        self.assertEqual(len(supports), 1)
        self.assertIn('position: sticky', supports[0])
        self.assertIn('top: var(--lock-top, 100px)', supports[0])
        # keyboard focus in the deck releases the lock
        self.assertRegex(supports[0], r'\.deck\[data-lock\]:has\(:focus-visible\) > \.band\s*\{[^}]*position:\s*relative')
        # earlier bands recede: higher index for later bands, scale and a soft blur, nothing more
        for i in range(1, 6):
            self.assertRegex(sheet, rf'\.deck > \.band:nth-child\({i}\) \{{ z-index: {i};')
        frames = blocks(sheet, '@keyframes band-recede')[0]
        for scale in re.findall(r'scale\(([\d.]+)\)', frames):
            self.assertGreaterEqual(float(scale), 0.9)
        for px in re.findall(r'blur\((\d+)px\)', frames):
            self.assertLessEqual(int(px), 4)
        self.assertEqual(int(re.search(r'\.site\s*\{[^}]*padding-top:\s*(\d+)px', sheet).group(1)), 100)

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for the lock behaviour test')
    def test_a_band_taller_than_the_window_sticks_only_once_its_end_is_in_view(self):
        script = r"""
const assert = require('assert');
const mk = h => ({classList: {contains: c => c === 'band'}, offsetHeight: h, props: {}, style: {setProperty(n, v) { this.owner.props[n] = v; }}});
const bands = [mk(600), mk(1800), mk(500)];
bands.forEach(b => { b.style.owner = b; });
const deck = {children: bands, attrs: {}, addEventListener() {}, hasAttribute(n) { return n in this.attrs; },
  toggleAttribute(n, on) { if (on) this.attrs[n] = ''; else delete this.attrs[n]; }};
let phone = false, reduce = false;
const bar = {getBoundingClientRect: () => ({top: 844 - 16 - 60}), querySelector: () => ({querySelectorAll: () => []}), contains: () => false, addEventListener() {}};
global.document = {documentElement: {dataset: {}, hasAttribute: () => false}, querySelector: s => s === '.deck' ? deck : s === '.site-bar' ? bar : null,
  querySelectorAll: () => [], getElementById: () => null, addEventListener() {}};
global.matchMedia = q => ({get matches() { return q.includes('reduce') ? reduce : q.includes('max-width') ? phone : false; }, addEventListener() {}});
global.CSS = {supports: () => true};
global.addEventListener = () => {};
global.requestAnimationFrame = f => f();
global.innerHeight = 900; global.scrollY = 0;
require(process.argv[1]);
assert.ok(deck.hasAttribute('data-lock'));
assert.strictEqual(bands[0].props['--lock-top'], '100px');       // short: right under the bar
assert.strictEqual(bands[1].props['--lock-top'], (900 - 16 - 1800) + 'px');  // tall: its end 16px above the bottom
"""
        subprocess.run([shutil.which('node'), '-e', script, str(SITE / 'js/site.js')], check=True, capture_output=True, text=True)
        # with reduced motion or without scroll timelines there is no lock at all
        for setup in ("reduce = true;", "global.CSS = {supports: () => false};"):
            calm = script.replace("require(process.argv[1]);", setup + "\nrequire(process.argv[1]);").split('assert.ok(')[0]
            calm += "assert.ok(!deck.hasAttribute('data-lock'));\n"
            subprocess.run([shutil.which('node'), '-e', calm, str(SITE / 'js/site.js')], check=True, capture_output=True, text=True)

    def test_site_animations_run_only_without_reduced_motion(self):
        sheet = css()
        for body in blocks(sheet, '@media (prefers-reduced-motion: no-preference)'):
            sheet = sheet.replace(body, '')
        for keyframes in blocks(sheet, '@keyframes'):
            sheet = sheet.replace(keyframes, '')
        self.assertEqual(re.findall(r'animation(?:-name)?:\s*(?!none)[^;]+', sheet), [])

    def test_no_caps_and_no_small_text(self):
        # F14: no uppercase labels, nothing under 14px from the site's own styles
        sheet = css()
        self.assertNotIn('uppercase', sheet)
        self.assertNotRegex(sheet, r'--fs-(?:label-s|label-m|body-s)\b|font-size:\s*1[0-3]px')
        for page in SITE.glob('*.html'):
            self.assertNotRegex(page.read_text(encoding='utf-8'), r'class="[^"]*\bcaps\b', page.name)

    def test_docs_nav_is_grouped_and_follows_the_article(self):
        from test_site import tree
        root = tree('docs.html')
        nav = next(root.all('nav', 'docs-nav'))
        groups = list(nav.all('div', 'docs-nav-group'))
        self.assertEqual([next(g.all('a')).attrs['href'] for g in groups], ['#' + id for id in DOC_SECTIONS])
        self.assertEqual([next(g.all('p', 'docs-group')).text() for g in groups],
                         ['Overview', 'Method', 'Safety', 'Getting started', 'Limits', 'Sources'])
        article = next(root.all('article'))
        self.assertEqual([n.attrs['id'] for n in article.all('h2')], DOC_SECTIONS)
        # Each group follows its part's headings in reading order; Sources has only its top-level link.
        expected, part = {}, None
        for heading in article.all():
            if heading.tag == 'h2':
                part = heading.attrs['id']
                expected[part] = [part]
            elif heading.tag == 'h3' and part != 'sources':
                expected[part].append(heading.attrs['id'])
        for group, part in zip(groups, DOC_SECTIONS):
            self.assertEqual([a.attrs['href'] for a in group.all('a')], ['#' + id for id in expected[part]])
        self.assertRegex(css(), r'\.docs-nav a\[aria-current="location"\]')

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for the docs behaviour test')
    def test_docs_marks_exactly_one_current_section(self):
        script = r"""
const assert = require('assert');
const ids = %s;
const el = (extra) => Object.assign({attrs: {}, handlers: {}, addEventListener(n, f) { this.handlers[n] = f; },
  setAttribute(n, v) { this.attrs[n] = String(v); }, removeAttribute(n) { delete this.attrs[n]; },
  hasAttribute(n) { return n in this.attrs; }, getAttribute(n) { return this.attrs[n] ?? null; },
  toggleAttribute(n, on) { on ? this.setAttribute(n, '') : this.removeAttribute(n); }}, extra);
let y = 0;
const heads = Object.fromEntries(ids.map((id, i) => [id, el({id, getBoundingClientRect: () => ({top: 600 + i * 800 - y})})]));
const links = ids.map((id, i) => el({offsetLeft: i * 120, offsetWidth: 110}));
links.forEach((a, i) => a.setAttribute('href', '#' + ids[i]));
const docsNav = el({scrollWidth: 400, clientWidth: 400, scrollLeft: 0, scrollTo() {}, querySelectorAll: () => links});
const root = {dataset: {}, scrollHeight: 9000};
global.document = {documentElement: root, querySelector: s => s === '.docs-nav' ? docsNav : null,
  querySelectorAll: () => [], getElementById: id => heads[id] || null, activeElement: null};
const handlers = {};
global.addEventListener = (n, f) => { handlers[n] = f; };
global.matchMedia = () => ({matches: false});
global.requestAnimationFrame = f => f();
global.innerHeight = 800;
Object.defineProperty(global, 'scrollY', {get: () => y});
require(process.argv[1]);
const current = () => links.filter(a => a.attrs['aria-current'] === 'location').map(a => a.attrs.href);
const at = top => { y = top; handlers.scroll(); return current(); };
assert.deepStrictEqual(at(0), ['#overview']);                 // before the first heading: the first item
assert.deepStrictEqual(at(600 - 104), ['#overview']);
assert.deepStrictEqual(at(600 + 800 - 104), ['#method']);
assert.deepStrictEqual(at(600 + 3 * 800 - 90), ['#getting-started']);
assert.deepStrictEqual(at(600 + 3 * 800 - 200), ['#safety']);     // and back up again
assert.deepStrictEqual(at(9000 - 800), ['#sources']);           // at the very bottom: the last item
""" % repr(DOC_SECTIONS).replace("'", '"')
        subprocess.run([shutil.which('node'), '-e', script, str(SITE / 'js/site.js')], check=True,
                       capture_output=True, text=True)

    def test_header_marks_the_page_it_is_on(self):
        for name, label in (('index.html', 'Home'), ('docs.html', 'Docs'), ('examples.html', 'Examples')):
            nav = re.search(r'<nav class="site-links"[^>]*>(.*?)</nav>', (SITE / name).read_text(encoding='utf-8'), re.S).group(1)
            self.assertEqual(re.findall(r'<a [^>]*aria-current="page"[^>]*>([^<]+)</a>', nav), [label], name)

    def test_copy_results_are_spoken(self):
        html = (SITE / 'index.html').read_text(encoding='utf-8')
        self.assertEqual(len(re.findall(r'id="copy-status"[^>]*role="status"|role="status"[^>]*id="copy-status"', html)), 1)
        js = (SITE / 'js/site.js').read_text(encoding='utf-8')
        self.assertIn("getElementById('copy-status')", js)
        self.assertIn('selectNodeContents', js)  # when the clipboard is blocked the sentence is selected instead

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for the copy behaviour test')
    def test_each_copy_attempt_reports_its_own_result(self):
        script = r"""
const assert = require('assert');
const mk = () => ({attrs: {}, handlers: {}, dataset: {copy: '#setup-line'}, addEventListener(n, f) { this.handlers[n] = f; },
  setAttribute(n, v) { this.attrs[n] = v; }, removeAttribute(n) { delete this.attrs[n]; }, hasAttribute(n) { return n in this.attrs; }});
const a = mk(), b = mk();
const status = {textContent: ''};
const source = {textContent: ' the sentence '};
let selected = 0;
global.document = {documentElement: {dataset: {}, hasAttribute: () => false}, querySelector: s => s === '#setup-line' ? source : null,
  querySelectorAll: s => s === '[data-copy]' ? [a, b] : [], getElementById: id => id === 'copy-status' ? status : null,
  createRange: () => ({selectNodeContents() {}})};
global.getSelection = () => ({removeAllRanges() {}, addRange() { selected += 1; }});
global.matchMedia = () => ({matches: false});
global.addEventListener = () => {}; global.requestAnimationFrame = () => {}; global.scrollY = 0; global.innerHeight = 800;
const pending = [];
Object.defineProperty(global, 'navigator', {configurable: true, value: {clipboard: {writeText: text => new Promise((ok, no) => pending.push({text, ok, no}))}}});
const realTimeout = setTimeout;
global.setTimeout = (f, ms) => (ms === 50 ? f() : 0);
require(process.argv[1]);
const flush = () => new Promise(r => realTimeout(r, 0));
(async () => {
  // a slow success is reported as success, however long it takes
  a.handlers.click(); assert.strictEqual(pending[0].text, 'the sentence');
  await flush(); assert.strictEqual(status.textContent, '');
  pending[0].ok(); await flush();
  assert.match(status.textContent, /^Copied/); assert.ok(a.hasAttribute('data-done'));
  // a refusal selects the sentence and says so, even right after a success
  b.handlers.click(); assert.ok(!a.hasAttribute('data-done'));
  pending[1].no(new Error('denied')); await flush();
  assert.match(status.textContent, /not allowed/); assert.strictEqual(selected, 1); assert.ok(!b.hasAttribute('data-done'));
  // two quick presses: only the newer one speaks, whatever order the clipboard answers in
  a.handlers.click(); b.handlers.click();
  pending[3].no(new Error('denied')); await flush();
  pending[2].ok(); await flush();
  assert.match(status.textContent, /not allowed/); assert.ok(!a.hasAttribute('data-done'));
})().catch(e => { console.error(e); process.exit(1); });
"""
        subprocess.run([shutil.which('node'), '-e', script, str(SITE / 'js/site.js')], check=True, capture_output=True, text=True)


if __name__ == '__main__':
    unittest.main()
