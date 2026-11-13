"""The standard-library docs build, its source contract and checked-in output."""
import importlib.util
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import writable_copy  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/build_site_docs.py'
GENERATED = ('docs.html', 'changelog.html', 'contribute.html')


def renderer():
    spec = importlib.util.spec_from_file_location('build_site_docs', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DocsBuildTest(unittest.TestCase):
    def run_build(self, root=ROOT, check=False):
        return subprocess.run([sys.executable, str(SCRIPT), '--root', str(root)] + (['--check'] if check else []),
                              capture_output=True, text=True)

    def fixture(self, root):
        writable_copy(ROOT / 'docs/guide', root / 'docs/guide')
        (root / 'site').mkdir()
        writable_copy(ROOT / 'site/index.html', root / 'site/index.html')
        writable_copy(ROOT / 'CHANGELOG.md', root / 'CHANGELOG.md')

    def test_checked_in_pages_are_current(self):
        result = self.run_build(check=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_stale_markdown_and_changelog_and_missing_output(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root)
            self.assertNotEqual(self.run_build(root, True).returncode, 0)
            self.assertFalse((root / 'site/docs.html').exists(), '--check must not write')
            for source in ('docs/guide/overview.md', 'CHANGELOG.md'):
                self.assertEqual(self.run_build(root).returncode, 0)
                self.assertEqual(self.run_build(root, True).returncode, 0)
                output = {n: (root / 'site' / n).read_bytes() for n in GENERATED}
                self.assertEqual(self.run_build(root).returncode, 0)
                self.assertEqual(output, {n: (root / 'site' / n).read_bytes() for n in GENERATED})
                with (root / source).open('a') as stream:
                    stream.write('\nA changed source & its evidence.\n')
                self.assertNotEqual(self.run_build(root, True).returncode, 0)
                self.assertEqual(output, {n: (root / 'site' / n).read_bytes() for n in GENERATED}, '--check wrote')
                self.assertEqual(self.run_build(root).returncode, 0)
                self.assertEqual(self.run_build(root, True).returncode, 0)
            (root / 'site/contribute.html').unlink()
            self.assertNotEqual(self.run_build(root, True).returncode, 0)
            self.assertEqual(self.run_build(root).returncode, 0)
            with (root / 'site/docs.html').open('ab') as stream:
                stream.write(b'\n')
            self.assertNotEqual(self.run_build(root, True).returncode, 0, 'byte differences must fail')

    def test_new_guide_cannot_be_omitted_and_examples_are_not_written(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root)
            examples = root / 'site/examples.html'
            examples.write_text('Owned by the examples package.\n')
            self.assertEqual(self.run_build(root).returncode, 0)
            self.assertEqual(examples.read_text(), 'Owned by the examples package.\n')
            (root / 'docs/guide/new.md').write_text('# New\n')
            result = self.run_build(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('unexpected guide filename: new.md', result.stderr)

    def test_each_document_starts_with_exactly_one_h1(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root)
            source = root / 'docs/guide/method.md'
            for content in ('No title\n', '# Method\n\n# Another title\n'):
                source.write_text(content)
                result = self.run_build(root)
                self.assertNotEqual(result.returncode, 0)
                self.assertRegex(result.stderr, r'method.md:\d+:')

    def test_relative_source_links_resolve_across_pages_and_duplicate_headings(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root)
            (root / 'docs/guide/overview.md').write_text(
                '# Overview\n\n## Same!\n\n## Same!\n\n'
                '[Again](#same-2), [method](method.md#one-lead), '
                '[contribute](contribute.md#license), [changes](../../CHANGELOG.md#010-2026-11-06).\n')
            result = self.run_build(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            html = (root / 'site/docs.html').read_text()
            for ref in ('docs.html#overview-same-2', 'docs.html#method-one-lead',
                        'contribute.html#license', 'changelog.html#010-2026-11-06'):
                self.assertIn('href="' + ref + '"', html)

    def test_source_fragment_is_preserved_when_its_html_id_is_reserved(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root)
            (root / 'CHANGELOG.md').write_text('# Top\n\n[Heading](#top)\n')
            result = self.run_build(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            html = (root / 'site/changelog.html').read_text()
            self.assertIn('id="top-2"', html)
            self.assertIn('href="changelog.html#top-2"', html)


class MarkdownTest(unittest.TestCase):
    def render(self, text):
        return renderer().render_markdown(text, 'fixture.md')

    def test_subset_and_escaping(self):
        html = self.render('# A & B!\n\nText < 3 & "quoted"; `a < b & c` and **bold** and *emphasis*. '
                           '[a *label*](https://github.com/vaiev/wouf?q=a&b=c).\n\n'
                           '- first\n- second\n  - nested\n\n3. third\n4. fourth\n\n'
                           '> A quote.\n>\n> Another paragraph.\n\n```python\n<x>&"\n```\n')
        for fragment in ('A &amp; B!', 'Text &lt; 3 &amp;', '<code>a &lt; b &amp; c</code>',
                         '<strong>bold</strong>', '<em>emphasis</em>', 'q=a&amp;b=c',
                         '<ul>', '<ol start="3">', '<blockquote>', '&lt;x&gt;&amp;&quot;'):
            self.assertIn(fragment, html)

    def test_punctuation_and_balanced_urls(self):
        html = self.render('Words_with_underscores; escaped \\*star\\*. '
                           '[link](https://github.com/a_(b)). <https://github.com/vaiev/wouf>\n')
        self.assertIn('Words_with_underscores; escaped *star*.', html)
        self.assertIn('href="https://github.com/a_(b)"', html)
        self.assertNotIn('<em>', html)

    def test_heading_ids_do_not_collide_with_suffixes_or_the_shell(self):
        html = self.render('# Top\n\n## Repeat\n\n## Repeat\n\n## Repeat-2\n')
        ids = re.findall(r'\bid="([^"]+)"', html)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertNotIn('top', ids)

    def test_invalid_markup_has_file_and_line(self):
        invalid = ('<script>alert(1)</script>', '<!-- comment -->', '[bad](javascript:alert(1))',
                   '[bad](data:text/html,x)', '[bad](//evil.example/x)', '[bad](java%73cript:x)',
                   '![image](image.png)', '[ref][target]', '~~strike~~', '*unclosed', '`unclosed',
                   '| table |', '---', '    indented code', '- [ ] task', '```python\nunclosed',
                   'A title\n=======', 'A | B\n--- | ---',
                   '[link](https://github.com "title")', 'line break\\\nnext line')
        for source in invalid:
            with self.subTest(source=source):
                with self.assertRaisesRegex(ValueError, r'fixture.md:3:'):
                    self.render('# Title\n\n' + source + '\n')


class GeneratedSiteTest(unittest.TestCase):
    def test_header_docs_is_current_only_on_docs_page(self):
        from test_site import tree
        for name in GENERATED:
            with self.subTest(page=name):
                header = next(tree(name).all('header', 'site-bar'))
                links = list(header.all('a'))
                docs = [a for a in links if a.attrs.get('href') == 'docs.html']
                self.assertEqual(len(docs), 1)
                current = [a for a in links if a.attrs.get('aria-current') == 'page']
                self.assertEqual(current, docs if name == 'docs.html' else [])

    def test_isolation_claims_qualify_sandbox_and_host_modes(self):
        from test_site import tree
        qualification = ("Docker, Podman and bubblewrap runs stage inputs and a repository copy into a separate workspace; "
                         "host mode runs without a sandbox and can access the host under the user's permissions.")
        for name in ('overview.md', 'safety.md'):
            with self.subTest(source=name):
                self.assertIn(qualification, (ROOT / 'docs/guide' / name).read_text())
        text = tree('docs.html').text()
        self.assertGreaterEqual(text.count(qualification), 2)
        for claim in ('nothing of your computer', 'nothing of your project folder or your computer is mounted'):
            self.assertNotIn(claim, text)

    def test_privacy_explains_cloud_local_and_other_services(self):
        from test_site import tree
        section = next(tree('docs.html').all('section', 'docs-privacy'))
        choices = [li.text() for li in section.all('li')]
        self.assertEqual(len(choices), 2)
        self.assertIn('Cloud model:', choices[0])
        self.assertIn('sends the content it uses to its model provider', choices[0])
        self.assertIn('An allowed network destination can receive data.', choices[0])
        self.assertIn('Compatible local model:', choices[1])
        self.assertIn('model server you configure', choices[1])
        self.assertIn('Verify model support, authentication, tool use and network reachability', choices[1])
        self.assertIn('including Git hosting and external engines, may still receive data', choices[1])
        self.assertIn('Host mode runs under your user permissions without a sandbox.', section.text())

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for the navigation simulation')
    def test_navigation_scroll_history_deep_links_disclosures_and_no_observer(self):
        script = r"""
const assert = require('assert');
const events = {}, frames = [];
global.addEventListener = (name, fn) => (events[name] ||= []).push(fn);
global.dispatchEvent = event => (events[event.type] || []).forEach(fn => fn(event));
global.Event = class { constructor(type) { this.type = type; } };
const fire = name => dispatchEvent(new Event(name));
global.requestAnimationFrame = fn => frames.push(fn);
const flush = () => { let n = 0; while (frames.length) { assert.ok(n++ < 100); frames.shift()(); } };
let y = 0;
global.innerHeight = 800;
Object.defineProperty(global, 'scrollY', {get: () => y});
global.matchMedia = () => ({matches: true}); // reduced motion; no IntersectionObserver
const el = extra => Object.assign({attrs: {}, handlers: {}, parentElement: null,
  addEventListener(n, fn) { this.handlers[n] = fn; },
  getAttribute(n) { return this.attrs[n] ?? null; },
  setAttribute(n, v) { this.attrs[n] = v; }, removeAttribute(n) { delete this.attrs[n]; },
  toggleAttribute(n, on) { if (on) this.attrs[n] = ''; else delete this.attrs[n]; }}, extra);
const ids = ['first', 'second', 'third', 'last'];
const heads = Object.fromEntries(ids.map((id, i) => [id, el({id,
  getBoundingClientRect: () => ({top: 600 + i * 800 - y})})]));
const links = ids.map((id, i) => el({attrs: {href: '#' + id}, offsetLeft: 0, offsetWidth: 100,
  getBoundingClientRect: () => ({top: 104 + i * 140 - nav.scrollTop, bottom: 148 + i * 140 - nav.scrollTop})}));
const nav = el({scrollWidth: 400, clientWidth: 400, scrollLeft: 0, scrollTop: 0, scrollHeight: 600, clientHeight: 300,
  scrollTo() {}, querySelectorAll: () => links, contains: () => false,
  querySelector: () => links.find(a => a.attrs['aria-current'] === 'location'),
  getBoundingClientRect: () => ({top: 104, bottom: 404})});
const details = el({tagName: 'DETAILS', open: false});
const hidden = el({parentElement: details, scrollIntoView() { y = 600 + 2 * 800 - 104; }});
heads.hidden = hidden;
global.location = {hash: '#hidden'};
global.document = {documentElement: {dataset: {}, scrollHeight: 5000, hasAttribute: () => false},
  querySelector: s => s === '.docs-nav' ? nav : null,
  querySelectorAll: s => s === '.docs-details' ? [details] : [],
  getElementById: id => heads[id] || null, activeElement: null};
require(process.argv[1]);
require(process.argv[2]);
flush();
const current = () => links.filter(a => a.attrs['aria-current'] === 'location').map(a => a.attrs.href);
const at = top => { y = top; fire('scroll'); flush(); return current(); };
assert.ok(details.open); assert.deepStrictEqual(current(), ['#third']); // initial hidden deep link
assert.deepStrictEqual(at(0), ['#first']);
assert.deepStrictEqual(at(1400 - 104), ['#second']);
assert.deepStrictEqual(at(2200 - 104), ['#third']);
assert.deepStrictEqual(at(1400 - 200), ['#first']); // back up
assert.deepStrictEqual(at(4200), ['#last']); // bottom, including footer
assert.ok(nav.scrollTop > 0); // the current entry in a long desktop nav stays visible
location.hash = '#second'; y = 1400 - 104; fire('hashchange'); flush();
assert.deepStrictEqual(current(), ['#second']);
y = 600 - 104; fire('popstate'); flush(); assert.deepStrictEqual(current(), ['#first']);
y = 2200 - 104; fire('pageshow'); flush(); assert.deepStrictEqual(current(), ['#third']);
y = 0; details.handlers.toggle(); flush(); assert.deepStrictEqual(current(), ['#first']);
"""
        result = subprocess.run([shutil.which('node'), '-e', script, str(ROOT / 'site/js/site.js'),
                                 str(ROOT / 'site/js/docs.js')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unique_ids_parts_compatibility_and_nav(self):
        from test_site import Page, Tree
        html = (ROOT / 'site/docs.html').read_text()
        parser = Tree()
        parser.feed(html)
        nav = next(parser.root.all('nav', 'docs-nav'))
        groups = list(nav.all('div', 'docs-nav-group'))
        self.assertEqual(len(groups), 6)
        self.assertEqual([next(g.all('p')).text() for g in groups],
                         ['Overview', 'Method', 'Safety', 'Getting started', 'Limits', 'Sources'])
        ids = re.findall(r'\bid="([^"]+)"', html)
        self.assertEqual(len(ids), len(set(ids)))
        for alias in ('what', 'quickstart', 'how', 'where', 'setup', 'use', 'yours', 'maintain', 'limits', 'license'):
            self.assertEqual(ids.count(alias), 1, alias)
        targets = [a.attrs['href'][1:] for a in nav.all('a')]
        self.assertEqual(len(targets), len(set(targets)))
        self.assertTrue(set(targets) <= set(ids))
        self.assertEqual([n.text() for n in parser.root.all('h2')],
                         ['Overview', 'Method', 'Safety', 'Getting started', 'Limits', 'Sources'])
        for name in GENERATED:
            page = Page()
            content = (ROOT / 'site' / name).read_text()
            page.feed(content)
            self.assertEqual(page.h1, 1, name)
            self.assertNotIn('Content coming soon.', content)
            self.assertIn('css/docs.css', content)
            self.assertIn('js/site.js', content)
            self.assertIn('js/docs.js', content)

    def test_all_source_blocks_are_present_and_f19_additions_are_accessible(self):
        from test_site import Tree
        build = renderer()
        for name, destination in build.DESTINATIONS.items():
            source, page = Tree(), Tree()
            source.feed(build.render_markdown((ROOT / name).read_text(), name))
            page.feed((ROOT / 'site' / destination).read_text())
            text = page.root.text()
            for tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'pre'):
                for block in source.root.all(tag):
                    self.assertIn(block.text(), text, (name, tag))
        page = Tree()
        page.feed((ROOT / 'site/docs.html').read_text())
        self.assertEqual(len(list(page.root.all('dfn'))), 2)
        self.assertGreaterEqual(len(list(page.root.all('details', 'docs-details'))), 3)
        fork = next(page.root.all('div', 'docs-fork'))
        self.assertEqual([a.text() for a in fork.all('a')], ['Just get started', 'Understand and adapt'])
        copy = next(n for n in page.root.all() if n.attrs.get('id') == 'setup-line')
        source = Tree()
        source.feed(build.render_markdown((ROOT / 'docs/guide/getting-started.md').read_text()))
        self.assertEqual(copy.text(), next(source.root.all('blockquote')).text())
        self.assertEqual(len([n for n in page.root.all() if n.attrs.get('role') == 'status']), 1)
        self.assertIn('Privacy and where your data goes', page.root.text())

    def test_shared_site_rules_cover_generated_pages(self):
        # The existing checks already iterate every *.html and SITE_PAGES includes all three destinations.
        # Reuse the independent checks, including their single-brand rule and v8 exceptions, unchanged.
        from test_site import Site, SITE_PAGES
        self.assertTrue(set(GENERATED) <= set(SITE_PAGES))
        checks = ('test_every_relative_href_and_src_exists', 'test_every_url_in_the_css_exists',
                  'test_every_page_has_the_two_line_footer',
                  'test_the_only_absolute_hosts_are_github_wouf_dev_and_the_vaiev_footer_link',
                  'test_scripts_stylesheets_and_fonts_come_from_the_site',
                  'test_each_page_has_a_csp_one_h1_a_lang_a_title_and_no_form',
                  'test_the_folder_names_no_other_brand_and_not_the_companion',
                  'test_vendored_v8_digests')
        for name in checks:
            getattr(Site(name), name)()


if __name__ == '__main__':
    unittest.main()
