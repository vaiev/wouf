# wouf.dev

The static files of wouf.dev, in version 8.0.0 of the VAIEV design system. Every page is plain HTML with its CSS, scripts, fonts and icons next to it; there is no build step, no form, no tracking, and nothing loads from another host.

## Preview

```bash
python3 -m http.server -d site
```

Then open `http://localhost:8000/`. `index.html` and `docs.html` also open straight from the file system. `404.html` links from the site root (`/css/…`), so it shows its styles only when a server returns it, at any depth.

## Where it comes from

1. The shared CSS and scripts (`css/materials.css`, `css/components.css`, `css/patterns.css`, `js/vt.js`, `js/vaiev.js`) are the supplied VAIEV UI v8 snapshot verbatim.
2. Single-brand trimming on `css/base.css` and `css/tokens.css`: in `css/tokens.css` only the base tokens and the three `wouf` blocks stay; the token blocks and font variables of the other brands, and their curtain colours and assets in `css/base.css` (and the brand list in its comment), are removed.
3. The WOUF font subset is in `css/site-fonts.css`; site-specific styles and behavior live in `css/site.css` and `js/site.js`. The local sprite (`icons.svg`) retains the GitHub mark. `examples.html`, `changelog.html` and `contribute.html` are interim local destinations.
4. `css/site.css` only places, spaces and extends v8 parts (it repeats none of their rules): the 1200px frame with 16/24px gutters; the site bar (from 860px at the top and as wide as the footer's content, v8's 560px fold while reading down; on phones v8's bottom bar with Home, Examples and Docs, always visible, above the safe area); the home hero, the only full-screen section, with its own sky; the demo; flat glass bands in one colour with hairline structure instead of cards; and the section lock: where scroll timelines exist and motion is welcome, each band but the last sticks once it is fully in view (a band taller than the window only once its end is in view) and recedes (scale, dim, 3px blur) as the next one covers it. Keyboard focus in the deck releases the lock; without scroll timelines, without the script or with reduced motion the bands are a still column.
5. `js/site.js` adds what v8 leaves to a site: the bar's top/down/up/bottom states on top of v8's scroll module, the demo (any `[data-demo]` figure: dialogs as `ol.demo-dialog[data-scenario]` with `li.msg` lines, chips `button[data-scenario]`, `[data-demo-toggle]`, `[data-demo-restart]`, `[data-demo-typed]`; state in `data-active`, `data-shown`, `data-ready`, `data-still`), the lock's `--lock-top` per band (CSSOM), the current docs section (`aria-current="location"`), and copying: `index.html` starts v8's modules itself (`data-vaiev-manual`) without v8's copy module, so each press reports its own clipboard result.

## Text changes

Every claim on `index.html` and `docs.html` is backed by `README.md` or `INSTALL.md` at the release tag. Lines that went further than those files were changed:

| Page | Before (sample) | After | Source |
|---|---|---|---|
| `index.html` description, `og:description` | "… Open source, Apache-2.0." | "… Licensed under Apache-2.0." | README.md:124 |
| `index.html` hero pill | "Open source · Apache-2.0 · no harness lock-in" | "Apache-2.0 · any agent CLI that reads AGENTS.md" | README.md:7, README.md:124 |
| `index.html` terminal example | "result comes back as a git branch" | "code comes back as a git branch" | README.md:42 |
| `index.html` "How it works", step 5 | "Decide takes the hard calls the lead hands it, and the lead tells you afterwards." | "Decide: with `autonomy = auto` the lead takes the hard calls with the decide bot and tells you afterwards." | README.md:43 |
| `index.html` card "A whole task can run on its own" | "a gate without any model" | "a pre-gate without any model" | README.md:44 |
| `index.html` Quickstart, the sentence to copy | the technical sentence only | the Quickstart sentence with "I am not technical: explain in plain words and do the steps for me." | README.md:15 |
| `index.html` CLI band label | "No harness lock-in" | "Any agent CLI" | README.md:7 |
| `index.html` Limits, network card | "Block your local network and the provider's metadata service …" | "… so a bot can reach your local network and, on a cloud VM, the provider's metadata service. Block those …" | README.md:105 |
| `index.html` "Built with WOUF" | "It builds (another product), and it builds itself." | "It builds itself." (the other product's name breaks the single-brand rule) | README.md:90 |
| `index.html`, `docs.html` footer | "… · a VAIEV project", linked to vaiev.com | "Copyright 2026 VAIEV · Apache-2.0" and "Initialized by" with the VAIEV symbol, linked to vaiev.com (the only hosts are github.com, wouf.dev and that footer link) | README.md:124 |
| `docs.html` "Remove" | "Ask the lead. It removes every bot …" | "The lead asks you first. Then it removes every bot …" | INSTALL.md:87 |
| `docs.html` "Licence" | "Source, issues and contributing" | "Source and contributing" | README.md:120 |
| `index.html`, `docs.html` `<html>` | `data-arrive="vaiev.com"` | removed (no arrival from another brand's site) | none, not text |

## Checks

`tests/test_site.py` (standard library) checks that every relative `href` and `src` and every `url()` in `css/` exists, that the only absolute hosts are github.com and wouf.dev, with vaiev.com allowed only as the `href` of the footer link, that scripts, stylesheets and fonts come from this folder, that every page has a CSP, one `h1`, a `lang` and a title and no form, that no other brand is named, and that `data-brand="wouf"` keeps every token the CSS uses.

```bash
python3 -m unittest discover -s tests -p 'test_site*.py'
```
