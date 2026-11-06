/* VAIEV UI v8 — page transitions. Load in <head> WITHOUT defer, so it listens before the first frame:
   <script src="…/js/vt.js"></script>. Pages then move like an app: the CSS (base.css) animates by direction,
   this script only says which direction a navigation has.

   data-vt="right"  the next page comes in from the right (into a post, a detail)
   data-vt="left"   it comes in from the left (back, or the composer, which lives left of the board)
   (none)           right within a site, a fade-through between sites
   data-morph="x"   the clicked element and the element with the same data-morph on the next page morph
                    into each other (a card into a hero, a post into its thread)
   Browser back and forward replay the inverse direction of how a page was entered.

   Between different origins browsers cannot run a view transition. There a link with data-morph and
   data-curtain="brand" morphs into a full-screen curtain in the next brand's colours before it leaves, and
   a page with <html data-arrive="vaiev.com …"> lifts the same curtain when it is opened from one of those hosts.
   No inline styles: names are set through CSSOM, which the CSP allows. */
(() => {
  const root = document.documentElement;
  const reduce = matchMedia('(prefers-reduced-motion: reduce)');
  const KEY = 'vv-vt', DIRS = 'vv-vt-dirs';
  const read = k => { try { return JSON.parse(sessionStorage.getItem(k)); } catch (_) { return null; } };
  const save = (k, v) => { try { sessionStorage.setItem(k, JSON.stringify(v)); } catch (_) { /* private mode */ } };
  const invert = t => (t === 'left' ? 'right' : t === 'right' ? 'left' : t);
  const folder = u => u.pathname.replace(/[^/]*$/, '');
  let clicked = null;

  // arriving from another VAIEV site: lift the curtain that site drew
  const from = document.referrer && new URL(document.referrer);
  if (from && from.origin !== location.origin && (root.dataset.arrive || '').split(/\s+/).includes(from.hostname) && !reduce.matches) {
    root.classList.add('vv-arrive');
    addEventListener('animationend', e => { if (e.animationName === 'curtain-lift') root.classList.remove('vv-arrive'); });
  }

  function remember(el, url) {
    if (url.origin !== location.origin) return false;
    const type = el.dataset.vt || (folder(url) === folder(location) ? 'right' : 'site');
    clicked = el.closest('[data-morph]');
    save(KEY, { type, morph: clicked ? clicked.dataset.morph : '' });
    return true;
  }

  addEventListener('click', e => {
    const a = e.target.closest && e.target.closest('a[href]');
    if (!a || e.defaultPrevented || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || a.target) return;
    const url = new URL(a.href, location.href);
    if (remember(a, url)) return;
    // another origin: morph into a curtain, then go
    const card = a.closest('[data-morph]');
    if (!card || !a.dataset.curtain || !document.startViewTransition || reduce.matches) return;
    e.preventDefault();
    const curtain = document.createElement('div');
    curtain.className = 'curtain';
    curtain.dataset.to = a.dataset.curtain;
    card.style.viewTransitionName = 'morph';
    const vt = document.startViewTransition(() => {
      card.style.viewTransitionName = '';
      curtain.style.viewTransitionName = 'morph';
      document.body.append(curtain);
    });
    vt.finished.finally(() => { location.href = url.href; });
  });
  addEventListener('submit', e => { if (!e.defaultPrevented) remember(e.target, new URL(e.target.action, location.href)); });

  addEventListener('pageswap', e => {
    if (!e.viewTransition) return;
    const s = read(KEY);
    if (s && s.type) e.viewTransition.types.add(s.type);
    if (clicked) clicked.style.viewTransitionName = 'morph';
  });

  addEventListener('pagereveal', e => {
    if (!e.viewTransition) return;
    const act = window.navigation && navigation.activation;
    const dirs = read(DIRS) || {};
    let s = read(KEY);
    try { sessionStorage.removeItem(KEY); } catch (_) { /* nothing stored */ }
    if (act && act.navigationType === 'reload') { e.viewTransition.skipTransition(); return; }
    if (act && act.navigationType === 'traverse' && act.from) {
      const back = act.from.index > act.entry.index;
      s = { type: back ? invert(dirs[act.from.url] || 'right') : dirs[act.entry.url] || 'right' };
    } else if (s) {
      dirs[location.href] = s.type;
      save(DIRS, dirs);
    }
    if (!s) return;
    e.viewTransition.types.add(s.type);
    if (s.morph) {
      const target = document.querySelector(`[data-morph="${CSS.escape(s.morph)}"]`);
      if (target) {
        target.style.viewTransitionName = 'morph';
        e.viewTransition.finished.finally(() => { target.style.viewTransitionName = ''; });
      }
    }
  });
})();
