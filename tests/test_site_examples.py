"""site/examples.html (T-0115, F18): four examples as a spectrum, from a project lead in an authority to a developer.
Each tells its starting point, plays a dialog in the site's own demo component, says what lies in the folder at the
end, which bots took part, what you must settle yourself, and links into the docs. Every one is marked as an
illustrated scenario, and the page keeps to the binding legal frame: no compliance promise."""
import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path

SITE = Path(__file__).resolve().parents[1] / 'site'
ORDER = ['authority', 'club', 'company', 'developer']
FRAME = ('You decide where team, files and model run. No WOUF cloud, no WOUF server. '
         'Compliance depends on the model and how you run it.')
LOCAL = 'A compatible local model can be configured with Codex, subject to your setup and model support.'
# CLAIMS.md "Claims that must not appear" and F18's legal frame, as patterns over the page's lowercased text
BANNED = (r'\bcompliant\b', r'ensures? (?:\w+ )*compliance', r'guarantees? (?:\w+ )*compliance', r'compliance guaranteed',
          r'makes? (?:\w+ )*(?:nDSG|AI Act|CLOUD Act|compliant|lawful|legal)', r'solves? (?:\w+ )*(?:nDSG|AI Act|CLOUD Act)',
          r'cloud act (?:prohibits|forbids|bans)', r'sovereign model guarantees', r'(?:cloud models|providers?) receives? no content',
          r'never access your computer', r'all modes are sandboxed', r'all networking is filtered', r'nothing from the host is mounted',
          r'no ai cost', r'all complexity increases are blocked', r'guarantees quality', r'cannot conflict',
          r'permanently recorded', r'decisions are enforced', r'automatically trusted', r'works out of the box',
          r'every local or sovereign model', r'\bthe best\b', r'almost no other', r'masterstroke')


class Node:
    def __init__(self, tag, attrs, parent=None):
        self.tag, self.attrs, self.parent, self.children = tag, attrs, parent, []

    @property
    def classes(self):
        return (self.attrs.get('class') or '').split()

    def text(self):
        return ' '.join(''.join(c if isinstance(c, str) else ' ' + c.text() + ' ' for c in self.children).split())

    def all(self, tag=None, cls=None, attr=None):
        for child in self.children:
            if isinstance(child, Node):
                if ((tag is None or child.tag == tag) and (cls is None or cls in child.classes)
                        and (attr is None or attr in child.attrs)):
                    yield child
                yield from child.all(tag, cls, attr)

    def one(self, tag=None, cls=None, attr=None):
        found = list(self.all(tag, cls, attr))
        assert len(found) == 1, (tag, cls, attr, len(found))
        return found[0]


class Tree(HTMLParser):
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


def html():
    return (SITE / 'examples.html').read_text(encoding='utf-8')


def page():
    parser = Tree()
    parser.feed(html())
    return parser.root


def examples():
    return [s for s in page().all('section', 'example')]


def sheet():
    return (SITE / 'css/examples.css').read_text(encoding='utf-8')


class ExamplesPage(unittest.TestCase):
    def test_four_examples_in_order_from_authority_to_developer(self):
        found = examples()
        self.assertEqual([s.attrs.get('id') for s in found], ORDER)
        heads = [s.one('h2').text().lower() for s in found]
        for head, words in zip(heads, (('project lead', 'authority'), ('club',), ('company',), ('developer',))):
            for word in words:
                self.assertIn(word, head)
        # the jump row names the same four, in the same order
        nav = page().one('nav', 'example-nav')
        self.assertEqual([a.attrs['href'] for a in nav.all('a')], ['#' + i for i in ORDER])

    def test_each_example_has_every_part(self):
        docs_ids = set(re.findall(r'\bid="([^"]+)"', (SITE / 'docs.html').read_text(encoding='utf-8')))
        for section in examples():
            with self.subTest(example=section.attrs['id']):
                self.assertIn('Illustrated scenario', section.one(cls='example-mark').text())
                start = section.one('p', 'example-start').text()
                self.assertEqual(len(re.findall(r'[.!?](?:\s|$)', start)), 1, start)  # one sentence
                # the dialog: the site's demo component, one dialog that is already in the page, five lines or more
                demo = section.one('figure', 'demo', 'data-demo')
                dialog = demo.one('ol', 'demo-dialog')
                self.assertIn('data-active', dialog.attrs)
                self.assertTrue(dialog.attrs.get('aria-label'))
                kinds = [next(c for c in ('msg-you', 'msg-ask', 'msg-done', 'msg-lead') if c in li.classes)
                         for li in dialog.all('li', 'msg')]
                self.assertGreaterEqual(len(kinds), 5)
                self.assertEqual(kinds[0], 'msg-you')
                for kind in ('msg-ask', 'msg-done'):
                    self.assertIn(kind, kinds)
                for li in dialog.all('li', 'msg'):
                    self.assertTrue(li.one('span', 'who').text())
                    self.assertTrue(li.one('p').text())
                demo.one(attr='data-demo-typed')
                demo.one('button', attr='data-demo-toggle')
                demo.one('button', attr='data-demo-restart')
                self.assertIn('illustrated scenario', demo.one('figcaption', 'demo-note').text().lower())
                # the outcome, the bots, your own part, the docs
                for cls in ('example-folder', 'example-bots', 'example-own'):
                    part = section.one(cls=cls)
                    self.assertGreaterEqual(len(list(part.all('li'))), 1, cls)
                self.assertEqual(section.one(cls='example-own').one('h3').text(), 'What you must settle yourself')
                self.assertIn('lead', section.one(cls='example-bots').text().lower())
                docs = [a.attrs['href'] for a in section.all('a') if a.attrs.get('href', '').startswith('docs.html#')]
                self.assertEqual(len(docs), 1)
                self.assertIn(docs[0].split('#', 1)[1], docs_ids)

    def test_the_legal_frame_holds(self):
        text = ' '.join(page().one('main').text().split())
        low = text.lower()
        for pattern in BANNED:
            self.assertNotRegex(low, pattern.lower())
        # the one allowed sentence, word for word, in the authority example
        self.assertEqual(text.count(FRAME), 1)
        self.assertIn(FRAME, examples()[0].text())
        # the local-model sentence only as CLAIMS.md words it (true there, with qualification)
        if 'local model' in low:
            self.assertIn(LOCAL, text)
        # cloud models: the content goes to the provider, and the page says so
        self.assertIn("provider's model API", examples()[0].text())
        # HERMES only as an example of a phase-based project method, once, in the authority example
        self.assertEqual(text.count('HERMES'), 1)
        self.assertIn('HERMES', examples()[0].text())
        self.assertRegex(text, r'such as HERMES')
        self.assertNotRegex(low, r'hermes (?:phase|role|result|module|certif|requires|says|defines)')

    def test_same_layout_language_as_the_index(self):
        source = html()
        root = page()
        # header with the current page marked, the bottom bar comes from site.css; no cards, no caps, no numbering
        nav = re.search(r'<nav class="site-links"[^>]*>(.*?)</nav>', source, re.S).group(1)
        self.assertEqual(re.findall(r'<a [^>]*aria-current="page"[^>]*>([^<]+)</a>', nav), ['Examples'])
        self.assertNotRegex(source, r'class="[^"]*\b(?:card|plate|band-plate|node|caps|label)\b')
        self.assertNotRegex(root.one('main').text(), r'\b0\d\b|\b\d+\s*·|Step \d')
        for section in examples():
            self.assertIn('band', section.classes)
            self.assertFalse(list(section.all('section', 'band')))
        css = sheet()
        self.assertNotIn('uppercase', css)
        self.assertNotRegex(css, r'--fs-(?:label-s|label-m|body-s)\b|font-size:\s*1[0-3]px')
        self.assertNotRegex(re.sub(r'/\*.*?\*/', '', css, flags=re.S), r'gradient|box-shadow|position:\s*fixed|\d+d?s?l?vh')

    def test_the_demo_is_reused_not_copied(self):
        source = html()
        # site.js plays every [data-demo]; the page adds only its own stylesheet and script
        for ref in ('css/site.css', 'css/examples.css', 'js/site.js', 'js/examples.js'):
            self.assertIn(ref, source)
        self.assertRegex(source, r'<script src="js/examples\.js" defer></script>')
        js = (SITE / 'js/examples.js').read_text(encoding='utf-8')
        self.assertNotRegex(js, r'data-demo-typed|data-shown|\.demo-dialog')
        self.assertNotRegex(sheet(), r'(?m)^\s*\.(?:msg|demo-compose|demo-typed|demo-dialog)\b')
        # CSP-safe, local only: no inline style or script, no handler attributes, no other host
        self.assertNotRegex(source, r'\sstyle=|<style|\son\w+=|<script(?![^>]*\ssrc=)')
        hosts = set(re.findall(r'(?:https?:)?//([a-z0-9.-]+)', source + js + sheet(), re.I))
        self.assertLessEqual(hosts, {'github.com', 'wouf.dev', 'vaiev.com'})

    def test_reduced_motion_shows_finished_dialogs(self):
        css = re.sub(r'/\*.*?\*/', '', sheet(), flags=re.S)
        calm = re.findall(r'@media \(prefers-reduced-motion: no-preference\)\s*\{', css)
        outside = css
        for start in calm:
            i = outside.index(start)
            depth, j = 0, i
            while True:
                j += 1
                if outside[j] == '{':
                    depth += 1
                elif outside[j] == '}':
                    depth -= 1
                    if depth == 0:
                        break
            outside = outside[:i] + outside[j + 1:]
        self.assertEqual(re.findall(r'(?:animation|transition)(?:-name)?:\s*(?!none)[^;]+', outside), [])
        # site.js shows every line at once with reduced motion; the page does not hide lines on its own
        self.assertNotRegex(css, r'\.msg[^{]*\{[^}]*(?:opacity|visibility)')

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for the jump row test')
    def test_the_jump_row_marks_the_example_in_view(self):
        script = r"""
const assert = require('assert');
const ids = %s;
let y = 0;
const heads = Object.fromEntries(ids.map((id, i) => [id, {getBoundingClientRect: () => ({top: 300 + i * 1000 - y})}]));
const links = ids.map(id => ({attrs: {}, getAttribute(n) { return n === 'href' ? '#' + id : this.attrs[n]; },
  setAttribute(n, v) { this.attrs[n] = v; }, removeAttribute(n) { delete this.attrs[n]; }}));
const handlers = {};
global.document = {querySelector: s => s === '.example-nav' ? {querySelectorAll: () => links} : null,
  getElementById: id => heads[id], documentElement: {scrollHeight: 4600}};
global.matchMedia = () => ({matches: false});
global.addEventListener = (n, f) => { handlers[n] = f; };
global.requestAnimationFrame = f => f();
global.innerHeight = 800;
Object.defineProperty(global, 'scrollY', {get: () => y});
require(process.argv[1]);
const at = top => { y = top; handlers.scroll(); return links.filter(l => l.attrs['aria-current']).map(l => l.getAttribute('href')); };
assert.deepStrictEqual(at(0), ['#authority']);
assert.deepStrictEqual(at(1300 - 200), ['#club']);
assert.deepStrictEqual(at(2300 - 200), ['#company']);
assert.deepStrictEqual(at(1300 - 400), ['#authority']);  // and back up again
assert.deepStrictEqual(at(4600 - 800), ['#developer']);  // at the very bottom: the last one
""" % repr(ORDER).replace("'", '"')
        subprocess.run([shutil.which('node'), '-e', script, str(SITE / 'js/examples.js')], check=True,
                       capture_output=True, text=True)


if __name__ == '__main__':
    unittest.main()
